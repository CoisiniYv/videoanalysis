"""C2.13P RTSP pose/person intrusion probe contract tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "scripts" / "tools" / "run_c2_13p_rtsp_pose_intrusion_probe.py"


def test_pass_requires_rtsp_source() -> None:
    tool = _load_tool()

    decision = tool.decide_overall(
        input_type="file",
        pose_model_present=True,
        person_objects_in_metadata=True,
        person_observation_count=1,
        intrusion_event_count=1,
        intrusion_event_emitted_count=1,
        event_worker_ok=True,
        rule_bound=True,
    )

    assert decision["result_marker"] == tool.RESULT_FAIL


def test_pass_requires_real_person_pose_observation() -> None:
    tool = _load_tool()

    decision = tool.decide_overall(
        input_type="rtsp",
        pose_model_present=True,
        person_objects_in_metadata=True,
        person_observation_count=1,
        intrusion_event_count=1,
        intrusion_event_emitted_count=1,
        event_worker_ok=True,
        rule_bound=True,
    )

    assert decision["result_marker"] == tool.RESULT_PASS


def test_pass_requires_intrusion_event_not_fake_event() -> None:
    tool = _load_tool()

    decision = tool.decide_overall(
        input_type="rtsp",
        pose_model_present=True,
        person_objects_in_metadata=True,
        person_observation_count=4,
        intrusion_event_count=0,
        intrusion_event_emitted_count=0,
        event_worker_ok=True,
        rule_bound=True,
    )

    assert decision["result_marker"] == tool.RESULT_EXPORT_GAP


def test_missing_person_observations_returns_partial_not_fake_pass() -> None:
    tool = _load_tool()

    decision = tool.decide_overall(
        input_type="rtsp",
        pose_model_present=True,
        person_objects_in_metadata=True,
        person_observation_count=0,
        intrusion_event_count=0,
        intrusion_event_emitted_count=0,
        event_worker_ok=True,
        rule_bound=True,
    )

    assert decision["result_marker"] == tool.RESULT_EXPORT_GAP


def test_pose_metadata_present_exporter_gap_distinct_from_pose_model_missing() -> None:
    tool = _load_tool()
    export_gap = tool.decide_intrusion_result(
        input_type="rtsp",
        pose_model_present=True,
        person_objects_in_metadata=True,
        person_observation_count=0,
        rule_bound=True,
        intrusion_event_emitted_count=0,
        intrusion_event_persisted_count=0,
        event_worker_ok=True,
    )
    missing_model = tool.decide_intrusion_result(
        input_type="rtsp",
        pose_model_present=False,
        person_objects_in_metadata=False,
        person_observation_count=0,
        rule_bound=False,
        intrusion_event_emitted_count=0,
        intrusion_event_persisted_count=0,
        event_worker_ok=True,
    )

    assert export_gap["result"] == tool.RESULT_EXPORT_GAP
    assert missing_model["result"] == tool.RESULT_POSE_MODEL_NOT_ACTIVE


def test_event_payload_has_no_embedding() -> None:
    tool = _load_tool()
    scan = tool.c2_13.scan_for_unsafe_payload({"event": {"payload": {"person_bbox": [1, 2, 3, 4]}}})

    assert scan["payload_has_embedding"] is False
    assert scan["passed"] is True


def test_event_payload_has_no_image_base64_or_crop_bytes() -> None:
    tool = _load_tool()
    scan = tool.c2_13.scan_for_unsafe_payload({"event": {"payload": {"image_base64": "abc"}}})

    assert scan["payload_has_image_bytes"] is True
    assert scan["passed"] is False


def test_roi_must_bind_active_source_id() -> None:
    tool = _load_tool()
    probe = tool.build_intrusion_rule_input_probe(
        source_status={"input_type": "rtsp"},
        runtime_config_probe={
            "runtime_host_config_path": "/tmp/cameras.yml",
            "runtime_config": {
                "source_id_matches_runtime": True,
                "configured_sources": ["c2_post_savant_fps_probe"],
                "intrusion_rule": {"rule_id": "c2_13p_rtsp_intrusion_rule"},
            },
        },
        source_id="c2_post_savant_fps_probe",
        camera_id="c2_post_savant_fps_probe",
    )

    assert probe["roi_bound_to_active_source"] is True


def test_stale_c1e_source_cannot_satisfy_active_rtsp_intrusion() -> None:
    tool = _load_tool()
    probe = tool.build_intrusion_rule_input_probe(
        source_status={"input_type": "rtsp"},
        runtime_config_probe={
            "runtime_host_config_path": "/tmp/cameras.yml",
            "runtime_config": {
                "source_id_matches_runtime": False,
                "configured_sources": ["c1e_rtsp_replay"],
                "intrusion_rule": {"rule_id": "old"},
            },
        },
        source_id="c2_post_savant_fps_probe",
        camera_id="c2_post_savant_fps_probe",
    )

    assert probe["stale_c1e_source_present"] is True
    assert probe["stale_c1e_source_satisfies_active_source"] is False
    assert probe["roi_bound_to_active_source"] is False


def test_event_style_replay_not_claimed() -> None:
    tool = _load_tool()
    scan = tool.c2_13.scan_for_unsafe_payload(
        {"summary": {"event_style_replay_job_passed": False, "event_style_replay_claimed": False}}
    )

    assert scan["passed"] is True


def test_no_person_in_scene_returns_partial_not_fail() -> None:
    tool = _load_tool()
    decision = tool.decide_overall(
        input_type="rtsp",
        pose_model_present=True,
        person_objects_in_metadata=False,
        person_observation_count=0,
        intrusion_event_count=0,
        intrusion_event_emitted_count=0,
        event_worker_ok=True,
        rule_bound=True,
    )

    assert decision["result_marker"] == tool.RESULT_NO_PERSON_IN_SCENE


def test_parse_savant_logs_detects_pose_metadata_and_unknown_source() -> None:
    tool = _load_tool()
    parsed = tool.parse_savant_logs(
        "\n".join(
            [
                "stage=savant_security_behavior_rules_unknown_source source_id=c2_post_savant_fps_probe known_sources=['c1e_rtsp_replay']",
                "[face_assoc] frame=10 source=c2_post_savant_fps_probe persons=3 faces=1 associated=1",
                "stage=savant_security_behavior_rules_tick frame=10 source_id=c2_post_savant_fps_probe raw_observation_count=3 observation_count=3 tracked_count=3 track_count=3 events_exported=1 person_observations_exported=3 person_observations_skipped=0 total_person_observations_exported=3",
            ]
        ),
        source_id="c2_post_savant_fps_probe",
    )

    assert parsed["face_assoc_max_persons_per_frame"] == 3
    assert parsed["behavior_raw_person_max"] == 3
    assert parsed["behavior_person_observations_exported_sum"] == 3
    assert parsed["behavior_intrusion_events_exported_sum"] == 1
    assert parsed["behavior_rules_unknown_source_lines"]


def test_runtime_config_gap_detected_when_repo_has_source_but_runtime_does_not(tmp_path: Path) -> None:
    tool = _load_tool()
    repo = tmp_path / "repo.yml"
    runtime = tmp_path / "runtime.yml"
    repo.write_text(
        """
cameras:
  c2_post_savant_fps_probe:
    enabled: true
    source_id: c2_post_savant_fps_probe
    zones: {}
    rules:
      intrusion:
        enabled: true
""",
        encoding="utf-8",
    )
    runtime.write_text(
        """
cameras:
  stale:
    enabled: true
    source_id: c1e_rtsp_replay
    zones: {}
    rules:
      intrusion:
        enabled: true
""",
        encoding="utf-8",
    )
    probe = {
        "repo_config_path": str(repo),
        "runtime_host_config_path": str(runtime),
        "repo_config": tool.c2_13.load_camera_config_summary(repo, "c2_post_savant_fps_probe"),
        "runtime_config": tool.c2_13.load_camera_config_summary(runtime, "c2_post_savant_fps_probe"),
    }
    probe["runtime_config_gap"] = bool(probe["repo_config"]["source_id_matches_runtime"]) and not bool(
        probe["runtime_config"]["source_id_matches_runtime"]
    )

    assert tool.should_sync_runtime_config(probe) is True


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("run_c2_13p_rtsp_pose_intrusion_probe", TOOL_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
