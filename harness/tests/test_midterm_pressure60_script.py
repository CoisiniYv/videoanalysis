"""Static contracts for the midterm 60-stream pressure runner."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT.parent / "scripts" / "runtime" / "run_midterm_pressure60.py"
COMPOSE = ROOT.parent / "infra" / "docker-compose.midterm.yml"


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
        "pose_batch_size": 4,
        "face_detector_batch_size": 4,
        "face_embedding_batch_size": 16,
        "face_infer_interval": 1,
        "face_embedding_infer_interval": 1,
        "max_parallel_streams": 64,
        "batched_push_timeout": 40000,
        "duration_s": 1,
        "sample_interval_s": 1,
        "drain_s": 1,
        "guard_wait_s": 1,
        "keep_evidence": 1,
        "evidence_group_size": 10,
        "evidence_policy_groups": ((2, 2), (3, 3), (5, 5), (8, 8), (10, 10), (15, 15)),
        "pressure_materialization_event_type_quotas": "",
        "pressure_disable_evidence_admission": False,
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
        "rtsp_republish_input_offset_s": 0.0,
        "rtsp_republish_input_loop": False,
        "max_send_failures": 0,
        "max_exited_sources": 0,
        "max_validate_seq_iq": 0,
        "forwarder_null_sink": False,
        "dual_shard_same_gpu": False,
        "dual_shard_api": False,
        "dual_shard_gpu": "0",
        "dual_shard_source_mode": "balanced",
        "evidence_shard_count": 4,
        "rolling_cache_evidence": False,
        "rolling_cache_enable_coverage_merge": False,
        "cleanup": True,
        "cuda_mps": False,
        "adaface_classifier_async": False,
        "face_secondary_track_id": False,
        "adaface_input_queue": False,
        "adaface_crop_resize": False,
        "adaface_pre_gate": False,
        "adaface_decoupled": False,
        "rolling_cache_postfill_s": 0,
        "pressure_algorithm_cooldown_s": 30,
        "pressure_source_visibility_timeout_s": 180,
        "pressure_source_visibility_poll_s": 5,
        "pressure_source_visibility_stable_samples": 2,
        "pressure_source_visibility_restart_attempts": 1,
        "pressure_source_ffmpeg_timeout_ms": 60000,
        "pressure_source_start_stagger_s": 0.5,
    }
    values.update(overrides)
    return module.PressureConfig(**values)


def test_pressure_rtsp_uri_uses_source_id_when_republish_base_has_no_placeholder() -> None:
    module = _load_module()
    cfg = _config(module, rtsp_republish_output_base="rtsp://127.0.0.1:8554/pressure")

    assert module.pressure_rtsp_uri(cfg, index=3, source_id="pressure60_test_03") == (
        "rtsp://127.0.0.1:8554/pressure/pressure60_test_03"
    )


def test_restore_cameras_does_not_reenable_stale_pressure_sources() -> None:
    module = _load_module()

    class FakeTx:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def __init__(self):
            self.calls = []

        def transaction(self):
            return FakeTx()

        def execute(self, query, params):
            self.calls.append((query, params))

    conn = FakeConn()
    module.restore_cameras(
        conn,
        [
            {"id": "lab-camera", "source_id": "lab", "enabled": True},
            {
                "id": "old-pressure",
                "source_id": "rc400_rawtap_fix_20260706T173642Z_00",
                "enabled": True,
            },
        ],
    )

    assert conn.calls == [
        (
            "UPDATE cameras SET enabled=%s, updated_at=now() WHERE id=%s",
            (True, "lab-camera"),
        )
    ]


def test_pressure_cleanup_syncs_runtime_sources_after_camera_deletes() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "runtime_sources_apply_after_pressure_cleanup.json" in source
    assert "runtime_sources_apply_after_pressure_runtime_cleanup.json" in source


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


def test_rtsp_republish_command_supports_deterministic_file_offset_and_loop() -> None:
    module = _load_module()

    command = module.rtsp_republish_command(
        ffmpeg="/usr/bin/ffmpeg",
        input_uri="/data/video-analytics/pressure/input.mp4",
        output_uri="rtsp://127.0.0.1:8554/pressure/out",
        mode="copy",
        input_offset_s=12.5,
        input_loop=True,
    )

    assert command[0] == "/usr/bin/ffmpeg"
    assert command[command.index("-stream_loop") + 1] == "-1"
    assert command[command.index("-ss") + 1] == "12.5"
    assert "-stream_loop" in command[: command.index("-i")]
    assert "-ss" in command[: command.index("-i")]
    assert "-use_wallclock_as_timestamps" not in command
    assert "/data/video-analytics/pressure/input.mp4" in command
    assert "copy" in command


def test_rtsp_republish_transcode_uses_short_gop_for_rolling_segments() -> None:
    module = _load_module()

    command = module.rtsp_republish_command(
        ffmpeg="/usr/bin/ffmpeg",
        input_uri="/data/video-analytics/pressure/input.mp4",
        output_uri="rtsp://127.0.0.1:8554/pressure/out",
        mode="transcode",
    )

    assert "libx264" in command
    assert command[command.index("-g") + 1] == "8"
    assert command[command.index("-keyint_min") + 1] == "8"
    assert command[command.index("-sc_threshold") + 1] == "0"
    assert command[command.index("-bf") + 1] == "0"


def test_pressure_runner_exposes_rolling_cache_canary_flags() -> None:
    module = _load_module()

    parsed = module.parse_args(
        [
            "--rolling-cache-evidence",
            "--rolling-cache-enable-coverage-merge",
        ]
    )

    assert parsed.rolling_cache_evidence is True
    assert parsed.rolling_cache_enable_coverage_merge is True


def test_pressure_runner_defaults_to_high_density_acceptance_window() -> None:
    module = _load_module()

    parsed = module.parse_args([])

    assert parsed.fps == "8/1"
    assert parsed.duration_s == 600
    assert parsed.drain_s == 120
    assert parsed.keep_evidence == -1


def test_pressure_source_ids_match_inserted_camera_ids() -> None:
    module = _load_module()
    cfg = _config(module, run_id="rolling_canary", stream_count=3)

    assert module.pressure_source_ids(cfg) == [
        "rolling_canary_00",
        "rolling_canary_01",
        "rolling_canary_02",
    ]


def test_pressure_camera_insert_uses_per_algorithm_alert_cooldown() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"global_alert_cooldown_s": cfg.pressure_algorithm_cooldown_s' in source
    assert '"cooldown_scope": "algorithm"' in source
    assert '"store_suppressed_events": True' in source
    assert "--pressure-algorithm-cooldown-s" in source


def test_rolling_cache_pressure_clamps_coverage_parent_duration() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "max_policy_window_s" in source
    assert '"EVIDENCE_COVERAGE_PARENT_MAX_DURATION_SECONDS": str(max_policy_window_s)' in source


def test_rolling_cache_pressure_indexes_db_timeline_and_overlays() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"EVIDENCE_DB_INDEX_EXPANDED_ROWS_ENABLED": "true"' in source
    assert "timeline rows, overlay rows" in source


def test_rolling_cache_pressure_sets_fast_ready_poll_interval() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S": "1"' in source
    assert '"ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S"' in source


def test_rolling_cache_pressure_uses_calibrated_ready_grace() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS": "9"' in source
    assert '"ROLLING_CACHE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS": "9"' in source


def test_rolling_cache_pressure_disables_legacy_replay_fallback() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"ROLLING_CACHE_FALLBACK_TO_REPLAY": "false"' in source
    assert "must prove the Replay raw tap -> rolling-cache" in source


def test_rolling_cache_pressure_sets_named_high_density_profile() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"EVIDENCE_DENSITY_PROFILE": "high_density"' in source
    assert 'density_profile="high_density" if cfg.rolling_cache_evidence else None' in source
    assert 'materialization_pressure_level=(' in source
    assert '"high_density" if cfg.rolling_cache_evidence else None' in source


def test_rolling_cache_pressure_attempts_sidecar_for_all_retained_evidence() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"FRAME_CACHE_SIDECAR_MAX_EVENTS_PER_RUN": str(' in source
    assert "max(1000, cfg.stream_count * 40)" in source
    assert '"FRAME_CACHE_SIDECAR_MAX_EVENTS_PER_RUN",' in source


def test_pressure_drain_does_not_treat_reasoned_deferred_as_active() -> None:
    module = _load_module()

    assert "materialization_deferred" not in module.ACTIVE_MATERIALIZATION_STATES
    source = SCRIPT.read_text(encoding="utf-8")
    assert "COALESCE(rt.materialization_defer_reason, '') = ''" in source
    assert "COALESCE(rt.materialization_defer_reason, '') IN (\n" not in source


def test_pressure_source_visibility_barrier_waits_for_all_sources(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        stream_count=2,
        pressure_source_visibility_timeout_s=20,
        pressure_source_visibility_poll_s=1,
        pressure_source_visibility_stable_samples=2,
        pressure_source_visibility_restart_attempts=0,
    )
    overviews = [
        {
            "forwarder": {
                "global": {"queue_depth": 0},
                "sources": [
                    {"source_id": "pressure60_test_00", "frames_seen_total": 8},
                ],
            },
            "metrics": {
                "sources": [
                    {"source_id": "pressure60_test_00", "frames_seen_total": 8},
                ],
            },
        },
        {
            "forwarder": {
                "global": {"queue_depth": 0},
                "sources": [
                    {"source_id": "pressure60_test_00", "frames_seen_total": 16},
                    {"source_id": "pressure60_test_01", "frames_seen_total": 16},
                ],
            },
            "metrics": {
                "sources": [
                    {"source_id": "pressure60_test_00", "frames_seen_total": 16},
                    {"source_id": "pressure60_test_01", "frames_seen_total": 16},
                ],
            },
        },
        {
            "forwarder": {
                "global": {"queue_depth": 0},
                "sources": [
                    {"source_id": "pressure60_test_00", "frames_seen_total": 24},
                    {"source_id": "pressure60_test_01", "frames_seen_total": 24},
                ],
            },
            "metrics": {
                "sources": [
                    {"source_id": "pressure60_test_00", "frames_seen_total": 24},
                    {"source_id": "pressure60_test_01", "frames_seen_total": 24},
                ],
            },
        },
    ]
    now = [1_000.0]

    monkeypatch.setattr(module, "pressure_runtime_overview", lambda _cfg: overviews.pop(0))
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    summary = module.wait_for_pressure_source_visibility(cfg, sources_path=None)

    assert summary["status"] == "ready"
    assert summary["stable_samples"] == 2
    assert summary["last_snapshot"]["forwarder_visible_count"] == 2
    snapshots = json.loads(
        (tmp_path / "pressure_source_visibility_snapshots.json").read_text()
    )
    assert snapshots[0]["missing_forwarder_sources"] == ["pressure60_test_01"]


def test_pressure_visibility_waits_for_decoupled_eligible_sources(
    monkeypatch,
) -> None:
    module = _load_module()
    cfg = _config(
        module,
        stream_count=2,
        adaface_decoupled=True,
    )
    overview = {
        "forwarder": {
            "global": {"queue_depth": 0},
            "sources": [
                {"source_id": "pressure60_test_00", "frames_seen_total": 8},
                {"source_id": "pressure60_test_01", "frames_seen_total": 8},
            ],
        },
        "metrics": {
            "sources": [
                {"source_id": "pressure60_test_00", "frames_seen_total": 8},
                {"source_id": "pressure60_test_01", "frames_seen_total": 8},
            ],
        },
        "adaface_forwarder": {
            "sources": [
                {
                    "source_id": "pressure60_test_00",
                    "frames_seen_total": 4,
                    "frames_forwarded_total": 2,
                },
                {
                    "source_id": "pressure60_test_01",
                    "frames_seen_total": 4,
                    "frames_forwarded_total": 0,
                },
            ],
        },
        "adaface_central": {"sources": []},
    }
    monkeypatch.setattr(module, "pressure_runtime_overview", lambda _cfg: overview)

    waiting = module.pressure_source_visibility_snapshot(cfg)

    assert waiting["status"] == "waiting"
    assert waiting["missing_adaface_central_eligible_sources"] == [
        "pressure60_test_00"
    ]

    overview["adaface_central"]["sources"] = [
        {"source_id": "pressure60_test_00", "frames_seen_total": 1}
    ]
    ready = module.pressure_source_visibility_snapshot(cfg)

    assert ready["status"] == "all_visible"
    assert ready["adaface_eligible_count"] == 1
    assert ready["adaface_central_visible_count"] == 1


def test_prepare_pressure_sampling_window_clears_visibility_warmup_rows(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        rolling_cache_evidence=False,
        rolling_cache_prefill_s=0,
    )
    calls = []

    monkeypatch.setattr(
        module,
        "clear_pressure_warmup_rows",
        lambda _conn, _cfg: calls.append("cleared") or {"event_rows_deleted": 3},
    )
    monkeypatch.setattr(module.time, "time", lambda: 1234.0)

    started = module.prepare_pressure_sampling_window(object(), cfg, pressure_started_monotonic=99.0)

    assert started == 1234.0
    assert calls == ["cleared"]
    summary = json.loads((tmp_path / "rolling_cache_prefill_summary.json").read_text())
    assert summary["cleanup"]["event_rows_deleted"] == 3


def test_decoupled_adaface_postfill_keeps_sources_alive_for_visibility(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        adaface_decoupled=True,
        rolling_cache_evidence=False,
        rolling_cache_postfill_s=0,
    )
    sleeps: list[float] = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        module,
        "pressure_source_visibility_snapshot",
        lambda _cfg: {
            "status": "all_visible",
            "missing_adaface_central_eligible_sources": [],
        },
    )

    summary = module.rolling_cache_postfill_after_sampling(cfg)

    assert sleeps == [5]
    assert summary["postfill_s"] == 5
    assert summary["adaface_visibility"]["status"] == "all_visible"


def test_rolling_cache_segment_visibility_summary_uses_metadata_mtime(
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(module, run_id="rolling_canary")
    metadata_path = (
        tmp_path
        / "midterm"
        / "epochs"
        / "epoch-a"
        / "rolling_canary_00"
        / "segments"
        / "0001"
        / "metadata.json"
    )
    metadata_path.parent.mkdir(parents=True)
    last_pts = 1_783_329_600_000_000_000
    metadata_path.write_text(
        json.dumps(
            {
                "frames": [
                    {"type": "VideoFrame", "pts": last_pts - 4_000_000_000},
                    {"type": "VideoFrame", "pts": last_pts},
                ]
            }
        ),
        encoding="utf-8",
    )
    visible_at_s = last_pts / 1_000_000_000 + 1.25
    os.utime(metadata_path, (visible_at_s, visible_at_s))

    summary = module.collect_rolling_cache_segment_visibility(cfg, root=tmp_path)

    assert summary["segments_measured"] == 1
    assert summary["source_count"] == 1
    assert summary["metadata_visible_lag_s"]["p50"] == 1.25
    assert summary["segment_duration_s"]["p50"] == 4.0
    assert summary["segment_frame_rate_fps"]["p50"] == 0.5


def test_rolling_cache_segment_visibility_accepts_jsonl_metadata(
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(module, run_id="rolling_canary")
    metadata_path = (
        tmp_path
        / "midterm"
        / "epochs"
        / "epoch-a"
        / "rolling_canary_00"
        / "segments"
        / "0001"
        / "metadata.json"
    )
    metadata_path.parent.mkdir(parents=True)
    first_pts = 1_783_329_600_000_000_000
    last_pts = first_pts + 2_000_000_000
    metadata_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "VideoFrame", "pts": first_pts}),
                json.dumps({"type": "VideoFrame", "pts": last_pts}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    visible_at_s = last_pts / 1_000_000_000 + 0.5
    os.utime(metadata_path, (visible_at_s, visible_at_s))

    summary = module.collect_rolling_cache_segment_visibility(cfg, root=tmp_path)

    assert summary["segments_measured"] == 1
    assert summary["parse_errors"] == 0
    assert summary["metadata_visible_lag_s"]["p50"] == 0.5


def test_rolling_cache_full_rate_gate_requires_all_sources_and_fps() -> None:
    module = _load_module()
    cfg = _config(module, stream_count=2, fps="8/1")

    gate = module.rolling_cache_full_rate_gate(
        cfg,
        {
            "segments_measured": 2,
            "source_count": 2,
            "segment_frame_rate_fps": {"p50": 8.1},
        },
    )

    assert gate["full_rate_ok"] is True
    assert module.rolling_cache_full_rate_failure_reasons(gate) == []

    failed = module.rolling_cache_full_rate_gate(
        cfg,
        {
            "segments_measured": 2,
            "source_count": 1,
            "segment_frame_rate_fps": {"p50": 4.0},
        },
    )

    assert "rolling_cache_missing_source_segments" in module.rolling_cache_full_rate_failure_reasons(failed)
    assert "rolling_cache_segment_fps_below_full_rate" in module.rolling_cache_full_rate_failure_reasons(failed)


def test_rolling_cache_full_rate_gate_uses_raw_input_fps_when_available() -> None:
    module = _load_module()
    cfg = _config(module, stream_count=2, fps="8/1")

    gate = module.rolling_cache_full_rate_gate(
        cfg,
        {
            "segments_measured": 2,
            "source_count": 2,
            "segment_frame_rate_fps": {"p50": 23.95},
        },
        {"rolling_cache_expected_raw_fps": 23.976},
    )

    assert gate["expected_fps"] == 23.976
    assert gate["expected_fps_source"] == "rolling_cache_raw_input"
    assert gate["full_rate_ok"] is True


def test_rolling_cache_pressure_requires_all_evidence_retained() -> None:
    module = _load_module()
    cfg = _config(module, rolling_cache_evidence=True, keep_evidence=50)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 0,
            "max_queue_depth": 0,
            "queue_full_samples": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "rolling_cache_acceptance_must_keep_all_evidence" in reasons


def test_rolling_cache_8090_annotation_gate_rejects_unchecked_annotations() -> None:
    module = _load_module()

    reasons = module.evidence_annotation_failure_reasons(
        {
            "retained_count": 3,
            "annotation_checked_count": 1,
            "annotation_ok_count": 1,
            "annotation_skipped_zero_expected_count": 1,
        }
    )

    assert "evidence_8090_annotations_unchecked_or_zero" in reasons


def test_rolling_cache_8090_annotation_gate_allows_expected_image_skips() -> None:
    module = _load_module()

    reasons = module.evidence_annotation_failure_reasons(
        {
            "retained_count": 3,
            "annotation_checked_count": 2,
            "annotation_ok_count": 2,
            "annotation_skipped_zero_expected_count": 1,
        }
    )

    assert "evidence_8090_annotations_unchecked_or_zero" not in reasons


def test_keep_all_drain_exits_when_run_produces_zero_events(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        keep_evidence=-1,
        drain_s=120,
    )
    calls = {"count": 0}

    def fake_summary(_cfg):
        calls["count"] += 1
        return {
            "events": 0,
            "playable_bundles": 0,
            "distinct_events_with_playable_evidence": 0,
            "blocking_materialization_tasks": 0,
            "distinct_events_with_terminal_nonplayable_outcome": 0,
        }

    monkeypatch.setattr(module, "db_summary_connect", fake_summary)
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(
            AssertionError("zero-event drain should not sleep")
        ),
    )

    module.wait_for_drain(cfg)

    assert calls["count"] == 1
    assert (tmp_path / "drain_snapshots.json").exists()


def test_stop_pressure_sources_captures_source_logs_before_removal() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "capture_pressure_source_logs" in source
    assert '"source_logs": source_logs' in source
    assert "removed_source_containers" in source


def test_stop_pressure_sources_tolerates_runtime_apply_error_when_direct_removal_stable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path)
    calls: dict[str, object] = {}

    class _Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Conn:
        def transaction(self):
            return _Transaction()

        def execute(self, *_args, **_kwargs):
            calls["db_updated"] = True

    def fake_apply(_cfg, artifact_name, *, raise_on_error=True):
        calls["artifact_name"] = artifact_name
        calls["raise_on_error"] = raise_on_error
        return {
            "data": None,
            "error": {
                "message": "runtime source apply filesystem error: timed out",
                "code": 503,
            },
        }

    monkeypatch.setattr(module, "apply_sources_only", fake_apply)
    monkeypatch.setattr(
        module,
        "capture_pressure_source_logs",
        lambda *_args, **_kwargs: {"captured_count": 1},
    )
    monkeypatch.setattr(
        module,
        "remove_pressure_source_containers",
        lambda _run_id: {"removed": ["video-analytics-source-test_00"]},
    )
    monkeypatch.setattr(
        module,
        "remove_pressure_source_containers_until_stable",
        lambda _run_id: {"stable": True, "running": 0, "total": 0},
    )

    module.stop_pressure_sources(_Conn(), cfg)

    assert calls["db_updated"] is True
    assert calls["artifact_name"] == "runtime_sources_apply_stop_pressure_sources.json"
    assert calls["raise_on_error"] is False
    summary = json.loads(
        (tmp_path / "runtime_sources_apply_stop_pressure.json").read_text()
    )
    assert summary["sources_apply_error_ignored"] is True
    assert summary["stable_source_container_removal"]["stable"] is True


def test_rtsp_republishers_stopped_by_runner_are_not_counted_exited(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path)
    log_path = tmp_path / "republish.log"
    log_path.write_text("", encoding="utf-8")
    (tmp_path / "rtsp_republish_manifest.json").write_text(
        json.dumps(
            [
                {
                    "source_id": "pressure60_test_00",
                    "pid": 123456,
                    "log_path": str(log_path),
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "rtsp_republish_stop.json").write_text(
        json.dumps([{"pid": 123456, "returncode": 255}]),
        encoding="utf-8",
    )

    class _Completed:
        stdout = ""

    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: _Completed())

    summary = module.inspect_rtsp_republishers(cfg)

    assert summary["exited"] == 0
    assert summary["stopped_intentionally"] == 1


def test_cleanup_keeps_covered_event_alias_rows() -> None:
    module = _load_module()
    cfg = _config(module, run_id="rolling_canary")

    class _Conn:
        def execute(self, sql, params):
            self.sql = sql
            self.params = params

            class _Rows:
                def fetchall(self):
                    return [{"event_id": "00000000-0000-4000-8000-000000000002"}]

            return _Rows()

    conn = _Conn()
    covered = module.select_covered_event_ids_for_bundles(
        conn,
        cfg,
        {"00000000-0000-4000-8000-000000000001"},
    )

    assert covered == {"00000000-0000-4000-8000-000000000002"}
    assert "evidence_event_links" in conn.sql
    assert conn.params[1] == "rolling_canary_%"


def test_rolling_cache_cleanup_removes_orphan_materialized_dirs_by_metadata(
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(module, run_id="rolling_canary")
    materialized_root = tmp_path / "rolling-cache-materialized"
    event_dir = (
        materialized_root
        / "midterm"
        / "epochs"
        / "epoch-a"
        / "materialized"
        / "11111111-1111-4111-8111-111111111111"
    )
    event_dir.mkdir(parents=True)
    (event_dir / "metadata.json").write_text(
        json.dumps(
            {
                "event_id": "11111111-1111-4111-8111-111111111111",
                "source_id": "rolling_canary_00",
                "labels": {"source_id": "rolling_canary_00"},
            }
        ),
        encoding="utf-8",
    )
    (event_dir / "video.mov").write_bytes(b"video")

    class _Rows:
        def fetchall(self):
            return []

    class _Conn:
        def execute(self, *_args, **_kwargs):
            return _Rows()

    cleanup = module.remove_pressure_rolling_cache_artifacts(
        _Conn(),
        cfg,
        event_ids=[],
        materialized_root=materialized_root,
    )

    assert cleanup["materialized_dirs"] == 1
    assert not event_dir.exists()


def test_8090_observability_checks_annotation_endpoint(monkeypatch) -> None:
    module = _load_module()
    cfg = _config(module)
    requests: list[str] = []

    def fake_api_json(_base: str, _method: str, path: str, **_kwargs: object):
        requests.append(path)
        if path == "/evidence/health":
            return {"data": {"status": "ok"}}
        if path == "/evidence/bundles/event-1":
            return {
                "data": {
                    "event_id": "event-1",
                    "annotation_lines": 3,
                }
            }
        if path == "/evidence/bundles/event-1/sink-metadata":
            return {"data": {"count": 3, "records": [{"pts": 1}]}}
        if path == "/evidence/bundles/event-1/annotations?include_records=true":
            return {
                "data": {
                    "count": 3,
                    "records": [
                        {
                            "objects": [
                                {
                                    "object_type": "person",
                                    "annotation_role": "person_context",
                                    "bbox": {"format": "xyxy", "xyxy": [1, 2, 3, 4]},
                                }
                            ]
                        }
                    ],
                }
            }
        raise AssertionError(path)

    monkeypatch.setattr(module, "api_json", fake_api_json)

    summary = module.evidence_8090_observability_summary(
        cfg,
        [{"event_id": "event-1"}],
    )

    assert summary["ok_count"] == 1
    assert summary["timeline_ok_count"] == 1
    assert summary["annotation_checked_count"] == 1
    assert summary["annotation_ok_count"] == 1
    assert summary["annotation_failed"] == []
    assert "/evidence/bundles/event-1/annotations?include_records=true" in requests
    assert "/evidence/bundles/event-1/sink-metadata" in requests


def test_8090_observability_flags_missing_annotation_records(monkeypatch) -> None:
    module = _load_module()
    cfg = _config(module)

    def fake_api_json(_base: str, _method: str, path: str, **_kwargs: object):
        if path == "/evidence/health":
            return {"data": {"status": "ok"}}
        if path == "/evidence/bundles/event-1":
            return {
                "data": {
                    "event_id": "event-1",
                    "annotation_lines": 3,
                }
            }
        if path == "/evidence/bundles/event-1/sink-metadata":
            return {"data": {"count": 3, "records": [{"pts": 1}]}}
        if path == "/evidence/bundles/event-1/annotations?include_records=true":
            return {"data": {"count": 0, "records": []}}
        raise AssertionError(path)

    monkeypatch.setattr(module, "api_json", fake_api_json)

    summary = module.evidence_8090_observability_summary(
        cfg,
        [{"event_id": "event-1"}],
    )

    assert summary["annotation_checked_count"] == 1
    assert summary["annotation_ok_count"] == 0
    assert any(
        item["error"] == "annotations_missing_from_8090_endpoint"
        for item in summary["annotation_failed"]
    )


def test_8090_observability_allows_merged_db_overlay_rows(monkeypatch) -> None:
    module = _load_module()
    cfg = _config(module)

    def fake_api_json(_base: str, _method: str, path: str, **_kwargs: object):
        if path == "/evidence/health":
            return {"data": {"status": "ok"}}
        if path == "/evidence/bundles/event-1":
            return {
                "data": {
                    "event_id": "event-1",
                    "event_type": "intrusion",
                    "annotation_lines": 19,
                }
            }
        if path == "/evidence/bundles/event-1/sink-metadata":
            return {"data": {"count": 20, "records": [{"pts": 1}]}}
        if path == "/evidence/bundles/event-1/annotations?include_records=true":
            return {
                "data": {
                    "count": 18,
                    "source": "database",
                    "fallback_used": False,
                    "records": [
                        {
                            "objects": [
                                {
                                    "object_type": "person",
                                    "annotation_role": "person_context",
                                    "bbox": {"format": "xyxy", "xyxy": [1, 2, 3, 4]},
                                },
                                {
                                    "object_type": "face",
                                    "bbox": {"format": "xyxy", "xyxy": [5, 6, 7, 8]},
                                },
                            ]
                        }
                    ],
                }
            }
        raise AssertionError(path)

    monkeypatch.setattr(module, "api_json", fake_api_json)

    summary = module.evidence_8090_observability_summary(
        cfg,
        [{"event_id": "event-1", "event_type": "intrusion"}],
    )

    assert summary["annotation_ok_count"] == 1
    assert summary["annotation_count_mismatch_count"] == 1
    assert summary["annotation_failed"] == []


def test_8090_observability_skips_zero_annotation_bundles(monkeypatch) -> None:
    module = _load_module()
    cfg = _config(module)
    requests: list[str] = []

    def fake_api_json(_base: str, _method: str, path: str, **_kwargs: object):
        requests.append(path)
        if path == "/evidence/health":
            return {"data": {"status": "ok"}}
        if path == "/evidence/bundles/event-1":
            return {"data": {"event_id": "event-1"}}
        raise AssertionError(path)

    monkeypatch.setattr(module, "api_json", fake_api_json)

    summary = module.evidence_8090_observability_summary(
        cfg,
        [{"event_id": "event-1", "annotation_count": 0, "image_evidence": True}],
    )

    assert summary["ok_count"] == 1
    assert summary["annotation_checked_count"] == 0
    assert summary["annotation_skipped_zero_expected_count"] == 1
    assert not any("/annotations" in path for path in requests)
    assert not any(path.endswith("/sink-metadata") for path in requests)


def test_default_run_id_marks_8090_topology_dual_shard() -> None:
    module = _load_module()

    run_id = module._default_run_id(
        "8/1",
        dual_shard_same_gpu=True,
        dual_shard_api=True,
    )

    assert run_id.startswith("pressure60_8090topology_dual1gpu_8p1_")


def test_topology_pressure_payload_uses_8090_dual_same_gpu_shape() -> None:
    module = _load_module()
    cfg = _config(
        module,
        stream_count=60,
        fps="8/1",
        min_fps="2/1",
        batch_size=4,
        pose_batch_size=4,
        face_detector_batch_size=4,
        face_embedding_batch_size=16,
        max_parallel_streams=32,
        dual_shard_same_gpu=True,
        dual_shard_api=True,
        dual_shard_gpu="0",
        keep_evidence=0,
    )

    payload = module.topology_pressure_payload(cfg)

    assert payload["topology_mode"] == "dual_same_gpu"
    assert payload["shard_strategy"] == "balanced"
    assert payload["streams_per_branch"] == 30
    assert payload["branches"]["a"]["gpu_id"] == 0
    assert payload["branches"]["b"]["gpu_id"] == 0
    assert payload["branches"]["a"]["savant_batch_size"] == 4
    assert payload["branches"]["a"]["analysis_fps"] == "8/1"
    assert payload["branches"]["a"]["face_infer_interval"] == 1
    assert payload["branches"]["a"]["face_embedding_infer_interval"] == 1


def test_topology_pressure_payload_supports_single_shard_manual_assignment() -> None:
    module = _load_module()
    cfg = _config(
        module,
        run_id="pressure60_single_shard",
        stream_count=3,
        dual_shard_same_gpu=True,
        dual_shard_api=True,
        dual_shard_source_mode="all-a",
    )

    payload = module.topology_pressure_payload(cfg)

    assert payload["shard_strategy"] == "manual"
    assert payload["manual_assignments"] == {
        "pressure60_single_shard_00": "a",
        "pressure60_single_shard_01": "a",
        "pressure60_single_shard_02": "a",
    }


def test_pressure_face_interval_defaults_to_one_fps_from_input_fps() -> None:
    module = _load_module()

    assert module.infer_interval_for_target_fps("4/1", target_fps=module.Fraction(1, 1)) == 3
    assert module.infer_interval_for_target_fps("8/1", target_fps=module.Fraction(1, 1)) == 7
    assert module.infer_interval_for_target_fps("1/1", target_fps=module.Fraction(1, 1)) == 0


def test_dual_shard_pressure_enables_stage_metrics(tmp_path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        dual_shard_same_gpu=True,
        dual_shard_api=True,
    )

    override_path = module.write_dual_shard_same_gpu_compose_override(cfg)
    override = yaml.safe_load(override_path.read_text(encoding="utf-8"))

    for service in ("savant-a", "savant-b"):
        assert override["services"][service]["environment"][
            "SAVANT_STAGE_METRICS_ENABLED"
        ] == "true"


def test_dual_shard_pressure_can_propagate_face_secondary_track_ids(tmp_path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        dual_shard_same_gpu=True,
        face_secondary_track_id=True,
    )

    override_path = module.write_dual_shard_same_gpu_compose_override(cfg)
    override = yaml.safe_load(override_path.read_text(encoding="utf-8"))

    for service in ("savant-a", "savant-b"):
        assert override["services"][service]["environment"][
            "FACE_SECONDARY_TRACK_ID_ENABLED"
        ] == "true"


def test_dual_shard_mps_override_shares_pipe_and_host_ipc(tmp_path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        dual_shard_same_gpu=True,
        dual_shard_api=False,
        cuda_mps=True,
    )

    override_path = module.write_dual_shard_same_gpu_compose_override(cfg)
    override = yaml.safe_load(override_path.read_text(encoding="utf-8"))
    mps_root, pipe_dir, log_dir = module.cuda_mps_paths(cfg)

    assert str(mps_root) == "/tmp/video-analytics-mps-pressure"
    assert len(str(pipe_dir / "control").encode("utf-8")) < 100

    for service in ("savant-a", "savant-b"):
        service_doc = override["services"][service]
        assert service_doc["ipc"] == "host"
        assert service_doc["environment"]["CUDA_MPS_PIPE_DIRECTORY"] == str(pipe_dir)
        assert service_doc["environment"]["CUDA_MPS_LOG_DIRECTORY"] == str(log_dir)
        assert f"{mps_root}:{mps_root}:rw" in service_doc["volumes"]


def test_parse_savant_stage_latency_and_batch_occupancy() -> None:
    module = _load_module()
    parsed = module.parse_savant_metrics_text(
        """
va_savant_stage_duration_seconds_count{stage="yolo26_pose"} 10
va_savant_stage_duration_seconds_sum{stage="yolo26_pose"} 0.08
va_savant_stage_duration_seconds_last{stage="yolo26_pose"} 0.009
va_savant_stage_duration_seconds_bucket{stage="yolo26_pose",le="0.005"} 4
va_savant_stage_duration_seconds_bucket{stage="yolo26_pose",le="0.01"} 8
va_savant_stage_duration_seconds_bucket{stage="yolo26_pose",le="0.025"} 10
va_savant_stage_duration_seconds_bucket{stage="yolo26_pose",le="+Inf"} 10
va_savant_batch_occupancy_total{stage="yolo26_pose",batch_size="3"} 3
va_savant_batch_occupancy_total{stage="yolo26_pose",batch_size="4"} 7
"""
    )

    pose = parsed["stage_metrics"]["yolo26_pose"]
    assert pose["duration_count"] == 10
    assert pose["duration_mean_ms"] == 8.0
    assert pose["duration_p50_upper_ms"] == 10.0
    assert pose["duration_p95_upper_ms"] == 25.0
    assert pose["batch_occupancy"] == {"3": 3, "4": 7}
    assert pose["batch_full_ratio"] == 0.7


def test_aggregate_savant_stage_metrics_sums_dual_branches() -> None:
    module = _load_module()
    row = {
        "duration_count": 5,
        "duration_sum_s": 0.04,
        "duration_last_s": 0.009,
        "duration_buckets": {"0.01": 4, "0.025": 5, "+Inf": 5},
        "batch_occupancy": {"4": 5},
    }
    merged = module.aggregate_savant_stage_metrics(
        {"shards": [{"stage_metrics": {"yolo26_pose": row}}, {"stage_metrics": {"yolo26_pose": row}}]}
    )

    pose = merged["yolo26_pose"]
    assert pose["duration_count"] == 10
    assert pose["duration_mean_ms"] == 8.0
    assert pose["batch_occupancy"] == {"4": 10}
    assert pose["batch_full_ratio"] == 1.0


def test_savant_ablation_module_is_cumulative_and_artifact_scoped(tmp_path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        savant_ablation_stage="pose-face",
        savant_output_mode="metadata-only",
    )

    generated_path = module.write_savant_ablation_module(cfg)
    generated = yaml.safe_load(generated_path.read_text(encoding="utf-8"))
    names = [element["name"] for element in generated["pipeline"]["elements"]]

    assert generated_path.parent == tmp_path
    assert names == [
        "yolo26_pose",
        "tracker",
        "behavior_rules",
        "yolov8_face",
        "face_person_associator",
        "savant_perf_metrics",
    ]
    manifest = json.loads((tmp_path / "savant_ablation_manifest.json").read_text())
    assert manifest["stage"] == "pose-face"
    assert manifest["output_mode"] == "metadata-only"
    assert "adaface" in manifest["removed_elements"]


def test_adaface_classifier_async_uses_generated_nvinfer_config(tmp_path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        savant_ablation_stage="full-exporter",
        adaface_classifier_async=True,
    )

    generated_path = module.write_savant_ablation_module(cfg)
    generated = yaml.safe_load(generated_path.read_text(encoding="utf-8"))
    adaface = next(
        element
        for element in generated["pipeline"]["elements"]
        if element["name"] == "adaface"
    )
    config_path = tmp_path / "adaface_nvinfer_classifier_async.txt"

    assert config_path.read_text(encoding="utf-8") == (
        "[property]\nclassifier-async-mode=1\n"
    )
    assert adaface["model"]["local_path"] == str(tmp_path)
    assert adaface["model"]["config_file"] == config_path.name


def test_adaface_input_queue_is_bounded_and_non_leaky(tmp_path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        savant_ablation_stage="full-exporter",
        adaface_input_queue=True,
    )

    generated_path = module.write_savant_ablation_module(cfg)
    generated = yaml.safe_load(generated_path.read_text(encoding="utf-8"))
    elements = generated["pipeline"]["elements"]
    names = [element.get("name") for element in elements]
    queue = elements[names.index("adaface_input_queue")]

    assert names.index("adaface_input_queue") + 1 == names.index("adaface")
    assert queue["element"] == "queue"
    assert queue["properties"] == {
        "max-size-buffers": 32,
        "max-size-bytes": 0,
        "max-size-time": 0,
        "leaky": 0,
    }


def test_adaface_crop_resize_canary_replaces_landmark_preprocessor(tmp_path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        savant_ablation_stage="full-exporter",
        adaface_crop_resize=True,
    )

    generated_path = module.write_savant_ablation_module(cfg)
    generated = yaml.safe_load(generated_path.read_text(encoding="utf-8"))
    adaface = next(
        element
        for element in generated["pipeline"]["elements"]
        if element.get("name") == "adaface"
    )

    assert adaface["model"]["input"]["preprocess_object_image"] == {
        "module": "custom.preprocessors.face_crop_resize",
        "class_name": "FaceCropResizePreprocessingObjectImageGPU",
    }


def test_adaface_pre_gate_throttles_candidates_before_embedding(tmp_path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        savant_ablation_stage="full-exporter",
        adaface_pre_gate=True,
    )

    generated_path = module.write_savant_ablation_module(cfg)
    generated = yaml.safe_load(generated_path.read_text(encoding="utf-8"))
    elements = generated["pipeline"]["elements"]
    names = [element.get("name") for element in elements]
    by_name = {element.get("name"): element for element in elements}

    assert names.index("face_reid_candidate_gate") + 1 == names.index("adaface")
    assert names.index("face_observation_exporter") + 1 == names.index(
        "savant_perf_metrics"
    )
    assert names.index("savant_perf_metrics") + 1 == names.index(
        "face_reid_candidate_cleanup"
    )
    assert names.index("face_reid_candidate_cleanup") < names.index(
        "frame_annotation_exporter"
    )
    assert by_name["adaface"]["model"]["input"]["object"] == (
        "face_reid_candidate.face"
    )
    assert by_name["face_reid_gate"]["kwargs"]["face_element_name"] == (
        "face_reid_candidate"
    )
    assert by_name["face_observation_exporter"]["kwargs"][
        "face_element_name"
    ] == "face_reid_candidate"
    manifest = json.loads((tmp_path / "savant_ablation_manifest.json").read_text())
    assert manifest["adaface_pre_gate"] is True


def test_decoupled_adaface_keeps_embedding_off_primary_critical_path(
    tmp_path,
) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        dual_shard_same_gpu=True,
        savant_ablation_stage="full-exporter",
        savant_output_mode="metadata-only",
        adaface_decoupled=True,
        batched_push_timeout=10000,
    )

    override_path = module.write_dual_shard_same_gpu_compose_override(cfg)
    override = yaml.safe_load(override_path.read_text(encoding="utf-8"))
    primary = yaml.safe_load((tmp_path / "module.pressure.yml").read_text())
    central = yaml.safe_load(
        (tmp_path / "module.adaface-central.yml").read_text()
    )
    primary_names = [item["name"] for item in primary["pipeline"]["elements"]]
    central_names = [item["name"] for item in central["pipeline"]["elements"]]

    assert "yolov8_face" in primary_names
    assert "frame_annotation_exporter" in primary_names
    assert "adaface" not in primary_names
    assert "face_observation_exporter" not in primary_names
    assert central_names == [
        "face_reid_candidate_gate",
        "adaface",
        "face_reid_gate",
        "face_observation_exporter",
        "savant_perf_metrics",
        "face_reid_candidate_cleanup",
    ]
    services = override["services"]
    for shard in ("a", "b"):
        assert services[f"savant-{shard}"]["environment"]["OUTPUT_FRAME"] == (
            '{"codec":"copy"}'
        )
        forwarder_service = services[f"adaface-forwarder-{shard}"]
        assert forwarder_service["volumes"] == [
            f"{(Path.cwd() / 'services/analysis-forwarder/app').resolve()}:/app/app:ro"
        ]
        assert forwarder_service["ports"] == [
            f"{18188 if shard == 'a' else 18189}:8081"
        ]
        forwarder = forwarder_service["environment"]
        assert forwarder["FORWARDER_QUEUE_MAX_SIZE"] == "512"
        assert forwarder["FORWARDER_SAMPLER_ENABLED"] == "false"
        assert forwarder["FORWARDER_REQUIRE_OBJECT_NAMESPACE"] == "yolov8_face"
        assert forwarder["FORWARDER_REQUIRE_ATTRIBUTE_NAME"] == "person_track_id"
        assert "env_file" not in forwarder_service
        assert forwarder["FORWARDER_MIN_OBJECT_CONFIDENCE"] == "0.45"
        assert forwarder["FORWARDER_MIN_OBJECT_WIDTH"] == "40"
        assert forwarder["FORWARDER_MIN_OBJECT_HEIGHT"] == "40"
        assert forwarder["FORWARDER_SEND_TIMEOUT_MS"] == "50"
        assert forwarder["FORWARDER_SEND_RETRIES"] == "0"
        assert forwarder["FORWARDER_OUT_ENDPOINT"].endswith(
            "savant-adaface-central:5557"
        )
    central_service = services["savant-adaface-central"]
    assert central_service["environment"]["BATCH_SIZE"] == "16"
    assert central_service["environment"]["BATCHED_PUSH_TIMEOUT"] == "10000"
    assert central_service["environment"]["FACE_EMBEDDING_BATCH_SIZE"] == "16"
    assert central_service["environment"]["OUTPUT_FRAME"] == "null"
    assert module.dual_shard_services(cfg)[-3:] == module.ADAFACE_DECOUPLED_SERVICES


def test_non_evidence_ablation_skips_strict_event_quiescence() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'cfg.savant_ablation_stage == "full-evidence"' in source
    assert '"reason": "non_evidence_savant_ablation"' in source


def test_non_evidence_ablation_disables_evidence_tasks_via_compose_override(
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        savant_ablation_stage="full-exporter",
    )
    captured: dict[str, object] = {}

    original_run = module.run
    original_snapshot = module.event_worker_evidence_env_snapshot
    module.run = lambda cmd, path, **kwargs: captured.update(
        {"cmd": cmd, "path": path, "kwargs": kwargs}
    )
    module.event_worker_evidence_env_snapshot = lambda: {
        "EVIDENCE_TASK_CREATION_ENABLED": "false"
    }
    try:
        result = module.configure_event_worker_for_pressure(cfg)
    finally:
        module.run = original_run
        module.event_worker_evidence_env_snapshot = original_snapshot

    assert result is not None
    override_path = tmp_path / "compose_recreate_event_worker_pressure_admission.override.yml"
    override = yaml.safe_load(override_path.read_text(encoding="utf-8"))
    assert override["services"]["event-worker"]["environment"] == {
        "EVIDENCE_TASK_CREATION_ENABLED": "false"
    }
    assert str(override_path) in captured["cmd"]


def test_gpu_samples_include_t4_clock_power_and_throttle_state() -> None:
    module = _load_module()

    class Completed:
        stdout = "gpu-sample\n"

    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return Completed()

    original_run = module.subprocess.run
    module.subprocess.run = fake_run
    try:
        assert module.nvidia_smi_csv() == "gpu-sample\n"
    finally:
        module.subprocess.run = original_run

    query = calls[0][1]
    assert "pstate" in query
    assert "power.draw" in query
    assert "clocks.current.sm" in query
    assert "clocks_throttle_reasons.active" in query


def test_dual_shard_override_uses_artifact_module_and_metadata_output(tmp_path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        dual_shard_same_gpu=True,
        dual_shard_api=True,
        savant_ablation_stage="pose-only",
        savant_output_mode="metadata-only",
        cpu_isolation_profile="t4-16cpu",
    )

    override_path = module.write_dual_shard_same_gpu_compose_override(cfg)
    override = yaml.safe_load(override_path.read_text(encoding="utf-8"))

    for service in ("savant-a", "savant-b"):
        service_doc = override["services"][service]
        assert service_doc["environment"]["SAVANT_MODULE_FILE"] == str(
            tmp_path.resolve() / "module.pressure.yml"
        )
        assert service_doc["environment"]["OUTPUT_FRAME"] == "null"
        assert "volumes" not in service_doc
    assert override["services"]["savant-a"]["cpuset"] == "0-2,8-10"
    assert override["services"]["savant-b"]["cpuset"] == "3-5,11-13"
    assert override["services"]["analysis-forwarder-a"]["cpuset"] == "6,14"
    assert override["services"]["analysis-forwarder-b"]["cpuset"] == "6,14"


def test_worker_cpu_restore_recreates_containers_for_empty_original_cpuset(
    tmp_path, monkeypatch
) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path)
    subprocess_calls: list[list[str]] = []
    compose_calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = ""

    def fake_subprocess_run(cmd, **_kwargs):
        subprocess_calls.append(cmd)
        return Completed()

    def fake_run(cmd, _log_path, check=True):
        compose_calls.append(cmd)
        return Completed()

    observed = {
        "video-analytics-midterm-event-worker": "1-2",
        "video-analytics-midterm-face-worker": "",
    }
    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "docker_container_cpuset", observed.__getitem__)

    result = module.restore_worker_cpu_isolation(
        cfg,
        {
            "profile": "test",
            "containers": {
                "video-analytics-midterm-event-worker": {"original_cpuset": "1-2"},
                "video-analytics-midterm-face-worker": {"original_cpuset": ""},
            },
        },
    )

    assert subprocess_calls == [
        [
            "docker",
            "update",
            "--cpuset-cpus",
            "1-2",
            "video-analytics-midterm-event-worker",
        ]
    ]
    assert compose_calls[0][-1] == "face-worker"
    assert result["containers"]["video-analytics-midterm-event-worker"]["ok"] is True
    assert result["containers"]["video-analytics-midterm-face-worker"] == {
        "restored_cpuset": "",
        "observed_cpuset": "",
        "method": "compose_recreate",
        "ok": True,
        "error": "",
    }


def test_pressure_materialization_quota_auto_keeps_retained_headroom() -> None:
    module = _load_module()

    assert module.pressure_materialization_event_type_quotas(
        "auto",
        keep_evidence=50,
    ) == "intrusion:36,watchlist_hit:24"
    assert module.pressure_materialization_event_type_quotas(
        "off",
        keep_evidence=50,
    ) == ""
    assert module.pressure_materialization_event_type_quotas(
        "intrusion:10,watchlist_hit:5",
        keep_evidence=50,
    ) == "intrusion:10,watchlist_hit:5"


def test_clip_worker_compose_accepts_inline_replay_shards_json() -> None:
    compose_path = ROOT.parent / "infra" / "docker-compose.midterm.yml"
    compose = compose_path.read_text(encoding="utf-8")
    clip_start = compose.index("  clip-worker:")
    clip_end = compose.index("  video-file-sink:", clip_start)
    clip_worker = compose[clip_start:clip_end]

    assert 'REPLAY_SHARDS_JSON: "${REPLAY_SHARDS_JSON:-}"' in clip_worker


def test_configure_clip_worker_for_topology_shards_injects_json_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        pressure_materialization_event_type_quotas="intrusion:2,watchlist_hit:2",
    )
    shard_path = tmp_path / "replay_shards.topology.json"
    shard_doc = {
        "default_shard_id": "replay-a",
        "shards": [
            {
                "shard_id": "replay-a",
                "replay_api_url": "http://replay-a:8080",
                "in_stream_endpoint": "dealer+connect:tcp://replay-a:5555",
                "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-a:6666",
                "source_ids": ["pressure60_test_00"],
            },
            {
                "shard_id": "replay-b",
                "replay_api_url": "http://replay-b:8080",
                "in_stream_endpoint": "dealer+connect:tcp://replay-b:5555",
                "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-b:6666",
                "source_ids": ["pressure60_test_01"],
            },
        ],
    }
    shard_path.write_text(json.dumps(shard_doc), encoding="utf-8")
    observed: dict[str, str] = {}

    def fake_run(cmd, log_path, *, env=None, check=True):
        observed["cmd"] = " ".join(cmd)
        observed["log_path"] = str(log_path)
        observed["REPLAY_SHARDS_JSON"] = (env or {}).get("REPLAY_SHARDS_JSON", "")
        observed["REPLAY_SHARDS_CONFIG_PATH"] = (env or {}).get(
            "REPLAY_SHARDS_CONFIG_PATH",
            "",
        )
        observed["EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS"] = (env or {}).get(
            "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS",
            "",
        )
        observed["EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY"] = (env or {}).get(
            "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY",
            "",
        )

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(
        module,
        "clip_worker_replay_shard_env_snapshot",
        lambda: {
            "REPLAY_SHARDS_JSON": observed.get("REPLAY_SHARDS_JSON", ""),
            "REPLAY_SHARDS_CONFIG_PATH": observed.get("REPLAY_SHARDS_CONFIG_PATH", ""),
            "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS": observed.get(
                "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS",
                "",
            ),
            "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY": observed.get(
                "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY",
                "",
            ),
            "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD": "9",
        },
    )

    summary = module.configure_clip_worker_for_topology_shards(
        cfg,
        {"replay_shards_path": str(shard_path)},
    )

    assert "up -d --no-deps --force-recreate clip-worker" in observed["cmd"]
    assert observed["REPLAY_SHARDS_CONFIG_PATH"] == ""
    assert observed["EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS"] == (
        "intrusion:2,watchlist_hit:2"
    )
    expected_shard_doc = {
        **shard_doc,
        "mapping_version": "pressure60_test:topology:v1",
    }
    assert json.loads(observed["REPLAY_SHARDS_JSON"]) == expected_shard_doc
    assert summary["shard_count"] == 2
    assert summary["source_count"] == 2
    assert summary["observed_replay_shards_json_sha256"] == summary[
        "replay_shards_json_sha256"
    ]
    assert summary["observed_materialization_event_type_quotas"] == (
        "intrusion:2,watchlist_hit:2"
    )
    assert observed["EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY"] == "18"
    assert summary["observed_materialization_max_concurrency"] == "18"
    assert summary["observed_materialization_max_concurrency_per_shard"] == "9"
    written = json.loads((tmp_path / "clip_worker_replay_shards_pressure.json").read_text())
    assert written["topology_replay_shards_path"] == str(shard_path)


def test_configure_clip_worker_replay_shards_fails_if_env_not_applied(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path)

    monkeypatch.setattr(module, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module,
        "clip_worker_replay_shard_env_snapshot",
        lambda: {"REPLAY_SHARDS_JSON": "", "REPLAY_SHARDS_CONFIG_PATH": ""},
    )

    try:
        module.configure_clip_worker_replay_shards(
            cfg,
            replay_shards_json='{"shards":[]}',
            replay_shards_config_path="",
            artifact_name="compose_recreate_clip_worker_replay_shards.log",
        )
    except RuntimeError as exc:
        assert "REPLAY_SHARDS_JSON was not applied" in str(exc)
    else:
        raise AssertionError("expected unapplied replay shard env to fail")


def test_validate_seq_iq_from_sampling_is_warning_without_ingress_failure() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=0)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 0,
            "max_forwarder_queue_depth": 0,
            "queue_full_samples": 0,
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


def test_validate_seq_iq_with_transient_non_full_queue_remains_warning() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=0)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 1,
            "max_savant_send_failures_delta": 0,
            "max_forwarder_queue_depth": 1,
            "queue_full_samples": 0,
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


def test_retained_evidence_run_fails_when_savant_semantic_outputs_are_zero() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=50)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 0,
            "max_queue_depth": 0,
            "queue_full_samples": 0,
            "final_savant_pose_objects_total": 0,
            "final_savant_person_observations_exported_total": 0,
            "final_savant_face_observations_exported_total": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "savant_pose_objects_zero" in reasons
    assert "savant_person_observations_zero" in reasons
    assert "savant_face_observations_zero" in reasons


def test_ingress_only_run_does_not_require_savant_semantic_outputs() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=0)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 0,
            "max_queue_depth": 0,
            "queue_full_samples": 0,
            "final_savant_pose_objects_total": 0,
            "final_savant_person_observations_exported_total": 0,
            "final_savant_face_observations_exported_total": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "savant_pose_objects_zero" not in reasons
    assert "savant_person_observations_zero" not in reasons
    assert "savant_face_observations_zero" not in reasons


def test_forwarder_null_sink_skips_savant_and_evidence_gates() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=50, forwarder_null_sink=True)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 0,
            "max_savant_send_failures_total": 0,
            "max_queue_depth": 0,
            "queue_full_samples": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 25}},
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "insufficient_playable_evidence" not in reasons
    assert "savant_did_not_see_all_sources" not in reasons
    assert "validate_seq_iq_exceeded" not in reasons
    assert module.pressure_warnings(cfg, diagnostics, reasons) == []


def test_keep_all_evidence_run_requires_every_event_accounted_for() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=-1)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 0,
            "max_queue_depth": 0,
            "queue_full_samples": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
    }

    reasons = module.pressure_failure_reasons(
        cfg,
        [],
        diagnostics,
        db_before_cleanup={
            "events": 4,
            "playable_bundles": 3,
            "task_statuses": [
                {
                    "materialization_status": "materialization_expired",
                    "count": 1,
                }
            ],
        },
    )

    assert "event_outcomes_unaccounted" in reasons
    assert "materialization_expired_present" in reasons


def test_pressure_run_fails_when_observed_window_exceeds_duration() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=-1, duration_s=400)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 0,
            "max_queue_depth": 0,
            "queue_full_samples": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
    }

    reasons = module.pressure_failure_reasons(
        cfg,
        [],
        diagnostics,
        db_before_cleanup={
            "events": 1,
            "distinct_events_with_playable_evidence": 1,
            "distinct_events_with_terminal_nonplayable_outcome": 0,
            "blocking_materialization_tasks": 0,
            "task_statuses": [],
        },
        event_quiescence={
            "last_snapshot": {
                "db_ingest": {
                    "event_ts_span_s": "1772.0",
                    "created_at_span_s": "1771.8",
                }
            }
        },
    )

    assert "pressure_observed_window_exceeded" in reasons


def test_pressure_observed_window_uses_event_time_not_delayed_insert_time() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=-1, duration_s=400)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 0,
            "max_queue_depth": 0,
            "queue_full_samples": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
    }

    reasons = module.pressure_failure_reasons(
        cfg,
        [],
        diagnostics,
        db_before_cleanup={
            "events": 1,
            "distinct_events_with_playable_evidence": 1,
            "distinct_events_with_terminal_nonplayable_outcome": 0,
            "blocking_materialization_tasks": 0,
            "task_statuses": [],
        },
        event_quiescence={
            "last_snapshot": {
                "db_ingest": {
                    "event_ts_span_s": "399.5",
                    "created_at_span_s": "1771.8",
                }
            }
        },
    )

    assert "pressure_observed_window_exceeded" not in reasons


def test_pressure_observed_window_uses_post_cleanup_span_when_available() -> None:
    module = _load_module()
    cfg = _config(module, keep_evidence=-1, duration_s=400)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 2,
            "max_savant_send_failures_total": 0,
            "max_queue_depth": 0,
            "queue_full_samples": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
    }

    reasons = module.pressure_failure_reasons(
        cfg,
        [],
        diagnostics,
        db_before_cleanup={
            "events": 1,
            "distinct_events_with_playable_evidence": 1,
            "distinct_events_with_terminal_nonplayable_outcome": 0,
            "blocking_materialization_tasks": 0,
            "task_statuses": [],
        },
        event_quiescence={
            "last_snapshot": {
                "db_ingest": {
                    "event_ts_span_s": "474.44",
                    "created_at_span_s": "509.82",
                }
            }
        },
        post_sample_cleanup={
            "status": "completed",
            "event_rows_deleted": 120,
            "post_cleanup_db_ingest": {
                "event_ts_span_s": "400.0",
                "created_at_span_s": "430.0",
            },
        },
    )

    assert "pressure_observed_window_exceeded" not in reasons


def test_pressure_run_fails_when_extra_sources_are_visible() -> None:
    module = _load_module()
    cfg = _config(module, stream_count=60, keep_evidence=0)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 88,
            "max_savant_sources": 88,
            "max_savant_send_failures_total": 0,
            "max_queue_depth": 0,
            "queue_full_samples": 0,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "forwarder_saw_extra_sources" in reasons
    assert "savant_saw_extra_sources" in reasons


def test_runtime_sample_summary_dedupes_duplicate_source_rows(tmp_path: Path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        stream_count=2,
        duration_s=10,
        fps="1/1",
        adaface_decoupled=True,
    )
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "runtime_000.json").write_text(
        json.dumps(
            {
                "metrics": {
                    "sources": [
                        {
                            "source_id": "pressure60_test_00",
                            "frames_seen_total": 10,
                            "pose_objects_total": 2,
                            "face_objects_total": 3,
                            "adaface_embeddings_total": 3,
                            "person_observations_exported_total": 2,
                            "face_observations_exported_total": 1,
                            "windows": {"10s": {"va_savant_effective_fps": 1.0}},
                        },
                        {
                            "source_id": "pressure60_test_00",
                            "frames_seen_total": 5,
                            "pose_objects_total": 1,
                            "face_objects_total": 1,
                            "adaface_embeddings_total": 1,
                            "person_observations_exported_total": 1,
                            "face_observations_exported_total": 0,
                            "windows": {"10s": {"va_savant_effective_fps": 0.5}},
                        },
                        {
                            "source_id": "pressure60_test_01",
                            "frames_seen_total": 7,
                            "pose_objects_total": 1,
                            "face_objects_total": 2,
                            "adaface_embeddings_total": 2,
                            "person_observations_exported_total": 1,
                            "face_observations_exported_total": 1,
                            "windows": {"10s": {"va_savant_effective_fps": 1.0}},
                        },
                    ]
                },
                "forwarder": {
                    "global": {"queue_depth": 0},
                    "sources": [
                        {
                            "source_id": "pressure60_test_00",
                            "frames_seen_total": 10,
                            "frames_forwarded_total": 6,
                            "frames_dropped_total": 4,
                            "savant_send_failures_total": 0,
                        },
                        {
                            "source_id": "pressure60_test_00",
                            "frames_seen_total": 5,
                            "frames_forwarded_total": 3,
                            "frames_dropped_total": 2,
                            "savant_send_failures_total": 0,
                        },
                        {
                            "source_id": "pressure60_test_01",
                            "frames_seen_total": 7,
                            "frames_forwarded_total": 4,
                            "frames_dropped_total": 3,
                            "savant_send_failures_total": 0,
                        },
                    ],
                },
                "adaface_forwarder": {
                    "global": {"queue_depth": 0},
                    "sources": [
                        {
                            "source_id": "pressure60_test_00",
                            "frames_seen_total": 6,
                            "frames_forwarded_total": 2,
                            "frames_dropped_total": 4,
                            "metadata_filtered_total": 4,
                            "savant_send_failures_total": 0,
                        },
                        {
                            "source_id": "pressure60_test_01",
                            "frames_seen_total": 4,
                            "frames_forwarded_total": 0,
                            "frames_dropped_total": 4,
                            "metadata_filtered_total": 4,
                            "savant_send_failures_total": 0,
                        },
                    ],
                },
                "adaface_central": {
                    "sources": [
                        {
                            "source_id": "pressure60_test_00",
                            "frames_seen_total": 2,
                            "adaface_embeddings_total": 1,
                            "face_observations_exported_total": 1,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (samples / "docker_stats_000.json").write_text("{}", encoding="utf-8")

    summary = module.summarize_runtime_samples(cfg)

    assert summary["max_forwarder_sources"] == 2
    assert summary["max_savant_sources"] == 2
    assert summary["final_forwarder_frames_seen_total"] == 17
    assert summary["final_forwarder_frames_forwarded_total"] == 10
    assert summary["final_savant_pose_objects_total"] == 3
    assert summary["max_adaface_forwarder_sources"] == 2
    assert summary["max_adaface_forwarder_eligible_sources"] == 1
    assert summary["max_adaface_central_sources"] == 1
    assert summary["max_adaface_central_missing_eligible_sources"] == 0
    assert summary["final_adaface_forwarder_metadata_filtered_total"] == 8


def test_runtime_sample_summary_enforces_configured_steady_fps(tmp_path: Path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        stream_count=1,
        duration_s=60,
        fps="4/1",
        min_fps="99/25",
    )
    samples = tmp_path / "samples"
    samples.mkdir()
    for index, fps in enumerate((1.0, 3.95, 3.97)):
        (samples / f"runtime_{index:03d}.json").write_text(
            json.dumps(
                {
                    "metrics": {
                        "sources": [
                            {
                                "source_id": "pressure60_test_00",
                                "frames_seen_total": (index + 1) * 10,
                                "windows": {"10s": {"va_savant_effective_fps": fps}},
                            }
                        ]
                    },
                    "forwarder": {
                        "global": {"queue_depth": 0},
                        "sources": [
                            {
                                "source_id": "pressure60_test_00",
                                "frames_seen_total": (index + 1) * 10,
                                "frames_forwarded_total": (index + 1) * 10,
                                "frames_dropped_total": 0,
                                "savant_send_failures_total": 0,
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        (samples / f"docker_stats_{index:03d}.json").write_text("{}", encoding="utf-8")

    summary = module.summarize_runtime_samples(cfg)

    assert summary["steady_effective_fps_sample_count"] == 2
    assert summary["steady_effective_fps_mean"] == 3.96
    assert summary["minimum_effective_fps"] == 3.96
    assert summary["steady_effective_fps_meets_minimum"] is True
    assert summary["steady_effective_fps_target_ratio"] == 0.99


def test_pressure_gate_rejects_steady_fps_below_minimum() -> None:
    module = _load_module()
    cfg = _config(module, stream_count=1, keep_evidence=0, fps="4/1", min_fps="99/25")
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 1,
            "max_savant_sources": 1,
            "max_savant_send_failures_delta": 0,
            "queue_full_samples": 0,
            "steady_effective_fps_sample_count": 2,
            "steady_effective_fps_mean": 3.95,
            "steady_effective_fps_meets_minimum": False,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "steady_effective_fps_below_minimum" in reasons


def test_decoupled_adaface_gate_requires_only_eligible_sources_in_central() -> None:
    module = _load_module()
    cfg = _config(
        module,
        stream_count=60,
        keep_evidence=0,
        adaface_decoupled=True,
    )
    sample_summary = {
        "max_forwarder_sources": 60,
        "max_savant_sources": 60,
        "max_adaface_forwarder_sources": 60,
        "max_adaface_forwarder_eligible_sources": 55,
        "max_adaface_central_sources": 55,
        "max_adaface_central_missing_eligible_sources": 0,
        "final_adaface_central_missing_eligible_source_ids": [],
        "max_adaface_forwarder_queue_depth": 0,
        "final_adaface_forwarder_send_failures_total": 0,
        "max_savant_send_failures_delta": 0,
        "queue_full_samples": 0,
        "steady_effective_fps_sample_count": 2,
        "steady_effective_fps_meets_minimum": True,
        "final_savant_adaface_embeddings_total": 3249,
    }
    diagnostics = {
        "sample_summary": sample_summary,
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "adaface_central_did_not_see_all_sources" not in reasons
    assert "adaface_central_missed_eligible_sources" not in reasons

    sample_summary["final_adaface_central_missing_eligible_source_ids"] = [
        "pressure60_test_59"
    ]
    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "adaface_central_missed_eligible_sources" in reasons


def test_pressure_gate_requires_live_mps_client_when_enabled() -> None:
    module = _load_module()
    cfg = _config(
        module,
        stream_count=1,
        keep_evidence=0,
        fps="4/1",
        min_fps="99/25",
        cuda_mps=True,
    )
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 1,
            "max_savant_sources": 1,
            "max_savant_send_failures_delta": 0,
            "queue_full_samples": 0,
            "steady_effective_fps_sample_count": 2,
            "steady_effective_fps_mean": 4.0,
            "steady_effective_fps_meets_minimum": True,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
        "cuda_mps": {"ready": True, "server_list": []},
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "cuda_mps_client_unavailable" in reasons


def test_runtime_sample_summary_gates_send_failure_delta(tmp_path: Path) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, stream_count=1, duration_s=10, fps="1/1")
    samples = tmp_path / "samples"
    samples.mkdir()

    for index, frames_seen in enumerate((10, 20)):
        (samples / f"runtime_{index:03d}.json").write_text(
            json.dumps(
                {
                    "metrics": {
                        "sources": [
                            {
                                "source_id": "pressure60_test_00",
                                "frames_seen_total": frames_seen,
                                "pose_objects_total": 1,
                                "face_objects_total": 1,
                                "adaface_embeddings_total": 1,
                                "person_observations_exported_total": 1,
                                "face_observations_exported_total": 1,
                            }
                        ]
                    },
                    "forwarder": {
                        "global": {"queue_depth": 0},
                        "sources": [
                            {
                                "source_id": "pressure60_test_00",
                                "frames_seen_total": frames_seen,
                                "frames_forwarded_total": frames_seen,
                                "frames_dropped_total": 0,
                                "savant_send_failures_total": 1,
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        (samples / f"docker_stats_{index:03d}.json").write_text("{}", encoding="utf-8")

    summary = module.summarize_runtime_samples(cfg)
    reasons = module.pressure_failure_reasons(
        cfg,
        [],
        {
            "sample_summary": summary,
            "source_containers": {
                "exited": 0,
                "restart_count_total": 0,
                "negative_pts_error_total": 0,
            },
            "log_summary": {"savant": {"validate_seq_iq": 0}},
        },
    )

    assert summary["baseline_savant_send_failures_total"] == 1
    assert summary["max_savant_send_failures_total"] == 1
    assert summary["max_savant_send_failures_delta"] == 0
    assert summary["stable_samples"] == 2
    assert "savant_send_failures" not in reasons


def test_forwarder_null_sink_fails_when_queue_is_sampled_full() -> None:
    module = _load_module()
    cfg = _config(module, forwarder_null_sink=True)
    diagnostics = {
        "sample_summary": {
            "max_forwarder_sources": 2,
            "max_savant_sources": 0,
            "max_savant_send_failures_total": 0,
            "max_queue_depth": 2048,
            "queue_full_samples": 1,
        },
        "source_containers": {
            "exited": 0,
            "restart_count_total": 0,
            "negative_pts_error_total": 0,
        },
        "log_summary": {"savant": {"validate_seq_iq": 0}},
    }

    reasons = module.pressure_failure_reasons(cfg, [], diagnostics)

    assert "forwarder_queue_full" in reasons


def test_dual_shard_same_gpu_run_id_prefix() -> None:
    module = _load_module()

    run_id = module._default_run_id("8/1", dual_shard_same_gpu=True)

    assert run_id.startswith("pressure60_dual1gpu_8p1_")


def test_dual_shard_same_gpu_override_pins_both_savants(tmp_path: Path) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, dual_shard_same_gpu=True, dual_shard_gpu="0")

    override_path = module.write_dual_shard_same_gpu_compose_override(cfg)
    text = override_path.read_text(encoding="utf-8")

    assert "savant-a:" in text
    assert "savant-b:" in text
    assert "NVIDIA_VISIBLE_DEVICES: '0'" in text
    assert "CUDA_VISIBLE_DEVICES: '0'" in text
    assert "POSE_BATCH_SIZE: '4'" in text
    assert "FACE_DETECTOR_BATCH_SIZE: '4'" in text
    assert "FACE_EMBEDDING_BATCH_SIZE: '16'" in text
    assert "MAX_PARALLEL_STREAMS: '64'" in text
    assert "BATCHED_PUSH_TIMEOUT: '40000'" in text
    assert "device_ids:" in text


def test_write_dual_shard_pressure_sources_splits_sources_30_30(tmp_path: Path) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, stream_count=60, evidence_shard_count=2)

    class _Rows:
        def fetchall(self):
            return [
                {
                    "id": f"00000000-0000-4000-8000-{index:012d}",
                    "name": f"pressure {index:02d}",
                    "source_id": f"pressure60_test_{index:02d}",
                    "rtsp_url": "rtsp://camera/live",
                    "enabled": True,
                }
                for index in range(60)
            ]

    class _Conn:
        def execute(self, *_args, **_kwargs):
            return _Rows()

    plan = module.write_dual_shard_pressure_sources(_Conn(), cfg)

    assert plan["shards"] == {"replay-a": 30, "replay-b": 30}
    shard_plan = json.loads(Path(plan["shard_plan_path"]).read_text(encoding="utf-8"))
    assert shard_plan["mapping_version"] == "pressure60_test:evidence-shards-2:v1"
    assert len(shard_plan["shards"][0]["source_ids"]) == 30
    assert len(shard_plan["shards"][1]["source_ids"]) == 30
    sources_text = Path(plan["sources_path"]).read_text(encoding="utf-8")
    assert "dealer+connect:tcp://replay-a:5555" in sources_text
    assert "dealer+connect:tcp://replay-b:5555" in sources_text


def test_write_dual_shard_pressure_sources_splits_four_evidence_shards(
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, stream_count=60, evidence_shard_count=4)

    class _Rows:
        def fetchall(self):
            return [
                {
                    "id": f"00000000-0000-4000-8000-{index:012d}",
                    "name": f"pressure {index:02d}",
                    "source_id": f"pressure60_test_{index:02d}",
                    "rtsp_url": "rtsp://camera/live",
                    "enabled": True,
                }
                for index in range(60)
            ]

    class _Conn:
        def execute(self, *_args, **_kwargs):
            return _Rows()

    plan = module.write_dual_shard_pressure_sources(_Conn(), cfg)

    assert plan["evidence_shard_count"] == 4
    assert plan["shards"] == {
        "replay-a": 15,
        "replay-b": 15,
        "replay-c": 15,
        "replay-d": 15,
    }
    shard_plan = json.loads(Path(plan["shard_plan_path"]).read_text(encoding="utf-8"))
    assert shard_plan["mapping_version"] == "pressure60_test:evidence-shards-4:v1"
    assert [len(shard["source_ids"]) for shard in shard_plan["shards"]] == [15, 15, 15, 15]
    sources_text = Path(plan["sources_path"]).read_text(encoding="utf-8")
    assert "dealer+connect:tcp://replay-c:5555" in sources_text
    assert "dealer+connect:tcp://replay-d:5555" in sources_text


def test_four_evidence_shards_raise_global_materialization_limit(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setenv("EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD", "9")

    assert module._evidence_materialization_global_limit(4) == "36"
    assert module._evidence_materialization_global_limit(2) == "18"
    assert module._evidence_materialization_global_limit(8) == "72"


def test_write_dual_shard_pressure_sources_splits_eight_evidence_shards(
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, stream_count=60, evidence_shard_count=8)

    class _Rows:
        def fetchall(self):
            return [
                {
                    "id": f"00000000-0000-4000-8000-{index:012d}",
                    "name": f"pressure {index:02d}",
                    "source_id": f"pressure60_test_{index:02d}",
                    "rtsp_url": "rtsp://camera/live",
                    "enabled": True,
                }
                for index in range(60)
            ]

    class _Conn:
        def execute(self, *_args, **_kwargs):
            return _Rows()

    plan = module.write_dual_shard_pressure_sources(_Conn(), cfg)

    assert plan["evidence_shard_count"] == 8
    assert plan["shards"] == {
        "replay-a": 8,
        "replay-b": 8,
        "replay-c": 8,
        "replay-d": 8,
        "replay-e": 7,
        "replay-f": 7,
        "replay-g": 7,
        "replay-h": 7,
    }
    shard_plan = json.loads(Path(plan["shard_plan_path"]).read_text(encoding="utf-8"))
    assert shard_plan["mapping_version"] == "pressure60_test:evidence-shards-8:v1"
    assert [len(shard["source_ids"]) for shard in shard_plan["shards"]] == [
        8,
        8,
        8,
        8,
        7,
        7,
        7,
        7,
    ]
    sources_text = Path(plan["sources_path"]).read_text(encoding="utf-8")
    assert "dealer+connect:tcp://replay-g:5555" in sources_text
    assert "dealer+connect:tcp://replay-h:5555" in sources_text


def test_write_dual_shard_pressure_sources_supports_single_shard_probe(
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        stream_count=10,
        dual_shard_source_mode="all-a",
    )

    class _Rows:
        def fetchall(self):
            return [
                {
                    "id": f"00000000-0000-4000-8000-{index:012d}",
                    "name": f"pressure {index:02d}",
                    "source_id": f"pressure60_test_{index:02d}",
                    "rtsp_url": "rtsp://camera/live",
                    "enabled": True,
                }
                for index in range(10)
            ]

    class _Conn:
        def execute(self, *_args, **_kwargs):
            return _Rows()

    plan = module.write_dual_shard_pressure_sources(_Conn(), cfg)

    assert plan["source_mode"] == "all-a"
    assert plan["shards"] == {
        "replay-a": 10,
        "replay-b": 0,
        "replay-c": 0,
        "replay-d": 0,
    }
    shard_plan = json.loads(Path(plan["shard_plan_path"]).read_text(encoding="utf-8"))
    assert len(shard_plan["shards"][0]["source_ids"]) == 10
    assert shard_plan["shards"][1]["source_ids"] == []


def test_dual_shard_runtime_overview_merges_forwarder_and_savant(monkeypatch) -> None:
    module = _load_module()
    cfg = _config(
        module,
        dual_shard_same_gpu=True,
        adaface_decoupled=True,
    )

    def fake_fetch(url: str, *, timeout_s: float):
        if "18182" in url:
            return "\n".join(
                [
                    "va_forwarder_queue_depth 0",
                    "va_forwarder_running 1",
                    'va_forwarder_frames_seen_total{source_id="pressure60_test_00"} 10',
                    'va_forwarder_frames_forwarded_total{source_id="pressure60_test_00"} 4',
                    'va_forwarder_frames_dropped_total{source_id="pressure60_test_00"} 6',
                    'va_forwarder_savant_send_failures_total{source_id="pressure60_test_00"} 0',
                ]
            )
        if "18183" in url:
            return "\n".join(
                [
                    "va_forwarder_queue_depth 7",
                    "va_forwarder_running 1",
                    'va_forwarder_frames_seen_total{source_id="pressure60_test_01"} 20',
                    'va_forwarder_frames_forwarded_total{source_id="pressure60_test_01"} 8',
                    'va_forwarder_frames_dropped_total{source_id="pressure60_test_01"} 12',
                    'va_forwarder_savant_send_failures_total{source_id="pressure60_test_01"} 0',
                ]
            )
        if "18188" in url:
            return "\n".join(
                [
                    "va_forwarder_queue_depth 0",
                    "va_forwarder_running 1",
                    'va_forwarder_frames_seen_total{source_id="pressure60_test_00"} 4',
                    'va_forwarder_frames_forwarded_total{source_id="pressure60_test_00"} 1',
                    'va_forwarder_frames_dropped_total{source_id="pressure60_test_00"} 3',
                    'va_forwarder_metadata_filtered_total{source_id="pressure60_test_00"} 3',
                    'va_forwarder_savant_send_failures_total{source_id="pressure60_test_00"} 0',
                ]
            )
        if "18189" in url:
            return "\n".join(
                [
                    "va_forwarder_queue_depth 0",
                    "va_forwarder_running 1",
                    'va_forwarder_frames_seen_total{source_id="pressure60_test_01"} 8',
                    'va_forwarder_frames_forwarded_total{source_id="pressure60_test_01"} 2',
                    'va_forwarder_frames_dropped_total{source_id="pressure60_test_01"} 6',
                    'va_forwarder_metadata_filtered_total{source_id="pressure60_test_01"} 6',
                    'va_forwarder_savant_send_failures_total{source_id="pressure60_test_01"} 0',
                ]
            )
        if "18180" in url:
            return "\n".join(
                [
                    'va_savant_sources_active{service="savant-a"} 1',
                    'va_savant_frames_seen_total{source_id="pressure60_test_00"} 4',
                    'va_savant_effective_fps{source_id="pressure60_test_00",window="10s"} 4.0',
                ]
            )
        if "18181" in url:
            return "\n".join(
                [
                    'va_savant_sources_active{service="savant-b"} 1',
                    'va_savant_frames_seen_total{source_id="pressure60_test_01"} 8',
                    'va_savant_effective_fps{source_id="pressure60_test_01",window="10s"} 8.0',
                ]
            )
        if "18187" in url:
            return "\n".join(
                [
                    'va_savant_sources_active{service="savant-adaface-central"} 2',
                    'va_savant_frames_seen_total{source_id="pressure60_test_00"} 1',
                    'va_savant_frames_seen_total{source_id="pressure60_test_01"} 2',
                ]
            )
        raise AssertionError(url)

    monkeypatch.setattr(module, "fetch_text_url", fake_fetch)

    overview = module.dual_shard_runtime_overview(cfg)

    assert overview["forwarder"]["global"]["queue_depth"] == 7
    assert len(overview["forwarder"]["sources"]) == 2
    assert overview["metrics"]["sources_active"] == 2
    assert len(overview["metrics"]["sources"]) == 2
    assert overview["adaface_forwarder"]["global"]["queue_depth"] == 0
    assert len(overview["adaface_forwarder"]["sources"]) == 2
    assert (
        overview["adaface_forwarder"]["sources"][0][
            "metadata_filtered_total"
        ]
        == 3
    )
    assert len(overview["adaface_central"]["sources"]) == 2


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


def test_evidence_window_validation_requires_configured_windows_and_durations() -> None:
    module = _load_module()
    cfg = _config(
        module,
        evidence_policy_groups=((5, 5), (10, 10), (15, 15)),
    )

    summary = module.evidence_window_validation_summary(
        cfg,
        [
            {
                "event_id": "event-ok",
                "rule_pre_seconds": 5,
                "rule_post_seconds": 5,
                "raw_clip_duration_seconds": 10.0,
            },
            {
                "event_id": "event-short",
                "rule_pre_seconds": 15,
                "rule_post_seconds": 15,
                "raw_clip_duration_seconds": 30.0,
                "actual_raw_clip_duration_seconds": 16.0,
            },
            {
                "event_id": "event-unexpected",
                "rule_pre_seconds": 8,
                "rule_post_seconds": 8,
                "raw_clip_duration_seconds": 16.0,
            },
        ],
    )

    reasons = module.evidence_window_failure_reasons(summary)

    assert summary["status"] == "failed"
    assert "evidence_clip_duration_mismatch" in reasons
    assert "evidence_unexpected_window_policy" in reasons
    assert summary["duration_mismatches"][0]["duration_source"] == "ffprobe"


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


def test_wait_for_drain_full_generation_uses_distinct_event_coverage(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, keep_evidence=-1, drain_s=60)
    summaries = [
        {
            "cameras": 60,
            "events": 100,
            "tasks": 100,
            "bundles": 60,
            "playable_bundles": 60,
            "covered_playable_events": 60,
            "distinct_events_with_playable_evidence": 80,
            "distinct_events_with_terminal_nonplayable_outcome": 0,
            "blocking_materialization_tasks": 20,
            "task_statuses": [],
            "event_types": [],
        },
        {
            "cameras": 60,
            "events": 100,
            "tasks": 100,
            "bundles": 80,
            "playable_bundles": 80,
            "covered_playable_events": 80,
            "distinct_events_with_playable_evidence": 100,
            "distinct_events_with_terminal_nonplayable_outcome": 0,
            "blocking_materialization_tasks": 0,
            "task_statuses": [],
            "event_types": [],
        },
    ]
    observed: list[dict] = []

    def fake_db_summary_connect(_cfg):
        summary = summaries.pop(0)
        observed.append(summary)
        return summary

    now = [2_000.0]
    monkeypatch.setattr(module, "db_summary_connect", fake_db_summary_connect)
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    module.wait_for_drain(cfg)

    assert [item["distinct_events_with_playable_evidence"] for item in observed] == [80, 100]
    snapshots = json.loads((tmp_path / "drain_snapshots.json").read_text())
    assert len(snapshots) == 2


def test_wait_for_drain_does_not_close_on_deferred_nonplayable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, keep_evidence=-1, drain_s=21)
    summary = {
        "cameras": 60,
        "events": 100,
        "suppressed_events": 0,
        "tasks": 100,
        "bundles": 80,
        "playable_bundles": 80,
        "covered_playable_events": 80,
        "distinct_events_with_playable_evidence": 80,
        "distinct_events_with_terminal_nonplayable_outcome": 20,
        "blocking_materialization_tasks": 0,
        "task_statuses": [
            {
                "status": "materialization_deferred",
                "materialization_status": "materialization_deferred",
                "count": 20,
            }
        ],
        "event_types": [],
    }
    observed: list[dict] = []

    def fake_db_summary_connect(_cfg):
        observed.append(dict(summary))
        return dict(summary)

    now = [2_000.0]
    monkeypatch.setattr(module, "db_summary_connect", fake_db_summary_connect)
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    module.wait_for_drain(cfg)

    assert len(observed) == 3
    snapshots = json.loads((tmp_path / "drain_snapshots.json").read_text())
    assert len(snapshots) == 3


def test_pressure_event_quiescence_waits_for_stable_run_events(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, keep_evidence=-1, drain_s=60)
    db_summaries = [
        {"events": 10, "new_events": 6, "suppressed_events": 4, "max_event_ts_ms": 1_000, "max_created_at": "t1"},
        {"events": 12, "new_events": 7, "suppressed_events": 5, "max_event_ts_ms": 2_000, "max_created_at": "t2"},
        {"events": 12, "new_events": 7, "suppressed_events": 5, "max_event_ts_ms": 2_000, "max_created_at": "t2"},
        {"events": 12, "new_events": 7, "suppressed_events": 5, "max_event_ts_ms": 2_000, "max_created_at": "t2"},
    ]
    observed: list[dict] = []

    def fake_db_summary_connect(_cfg):
        summary = db_summaries.pop(0)
        observed.append(summary)
        return summary

    now = [1_000.0]
    monkeypatch.setattr(module, "db_pressure_event_ingest_summary_connect", fake_db_summary_connect)
    monkeypatch.setattr(
        module,
        "redis_run_id_stream_summary",
        lambda *_args: {"total_run_id_entries": 12, "streams": {}},
    )
    monkeypatch.setattr(
        module,
        "inspect_pressure_source_containers",
        lambda _run_id: {"total": 0, "running": 0},
    )
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    summary = module.wait_for_pressure_event_quiescence(
        cfg,
        object(),
        stable_samples_required=3,
        poll_s=10,
    )

    assert summary["status"] == "quiesced"
    assert [item["events"] for item in observed] == [10, 12, 12, 12]
    snapshots = json.loads((tmp_path / "pressure_event_quiescence_snapshots.json").read_text())
    assert snapshots[-1]["stable_samples"] == 3


def test_pressure_runner_refreshes_worker_logs_after_drain_before_observability() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    stop_index = source.index("stop_pressure_sources(conn, cfg)")
    quiescence_index = source.index("wait_for_pressure_event_quiescence(", stop_index)
    wait_index = source.index("wait_for_drain(cfg)")
    refresh_index = source.index("capture_runtime_logs_since_start(cfg, started_at)", wait_index)
    summary_index = source.index('diagnostics["log_summary"] = summarize_logs(cfg)', refresh_index)
    observability_index = source.index("collect_downstream_observability(", summary_index)

    assert stop_index < quiescence_index < wait_index
    assert wait_index < refresh_index < summary_index < observability_index


def test_pressure_runner_postfills_rolling_cache_before_stopping_sources() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    sample_index = source.index('report["pressure_sampling_window"] = sample_runtime(')
    cutoff_index = source.index("pressure_event_sampling_cutoff(conn, cfg)", sample_index)
    postfill_index = source.index("rolling_cache_postfill_after_sampling(cfg)", cutoff_index)
    stop_index = source.index("stop_pressure_sources(conn, cfg)", postfill_index)
    quiescence_index = source.index("wait_for_pressure_event_quiescence(", stop_index)
    post_cleanup_index = source.index("clear_pressure_post_sample_rows(", quiescence_index)
    wait_index = source.index("wait_for_drain(cfg)", post_cleanup_index)

    assert sample_index < cutoff_index < postfill_index < stop_index
    assert stop_index < quiescence_index < post_cleanup_index < wait_index


def test_pressure_runner_starts_rolling_cache_sinks_after_runtime_epoch_changes() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    stop_sink_index = source.index(
        'report["rolling_cache_sinks_stopped_before_reconfigure"]'
    )
    dual_apply_index = source.index("topology = save_and_apply_topology_config(cfg)")
    dual_sink_index = source.index("start_rolling_cache_sinks_for_pressure(", dual_apply_index)
    single_restart_index = source.index("restart = restart_camera_runtime(cfg)")
    single_sink_index = source.index("start_rolling_cache_sinks_for_pressure(", single_restart_index)

    assert stop_sink_index < dual_apply_index
    assert stop_sink_index < single_restart_index
    assert dual_apply_index < dual_sink_index
    assert single_restart_index < single_sink_index


def test_stop_rolling_cache_sinks_before_pressure_reconfigure_stops_dual_sinks(
    monkeypatch, tmp_path: Path
) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        dual_shard_same_gpu=True,
        rolling_cache_evidence=True,
    )
    calls = []

    monkeypatch.setattr(
        module,
        "run",
        lambda command, log_path, **kwargs: calls.append(
            {"command": command, "log_path": log_path, "kwargs": kwargs}
        ),
    )
    monkeypatch.setattr(
        module,
        "docker_container_state",
        lambda name: {"container": name, "running": False, "status": "exited"},
    )

    summary = module.stop_rolling_cache_sinks_before_pressure_reconfigure(cfg)

    assert calls
    command = calls[0]["command"]
    assert "stop" in command
    assert "rolling-cache-sink-a" in command
    assert "rolling-cache-sink-b" in command
    assert calls[0]["kwargs"]["check"] is False
    assert summary["services"] == ["rolling-cache-sink-a", "rolling-cache-sink-b"]


def test_start_rolling_cache_sinks_passes_runtime_epoch_id(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        dual_shard_same_gpu=True,
        rolling_cache_evidence=True,
    )
    calls = []

    def fake_run(command, log_path, env=None, **kwargs):
        calls.append({"command": command, "log_path": log_path, "env": env or {}, "kwargs": kwargs})

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(
        module,
        "docker_container_state",
        lambda name: {"container": name, "running": True, "status": "running"},
    )
    monkeypatch.setattr(
        module,
        "probe_video_input_fps",
        lambda uri: {"status": "measured", "fps": 23.976, "rate": "24000/1001", "uri": uri},
    )

    summary = module.start_rolling_cache_sinks_for_pressure(
        cfg,
        runtime_epoch_id="midterm-epoch-123",
    )

    assert calls
    assert calls[0]["env"]["ROLLING_CACHE_RUNTIME_EPOCH_ID"] == "midterm-epoch-123"
    assert calls[0]["env"]["ROLLING_CACHE_FPS"] == "23.976"
    assert summary["runtime_epoch_id"] == "midterm-epoch-123"
    assert summary["rolling_cache_expected_raw_fps"] == 23.976
    assert summary["dependency_services"] == [
        "replay-raw-fanout-a",
        "replay-raw-fanout-b",
    ]
    assert summary["missing_dependencies"] == []


def test_start_rolling_cache_sinks_fails_when_dual_raw_fanout_missing(
    monkeypatch, tmp_path: Path
) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        dual_shard_same_gpu=True,
        rolling_cache_evidence=True,
    )

    monkeypatch.setattr(module, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module,
        "probe_video_input_fps",
        lambda uri: {"status": "measured", "fps": 23.976, "rate": "24000/1001", "uri": uri},
    )

    def fake_container_state(name: str) -> dict[str, object]:
        return {
            "container": name,
            "running": "replay-raw-fanout-a" not in name,
            "status": "running" if "replay-raw-fanout-a" not in name else "missing",
        }

    monkeypatch.setattr(module, "docker_container_state", fake_container_state)

    try:
        module.start_rolling_cache_sinks_for_pressure(
            cfg,
            runtime_epoch_id="midterm-epoch-123",
        )
    except RuntimeError as exc:
        assert "replay-raw-fanout-a" in str(exc)
    else:
        raise AssertionError("expected missing raw fanout dependency to fail")


def test_capture_runtime_logs_includes_rolling_cache_sinks(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()
    cfg = _config(
        module,
        artifact_dir=tmp_path,
        dual_shard_same_gpu=True,
        rolling_cache_evidence=True,
    )
    runs = []
    combined = []

    monkeypatch.setattr(
        module,
        "write_combined_docker_logs",
        lambda containers, path, since=None: combined.append(
            {"containers": containers, "path": path, "since": since}
        ),
    )
    monkeypatch.setattr(
        module,
        "run",
        lambda command, path, check=False, **kwargs: runs.append(
            {"command": command, "path": path, "check": check}
        ),
    )

    module.capture_runtime_logs_since_start(
        cfg,
        datetime(2026, 7, 5, 3, 14, 41, tzinfo=timezone.utc),
    )

    commands = [" ".join(item["command"]) for item in runs]
    assert any("video-analytics-midterm-rolling-cache-sink-a" in item for item in commands)
    assert any("video-analytics-midterm-rolling-cache-sink-b" in item for item in commands)


def test_compose_exposes_runtime_epoch_override_for_rolling_cache_sinks() -> None:
    source = COMPOSE.read_text(encoding="utf-8")

    assert source.count('ROLLING_CACHE_RUNTIME_EPOCH_ID: "${ROLLING_CACHE_RUNTIME_EPOCH_ID:-}"') >= 3


def test_downstream_observability_schema_accepts_explicit_not_enough_data() -> None:
    module = _load_module()
    summary = {
        "schema_version": module.DOWNSTREAM_OBSERVABILITY_SCHEMA_VERSION,
        "redis": {"streams": {}},
            "postgresql": {
                "run_summary": {},
                "table_stats": module._not_enough_data("synthetic"),
                "evidence_task_lifecycle_seconds": module._not_enough_data("synthetic"),
                "rolling_cache_ready_schedule_seconds": module._not_enough_data("synthetic"),
            },
        "event_worker": {
            "record_request_dedupe": {
                "reserved_count": 0,
                "duplicate_count": 0,
                "reserve_failed_count": 0,
                "latency_ms": module._not_enough_data("synthetic"),
            }
        },
        "face_worker": {
            "gallery_query_latency_ms": module._not_enough_data("synthetic")
        },
        "qdrant": {
            "query_latency_ms": module._not_enough_data("synthetic"),
            "exact_rerank_latency_ms": module._not_enough_data("synthetic"),
            "fallback_count": 0,
            "outbox": module._not_enough_data("synthetic"),
        },
        "media_worker": {
            "finalization_duration_ms": module._not_enough_data("synthetic"),
            "ffprobe_duration_ms": module._not_enough_data("synthetic"),
            "throttle_sleep_s": module._not_enough_data("synthetic"),
            "deadline_slack_s": module._not_enough_data("synthetic"),
            "claim_wait_ms": module._not_enough_data("synthetic"),
            "replay_to_sink_metadata_ms": module._not_enough_data("synthetic"),
            "sink_metadata_to_video_ms": module._not_enough_data("synthetic"),
            "sink_video_to_stable_ms": module._not_enough_data("synthetic"),
            "sink_stable_to_ffprobe_ready_ms": module._not_enough_data("synthetic"),
            "sink_ffprobe_ready_to_finalizer_start_ms": module._not_enough_data("synthetic"),
            "finalizer_pool_wait_ms": module._not_enough_data("synthetic"),
            "queue_wait_ms_by_source": {},
            "queue_wait_ms_by_shard": {},
            "duplicate_materialization_count": 0,
        },
        "phase_latency_ms": {"clip_worker": {}, "media_worker": {}},
        "replay_admission": {
            "slot_acquired_count": 0,
            "slot_released_count": 0,
            "duration_seconds_effective": module._not_enough_data("synthetic"),
            "timeout_budget_s": module._not_enough_data("synthetic"),
            "release_sink_video_to_stable_ms": module._not_enough_data("synthetic"),
        },
        "replay_topology": {
            "replay_topology": "single_sink",
            "dual_replay_enabled": False,
            "containers": {},
        },
        "video_file_sink": {
            "instances": {},
            "aggregate": module._not_enough_data("synthetic"),
        },
        "evidence_8090": {
            "retained_count": 0,
            "checked_count": 0,
            "ok_count": 0,
        },
    }

    assert module.validate_downstream_observability_schema(summary) is True


def test_qdrant_outbox_summary_uses_valid_aggregate_filters() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "count(*) FILTER (WHERE status IN ('pending', 'retry', 'processing'))" in source
    assert "min(created_at)\n                            FILTER" in source
    assert "max(processed_at)\n                            FILTER" in source
    assert "EXTRACT(EPOCH FROM (now() - min(created_at)))\n                        FILTER" not in source


def test_downstream_observability_schema_rejects_missing_required_field() -> None:
    module = _load_module()
    summary = {
        "schema_version": module.DOWNSTREAM_OBSERVABILITY_SCHEMA_VERSION,
        "redis": {"streams": {}},
        "postgresql": {
            "run_summary": {},
            "table_stats": {},
            "evidence_task_lifecycle_seconds": {},
        },
        "event_worker": {"record_request_dedupe": {}},
        "face_worker": {"gallery_query_latency_ms": {}},
        "media_worker": {"finalization_duration_ms": {}},
        "evidence_8090": {"retained_count": 0, "checked_count": 0, "ok_count": 0},
    }

    try:
        module.validate_downstream_observability_schema(summary)
    except ValueError as exc:
        assert "media_worker.ffprobe_duration_ms" in str(exc)
    else:
        raise AssertionError("schema validation should reject missing ffprobe metric")


def test_summarize_logs_extracts_downstream_worker_metrics(tmp_path: Path) -> None:
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path)
    (tmp_path / "event_worker_logs_since_start.txt").write_text(
        "\n".join(
            [
                "record_request_dedupe_reserved source_event_id=a strategy=savant_replay",
                "record_request_dedupe_duplicate source_event_id=a strategy=savant_replay",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "face_worker_logs_since_start.txt").write_text(
        "\n".join(
            [
                "watchlist_gallery_query_completed source_observation_id=obs-1 "
                "camera_id=cam rule_id=rule match_source=db_camera_rule "
                "target_count=2 top_k=5 threshold=0.6000 result_count=1 "
                "gallery_query_duration_ms=12",
                "watchlist_gallery_query_completed source_observation_id=obs-2 "
                "camera_id=cam rule_id=rule match_source=db_camera_rule "
                "target_count=2 top_k=5 threshold=0.6000 result_count=0 "
                "gallery_query_duration_ms=34",
                "watchlist_hit_emitted source_observation_id=obs-1 camera_id=cam",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "clip_worker_logs_since_start.txt").write_text(
        "\n".join(
            [
                "clip_worker_phase_timing event_id=e1 request_id=req-1 "
                "record_request_pending_ms=100 proof_wait_ms=20 "
                "replay_job_create_ms=7 replay_slot_hold_ms=15000 "
                "replay_active_global_count=3 replay_active_shard_count=2 "
                "replay_active_source_count=1",
                "clip_worker_phase_timing event_id=e2 request_id=req-2 "
                "record_request_pending_ms=200 proof_wait_ms=30 "
                "replay_job_create_ms=9 replay_slot_hold_ms=15000 "
                "replay_active_global_count=4 replay_active_shard_count=2 "
                "replay_active_source_count=1",
                "replay_slot_acquired event_id=e2 job_id=job-2 "
                "slot_acquired=True source_id=source-b replay_shard_id=replay-b "
                "sink_instance=video-file-sink-b "
                "resulting_stream_id=replay-event-e2 "
                "replay_duration_seconds_effective=15.0 timeout_budget_s=120.0",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "media_worker_logs_since_start.txt").write_text(
        "\n".join(
            [
                "media_event_finalized event_id=e1 finalization_duration_ms=100 "
                "worker_id=finalizer-1 source_id=source-a replay_shard_id=replay-a "
                "claim_status=claimed claim_wait_ms=3 "
                "scan_duration_ms=1 queue_wait_ms=10 lifecycle_elapsed_ms=110 "
                "post_savant_finalization_elapsed_ms=90 "
                "proof_wait_ms=20 replay_job_create_ms=7 "
                "replay_to_sink_metadata_ms=50 sink_metadata_to_video_ms=5 "
                "sink_video_to_stable_ms=10000 "
                "sink_stable_to_ffprobe_ready_ms=40 "
                "sink_ffprobe_ready_to_finalizer_start_ms=3 "
                "finalizer_pool_wait_ms=4 "
                "throttle_sleep_s=2.0 throttle_reason=paced deadline_slack_s=210.5 "
                "metadata_files_visited=1 ffprobe_invocations=1 "
                "ffprobe_duration_ms=20 ffmpeg_invocations=1 ffmpeg_duration_ms=80 "
                "imageio_ffmpeg_fallback_count=0 imageio_ffmpeg_fallback_duration_ms=0",
                "media_event_finalized event_id=e2 finalization_duration_ms=300 "
                "worker_id=finalizer-2 source_id=source-b replay_shard_id=replay-b "
                "claim_status=claimed claim_wait_ms=7 "
                "scan_duration_ms=1 queue_wait_ms=30 lifecycle_elapsed_ms=330 "
                "post_savant_finalization_elapsed_ms=250 "
                "proof_wait_ms=30 replay_job_create_ms=9 "
                "replay_to_sink_metadata_ms=70 sink_metadata_to_video_ms=7 "
                "sink_video_to_stable_ms=11000 "
                "sink_stable_to_ffprobe_ready_ms=60 "
                "sink_ffprobe_ready_to_finalizer_start_ms=4 "
                "finalizer_pool_wait_ms=6 "
                "throttle_sleep_s=0.0 throttle_reason=deadline_guard deadline_slack_s=45.0 "
                "metadata_files_visited=1 ffprobe_invocations=1 "
                "ffprobe_duration_ms=40 ffmpeg_invocations=1 ffmpeg_duration_ms=160 "
                "imageio_ffmpeg_fallback_count=2 imageio_ffmpeg_fallback_duration_ms=0",
                "media_materialization_paced event_id=e3 reason=max_per_poll_reached",
                "media_finalization_claim_busy event_id=e4",
                "replay_slot_released event_id=e2 "
                "release_reason=sink_video_stable sink_video_to_stable_ms=31000",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "video_file_sink_a_logs_since_start.txt").write_text(
        "\n".join(
            [
                "New writer for source=replay-event-1 is initialized, amount of resident writers is 46",
                "pending_reclaim=62",
                "The pipeline is about to stop. Operation took 0:00:35.100000.",
                "Received EOS from source replay-event-1",
                "(python:1): GStreamer-WARNING **: warning",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "video_file_sink_b_logs_since_start.txt").write_text(
        "\n".join(
            [
                "New writer for source=replay-event-2 is initialized, amount of resident writers is 47",
                "pending reclaim=83",
                "The pipeline is about to stop. Operation took 0:00:36.000000.",
                "Received EOS from source replay-event-2",
                "(python:1): GStreamer-CRITICAL **: critical",
            ]
        ),
        encoding="utf-8",
    )

    summary = module.summarize_logs(cfg)

    assert summary["event_worker"]["record_request_dedupe_reserved"] == 1
    assert summary["event_worker"]["record_request_dedupe_duplicate"] == 1
    assert summary["face_worker"]["watchlist_hit_emitted"] == 1
    assert summary["face_worker"]["watchlist_gallery_query_completed"] == 2
    assert summary["face_worker"]["face_gallery_query_latency_ms"]["count"] == 2
    assert summary["face_worker"]["face_gallery_query_latency_ms"]["max"] == 34.0
    assert summary["media_worker"]["media_event_finalized"] == 2
    assert summary["media_worker"]["media_finalization_claim_busy"] == 1
    assert summary["media_worker"]["media_finalizer_worker_count"] == 2
    assert summary["media_worker"]["media_finalizer_worker_counts"] == {
        "finalizer-1": 1,
        "finalizer-2": 1,
    }
    assert summary["media_worker"]["media_materialization_paced"] == 1
    assert summary["media_worker"]["media_materialization_throttle_paced"] == 1
    assert summary["media_worker"]["media_materialization_throttle_deadline_guard"] == 1
    assert summary["media_worker"]["media_finalization_duration_ms"]["count"] == 2
    assert summary["media_worker"]["media_finalization_duration_ms"]["p50"] == 200.0
    assert summary["media_worker"]["media_queue_wait_ms"]["p95"] == 29.0
    assert (
        summary["media_worker"]["media_queue_wait_ms_by_source"]["source-a"]["p50"]
        == 10.0
    )
    assert (
        summary["media_worker"]["media_queue_wait_ms_by_shard"]["replay-b"]["p50"]
        == 30.0
    )
    assert summary["media_worker"]["media_claim_wait_ms"]["max"] == 7.0
    assert summary["media_worker"]["media_duplicate_materialization_count"] == 0
    assert summary["media_worker"]["media_lifecycle_elapsed_ms"]["max"] == 330.0
    assert summary["media_worker"]["media_post_savant_finalization_elapsed_ms"]["max"] == 250.0
    assert summary["clip_worker"]["clip_proof_wait_ms"]["p95"] == 29.5
    assert summary["clip_worker"]["clip_replay_job_create_ms"]["max"] == 9.0
    assert summary["media_worker"]["media_replay_to_sink_metadata_ms"]["p50"] == 60.0
    assert summary["media_worker"]["media_finalizer_pool_wait_ms"]["max"] == 6.0
    assert summary["clip_worker"]["replay_slot_acquired"] == 1
    assert summary["clip_worker"]["replay_slot_timeout_budget_s"]["max"] == 120.0
    assert summary["media_worker"]["replay_slot_released"] == 1
    assert summary["media_worker"]["replay_slot_release_reasons"] == {
        "sink_video_stable": 1
    }
    assert (
        summary["media_worker"]["replay_slot_release_sink_video_to_stable_ms"]["max"]
        == 31000.0
    )
    assert summary["media_worker"]["media_ffprobe_duration_ms"]["max"] == 40.0
    assert summary["media_worker"]["imageio_ffmpeg_fallback"] == 2
    assert (
        summary["media_worker"]["imageio_ffmpeg_fallback_count_distribution"]["max"]
        == 2.0
    )
    assert summary["media_worker"]["media_throttle_sleep_s"]["max"] == 2.0
    assert summary["media_worker"]["media_deadline_slack_s"]["min"] == 45.0
    sink_summary = summary["video_file_sink"]
    assert sink_summary["instances"]["video-file-sink-a"]["new_writer_count"] == 1
    assert sink_summary["instances"]["video-file-sink-b"]["resident_writer_max"] == 47
    assert sink_summary["aggregate"]["new_writer_count"] == 2
    assert sink_summary["aggregate"]["pending_reclaim_max"] == 83
    assert sink_summary["aggregate"]["gst_error_count"] == 1


def test_dual_pressure_services_include_replay_sinks() -> None:
    module = _load_module()

    assert "video-file-sink-a" in module.DUAL_SHARD_SERVICES
    assert "video-file-sink-b" in module.DUAL_SHARD_SERVICES
    assert "replay-raw-fanout-a" in module.DUAL_SHARD_SERVICES
    assert "replay-raw-fanout-b" in module.DUAL_SHARD_SERVICES


def test_replay_topology_reports_single_sink_when_shards_not_active(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.setattr(
        module,
        "clip_worker_replay_shard_env_snapshot",
        lambda: {"REPLAY_SHARDS_JSON": "", "REPLAY_SHARDS_CONFIG_PATH": ""},
    )
    monkeypatch.setattr(
        module,
        "docker_container_state",
        lambda name: {
            "container": name,
            "exists": True,
            "running": name.endswith("video-file-sink"),
            "status": "running" if name.endswith("video-file-sink") else "exited",
            "health": "",
        },
    )

    summary = module.replay_topology_summary()

    assert summary["replay_topology"] == "single_sink"
    assert summary["dual_replay_enabled"] is False
    assert (
        summary["reason"]
        == "REPLAY_SHARDS_CONFIG_PATH empty or replay/video-file-sink shard containers stopped"
    )
    assert "REPLAY_SHARDS_CONFIG_PATH empty" in summary["reason_details"][0]


def test_replay_topology_reports_dual_shard_when_shards_active(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.setattr(
        module,
        "clip_worker_replay_shard_env_snapshot",
        lambda: {"REPLAY_SHARDS_JSON": '{"shards":[]}', "REPLAY_SHARDS_CONFIG_PATH": ""},
    )
    monkeypatch.setattr(
        module,
        "docker_container_state",
        lambda name: {
            "container": name,
            "exists": True,
            "running": True,
            "status": "running",
            "health": "",
        },
    )

    summary = module.replay_topology_summary()

    assert summary["replay_topology"] == "dual_shard"
    assert summary["dual_replay_enabled"] is True
    assert summary["replay-a"]["status"] == "running"
    assert summary["video-file-sink-b"]["running"] is True
