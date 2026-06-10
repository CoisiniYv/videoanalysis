from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import json
import yaml


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.services import runtime_apply  # noqa: E402


class FakeDockerClient:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.containers: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        ok_statuses: set[int] | None = None,
    ) -> tuple[int, bytes]:
        self.calls.append((method, path, body))
        if method == "GET" and path == "/containers/json?all=true":
            return 200, json.dumps(self.containers).encode("utf-8")
        if method == "POST" and path.startswith("/containers/create"):
            return 201, b"{}"
        if method == "DELETE":
            return 204, b""
        if method == "POST":
            return 204, b""
        return 200, b"{}"


class FakeRedisClient:
    values: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.values[str(key)] = str(value)


class FakeRedis:
    @classmethod
    def from_url(cls, _url: str) -> FakeRedisClient:
        return FakeRedisClient()


def test_runtime_apply_writes_configs_and_recreates_dynamic_rtsp(monkeypatch, tmp_path: Path) -> None:
    fake = FakeDockerClient("/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setenv("CAMERA_RUNTIME_MODULE_CONFIG_PATH", str(tmp_path / "cameras.midterm.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", str(tmp_path / "sources.generated.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_COMPOSE_SOURCE_ID", "primary_rtsp")
    monkeypatch.setenv("RUNTIME_EPOCH_ROOT", str(tmp_path / "replay-sink-output" / "midterm"))
    monkeypatch.setenv(
        "RUNTIME_EPOCH_STATE_PATH",
        str(tmp_path / "replay-sink-output" / "midterm" / ".current_epoch.json"),
    )
    monkeypatch.setattr(runtime_apply, "DockerSocketClient", lambda socket_path: fake)
    monkeypatch.setattr(runtime_apply, "Redis", FakeRedis)
    FakeRedisClient.values.clear()
    fake.containers = [{"Names": ["/video-analytics-source-stale"]}]

    export_doc = {
        "cameras": {
            "primary": {"source_id": "primary_rtsp", "enabled": True},
            "lab": {"source_id": "source_lab", "enabled": True},
        }
    }
    cameras = [
        {
            "id": "primary",
            "source_id": "primary_rtsp",
            "rtsp_url": "rtsp://primary/stream",
            "enabled": True,
        },
        {
            "id": "lab",
            "source_id": "source_lab",
            "rtsp_url": "rtsp://lab/stream",
            "enabled": True,
        },
    ]

    result = runtime_apply.apply_camera_runtime(export_doc=export_doc, cameras=cameras)

    assert result["runtime_action"] == "apply"
    assert result["dynamic_sources_started"] == ["source_lab"]
    assert result["compose_sources_started"] == ["primary_rtsp"]
    assert result["sources_skipped"] == []
    assert result["replay_restarted"] == "video-analytics-midterm-replay-service"
    assert result["savant_restarted"] == "video-analytics-midterm-savant"
    assert result["source_containers_stopped"] == [
        "video-analytics-midterm-source-adapter",
        "video-analytics-source-source_lab",
        "video-analytics-source-stale",
    ]
    assert "video-analytics-midterm-api" in result["management_containers_preserved"]
    assert "video-analytics-midterm-evidence-viewer" in result["management_containers_preserved"]
    written_export = yaml.safe_load((tmp_path / "cameras.midterm.yml").read_text())
    assert written_export["runtime_epoch_id"] == result["runtime_epoch_id"]
    assert written_export["cameras"]["primary"]["runtime_epoch_id"] == result["runtime_epoch_id"]
    assert written_export["cameras"]["lab"]["runtime_epoch_id"] == result["runtime_epoch_id"]
    sources_doc = yaml.safe_load((tmp_path / "sources.generated.yml").read_text())
    assert sources_doc["sources"]["lab"]["zmq_endpoint"] == (
        "dealer+connect:tcp://replay-service:5555"
    )

    create_calls = [
        body for method, path, body in fake.calls
        if method == "POST" and path.startswith("/containers/create")
    ]
    assert len(create_calls) == 2
    source_create = create_calls[-1]
    sink_create = create_calls[0]
    sink_env = set(sink_create["Env"])
    assert any(
        item == (
            "DIR_LOCATION=/media/replay-sink-output/midterm/epochs/"
            f"{result['runtime_epoch_id']}/%source_id%/%src_filename%/"
        )
        for item in sink_env
    )
    env = set(source_create["Env"])
    assert "SOURCE_ID=source_lab" in env
    assert "RTSP_URI=rtsp://lab/stream" in env
    assert "EOS_ON_START=false" in env
    assert not any(item.startswith("USE_ABSOLUTE_TIMESTAMPS=") for item in env)
    assert source_create["HostConfig"]["NetworkMode"] == "video-analytics-midterm_default"
    stop_primary_index = next(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST" and path == "/containers/video-analytics-midterm-source-adapter/stop?t=10"
    )
    replay_restart_index = next(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST" and path == "/containers/video-analytics-midterm-replay-service/restart?t=10"
    )
    savant_restart_index = next(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST" and path == "/containers/video-analytics-midterm-savant/restart?t=10"
    )
    start_primary_index = next(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST" and path == "/containers/video-analytics-midterm-source-adapter/start"
    )
    source_create_index = max(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST" and path.startswith("/containers/create")
    )
    assert stop_primary_index < replay_restart_index < savant_restart_index
    assert savant_restart_index < start_primary_index < source_create_index
    assert FakeRedisClient.values["video_analytics:midterm:runtime_epoch"] == result["runtime_epoch_id"]


def test_runtime_restart_uses_same_controlled_surface(monkeypatch, tmp_path: Path) -> None:
    fake = FakeDockerClient("/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setenv("CAMERA_RUNTIME_MODULE_CONFIG_PATH", str(tmp_path / "cameras.midterm.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", str(tmp_path / "sources.generated.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_COMPOSE_SOURCE_ID", "primary_rtsp")
    monkeypatch.setenv("RUNTIME_EPOCH_ROOT", str(tmp_path / "replay-sink-output" / "midterm"))
    monkeypatch.setenv(
        "RUNTIME_EPOCH_STATE_PATH",
        str(tmp_path / "replay-sink-output" / "midterm" / ".current_epoch.json"),
    )
    monkeypatch.setattr(runtime_apply, "DockerSocketClient", lambda socket_path: fake)
    monkeypatch.setattr(runtime_apply, "Redis", FakeRedis)

    result = runtime_apply.restart_camera_runtime(
        export_doc={"cameras": {"primary": {"source_id": "primary_rtsp", "enabled": True}}},
        cameras=[
            {
                "id": "primary",
                "source_id": "primary_rtsp",
                "rtsp_url": "rtsp://primary/stream",
                "enabled": True,
            }
        ],
    )

    called_paths = [path for _method, path, _body in fake.calls]
    assert result["runtime_action"] == "restart"
    assert "/containers/video-analytics-midterm-api/restart?t=10" not in called_paths
    assert "/containers/video-analytics-midterm-evidence-viewer/restart?t=10" not in called_paths
    assert "/containers/video-analytics-midterm-replay-service/restart?t=10" in called_paths
    assert "/containers/video-analytics-midterm-savant/restart?t=10" in called_paths


def test_runtime_apply_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CAMERA_RUNTIME_APPLY_ENABLED", raising=False)
    try:
        runtime_apply.apply_camera_runtime(export_doc={"cameras": {}}, cameras=[])
    except runtime_apply.RuntimeApplyError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("runtime apply should be disabled by default")
