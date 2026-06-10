"""C2.14C RTSP intrusion evidence acceptance contract tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
AUDIT_TOOL = ROOT / "scripts" / "tools" / "audit_c2_14c_rtsp_evidence_bundle.py"


def test_summary_flags_reject_db_fallback_and_event_style_replay() -> None:
    tool = _load(AUDIT_TOOL)
    summary = _summary(db_window_fallback_used=True, event_style_replay_job_passed=True)

    result = tool.evaluate_summary_flags(summary)

    assert result["passed"] is False
    assert "db_window_fallback_used_false" in result["failure_reasons"]
    assert "event_style_replay_job_passed_false" in result["failure_reasons"]


def test_sidecar_alignment_requires_count_match_or_explanation() -> None:
    tool = _load(AUDIT_TOOL)

    result = tool.evaluate_sidecar_alignment(decoded_frame_count=75, sidecar_rows=[{}] * 74, summary={})

    assert result["passed"] is False
    assert result["status"] == "unexplained_mismatch"


def test_sidecar_alignment_accepts_explicit_explanation() -> None:
    tool = _load(AUDIT_TOOL)

    result = tool.evaluate_sidecar_alignment(
        decoded_frame_count=75,
        sidecar_rows=[{}] * 74,
        summary={"sidecar_alignment_explanation": "one trailing decode-only frame"},
    )

    assert result["passed"] is True
    assert result["status"] == "explained_mismatch"


def test_event_coverage_requires_event_frame_in_sidecar_and_metadata() -> None:
    tool = _load(AUDIT_TOOL)
    summary = _summary(event_frame_pts=50, event_ts_ms=5000)
    event = {"event_ts_ms": 5000}

    result = tool.evaluate_event_coverage(
        event=event,
        summary=summary,
        sidecar_rows=[{"frame_pts": 1}, {"frame_pts": 100}],
        metadata_rows=[{"pts": 1}, {"pts": 100}],
        expected_frame_pts=50,
        expected_event_ts_ms=5000,
    )

    assert result["passed"] is False
    assert "event_frame_pts_present" in result["failure_reasons"]


def test_event_coverage_accepts_exact_event_frame() -> None:
    tool = _load(AUDIT_TOOL)
    summary = _summary(event_frame_pts=50, event_ts_ms=5000)
    event = {"event_ts_ms": 5000}

    result = tool.evaluate_event_coverage(
        event=event,
        summary=summary,
        sidecar_rows=[{"frame_pts": 1}, {"frame_pts": 50}, {"frame_pts": 100}],
        metadata_rows=[{"pts": 1}, {"pts": 50}, {"pts": 100}],
        expected_frame_pts=50,
        expected_event_ts_ms=5000,
    )

    assert result["passed"] is True


def test_operator_report_accepts_summary_references_and_records_html_gap(tmp_path: Path) -> None:
    tool = _load(AUDIT_TOOL)
    (tmp_path / "operator_rtsp_event_clip_report.html").write_text("raw_clip.mp4", encoding="utf-8")
    summary = _summary(
        raw_clip=str(tmp_path / "raw_clip.mp4"),
        sink_metadata=str(tmp_path / "sink_metadata.json"),
        sidecar=str(tmp_path / "annotations.frame_cache.identity.jsonl"),
    )

    result = tool.evaluate_operator_reports(tmp_path, summary)

    assert result["passed"] is True
    assert result["summary_report_references_all"] is True
    assert result["operator_html_reference_gap"] is True
    assert result["live_evidence_viewer"] == "not_checked"


def test_unsafe_payload_scan_rejects_embedding_and_image_bytes() -> None:
    tool = _load(AUDIT_TOOL)

    result = tool.scan_unsafe_payload({"sidecar": {"embedding": [0.1] * 32, "image_base64": "abc"}})

    assert result["passed"] is False
    assert result["payload_has_embedding"] is True
    assert result["payload_has_image_bytes"] is True


def test_retention_fixture_deletes_only_expired_target_segment(tmp_path: Path) -> None:
    tool = _load(AUDIT_TOOL)
    fixture = tmp_path / "c2_14c_retention_fixture_case"

    result = tool.run_retention_fixture(fixture)

    assert result["result_marker"] == tool.RESULT_PASS
    assert result["checks"]["expired_segment_deleted"] is True
    assert result["checks"]["min_keep_segment_preserved"] is True
    assert result["checks"]["non_target_source_preserved"] is True
    assert result["checks"]["unsafe_path_preserved"] is True
    assert result["delete_candidate_count"] == 1
    assert result["deleted_count"] == 1
    assert not fixture.exists()


def test_bundle_missing_returns_partial(tmp_path: Path) -> None:
    tool = _load(AUDIT_TOOL)

    result = tool.audit_bundle(tmp_path / "missing", require_canonical_root=False)

    assert result["result_marker"] == tool.RESULT_BUNDLE_MISSING


def _summary(**overrides: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "evidence_capture_mode": "rtsp_segment_ring",
        "fallback_used": False,
        "db_window_fallback_used": False,
        "legacy_used_for_visual_binding": False,
        "event_style_replay_job_passed": False,
        "visual_evidence_required_for_evidence_pass": True,
        "selected_segment_window_covers_event": True,
        "event_frame_located_in_filtered_metadata": True,
        "event_frame_pts": 8313013044444,
        "event_ts_ms": 1780891860312,
        "raw_clip": "/bundle/raw_clip.mp4",
        "sink_metadata": "/bundle/sink_metadata.json",
        "sidecar": "/bundle/annotations.frame_cache.identity.jsonl",
        "acceptance": {
            "passed": True,
            "db_window_fallback_used": False,
            "legacy_used_for_visual_binding": False,
            "event_style_replay_job_passed": False,
        },
    }
    for key, value in overrides.items():
        if key in {
            "db_window_fallback_used",
            "legacy_used_for_visual_binding",
            "event_style_replay_job_passed",
        }:
            summary[key] = value
            summary["acceptance"][key] = value
        else:
            summary[key] = value
    return summary


def _load(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module
