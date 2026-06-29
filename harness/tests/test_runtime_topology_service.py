from __future__ import annotations

import sys
from pathlib import Path


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

from app.services.runtime_topology import build_topology_plan  # noqa: E402


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
