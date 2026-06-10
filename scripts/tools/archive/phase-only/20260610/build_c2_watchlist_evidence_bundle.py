#!/usr/bin/env python3
"""Build a C2.5 watchlist-hit evidence bundle from C2.4 identity evidence."""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SIDECAR_FILE = "annotations.frame_cache.identity.jsonl"
SUMMARY_FILE = "summary.json"
SIDECAR_SUMMARY_FILE = "summary.frame_cache.identity.json"
IDENTITY_PATCHES_FILE = "identity_patches.jsonl"
WATCHLIST_EVENT_FILE = "watchlist_event.json"
WATCHLIST_SUMMARY_FILE = "watchlist_evidence_summary.json"
WATCHLIST_REPORT_FILE = "watchlist_evidence_report.html"
RESULT_PASS = "PASS_C2_5_WATCHLIST_EVENT_EVIDENCE_SEMANTICS_READY"
RESULT_FAIL = "FAIL_C2_5_WATCHLIST_EVENT_SEMANTICS_BLOCKED"
EVENT_TYPE = "watchlist_hit"
DEFAULT_WATCHLIST_RULE_ID = "c2_5_test_watchlist_rule"
DEFAULT_WATCHLIST_RULE_NAME = "C2.5 Test Watchlist Rule"
DEFAULT_PERSON_NAME = "C2.5 Test Watchlist Person"
DEFAULT_CAMERA_ID = "c2_post_savant_fps_probe"
DEFAULT_SOURCE_ID = "c2_post_savant_fps_probe"

FORBIDDEN_EVENT_KEYS = {
    "embedding",
    "embeddings",
    "embedding_vector",
    "embedding_values",
    "feature",
    "features",
    "crop_bytes",
    "image_bytes",
    "raw_frame",
    "frame_bytes",
    "jpeg",
    "png",
    "base64",
    "base64_image",
}


@dataclass(frozen=True)
class KnownFaceBinding:
    row_index: int
    object_index: int
    frame_index: int
    frame_pts: int | None
    track_id: str | None
    object_id: str | None
    face_bbox: list[float]
    source_observation_id: str
    person_id: int
    external_person_id: str
    person_name: str | None
    gallery_embedding_id: int
    match_result_id: int
    similarity: float
    threshold: float
    join_method: str | None


@dataclass(frozen=True)
class BuildResult:
    result_marker: str
    input_bundle: Path
    output_bundle: Path
    watchlist_event_path: Path
    watchlist_summary_path: Path
    report_path: Path
    summary_path: Path
    sidecar_path: Path
    watchlist_event: dict[str, Any]
    watchlist_summary: dict[str, Any]
    summary: dict[str, Any]


def build_watchlist_evidence_bundle(
    *,
    input_bundle: Path,
    output_dir: Path,
    person_id: int,
    external_person_id: str,
    source_observation_id: str,
    watchlist_rule_id: str = DEFAULT_WATCHLIST_RULE_ID,
    watchlist_rule_name: str = DEFAULT_WATCHLIST_RULE_NAME,
    person_name: str = DEFAULT_PERSON_NAME,
    threshold: float = 0.99,
    severity: str = "high",
    camera_id: str = DEFAULT_CAMERA_ID,
    source_id: str = DEFAULT_SOURCE_ID,
    source_event_id: str | None = None,
    overwrite: bool = False,
) -> BuildResult:
    """Copy a C2.4 identity bundle and add watchlist-hit semantics."""

    input_bundle = input_bundle.resolve(strict=False)
    output_dir = output_dir.resolve(strict=False)
    _require_input_bundle(input_bundle)
    sidecar_rows = _read_jsonl(input_bundle / SIDECAR_FILE)
    input_summary = _read_json(input_bundle / SUMMARY_FILE)
    identity_patches = _read_jsonl(input_bundle / IDENTITY_PATCHES_FILE)
    _validate_input_summary(input_summary)
    binding = find_known_face_binding(
        rows=sidecar_rows,
        identity_patches=identity_patches,
        person_id=person_id,
        external_person_id=external_person_id,
        source_observation_id=source_observation_id,
        threshold=threshold,
    )
    if binding is None:
        raise RuntimeError("known_face_binding_missing")

    source_event_id = source_event_id or (
        f"c2_5:watchlist_hit:{source_observation_id}:{person_id}"
    )
    watchlist_event = build_watchlist_event(
        binding=binding,
        input_bundle=input_bundle,
        output_bundle=output_dir,
        source_event_id=source_event_id,
        watchlist_rule_id=watchlist_rule_id,
        watchlist_rule_name=watchlist_rule_name,
        person_name=person_name,
        threshold=threshold,
        severity=severity,
        camera_id=camera_id,
        source_id=source_id,
    )
    assert_no_forbidden_event_payload(watchlist_event)

    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        if output_dir.is_dir():
            shutil.rmtree(output_dir)
        else:
            output_dir.unlink()
    shutil.copytree(input_bundle, output_dir)

    patched_rows = patch_sidecar_watchlist_fields(
        sidecar_rows,
        binding=binding,
        watchlist_event=watchlist_event,
        watchlist_rule_id=watchlist_rule_id,
        watchlist_rule_name=watchlist_rule_name,
        severity=severity,
    )
    summary = patch_summary(
        input_summary,
        rows=patched_rows,
        binding=binding,
        watchlist_event=watchlist_event,
        watchlist_rule_id=watchlist_rule_id,
        watchlist_rule_name=watchlist_rule_name,
        severity=severity,
    )
    watchlist_summary = build_watchlist_summary(
        input_bundle=input_bundle,
        output_bundle=output_dir,
        summary=summary,
        binding=binding,
        watchlist_event=watchlist_event,
        watchlist_rule_id=watchlist_rule_id,
        watchlist_rule_name=watchlist_rule_name,
    )
    _validate_output(summary, watchlist_event, watchlist_summary)

    sidecar_path = output_dir / SIDECAR_FILE
    summary_path = output_dir / SUMMARY_FILE
    sidecar_summary_path = output_dir / SIDECAR_SUMMARY_FILE
    event_path = output_dir / WATCHLIST_EVENT_FILE
    watchlist_summary_path = output_dir / WATCHLIST_SUMMARY_FILE
    report_path = output_dir / WATCHLIST_REPORT_FILE
    _write_jsonl(sidecar_path, patched_rows)
    _write_json(summary_path, summary)
    _write_json(sidecar_summary_path, summary)
    _write_json(event_path, watchlist_event)
    _write_json(watchlist_summary_path, watchlist_summary)
    _write_report(report_path, watchlist_summary)

    return BuildResult(
        result_marker=RESULT_PASS,
        input_bundle=input_bundle,
        output_bundle=output_dir,
        watchlist_event_path=event_path,
        watchlist_summary_path=watchlist_summary_path,
        report_path=report_path,
        summary_path=summary_path,
        sidecar_path=sidecar_path,
        watchlist_event=watchlist_event,
        watchlist_summary=watchlist_summary,
        summary=summary,
    )


def find_known_face_binding(
    *,
    rows: list[dict[str, Any]],
    identity_patches: list[dict[str, Any]],
    person_id: int,
    external_person_id: str,
    source_observation_id: str,
    threshold: float,
) -> KnownFaceBinding | None:
    patch_by_source = {
        str(patch.get("source_observation_id")): patch
        for patch in identity_patches
        if patch.get("source_observation_id")
    }
    for row_index, row in enumerate(rows):
        objects = row.get("objects") if isinstance(row.get("objects"), list) else []
        for object_index, obj in enumerate(objects):
            if not isinstance(obj, dict):
                continue
            identity = _dict(obj.get("identity"))
            label = _dict(obj.get("label"))
            if _object_type(obj) != "known_face" and label.get("kind") != "known_face":
                continue
            if identity.get("source_observation_id") != source_observation_id:
                continue
            if int(identity.get("person_id") or -1) != int(person_id):
                continue
            if str(identity.get("external_person_id") or "") != str(external_person_id):
                continue
            if int(identity.get("gallery_embedding_id") or -1) <= 0:
                continue
            if int(identity.get("match_result_id") or -1) <= 0:
                continue
            similarity = _float_required(identity.get("similarity"), "similarity")
            if similarity < threshold:
                continue
            bbox = _sidecar_bbox_xyxy(obj)
            if bbox is None:
                continue
            patch = patch_by_source.get(source_observation_id, {})
            return KnownFaceBinding(
                row_index=row_index,
                object_index=object_index,
                frame_index=int(row.get("frame_index") or row_index),
                frame_pts=_int_or_none(row.get("frame_pts") or obj.get("frame_pts")),
                track_id=_text_or_none(obj.get("track_id")),
                object_id=_text_or_none(obj.get("object_id")),
                face_bbox=bbox,
                source_observation_id=source_observation_id,
                person_id=int(person_id),
                external_person_id=external_person_id,
                person_name=_text_or_none(identity.get("person_name") or identity.get("display_name")),
                gallery_embedding_id=int(identity.get("gallery_embedding_id")),
                match_result_id=int(identity.get("match_result_id")),
                similarity=similarity,
                threshold=_float_required(identity.get("threshold"), "threshold"),
                join_method=_text_or_none(identity.get("join_method") or patch.get("join_method")),
            )
    return None


def build_watchlist_event(
    *,
    binding: KnownFaceBinding,
    input_bundle: Path,
    output_bundle: Path,
    source_event_id: str,
    watchlist_rule_id: str,
    watchlist_rule_name: str,
    person_name: str,
    threshold: float,
    severity: str,
    camera_id: str,
    source_id: str,
) -> dict[str, Any]:
    event_ts_ms = _event_ts_ms(binding)
    return {
        "schema_version": "1.0",
        "event_type": EVENT_TYPE,
        "source_event_id": source_event_id,
        "producer": "c2_5_file_semantics_tool",
        "camera_id": camera_id,
        "source_id": source_id,
        "track_id": binding.track_id,
        "source_observation_id": binding.source_observation_id,
        "person_id": binding.person_id,
        "algorithm_type": "face_intelligence",
        "algorithm_version": "c2.5-evidence-semantics-mvp",
        "external_person_id": binding.external_person_id,
        "person_name": person_name,
        "identity_person_name": binding.person_name,
        "gallery_embedding_id": binding.gallery_embedding_id,
        "match_result_id": binding.match_result_id,
        "similarity": binding.similarity,
        "confidence": binding.similarity,
        "threshold": threshold,
        "watchlist_rule_id": watchlist_rule_id,
        "watchlist_rule_name": watchlist_rule_name,
        "rule_name": watchlist_rule_name,
        "severity": severity,
        "event_ts_ms": event_ts_ms,
        "start_ts_ms": event_ts_ms,
        "end_ts_ms": event_ts_ms,
        "frame_pts": binding.frame_pts,
        "frame_num": binding.frame_index,
        "frame_id": binding.frame_index,
        "snapshot_required": True,
        "clip_required": True,
        "evidence_policy": {"snapshot_required": True, "clip_required": True, "pre_seconds": 5, "post_seconds": 5},
        "evidence_bundle": str(output_bundle),
        "description": f"Watchlist hit for {person_name}".strip(),
        "payload": {
            "identity_source": "match_results",
            "identity_binding_status": "matched",
            "evidence_capture_mode": "stable_post_savant_sink_time_crop",
            "workaround_used": True,
            "event_style_replay_job_passed": False,
            "input_identity_bundle": str(input_bundle),
            "geometry_source": "post_savant_sidecar",
            "geometry_modified": False,
            "join_method": binding.join_method,
        },
    }


def patch_sidecar_watchlist_fields(
    rows: list[dict[str, Any]],
    *,
    binding: KnownFaceBinding,
    watchlist_event: dict[str, Any],
    watchlist_rule_id: str,
    watchlist_rule_name: str,
    severity: str,
) -> list[dict[str, Any]]:
    patched = json.loads(json.dumps(rows))
    obj = patched[binding.row_index]["objects"][binding.object_index]
    original_geometry = _geometry_snapshot(obj)
    obj["object_type"] = "known_face"
    obj["label"] = {
        **_dict(obj.get("label")),
        "kind": "known_face",
        "event_type": EVENT_TYPE,
        "watchlist_rule_id": watchlist_rule_id,
        "watchlist_rule_name": watchlist_rule_name,
        "severity": severity,
        "source_event_id": watchlist_event["source_event_id"],
    }
    obj["identity"] = {
        **_dict(obj.get("identity")),
        "event_type": EVENT_TYPE,
        "source_event_id": watchlist_event["source_event_id"],
        "watchlist_rule_id": watchlist_rule_id,
        "watchlist_rule_name": watchlist_rule_name,
        "watchlist_hit_status": "matched",
        "watchlist_severity": severity,
        "source_observation_id": binding.source_observation_id,
        "person_id": binding.person_id,
        "external_person_id": binding.external_person_id,
        "gallery_embedding_id": binding.gallery_embedding_id,
        "match_result_id": binding.match_result_id,
        "similarity": binding.similarity,
        "threshold": watchlist_event["threshold"],
        "identity_source": "match_results",
        "identity_binding_status": "matched",
        "recognition_claim_allowed": True,
        "visual_evidence_status": "watchlist_hit_identity_bound",
    }
    obj["action"] = {
        **_dict(obj.get("action")),
        "event_type": EVENT_TYPE,
        "source_event_id": watchlist_event["source_event_id"],
        "watchlist_rule_id": watchlist_rule_id,
        "severity": severity,
        "status": "event_triggered",
    }
    obj["style"] = {
        **_dict(obj.get("style")),
        "bbox_color": "#D50000",
        "label_color": "#D50000",
        "reason": "watchlist_hit",
        "priority": 80,
    }
    if _geometry_snapshot(obj) != original_geometry:
        raise RuntimeError("watchlist_patch_modified_geometry")
    return patched


def patch_summary(
    summary: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    binding: KnownFaceBinding,
    watchlist_event: dict[str, Any],
    watchlist_rule_id: str,
    watchlist_rule_name: str,
    severity: str,
) -> dict[str, Any]:
    output = json.loads(json.dumps(summary))
    counts = _count_objects(rows)
    output.update(
        {
            "evidence_topology": "post_savant",
            "evidence_capture_mode": "stable_post_savant_sink_time_crop",
            "workaround_used": True,
            "event_style_replay_job_passed": False,
            "event_type": EVENT_TYPE,
            "source_event_id": watchlist_event["source_event_id"],
            "watchlist_hit_count": 1,
            "watchlist_rule_id": watchlist_rule_id,
            "watchlist_rule_name": watchlist_rule_name,
            "watchlist_severity": severity,
            "identity_binding_connected": True,
            "identity_patch_source": "match_results",
            "recognition_claim_allowed": counts["known_face"] > 0,
            "fallback_used": False,
            "legacy_used_for_visual_binding": False,
            "allow_db_annotation_fallback": False,
            "allow_legacy_annotation_fallback": False,
            "production_ready": True,
            "watchlist_event_file": WATCHLIST_EVENT_FILE,
            "watchlist_evidence_summary_file": WATCHLIST_SUMMARY_FILE,
            "watchlist_evidence_report_file": WATCHLIST_REPORT_FILE,
            "source_observation_id": binding.source_observation_id,
            "person_id": binding.person_id,
            "external_person_id": binding.external_person_id,
            "gallery_embedding_id": binding.gallery_embedding_id,
            "match_result_id": binding.match_result_id,
            "similarity": binding.similarity,
            "threshold": watchlist_event["threshold"],
            "frame_num": binding.frame_index,
            "frame_pts": binding.frame_pts,
            "track_id": binding.track_id,
            "object_counts": {
                "person": counts["person"],
                "face": counts["face"],
                "known_face": counts["known_face"],
            },
            "known_face_count": counts["known_face"],
            "unknown_face_count": counts["face"],
            "known_face_objects_count": counts["known_face"],
            "face_objects_count": counts["face"],
            "person_objects_count": counts["person"],
        }
    )
    limitations = list(output.get("limitations") or [])
    for item in (
        "deterministic_test_watchlist_event",
        "similarity_1_0_controlled_self_match",
        "event_style_replay_not_production_ready_stable_sink_workaround",
        "live_viewer_http_not_required_for_c2_5_contract",
    ):
        if item not in limitations:
            limitations.append(item)
    output["limitations"] = limitations
    return output


def build_watchlist_summary(
    *,
    input_bundle: Path,
    output_bundle: Path,
    summary: dict[str, Any],
    binding: KnownFaceBinding,
    watchlist_event: dict[str, Any],
    watchlist_rule_id: str,
    watchlist_rule_name: str,
) -> dict[str, Any]:
    return {
        "result_marker": RESULT_PASS,
        "input_bundle": str(input_bundle),
        "output_bundle": str(output_bundle),
        "event_type": EVENT_TYPE,
        "watchlist_rule_id": watchlist_rule_id,
        "watchlist_rule_name": watchlist_rule_name,
        "source_event_id": watchlist_event["source_event_id"],
        "person_id": binding.person_id,
        "external_person_id": binding.external_person_id,
        "person_name": watchlist_event["person_name"],
        "identity_person_name": binding.person_name,
        "gallery_embedding_id": binding.gallery_embedding_id,
        "match_result_id": binding.match_result_id,
        "source_observation_id": binding.source_observation_id,
        "similarity": binding.similarity,
        "threshold": watchlist_event["threshold"],
        "frame_num": binding.frame_index,
        "frame_pts": binding.frame_pts,
        "track_id": binding.track_id,
        "join_method": binding.join_method,
        "face_bbox": binding.face_bbox,
        "evidence_capture_mode": summary.get("evidence_capture_mode"),
        "workaround_used": summary.get("workaround_used"),
        "event_style_replay_job_passed": summary.get("event_style_replay_job_passed"),
        "known_face_count": summary.get("known_face_count"),
        "watchlist_hit_count": summary.get("watchlist_hit_count"),
        "unknown_face_count": summary.get("unknown_face_count"),
        "production_ready": summary.get("production_ready"),
        "video_integrity": summary.get("video_integrity"),
        "fallback_used": summary.get("fallback_used"),
        "legacy_used_for_visual_binding": summary.get("legacy_used_for_visual_binding"),
        "allow_db_annotation_fallback": summary.get("allow_db_annotation_fallback"),
        "allow_legacy_annotation_fallback": summary.get("allow_legacy_annotation_fallback"),
        "limitations": summary.get("limitations") or [],
    }


def assert_no_forbidden_event_payload(event: dict[str, Any]) -> None:
    hits = sorted(set(_find_forbidden_keys(event)))
    if hits:
        raise RuntimeError(f"watchlist_event_contains_forbidden_payload:{','.join(hits)}")


def _find_forbidden_keys(value: Any, *, parent_key: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_EVENT_KEYS:
                yield parent_key + str(key)
            yield from _find_forbidden_keys(nested, parent_key=parent_key + str(key) + ".")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _find_forbidden_keys(nested, parent_key=f"{parent_key}{index}.")


def _validate_input_summary(summary: dict[str, Any]) -> None:
    if summary.get("evidence_capture_mode") != "stable_post_savant_sink_time_crop":
        raise RuntimeError("stable_sink_capture_mode_required")
    if summary.get("workaround_used") is not True:
        raise RuntimeError("workaround_used_required")
    if summary.get("event_style_replay_job_passed") is not False:
        raise RuntimeError("event_style_replay_job_must_not_be_passed")
    if summary.get("identity_binding_connected") is not True:
        raise RuntimeError("identity_binding_connected_required")
    if int(summary.get("known_face_count") or _dict(summary.get("object_counts")).get("known_face") or 0) <= 0:
        raise RuntimeError("known_face_count_positive_required")
    if summary.get("production_ready") is not True:
        raise RuntimeError("production_ready_required")
    if summary.get("fallback_used") is not False:
        raise RuntimeError("fallback_used_must_be_false")
    if summary.get("legacy_used_for_visual_binding") is not False:
        raise RuntimeError("legacy_used_for_visual_binding_must_be_false")
    if summary.get("allow_db_annotation_fallback") is not False:
        raise RuntimeError("allow_db_annotation_fallback_must_be_false")
    if summary.get("allow_legacy_annotation_fallback") is not False:
        raise RuntimeError("allow_legacy_annotation_fallback_must_be_false")
    video_integrity = _dict(summary.get("video_integrity"))
    if video_integrity.get("production_gate_passed") is not True:
        raise RuntimeError("video_integrity_gate_required")


def _validate_output(
    summary: dict[str, Any],
    watchlist_event: dict[str, Any],
    watchlist_summary: dict[str, Any],
) -> None:
    assert_no_forbidden_event_payload(watchlist_event)
    if watchlist_event.get("event_type") != EVENT_TYPE:
        raise RuntimeError("watchlist_event_type_invalid")
    required_event = (
        "source_observation_id",
        "person_id",
        "gallery_embedding_id",
        "match_result_id",
        "similarity",
        "threshold",
        "watchlist_rule_id",
    )
    for key in required_event:
        if watchlist_event.get(key) in (None, ""):
            raise RuntimeError(f"watchlist_event_missing_{key}")
    for key in ("person_id", "gallery_embedding_id", "match_result_id"):
        if int(watchlist_event.get(key) or 0) <= 0:
            raise RuntimeError(f"watchlist_event_invalid_{key}")
    payload = _dict(watchlist_event.get("payload"))
    if payload.get("identity_source") != "match_results":
        raise RuntimeError("watchlist_event_identity_source_invalid")
    if payload.get("identity_binding_status") != "matched":
        raise RuntimeError("watchlist_event_identity_binding_status_invalid")
    if payload.get("evidence_capture_mode") != "stable_post_savant_sink_time_crop":
        raise RuntimeError("watchlist_event_capture_mode_invalid")
    if payload.get("workaround_used") is not True:
        raise RuntimeError("watchlist_event_workaround_required")
    if payload.get("event_style_replay_job_passed") is not False:
        raise RuntimeError("watchlist_event_replay_status_invalid")
    if payload.get("geometry_modified") is not False:
        raise RuntimeError("watchlist_event_geometry_modified")
    if int(summary.get("watchlist_hit_count") or 0) != 1:
        raise RuntimeError("watchlist_hit_count_must_be_1")
    if int(summary.get("known_face_count") or 0) <= 0:
        raise RuntimeError("known_face_count_positive_required")
    if summary.get("evidence_capture_mode") != "stable_post_savant_sink_time_crop":
        raise RuntimeError("summary_capture_mode_invalid")
    if summary.get("workaround_used") is not True:
        raise RuntimeError("summary_workaround_required")
    if summary.get("event_style_replay_job_passed") is not False:
        raise RuntimeError("summary_replay_status_invalid")
    if summary.get("fallback_used") is not False:
        raise RuntimeError("summary_fallback_used")
    if summary.get("legacy_used_for_visual_binding") is not False:
        raise RuntimeError("summary_legacy_used")
    if summary.get("allow_db_annotation_fallback") is not False:
        raise RuntimeError("summary_db_fallback_allowed")
    if summary.get("allow_legacy_annotation_fallback") is not False:
        raise RuntimeError("summary_legacy_fallback_allowed")
    if summary.get("production_ready") is not True:
        raise RuntimeError("summary_production_ready_required")
    if _dict(summary.get("video_integrity")).get("production_gate_passed") is not True:
        raise RuntimeError("summary_video_integrity_gate_required")
    if summary.get("identity_binding_connected") is not True:
        raise RuntimeError("summary_identity_binding_connected_required")
    if watchlist_summary.get("event_type") != EVENT_TYPE:
        raise RuntimeError("watchlist_summary_event_type_invalid")


def _require_input_bundle(bundle: Path) -> None:
    if not bundle.is_dir():
        raise FileNotFoundError(f"input bundle missing: {bundle}")
    for name in (SIDECAR_FILE, SUMMARY_FILE, IDENTITY_PATCHES_FILE, "sink_metadata.json"):
        path = bundle / name
        if not path.is_file():
            raise FileNotFoundError(f"required bundle file missing: {path}")
    if not any((bundle / name).is_file() for name in ("raw_clip.mov", "raw_clip.mp4")):
        raise FileNotFoundError(f"raw clip missing in bundle: {bundle}")


def _event_ts_ms(binding: KnownFaceBinding) -> int | None:
    if binding.frame_pts is None:
        return None
    return int(round(binding.frame_pts / 1_000_000))


def _sidecar_bbox_xyxy(obj: dict[str, Any]) -> list[float] | None:
    bbox = obj.get("bbox")
    if not isinstance(bbox, dict):
        return None
    values = bbox.get("xyxy")
    if not isinstance(values, list) or len(values) < 4:
        return None
    parsed = [_float_or_none(value) for value in values[:4]]
    if any(value is None for value in parsed):
        return None
    return [float(value) for value in parsed if value is not None]


def _object_type(obj: dict[str, Any]) -> str:
    object_type = _text_or_none(obj.get("object_type"))
    if object_type:
        return object_type
    kind = _text_or_none(_dict(obj.get("label")).get("kind"))
    if kind == "known_face":
        return "known_face"
    if kind in {"face", "unknown_face"}:
        return "face"
    return "unknown"


def _count_objects(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"person": 0, "face": 0, "known_face": 0}
    for row in rows:
        for obj in row.get("objects") or []:
            if not isinstance(obj, dict):
                continue
            object_type = _object_type(obj)
            if object_type in counts:
                counts[object_type] += 1
    return counts


def _geometry_snapshot(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "bbox": json.loads(json.dumps(obj.get("bbox"))),
        "landmarks": json.loads(json.dumps(obj.get("landmarks"))),
        "pose": json.loads(json.dumps(obj.get("pose"))),
        "track_id": obj.get("track_id"),
        "object_id": obj.get("object_id"),
    }


def _write_report(path: Path, summary: dict[str, Any]) -> None:
    rows = [
        ("Result", summary.get("result_marker")),
        ("Event", "Watchlist Hit"),
        ("Rule", f"{summary.get('watchlist_rule_name')} ({summary.get('watchlist_rule_id')})"),
        ("Person", f"{summary.get('person_id')} / {summary.get('external_person_id')}"),
        ("Source observation", summary.get("source_observation_id")),
        ("Similarity", f"{summary.get('similarity')} / threshold {summary.get('threshold')}"),
        ("Frame", f"{summary.get('frame_num')} pts={summary.get('frame_pts')} track={summary.get('track_id')}"),
        ("Capture mode", summary.get("evidence_capture_mode")),
        ("Workaround", f"workaround_used={summary.get('workaround_used')} event_style_replay_job_passed={summary.get('event_style_replay_job_passed')}"),
    ]
    table = "\n".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in rows
    )
    limitations = "".join(f"<li>{html.escape(str(item))}</li>" for item in summary.get("limitations") or [])
    path.write_text(
        f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <title>C2.5 Watchlist Evidence</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 32px; color: #1f2937; }}
    table {{ border-collapse: collapse; min-width: 760px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; text-align: left; }}
    th {{ width: 220px; background: #f3f4f6; }}
    .warn {{ margin-top: 24px; padding: 12px; background: #fff7ed; border: 1px solid #fed7aa; }}
  </style>
</head>
<body>
  <h1>Watchlist Hit</h1>
  <table>{table}</table>
  <div class=\"warn\">
    <strong>Boundary:</strong> stable sink workaround is still active; event-style Replay has not passed.
  </div>
  <h2>Limitations</h2>
  <ul>{limitations}</ul>
</body>
</html>
""",
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"expected object JSONL row at {path}:{line_number}")
        rows.append(payload)
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_required(value: Any, field: str) -> float:
    parsed = _float_or_none(value)
    if parsed is None:
        raise RuntimeError(f"{field}_required")
    return parsed


def _result_payload(result: BuildResult) -> dict[str, Any]:
    return {
        "result_marker": result.result_marker,
        "input_bundle": str(result.input_bundle),
        "output_bundle": str(result.output_bundle),
        "watchlist_event_path": str(result.watchlist_event_path),
        "watchlist_evidence_summary_path": str(result.watchlist_summary_path),
        "watchlist_evidence_report_path": str(result.report_path),
        "summary_path": str(result.summary_path),
        "sidecar_path": str(result.sidecar_path),
        "source_event_id": result.watchlist_event.get("source_event_id"),
        "watchlist_rule_id": result.watchlist_event.get("watchlist_rule_id"),
        "person_id": result.watchlist_event.get("person_id"),
        "external_person_id": result.watchlist_event.get("external_person_id"),
        "source_observation_id": result.watchlist_event.get("source_observation_id"),
        "gallery_embedding_id": result.watchlist_event.get("gallery_embedding_id"),
        "match_result_id": result.watchlist_event.get("match_result_id"),
        "similarity": result.watchlist_event.get("similarity"),
        "threshold": result.watchlist_event.get("threshold"),
        "frame_num": result.watchlist_event.get("frame_num"),
        "track_id": result.watchlist_event.get("track_id"),
        "known_face_count": result.summary.get("known_face_count"),
        "watchlist_hit_count": result.summary.get("watchlist_hit_count"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-bundle", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--person-id", required=True, type=int)
    parser.add_argument("--external-person-id", required=True)
    parser.add_argument("--source-observation-id", required=True)
    parser.add_argument("--watchlist-rule-id", default=DEFAULT_WATCHLIST_RULE_ID)
    parser.add_argument("--watchlist-rule-name", default=DEFAULT_WATCHLIST_RULE_NAME)
    parser.add_argument("--person-name", default=DEFAULT_PERSON_NAME)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--severity", default="high")
    parser.add_argument("--camera-id", default=DEFAULT_CAMERA_ID)
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    parser.add_argument("--source-event-id", default=None)
    parser.add_argument("--overwrite", action="store_true", default=False)
    args = parser.parse_args(argv)

    try:
        result = build_watchlist_evidence_bundle(
            input_bundle=args.input_bundle,
            output_dir=args.output_dir,
            person_id=args.person_id,
            external_person_id=args.external_person_id,
            source_observation_id=args.source_observation_id,
            watchlist_rule_id=args.watchlist_rule_id,
            watchlist_rule_name=args.watchlist_rule_name,
            person_name=args.person_name,
            threshold=args.threshold,
            severity=args.severity,
            camera_id=args.camera_id,
            source_id=args.source_id,
            source_event_id=args.source_event_id,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        payload = {
            "result_marker": RESULT_FAIL,
            "reason": f"{type(exc).__name__}:{exc}",
            "input_bundle": str(args.input_bundle),
            "output_bundle": str(args.output_dir),
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    print(json.dumps(_result_payload(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
