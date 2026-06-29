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
        "pose_batch_size": 4,
        "face_detector_batch_size": 4,
        "face_embedding_batch_size": 16,
        "max_parallel_streams": 64,
        "batched_push_timeout": 40000,
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
        "forwarder_null_sink": False,
        "dual_shard_same_gpu": False,
        "dual_shard_api": False,
        "dual_shard_gpu": "0",
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
    cfg = _config(module, artifact_dir=tmp_path)
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

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(
        module,
        "clip_worker_replay_shard_env_snapshot",
        lambda: {
            "REPLAY_SHARDS_JSON": observed.get("REPLAY_SHARDS_JSON", ""),
            "REPLAY_SHARDS_CONFIG_PATH": observed.get("REPLAY_SHARDS_CONFIG_PATH", ""),
        },
    )

    summary = module.configure_clip_worker_for_topology_shards(
        cfg,
        {"replay_shards_path": str(shard_path)},
    )

    assert "up -d --no-deps --force-recreate clip-worker" in observed["cmd"]
    assert observed["REPLAY_SHARDS_CONFIG_PATH"] == ""
    assert json.loads(observed["REPLAY_SHARDS_JSON"]) == shard_doc
    assert summary["shard_count"] == 2
    assert summary["source_count"] == 2
    assert summary["observed_replay_shards_json_sha256"] == summary[
        "replay_shards_json_sha256"
    ]
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
    cfg = _config(module, artifact_dir=tmp_path, stream_count=60)

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
    assert len(shard_plan["shards"][0]["source_ids"]) == 30
    assert len(shard_plan["shards"][1]["source_ids"]) == 30
    sources_text = Path(plan["sources_path"]).read_text(encoding="utf-8")
    assert "dealer+connect:tcp://replay-a:5555" in sources_text
    assert "dealer+connect:tcp://replay-b:5555" in sources_text


def test_dual_shard_runtime_overview_merges_forwarder_and_savant(monkeypatch) -> None:
    module = _load_module()
    cfg = _config(module, dual_shard_same_gpu=True)

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
        raise AssertionError(url)

    monkeypatch.setattr(module, "fetch_text_url", fake_fetch)

    overview = module.dual_shard_runtime_overview(cfg)

    assert overview["forwarder"]["global"]["queue_depth"] == 7
    assert len(overview["forwarder"]["sources"]) == 2
    assert overview["metrics"]["sources_active"] == 2
    assert len(overview["metrics"]["sources"]) == 2


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


def test_pressure_runner_refreshes_worker_logs_after_drain_before_observability() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    wait_index = source.index("wait_for_drain(cfg)")
    refresh_index = source.index("capture_runtime_logs_since_start(cfg, started_at)", wait_index)
    summary_index = source.index('diagnostics["log_summary"] = summarize_logs(cfg)', refresh_index)
    observability_index = source.index("collect_downstream_observability(", summary_index)

    assert wait_index < refresh_index < summary_index < observability_index


def test_downstream_observability_schema_accepts_explicit_not_enough_data() -> None:
    module = _load_module()
    summary = {
        "schema_version": module.DOWNSTREAM_OBSERVABILITY_SCHEMA_VERSION,
        "redis": {"streams": {}},
        "postgresql": {
            "run_summary": {},
            "table_stats": module._not_enough_data("synthetic"),
            "evidence_task_lifecycle_seconds": module._not_enough_data("synthetic"),
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
        },
        "evidence_8090": {
            "retained_count": 0,
            "checked_count": 0,
            "ok_count": 0,
        },
    }

    assert module.validate_downstream_observability_schema(summary) is True


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
    (tmp_path / "media_worker_logs_since_start.txt").write_text(
        "\n".join(
            [
                "media_event_finalized event_id=e1 finalization_duration_ms=100 "
                "scan_duration_ms=1 queue_wait_ms=10 lifecycle_elapsed_ms=110 "
                "post_savant_finalization_elapsed_ms=90 "
                "throttle_sleep_s=2.0 throttle_reason=paced deadline_slack_s=210.5 "
                "metadata_files_visited=1 ffprobe_invocations=1 "
                "ffprobe_duration_ms=20 ffmpeg_invocations=1 ffmpeg_duration_ms=80 "
                "imageio_ffmpeg_fallback_count=0 imageio_ffmpeg_fallback_duration_ms=0",
                "media_event_finalized event_id=e2 finalization_duration_ms=300 "
                "scan_duration_ms=1 queue_wait_ms=30 lifecycle_elapsed_ms=330 "
                "post_savant_finalization_elapsed_ms=250 "
                "throttle_sleep_s=0.0 throttle_reason=deadline_guard deadline_slack_s=45.0 "
                "metadata_files_visited=1 ffprobe_invocations=1 "
                "ffprobe_duration_ms=40 ffmpeg_invocations=1 ffmpeg_duration_ms=160 "
                "imageio_ffmpeg_fallback_count=2 imageio_ffmpeg_fallback_duration_ms=0",
                "media_materialization_paced event_id=e3 reason=max_per_poll_reached",
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
    assert summary["media_worker"]["media_materialization_paced"] == 1
    assert summary["media_worker"]["media_materialization_throttle_paced"] == 1
    assert summary["media_worker"]["media_materialization_throttle_deadline_guard"] == 1
    assert summary["media_worker"]["media_finalization_duration_ms"]["count"] == 2
    assert summary["media_worker"]["media_finalization_duration_ms"]["p50"] == 200.0
    assert summary["media_worker"]["media_queue_wait_ms"]["p95"] == 29.0
    assert summary["media_worker"]["media_lifecycle_elapsed_ms"]["max"] == 330.0
    assert summary["media_worker"]["media_post_savant_finalization_elapsed_ms"]["max"] == 250.0
    assert summary["media_worker"]["media_ffprobe_duration_ms"]["max"] == 40.0
    assert summary["media_worker"]["imageio_ffmpeg_fallback"] == 2
    assert (
        summary["media_worker"]["imageio_ffmpeg_fallback_count_distribution"]["max"]
        == 2.0
    )
    assert summary["media_worker"]["media_throttle_sleep_s"]["max"] == 2.0
    assert summary["media_worker"]["media_deadline_slack_s"]["min"] == 45.0
