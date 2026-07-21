from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

api_root = Path(API_DIR).resolve()
loaded_app = sys.modules.get("app")
loaded_app_path = Path(getattr(loaded_app, "__file__", "") or "/").resolve()
if loaded_app is not None and not loaded_app_path.is_relative_to(api_root):
    for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        sys.modules.pop(_mod, None)

from app.services import runtime_topology  # noqa: E402
from app.services import runtime_topology_jobs  # noqa: E402
from app.services.runtime_topology import (  # noqa: E402
    _branch_containers,
    _normalize_config,
    _project_camera_selection,
    build_topology_plan,
    RuntimeTopologyConfig,
    RuntimeTopologyError,
)


def _cameras(count: int) -> list[dict[str, object]]:
    return [
        {
            "id": f"00000000-0000-0000-0000-{index:012d}",
            "source_id": f"camera_{index:02d}",
            "name": f"camera {index:02d}",
            "rtsp_url": f"rtsp://example.test/camera_{index:02d}",
            "enabled": True,
            "gpu_id": index % 2,
        }
        for index in range(count)
    ]


def test_auto_topology_keeps_small_runtime_single_branch() -> None:
    plan = build_topology_plan(
        {"topology_mode": "auto", "streams_per_branch": 30},
        _cameras(1),
        available_gpus=["0"],
    )

    assert plan["effective_mode"] == "single"
    assert plan["dual"] is False
    assert plan["branches"][0]["branch_id"] == "single"
    assert plan["branches"][0]["source_count"] == 1


def test_auto_topology_splits_sixty_sources_on_single_gpu_safely() -> None:
    plan = build_topology_plan(
        {"topology_mode": "auto", "streams_per_branch": 30},
        _cameras(60),
        available_gpus=["0"],
    )

    assert plan["effective_mode"] == "dual_auto"
    assert [branch["source_count"] for branch in plan["branches"]] == [30, 30]
    assert [branch["gpu_id"] for branch in plan["branches"]] == [0, 0]


def test_auto_topology_splits_sixty_sources_across_two_detected_gpus() -> None:
    plan = build_topology_plan(
        {"topology_mode": "auto", "streams_per_branch": 30},
        _cameras(60),
        available_gpus=["0", "1"],
    )

    assert plan["effective_mode"] == "dual_auto"
    assert [branch["source_count"] for branch in plan["branches"]] == [30, 30]
    assert [branch["gpu_id"] for branch in plan["branches"]] == [0, 1]


def test_manual_assignments_override_balanced_branching() -> None:
    plan = build_topology_plan(
        {
            "topology_mode": "dual_same_gpu",
            "shard_strategy": "manual",
            "manual_assignments": {
                "camera_00": "b",
                "camera_01": "b",
                "camera_02": "a",
            },
            "branches": {
                "a": {"gpu_id": 0},
                "b": {"gpu_id": 0},
            },
        },
        _cameras(4),
        available_gpus=["0"],
    )

    branches = {branch["branch_id"]: branch for branch in plan["branches"]}
    assert branches["a"]["source_ids"] == ["camera_02"]
    assert branches["b"]["source_ids"] == ["camera_00", "camera_01", "camera_03"]


def test_dual_topology_sources_and_replay_shards_share_same_source_map() -> None:
    plan = build_topology_plan(
        {
            "topology_mode": "dual_same_gpu",
            "streams_per_branch": 30,
            "branches": {
                "a": {"gpu_id": 0},
                "b": {"gpu_id": 0},
            },
        },
        _cameras(60),
        available_gpus=["0"],
    )

    branches = {branch["branch_id"]: branch for branch in plan["branches"]}
    shards = {
        shard["shard_id"]: shard
        for shard in (plan["replay_shards"] or {}).get("shards", [])
    }
    source_rows = list((plan["sources"] or {}).get("sources", {}).values())

    assert set(shards) == {"replay-a", "replay-b"}
    assert shards["replay-a"]["source_ids"] == branches["a"]["source_ids"]
    assert shards["replay-b"]["source_ids"] == branches["b"]["source_ids"]
    assert shards["replay-a"]["replay_job_sink_url"] == (
        "dealer+connect:tcp://video-file-sink-a:6666"
    )
    assert shards["replay-b"]["replay_job_sink_url"] == (
        "dealer+connect:tcp://video-file-sink-b:6666"
    )
    source_to_shard = {
        row["source_id"]: row["replay_shard_id"]
        for row in source_rows
    }
    assert {
        source_to_shard[source_id]
        for source_id in branches["a"]["source_ids"]
    } == {"replay-a"}
    assert {
        source_to_shard[source_id]
        for source_id in branches["b"]["source_ids"]
    } == {"replay-b"}
    assert {
        row["zmq_endpoint"]
        for row in source_rows
        if row["replay_shard_id"] == "replay-a"
    } == {"dealer+connect:tcp://replay-a:5555"}
    assert {
        row["zmq_endpoint"]
        for row in source_rows
        if row["replay_shard_id"] == "replay-b"
    } == {"dealer+connect:tcp://replay-b:5555"}


def test_production_t4_profile_is_the_full_validated_40_source_operating_point() -> None:
    config = _normalize_config(
        {
            "runtime_profile": "production_t4_40",
            # Named profiles are immutable presets; stale browser values must
            # not silently change the validated production operating point.
            "streams_per_branch": 30,
            "branches": {"a": {"analysis_fps": "8/1"}},
        },
        partial=True,
    )
    plan = build_topology_plan(config, _cameras(40), available_gpus=["0"])

    assert config["topology_mode"] == "dual_same_gpu"
    assert config["pipeline_mode"] == "full_evidence"
    assert config["streams_per_branch"] == 20
    assert [branch["source_count"] for branch in plan["branches"]] == [20, 20]
    assert [branch["gpu_id"] for branch in plan["branches"]] == [0, 0]
    assert plan["expected_source_count"] == 40
    assert plan["cuda_mps_enabled"] is True
    assert plan["mps_savant_percentage"] == 45
    assert plan["mps_adaface_percentage"] == 10
    assert plan["recording_cooldown_seconds"] == 60
    assert plan["media_worker_max_active"] == 10
    assert plan["media_worker_remux_workers"] == 5
    assert plan["media_worker_finalizer_workers"] == 5
    assert plan["materialization_ready_segment_grace_seconds"] == 1
    assert plan["frame_cache_sidecar_max_scan"] == 500
    assert plan["cpuset_cpus"]["savant_a"] == "0-2,8-10"
    assert plan["cpuset_cpus"]["savant_b"] == "3-5,11-13"
    assert plan["cpuset_cpus"]["media_worker"] == "6-7,14-15"
    for branch in config["branches"].values():
        assert branch["analysis_fps"] == "4/1"
        assert branch["analysis_min_fps"] == "99/25"
        assert branch["face_infer_interval"] == 3
        assert branch["batched_push_timeout"] == 10000


def test_local_4090_profile_is_full_60_source_8fps_without_mps() -> None:
    config = _normalize_config(
        {"runtime_profile": "local_4090_60"}, partial=True
    )
    plan = build_topology_plan(config, _cameras(60), available_gpus=["0"])

    assert [branch["source_count"] for branch in plan["branches"]] == [30, 30]
    assert plan["full_pipeline"] is True
    assert plan["cuda_mps_enabled"] is False
    assert plan["media_worker_max_active"] == 12
    assert plan["media_worker_remux_workers"] == 8
    for branch in config["branches"].values():
        assert branch["analysis_fps"] == "8/1"
        assert branch["analysis_min_fps"] == "198/25"
        assert branch["face_infer_interval"] == 7


def test_named_profile_keeps_manual_camera_assignment_choice() -> None:
    config = _normalize_config(
        {
            "runtime_profile": "production_t4_40",
            "shard_strategy": "manual",
            "manual_assignments": {"camera_00": "b", "camera_01": "a"},
        },
        partial=True,
    )
    plan = build_topology_plan(config, _cameras(40), available_gpus=["0"])
    branches = {branch["branch_id"]: branch for branch in plan["branches"]}

    assert config["shard_strategy"] == "manual"
    assert "camera_00" in branches["b"]["source_ids"]
    assert "camera_01" in branches["a"]["source_ids"]


def test_named_profile_cooldown_merges_json_fields_without_replacing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, int]]] = []

    class FakeCursor:
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            text = str(query)
            calls.append((text, dict(params)))
            self.rowcount = 40 if "UPDATE cameras" in text else 12

    class FakeConnection:
        def transaction(self):
            return self

        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def close(self):
            return None

    monkeypatch.setattr(
        runtime_topology.psycopg, "connect", lambda *_args, **_kwargs: FakeConnection()
    )

    result = runtime_topology._apply_profile_cooldown(
        runtime_profile="production_t4_40", cooldown_seconds=60
    )

    assert result == {
        "applied": True,
        "runtime_profile": "production_t4_40",
        "cooldown_seconds": 60,
        "camera_count": 40,
        "rule_count": 12,
        "merge_only": True,
    }
    assert len(calls) == 2
    assert all(params == {"cooldown": 60} for _query, params in calls)
    assert all("jsonb_set" in query and "COALESCE" in query for query, _ in calls)


def test_camera_first_selection_projects_enabled_state_without_mutating_rows() -> None:
    rows = _cameras(4)
    rows[0]["enabled"] = True
    rows[1]["enabled"] = False
    rows[2]["enabled"] = True
    rows[3]["enabled"] = False

    projected = _project_camera_selection(
        {
            "source_ids": ["camera_01", "camera_03"],
            "disable_unselected": True,
        },
        rows,
    )

    assert [row["enabled"] for row in projected] == [False, True, False, True]
    assert [row["enabled"] for row in rows] == [True, False, True, False]


def test_camera_first_selection_rejects_unknown_and_duplicate_sources() -> None:
    with pytest.raises(RuntimeTopologyError, match="not found"):
        _project_camera_selection(
            {"source_ids": ["camera_missing"], "disable_unselected": True},
            _cameras(2),
        )

    with pytest.raises(RuntimeTopologyError, match="duplicates"):
        _project_camera_selection(
            {"source_ids": ["camera_00", "camera_00"]},
            _cameras(2),
        )


def test_background_topology_job_persists_progress_and_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status_path = tmp_path / "topology-apply-status.json"
    monkeypatch.setenv("RUNTIME_TOPOLOGY_APPLY_STATUS_PATH", str(status_path))

    def fake_apply(**kwargs):
        progress = kwargs["progress_callback"]
        progress(
            {
                "phase": "rolling_cache_prefill",
                "percent": 90,
                "message": "rolling-cache 正在预热，还需约 5 秒",
                "rolling_cache": {
                    "prefill_seconds": 25,
                    "remaining_seconds": 5,
                },
            }
        )
        return {
            "mode": "dual_same_gpu",
            "runtime_epoch_id": "epoch-test",
            "camera_selection": {"selected_count": 40},
            "status": {"plan": {"rolling_prefill_seconds": 25}},
        }

    monkeypatch.setattr(runtime_topology_jobs, "apply_runtime_topology_config", fake_apply)
    job = runtime_topology_jobs.start_runtime_topology_apply_job(
        force=False,
        camera_selection={"source_ids": ["camera_00"]},
    )
    assert job["status"] == "running"
    runtime_topology_jobs._ACTIVE_THREAD.join(timeout=2)

    status = runtime_topology_jobs.get_runtime_topology_apply_status()
    assert status["status"] == "succeeded"
    assert status["percent"] == 100
    assert status["rolling_cache"]["remaining_seconds"] == 0
    assert status["result"]["runtime_epoch_id"] == "epoch-test"


def test_topology_status_concurrent_writers_publish_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status_path = tmp_path / "topology-apply-status.json"
    monkeypatch.setenv("RUNTIME_TOPOLOGY_APPLY_STATUS_PATH", str(status_path))
    barrier = threading.Barrier(12)
    failures = []

    def writer(index: int) -> None:
        try:
            barrier.wait(timeout=2.0)
            runtime_topology_jobs._write_status(
                {"status": "running", "phase": f"writer-{index}"}
            )
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3.0)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "running"
    assert payload["phase"].startswith("writer-")
    assert list(tmp_path.glob("*.tmp")) == []

def test_full_pipeline_uses_raw_fanout_as_forwarder_and_includes_rolling_sink() -> None:
    containers = _branch_containers("a", dual=True, full_pipeline=True)

    assert containers["forwarder"] == (
        "video-analytics-midterm-replay-raw-fanout-a"
    )
    assert containers["rolling_sink"] == (
        "video-analytics-midterm-rolling-cache-sink-a"
    )


def test_full_profile_apply_starts_raw_rolling_roi_then_activates_after_prefill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _normalize_config(
        {"runtime_profile": "production_t4_40"}, partial=True
    )
    plan = build_topology_plan(config, _cameras(40), available_gpus=["0"])
    cfg = RuntimeTopologyConfig(
        config_path=tmp_path / "topology.json",
        replay_shards_path=tmp_path / "replay-shards.json",
        docker_socket="/var/run/docker.sock",
        module_config_path=tmp_path / "module.yml",
        sources_config_path=tmp_path / "sources.yml",
        network="test-network",
        adapter_image="test-adapter",
        single_savant_container="single-savant",
        single_forwarder_container="single-forwarder",
        single_replay_container="single-replay",
        compose_source_container="single-source",
        savant_ready_timeout_s=1,
        savant_ready_poll_interval_s=0.01,
    )
    recreated: list[
        tuple[str, dict[str, str | None], dict[str, object]]
    ] = []
    started: list[str] = []

    monkeypatch.setattr(
        runtime_topology,
        "_preflight",
        lambda *_args, **_kwargs: {"ok": True, "checks": []},
    )
    monkeypatch.setattr(runtime_topology, "_stop_all_source_adapters", lambda *_: None)
    monkeypatch.setattr(runtime_topology, "_stop_container", lambda *_: None)
    monkeypatch.setattr(
        runtime_topology,
        "create_runtime_epoch_state",
        lambda **_kwargs: {"runtime_epoch_id": "epoch-test"},
    )
    monkeypatch.setattr(
        runtime_topology,
        "_recreate_container_with_env",
        lambda _client, name, env, **_kwargs: (
            recreated.append((name, dict(env), dict(_kwargs)))
            or {"container": name, "action": "recreated"}
        ),
    )
    monkeypatch.setattr(
        runtime_topology,
        "_start_container",
        lambda _client, name: started.append(name) or 204,
    )
    monkeypatch.setattr(
        runtime_topology,
        "_wait_for_container_ready",
        lambda _client, name, **_kwargs: {
            "container": name,
            "action": "wait_ready",
            "ready": True,
        },
    )
    monkeypatch.setattr(
        runtime_topology,
        "_wait_for_savant_ready",
        lambda *_args, **_kwargs: {"savant_ready": True},
    )
    monkeypatch.setattr(
        runtime_topology,
        "_start_sources_from_plan",
        lambda *_args, **_kwargs: [{"source_id": "camera_00"}],
    )
    monkeypatch.setattr(
        runtime_topology,
        "_wait_for_source_convergence",
        lambda *_args, **_kwargs: {"ready": True, "branches": []},
    )
    monkeypatch.setattr(
        runtime_topology,
        "_full_pipeline_convergence",
        lambda *_args, **_kwargs: {"ready": True, "mode": "full_evidence"},
    )
    monkeypatch.setattr(runtime_topology.time, "sleep", lambda _seconds: None)

    result = runtime_topology._apply_dual_topology(
        cfg,
        object(),
        saved_config=config,
        cameras=_cameras(40),
        export_doc={},
        plan=plan,
    )

    recreated_by_name = {name: env for name, env, _kwargs in recreated}
    recreate_kwargs = {name: kwargs for name, _env, kwargs in recreated}
    assert "video-analytics-midterm-cuda-mps-operator" in started
    assert "video-analytics-midterm-adaface-roi-worker" in recreated_by_name
    assert "video-analytics-midterm-replay-raw-fanout-a" in recreated_by_name
    assert "video-analytics-midterm-replay-raw-fanout-b" in recreated_by_name
    assert "video-analytics-midterm-rolling-cache-sink-a" in recreated_by_name
    assert "video-analytics-midterm-rolling-cache-sink-b" in recreated_by_name
    assert recreated_by_name["video-analytics-midterm-savant-a"]["OUTPUT_FRAME"] == "null"
    assert recreate_kwargs["video-analytics-midterm-savant-a"][
        "host_config_updates"
    ] == {"CpusetCpus": "0-2,8-10"}
    assert recreate_kwargs["video-analytics-midterm-savant-b"][
        "host_config_updates"
    ] == {"CpusetCpus": "3-5,11-13"}
    media_env = recreated_by_name["video-analytics-midterm-media-worker"]
    assert media_env["MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE"] == "10"
    assert media_env["ROLLING_CACHE_MATERIALIZATION_MAX_PER_POLL"] == "10"
    assert media_env["ROLLING_CACHE_MATERIALIZATION_WORKERS"] == "5"
    assert media_env["MEDIA_WORKER_FINALIZER_WORKERS"] == "5"
    assert media_env["MEDIA_WORKER_FINALIZER_PROCESS_WORKERS"] == "5"
    assert media_env["ROLLING_CACHE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS"] == "1"
    assert media_env["FRAME_CACHE_SIDECAR_MAX_SCAN"] == "500"
    assert media_env["FRAME_CACHE_SIDECAR_SCAN_HARD_LIMIT"] == "500"
    assert recreate_kwargs["video-analytics-midterm-media-worker"][
        "host_config_updates"
    ] == {"CpusetCpus": "6-7,14-15"}
    event_updates = [
        env
        for name, env, _kwargs in recreated
        if name == "video-analytics-midterm-event-worker"
    ]
    assert event_updates[0]["EVIDENCE_TASK_CREATION_ENABLED"] == "false"
    assert event_updates[0]["EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS"] == "1"
    assert event_updates[-1]["EVIDENCE_TASK_CREATION_ENABLED"] == "true"
    assert int(event_updates[-1]["EVIDENCE_TASK_EVENT_NOT_BEFORE_TS_MS"] or 0) > 0
    assert result["evidence_activation"]["prefill_seconds"] == 25
    assert result["pipeline_convergence"]["ready"] is True
