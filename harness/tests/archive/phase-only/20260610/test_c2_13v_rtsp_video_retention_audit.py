"""C2.13V RTSP video evidence and retention audit contract tests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "scripts" / "tools" / "run_c2_13v_rtsp_video_retention_audit.py"


def test_pass_video_evidence_requires_source_time_coverage() -> None:
    tool = _load_tool()

    result = tool.evaluate_video_evidence_acceptance(
        raw_clip_exists=True,
        metadata_exists=True,
        sidecar_exists=True,
        source_id_matches=False,
        timestamp_or_frame_covered=True,
        event_frame_located=True,
        decoded_video_frame_count=10,
        sidecar_frame_count=10,
        video_integrity_pass=True,
        fallback_used=False,
        legacy_used_for_visual_binding=False,
        db_window_fallback_used=False,
        safe_time_domain_crop=False,
    )

    assert result["passed"] is False
    assert "source_id_mismatch" in result["failure_reasons"]


def test_pass_video_evidence_requires_video_integrity_pass() -> None:
    tool = _load_tool()

    result = tool.evaluate_video_evidence_acceptance(
        raw_clip_exists=True,
        metadata_exists=True,
        sidecar_exists=True,
        source_id_matches=True,
        timestamp_or_frame_covered=True,
        event_frame_located=True,
        decoded_video_frame_count=10,
        sidecar_frame_count=10,
        video_integrity_pass=False,
        fallback_used=False,
        legacy_used_for_visual_binding=False,
        db_window_fallback_used=False,
        safe_time_domain_crop=False,
    )

    assert result["passed"] is False
    assert "video_integrity_failed" in result["failure_reasons"]


def test_event_style_replay_failure_cannot_be_hidden() -> None:
    tool = _load_tool()

    evidence = tool.build_video_evidence_results(
        {
            "generated_bundles": [],
            "priority_a_sink_attempts": [],
            "priority_b_replay_attempts": [
                {
                    "priority": "B",
                    "status": tool.REPLAY_FAILED,
                    "reason": "replay_video_integrity_failed",
                    "evidence_status": "fail",
                }
            ],
        }
    )
    decision = tool.decide_overall_marker(
        input_type="rtsp",
        event_count=1,
        evidence_generated_count=evidence["evidence_generated_count"],
        retention_policy_status=tool.RETENTION_BOUNDED_CONFIG,
        unsafe_payload_passed=True,
        runtime_errors=[],
    )

    assert evidence["replay_video_status"] == tool.REPLAY_FAILED
    assert decision["result_marker"] == tool.RESULT_VIDEO_GAP


def test_fixed_frame_count_240_cannot_be_treated_as_10_seconds() -> None:
    tool = _load_tool()

    payload = {
        "configuration": {"stored_stream_id": "c2_post_savant_fps_probe"},
        "stop_condition": {"frame_count": 240},
    }

    assert tool.fixed_frame_count_as_duration_proxy(payload, requested_duration_s=10.0) is True


def test_replay_payload_uses_time_domain_stop_condition_not_frame_count() -> None:
    tool = _load_tool()

    payload = tool.build_replay_job_payload(
        event=_event(),
        keyframe_uuid="key-a",
        replay_sink_url="dealer+connect:tcp://video-file-sink:6666",
        resulting_stream_id="replay-event-c2-13v-test",
    )

    assert payload["stop_condition"] == {"ts_delta_sec": {"max_delta_sec": 10.0}}
    assert "frame_count" not in payload["stop_condition"]
    assert "ts_discrepancy_fix_duration" in payload["configuration"]


def test_no_sink_coverage_returns_partial_not_fake_evidence(tmp_path: Path) -> None:
    tool = _load_tool()
    event = _event()

    attempt = tool.try_existing_sink_coverage(
        event=event,
        sink_root=tmp_path / "missing",
        output_dir=tmp_path / "out",
    )
    decision = tool.decide_overall_marker(
        input_type="rtsp",
        event_count=1,
        evidence_generated_count=0,
        retention_policy_status=tool.RETENTION_BOUNDED_CONFIG,
        unsafe_payload_passed=True,
        runtime_errors=[],
    )

    assert attempt["reason"] == "video_file_sink_not_recording"
    assert decision["result_marker"] == tool.RESULT_VIDEO_GAP


def test_replay_attempt_is_limited_to_one_event(monkeypatch, tmp_path: Path) -> None:
    tool = _load_tool()
    calls = []

    def fake_replay(**kwargs):
        calls.append(kwargs["event"]["source_event_id"])
        return {
            "event_identity": tool.event_identity(kwargs["event"]),
            "priority": "B",
            "status": tool.REPLAY_FAILED,
            "reason": "replay_retention_gap",
            "evidence_status": "fail",
        }

    monkeypatch.setattr(tool, "try_replay_event_video", fake_replay)
    attempts = tool.associate_visual_evidence(
        events=[_event(), {**_event(), "source_event_id": "src-evt-2", "event_id": "evt-2"}],
        architecture={"active_paths": {"video_file_sink_root": str(tmp_path / "missing")}},
        output_dir=tmp_path / "out",
        replay_api_url="http://127.0.0.1:8098",
        replay_sink_url="dealer+connect:tcp://video-file-sink:6666",
        try_replay=True,
        replay_wait_seconds=1,
    )

    assert calls == ["src-evt-1"]
    assert attempts["priority_b_replay_attempts"][1]["reason"] == "one_replay_event_already_attempted"


def test_unknown_retention_policy_returns_partial_not_pass() -> None:
    tool = _load_tool()

    decision = tool.decide_overall_marker(
        input_type="rtsp",
        event_count=1,
        evidence_generated_count=1,
        retention_policy_status=tool.RETENTION_UNKNOWN,
        unsafe_payload_passed=True,
        runtime_errors=[],
    )

    assert decision["result_marker"] == tool.RESULT_RETENTION_UNKNOWN


def test_unbounded_growth_risk_is_reported() -> None:
    tool = _load_tool()

    retention = tool.decide_retention_policy_status(
        config_report={
            "replay_storage_policy": {"classification": tool.RETENTION_BOUNDED_CONFIG},
            "video_file_sink_policy": {"classification": tool.RETENTION_UNKNOWN},
            "evidence_storage_policy": {"classification": tool.RETENTION_UNKNOWN},
        },
        disk_growth_summary={
            "total_delta_bytes": 4096,
            "automatic_removal_observed": False,
        },
    )

    assert retention["retention_policy_status"] == tool.RETENTION_UNKNOWN
    assert retention["unknown_unbounded_risk"] is True
    assert retention["unbounded_growth_risk_reported"] is True


def test_replay_runtime_deletion_alone_does_not_bound_all_media() -> None:
    tool = _load_tool()

    retention = tool.decide_retention_policy_status(
        config_report={
            "replay_storage_policy": {
                "classification": tool.RETENTION_BOUNDED_CONFIG,
                "storage_dir": "/replay",
            },
            "video_file_sink_policy": {
                "classification": tool.RETENTION_UNKNOWN,
                "output_root": "/sink",
            },
            "evidence_storage_policy": {
                "classification": tool.RETENTION_UNKNOWN,
                "path": "/evidence",
            },
        },
        disk_growth_summary={
            "automatic_removal_observed": True,
            "total_delta_bytes": -100,
            "paths": [
                {"path": "/replay", "delta_bytes": -100},
                {"path": "/sink", "delta_bytes": 10},
                {"path": "/evidence", "delta_bytes": 10},
            ],
        },
    )

    assert retention["replay_runtime_bounded_observed"] is True
    assert retention["bounded_runtime_verified"] is False
    assert retention["retention_policy_status"] == tool.RETENTION_UNKNOWN


def test_report_redacts_rtsp_url_secrets() -> None:
    tool = _load_tool()

    redacted = tool.redact_runtime_value({"rtsp_url": "rtsp://user:pass@10.0.0.2:8554/live"})

    assert redacted["rtsp_url"] == "rtsp://***:***@10.0.0.2:8554/live"


def test_output_has_no_embedding_vectors() -> None:
    tool = _load_tool()

    scan = tool.c2_13.scan_for_unsafe_payload({"payload": {"person_bbox": [1, 2, 3, 4]}})

    assert scan["passed"] is True
    assert scan["payload_has_embedding"] is False


def test_output_has_no_image_base64_or_crop_bytes() -> None:
    tool = _load_tool()

    scan = tool.c2_13.scan_for_unsafe_payload({"payload": {"crop_bytes": "abc"}})

    assert scan["passed"] is False
    assert scan["payload_has_image_bytes"] is True


def test_db_fallback_and_legacy_fallback_cannot_satisfy_video_pass() -> None:
    tool = _load_tool()

    result = tool.evaluate_video_evidence_acceptance(
        raw_clip_exists=True,
        metadata_exists=True,
        sidecar_exists=True,
        source_id_matches=True,
        timestamp_or_frame_covered=True,
        event_frame_located=True,
        decoded_video_frame_count=10,
        sidecar_frame_count=10,
        video_integrity_pass=True,
        fallback_used=False,
        legacy_used_for_visual_binding=True,
        db_window_fallback_used=True,
        safe_time_domain_crop=False,
    )

    assert result["passed"] is False
    assert "legacy_used_for_visual_binding" in result["failure_reasons"]
    assert "db_window_fallback_used" in result["failure_reasons"]


def test_visual_evidence_optional_for_algorithm_event_pass_but_required_for_evidence_pass() -> None:
    tool = _load_tool()

    evidence = tool.build_video_evidence_results(
        {"generated_bundles": [], "priority_a_sink_attempts": [], "priority_b_replay_attempts": []}
    )
    decision = tool.decide_overall_marker(
        input_type="rtsp",
        event_count=1,
        evidence_generated_count=0,
        retention_policy_status=tool.RETENTION_BOUNDED_CONFIG,
        unsafe_payload_passed=True,
        runtime_errors=[],
    )

    assert evidence["visual_evidence_optional_for_algorithm_event_pass"] is True
    assert evidence["visual_evidence_required_for_evidence_pass"] is True
    assert decision["result_marker"] == tool.RESULT_VIDEO_GAP


def test_sink_join_requires_active_source_and_event_frame(tmp_path: Path) -> None:
    tool = _load_tool()
    sink_dir = tmp_path / "sink" / "other%" / "unknown%"
    sink_dir.mkdir(parents=True)
    (sink_dir / "video.mov").write_bytes(b"not-real-video")
    _write_metadata(
        sink_dir / "metadata.json",
        [
            {"source_id": "other_source", "pts": 1000, "uuid": "frame-a", "objects": []},
            {"source_id": "other_source", "pts": 2000, "uuid": "frame-b", "objects": []},
        ],
    )

    report = tool.inspect_sink_event_join(sink_dir, _event(frame_pts=1500), mode="direct_sink")

    assert report["source_id_matches"] is False
    assert report["event_frame_located"] is False
    assert report["join_failure_reason"] == "no_sink_coverage"


def _event(frame_pts: int = 1000) -> dict[str, Any]:
    return {
        "event_id": "evt-1",
        "source_event_id": "src-evt-1",
        "event_type": "intrusion",
        "source_id": "c2_post_savant_fps_probe",
        "camera_id": "c2_post_savant_fps_probe",
        "track_id": "7",
        "frame_pts": frame_pts,
        "event_ts_ms": 123456789,
        "frame_uuid": "frame-a",
        "keyframe_uuid": "key-a",
    }


def _write_metadata(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _load_tool():
    spec = importlib.util.spec_from_file_location("c2_13v_tool", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
