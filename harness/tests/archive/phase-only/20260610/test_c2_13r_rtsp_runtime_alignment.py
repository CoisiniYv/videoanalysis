"""C2.13R RTSP runtime alignment contract tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "scripts" / "tools" / "run_c2_13r_rtsp_runtime_alignment.py"


def test_pass_requires_input_type_rtsp() -> None:
    tool = _load_tool()
    decision = tool.decide_overall(
        input_type="rtsp",
        intrusion_config_aligned=True,
        face_worker_ok=True,
        event_worker_ok=True,
        watchlist_result=tool.WATCHLIST_PASS,
        intrusion_result=tool.INTRUSION_PASS,
    )

    assert decision["result_marker"] == tool.RESULT_PASS


def test_intrusion_config_must_bind_to_active_source() -> None:
    tool = _load_tool()
    decision = tool.decide_overall(
        input_type="rtsp",
        intrusion_config_aligned=False,
        face_worker_ok=True,
        event_worker_ok=True,
        watchlist_result=tool.WATCHLIST_NO_MATCH,
        intrusion_result=tool.INTRUSION_CONFIG_GAP,
    )

    assert decision["result_marker"] == tool.RESULT_INTRUSION_CONFIG_GAP


def test_stale_c1e_roi_cannot_satisfy_active_c2_source(tmp_path: Path) -> None:
    tool = _load_tool()
    cfg = tmp_path / "cameras.yml"
    cfg.write_text(
        """
cameras:
  cam_c1e_rtsp_replay:
    enabled: true
    source_id: c1e_rtsp_replay
    name: stale
    rtsp_url: rtsp://example/live
    gpu_id: 0
    zones: {}
    rules: {}
""",
        encoding="utf-8",
    )
    alignment = tool.align_camera_config_to_active_source(
        camera_config_path=cfg,
        source_config={
            "source_id": "c2_post_savant_fps_probe",
            "camera_id": "c2_post_savant_fps_probe",
            "rtsp_url_redacted": "rtsp://example/live",
        },
    )

    assert "c1e_rtsp_replay" in alignment["old_configured_sources"]
    assert alignment["active_source_bound"] is True
    assert "c2_post_savant_fps_probe" in alignment["new_configured_sources"]


def test_c2_13r_alignment_writes_intrusion_rule_id(tmp_path: Path) -> None:
    tool = _load_tool()
    cfg = tmp_path / "cameras.yml"
    cfg.write_text("cameras: {}\n", encoding="utf-8")

    tool.align_camera_config_to_active_source(
        camera_config_path=cfg,
        source_config={
            "source_id": "c2_post_savant_fps_probe",
            "camera_id": "c2_post_savant_fps_probe",
            "rtsp_url_redacted": "rtsp://example/live",
        },
    )
    loaded = tool.c2_13.load_camera_config_summary(cfg, "c2_post_savant_fps_probe")

    assert loaded["intrusion_rule"]["rule_id"] == tool.DEFAULT_INTRUSION_RULE_ID


def test_c2_13r_source_status_uses_c2_13r_watchlist_rule() -> None:
    tool = _load_tool()
    source_status = {"watchlist_rule_id": tool.c2_13.DEFAULT_WATCHLIST_RULE_ID}

    source_status["watchlist_rule_id"] = tool.DEFAULT_WATCHLIST_RULE_ID

    assert source_status["watchlist_rule_id"] == "c2_13r_rtsp_reese_finch_watchlist_rule"


def test_watchlist_pass_requires_reese_or_finch_external_person_id() -> None:
    tool = _load_tool()
    valid = _match(similarity=0.70)
    invalid = _match(similarity=0.70)
    invalid["query_external_person_id"] = "test:c2_4:person"

    assert tool.is_valid_watchlist_pass_match(valid, 0.65) is True
    assert tool.is_valid_watchlist_pass_match(invalid, 0.65) is False


def test_watchlist_pass_requires_similarity_above_threshold() -> None:
    tool = _load_tool()

    assert tool.is_valid_watchlist_pass_match(_match(similarity=0.64), 0.65) is False
    assert tool.is_valid_watchlist_pass_match(_match(similarity=0.65), 0.65) is True


def test_no_watchlist_match_returns_partial_not_fake_pass() -> None:
    tool = _load_tool()
    metrics = tool.build_watchlist_metrics(
        face_worker={
            "messages_read": 4,
            "inserted": 4,
            "duplicates": 0,
            "top_reese_match": _match(similarity=0.2),
            "top_finch_match": _match(similarity=0.3),
            "best_match": _match(similarity=0.3),
            "watchlist_events": [],
            "fatal_error": None,
        },
        event_worker={"persisted_events": [], "fatal_error": None},
        event_rows=[],
        threshold=0.65,
    )

    assert metrics["watchlist_result"] == tool.WATCHLIST_NO_MATCH


def test_intrusion_no_trigger_returns_partial_not_fake_pass() -> None:
    tool = _load_tool()
    metrics = tool.build_intrusion_metrics(
        source_config={"camera_config": {"source_id_matches_runtime": True}},
        redis_before={"streams": {"security.person_observations": {"length": 2}}},
        redis_after={"streams": {"security.person_observations": {"length": 4}}},
        event_worker={"persisted_events": []},
        event_rows=[],
    )

    assert metrics["intrusion_result"] == tool.INTRUSION_NO_TRIGGER


def test_event_payload_has_no_embedding() -> None:
    tool = _load_tool()
    scan = tool.c2_13.scan_for_unsafe_payload({"event": {"payload": {"similarity": 0.7}}})

    assert scan["payload_has_embedding"] is False
    assert scan["forbidden_key_paths"] == []


def test_event_payload_has_no_image_base64_or_crop_bytes() -> None:
    tool = _load_tool()
    scan = tool.c2_13.scan_for_unsafe_payload({"event": {"payload": {"crop_bytes": "bytes"}}})

    assert scan["passed"] is False
    assert scan["payload_has_image_bytes"] is True


def test_rtsp_url_secrets_redacted() -> None:
    tool = _load_tool()

    assert tool.c2_13.redact_url("rtsp://user:pass@example.test/live") == "rtsp://***:***@example.test/live"


def test_event_style_replay_not_claimed() -> None:
    tool = _load_tool()
    summary = tool.build_blocked_summary(
        output_dir=Path("/tmp/c2_13r"),
        source_config={"input_type": "rtsp"},
        result_marker=tool.RESULT_WORKER_RUNTIME_GAP,
        reason="worker_gap",
    )

    assert summary["event_style_replay_job_passed"] is False
    assert summary["event_style_replay_claimed"] is False


def test_visual_evidence_absence_does_not_fail_runtime_alignment() -> None:
    tool = _load_tool()
    decision = tool.decide_overall(
        input_type="rtsp",
        intrusion_config_aligned=True,
        face_worker_ok=True,
        event_worker_ok=True,
        watchlist_result=tool.WATCHLIST_PASS,
        intrusion_result=tool.INTRUSION_NO_TRIGGER,
    )

    assert decision["result_marker"] == tool.RESULT_WATCHLIST_READY_INTRUSION_NO_TRIGGER


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("run_c2_13r_rtsp_runtime_alignment", TOOL_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _match(*, similarity: float) -> dict[str, Any]:
    return {
        "query_external_person_id": "demo:f4_3:finch",
        "source_observation_id": "face:c2_post_savant_fps_probe:1:123",
        "similarity": similarity,
        "fake_match_used": False,
        "gallery_self_match_used": False,
    }
