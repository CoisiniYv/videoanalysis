from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "services" / "api"
CLIP_WORKER_DIR = ROOT / "services" / "clip-worker"
SHARD_PLAN_SCRIPT = ROOT / "scripts" / "runtime" / "check_replay_shard_plan.py"
MIDTERM_COMPOSE = ROOT / "infra" / "docker-compose.midterm.yml"
DUAL_4090_SHARDS = ROOT / "infra" / "config" / "replay-shards.dual_4090_two_source.json"
REPLAY_A_CONFIG = ROOT / "modules" / "savant_replay" / "config.midterm.replay-a.json"
REPLAY_B_CONFIG = ROOT / "modules" / "savant_replay" / "config.midterm.replay-b.json"


def _activate_api():
    path = str(API_DIR)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _activate_clip_worker():
    path = str(CLIP_WORKER_DIR)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _sixty_source_shards() -> dict:
    return {
        "default_shard_id": "replay-a",
        "shards": [
            {
                "shard_id": "replay-a",
                "replay_api_url": "http://replay-a:8080",
                "in_stream_endpoint": "dealer+connect:tcp://replay-a:5555",
                "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-a:6666",
                "source_ids": [f"source_{index:02d}" for index in range(30)],
            },
            {
                "shard_id": "replay-b",
                "replay_api_url": "http://replay-b:8080",
                "in_stream_endpoint": "dealer+connect:tcp://replay-b:5555",
                "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-b:6666",
                "source_ids": [f"source_{index:02d}" for index in range(30, 60)],
            },
        ],
    }


def test_shard_map_routes_60_sources_without_camera_streams() -> None:
    _activate_clip_worker()
    from app.replay_shards import parse_replay_shard_map

    shard_map = parse_replay_shard_map(
        _sixty_source_shards(),
        default_replay_api_url="http://default-replay:8080",
        default_in_stream_endpoint="dealer+connect:tcp://default-replay:5555",
        default_replay_job_sink_url="dealer+connect:tcp://video-file-sink:6666",
    )

    assignments = {
        f"source_{index:02d}": shard_map.shard_for_source(f"source_{index:02d}").shard_id
        for index in range(60)
    }

    assert list(assignments.values()).count("replay-a") == 30
    assert list(assignments.values()).count("replay-b") == 30
    assert len(assignments) == 60
    assert shard_map.shard_for_source("source_00").replay_api_url == "http://replay-a:8080"
    assert shard_map.shard_for_source("source_59").replay_job_sink_url == (
        "dealer+connect:tcp://video-file-sink-b:6666"
    )


def test_explicit_shard_map_rejects_unknown_source() -> None:
    _activate_clip_worker()
    from app.replay_shards import ReplayShardConfigError, parse_replay_shard_map

    shard_map = parse_replay_shard_map(
        _sixty_source_shards(),
        default_replay_api_url="http://default-replay:8080",
        default_in_stream_endpoint="dealer+connect:tcp://default-replay:5555",
        default_replay_job_sink_url="dealer+connect:tcp://video-file-sink:6666",
    )

    try:
        shard_map.shard_for_source("source_99")
    except ReplayShardConfigError as exc:
        assert "not assigned" in str(exc)
    else:
        raise AssertionError("unknown source_id must not silently route to a default shard")


def test_runtime_apply_writes_per_source_replay_shard(monkeypatch, tmp_path: Path) -> None:
    _activate_api()
    from app.services import runtime_apply

    monkeypatch.setenv("REPLAY_SHARDS_JSON", json.dumps(_sixty_source_shards()))

    doc = runtime_apply._build_sources_doc(
        [
            {
                "id": "camera-a",
                "source_id": "source_00",
                "rtsp_url": "rtsp://example.local/a",
                "enabled": True,
            },
            {
                "id": "camera-b",
                "source_id": "source_59",
                "rtsp_url": "rtsp://example.local/b",
                "enabled": True,
            },
        ],
        replay_shards=runtime_apply._load_runtime_replay_shards(
            "dealer+connect:tcp://replay-service:5555"
        ),
    )
    out = tmp_path / "sources.generated.yml"
    out.write_text(yaml.safe_dump(doc), encoding="utf-8")
    parsed = yaml.safe_load(out.read_text(encoding="utf-8"))

    assert parsed["sources"]["camera-a"]["replay_shard_id"] == "replay-a"
    assert parsed["sources"]["camera-a"]["zmq_endpoint"] == (
        "dealer+connect:tcp://replay-a:5555"
    )
    assert parsed["sources"]["camera-b"]["replay_shard_id"] == "replay-b"
    assert parsed["sources"]["camera-b"]["zmq_endpoint"] == (
        "dealer+connect:tcp://replay-b:5555"
    )


def test_midterm_shard_plan_script_validates_default_60_source_file() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_replay_shard_plan", SHARD_PLAN_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_replay_shard_plan"] = module
    spec.loader.exec_module(module)

    summary = module.check_plan(
        module.load_config(ROOT / "infra" / "config" / "replay-shards.midterm.json"),
        expected_sources=60,
        max_imbalance=0,
    )

    assert summary["status"] == "PASS_REPLAY_SHARD_PLAN"
    assert summary["shard_counts"] == {"replay-a": 30, "replay-b": 30}


def test_midterm_shard_plan_job_sinks_exist_in_compose() -> None:
    compose = yaml.safe_load(MIDTERM_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]
    plan = yaml.safe_load(
        (ROOT / "infra" / "config" / "replay-shards.midterm.json").read_text(
            encoding="utf-8"
        )
    )

    sink_services = {
        str(shard["replay_job_sink_url"]).split("tcp://", 1)[1].split(":", 1)[0]
        for shard in plan["shards"]
    }

    assert sink_services == {"video-file-sink-a", "video-file-sink-b"}
    assert sink_services.issubset(services)
    assert services["video-file-sink-a"]["profiles"] == [
        "dual-replay-shards",
        "dual-4090-two-source",
    ]
    assert services["video-file-sink-b"]["profiles"] == [
        "dual-replay-shards",
        "dual-4090-two-source",
    ]


def test_dual_4090_two_source_shard_config_routes_real_sources() -> None:
    _activate_clip_worker()
    from app.replay_shards import ReplayShardConfigError, parse_replay_shard_map

    config = yaml.safe_load(DUAL_4090_SHARDS.read_text(encoding="utf-8"))
    shard_map = parse_replay_shard_map(
        config,
        default_replay_api_url="http://replay-service:8080",
        default_in_stream_endpoint="dealer+connect:tcp://replay-service:5555",
        default_replay_job_sink_url="dealer+connect:tcp://video-file-sink:6666",
    )

    primary = shard_map.shard_for_source("primary_rtsp")
    lab = shard_map.shard_for_source("source_00000000-0000-4000-8000-781078565686")

    assert primary.shard_id == "replay-a"
    assert primary.replay_api_url == "http://replay-a:8080"
    assert primary.in_stream_endpoint == "dealer+connect:tcp://replay-a:5555"
    assert primary.replay_job_sink_url == "dealer+connect:tcp://video-file-sink-a:6666"
    assert lab.shard_id == "replay-b"
    assert lab.replay_api_url == "http://replay-b:8080"
    assert lab.in_stream_endpoint == "dealer+connect:tcp://replay-b:5555"
    assert lab.replay_job_sink_url == "dealer+connect:tcp://video-file-sink-b:6666"
    try:
        shard_map.shard_for_source("source_not_in_dual_4090_plan")
    except ReplayShardConfigError as exc:
        assert "not assigned" in str(exc)
    else:
        raise AssertionError("dual 4090 validation must fail closed for unknown sources")


def test_runtime_apply_writes_dual_4090_source_endpoints(monkeypatch) -> None:
    _activate_api()
    from app.services import runtime_apply

    monkeypatch.setenv("REPLAY_SHARDS_CONFIG_PATH", str(DUAL_4090_SHARDS))

    doc = runtime_apply._build_sources_doc(
        [
            {
                "id": "primary-camera",
                "source_id": "primary_rtsp",
                "rtsp_url": "rtsp://example.local/primary",
                "enabled": True,
            },
            {
                "id": "lab-camera",
                "source_id": "source_00000000-0000-4000-8000-781078565686",
                "rtsp_url": "rtsp://example.local/lab",
                "enabled": True,
            },
        ],
        replay_shards=runtime_apply._load_runtime_replay_shards(
            "dealer+connect:tcp://replay-service:5555"
        ),
    )

    assert doc["sources"]["primary-camera"]["replay_shard_id"] == "replay-a"
    assert doc["sources"]["primary-camera"]["zmq_endpoint"] == (
        "dealer+connect:tcp://replay-a:5555"
    )
    assert doc["sources"]["lab-camera"]["replay_shard_id"] == "replay-b"
    assert doc["sources"]["lab-camera"]["zmq_endpoint"] == (
        "dealer+connect:tcp://replay-b:5555"
    )


def test_runtime_apply_routes_compose_source_as_dynamic_when_sharded(monkeypatch) -> None:
    _activate_api()
    from app.services import runtime_apply

    class FakeDockerClient:
        def request(self, method, path, *, body=None, ok_statuses=None):
            if method == "GET" and path == "/containers/json?all=true":
                return 200, b"[]"
            if method == "GET" and path.endswith("/json"):
                return 200, (
                    b'{"State":{"Status":"running"},"Config":{"Env":["SOURCE_ID=primary_rtsp",'
                    b'"ZMQ_ENDPOINT=dealer+connect:tcp://replay-service:5555"]}}'
                )
            raise AssertionError((method, path, body, ok_statuses))

    monkeypatch.setenv("CAMERA_RUNTIME_ZMQ_ENDPOINT", "dealer+connect:tcp://replay-service:5555")
    plan = runtime_apply._source_only_convergence_plan(
        FakeDockerClient(),
        sources_doc={
            "sources": {
                "primary-camera": {
                    "source_id": "primary_rtsp",
                    "uri": "rtsp://example.local/primary",
                    "enabled": True,
                    "adapter_type": "gstreamer",
                    "zmq_endpoint": "dealer+connect:tcp://replay-a:5555",
                    "replay_shard_id": "replay-a",
                }
            }
        },
        compose_source_id="primary_rtsp",
        compose_source_container="video-analytics-midterm-source-adapter",
    )

    actions = [(item["container_name"], item["planned_action"]) for item in plan]
    assert actions == [
        ("video-analytics-midterm-source-adapter", "stop_compose_disabled"),
        ("video-analytics-source-primary_rtsp", "create_start"),
    ]


def test_runtime_apply_allows_disabled_sources_outside_dual_4090_plan(monkeypatch) -> None:
    _activate_api()
    from app.services import runtime_apply

    monkeypatch.setenv("REPLAY_SHARDS_CONFIG_PATH", str(DUAL_4090_SHARDS))

    doc = runtime_apply._build_sources_doc(
        [
            {
                "id": "primary-camera",
                "source_id": "primary_rtsp",
                "rtsp_url": "rtsp://example.local/primary",
                "enabled": True,
            },
            {
                "id": "disabled-probe",
                "source_id": "source_disabled_probe",
                "rtsp_url": "rtsp://example.local/disabled",
                "enabled": False,
            },
        ],
        replay_shards=runtime_apply._load_runtime_replay_shards(
            "dealer+connect:tcp://replay-service:5555"
        ),
    )

    assert doc["sources"]["primary-camera"]["replay_shard_id"] == "replay-a"
    assert doc["sources"]["disabled-probe"]["enabled"] is False
    assert doc["sources"]["disabled-probe"]["replay_shard_id"] == "replay-a"


def test_dual_4090_compose_profile_declares_two_inference_shards() -> None:
    compose = yaml.safe_load(MIDTERM_COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]

    for service_name in (
        "replay-a",
        "replay-b",
        "analysis-forwarder-a",
        "analysis-forwarder-b",
        "savant-a",
        "savant-b",
        "video-file-sink-a",
        "video-file-sink-b",
    ):
        assert "dual-4090-two-source" in services[service_name]["profiles"]

    assert services["replay-a"]["volumes"][0] == (
        "../modules/savant_replay/config.midterm.replay-a.json:/opt/etc/config.json:ro"
    )
    assert services["replay-b"]["volumes"][0] == (
        "../modules/savant_replay/config.midterm.replay-b.json:/opt/etc/config.json:ro"
    )
    assert services["analysis-forwarder-a"]["environment"]["FORWARDER_OUT_ENDPOINT"] == (
        "dealer+connect:tcp://savant-a:5557"
    )
    assert services["analysis-forwarder-b"]["environment"]["FORWARDER_OUT_ENDPOINT"] == (
        "dealer+connect:tcp://savant-b:5557"
    )
    assert services["savant-a"]["environment"]["NVIDIA_VISIBLE_DEVICES"] == (
        "${SAVANT_A_NVIDIA_VISIBLE_DEVICES:-0}"
    )
    assert services["savant-b"]["environment"]["NVIDIA_VISIBLE_DEVICES"] == (
        "${SAVANT_B_NVIDIA_VISIBLE_DEVICES:-all}"
    )
    assert services["savant-b"]["environment"]["CUDA_VISIBLE_DEVICES"] == (
        "${SAVANT_B_CUDA_VISIBLE_DEVICES:-1}"
    )
    assert "/data/video-analytics/models:/models:rw" in services["savant-a"]["volumes"]
    assert (
        "${SAVANT_B_MODEL_ROOT:-/data/video-analytics/models-savant-b}:/models:rw"
        in services["savant-b"]["volumes"]
    )
    assert services["savant-a"]["deploy"]["resources"]["reservations"]["devices"][0][
        "device_ids"
    ] == ["0"]
    assert services["savant-b"]["deploy"]["resources"]["reservations"]["devices"][0][
        "device_ids"
    ] == ["0", "1"]


def test_dual_4090_replay_configs_forward_to_matching_forwarders() -> None:
    replay_a = yaml.safe_load(REPLAY_A_CONFIG.read_text(encoding="utf-8"))
    replay_b = yaml.safe_load(REPLAY_B_CONFIG.read_text(encoding="utf-8"))

    assert replay_a["in_stream"]["url"] == "router+bind:tcp://0.0.0.0:5555"
    assert replay_b["in_stream"]["url"] == "router+bind:tcp://0.0.0.0:5555"
    assert replay_a["out_stream"]["url"] == (
        "dealer+connect:tcp://analysis-forwarder-a:5557"
    )
    assert replay_b["out_stream"]["url"] == (
        "dealer+connect:tcp://analysis-forwarder-b:5557"
    )
