"""C1J.11 frame-cache identity sidecar writer.

This module is dependency-light so it can run inside the existing media-worker
image. It performs read-only Redis XREVRANGE calls, writes sidecar files under
an existing evidence directory, and never updates PostgreSQL or production
Redis streams.
"""

from __future__ import annotations

import copy
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.frame_annotation_event_window import (
    extract_evidence_event_anchor,
    select_frame_annotation_event_window,
)
from app.frame_annotation_shadow_builder import (
    build_shadow_annotations_from_frame_cache,
    ensure_person_context_annotations,
)
from app.canonical_timeline import (
    PTS_TIME_BASE_SECONDS,
    align_annotations_to_clip_metadata,
    load_metadata_rows,
)
from app.production_sidecar_policy import should_attempt_sidecar


PHASE = "C1J.11"
SCHEMA_VERSION = "1.0"
TIMELINE_DOMAIN_FINAL_CANONICAL_CLIP = "final_canonical_clip"
SIDECAR_TYPE_PRODUCTION = "production"
FORBIDDEN_VECTOR_FIELDS = {
    "embedding",
    "embedding_vector",
    "vector",
    "features",
    "feature",
    "embedding_values",
}
FORBIDDEN_IMAGE_FIELDS = {
    "crop_bytes",
    "image_bytes",
    "raw_frame",
    "jpeg",
    "png",
    "base64_image",
    "frame_bytes",
}


class RedisStreamReadClient:
    """Minimal Redis RESP client for XREVRANGE only."""

    def __init__(self, redis_url: str, *, timeout: float = 2.0) -> None:
        parsed = urlparse(redis_url)
        self.host = parsed.hostname or "redis"
        self.port = parsed.port or 6379
        self.timeout = float(timeout)

    def xrevrange(
        self,
        name: str,
        max: str = "+",
        min: str = "-",
        count: int | None = None,
    ) -> list[Any]:
        command: list[Any] = ["XREVRANGE", name, max, min]
        if count is not None:
            command.extend(["COUNT", int(count)])
        payload = _encode_resp(command)
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall(payload)
            reader = _RespReader(sock)
            value = reader.read()
        return value if isinstance(value, list) else []


def write_frame_cache_identity_sidecar(
    *,
    event: dict[str, Any],
    evidence_dir: str,
    raw_clip_path: str | None,
    metadata_path: str | None,
    redis_client: Any | None,
    config: dict[str, Any],
    state: Any | None = None,
    final_clip_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Write C1J.11 frame-cache identity sidecar files when enabled."""

    event_copy = copy.deepcopy(event) if isinstance(event, dict) else {}
    out_dir = Path(evidence_dir)
    annotations_name = str(config.get("annotations_filename") or "annotations.frame_cache.identity.jsonl")
    summary_name = str(config.get("summary_filename") or "summary.frame_cache.identity.json")
    annotations_path = out_dir / annotations_name
    summary_path = out_dir / summary_name
    old_annotations_path = out_dir / "annotations.jsonl"
    old_summary_path = out_dir / "summary.json"

    allowed, decision = should_attempt_sidecar(event_copy, config, state)
    base_summary = _base_summary(
        event_copy,
        config=config,
        decision=decision,
        evidence_dir=str(out_dir),
        raw_clip_path=raw_clip_path,
        metadata_path=metadata_path,
        annotations_path=str(annotations_path),
        summary_path=str(summary_path),
        old_annotations_path=str(old_annotations_path),
        old_summary_path=str(old_summary_path),
    )
    if not allowed:
        summary = {
            **base_summary,
            "annotation_status": "disabled" if decision.get("reason") == "disabled" else "skipped",
            "skip_reason": decision.get("reason"),
        }
        return summary, {"written": False, "annotations_path": None, "summary_path": None}

    if config.get("write_mode") != "sidecar_only":
        summary = {
            **base_summary,
            "annotation_status": "skipped",
            "skip_reason": "invalid_write_mode",
            "production_replacement": True,
            "error": "FRAME_CACHE_SIDECAR_WRITE_MODE must be sidecar_only",
        }
        _write_summary(summary_path, summary)
        return summary, {"written": False, "annotations_path": None, "summary_path": str(summary_path)}

    if annotations_path.name == "annotations.jsonl":
        summary = {
            **base_summary,
            "annotation_status": "skipped",
            "skip_reason": "sidecar_annotations_path_matches_old_annotations",
            "production_replacement": True,
            "error": "sidecar output would replace annotations.jsonl",
        }
        _write_summary(summary_path, summary)
        return summary, {"written": False, "annotations_path": None, "summary_path": str(summary_path)}

    try:
        messages, reader_summary = _read_frame_annotations(
            redis_client=redis_client,
            config=config,
            event=event_copy,
        )
        pre_seconds = float(config.get("pre_seconds") or 5.0)
        post_seconds = float(config.get("post_seconds") or 5.0)
        identity_annotations, build_summary = build_sidecar_identity_annotations(
            event=event_copy,
            frame_messages=messages,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
            max_frames=int(config.get("max_frames") or 300),
        )
        clip_timeline_summary = _align_sidecar_annotations_to_metadata(
            identity_annotations,
            metadata_path=metadata_path,
        )
        aligned_annotations = clip_timeline_summary.pop("_annotations")
        freshness_summary = _empty_freshness_guard_summary(
            annotations=aligned_annotations,
            event=event_copy,
            config=config,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
        )
        if clip_timeline_summary.get("enabled") is True:
            aligned_annotations, freshness_summary = apply_frame_cache_freshness_guard(
                aligned_annotations,
                event=event_copy,
                config=config,
                pre_seconds=pre_seconds,
                post_seconds=post_seconds,
            )
            identity_annotations, dropped_annotations, filter_summary = filter_production_sidecar_rows(
                aligned_annotations
            )
        else:
            identity_annotations = aligned_annotations
            dropped_annotations = []
            filter_summary = {
                "rows_total_input": len(aligned_annotations),
                "rows_written": len(aligned_annotations),
                "rows_non_displayable": sum(1 for row in aligned_annotations if row.get("displayable") is False),
                "rows_dropped_total": 0,
                "rows_dropped_out_of_window": 0,
                "rows_dropped_missing": 0,
                **freshness_summary,
            }
        counts = _count_identity_annotations(identity_annotations)
        clip_timeline_summary = {**clip_timeline_summary, **freshness_summary, **filter_summary}
        production_contract = _production_sidecar_contract_summary(
            event=event_copy,
            annotations=identity_annotations,
            identity_counts=counts,
            clip_timeline_summary=clip_timeline_summary,
            raw_clip_path=raw_clip_path,
            metadata_path=metadata_path,
            final_clip_context=final_clip_context,
            config=config,
        )
        annotation_status = _annotation_status_from_counts(
            event_window_messages=int(build_summary.get("event_window_messages") or 0),
            trigger_known_face_present=bool(counts["trigger_known_face_present"]),
            require_trigger_face=bool(config.get("require_trigger_face")),
            cache_stale_or_epoch_mismatch=bool(
                int(clip_timeline_summary.get("rows_rejected_stale_cache") or 0)
                or int(clip_timeline_summary.get("rows_rejected_epoch_mismatch") or 0)
                or int(clip_timeline_summary.get("rows_rejected_pts_non_unique") or 0)
                or clip_timeline_summary.get("trigger_row_stale_or_epoch_mismatch") is True
            ),
        )
        summary = {
            **base_summary,
            "frame_cache_reader_summary": reader_summary,
            "event_anchor": build_summary.get("event_anchor"),
            "event_anchor_summary": build_summary.get("event_anchor_summary"),
            "anchor_found_by": build_summary.get("anchor_found_by"),
            "event_window_messages": int(build_summary.get("event_window_messages") or 0),
            "annotations_input": int(filter_summary.get("rows_total_input") or 0),
            "annotations_written": len(identity_annotations),
            "known_face_count": counts["known_face_count"],
            "unknown_face_count": counts["unknown_face_count"],
            "trigger_known_face_present": counts["trigger_known_face_present"],
            "annotation_status": annotation_status,
            "embedding_vectors_in_output": _count_forbidden(identity_annotations, FORBIDDEN_VECTOR_FIELDS),
            "image_bytes_in_output": _count_forbidden(identity_annotations, FORBIDDEN_IMAGE_FIELDS),
            "identity_source": build_summary.get("identity_source"),
            "clip_timeline_alignment": clip_timeline_summary,
            **production_contract,
            "error": None,
        }
        _write_jsonl(annotations_path, identity_annotations)
        if dropped_annotations:
            _write_jsonl(_dropped_debug_path(annotations_path), dropped_annotations)
        _write_summary(summary_path, summary)
        if state is not None:
            state.sidecar_written += 1
            if annotation_status == "missing_frame_metadata":
                state.sidecar_missing_frame_metadata += 1
            if annotation_status == "missing_trigger_face_annotation":
                state.sidecar_missing_trigger_face += 1
        return summary, {
            "written": True,
            "annotations_path": str(annotations_path),
            "summary_path": str(summary_path),
        }
    except Exception as exc:
        if state is not None:
            state.sidecar_errors += 1
        summary = {
            **base_summary,
            "annotation_status": "partial",
            "skip_reason": None,
            "error": f"{type(exc).__name__}:{exc}",
            "fail_open": bool(config.get("fail_open", True)),
        }
        _write_summary(summary_path, summary)
        if not config.get("fail_open", True):
            raise
        return summary, {
            "written": False,
            "annotations_path": None,
            "summary_path": str(summary_path),
        }


def build_sidecar_identity_annotations(
    *,
    event: dict[str, Any],
    frame_messages: list[dict[str, Any]],
    pre_seconds: float = 5.0,
    post_seconds: float = 5.0,
    max_frames: int = 300,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build sidecar annotations from frame cache messages and event payload."""

    anchor, anchor_summary = extract_evidence_event_anchor(event)
    source_id = anchor.get("source_id") if anchor else None
    camera_id = anchor.get("camera_id") if anchor else None
    source_observation_id = anchor.get("source_observation_id") if anchor else None
    if not anchor or not source_id or not camera_id:
        return [], {
            "event_anchor": anchor,
            "event_anchor_summary": anchor_summary,
            "anchor_found_by": "missing",
            "event_window_messages": 0,
            "identity_source": "none",
        }

    window_messages, window_summary = select_frame_annotation_event_window(
        copy.deepcopy(frame_messages),
        source_id=str(source_id),
        camera_id=str(camera_id),
        anchor_frame_pts=anchor.get("frame_pts"),
        anchor_frame_uuid=anchor.get("frame_uuid"),
        anchor_source_observation_id=source_observation_id,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
        max_frames=max_frames,
    )
    frame_cache_annotations, frame_cache_summary = _frame_cache_annotations(
        window_messages,
        source_id=str(source_id),
        camera_id=str(camera_id),
    )
    patch = _identity_patch_from_event_payload(event, anchor=anchor)
    identity_annotations = _apply_event_payload_identity(
        frame_cache_annotations,
        patch,
        trigger_source_observation_id=source_observation_id,
    )
    return identity_annotations, {
        "event_anchor": anchor,
        "event_anchor_summary": anchor_summary,
        "window_summary": window_summary,
        "frame_cache_summary": frame_cache_summary,
        "anchor_found_by": window_summary.get("anchor_found_by"),
        "event_window_messages": int(window_summary.get("window_messages") or 0),
        "identity_source": "event_payload_bridge" if patch else "none",
    }


def _align_sidecar_annotations_to_metadata(
    annotations: list[dict[str, Any]],
    *,
    metadata_path: str | None,
) -> dict[str, Any]:
    path = Path(str(metadata_path or ""))
    if not metadata_path or not path.is_file():
        return {
            "_annotations": annotations,
            "enabled": False,
            "status": "metadata_missing",
            "metadata_path": metadata_path,
            "annotations_aligned": 0,
            "annotations_unmatched": len(annotations),
        }
    rows = load_metadata_rows(path)
    aligned, summary = align_annotations_to_clip_metadata(annotations, rows)
    aligned = ensure_person_context_annotations(aligned)
    return {
        **summary,
        "_annotations": aligned,
        "metadata_path": str(path),
    }


def _read_frame_annotations(
    *,
    redis_client: Any | None,
    config: dict[str, Any],
    event: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    anchor, _summary = extract_evidence_event_anchor(event)
    source_id = anchor.get("source_id") if anchor else None
    camera_id = anchor.get("camera_id") if anchor else None
    stream_name = str(config.get("stream_name") or "security.frame_annotations")
    lookback_count = int(config.get("lookback_count") or 10000)
    max_scan = int(config.get("max_scan") or 20000)
    count = min(lookback_count, max_scan)
    client = redis_client or RedisStreamReadClient(str(config.get("redis_url") or "redis://redis:6379/0"))
    entries = client.xrevrange(stream_name, max="+", min="-", count=count)
    messages: list[dict[str, Any]] = []
    summary = {
        "stream_name": stream_name,
        "entries_scanned": 0,
        "messages_valid": 0,
        "messages_invalid": 0,
        "messages_filtered_source": 0,
        "messages_filtered_camera": 0,
        "earliest_frame_pts": None,
        "latest_frame_pts": None,
        "lookback_count": lookback_count,
        "max_scan": max_scan,
        "consumer_group_used": False,
        "stream_mutated": False,
        "ack_used": False,
        "xdel_used": False,
    }
    for index, entry in enumerate(list(entries)[:count]):
        summary["entries_scanned"] += 1
        stream_id, fields = _split_entry(entry)
        message = _message_from_fields(fields)
        if not isinstance(message, dict):
            summary["messages_invalid"] += 1
            continue
        if source_id is not None and message.get("source_id") != source_id:
            summary["messages_filtered_source"] += 1
            continue
        if camera_id is not None and message.get("camera_id") != camera_id:
            summary["messages_filtered_camera"] += 1
            continue
        normalized = copy.deepcopy(message)
        normalized["_stream_id"] = stream_id
        normalized["_stream_order"] = index
        messages.append(normalized)

    messages.sort(key=lambda item: (item.get("frame_pts") is None, int(item.get("frame_pts") or 0), str(item.get("frame_uuid") or "")))
    pts_values = [int(item["frame_pts"]) for item in messages if isinstance(item.get("frame_pts"), int)]
    summary["messages_valid"] = len(messages)
    if pts_values:
        summary["earliest_frame_pts"] = min(pts_values)
        summary["latest_frame_pts"] = max(pts_values)
    return messages, summary


def _frame_cache_annotations(
    window_messages: list[dict[str, Any]],
    *,
    source_id: str,
    camera_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not window_messages:
        return [], {
            "annotation_status": "missing_frame_metadata",
            "annotations_written": 0,
        }
    clip_frames = []
    timestamps = [m.get("timestamp_ms") for m in window_messages if isinstance(m.get("timestamp_ms"), int)]
    first_ts = min(timestamps) if timestamps else None
    for index, message in enumerate(window_messages):
        timestamp_ms = message.get("timestamp_ms")
        t_ms = int(timestamp_ms) - int(first_ts) if isinstance(timestamp_ms, int) and isinstance(first_ts, int) else index
        clip_frames.append(
            {
                "clip_frame_index": index,
                "t_ms": max(t_ms, 0),
                "frame_uuid": message.get("frame_uuid"),
                "frame_pts": message.get("frame_pts"),
                "timestamp_ms": timestamp_ms,
            }
        )
    return build_shadow_annotations_from_frame_cache(
        clip_frames,
        window_messages,
        source_id=source_id,
        camera_id=camera_id,
        min_hit_ratio=0.2,
    )


def _identity_patch_from_event_payload(
    event: dict[str, Any],
    *,
    anchor: dict[str, Any],
) -> dict[str, Any] | None:
    if not anchor.get("source_observation_id"):
        return None
    if not (
        anchor.get("person_id") is not None
        or anchor.get("external_person_id")
        or anchor.get("display_name")
    ):
        return None
    return {
        "source_id": anchor.get("source_id"),
        "camera_id": anchor.get("camera_id"),
        "source_observation_id": anchor.get("source_observation_id"),
        "person_id": anchor.get("person_id"),
        "external_person_id": anchor.get("external_person_id"),
        "display_name": anchor.get("display_name"),
        "similarity": anchor.get("similarity"),
        "threshold": anchor.get("threshold"),
        "identity_source": "event_payload_bridge",
    }


def _apply_event_payload_identity(
    annotations: list[dict[str, Any]],
    patch: dict[str, Any] | None,
    *,
    trigger_source_observation_id: str | None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in copy.deepcopy(annotations):
        if not isinstance(row, dict):
            continue
        if (
            patch
            and row.get("object_type") == "face"
            and row.get("source_observation_id") == patch.get("source_observation_id")
        ):
            label = row.get("label") if isinstance(row.get("label"), dict) else {}
            label["kind"] = "known_face"
            label["person_id"] = patch.get("person_id")
            label["external_person_id"] = patch.get("external_person_id")
            display = patch.get("display_name") or patch.get("external_person_id") or patch.get("person_id")
            if display is not None:
                label["display_name"] = str(display)
            label["similarity"] = patch.get("similarity")
            label["threshold"] = patch.get("threshold")
            row["label"] = label
            row["identity_source"] = "event_payload_bridge"
            row["source"] = "frame_annotation_cache + event_payload_identity_bridge"
            if trigger_source_observation_id and row.get("source_observation_id") == trigger_source_observation_id:
                row["annotation_role"] = "watchlist_trigger_face"
        output.append(_sanitize_value(row))
    return output


def _count_identity_annotations(annotations: list[dict[str, Any]]) -> dict[str, Any]:
    known = 0
    unknown = 0
    trigger = False
    for row in annotations:
        if row.get("object_type") != "face":
            continue
        label = row.get("label") if isinstance(row.get("label"), dict) else {}
        if label.get("kind") == "known_face":
            known += 1
            if row.get("annotation_role") == "watchlist_trigger_face":
                trigger = True
        elif label.get("kind") == "unknown_face":
            unknown += 1
    return {
        "known_face_count": known,
        "unknown_face_count": unknown,
        "trigger_known_face_present": trigger,
    }


def _production_sidecar_contract_summary(
    *,
    event: dict[str, Any],
    annotations: list[dict[str, Any]],
    identity_counts: dict[str, Any],
    clip_timeline_summary: dict[str, Any],
    raw_clip_path: str | None,
    metadata_path: str | None,
    final_clip_context: dict[str, Any] | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    rows_written = len(annotations)
    rows_total = rows_written
    rows_total_input = int(
        clip_timeline_summary.get("rows_total_input")
        or clip_timeline_summary.get("annotations_input")
        or (
            int(clip_timeline_summary.get("annotations_aligned") or rows_written)
            + int(clip_timeline_summary.get("annotations_unmatched") or 0)
        )
    )
    rows_displayable = sum(1 for row in annotations if row.get("displayable") is not False)
    rows_non_displayable = sum(1 for row in annotations if row.get("displayable") is False)
    rows_unmatched = int(clip_timeline_summary.get("annotations_unmatched") or 0)
    rows_out_of_window = int(clip_timeline_summary.get("annotations_out_of_window") or 0)
    match_distribution = clip_timeline_summary.get("clip_timeline_match_distribution")
    if not isinstance(match_distribution, dict):
        match_distribution = {}

    written_contract = _production_written_rows_contract(annotations)
    stale_timing_rows = int(written_contract.get("stale_timing_rows") or 0)
    rows_rejected_stale_cache = int(clip_timeline_summary.get("rows_rejected_stale_cache") or 0)
    rows_rejected_epoch_mismatch = int(clip_timeline_summary.get("rows_rejected_epoch_mismatch") or 0)
    rows_rejected_pts_non_unique = int(clip_timeline_summary.get("rows_rejected_pts_non_unique") or 0)
    rows_matched_by_frame_uuid = int(
        clip_timeline_summary.get("rows_matched_by_frame_uuid")
        if clip_timeline_summary.get("rows_matched_by_frame_uuid") is not None
        else int(clip_timeline_summary.get("annotations_matched_frame_uuid") or 0)
    )
    rows_matched_by_pts_fallback = int(
        clip_timeline_summary.get("rows_matched_by_pts_fallback")
        if clip_timeline_summary.get("rows_matched_by_pts_fallback") is not None
        else (
            int(clip_timeline_summary.get("annotations_matched_frame_pts_exact") or 0)
            + int(clip_timeline_summary.get("annotations_matched_frame_pts_nearest") or 0)
        )
    )
    trigger_stale_or_epoch_mismatch = (
        clip_timeline_summary.get("trigger_row_stale_or_epoch_mismatch") is True
    )
    clip_duration_seconds = _clip_duration_seconds(
        final_clip_context=final_clip_context,
        clip_timeline_summary=clip_timeline_summary,
    )
    event_metrics = _event_clip_metrics(
        event=event,
        final_clip_context=final_clip_context,
        clip_timeline_summary=clip_timeline_summary,
        clip_duration_seconds=clip_duration_seconds,
    )
    identity_scope_status = _identity_scope_status(annotations, identity_counts)
    vector_count = _count_forbidden(annotations, FORBIDDEN_VECTOR_FIELDS)
    image_count = _count_forbidden(annotations, FORBIDDEN_IMAGE_FIELDS)

    min_duration = _float_config(config, "canonical_min_duration_seconds", 9.25)
    max_duration = _float_config(config, "canonical_max_duration_seconds", 10.75)
    min_event_t = _float_config(config, "canonical_event_min_t_s", 4.0)
    max_event_t = _float_config(config, "canonical_event_max_t_s", 6.5)
    max_event_ratio = _float_config(config, "canonical_event_max_position_ratio", 0.70)
    require_event_centered = bool(config.get("require_event_centered", True))

    duration_ok = (
        clip_duration_seconds is not None
        and min_duration <= float(clip_duration_seconds) <= max_duration
    )
    event_projected = event_metrics["event_projected_t_s"]
    event_center_ok = (
        event_projected is not None
        and min_event_t <= float(event_projected) <= max_event_t
    )
    event_ratio = event_metrics["event_position_ratio"]
    event_ratio_ok = event_ratio is not None and float(event_ratio) <= max_event_ratio
    metadata_path_matches = _path_matches_context(
        actual=metadata_path,
        final_clip_context=final_clip_context,
        candidate_keys=("sink_metadata_path", "metadata_path", "canonical_metadata_path"),
    )
    raw_clip_path_matches = _path_matches_context(
        actual=raw_clip_path,
        final_clip_context=final_clip_context,
        candidate_keys=("raw_clip_path", "canonical_clip_path"),
    )
    timeline_aligned = (
        clip_timeline_summary.get("enabled") is True
        and clip_timeline_summary.get("status") in {"aligned", "partial"}
        and rows_written > 0
        and rows_non_displayable == 0
        and rows_displayable == rows_written
        and not written_contract.get("failures")
    )

    failures: list[str] = []
    if not duration_ok:
        failures.append("canonical_duration_not_verified")
    if event_metrics["event_pts_inside_clip"] is not True:
        failures.append("event_pts_outside_clip")
    if require_event_centered and not event_center_ok:
        failures.append("event_not_centered_in_clip")
    if require_event_centered and not event_ratio_ok:
        failures.append("event_position_ratio_too_late")
    if not timeline_aligned:
        failures.extend(written_contract.get("failures") or ["rows_not_fully_rebased_to_final_metadata"])
    if not metadata_path_matches or not raw_clip_path_matches:
        failures.append("final_clip_metadata_path_mismatch")
    if rows_total <= 0:
        failures.append("no_sidecar_rows")
    if trigger_stale_or_epoch_mismatch:
        failures.append("trigger_row_stale_or_epoch_mismatch")
    if rows_rejected_stale_cache > 0:
        failures.append("stale_cache_rows_rejected")
    if rows_rejected_epoch_mismatch > 0:
        failures.append("epoch_mismatch_rows_rejected")
    if rows_rejected_pts_non_unique > 0:
        failures.append("pts_fallback_rows_rejected_as_non_unique")
    if stale_timing_rows > 0:
        failures.append("stale_timing_on_non_displayable_rows")
    identity_event = _event_requires_identity_trigger(event)
    if identity_event and identity_scope_status != "trigger_only":
        failures.append("known_face_not_trigger_only")
    if int(written_contract.get("person_context_rows") or 0) <= 0:
        failures.append("person_context_missing")
    if vector_count > 0:
        failures.append("embedding_vectors_in_output")
    if image_count > 0:
        failures.append("image_bytes_in_output")

    canonical_clip = bool(
        duration_ok
        and event_metrics["event_pts_inside_clip"] is True
        and (event_center_ok or not require_event_centered)
        and (event_ratio_ok or not require_event_centered)
        and metadata_path_matches
        and raw_clip_path_matches
    )
    production_ready = bool(canonical_clip and timeline_aligned and not failures)
    annotation_status = (
        "cache_stale_or_epoch_mismatch"
        if (
            rows_rejected_stale_cache
            or rows_rejected_epoch_mismatch
            or rows_rejected_pts_non_unique
            or trigger_stale_or_epoch_mismatch
        )
        else None
    )
    trigger_binding = _trigger_visual_binding_summary(
        event=event,
        annotations=annotations,
        production_ready=production_ready,
        annotation_status=annotation_status,
        fallback_reason=failures[0] if failures else None,
    )
    return {
        "sidecar_type": SIDECAR_TYPE_PRODUCTION,
        "timeline_domain": TIMELINE_DOMAIN_FINAL_CANONICAL_CLIP,
        "production_ready": production_ready,
        "canonical_clip": canonical_clip,
        "clip_duration_seconds": (
            round(float(clip_duration_seconds), 6)
            if clip_duration_seconds is not None
            else None
        ),
        "event_pts_inside_clip": bool(event_metrics["event_pts_inside_clip"]),
        "event_projected_t_s": (
            round(float(event_projected), 6) if event_projected is not None else None
        ),
        "event_position_ratio": (
            round(float(event_ratio), 6) if event_ratio is not None else None
        ),
        "event_center_required": require_event_centered,
        "event_centered_in_clip": bool(event_center_ok),
        "metadata_path_used": metadata_path,
        "raw_clip_path_used": raw_clip_path,
        "metadata_path_matches_raw_clip": bool(metadata_path_matches and raw_clip_path_matches),
        "rows_total": rows_total,
        "rows_total_input": rows_total_input,
        "rows_written": rows_written,
        "rows_matched_by_frame_uuid": rows_matched_by_frame_uuid,
        "rows_matched_by_pts_fallback": rows_matched_by_pts_fallback,
        "rows_rejected_stale_cache": rows_rejected_stale_cache,
        "rows_rejected_epoch_mismatch": rows_rejected_epoch_mismatch,
        "rows_rejected_pts_non_unique": rows_rejected_pts_non_unique,
        "freshness_guard_mode": clip_timeline_summary.get("freshness_guard_mode"),
        "freshness_guard_status": clip_timeline_summary.get("freshness_guard_status"),
        "rows_displayable": rows_displayable,
        "rows_non_displayable": rows_non_displayable,
        "rows_dropped_out_of_window": rows_out_of_window,
        "rows_unmatched": rows_unmatched,
        "clip_timeline_match_distribution": dict(match_distribution),
        "identity_scope_status": identity_scope_status,
        "known_face_trigger_only": identity_scope_status == "trigger_only",
        "identity_trigger_required": identity_event,
        "person_context_rows": int(written_contract.get("person_context_rows") or 0),
        "legacy_fallback_allowed": False,
        "production_ready_failures": failures,
        "stale_timing_rows": stale_timing_rows,
        **trigger_binding,
    }


def filter_production_sidecar_rows(
    annotations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return displayable-only production rows plus dropped diagnostic rows."""

    written: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for row in copy.deepcopy(annotations):
        if not isinstance(row, dict):
            continue
        match = str(row.get("clip_timeline_match") or "")
        should_drop = row.get("displayable") is False or match in {"missing", "out_of_window"}
        if should_drop:
            row["displayable"] = False
            for key in ("t_ms", "t_s", "clip_frame_index", "matched_metadata_pts"):
                row.pop(key, None)
            dropped.append(row)
            continue
        row["displayable"] = True
        written.append(row)
    dropped_out = sum(1 for row in dropped if row.get("clip_timeline_match") == "out_of_window")
    dropped_missing = sum(1 for row in dropped if row.get("clip_timeline_match") == "missing")
    dropped_stale = sum(1 for row in dropped if row.get("stale_or_epoch_mismatch") is True)
    dropped_epoch = sum(1 for row in dropped if row.get("epoch_mismatch") is True)
    dropped_pts_non_unique = sum(1 for row in dropped if row.get("pts_non_unique") is True)
    return written, dropped, {
        "rows_total_input": len(written) + len(dropped),
        "rows_written": len(written),
        "rows_non_displayable": sum(1 for row in written if row.get("displayable") is False),
        "rows_dropped_total": len(dropped),
        "rows_dropped_out_of_window": dropped_out,
        "rows_dropped_missing": dropped_missing,
        "rows_rejected_stale_cache": dropped_stale,
        "rows_rejected_epoch_mismatch": dropped_epoch,
        "rows_rejected_pts_non_unique": dropped_pts_non_unique,
    }


def apply_frame_cache_freshness_guard(
    annotations: list[dict[str, Any]],
    *,
    event: dict[str, Any],
    config: dict[str, Any],
    pre_seconds: float = 5.0,
    post_seconds: float = 5.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fail closed on stale frame-cache rows when final metadata matched by PTS."""

    rows = [copy.deepcopy(row) for row in annotations if isinstance(row, dict)]
    summary = _empty_freshness_guard_summary(
        annotations=rows,
        event=event,
        config=config,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
    )
    event_epoch_ms = _event_wall_clock_epoch_ms(event)
    summary["event_created_at_epoch_ms"] = event_epoch_ms
    summary["event_created_at_source"] = _event_wall_clock_source(event)
    guard_mode = str(config.get("freshness_guard_mode") or "wall_clock").strip().lower()
    summary["freshness_guard_mode"] = guard_mode
    if event_epoch_ms is None:
        summary["freshness_guard_status"] = "missing_event_created_at"
    lower_ms = summary.get("freshness_window_start_epoch_ms")
    upper_ms = summary.get("freshness_window_end_epoch_ms")

    for row in rows:
        match = str(row.get("clip_timeline_match") or "")
        is_uuid_match = match == "metadata_frame_uuid"
        is_pts_fallback = match in {"metadata_frame_pts_exact", "metadata_frame_pts_nearest"}
        if is_uuid_match:
            summary["rows_matched_by_frame_uuid"] += 1
        if is_pts_fallback:
            summary["rows_matched_by_pts_fallback"] += 1

        row_created_ms = _row_created_epoch_ms(row)
        if row_created_ms is not None:
            row["frame_annotation_created_at_epoch_ms"] = row_created_ms
        if row_created_ms is not None and event_epoch_ms is not None:
            row["frame_annotation_created_delta_to_event_s"] = round(
                (row_created_ms - event_epoch_ms) / 1000.0,
                3,
            )

        if row.get("displayable") is False:
            continue
        if guard_mode in {"metadata_pts", "metadata", "pts"}:
            continue
        if is_pts_fallback and not _row_is_fresh(
            row_created_ms,
            lower_ms=_int_or_none(lower_ms),
            upper_ms=_int_or_none(upper_ms),
        ):
            _reject_row_for_freshness(
                row,
                reason="stale_or_epoch_mismatch",
                event_epoch_ms=event_epoch_ms,
                lower_ms=_int_or_none(lower_ms),
                upper_ms=_int_or_none(upper_ms),
            )
            summary["rows_rejected_stale_cache"] += 1
            summary["rows_rejected_epoch_mismatch"] += 1
            if is_pts_fallback:
                summary["rows_rejected_pts_non_unique"] += 1
            if row.get("annotation_role") == "watchlist_trigger_face":
                summary["trigger_row_stale_or_epoch_mismatch"] = True

    summary["rows_displayable"] = sum(1 for row in rows if row.get("displayable") is not False)
    if (
        summary["rows_rejected_stale_cache"]
        or summary["rows_rejected_epoch_mismatch"]
        or summary["rows_rejected_pts_non_unique"]
        or summary["trigger_row_stale_or_epoch_mismatch"]
    ):
        summary["freshness_guard_status"] = "stale_or_epoch_mismatch"
    elif summary["freshness_guard_status"] == "not_evaluated":
        summary["freshness_guard_status"] = "passed"
    return rows, summary


def _empty_freshness_guard_summary(
    *,
    annotations: list[dict[str, Any]],
    event: dict[str, Any],
    config: dict[str, Any],
    pre_seconds: float,
    post_seconds: float,
) -> dict[str, Any]:
    before_s = _freshness_before_seconds(config, pre_seconds)
    after_s = _freshness_after_seconds(config, post_seconds)
    event_epoch_ms = _event_wall_clock_epoch_ms(event)
    return {
        "freshness_guard_enabled": True,
        "freshness_guard_mode": str(
            config.get("freshness_guard_mode") or "wall_clock"
        ),
        "freshness_guard_status": "not_evaluated",
        "max_row_age_before_event_seconds": before_s,
        "max_row_age_after_event_seconds": after_s,
        "freshness_window_start_epoch_ms": (
            int(round(event_epoch_ms - before_s * 1000.0))
            if event_epoch_ms is not None
            else None
        ),
        "freshness_window_end_epoch_ms": (
            int(round(event_epoch_ms + after_s * 1000.0))
            if event_epoch_ms is not None
            else None
        ),
        "event_created_at_epoch_ms": event_epoch_ms,
        "event_created_at_source": _event_wall_clock_source(event),
        "rows_matched_by_frame_uuid": 0,
        "rows_matched_by_pts_fallback": 0,
        "rows_rejected_stale_cache": 0,
        "rows_rejected_epoch_mismatch": 0,
        "rows_rejected_pts_non_unique": 0,
        "rows_displayable": sum(1 for row in annotations if row.get("displayable") is not False),
        "trigger_row_stale_or_epoch_mismatch": False,
    }


def filtered_production_sidecar_summary(
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Filter an existing production sidecar and update its readiness summary."""

    written, dropped, filter_summary = filter_production_sidecar_rows(rows)
    counts = _count_identity_annotations(written)
    rows_unmatched = int(
        summary.get("rows_unmatched")
        if summary.get("rows_unmatched") is not None
        else len(dropped)
    )
    rows_out = int(
        summary.get("rows_dropped_out_of_window")
        if summary.get("rows_dropped_out_of_window") is not None
        else sum(1 for row in dropped if row.get("clip_timeline_match") == "out_of_window")
    )
    rows_total_input = int(summary.get("rows_total_input") or filter_summary["rows_total_input"])
    clip_summary = {
        "enabled": True,
        "status": "partial" if rows_unmatched else "aligned",
        "annotations_aligned": len(written),
        "annotations_unmatched": rows_unmatched,
        "annotations_out_of_window": rows_out,
        "annotations_displayable": len(written),
        "clip_timeline_match_distribution": summary.get("clip_timeline_match_distribution") or {},
        "first_pts": None,
        "last_pts": None,
        **filter_summary,
        "rows_total_input": rows_total_input,
    }
    context = {
        "raw_clip_duration": summary.get("clip_duration_seconds"),
        "event_projected_t_s": summary.get("event_projected_t_s"),
        "event_pts_inside_clip": summary.get("event_pts_inside_clip"),
        "event_position_ratio": summary.get("event_position_ratio"),
    }
    updated = {
        **copy.deepcopy(summary),
        **_production_sidecar_contract_summary(
            event={},
            annotations=written,
            identity_counts=counts,
            clip_timeline_summary=clip_summary,
            raw_clip_path=summary.get("raw_clip_path_used") or summary.get("raw_clip_path"),
            metadata_path=summary.get("metadata_path_used") or summary.get("metadata_path"),
            final_clip_context=context,
            config={},
        ),
        "annotations_input": rows_total_input,
        "annotations_written": len(written),
        "known_face_count": counts["known_face_count"],
        "unknown_face_count": counts["unknown_face_count"],
        "trigger_known_face_present": counts["trigger_known_face_present"],
        "annotation_status": "complete" if written else "missing_frame_metadata",
    }
    updated["clip_timeline_alignment"] = {
        **(summary.get("clip_timeline_alignment") if isinstance(summary.get("clip_timeline_alignment"), dict) else {}),
        **clip_summary,
    }
    return written, dropped, updated


def _production_written_rows_contract(rows: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    stale = 0
    missing_timing = 0
    invalid_match = 0
    non_displayable = 0
    person_context_rows = 0
    for row in rows:
        if row.get("displayable") is False:
            non_displayable += 1
            if any(key in row for key in ("t_ms", "t_s", "clip_frame_index")):
                stale += 1
            continue
        match = str(row.get("clip_timeline_match") or "")
        if match in {"", "missing", "out_of_window"}:
            invalid_match += 1
        if "t_ms" not in row and "t_s" not in row:
            missing_timing += 1
        if row.get("object_type") == "person" and row.get("annotation_role") == "person_context":
            person_context_rows += 1
    if non_displayable:
        failures.append("non_displayable_rows_in_production_sidecar")
    if invalid_match:
        failures.append("displayable_rows_not_rebased_to_final_metadata")
    if missing_timing:
        failures.append("displayable_rows_missing_final_clip_time")
    if stale:
        failures.append("stale_timing_on_non_displayable_rows")
    return {
        "failures": failures,
        "stale_timing_rows": stale,
        "missing_timing_rows": missing_timing,
        "invalid_match_rows": invalid_match,
        "non_displayable_rows": non_displayable,
        "person_context_rows": person_context_rows,
    }


def _trigger_visual_binding_summary(
    *,
    event: dict[str, Any],
    annotations: list[dict[str, Any]],
    production_ready: bool,
    annotation_status: str | None,
    fallback_reason: str | None,
) -> dict[str, Any]:
    anchor, _anchor_summary = extract_evidence_event_anchor(event)
    source_observation_id = anchor.get("source_observation_id") if anchor else None
    trigger = _find_trigger_face_row(
        annotations,
        source_observation_id=str(source_observation_id or ""),
    )
    trigger_exists = trigger is not None
    trigger_fresh = bool(trigger_exists and _trigger_row_passed_freshness_guard(trigger or {}))
    method = _frame_identity_method(trigger) if trigger else _frame_identity_method_from_anchor(anchor)
    confidence = _frame_identity_confidence(
        method=method,
        production_ready=production_ready,
        trigger_exists=trigger_exists,
        trigger_fresh=trigger_fresh,
    )
    reason = _visual_binding_reason(
        production_ready=production_ready,
        annotation_status=annotation_status,
        trigger_exists=trigger_exists,
        trigger_fresh=trigger_fresh,
        method=method,
        fallback_reason=fallback_reason,
    )
    verified = (
        production_ready
        and trigger_exists
        and trigger_fresh
        and method in {"frame_uuid", "frame_pts_fresh"}
    )
    return {
        "visual_binding_status": "verified" if verified else "unverified",
        "visual_binding_reason": "production_sidecar_trigger_bound" if verified else reason,
        "evidence_visual_status": "verified" if verified else "unverified",
        "source_observation_id": source_observation_id,
        "frame_identity_method": method,
        "frame_identity_confidence": confidence,
        "trigger_face_row_exists": trigger_exists,
        "trigger_face_row_passed_freshness_guard": trigger_fresh,
        "trigger_face_row_match_type": trigger.get("clip_timeline_match") if trigger else None,
        "trigger_face_row_frame_uuid": trigger.get("frame_uuid") if trigger else anchor.get("frame_uuid") if anchor else None,
        "trigger_face_row_frame_pts": trigger.get("frame_pts") if trigger else anchor.get("frame_pts") if anchor else None,
        "legacy_used_for_visual_binding": False,
    }


def _find_trigger_face_row(
    annotations: list[dict[str, Any]],
    *,
    source_observation_id: str,
) -> dict[str, Any] | None:
    for row in annotations:
        if not isinstance(row, dict):
            continue
        if row.get("object_type") != "face":
            continue
        if row.get("annotation_role") == "watchlist_trigger_face":
            return row
        if source_observation_id and row.get("source_observation_id") == source_observation_id:
            label = row.get("label") if isinstance(row.get("label"), dict) else {}
            if label.get("kind") == "known_face":
                return row
    return None


def _trigger_row_passed_freshness_guard(row: dict[str, Any]) -> bool:
    if row.get("displayable") is False:
        return False
    if row.get("stale_or_epoch_mismatch") is True:
        return False
    if row.get("frame_cache_rejection_reason"):
        return False
    if row.get("pts_non_unique") is True or row.get("epoch_mismatch") is True:
        return False
    return True


def _frame_identity_method(row: dict[str, Any] | None) -> str:
    if not row:
        return "missing"
    match = str(row.get("clip_timeline_match") or row.get("match_type") or "")
    if match == "metadata_frame_uuid":
        return "frame_uuid"
    if match in {"metadata_frame_pts_exact", "metadata_frame_pts_nearest"}:
        return "frame_pts_fresh" if _trigger_row_passed_freshness_guard(row) else "frame_pts_stale_or_non_unique"
    if row.get("frame_pts") is not None:
        return "frame_pts_fresh" if _trigger_row_passed_freshness_guard(row) else "frame_pts_stale_or_non_unique"
    return "missing"


def _frame_identity_method_from_anchor(anchor: dict[str, Any] | None) -> str:
    if not anchor:
        return "missing"
    if anchor.get("frame_uuid"):
        return "frame_uuid"
    if anchor.get("frame_pts") is not None:
        return "frame_pts_without_sidecar_row"
    return "missing"


def _frame_identity_confidence(
    *,
    method: str,
    production_ready: bool,
    trigger_exists: bool,
    trigger_fresh: bool,
) -> str:
    if not production_ready or not trigger_exists or not trigger_fresh:
        return "none"
    if method == "frame_uuid":
        return "high"
    if method == "frame_pts_fresh":
        return "medium"
    return "none"


def _visual_binding_reason(
    *,
    production_ready: bool,
    annotation_status: str | None,
    trigger_exists: bool,
    trigger_fresh: bool,
    method: str,
    fallback_reason: str | None,
) -> str:
    if annotation_status == "cache_stale_or_epoch_mismatch":
        return "cache_stale_or_epoch_mismatch"
    if not production_ready:
        return fallback_reason or "production_sidecar_not_ready"
    if not trigger_exists:
        return "trigger_face_row_missing"
    if not trigger_fresh:
        return "trigger_face_row_failed_freshness_guard"
    if method not in {"frame_uuid", "frame_pts_fresh"}:
        return "frame_identity_missing"
    return "production_sidecar_trigger_bound"


def _clip_duration_seconds(
    *,
    final_clip_context: dict[str, Any] | None,
    clip_timeline_summary: dict[str, Any],
) -> float | None:
    for key in ("raw_clip_duration", "clip_duration_seconds", "duration_seconds"):
        value = _context_lookup(final_clip_context, key)
        parsed = _float_or_none(value)
        if parsed is not None and parsed > 0:
            return parsed
    first_pts = _int_or_none(clip_timeline_summary.get("first_pts"))
    last_pts = _int_or_none(clip_timeline_summary.get("last_pts"))
    if first_pts is not None and last_pts is not None and last_pts > first_pts:
        return (last_pts - first_pts) * PTS_TIME_BASE_SECONDS
    return None


def _event_clip_metrics(
    *,
    event: dict[str, Any],
    final_clip_context: dict[str, Any] | None,
    clip_timeline_summary: dict[str, Any],
    clip_duration_seconds: float | None,
) -> dict[str, Any]:
    anchor, _summary = extract_evidence_event_anchor(event)
    event_pts = _int_or_none((anchor or {}).get("frame_pts"))
    first_pts = _int_or_none(clip_timeline_summary.get("first_pts"))
    last_pts = _int_or_none(clip_timeline_summary.get("last_pts"))
    if event_pts is not None and first_pts is not None and last_pts is not None:
        projected = (event_pts - first_pts) * PTS_TIME_BASE_SECONDS
        duration = clip_duration_seconds
        if duration is None and last_pts > first_pts:
            duration = (last_pts - first_pts) * PTS_TIME_BASE_SECONDS
        return {
            "event_pts_inside_clip": first_pts <= event_pts <= last_pts,
            "event_projected_t_s": projected,
            "event_position_ratio": (
                projected / duration if duration is not None and duration > 0 else None
            ),
        }
    projected = _float_or_none(
        _context_lookup(final_clip_context, "event_projected_t_s")
        or _context_lookup(final_clip_context, "expected_event_t_s")
    )
    inside = _bool_or_none(_context_lookup(final_clip_context, "event_pts_inside_clip"))
    ratio = _float_or_none(_context_lookup(final_clip_context, "event_position_ratio"))
    if ratio is None and projected is not None and clip_duration_seconds:
        ratio = projected / clip_duration_seconds
    return {
        "event_pts_inside_clip": bool(inside),
        "event_projected_t_s": projected,
        "event_position_ratio": ratio,
    }


def _identity_scope_status(
    annotations: list[dict[str, Any]],
    identity_counts: dict[str, Any],
) -> str:
    known_rows = []
    non_trigger_known_rows = []
    for row in annotations:
        if not isinstance(row, dict):
            continue
        label = row.get("label") if isinstance(row.get("label"), dict) else {}
        if label.get("kind") != "known_face":
            continue
        known_rows.append(row)
        if row.get("annotation_role") != "watchlist_trigger_face":
            non_trigger_known_rows.append(row)
    if non_trigger_known_rows:
        return "known_face_scope_leak"
    if known_rows and identity_counts.get("trigger_known_face_present") is True:
        return "trigger_only"
    return "missing_trigger_known_face"


def _event_requires_identity_trigger(event: dict[str, Any]) -> bool:
    event_type = str(event.get("event_type") or "").strip()
    return event_type in {"watchlist_hit", "live_search_hit"}


def _path_matches_context(
    *,
    actual: str | None,
    final_clip_context: dict[str, Any] | None,
    candidate_keys: tuple[str, ...],
) -> bool:
    expected_values = [
        str(value)
        for key in candidate_keys
        for value in [_context_lookup(final_clip_context, key)]
        if value not in (None, "")
    ]
    if not expected_values:
        return True
    if not actual:
        return False
    actual_norm = _normalize_path_text(actual)
    return any(actual_norm == _normalize_path_text(value) for value in expected_values)


def _context_lookup(context: dict[str, Any] | None, key: str) -> Any:
    if not isinstance(context, dict):
        return None
    if key in context:
        return context.get(key)
    for child_key in ("media", "clip_validation", "canonical_clip_window", "canonical_metadata_summary"):
        child = context.get(child_key)
        if isinstance(child, dict):
            value = _context_lookup(child, key)
            if value is not None:
                return value
    return None


def _normalize_path_text(value: Any) -> str:
    return str(Path(str(value)).resolve(strict=False))


def _float_config(config: dict[str, Any], key: str, default: float) -> float:
    parsed = _float_or_none(config.get(key))
    return float(default if parsed is None else parsed)


def _float_or_none(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def _freshness_before_seconds(config: dict[str, Any], pre_seconds: float) -> float:
    configured = _float_or_none(config.get("max_row_age_before_event_seconds"))
    if configured is not None and configured > 0:
        return float(configured)
    return max(float(pre_seconds), 0.0) + 15.0


def _freshness_after_seconds(config: dict[str, Any], post_seconds: float) -> float:
    configured = _float_or_none(config.get("max_row_age_after_event_seconds"))
    if configured is not None and configured > 0:
        return float(configured)
    return max(float(post_seconds), 0.0) + 15.0


def _event_wall_clock_epoch_ms(event: dict[str, Any]) -> int | None:
    for value in _event_created_at_candidates(event):
        parsed = _epoch_ms_from_value(value)
        if parsed is not None:
            return parsed
    return None


def _event_wall_clock_source(event: dict[str, Any]) -> str | None:
    for key, value in _event_created_at_candidate_items(event):
        if _epoch_ms_from_value(value) is not None:
            return key
    return None


def _event_created_at_candidates(event: dict[str, Any]) -> list[Any]:
    return [value for _key, value in _event_created_at_candidate_items(event)]


def _event_created_at_candidate_items(event: dict[str, Any]) -> list[tuple[str, Any]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    request = media.get("replay_job_request") if isinstance(media.get("replay_job_request"), dict) else {}
    return [
        ("event.created_at", event.get("created_at")),
        ("payload.created_at", payload.get("created_at")),
        ("payload.media.created_at", media.get("created_at")),
        ("payload.media.request_created_at", media.get("request_created_at")),
        ("payload.media.replay_job_created_at", media.get("replay_job_created_at")),
        ("payload.media.replay_job_request.created_at", request.get("created_at")),
    ]


def _row_created_epoch_ms(row: dict[str, Any]) -> int | None:
    for key in (
        "frame_annotation_created_at",
        "redis_created_at",
        "created_at",
    ):
        parsed = _epoch_ms_from_value(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _epoch_ms_from_value(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(round(dt.timestamp() * 1000.0))
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        return int(round(numeric if numeric > 10_000_000_000 else numeric * 1000.0))
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _epoch_ms_from_value(int(text))
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000.0))


def _row_is_fresh(
    row_created_ms: int | None,
    *,
    lower_ms: int | None,
    upper_ms: int | None,
) -> bool:
    if row_created_ms is None or lower_ms is None or upper_ms is None:
        return False
    return int(lower_ms) <= int(row_created_ms) <= int(upper_ms)


def _reject_row_for_freshness(
    row: dict[str, Any],
    *,
    reason: str,
    event_epoch_ms: int | None,
    lower_ms: int | None,
    upper_ms: int | None,
) -> None:
    row["displayable"] = False
    row["stale_or_epoch_mismatch"] = True
    row["epoch_mismatch"] = True
    row["pts_non_unique"] = True
    row["frame_cache_rejection_reason"] = reason
    row["freshness_window_start_epoch_ms"] = lower_ms
    row["freshness_window_end_epoch_ms"] = upper_ms
    row["event_created_at_epoch_ms"] = event_epoch_ms
    for key in ("t_ms", "t_s", "clip_frame_index", "matched_metadata_pts"):
        row.pop(key, None)


def _base_summary(
    event: dict[str, Any],
    *,
    config: dict[str, Any],
    decision: dict[str, Any],
    evidence_dir: str,
    raw_clip_path: str | None,
    metadata_path: str | None,
    annotations_path: str,
    summary_path: str,
    old_annotations_path: str,
    old_summary_path: str,
) -> dict[str, Any]:
    anchor, _anchor_summary = extract_evidence_event_anchor(event)
    return {
        "schema_version": SCHEMA_VERSION,
        "phase": PHASE,
        "generated_at": _utc_now(),
        "sidecar_enabled": bool(config.get("enabled")),
        "sidecar_mode": config.get("write_mode"),
        "written_by": str(config.get("written_by") or "media_worker_hook"),
        "sidecar_trigger": str(config.get("sidecar_trigger") or "production_evidence_complete"),
        "hook_invoked": bool(config.get("hook_invoked", True)),
        "event_type": event.get("event_type"),
        "event_id": event.get("event_id") or event.get("id"),
        "source_event_id": event.get("source_event_id"),
        "source_id": (anchor or {}).get("source_id") or event.get("source_id"),
        "camera_id": (anchor or {}).get("camera_id") or event.get("camera_id"),
        "source_observation_id": (anchor or {}).get("source_observation_id"),
        "frame_uuid": (anchor or {}).get("frame_uuid") or event.get("frame_uuid"),
        "frame_pts": (anchor or {}).get("frame_pts"),
        "anchor_found_by": "missing",
        "event_window_messages": 0,
        "annotations_written": 0,
        "known_face_count": 0,
        "unknown_face_count": 0,
        "trigger_known_face_present": False,
        "annotation_status": "skipped",
        "sidecar_type": SIDECAR_TYPE_PRODUCTION,
        "timeline_domain": TIMELINE_DOMAIN_FINAL_CANONICAL_CLIP,
        "production_ready": False,
        "canonical_clip": False,
        "clip_duration_seconds": None,
        "event_pts_inside_clip": False,
        "event_projected_t_s": None,
        "metadata_path_used": metadata_path,
        "raw_clip_path_used": raw_clip_path,
        "rows_total": 0,
        "rows_total_input": 0,
        "rows_written": 0,
        "rows_matched_by_frame_uuid": 0,
        "rows_matched_by_pts_fallback": 0,
        "rows_rejected_stale_cache": 0,
        "rows_rejected_epoch_mismatch": 0,
        "rows_rejected_pts_non_unique": 0,
        "rows_displayable": 0,
        "rows_non_displayable": 0,
        "rows_dropped_out_of_window": 0,
        "rows_unmatched": 0,
        "clip_timeline_match_distribution": {},
        "identity_scope_status": "unavailable",
        "legacy_fallback_allowed": False,
        "production_ready_failures": ["sidecar_not_written"],
        "stale_timing_rows": 0,
        "embedding_vectors_in_output": 0,
        "image_bytes_in_output": 0,
        "old_annotations_preserved": Path(old_annotations_path).is_file()
        and Path(annotations_path).name != "annotations.jsonl",
        "old_summary_preserved": Path(old_summary_path).is_file(),
        "production_replacement": False,
        "fail_open": bool(config.get("fail_open", True)),
        "error": None,
        "decision": decision,
        "evidence_dir": evidence_dir,
        "raw_clip_path": raw_clip_path,
        "metadata_path": metadata_path,
        "sidecar_annotations_path": annotations_path,
        "sidecar_summary_path": summary_path,
        "old_annotations_path": old_annotations_path,
        "old_summary_path": old_summary_path,
        "db_writes": False,
        "production_redis_writes": False,
    }


def _annotation_status_from_counts(
    *,
    event_window_messages: int,
    trigger_known_face_present: bool,
    require_trigger_face: bool,
    cache_stale_or_epoch_mismatch: bool = False,
) -> str:
    if cache_stale_or_epoch_mismatch:
        return "cache_stale_or_epoch_mismatch"
    if event_window_messages <= 0:
        return "missing_frame_metadata"
    if require_trigger_face and not trigger_known_face_present:
        return "missing_trigger_face_annotation"
    return "complete"


def _message_from_fields(fields: dict[str, Any]) -> dict[str, Any] | None:
    data = fields.get("data") or fields.get("payload")
    if isinstance(data, dict):
        payload = copy.deepcopy(data)
    elif isinstance(data, str) and data.strip():
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return None
        payload = parsed if isinstance(parsed, dict) else None
    else:
        payload = dict(fields)
        objects = payload.get("objects")
        if isinstance(objects, str):
            try:
                payload["objects"] = json.loads(objects)
            except json.JSONDecodeError:
                payload["objects"] = []
    if not isinstance(payload, dict):
        return None
    if payload.get("message_type") != "frame_annotation":
        return None
    for field in ("frame_pts", "frame_num", "timestamp_ms", "ttl_seconds"):
        payload[field] = _int_or_none(payload.get(field))
    objects = payload.get("objects")
    if not isinstance(objects, list):
        payload["objects"] = []
    return payload


def _split_entry(entry: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return _decode_text(entry[0]), _fields_to_dict(entry[1])
    return "", {}


def _fields_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {_decode_text(k): _decode_maybe_text(v) for k, v in value.items()}
    if isinstance(value, list):
        result: dict[str, Any] = {}
        for index in range(0, len(value) - 1, 2):
            result[_decode_text(value[index])] = _decode_maybe_text(value[index + 1])
        return result
    return {}


def _encode_resp(parts: list[Any]) -> bytes:
    out = [f"*{len(parts)}\r\n".encode("ascii")]
    for part in parts:
        data = str(part).encode("utf-8")
        out.append(f"${len(data)}\r\n".encode("ascii"))
        out.append(data + b"\r\n")
    return b"".join(out)


class _RespReader:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = b""

    def read(self) -> Any:
        prefix = self._read_exact(1)
        if prefix == b"*":
            count = int(self._read_line())
            if count < 0:
                return []
            return [self.read() for _ in range(count)]
        if prefix == b"$":
            length = int(self._read_line())
            if length < 0:
                return None
            data = self._read_exact(length)
            self._read_exact(2)
            return data
        if prefix == b":":
            return int(self._read_line())
        if prefix == b"+":
            return self._read_line()
        if prefix == b"-":
            raise RuntimeError(self._read_line().decode("utf-8", errors="replace"))
        raise RuntimeError(f"unknown RESP prefix {prefix!r}")

    def _read_line(self) -> bytes:
        while b"\r\n" not in self.buffer:
            self.buffer += self.sock.recv(4096)
        line, self.buffer = self.buffer.split(b"\r\n", 1)
        return line

    def _read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise RuntimeError("redis connection closed")
            self.buffer += chunk
        data = self.buffer[:size]
        self.buffer = self.buffer[size:]
        return data


def _decode_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _decode_maybe_text(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _int_or_none(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_VECTOR_FIELDS or lowered in FORBIDDEN_IMAGE_FIELDS:
                continue
            if lowered == "embedding":
                continue
            result[str(key)] = _sanitize_value(child)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    return value


def _count_forbidden(value: Any, fields: set[str]) -> int:
    if isinstance(value, dict):
        count = 0
        for key, child in value.items():
            if str(key).lower() in fields:
                count += 1
            count += _count_forbidden(child, fields)
        return count
    if isinstance(value, list):
        return sum(_count_forbidden(item, fields) for item in value)
    return 0


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True, default=str) + "\n")
    tmp.replace(path)


def _dropped_debug_path(annotations_path: Path) -> Path:
    return annotations_path.with_name("annotations.frame_cache.identity.dropped.debug.jsonl")


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
