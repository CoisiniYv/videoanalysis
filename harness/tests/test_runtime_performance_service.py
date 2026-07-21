from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


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

from app.services import runtime_performance  # noqa: E402
from app.services.runtime_performance import RuntimePerformanceConfig  # noqa: E402


class FakeDockerClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.inspect_by_name: dict[str, dict[str, Any]] = {}

    def add_container(
        self,
        name: str,
        *,
        env: dict[str, str],
        running: bool = True,
        image: str = "image:latest",
    ) -> None:
        self.inspect_by_name[name] = {
            "Config": {
                "Image": image,
                "Env": [f"{key}={value}" for key, value in env.items()],
                "Labels": {"com.docker.compose.service": name},
                "ExposedPorts": {"8080/tcp": {}},
            },
            "HostConfig": {
                "NetworkMode": "video-analytics-midterm_default",
                "RestartPolicy": {"Name": "unless-stopped"},
            },
            "NetworkSettings": {
                "Networks": {
                    "video-analytics-midterm_default": {
                        "Aliases": [name],
                    }
                }
            },
            "State": {
                "Running": running,
                "Status": "running" if running else "exited",
                "StartedAt": "2026-06-27T00:00:00.000000000Z",
            },
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        ok_statuses: set[int] | None = None,
    ) -> tuple[int, bytes]:
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/containers/") and path.endswith("/json"):
            name = _container_name(path)
            if name not in self.inspect_by_name:
                return 404, b"{}"
            return 200, json.dumps(self.inspect_by_name[name]).encode("utf-8")
        if method == "POST" and path.startswith("/containers/") and "/stop" in path:
            name = _container_name(path)
            if name in self.inspect_by_name:
                self.inspect_by_name[name]["State"]["Running"] = False
                self.inspect_by_name[name]["State"]["Status"] = "exited"
            return 204, b""
        if method == "POST" and path.startswith("/containers/") and "/start" in path:
            name = _container_name(path)
            if name in self.inspect_by_name:
                self.inspect_by_name[name]["State"]["Running"] = True
                self.inspect_by_name[name]["State"]["Status"] = "running"
            return 204, b""
        if method == "POST" and path.startswith("/containers/") and "/rename" in path:
            name = _container_name(path)
            new_name = parse_qs(urlparse(path).query)["name"][0]
            self.inspect_by_name[new_name] = self.inspect_by_name.pop(name)
            return 204, b""
        if method == "POST" and path.startswith("/containers/create"):
            name = parse_qs(urlparse(path).query)["name"][0]
            self.inspect_by_name[name] = _inspect_from_create_body(name, body or {})
            return 201, b"{}"
        if method == "DELETE" and path.startswith("/containers/"):
            name = _container_name(path)
            self.inspect_by_name.pop(name, None)
            return 204, b""
        if method == "GET" and path.startswith("/containers/") and "/logs?" in path:
            return 200, b"pipeline state changed to PLAYING\n"
        return 404, b"{}"


def _container_name(path: str) -> str:
    return unquote(path.split("/containers/", 1)[1].split("/", 1)[0].split("?", 1)[0])


def _inspect_from_create_body(name: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "Config": {
            "Image": body.get("Image", "image:latest"),
            "Env": list(body.get("Env") or []),
            "Labels": dict(body.get("Labels") or {}),
            "ExposedPorts": dict(body.get("ExposedPorts") or {}),
        },
        "HostConfig": dict(body.get("HostConfig") or {}),
        "NetworkSettings": {
            "Networks": {
                network: {"Aliases": list((endpoint or {}).get("Aliases") or [name])}
                for network, endpoint in (
                    (body.get("NetworkingConfig") or {}).get("EndpointsConfig") or {}
                ).items()
            }
        },
        "State": {"Running": False, "Status": "created", "StartedAt": ""},
    }


def _config(tmp_path: Path) -> RuntimePerformanceConfig:
    return RuntimePerformanceConfig(
        config_path=tmp_path / "performance_config.json",
        docker_socket="/fake/docker.sock",
        forwarder_container="video-analytics-midterm-analysis-forwarder",
        savant_container="video-analytics-midterm-savant",
        savant_ready_timeout_s=0.0,
        savant_ready_poll_interval_s=0.0,
    )


def _fake_runtime() -> FakeDockerClient:
    fake = FakeDockerClient()
    fake.add_container(
        "video-analytics-midterm-analysis-forwarder",
        env={
            "ANALYSIS_FPS": "8/1",
            "ANALYSIS_MIN_FPS": "2/1",
            "FORWARDER_SAMPLER_ENABLED": "true",
            "FORWARDER_QUEUE_MAX_SIZE": "2048",
            "FORWARDER_SEND_TIMEOUT_MS": "2000",
            "FORWARDER_SEND_RETRIES": "3",
            "FORWARDER_SEND_HWM": "1000",
            "FORWARDER_OUT_ENDPOINT": "dealer+connect:tcp://savant-security:5557",
        },
    )
    fake.add_container(
        "video-analytics-midterm-savant",
        env={
            "INGRESS_FPS_GATE_ENABLED": "true",
            "MAX_FPS": "8/1",
            "MIN_FPS": "2/1",
            "POSE_INFER_INTERVAL": "1",
            "FACE_INFER_INTERVAL": "7",
            "FACE_EMBEDDING_INFER_INTERVAL": "7",
            "SAVANT_REDIS_EXPORTER_SOCKET_TIMEOUT_MS": "500",
            "SAVANT_REDIS_EXPORTER_CONNECT_TIMEOUT_MS": "500",
            "SAVANT_REDIS_EXPORTER_QUEUE_MAXSIZE": "8192",
            "SAVANT_REDIS_EXPORTER_WRITE_RETRIES": "10",
            "SAVANT_REDIS_EXPORTER_RETRY_SLEEP_MS": "20",
            "FRAME_ANNOTATION_WRITE_TIMEOUT_MS": "500",
            "FRAME_ANNOTATION_REDIS_QUEUE_MAXSIZE": "8192",
            "BATCHED_PUSH_TIMEOUT": "40000",
            "ZMQ_SRC_ENDPOINT": "router+bind:tcp://0.0.0.0:5557",
        },
    )
    return fake


def test_recreate_container_can_override_cpuset_without_losing_host_config() -> None:
    fake = _fake_runtime()

    result = runtime_performance._recreate_container_with_env(
        fake,
        "video-analytics-midterm-analysis-forwarder",
        {"ANALYSIS_FPS": "4/1"},
        force_start=True,
        host_config_updates={"CpusetCpus": "6,14"},
    )

    host_config = fake.inspect_by_name[
        "video-analytics-midterm-analysis-forwarder"
    ]["HostConfig"]
    assert host_config["NetworkMode"] == "video-analytics-midterm_default"
    assert host_config["RestartPolicy"] == {"Name": "unless-stopped"}
    assert host_config["CpusetCpus"] == "6,14"
    assert result["host_config_updates"] == ["CpusetCpus"]


def test_performance_config_uses_runtime_env_without_saved_file(tmp_path: Path) -> None:
    fake = _fake_runtime()
    fake.inspect_by_name["video-analytics-midterm-analysis-forwarder"]["Config"]["Env"][0] = (
        "ANALYSIS_FPS=10/1"
    )

    result = runtime_performance.get_runtime_performance_config(
        config=_config(tmp_path),
        docker_client=fake,
    )

    assert result["source"] == "runtime"
    assert result["saved_config"]["analysis_fps"] == "10/1"
    assert result["saved_config"]["forwarder_queue_max_size"] == 2048
    assert result["saved_config"]["forwarder_send_timeout_ms"] == 2000
    assert result["saved_config"]["forwarder_send_retries"] == 3
    assert result["saved_config"]["forwarder_send_hwm"] == 1000
    assert result["saved_config"]["savant_max_fps"] == "8/1"
    assert result["saved_config"]["savant_batch_size"] == 4
    assert result["saved_config"]["pose_batch_size"] == 4
    assert result["saved_config"]["face_detector_batch_size"] == 4
    assert result["saved_config"]["face_embedding_batch_size"] == 16
    assert result["saved_config"]["max_parallel_streams"] == 64
    assert result["defaults"]["savant_batch_size"] == 4
    assert result["defaults"]["pose_batch_size"] == 4
    assert result["defaults"]["face_detector_batch_size"] == 4
    assert result["defaults"]["max_parallel_streams"] == 64
    assert result["saved_config"]["frame_annotation_write_timeout_ms"] == 500
    assert result["saved_config"]["frame_annotation_redis_queue_maxsize"] == 8192
    assert result["saved_config"]["savant_redis_write_retries"] == 10
    assert result["saved_config"]["savant_redis_retry_sleep_ms"] == 20
    assert result["restart_required"] is False


def test_save_performance_config_validates_and_reports_pending(tmp_path: Path) -> None:
    fake = _fake_runtime()

    result = runtime_performance.save_runtime_performance_config(
        {
            **runtime_performance.DEFAULT_CONFIG,
            "analysis_fps": "6/1",
            "face_infer_interval": 3,
        },
        config=_config(tmp_path),
        docker_client=fake,
    )

    assert result["source"] == "file"
    assert result["saved_config"]["analysis_fps"] == "6/1"
    assert result["saved_config"]["face_infer_interval"] == 3
    assert result["restart_required"] is True
    saved = json.loads((tmp_path / "performance_config.json").read_text())
    assert saved["config"]["analysis_fps"] == "6/1"

    try:
        runtime_performance.save_runtime_performance_config(
            {"unknown": 1},
            config=_config(tmp_path),
            docker_client=fake,
        )
    except runtime_performance.RuntimePerformanceError as exc:
        assert exc.status_code == 400
        assert exc.details["unknown_fields"] == ["unknown"]
    else:
        raise AssertionError("unknown performance fields should be rejected")


def test_apply_performance_config_recreates_only_forwarder_and_savant(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = _fake_runtime()
    fake.add_container(
        "video-analytics-source-source_lab",
        env={
            "SOURCE_ID": "source_lab",
            "RTSP_URI": "rtsp://lab/stream",
            "ZMQ_ENDPOINT": "dealer+connect:tcp://replay-service:5555",
            "EOS_ON_START": "false",
        },
    )
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")
    monkeypatch.setattr(
        runtime_performance,
        "_check_evidence_restart_guard",
        lambda **kwargs: {"ok": True, "blocked": False, "action": kwargs["action"]},
    )
    monkeypatch.setattr(
        runtime_performance,
        "_wait_for_savant_ready",
        lambda *_args, **_kwargs: {
            "savant_ready": True,
            "savant_ready_reason": "test",
            "savant_ready_wait_seconds": 0.0,
            "savant_ready_attempts": 1,
        },
    )
    cfg = _config(tmp_path)
    runtime_performance.save_runtime_performance_config(
        {
            **runtime_performance.DEFAULT_CONFIG,
            "analysis_fps": "5/1",
            "forwarder_sampler_enabled": False,
            "forwarder_queue_max_size": 4096,
            "forwarder_send_timeout_ms": 1500,
            "savant_max_fps": "5/1",
            "savant_batch_size": 4,
            "pose_batch_size": 4,
            "face_detector_batch_size": 4,
            "face_embedding_batch_size": 16,
            "max_parallel_streams": 16,
            "face_embedding_infer_interval": 4,
            "frame_annotation_write_timeout_ms": 750,
        },
        config=cfg,
        docker_client=fake,
    )

    result = runtime_performance.apply_runtime_performance_config(
        config=cfg,
        docker_client=fake,
    )

    assert result["runtime_action"] == "performance_config_apply"
    assert result["changed"] is True
    assert result["pending_targets"] == ["forwarder", "savant"]
    recreated = [item["container"] for item in result["actions"] if item["action"] == "recreated"]
    assert recreated == [
        "video-analytics-midterm-savant",
        "video-analytics-midterm-analysis-forwarder",
    ]
    all_paths = "\n".join(path for _method, path, _body in fake.calls)
    assert "video-analytics-midterm-event-worker" not in all_paths
    assert "video-analytics-midterm-replay-service" not in all_paths
    assert "video-analytics-midterm-video-file-sink" not in all_paths
    assert "video-analytics-source-source_lab" not in all_paths
    create_bodies = {
        parse_qs(urlparse(path).query)["name"][0]: body
        for method, path, body in fake.calls
        if method == "POST" and path.startswith("/containers/create")
    }
    forwarder_env = "\n".join(create_bodies["video-analytics-midterm-analysis-forwarder"]["Env"])
    savant_env = "\n".join(create_bodies["video-analytics-midterm-savant"]["Env"])
    assert "ANALYSIS_FPS=5/1" in forwarder_env
    assert "FORWARDER_SAMPLER_ENABLED=false" in forwarder_env
    assert "FORWARDER_QUEUE_MAX_SIZE=4096" in forwarder_env
    assert "FORWARDER_SEND_TIMEOUT_MS=1500" in forwarder_env
    assert "FORWARDER_SEND_RETRIES=3" in forwarder_env
    assert "FORWARDER_SEND_HWM=1000" in forwarder_env
    assert "MAX_FPS=5/1" in savant_env
    assert "BATCH_SIZE=4" in savant_env
    assert "POSE_BATCH_SIZE=4" in savant_env
    assert "FACE_DETECTOR_BATCH_SIZE=4" in savant_env
    assert "FACE_EMBEDDING_BATCH_SIZE=16" in savant_env
    assert "MAX_PARALLEL_STREAMS=16" in savant_env
    assert "FACE_EMBEDDING_INFER_INTERVAL=4" in savant_env
    assert "SAVANT_REDIS_EXPORTER_SOCKET_TIMEOUT_MS=500" in savant_env
    assert "SAVANT_REDIS_EXPORTER_QUEUE_MAXSIZE=8192" in savant_env
    assert "SAVANT_REDIS_EXPORTER_WRITE_RETRIES=10" in savant_env
    assert "SAVANT_REDIS_EXPORTER_RETRY_SLEEP_MS=20" in savant_env
    assert "FRAME_ANNOTATION_WRITE_TIMEOUT_MS=750" in savant_env
    assert "FRAME_ANNOTATION_REDIS_QUEUE_MAXSIZE=8192" in savant_env
    assert result["status"]["restart_required"] is False


def test_apply_performance_config_blocks_before_container_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = _fake_runtime()
    monkeypatch.setenv("CAMERA_RUNTIME_APPLY_ENABLED", "true")

    def fake_guard(**_kwargs):
        raise runtime_performance.RuntimePerformanceBlockedError(
            "runtime restart blocked because evidence tasks are still active",
            status_code=409,
            details={"blocked": True, "active_count": 1},
        )

    monkeypatch.setattr(runtime_performance, "_check_evidence_restart_guard", fake_guard)
    cfg = _config(tmp_path)
    runtime_performance.save_runtime_performance_config(
        {**runtime_performance.DEFAULT_CONFIG, "analysis_fps": "4/1"},
        config=cfg,
        docker_client=fake,
    )
    fake.calls.clear()

    try:
        runtime_performance.apply_runtime_performance_config(config=cfg, docker_client=fake)
    except runtime_performance.RuntimePerformanceBlockedError as exc:
        assert exc.status_code == 409
        assert exc.details["active_count"] == 1
    else:
        raise AssertionError("performance apply should be blocked by evidence guard")

    assert all(method == "GET" for method, _path, _body in fake.calls)
