"""Resolve operator-safe evidence details for API event responses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


FORBIDDEN_RESPONSE_KEYS = {
    "embedding",
    "embeddings",
    "embedding_vector",
    "embedding_values",
    "embedding_list",
    "image_bytes",
    "crop_bytes",
    "frame_bytes",
    "raw_frame",
    "base64",
    "base64_image",
    "image_base64",
    "crop",
    "crop_image",
    "image",
}


def resolve_event_evidence_detail(event: Any) -> dict[str, Any]:
    """Build a metadata-only evidence detail payload for an EventResponse.

    The resolver reads small JSON summary files from the evidence bundle path
    declared in the event payload. It returns file paths and derived metadata;
    it never returns raw sidecar contents, embeddings, or image bytes.
    """

    payload = _dict(getattr(event, "payload", {}))
    media = _dict(getattr(event, "media", {}))
    evidence_payload = _dict(payload.get("evidence"))
    bundle_path = (
        _text(evidence_payload.get("bundle_path"))
        or _text(payload.get("evidence_bundle_path"))
        or _text(media.get("evidence_bundle_path"))
    )
    audit_path = (
        _text(evidence_payload.get("audit_path"))
        or _text(media.get("evidence_audit_path"))
    )
    capture_mode = (
        _text(evidence_payload.get("capture_mode"))
        or _text(payload.get("evidence_capture_mode"))
        or _text(media.get("evidence_capture_mode"))
    )
    workaround_used = _first_bool(
        evidence_payload.get("workaround_used"),
        payload.get("workaround_used"),
        media.get("workaround_used"),
    )
    replay_passed = _first_bool(
        evidence_payload.get("event_style_replay_job_passed"),
        payload.get("event_style_replay_job_passed"),
        media.get("event_style_replay_job_passed"),
    )

    bundle_dir = Path(bundle_path) if bundle_path else None
    summary = _read_json_if_exists(bundle_dir / "summary.json") if bundle_dir else {}
    c2_6r_summary = (
        _read_json_if_exists(bundle_dir / "c2_6r_redis_watchlist_summary.json")
        if bundle_dir
        else {}
    )
    watchlist_summary = (
        _read_json_if_exists(bundle_dir / "watchlist_evidence_summary.json")
        if bundle_dir
        else {}
    )
    selected_summary = summary or c2_6r_summary or watchlist_summary
    video_integrity = _dict(
        selected_summary.get("video_integrity")
        or c2_6r_summary.get("video_integrity")
        or watchlist_summary.get("video_integrity")
    )
    files = _evidence_files(bundle_dir)
    c2_7_output_dir = _resolve_c2_7_output_dir(
        explicit=_text(payload.get("c2_7_output_dir")),
        source_event_id=_text(getattr(event, "source_event_id", "")),
    )

    person = {
        "person_id": _int_or_none(
            getattr(event, "person_id", None)
            or payload.get("person_id")
            or _dict(payload.get("matched_person")).get("person_id")
        ),
        "external_person_id": (
            _text(payload.get("external_person_id"))
            or _text(_dict(payload.get("matched_person")).get("external_person_id"))
        ),
    }
    watchlist = {
        "watchlist_rule_id": _text(payload.get("watchlist_rule_id")),
        "similarity": _float_or_none(payload.get("similarity")),
        "threshold": _float_or_none(payload.get("threshold")),
        "match_result_id": _int_or_none(payload.get("match_result_id")),
        "gallery_embedding_id": _int_or_none(payload.get("gallery_embedding_id")),
    }
    source_observation_id = _text(
        payload.get("source_observation_id")
        or _dict(payload.get("match")).get("source_observation_id")
    )
    detail = {
        "event_id": _text(getattr(event, "id", "")),
        "source_event_id": _text(getattr(event, "source_event_id", "")),
        "event_type": _text(getattr(event, "event_type", "")),
        "status": _text(getattr(event, "status", "")),
        "camera_id": _text(getattr(event, "camera_id", "")),
        "source_id": _text(getattr(event, "source_id", "")),
        "track_id": _text(getattr(event, "track_id", "")),
        "source_observation_id": source_observation_id,
        "person": person,
        "watchlist": watchlist,
        "evidence": {
            "bundle_path": bundle_path,
            "bundle_exists": bool(bundle_dir and bundle_dir.is_dir()),
            "c2_7_output_dir": c2_7_output_dir,
            "raw_clip_path": files["raw_clip_path"],
            "raw_clip_url": _media_url(files["raw_clip_path"]),
            "summary_path": files["summary_path"],
            "summary_url": _media_url(files["summary_path"]),
            "sidecar_path": files["sidecar_path"],
            "sidecar_url": _media_url(files["sidecar_path"]),
            "watchlist_event_path": files["watchlist_event_path"],
            "watchlist_event_url": _media_url(files["watchlist_event_path"]),
            "audit_path": audit_path,
            "audit_url": _media_url(audit_path),
            "report_path": files["report_path"],
            "report_url": _media_url(files["report_path"]),
            "video_integrity_status": _text(video_integrity.get("integrity_status")),
            "video_integrity_production_gate_passed": video_integrity.get("production_gate_passed"),
            "production_ready": _first_bool(
                selected_summary.get("production_ready"),
                c2_6r_summary.get("production_ready"),
                watchlist_summary.get("production_ready"),
            ),
            "known_face_count": _int_or_none(selected_summary.get("known_face_count")),
            "watchlist_hit_count": _int_or_none(selected_summary.get("watchlist_hit_count")),
            "capture_mode": capture_mode,
            "workaround_used": workaround_used,
            "event_style_replay_job_passed": replay_passed,
        },
        "limitations": _limitations(
            selected_summary.get("limitations"),
            workaround_used=workaround_used,
            event_style_replay_job_passed=replay_passed,
        ),
    }
    scan = unsafe_payload_scan(detail)
    detail["unsafe_payload_scan"] = scan
    return detail


def unsafe_payload_scan(value: Any) -> dict[str, Any]:
    hits = sorted(set(_find_forbidden(value)))
    return {
        "payload_has_embedding": any("embedding" in hit.lower() for hit in hits),
        "payload_has_image_bytes": any(
            token in hit.lower()
            for hit in hits
            for token in ("image", "base64", "crop", "frame_bytes", "raw_frame")
        ),
        "forbidden_key_paths": hits,
    }


def _evidence_files(bundle_dir: Path | None) -> dict[str, str]:
    if bundle_dir is None:
        return {
            "raw_clip_path": "",
            "summary_path": "",
            "sidecar_path": "",
            "watchlist_event_path": "",
            "report_path": "",
        }
    raw_clip = _first_existing(bundle_dir, ("raw_clip.mov", "raw_clip.mp4"))
    watchlist_event = _first_existing(
        bundle_dir,
        (
            "redis_watchlist_event.json",
            "live_watchlist_event.json",
            "watchlist_event.json",
        ),
    )
    report = _first_existing(
        bundle_dir,
        (
            "watchlist_evidence_report.html",
            "index.html",
        ),
    )
    return {
        "raw_clip_path": str(raw_clip) if raw_clip else "",
        "summary_path": str(bundle_dir / "summary.json") if (bundle_dir / "summary.json").is_file() else "",
        "sidecar_path": str(bundle_dir / "annotations.frame_cache.identity.jsonl")
        if (bundle_dir / "annotations.frame_cache.identity.jsonl").is_file()
        else "",
        "watchlist_event_path": str(watchlist_event) if watchlist_event else "",
        "report_path": str(report) if report else "",
    }


def _first_existing(root: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _resolve_c2_7_output_dir(*, explicit: str, source_event_id: str) -> str:
    if explicit:
        return explicit
    marker = "c2_7_event_worker_persistence_"
    if marker not in source_event_id:
        return ""
    run_id = marker + source_event_id.rsplit(marker, 1)[1]
    candidate = Path("/data/video-analytics/media/evidence") / run_id
    return str(candidate) if candidate.is_dir() else ""


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _limitations(
    existing: Any,
    *,
    workaround_used: bool | None,
    event_style_replay_job_passed: bool | None,
) -> list[str]:
    values = [str(item) for item in existing if isinstance(item, str)] if isinstance(existing, list) else []
    if workaround_used is True and "stable_sink_workaround" not in values:
        values.append("stable_sink_workaround")
    if event_style_replay_job_passed is False and "event_style_replay_not_passed" not in values:
        values.append("event_style_replay_not_passed")
    if "not_broad_accuracy_test" not in values:
        values.append("not_broad_accuracy_test")
    return values


def _media_url(path: str | None) -> str | None:
    if not path:
        return None
    value = str(path)
    media_root = "/data/video-analytics/media"
    if value.startswith(media_root + "/"):
        return "/media" + value[len(media_root) :]
    if value.startswith("/media/"):
        return value
    return None


def _find_forbidden(value: Any, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_RESPONSE_KEYS:
                hits.append(next_path)
            hits.extend(_find_forbidden(nested, next_path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            next_path = f"{path}.{index}" if path else str(index)
            hits.extend(_find_forbidden(nested, next_path))
    elif isinstance(value, str):
        lowered = value.lower()
        if ";base64," in lowered or lowered.startswith("data:image"):
            hits.append(path or "value")
    return hits


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_bool(*values: Any) -> bool | None:
    for value in values:
        if isinstance(value, bool):
            return value
    return None
