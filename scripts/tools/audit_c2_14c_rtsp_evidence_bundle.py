#!/usr/bin/env python3
"""Audit the accepted C2.14B RTSP intrusion evidence bundle."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(MEDIA_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(MEDIA_WORKER_ROOT))

from app.post_savant_video_integrity import inspect_video_integrity  # type: ignore  # noqa: E402
from scripts.tools import manage_c2_14_rtsp_segment_ring as ring  # noqa: E402


SCHEMA_VERSION = "1.0-c2.14c-rtsp-evidence-acceptance"
CANONICAL_ROOT = Path("/home/user/video-analytics")
DEFAULT_BUNDLE = Path("/data/video-analytics/media/evidence/c2_14b_rtsp_event_clip_20260608T041137")
DEFAULT_SOURCE_ID = "c2_post_savant_fps_probe"
EXPECTED_EVENT_TYPE = "intrusion"
EXPECTED_SOURCE_EVENT_ID = "savant_security:c2_post_savant_fps_probe:682:intrusion:1780891860187"
EXPECTED_FRAME_PTS = 8313013044444
EXPECTED_EVENT_TS_MS = 1780891860312
TARGET_DURATION_S = 10.0
RESULT_PASS = "PASS_C2_14C_RTSP_INTRUSION_EVIDENCE_ACCEPTANCE_READY"
RESULT_BUNDLE_MISSING = "PARTIAL_C2_14C_BUNDLE_MISSING"
RESULT_SIDECAR_GAP = "PARTIAL_C2_14C_SIDECAR_ALIGNMENT_GAP"
RESULT_RETENTION_UNSAFE = "FAIL_C2_14C_RETENTION_UNSAFE"
RESULT_BINDING_INVALID = "FAIL_C2_14C_EVIDENCE_BINDING_INVALID"
RESULT_UNSAFE_PAYLOAD = "FAIL_C2_14C_UNSAFE_PAYLOAD"
RESULT_WRONG_WORKTREE = "FAIL_C2_14C_WRONG_WORKTREE"


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"expected object at {path}:{line_number}")
        rows.append(payload)
    return rows


def int_or_none(value: Any) -> int | None:
    return ring.int_or_none(value)


def _bool_false(value: Any) -> bool:
    return value is False


def _nested(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def frame_pts_values(rows: list[dict[str, Any]]) -> list[int]:
    values: list[int] = []
    for row in rows:
        value = int_or_none(row.get("frame_pts") or row.get("pts"))
        if value is not None:
            values.append(value)
    return values


def source_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {str(row.get("source_id")) for row in rows if row.get("source_id") not in (None, "")}


def evaluate_summary_flags(summary: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "evidence_capture_mode": summary.get("evidence_capture_mode") == "rtsp_segment_ring",
        "fallback_used_false": _bool_false(summary.get("fallback_used")),
        "db_window_fallback_used_false": _bool_false(summary.get("db_window_fallback_used"))
        and _bool_false(_nested(summary, "acceptance", "db_window_fallback_used")),
        "legacy_used_for_visual_binding_false": _bool_false(summary.get("legacy_used_for_visual_binding"))
        and _bool_false(_nested(summary, "acceptance", "legacy_used_for_visual_binding")),
        "event_style_replay_job_passed_false": _bool_false(summary.get("event_style_replay_job_passed"))
        and _bool_false(_nested(summary, "acceptance", "event_style_replay_job_passed")),
        "acceptance_passed": _nested(summary, "acceptance", "passed") is True,
        "visual_evidence_required": summary.get("visual_evidence_required_for_evidence_pass") is True,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "failure_reasons": [name for name, passed in checks.items() if not passed],
    }


def evaluate_sidecar_alignment(
    *,
    decoded_frame_count: int | None,
    sidecar_rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    sidecar_frame_count = len(sidecar_rows)
    if decoded_frame_count == sidecar_frame_count:
        return {
            "passed": True,
            "status": "matched",
            "decoded_frame_count": decoded_frame_count,
            "sidecar_frame_count": sidecar_frame_count,
            "explanation": "decoded_frame_count_equals_sidecar_frame_count",
        }
    explanation = summary.get("sidecar_alignment_explanation") or _nested(summary, "acceptance", "sidecar_alignment_explanation")
    return {
        "passed": bool(explanation),
        "status": "explained_mismatch" if explanation else "unexplained_mismatch",
        "decoded_frame_count": decoded_frame_count,
        "sidecar_frame_count": sidecar_frame_count,
        "explanation": explanation,
    }


def evaluate_event_coverage(
    *,
    event: dict[str, Any],
    summary: dict[str, Any],
    sidecar_rows: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
    expected_frame_pts: int,
    expected_event_ts_ms: int,
) -> dict[str, Any]:
    sidecar_pts = frame_pts_values(sidecar_rows)
    metadata_pts = frame_pts_values(metadata_rows)
    sidecar_covers_pts = bool(sidecar_pts) and min(sidecar_pts) <= expected_frame_pts <= max(sidecar_pts)
    metadata_covers_pts = bool(metadata_pts) and min(metadata_pts) <= expected_frame_pts <= max(metadata_pts)
    event_frame_present = expected_frame_pts in set(sidecar_pts) and expected_frame_pts in set(metadata_pts)
    event_ts_matches = (
        int_or_none(event.get("event_ts_ms")) == expected_event_ts_ms
        and int_or_none(summary.get("event_ts_ms")) == expected_event_ts_ms
    )
    checks = {
        "summary_window_covers_event": summary.get("selected_segment_window_covers_event") is True,
        "summary_event_frame_located": summary.get("event_frame_located_in_filtered_metadata") is True,
        "sidecar_pts_covers_event": sidecar_covers_pts,
        "metadata_pts_covers_event": metadata_covers_pts,
        "event_frame_pts_present": event_frame_present,
        "event_ts_ms_matches_event_and_summary": event_ts_matches,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "sidecar_first_frame_pts": min(sidecar_pts) if sidecar_pts else None,
        "sidecar_last_frame_pts": max(sidecar_pts) if sidecar_pts else None,
        "metadata_first_frame_pts": min(metadata_pts) if metadata_pts else None,
        "metadata_last_frame_pts": max(metadata_pts) if metadata_pts else None,
        "failure_reasons": [name for name, passed in checks.items() if not passed],
    }


def evaluate_operator_reports(bundle: Path, summary: dict[str, Any]) -> dict[str, Any]:
    raw_clip = str(summary.get("raw_clip") or "")
    sink_metadata = str(summary.get("sink_metadata") or "")
    sidecar = str(summary.get("sidecar") or "")
    summary_refs = {
        "raw_clip": raw_clip.endswith("/raw_clip.mp4"),
        "sink_metadata": sink_metadata.endswith("/sink_metadata.json"),
        "sidecar": sidecar.endswith("/annotations.frame_cache.identity.jsonl"),
    }
    html_path = bundle / "operator_rtsp_event_clip_report.html"
    html_exists = html_path.is_file()
    html_refs = {"raw_clip": False, "sink_metadata": False, "sidecar": False}
    if html_exists:
        text = html_path.read_text(encoding="utf-8", errors="replace")
        html_refs = {
            "raw_clip": "raw_clip.mp4" in text,
            "sink_metadata": "sink_metadata.json" in text,
            "sidecar": "annotations.frame_cache.identity.jsonl" in text,
        }
    return {
        "passed": all(summary_refs.values()) and (not html_exists or html_refs["raw_clip"]),
        "summary_report_references_all": all(summary_refs.values()),
        "summary_report_references": summary_refs,
        "operator_html_exists": html_exists,
        "operator_html_references": html_refs,
        "operator_html_reference_gap": html_exists and not all(html_refs.values()),
        "operator_html_path": str(html_path) if html_exists else None,
        "live_evidence_viewer": "not_checked",
        "live_evidence_viewer_checked": False,
    }


def scan_unsafe_payload(payload: Any) -> dict[str, Any]:
    return ring.scan_for_unsafe_payload(payload)


def audit_bundle(
    bundle: Path,
    *,
    expected_source_id: str = DEFAULT_SOURCE_ID,
    expected_event_type: str = EXPECTED_EVENT_TYPE,
    expected_source_event_id: str = EXPECTED_SOURCE_EVENT_ID,
    expected_frame_pts: int = EXPECTED_FRAME_PTS,
    expected_event_ts_ms: int = EXPECTED_EVENT_TS_MS,
    target_duration_s: float = TARGET_DURATION_S,
    require_canonical_root: bool = True,
) -> dict[str, Any]:
    root_ok = ROOT.resolve(strict=False) == CANONICAL_ROOT.resolve(strict=False)
    if require_canonical_root and not root_ok:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "fail",
            "result_marker": RESULT_WRONG_WORKTREE,
            "repo_root": str(ROOT),
            "expected_repo_root": str(CANONICAL_ROOT),
        }
    if not bundle.is_dir():
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "partial",
            "result_marker": RESULT_BUNDLE_MISSING,
            "bundle_path": str(bundle),
            "reason": "bundle_directory_missing",
        }

    raw_clip_path = bundle / "raw_clip.mp4"
    metadata_path = bundle / "sink_metadata.json"
    sidecar_path = bundle / "annotations.frame_cache.identity.jsonl"
    summary_path = bundle / "summary.json"
    event_path = bundle / "event.json"
    sidecar_summary_path = bundle / "summary.frame_cache.identity.json"
    existing_scan_path = bundle / "unsafe_payload_scan.json"

    failures: list[str] = []
    partials: list[str] = []
    files = {
        "raw_clip_exists": raw_clip_path.is_file() and raw_clip_path.stat().st_size > 0,
        "sink_metadata_exists": metadata_path.is_file() and metadata_path.stat().st_size > 0,
        "sidecar_exists": sidecar_path.is_file() and sidecar_path.stat().st_size > 0,
        "summary_exists": summary_path.is_file() and summary_path.stat().st_size > 0,
        "event_exists": event_path.is_file() and event_path.stat().st_size > 0,
    }
    missing = [name for name, ok in files.items() if not ok]
    if missing:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "partial",
            "result_marker": RESULT_BUNDLE_MISSING,
            "bundle_path": str(bundle),
            "files": files,
            "failure_reasons": missing,
        }

    summary = load_json(summary_path)
    event = load_json(event_path)
    sidecar_summary = load_json(sidecar_summary_path) if sidecar_summary_path.is_file() else {}
    metadata_rows = ring.load_native_metadata(metadata_path)
    sidecar_rows = read_jsonl(sidecar_path)
    integrity = inspect_video_integrity(
        raw_clip_path,
        requested_duration_s=target_duration_s,
        sidecar_frame_count=len(sidecar_rows),
        trim_occurred=bool(_nested(summary, "clip_result", "time_domain_crop_applied")),
        time_domain_crop_applied=bool(_nested(summary, "clip_result", "time_domain_crop_applied")),
    )
    existing_scan = load_json(existing_scan_path) if existing_scan_path.is_file() else {"passed": None}
    computed_scan = scan_unsafe_payload(
        {
            "event": event,
            "summary": summary,
            "sidecar_summary": sidecar_summary,
            "sidecar_rows": sidecar_rows,
        }
    )

    decoded_count = int_or_none(integrity.get("decoded_frame_count"))
    duration_s = integrity.get("duration_s")
    video_checks = {
        "raw_clip_decodable": bool(integrity.get("production_gate_passed")),
        "duration_reasonable": isinstance(duration_s, (int, float)) and 8.0 <= float(duration_s) <= 12.5,
        "decoded_frames_gt_zero": decoded_count is not None and decoded_count > 0,
        "decode_errors_zero": int_or_none(integrity.get("decode_error_count")) == 0,
    }
    if not all(video_checks.values()):
        failures.extend([name for name, ok in video_checks.items() if not ok])

    identity_checks = {
        "event_type_intrusion": event.get("event_type") == expected_event_type and summary.get("event_type") == expected_event_type,
        "source_id_summary": summary.get("source_id") == expected_source_id,
        "source_id_event": event.get("source_id") == expected_source_id,
        "source_event_id": event.get("source_event_id") == expected_source_event_id,
        "event_frame_pts": int_or_none(event.get("frame_pts")) == expected_frame_pts and int_or_none(summary.get("event_frame_pts")) == expected_frame_pts,
        "event_ts_ms": int_or_none(event.get("event_ts_ms")) == expected_event_ts_ms and int_or_none(summary.get("event_ts_ms")) == expected_event_ts_ms,
        "sidecar_source_id": source_ids(sidecar_rows) == {expected_source_id},
        "metadata_source_id": source_ids(metadata_rows) == {expected_source_id},
    }
    if not all(identity_checks.values()):
        failures.extend([name for name, ok in identity_checks.items() if not ok])

    summary_flags = evaluate_summary_flags(summary)
    if not summary_flags["passed"]:
        failures.extend(f"summary_flag:{reason}" for reason in summary_flags["failure_reasons"])

    alignment = evaluate_sidecar_alignment(decoded_frame_count=decoded_count, sidecar_rows=sidecar_rows, summary=summary)
    if not alignment["passed"]:
        partials.append("decoded_sidecar_frame_count_mismatch")

    coverage = evaluate_event_coverage(
        event=event,
        summary=summary,
        sidecar_rows=sidecar_rows,
        metadata_rows=metadata_rows,
        expected_frame_pts=expected_frame_pts,
        expected_event_ts_ms=expected_event_ts_ms,
    )
    if not coverage["passed"]:
        failures.extend(f"coverage:{reason}" for reason in coverage["failure_reasons"])

    operator_reports = evaluate_operator_reports(bundle, summary)
    if not operator_reports["passed"]:
        failures.append("operator_report_references_missing")

    unsafe_passed = bool(existing_scan.get("passed")) and bool(computed_scan.get("passed"))
    if not unsafe_passed:
        failures.append("unsafe_payload_detected")

    retention = run_retention_fixture()
    if retention["result_marker"] != RESULT_PASS:
        failures.append("retention_fixture_unsafe")

    marker = RESULT_PASS
    status = "pass"
    if "unsafe_payload_detected" in failures:
        marker = RESULT_UNSAFE_PAYLOAD
        status = "fail"
    elif "retention_fixture_unsafe" in failures:
        marker = RESULT_RETENTION_UNSAFE
        status = "fail"
    elif failures:
        marker = RESULT_BINDING_INVALID
        status = "fail"
    elif partials:
        marker = RESULT_SIDECAR_GAP
        status = "partial"

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "result_marker": marker,
        "bundle_path": str(bundle),
        "files": files,
        "event_type": event.get("event_type"),
        "source_id": summary.get("source_id"),
        "source_event_id": event.get("source_event_id"),
        "event_frame_pts": summary.get("event_frame_pts"),
        "event_ts_ms": summary.get("event_ts_ms"),
        "decoded_frame_count": decoded_count,
        "sidecar_frame_count": len(sidecar_rows),
        "metadata_frame_count": len(metadata_rows),
        "duration_s": duration_s,
        "video_integrity": {
            "integrity_status": integrity.get("integrity_status"),
            "production_gate_passed": integrity.get("production_gate_passed"),
            "decode_error_count": integrity.get("decode_error_count"),
            "failure_reasons": integrity.get("failure_reasons") or [],
            "warning_reasons": integrity.get("warning_reasons") or [],
        },
        "video_checks": video_checks,
        "identity_checks": identity_checks,
        "summary_flags": summary_flags,
        "sidecar_alignment": alignment,
        "event_coverage": coverage,
        "unsafe_payload_scan": {
            "passed": unsafe_passed,
            "existing_scan": existing_scan,
            "computed_scan": computed_scan,
        },
        "operator_reports": operator_reports,
        "retention_fixture": retention,
        "failure_reasons": failures,
        "partial_reasons": partials,
        "limitations": [
            "intrusion_event_clip_only",
            "watchlist_hit_known_face_clip_not_proven_by_c2_14c",
            "runtime_module_provenance_not_normalized_yet",
            "data_directory_not_cleaned",
            "worker_api_product_chain_not_restored",
            "live_evidence_viewer_not_checked",
        ],
    }


def run_retention_fixture(fixture_root: Path | None = None) -> dict[str, Any]:
    created_temp = fixture_root is None
    root = fixture_root or Path(tempfile.mkdtemp(prefix="c2_14c_retention_fixture_", dir="/tmp"))
    source_id = DEFAULT_SOURCE_ID
    ring_root = root / "ring"
    source_segments = ring_root / source_id / "segments"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    paths: dict[str, Path] = {}
    try:
        paths["expired"] = source_segments / "expired"
        paths["young"] = source_segments / "young"
        paths["active"] = source_segments / "active"
        paths["unsafe"] = root / "outside-target"
        paths["other_source"] = ring_root / "other_source" / "segments" / "expired-other"
        for path in paths.values():
            _make_fixture_segment(path)
        rows = [
            _fixture_index_row("expired", paths["expired"], created_at=now - timedelta(minutes=20), expires_at=now - timedelta(minutes=10)),
            _fixture_index_row("young", paths["young"], created_at=now - timedelta(seconds=30), expires_at=now - timedelta(seconds=29)),
            _fixture_index_row("active", paths["active"], created_at=now - timedelta(seconds=1), expires_at=now + timedelta(seconds=1)),
            _fixture_index_row("../outside-target", paths["unsafe"], created_at=now - timedelta(minutes=20), expires_at=now - timedelta(minutes=10)),
        ]
        other_rows = [
            _fixture_index_row(
                "expired-other",
                paths["other_source"],
                source_id="other_source",
                created_at=now - timedelta(minutes=20),
                expires_at=now - timedelta(minutes=10),
            )
        ]
        ring.write_jsonl(ring.index_path(ring_root, source_id), rows)
        ring.write_jsonl(ring.index_path(ring_root, "other_source"), other_rows)
        plan = ring.retention_plan(
            ring_root=ring_root,
            source_id=source_id,
            ttl_seconds=1,
            max_bytes=10_000_000,
            min_keep_seconds=120,
            now=now,
        )
        applied = ring.apply_retention_plan(plan, dry_run=False)
        checks = {
            "expired_segment_deleted": not paths["expired"].exists(),
            "min_keep_segment_preserved": paths["young"].exists(),
            "active_segment_preserved": paths["active"].exists(),
            "non_target_source_preserved": paths["other_source"].exists(),
            "unsafe_path_preserved": paths["unsafe"].exists(),
            "delete_candidate_count_matches": plan.get("delete_candidate_count") == 1,
            "deleted_count_matches": applied.get("deleted_count") == 1,
            "unsafe_row_skipped": len(plan.get("unsafe_rows_skipped") or []) == 1,
            "delete_error_count_zero": applied.get("delete_error_count") == 0,
        }
        marker = RESULT_PASS if all(checks.values()) else RESULT_RETENTION_UNSAFE
        return {
            "result_marker": marker,
            "fixture_root": str(root),
            "checks": checks,
            "delete_candidate_count": plan.get("delete_candidate_count"),
            "deleted_count": applied.get("deleted_count"),
            "unsafe_rows_skipped": plan.get("unsafe_rows_skipped") or [],
            "cleanup_attempted": True,
        }
    finally:
        if root.exists():
            shutil.rmtree(root)
        if created_temp:
            pass


def _make_fixture_segment(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "segment_manifest.json").write_text("{}", encoding="utf-8")
    (path / "video.mov").write_bytes(b"fixture")
    (path / "metadata.json").write_text("{}", encoding="utf-8")


def _fixture_index_row(
    segment_id: str,
    segment_dir: Path,
    *,
    source_id: str = DEFAULT_SOURCE_ID,
    created_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": ring.SCHEMA_VERSION,
        "source_id": source_id,
        "segment_id": segment_id,
        "segment_dir": str(segment_dir),
        "video_path": str(segment_dir / "video.mov"),
        "metadata_path": str(segment_dir / "metadata.json"),
        "first_frame_pts": 0,
        "last_frame_pts": 10_000_000_000,
        "first_timestamp_ms": 0,
        "last_timestamp_ms": 10_000,
        "first_frame_uuid": f"{segment_id}-first",
        "last_frame_uuid": f"{segment_id}-last",
        "frame_count": 10,
        "keyframe_count": 1,
        "size_bytes": 7,
        "created_at": ring.isoformat(created_at),
        "expires_at": ring.isoformat(expires_at),
        "evidence_refs": [],
        "cleanup_eligible": True,
        "completed": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    parser.add_argument("--json-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = audit_bundle(args.bundle, expected_source_id=args.source_id)
    if args.json_output:
        write_json(args.json_output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    marker = result.get("result_marker")
    if marker == RESULT_PASS:
        return 0
    if str(marker).startswith("PARTIAL_"):
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
