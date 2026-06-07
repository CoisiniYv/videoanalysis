"""C2.13 RTSP watchlist + intrusion smoke contract tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "scripts" / "tools" / "run_c2_13_rtsp_watchlist_intrusion_probe.py"


def test_pass_requires_input_type_rtsp() -> None:
    tool = _load_tool()
    decision = tool.decide_overall(
        input_type="rtsp",
        rtsp_url="rtsp://camera/live",
        watchlist_result=tool.WATCHLIST_PASS,
        intrusion_result=tool.INTRUSION_PASS,
    )

    assert decision["result_marker"] == tool.RESULT_PASS


def test_local_file_input_cannot_produce_full_pass() -> None:
    tool = _load_tool()
    decision = tool.decide_overall(
        input_type="file",
        rtsp_url="",
        watchlist_result=tool.WATCHLIST_PASS,
        intrusion_result=tool.INTRUSION_PASS,
    )

    assert decision["result_marker"] != tool.RESULT_PASS
    assert decision["result_marker"] == tool.RESULT_RTSP_CONFIG_MISSING


def test_watchlist_pass_requires_real_source_observation_and_target_identity() -> None:
    tool = _load_tool()
    valid = _watchlist_match(similarity=0.70)
    invalid_identity = _watchlist_match(similarity=0.70)
    invalid_identity["query_external_person_id"] = "test:c2_4:person"
    missing_sid = _watchlist_match(similarity=0.70)
    missing_sid["source_observation_id"] = ""

    assert tool.is_valid_watchlist_pass_match(valid, threshold=0.65) is True
    assert tool.is_valid_watchlist_pass_match(invalid_identity, threshold=0.65) is False
    assert tool.is_valid_watchlist_pass_match(missing_sid, threshold=0.65) is False


def test_watchlist_pass_requires_similarity_above_threshold() -> None:
    tool = _load_tool()
    assert tool.is_valid_watchlist_pass_match(_watchlist_match(similarity=0.64), threshold=0.65) is False
    assert tool.is_valid_watchlist_pass_match(_watchlist_match(similarity=0.65), threshold=0.65) is True


def test_intrusion_pass_requires_real_intrusion_event_type() -> None:
    tool = _load_tool()

    assert tool.is_valid_intrusion_event({"event_type": "intrusion", "source_event_id": "x"}) is True
    assert tool.is_valid_intrusion_event({"event_type": "watchlist_hit", "source_event_id": "x"}) is False


def test_no_watchlist_match_returns_partial_not_fake_pass() -> None:
    tool = _load_tool()
    result = tool.decide_watchlist_result(
        input_type="rtsp",
        face_observation_count=10,
        best_match=_watchlist_match(similarity=0.40),
        threshold=0.65,
        watchlist_hit_count=0,
    )

    assert result["result"] == tool.WATCHLIST_NO_MATCH


def test_no_intrusion_trigger_returns_partial_not_fake_pass() -> None:
    tool = _load_tool()
    result = tool.decide_intrusion_result(
        input_type="rtsp",
        roi_configured=True,
        person_pose_count=12,
        intrusion_event_count=0,
    )

    assert result["result"] == tool.INTRUSION_NO_TRIGGER


def test_event_payload_has_no_embedding() -> None:
    tool = _load_tool()
    scan = tool.scan_for_unsafe_payload(
        {"event": {"payload": {"match": {"similarity": 0.7}, "face_bbox": [1, 2, 3, 4]}}}
    )

    assert scan["payload_has_embedding"] is False
    assert scan["forbidden_key_paths"] == []


def test_event_payload_has_no_image_base64_or_crop_bytes() -> None:
    tool = _load_tool()
    scan = tool.scan_for_unsafe_payload({"event": {"payload": {"image_base64": "data:image/jpeg;base64,AAAA"}}})

    assert scan["passed"] is False
    assert scan["payload_has_image_bytes"] is True


def test_rtsp_url_secrets_are_redacted() -> None:
    tool = _load_tool()
    redacted = tool.redact_url("rtsp://user:pass@example.test:8554/live")

    assert redacted == "rtsp://***:***@example.test:8554/live"
    assert "pass" not in redacted


def test_event_style_replay_not_claimed() -> None:
    tool = _load_tool()
    visual = {
        "visual_evidence_status": "not_generated",
        "reason": "C2.13 prioritizes RTSP algorithm/event verification; event-style Replay still not passed",
        "event_style_replay_job_passed": False,
    }

    assert visual["event_style_replay_job_passed"] is False


def test_visual_evidence_absence_does_not_fail_algorithm_smoke() -> None:
    tool = _load_tool()
    decision = tool.decide_overall(
        input_type="rtsp",
        rtsp_url="rtsp://camera/live",
        watchlist_result=tool.WATCHLIST_PASS,
        intrusion_result=tool.INTRUSION_NO_TRIGGER,
    )

    assert decision["result_marker"] == tool.RESULT_WATCHLIST_READY_INTRUSION_NO_TRIGGER


def test_rtsp_config_missing_marker_when_no_url() -> None:
    tool = _load_tool()
    decision = tool.decide_overall(
        input_type="missing",
        rtsp_url=None,
        watchlist_result=tool.WATCHLIST_NO_FACE_OBSERVATIONS,
        intrusion_result=tool.INTRUSION_NO_POSE_PERSON,
    )

    assert decision["result_marker"] == tool.RESULT_RTSP_CONFIG_MISSING


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("run_c2_13_rtsp_watchlist_intrusion_probe", TOOL_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _watchlist_match(*, similarity: float) -> dict[str, Any]:
    return {
        "query_external_person_id": "demo:f4_3:finch",
        "query_person_id": 6,
        "query_gallery_embedding_id": 5,
        "similarity": similarity,
        "source_observation_id": "face:c2_post_savant_fps_probe:1:1234",
        "fake_match_used": False,
        "gallery_self_match_used": False,
    }
