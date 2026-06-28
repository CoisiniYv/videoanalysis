from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import json
import yaml


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.services import runtime_apply  # noqa: E402


class FakeDockerClient:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.containers: list[dict[str, Any]] = []
        self.inspect_by_name: dict[str, dict[str, Any]] = {}
        self.logs_by_name: dict[str, str] = {}

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
        if method == "GET" and path.startswith("/containers/") and path.endswith("/json"):
            name = path.split("/", 3)[2]
            return 200, json.dumps(self.inspect_by_name.get(name, {})).encode("utf-8")
        if method == "GET" and path.startswith("/containers/") and "/logs?" in path:
            name = path.split("/", 3)[2]
            return 200, self.logs_by_name.get(name, "").encode("utf-8")
        if method == "POST" and path.startswith("/containers/create"):
            return 201, b"{}"
        if method == "DELETE":
            return 204, b""
        if method == "POST":
            return 204, b""
        return 200, b"{}"


class FakeRedisClient:
    values: dict[str, str] = {}
    deleted: list[str] = []

    def set(self, key: str, value: str) -> None:
        self.values[str(key)] = str(value)

    def delete(self, *keys: str) -> int:
        self.deleted.extend(str(key) for key in keys)
        return len(keys)


class FakeRedis:
    @classmethod
    def from_url(cls, _url: str) -> FakeRedisClient:
        return FakeRedisClient()


def _allow_runtime_evidence_restart(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_apply,
        "check_runtime_restart_evidence_guard",
        lambda **kwargs: {
            "ok": True,
            "blocked": False,
            "forced": bool(kwargs.get("force")),
            "action": kwargs.get("action", ""),
            "active_count": 0,
            "blocking_states": list(runtime_apply.RUNTIME_RESTART_BLOCKING_EVIDENCE_STATES),
            "tasks": [],
        },
    )


def test_docker_socket_response_parser_decodes_chunked_json() -> None:
    body = b'{"State":{"Status":"running"}}'
    chunked_body = (
        b"a\r\n"
        + body[:10]
        + b"\r\n"
        + f"{len(body[10:]):x}\r\n".encode("ascii")
        + body[10:]
        + b"\r\n0\r\n\r\n"
    )
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        + chunked_body
    )

    status, content = runtime_apply._parse_http_response(response)

    assert status == 200
    assert json.loads(content) == {"State": {"Status": "running"}}


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
    _allow_runtime_evidence_restart(monkeypatch)
    FakeRedisClient.values.clear()
    FakeRedisClient.deleted.clear()
    fake.containers = [{"Names": ["/video-analytics-source-stale"]}]
    fake.inspect_by_name["video-analytics-midterm-savant"] = {
        "State": {"StartedAt": "2026-06-11T00:00:00.000000000Z"}
    }
    fake.logs_by_name["video-analytics-midterm-savant"] = (
        "2026-06-11T00:00:00Z pipeline state changed to PLAYING\n"
    )

    export_doc = {
        "cameras": {
            "primary": {
                "source_id": "primary_rtsp",
                "enabled": True,
                "rules": {
                    "intrusion_full_frame": {
                        "rule_id": "intrusion_full_frame",
                        "algorithm_id": "behavior.intrusion",
                        "rule_type": "intrusion",
                        "enabled": True,
                        "zone_id": "full_frame",
                    },
                    "watchlist_config": {
                        "rule_id": "watchlist_config",
                        "algorithm_id": "face.watchlist",
                        "rule_type": "face.watchlist",
                        "enabled": True,
                    },
                    "running_lobby": {
                        "rule_id": "running_lobby",
                        "algorithm_id": "behavior.running",
                        "rule_type": "running",
                        "enabled": True,
                    },
                },
            },
            "lab": {"source_id": "source_lab", "enabled": True, "rules": {}},
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
    assert result["runtime_epoch"]["runtime_epoch_id"] == result["runtime_epoch_id"]
    assert result["camera_ids"] == ["primary", "lab"]
    assert result["source_ids"] == ["primary_rtsp", "source_lab"]
    assert [rule["rule_id"] for rule in result["applied_rules"]] == [
        "intrusion_full_frame",
        "watchlist_config",
        "running_lobby",
    ]
    assert {
        rule["rule_id"]: rule["runtime_skip_reason"]
        for rule in result["skipped_rules"]
    } == {}
    assert [rule["rule_id"] for rule in result["unsupported_rules"]] == []
    assert {
        rule["rule_id"] for rule in result["enabled_rules"]
    } == {"intrusion_full_frame", "watchlist_config", "running_lobby"}
    primary_apply = next(
        camera for camera in result["applied_cameras"] if camera["camera_id"] == "primary"
    )
    assert primary_apply["applied_rule_ids"] == [
        "intrusion_full_frame",
        "watchlist_config",
        "running_lobby",
    ]
    assert primary_apply["skipped_rule_ids"] == []
    assert primary_apply["unsupported_rule_ids"] == []
    assert result["dynamic_sources_started"] == ["source_lab"]
    assert result["compose_sources_started"] == ["primary_rtsp"]
    assert result["sources_skipped"] == []
    assert result["replay_restarted"] == "video-analytics-midterm-replay-service"
    assert result["forwarder_restarted"] == "video-analytics-midterm-analysis-forwarder"
    assert result["savant_restarted"] == "video-analytics-midterm-savant"
    assert result["savant_ready"] is True
    assert result["savant_ready_reason"].startswith("log:")
    assert result["savant_ready_attempts"] == 1
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
    assert sink_create["NetworkingConfig"]["EndpointsConfig"][
        "video-analytics-midterm_default"
    ]["Aliases"] == ["video-file-sink"]
    env = set(source_create["Env"])
    assert "SOURCE_ID=source_lab" in env
    assert "RTSP_URI=rtsp://lab/stream" in env
    assert "SYNC_OUTPUT=false" in env
    assert "EOS_ON_START=true" in env
    assert "MAX_FPS=8/1" not in env
    assert "MIN_FPS=2/1" not in env
    assert not any(item.startswith("USE_ABSOLUTE_TIMESTAMPS=") for item in env)
    assert source_create["HostConfig"]["NetworkMode"] == "video-analytics-midterm_default"
    assert source_create["HostConfig"]["RestartPolicy"] == {"Name": "no"}
    assert result["source_lifecycle"] == [
        {
            "source_id": "primary_rtsp",
            "camera_id": "primary",
            "adapter_type": "gstreamer",
            "enabled": True,
            "uri_host": "primary",
            "compose_source": True,
            "dynamic_source": False,
            "ffmpeg_timeout_ms": 20000,
            "restart_policy": "no",
            "eos_on_start": True,
            "action": "started",
            "container_name": "video-analytics-midterm-source-adapter",
            "start_status": 204,
        },
        {
            "source_id": "source_lab",
            "camera_id": "lab",
            "adapter_type": "gstreamer",
            "enabled": True,
            "uri_host": "lab",
            "compose_source": False,
            "dynamic_source": True,
            "ffmpeg_timeout_ms": 20000,
            "restart_policy": "no",
            "eos_on_start": True,
            "container_name": "video-analytics-source-source_lab",
            "delete_status": 204,
            "create_status": 201,
            "start_status": 204,
            "action": "created_started",
        },
    ]
    stop_primary_index = next(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST" and path == "/containers/video-analytics-midterm-source-adapter/stop?t=10"
    )
    replay_restart_index = next(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST" and path == "/containers/video-analytics-midterm-replay-service/restart?t=10"
    )
    forwarder_restart_index = next(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST"
        and path == "/containers/video-analytics-midterm-analysis-forwarder/restart?t=10"
    )
    savant_restart_index = next(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST" and path == "/containers/video-analytics-midterm-savant/restart?t=10"
    )
    savant_logs_index = next(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "GET"
        and path.startswith("/containers/video-analytics-midterm-savant/logs?")
        and "&since=" in path
    )
    worker_start_index = next(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST" and path == "/containers/video-analytics-midterm-event-worker/start"
    )
    start_primary_index = next(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST" and path == "/containers/video-analytics-midterm-source-adapter/start"
    )
    source_create_index = max(
        i for i, (method, path, _body) in enumerate(fake.calls)
        if method == "POST" and path.startswith("/containers/create")
    )
    assert stop_primary_index < replay_restart_index < forwarder_restart_index < savant_restart_index
    assert savant_restart_index < savant_logs_index < worker_start_index
    assert worker_start_index < start_primary_index
    assert savant_restart_index < start_primary_index < source_create_index
    assert FakeRedisClient.values["video_analytics:midterm:runtime_epoch"] == result["runtime_epoch_id"]
    assert FakeRedisClient.deleted == ["security.frame_annotations"]
    assert result["redis_frame_cache_streams_reset"] == ["security.frame_annotations"]
    assert result["redis_frame_cache_reset_count"] == 1


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
    _allow_runtime_evidence_restart(monkeypatch)
    fake.inspect_by_name["video-analytics-midterm-savant"] = {
        "State": {"Health": {"Status": "healthy"}}
    }

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


def test_runtime_apply_blocks_before_docker_when_evidence_active(monkeypatch, tmp_path: Path) -> None:
    fake = FakeDockerClient("/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setenv("CAMERA_RUNTIME_MODULE_CONFIG_PATH", str(tmp_path / "cameras.midterm.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", str(tmp_path / "sources.generated.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/fake/docker.sock")
    monkeypatch.setattr(runtime_apply, "DockerSocketClient", lambda socket_path: fake)

    def fake_guard(**_kwargs):
        raise runtime_apply.RuntimeApplyBlockedError(
            "runtime restart blocked because evidence tasks are still active",
            details={
                "ok": False,
                "blocked": True,
                "active_count": 1,
                "tasks": [{"task_id": "task-1", "blocking_state": "replaying"}],
            },
            status_code=409,
        )

    monkeypatch.setattr(runtime_apply, "check_runtime_restart_evidence_guard", fake_guard)

    try:
        runtime_apply.apply_camera_runtime(
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
    except runtime_apply.RuntimeApplyBlockedError as exc:
        assert exc.status_code == 409
        assert exc.details["active_count"] == 1
    else:
        raise AssertionError("runtime apply should block while evidence is active")

    assert fake.calls == []
    assert not (tmp_path / "cameras.midterm.yml").exists()
    assert not (tmp_path / "sources.generated.yml").exists()


def test_evidence_guard_ignores_terminal_materialization_before_clip_pending(
    monkeypatch,
) -> None:
    class FakeCursor:
        sql = ""
        params: dict[str, Any] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql: str, params: dict[str, Any]) -> None:
            self.sql = sql
            self.params = params

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    class FakeConn:
        cursor_obj = FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self) -> FakeCursor:
            return self.cursor_obj

    fake_conn = FakeConn()
    monkeypatch.setattr(runtime_apply.psycopg, "connect", lambda *_args, **_kwargs: fake_conn)

    snapshot = runtime_apply._active_evidence_tasks_snapshot(
        limit=12,
        states=list(runtime_apply.RUNTIME_RESTART_BLOCKING_EVIDENCE_STATES),
        stale_after_s=900,
    )

    assert snapshot["active_count"] == 0
    assert "terminal_evidence_state" in fake_conn.cursor_obj.sql
    assert "WHEN terminal_evidence_state THEN NULL" in fake_conn.cursor_obj.sql
    assert "FROM base" in fake_conn.cursor_obj.sql
    assert "NOT terminal_evidence_state" in fake_conn.cursor_obj.sql
    assert fake_conn.cursor_obj.params["terminal_states"] == list(
        runtime_apply.RUNTIME_RESTART_TERMINAL_EVIDENCE_STATES
    )


def test_runtime_apply_force_records_evidence_guard_and_continues(
    monkeypatch,
    tmp_path: Path,
) -> None:
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
    monkeypatch.setattr(
        runtime_apply,
        "check_runtime_restart_evidence_guard",
        lambda **kwargs: {
            "ok": True,
            "blocked": False,
            "forced": bool(kwargs.get("force")),
            "action": kwargs.get("action", ""),
            "active_count": 1,
            "blocking_states": list(runtime_apply.RUNTIME_RESTART_BLOCKING_EVIDENCE_STATES),
            "tasks": [{"task_id": "task-1", "blocking_state": "replaying"}],
        },
    )
    fake.inspect_by_name["video-analytics-midterm-savant"] = {
        "State": {"Health": {"Status": "healthy"}}
    }

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
        force=True,
    )

    called_paths = [path for _method, path, _body in fake.calls]
    assert result["evidence_restart_guard"]["forced"] is True
    assert result["evidence_restart_guard"]["active_count"] == 1
    assert "/containers/video-analytics-midterm-replay-service/restart?t=10" in called_paths


def test_runtime_apply_fails_before_sources_when_savant_not_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = FakeDockerClient("/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setenv("CAMERA_RUNTIME_MODULE_CONFIG_PATH", str(tmp_path / "cameras.midterm.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", str(tmp_path / "sources.generated.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_COMPOSE_SOURCE_ID", "primary_rtsp")
    monkeypatch.setenv("CAMERA_RUNTIME_SAVANT_READY_TIMEOUT_S", "0")
    monkeypatch.setenv("CAMERA_RUNTIME_SAVANT_READY_POLL_INTERVAL_S", "0")
    monkeypatch.setenv("RUNTIME_EPOCH_ROOT", str(tmp_path / "replay-sink-output" / "midterm"))
    monkeypatch.setenv(
        "RUNTIME_EPOCH_STATE_PATH",
        str(tmp_path / "replay-sink-output" / "midterm" / ".current_epoch.json"),
    )
    monkeypatch.setattr(runtime_apply, "DockerSocketClient", lambda socket_path: fake)
    monkeypatch.setattr(runtime_apply, "Redis", FakeRedis)
    _allow_runtime_evidence_restart(monkeypatch)
    fake.logs_by_name["video-analytics-midterm-savant"] = "still loading models\n"

    try:
        runtime_apply.apply_camera_runtime(
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
    except runtime_apply.RuntimeApplyError as exc:
        assert "savant not ready" in str(exc)
        assert "logs_without_ready_marker" in str(exc)
    else:
        raise AssertionError("runtime apply should fail when Savant never becomes ready")

    called_paths = [path for _method, path, _body in fake.calls]
    assert "/containers/video-analytics-midterm-savant/restart?t=10" in called_paths
    assert "/containers/video-analytics-midterm-event-worker/start" not in called_paths
    assert "/containers/video-analytics-midterm-source-adapter/start" not in called_paths


def test_source_only_converge_recreates_changed_dynamic_and_removes_disabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = FakeDockerClient("/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", str(tmp_path / "sources.generated.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_COMPOSE_SOURCE_ID", "primary_rtsp")
    monkeypatch.setenv("RUNTIME_EPOCH_ROOT", str(tmp_path / "replay-sink-output" / "midterm"))
    monkeypatch.setattr(runtime_apply, "DockerSocketClient", lambda socket_path: fake)
    fake.containers = [
        {"Names": ["/video-analytics-source-source_lab"], "State": "running"},
        {"Names": ["/video-analytics-source-source_disabled"], "State": "running"},
        {"Names": ["/video-analytics-source-stale"], "State": "exited"},
    ]
    fake.inspect_by_name["video-analytics-midterm-savant"] = {
        "State": {"StartedAt": "2026-06-11T00:00:00.000000000Z"}
    }
    fake.logs_by_name["video-analytics-midterm-savant"] = (
        "2026-06-11T00:00:00Z pipeline state changed to PLAYING\n"
    )
    fake.inspect_by_name["video-analytics-source-source_lab"] = {
        "Config": {
            "Env": [
                "SOURCE_ID=source_lab",
                "RTSP_URI=rtsp://old-lab/stream",
                "ZMQ_ENDPOINT=dealer+connect:tcp://replay-service:5555",
                "EOS_ON_START=false",
                "FFMPEG_TIMEOUT_MS=20000",
            ]
        }
    }

    result = runtime_apply.converge_camera_sources(
        cameras=[
            {
                "id": "primary",
                "source_id": "primary_rtsp",
                "name": "Primary",
                "rtsp_url": "rtsp://primary/stream",
                "enabled": True,
            },
            {
                "id": "lab",
                "source_id": "source_lab",
                "name": "lab",
                "rtsp_url": "rtsp://new-lab/stream",
                "enabled": True,
            },
            {
                "id": "disabled",
                "source_id": "source_disabled",
                "name": "Disabled",
                "rtsp_url": "rtsp://disabled/stream",
                "enabled": False,
            },
        ],
    )

    called_paths = [path for _method, path, _body in fake.calls]
    assert result["runtime_action"] == "source_converge"
    assert result["savant_ready"] is True
    assert result["dynamic_sources_started"] == ["source_lab"]
    assert result["dynamic_sources_recreated"] == ["source_lab"]
    assert result["dynamic_sources_stopped"] == [
        "source_disabled",
        "video-analytics-source-stale",
    ]
    assert "/containers/video-analytics-midterm-replay-service/restart?t=10" not in called_paths
    assert "/containers/video-analytics-midterm-savant/restart?t=10" not in called_paths
    assert "/containers/video-analytics-source-source_disabled?force=true" in called_paths
    assert "/containers/video-analytics-source-stale?force=true" in called_paths
    create_calls = [
        body for method, path, body in fake.calls
        if method == "POST" and path.startswith("/containers/create")
    ]
    assert len(create_calls) == 1
    env = set(create_calls[0]["Env"])
    assert "SOURCE_ID=source_lab" in env
    assert "RTSP_URI=rtsp://new-lab/stream" in env
    assert "EOS_ON_START=true" in env
    assert "MAX_FPS=8/1" not in env
    assert create_calls[0]["HostConfig"]["RestartPolicy"] == {"Name": "no"}
    sources_doc = yaml.safe_load((tmp_path / "sources.generated.yml").read_text())
    assert sources_doc["sources"]["lab"]["camera_name"] == "lab"


def test_source_only_converge_stops_disabled_compose_source(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = FakeDockerClient("/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", str(tmp_path / "sources.generated.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_COMPOSE_SOURCE_ID", "primary_rtsp")
    monkeypatch.setattr(runtime_apply, "DockerSocketClient", lambda socket_path: fake)
    fake.inspect_by_name["video-analytics-midterm-source-adapter"] = {
        "State": {"Status": "running"}
    }

    result = runtime_apply.converge_camera_sources(
        cameras=[
            {
                "id": "primary",
                "source_id": "primary_rtsp",
                "name": "Primary",
                "rtsp_url": "rtsp://primary/stream",
                "enabled": False,
            },
        ],
    )

    called_paths = [path for _method, path, _body in fake.calls]
    assert result["runtime_action"] == "source_converge"
    assert result["compose_sources_stopped"] == ["primary_rtsp"]
    assert result["dynamic_sources_stopped"] == []
    assert result["source_lifecycle"] == [
        {
            "source_id": "primary_rtsp",
            "camera_id": "primary",
            "adapter_type": "gstreamer",
            "enabled": False,
            "uri_host": "primary",
            "compose_source": True,
            "dynamic_source": False,
            "ffmpeg_timeout_ms": 20000,
            "restart_policy": "no",
            "eos_on_start": True,
            "camera_name": "Primary",
            "container_name": "video-analytics-midterm-source-adapter",
            "actual_state": "running",
            "actual_present": True,
            "planned_action": "stop_compose_disabled",
            "action": "stopped",
        }
    ]
    assert "/containers/video-analytics-midterm-source-adapter/stop?t=10" in called_paths
    sources_doc = yaml.safe_load((tmp_path / "sources.generated.yml").read_text())
    assert sources_doc["sources"]["primary"]["enabled"] is False


def test_source_only_sync_writes_module_config_and_preserves_epoch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = FakeDockerClient("/fake/docker.sock")
    module_path = tmp_path / "cameras.midterm.yml"
    module_path.write_text(
        "runtime_epoch_id: midterm-existing\n"
        "cameras:\n"
        "  primary:\n"
        "    enabled: true\n"
        "    source_id: primary_rtsp\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setenv("CAMERA_RUNTIME_MODULE_CONFIG_PATH", str(module_path))
    monkeypatch.setenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", str(tmp_path / "sources.generated.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_COMPOSE_SOURCE_ID", "primary_rtsp")
    monkeypatch.setattr(runtime_apply, "DockerSocketClient", lambda socket_path: fake)
    fake.inspect_by_name["video-analytics-midterm-source-adapter"] = {
        "State": {"Status": "running"}
    }

    export_doc = {
        "cameras": {
            "primary": {
                "enabled": False,
                "source_id": "primary_rtsp",
                "name": "Primary",
                "rtsp_url": "rtsp://primary/stream",
            }
        }
    }
    result = runtime_apply.sync_camera_runtime_config_and_sources(
        export_doc=export_doc,
        cameras=[
            {
                "id": "primary",
                "source_id": "primary_rtsp",
                "name": "Primary",
                "rtsp_url": "rtsp://primary/stream",
                "enabled": False,
            }
        ],
    )

    module_doc = yaml.safe_load(module_path.read_text(encoding="utf-8"))
    assert result["module_config_synced"] is True
    assert result["runtime_epoch_id_preserved"] == "midterm-existing"
    assert module_doc["runtime_epoch_id"] == "midterm-existing"
    assert module_doc["cameras"]["primary"]["runtime_epoch_id"] == "midterm-existing"
    assert module_doc["cameras"]["primary"]["enabled"] is False


def test_source_only_converge_starts_enabled_compose_source(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = FakeDockerClient("/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", str(tmp_path / "sources.generated.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_COMPOSE_SOURCE_ID", "primary_rtsp")
    monkeypatch.setattr(runtime_apply, "DockerSocketClient", lambda socket_path: fake)
    fake.inspect_by_name["video-analytics-midterm-source-adapter"] = {
        "State": {"Status": "exited"}
    }
    fake.inspect_by_name["video-analytics-midterm-savant"] = {
        "State": {"StartedAt": "2026-06-11T00:00:00.000000000Z"}
    }
    fake.logs_by_name["video-analytics-midterm-savant"] = (
        "2026-06-11T00:00:00Z pipeline state changed to PLAYING\n"
    )

    result = runtime_apply.converge_camera_sources(
        cameras=[
            {
                "id": "primary",
                "source_id": "primary_rtsp",
                "name": "Primary",
                "rtsp_url": "rtsp://primary/stream",
                "enabled": True,
            },
        ],
    )

    called_paths = [path for _method, path, _body in fake.calls]
    assert result["compose_sources_started"] == ["primary_rtsp"]
    assert result["dynamic_sources_started"] == []
    assert "/containers/video-analytics-midterm-source-adapter/start" in called_paths


def test_source_only_converge_fails_before_starting_sources_when_savant_not_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = FakeDockerClient("/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", str(tmp_path / "sources.generated.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_COMPOSE_SOURCE_ID", "primary_rtsp")
    monkeypatch.setenv("CAMERA_RUNTIME_SAVANT_READY_TIMEOUT_S", "0")
    monkeypatch.setenv("CAMERA_RUNTIME_SAVANT_READY_POLL_INTERVAL_S", "0")
    monkeypatch.setenv("RUNTIME_EPOCH_ROOT", str(tmp_path / "replay-sink-output" / "midterm"))
    monkeypatch.setattr(runtime_apply, "DockerSocketClient", lambda socket_path: fake)
    fake.logs_by_name["video-analytics-midterm-savant"] = "still loading models\n"

    try:
        runtime_apply.converge_camera_sources(
            cameras=[
                {
                    "id": "lab",
                    "source_id": "source_lab",
                    "name": "lab",
                    "rtsp_url": "rtsp://lab/stream",
                    "enabled": True,
                }
            ],
        )
    except runtime_apply.RuntimeApplyError as exc:
        assert "savant not ready for source convergence" in str(exc)
        assert "logs_without_ready_marker" in str(exc)
    else:
        raise AssertionError("source-only convergence should fail when Savant is not ready")

    called_paths = [path for _method, path, _body in fake.calls]
    assert not any(path.startswith("/containers/create") for path in called_paths)
    assert "/containers/video-analytics-source-source_lab/start" not in called_paths


def test_runtime_apply_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CAMERA_RUNTIME_APPLY_ENABLED", raising=False)
    try:
        runtime_apply.apply_camera_runtime(export_doc={"cameras": {}}, cameras=[])
    except runtime_apply.RuntimeApplyError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("runtime apply should be disabled by default")


def test_source_only_converge_rejects_unsafe_source_id_before_docker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = FakeDockerClient("/fake/docker.sock")
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", str(tmp_path / "sources.generated.yml"))
    monkeypatch.setenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/fake/docker.sock")
    monkeypatch.setattr(runtime_apply, "DockerSocketClient", lambda socket_path: fake)

    try:
        runtime_apply.converge_camera_sources(
            cameras=[
                {
                    "id": "bad",
                    "source_id": "bad/source",
                    "rtsp_url": "rtsp://bad/stream",
                    "enabled": True,
                }
            ],
        )
    except runtime_apply.RuntimeApplyError as exc:
        assert "unsafe source_id" in str(exc)
    else:
        raise AssertionError("unsafe source_id should be rejected before Docker calls")

    assert fake.calls == []
