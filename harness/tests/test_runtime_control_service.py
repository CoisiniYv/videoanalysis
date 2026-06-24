from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.services.runtime_control import (  # noqa: E402
    RuntimeControlConfig,
    start_single_runtime,
    stop_dual_runtime,
    stop_single_runtime,
)


class FakeDockerClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.state_by_name: dict[str, str] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        ok_statuses: set[int] | None = None,
    ) -> tuple[int, bytes]:
        self.calls.append((method, path, body))
        if path.startswith("/containers/"):
            name = unquote(path.split("/containers/", 1)[1].split("/", 1)[0].split("?", 1)[0])
            if method == "POST":
                if path.endswith("/start"):
                    self.state_by_name[name] = "running"
                    return 204, b""
                if "/stop" in path:
                    self.state_by_name[name] = "exited"
                    return 204, b""
                if "/restart" in path:
                    self.state_by_name[name] = "running"
                    return 204, b""
            if method == "GET" and path.endswith("/json"):
                state = self.state_by_name.get(name, "exited")
                payload = {
                    "RestartCount": 0,
                    "State": {
                        "Status": state,
                        "Running": state == "running",
                        "Restarting": state == "restarting",
                    },
                }
                return 200, json.dumps(payload).encode("utf-8")
        return 404, b"{}"


def _config() -> RuntimeControlConfig:
    return RuntimeControlConfig(
        docker_socket="/fake/docker.sock",
        single_start_containers=(
            "phase0-postgres",
            "video-analytics-midterm-redis",
            "video-analytics-midterm-replay-service",
            "video-analytics-midterm-savant",
            "video-analytics-midterm-event-worker",
            "video-analytics-midterm-source-adapter",
        ),
        single_stop_containers=(
            "video-analytics-midterm-source-adapter",
            "video-analytics-midterm-event-worker",
            "video-analytics-midterm-savant",
            "video-analytics-midterm-replay-service",
        ),
        dual_containers=(
            "video-analytics-midterm-replay-a",
            "video-analytics-midterm-replay-b",
            "video-analytics-midterm-savant-a",
            "video-analytics-midterm-savant-b",
        ),
        management_containers=(
            "phase0-postgres",
            "video-analytics-midterm-redis",
            "video-analytics-midterm-api",
            "video-analytics-midterm-evidence-viewer",
        ),
    )


def test_start_single_runtime_starts_database_then_single_chain() -> None:
    fake = FakeDockerClient()

    result = start_single_runtime(config=_config(), docker_client=fake)

    post_paths = [path for method, path, _body in fake.calls if method == "POST"]
    assert post_paths[:6] == [
        "/containers/phase0-postgres/start",
        "/containers/video-analytics-midterm-redis/start",
        "/containers/video-analytics-midterm-replay-service/start",
        "/containers/video-analytics-midterm-savant/start",
        "/containers/video-analytics-midterm-event-worker/start",
        "/containers/video-analytics-midterm-source-adapter/start",
    ]
    assert result["runtime_action"] == "single_start"
    assert result["status"]["management"][2]["name"] == "video-analytics-midterm-api"


def test_stop_single_runtime_preserves_8090_management_plane() -> None:
    fake = FakeDockerClient()

    result = stop_single_runtime(config=_config(), docker_client=fake)

    post_paths = [path for method, path, _body in fake.calls if method == "POST"]
    assert "/containers/video-analytics-midterm-api/stop?t=10" not in post_paths
    assert "/containers/video-analytics-midterm-evidence-viewer/stop?t=10" not in post_paths
    assert "/containers/phase0-postgres/stop?t=10" not in post_paths
    assert "/containers/video-analytics-midterm-source-adapter/stop?t=10" in post_paths
    assert result["runtime_action"] == "single_stop"


def test_stop_dual_runtime_only_stops_dual_extension_containers() -> None:
    fake = FakeDockerClient()

    result = stop_dual_runtime(config=_config(), docker_client=fake)

    post_paths = [path for method, path, _body in fake.calls if method == "POST"]
    assert post_paths == [
        "/containers/video-analytics-midterm-replay-a/stop?t=10",
        "/containers/video-analytics-midterm-replay-b/stop?t=10",
        "/containers/video-analytics-midterm-savant-a/stop?t=10",
        "/containers/video-analytics-midterm-savant-b/stop?t=10",
    ]
    assert result["runtime_action"] == "dual_stop"
