"""Static contracts for the midterm 60-stream pressure runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT.parent / "scripts" / "runtime" / "run_midterm_pressure60.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_midterm_pressure60", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _config(module, **overrides):
    values = {
        "run_id": "pressure60_test",
        "stream_count": 2,
        "rtsp_uri": "rtsp://camera/live",
        "fps": "2/1",
        "min_fps": "1/1",
        "batch_size": 4,
        "max_parallel_streams": 64,
        "duration_s": 1,
        "sample_interval_s": 1,
        "drain_s": 1,
        "guard_wait_s": 1,
        "keep_evidence": 1,
        "evidence_group_size": 10,
        "evidence_policy_groups": ((2, 2), (3, 3), (5, 5), (8, 8), (10, 10), (15, 15)),
        "artifact_dir": Path("/tmp/pressure60-test"),
        "db_url": "postgresql://video:video@127.0.0.1:5432/video_analytics",
        "redis_url": "redis://127.0.0.1:6396/0",
        "api_base": "http://127.0.0.1:8090/api/v1",
        "compose_file": "infra/docker-compose.midterm.yml",
        "env_file": "infra/env/midterm.env",
        "evidence_root": Path("/data/video-analytics/media/evidence"),
        "replay_epoch_root": Path("/data/video-analytics/media/replay-sink-output/midterm/epochs"),
        "force_runtime_restart": False,
        "no_quiesce_before_guard": False,
        "rtsp_republish_output_base": "",
        "rtsp_republish_input_uri": "rtsp://camera/live",
        "rtsp_republish_mode": "copy",
        "rtsp_republish_warmup_s": 0,
        "max_send_failures": 0,
        "max_exited_sources": 0,
        "max_validate_seq_iq": 0,
        "cleanup": True,
    }
    values.update(overrides)
    return module.PressureConfig(**values)


def test_pressure_rtsp_uri_uses_source_id_when_republish_base_has_no_placeholder() -> None:
    module = _load_module()
    cfg = _config(module, rtsp_republish_output_base="rtsp://127.0.0.1:8554/pressure")

    assert module.pressure_rtsp_uri(cfg, index=3, source_id="pressure60_test_03") == (
        "rtsp://127.0.0.1:8554/pressure/pressure60_test_03"
    )


def test_pressure_rtsp_uri_supports_republish_placeholders() -> None:
    module = _load_module()
    cfg = _config(
        module,
        rtsp_republish_output_base=(
            "rtsp://127.0.0.1:8554/pressure/{run_id}/{index}/{source_id}"
        ),
    )

    assert module.pressure_rtsp_uri(cfg, index=7, source_id="pressure60_test_07") == (
        "rtsp://127.0.0.1:8554/pressure/pressure60_test/07/pressure60_test_07"
    )


def test_rtsp_republish_command_normalizes_timestamps_without_rebuild() -> None:
    module = _load_module()

    command = module.rtsp_republish_command(
        ffmpeg="/usr/bin/ffmpeg",
        input_uri="rtsp://camera/live",
        output_uri="rtsp://127.0.0.1:8554/pressure/out",
        mode="copy",
    )

    assert command[0] == "/usr/bin/ffmpeg"
    assert "-fflags" in command
    assert "+genpts" in command
    assert "-use_wallclock_as_timestamps" in command
    assert "-avoid_negative_ts" in command
    assert "make_zero" in command
    assert "-c:v" in command
    assert "copy" in command
    assert "rtsp://127.0.0.1:8554/pressure/out" == command[-1]


def test_validate_seq_iq_from_sampling_is_warning_without_ingress_failure() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=0)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 0,
            "max_forwarder_queue_depth": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 25}},
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "validate_seq_iq_exceeded" not in reasons
    assert module.pressure_warnings(cfg, diagnostics, reasons) == [
        "validate_seq_iq_expected_sampling_gap"
    ]


def test_validate_seq_iq_still_fails_when_ingress_is_unhealthy() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=0)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 1,
            "max_forwarder_queue_depth": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 25}},
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "savant_send_failures" in reasons
    assert "validate_seq_iq_exceeded" in reasons
    assert module.pressure_warnings(cfg, diagnostics, reasons) == []


def test_frame_annotation_redis_errors_are_explicit_failure() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=0)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 0,
            "max_queue_depth": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {
            "savant": {
                "validate_seq_iq": 0,
                "frame_annotation_redis_write_error": 12,
            }
        },
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "frame_annotation_redis_write_errors" in reasons


def test_evidence_policy_groups_parse_pre_post_pairs() -> None:
    module = _load_module()

    assert module.parse_evidence_policy_groups("2:3,5/6,8") == (
        (2, 3),
        (5, 6),
        (8, 8),
    )


def test_evidence_policy_groups_assign_ten_cameras_per_group() -> None:
    module = _load_module()
    cfg = _config(
        module,
        evidence_group_size=10,
        evidence_policy_groups=((2, 2), (3, 3), (5, 5), (8, 8), (10, 10), (15, 15)),
    )

    assert module.evidence_policy_for_index(cfg, 0)["pressure_group_index"] == 0
    assert module.evidence_policy_for_index(cfg, 9)["pre_seconds"] == 2
    assert module.evidence_policy_for_index(cfg, 10)["pressure_group_index"] == 1
    assert module.evidence_policy_for_index(cfg, 10)["post_seconds"] == 3
    assert module.evidence_policy_for_index(cfg, 59)["pressure_group_index"] == 5
    assert module.evidence_policy_for_index(cfg, 59)["pressure_total_seconds"] == 30


def test_wait_for_drain_waits_for_playable_evidence(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, keep_evidence=50, drain_s=60)
    summaries = [
        {
            "cameras": 60,
            "events": 100,
            "tasks": 100,
            "bundles": 59,
            "playable_bundles": 42,
            "task_statuses": [],
            "event_types": [],
        },
        {
            "cameras": 60,
            "events": 100,
            "tasks": 100,
            "bundles": 65,
            "playable_bundles": 50,
            "task_statuses": [],
            "event_types": [],
        },
    ]
    observed: list[dict] = []

    def fake_db_summary_connect(_cfg):
        summary = summaries.pop(0)
        observed.append(summary)
        return summary

    now = [1_000.0]

    monkeypatch.setattr(module, "db_summary_connect", fake_db_summary_connect)
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    module.wait_for_drain(cfg)

    assert [item["playable_bundles"] for item in observed] == [42, 50]
    snapshots = json.loads((tmp_path / "drain_snapshots.json").read_text())
    assert len(snapshots) == 2
