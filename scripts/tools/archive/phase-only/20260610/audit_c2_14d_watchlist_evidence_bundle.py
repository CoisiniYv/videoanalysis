#!/usr/bin/env python3
"""Audit a C2.14D RTSP watchlist known-face evidence bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tools import build_c2_14d_watchlist_clip_from_ring as builder  # noqa: E402
from scripts.tools import manage_c2_14_rtsp_segment_ring as ring  # noqa: E402


SCHEMA_VERSION = "1.0-c2.14d-watchlist-evidence-audit"
RESULT_PASS = builder.RESULT_PASS
RESULT_NO_HIT = builder.RESULT_NO_HIT
RESULT_HIT_OUTSIDE_RING = builder.RESULT_HIT_OUTSIDE_RING
RESULT_IDENTITY_GAP = builder.RESULT_IDENTITY_GAP
RESULT_VIDEO_GAP = builder.RESULT_VIDEO_GAP
RESULT_FAKE_HIT = builder.RESULT_FAKE_HIT
RESULT_UNSAFE_PAYLOAD = builder.RESULT_UNSAFE_PAYLOAD
RESULT_DB_BROAD_FALLBACK = builder.RESULT_DB_BROAD_FALLBACK
RESULT_WRONG_WORKTREE = builder.RESULT_WRONG_WORKTREE


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"expected object at {path}:{line_number}")
        rows.append(payload)
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def int_or_none(value: Any) -> int | None:
    return ring.int_or_none(value)


def float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def known_face_objects(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    objects = []
    for row in rows:
        for obj in row.get("objects") or []:
            if isinstance(obj, dict) and obj.get("object_type") == "known_face":
                objects.append({"row": row, "object": obj})
    return objects


def audit_bundle(bundle: Path) -> dict[str, Any]:
    if not bundle.is_dir():
        return {"schema_version": SCHEMA_VERSION, "status": "partial", "result_marker": RESULT_NO_HIT, "bundle_path": str(bundle), "reason": "bundle_missing"}
    summary_path = bundle / "summary.json"
    if not summary_path.is_file():
        return {"schema_version": SCHEMA_VERSION, "status": "partial", "result_marker": RESULT_NO_HIT, "bundle_path": str(bundle), "reason": "summary_missing"}
    summary = load_json(summary_path)
    marker = summary.get("result_marker")
    if marker != RESULT_PASS:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": summary.get("status") or "partial",
            "result_marker": marker,
            "bundle_path": str(bundle),
            "summary": summary,
            "reason": summary.get("reason"),
        }

    required_paths = {
        "raw_clip": bundle / "raw_clip.mp4",
        "sink_metadata": bundle / "sink_metadata.json",
        "sidecar": bundle / "annotations.frame_cache.identity.jsonl",
        "selected_watchlist_event": bundle / "selected_watchlist_event.json",
        "video_integrity_report": bundle / "video_integrity_report.json",
        "index_html": bundle / "index.html",
    }
    failures: list[str] = []
    for name, path in required_paths.items():
        if not path.is_file() or path.stat().st_size <= 0:
            failures.append(f"{name}_missing")
    if failures:
        return {"schema_version": SCHEMA_VERSION, "status": "partial", "result_marker": RESULT_VIDEO_GAP, "bundle_path": str(bundle), "failure_reasons": failures}

    event = load_json(required_paths["selected_watchlist_event"])
    sidecar_rows = read_jsonl(required_paths["sidecar"])
    integrity = load_json(required_paths["video_integrity_report"])
    unsafe_scan = load_json(bundle / "unsafe_payload_scan.json") if (bundle / "unsafe_payload_scan.json").is_file() else {"passed": None}
    computed_scan = builder.scan_for_unsafe_payload({"summary": summary, "event": event, "sidecar_rows": sidecar_rows})
    known = known_face_objects(sidecar_rows)
    similarity = float_or_none(summary.get("similarity"))
    threshold = float_or_none(summary.get("threshold"))
    sidecar_count = len(sidecar_rows)
    decoded_count = int_or_none(integrity.get("decoded_frame_count"))
    identity_methods = [
        ((item["object"].get("identity") or {}).get("identity_binding_method"))
        for item in known
    ]
    checks = {
        "event_type_watchlist": summary.get("event_type") == "watchlist_hit" and event.get("event_type") == "watchlist_hit",
        "source_id_present": bool(summary.get("source_id")),
        "person_id_target": int_or_none(summary.get("person_id")) in builder.TARGET_PERSON_IDS,
        "external_person_target": summary.get("external_person_id") in builder.TARGET_EXTERNAL_IDS,
        "source_observation_id_present": bool(summary.get("source_observation_id")),
        "gallery_embedding_id_present": int_or_none(summary.get("gallery_embedding_id")) is not None,
        "similarity_passes_threshold": similarity is not None and threshold is not None and similarity >= threshold,
        "frame_anchor_present": int_or_none(summary.get("frame_pts")) is not None or bool(summary.get("frame_uuid")),
        "known_face_sidecar_object_present": len(known) >= 1,
        "identity_binding_direct": any(method in {"direct_source_observation_id", "direct_frame_pts"} for method in identity_methods),
        "decoded_frames_gt_zero": decoded_count is not None and decoded_count > 0,
        "sidecar_frames_gt_zero": sidecar_count > 0,
        "decoded_sidecar_frames_match": decoded_count == sidecar_count,
        "video_integrity_pass": bool(integrity.get("production_gate_passed")),
        "unsafe_payload_scan_pass": bool(unsafe_scan.get("passed")) and bool(computed_scan.get("passed")),
        "no_db_broad_fallback": summary.get("db_broad_window_fallback_used") is False and summary.get("db_window_fallback_used") is False,
        "no_legacy_binding": summary.get("legacy_used_for_visual_binding") is False,
        "no_event_style_replay": summary.get("event_style_replay_job_passed") is False,
        "not_fake_watchlist_hit": summary.get("fake_watchlist_hit") is False,
    }
    failures = [name for name, passed in checks.items() if not passed]
    result_marker = RESULT_PASS
    status = "pass"
    if not checks["not_fake_watchlist_hit"] or not checks["similarity_passes_threshold"]:
        result_marker = RESULT_FAKE_HIT
        status = "fail"
    elif not checks["unsafe_payload_scan_pass"]:
        result_marker = RESULT_UNSAFE_PAYLOAD
        status = "fail"
    elif not checks["no_db_broad_fallback"]:
        result_marker = RESULT_DB_BROAD_FALLBACK
        status = "fail"
    elif not checks["known_face_sidecar_object_present"] or not checks["identity_binding_direct"]:
        result_marker = RESULT_IDENTITY_GAP
        status = "partial"
    elif not checks["video_integrity_pass"] or not checks["decoded_sidecar_frames_match"]:
        result_marker = RESULT_VIDEO_GAP
        status = "partial"
    elif failures:
        result_marker = RESULT_IDENTITY_GAP
        status = "partial"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "result_marker": result_marker,
        "bundle_path": str(bundle),
        "raw_clip": str(required_paths["raw_clip"]),
        "decoded_frame_count": decoded_count,
        "sidecar_frame_count": sidecar_count,
        "person_id": summary.get("person_id"),
        "external_person_id": summary.get("external_person_id"),
        "similarity": summary.get("similarity"),
        "threshold": summary.get("threshold"),
        "source_observation_id": summary.get("source_observation_id"),
        "frame_pts": summary.get("frame_pts"),
        "event_ts_ms": summary.get("event_ts_ms"),
        "identity_binding_method": summary.get("identity_binding_method"),
        "checks": checks,
        "failure_reasons": failures,
        "unsafe_payload_scan": {"existing": unsafe_scan, "computed": computed_scan, "passed": checks["unsafe_payload_scan_pass"]},
        "limitations": summary.get("limitations") or [],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = audit_bundle(args.bundle)
    if args.json_output:
        write_json(args.json_output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    marker = str(result.get("result_marker") or "")
    if marker == RESULT_PASS:
        return 0
    if marker.startswith("PARTIAL_"):
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
