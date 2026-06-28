from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.services.savant_supervisor import (  # noqa: E402
    SavantSupervisor,
    SavantSupervisorConfig,
)
import app.services.savant_supervisor as savant_supervisor_module  # noqa: E402


class FakeDockerClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.containers: list[dict[str, Any]] = [
            {"Names": ["/video-analytics-midterm-source-adapter"], "State": "running"},
            {"Names": ["/video-analytics-source-source_lab"], "State": "running"},
            {"Names": ["/video-analytics-midterm-source-adapter-extra"], "State": "running"},
            {"Names": ["/video-analytics-midterm-redis"], "State": "running"},
        ]
        self.inspect_by_name: dict[str, dict[str, Any]] = {
            "video-analytics-midterm-savant": {
                "Id": "savant-container-id",
                "State": {
                    "Status": "running",
                    "StartedAt": "2026-06-11T00:00:00.000000000Z",
                },
            }
        }
        self.exec_output_by_id: dict[str, bytes] = {}
        self.next_exec_id = 1
        self.savant_status = "running\n"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        ok_statuses: set[int] | None = None,
    ) -> tuple[int, bytes]:
        del ok_statuses
        self.calls.append((method, path, body))
        if method == "GET" and path == "/containers/json?all=true":
            return 200, json.dumps(self.containers).encode("utf-8")
        if method == "GET" and path.startswith("/containers/") and path.endswith("/json"):
            name = path.split("/", 3)[2]
            return 200, json.dumps(self.inspect_by_name.get(name, {})).encode("utf-8")
        if method == "POST" and path.startswith("/containers/") and path.endswith("/exec"):
            exec_id = f"exec-{self.next_exec_id}"
            self.next_exec_id += 1
            command = list((body or {}).get("Cmd") or [])
            output = self.savant_status if command == ["cat", "/opt/savant/status.txt"] else ""
            self.exec_output_by_id[exec_id] = output.encode("utf-8")
            return 201, json.dumps({"Id": exec_id}).encode("utf-8")
        if method == "POST" and path.startswith("/exec/") and path.endswith("/start"):
            exec_id = path.split("/", 3)[2]
            return 200, self.exec_output_by_id.get(exec_id, b"")
        if method == "POST" and path.startswith("/containers/") and "/restart" in path:
            return 204, b""
        return 200, b"{}"


class FakeRedis:
    def __init__(self, *, last_generated_id: str = "100000-0", now_s: int = 120) -> None:
        self.last_generated_id = last_generated_id
        self.now_s = now_s

    def xinfo_stream(self, _stream: str) -> dict[str, str]:
        return {"last-generated-id": self.last_generated_id}

    def time(self) -> tuple[int, int]:
        return (self.now_s, 0)


def _config(**overrides: Any) -> SavantSupervisorConfig:
    values = {
        "enabled": True,
        "restart_wait_s": 0.0,
        "restart_cooldown_s": 0.0,
        "stall_seconds": 10.0,
    }
    values.update(overrides)
    return SavantSupervisorConfig(**values)


def test_supervisor_discovers_primary_exact_and_dynamic_prefix_only() -> None:
    fake = FakeDockerClient()
    supervisor = SavantSupervisor(
        config=_config(),
        docker_client=fake,  # type: ignore[arg-type]
        redis_client=FakeRedis(),
    )

    assert supervisor.source_adapter_names() == [
        "video-analytics-midterm-source-adapter",
        "video-analytics-source-source_lab",
    ]


def test_disabled_supervisor_snapshot_does_not_touch_docker() -> None:
    fake = FakeDockerClient()
    supervisor = SavantSupervisor(
        config=_config(enabled=False),
        docker_client=fake,  # type: ignore[arg-type]
        redis_client=FakeRedis(),
    )

    snapshot = supervisor.snapshot()

    assert snapshot["enabled"] is False
    assert snapshot["savant_container"] == "video-analytics-midterm-savant"
    assert fake.calls == []


def test_supervisor_recovery_restarts_savant_then_sources_without_replay() -> None:
    fake = FakeDockerClient()
    supervisor = SavantSupervisor(
        config=_config(restart_replay=False),
        docker_client=fake,  # type: ignore[arg-type]
        redis_client=FakeRedis(),
    )

    result = supervisor.recover("unit_test", now=1000.0, force=True)

    called_paths = [path for method, path, _body in fake.calls if method == "POST"]
    savant_restart = called_paths.index("/containers/video-analytics-midterm-savant/restart?t=30")
    primary_restart = called_paths.index(
        "/containers/video-analytics-midterm-source-adapter/restart?t=15"
    )
    dynamic_restart = called_paths.index(
        "/containers/video-analytics-source-source_lab/restart?t=15"
    )
    assert savant_restart < primary_restart < dynamic_restart
    assert "/containers/video-analytics-midterm-replay-service/restart?t=30" not in called_paths
    assert result["source_adapters"] == [
        "video-analytics-midterm-source-adapter",
        "video-analytics-source-source_lab",
    ]
    assert result["savant_ready"]["ready"] is True


def test_supervisor_run_once_recovers_annotation_stall() -> None:
    fake = FakeDockerClient()
    supervisor = SavantSupervisor(
        config=_config(stall_seconds=10.0),
        docker_client=fake,  # type: ignore[arg-type]
        redis_client=FakeRedis(last_generated_id="100000-0", now_s=120),
    )

    result = supervisor.run_once(now=1000.0)

    assert result["action"] == "recovered"
    assert result["annotation_age_s"] == 20
    assert result["recovery"]["reason"] == "annotation_stall(age=20s)"
    called_paths = [path for method, path, _body in fake.calls if method == "POST"]
    assert "/containers/video-analytics-midterm-savant/restart?t=30" in called_paths
    assert "/containers/video-analytics-midterm-source-adapter/restart?t=15" in called_paths
    assert "/containers/video-analytics-source-source_lab/restart?t=15" in called_paths


def test_supervisor_run_once_converges_unhealthy_sources_without_savant_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sources_path = tmp_path / "sources.generated.yml"
    module_path = tmp_path / "cameras.midterm.yml"
    sources_path.write_text(
        """
sources:
  lab:
    camera_id: lab
    source_id: source_lab
    camera_name: lab
    uri: rtsp://lab/stream
    enabled: true
    adapter_type: gstreamer
    zmq_endpoint: dealer+connect:tcp://replay-service:5555
""".lstrip(),
        encoding="utf-8",
    )
    module_path.write_text("cameras: {}\n", encoding="utf-8")
    fake = FakeDockerClient()
    fake.containers = [
        {"Names": ["/video-analytics-source-stale"], "State": "running"},
    ]
    captured: dict[str, Any] = {}

    def fake_converge(*, cameras):
        captured["cameras"] = cameras
        return {"runtime_action": "source_converge", "dynamic_sources_started": ["source_lab"]}

    monkeypatch.setattr(savant_supervisor_module, "converge_camera_sources", fake_converge)
    supervisor = SavantSupervisor(
        config=_config(
            module_config_path=str(module_path),
            sources_config_path=str(sources_path),
            source_convergence_cooldown_s=0.0,
        ),
        docker_client=fake,  # type: ignore[arg-type]
        redis_client=FakeRedis(last_generated_id="119000-0", now_s=120),
    )

    result = supervisor.run_once(now=1000.0)

    called_paths = [path for method, path, _body in fake.calls if method == "POST"]
    assert result["action"] == "source_converged"
    assert result["source_convergence_repair"]["converged"] is True
    assert captured["cameras"] == [
        {
            "id": "lab",
            "source_id": "source_lab",
            "name": "lab",
            "rtsp_url": "rtsp://lab/stream",
            "enabled": True,
        }
    ]
    assert "/containers/video-analytics-midterm-savant/restart?t=30" not in called_paths
    assert "/containers/video-analytics-midterm-replay-service/restart?t=30" not in called_paths


def test_supervisor_does_not_auto_repair_stopped_dynamic_source_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sources_path = tmp_path / "sources.generated.yml"
    module_path = tmp_path / "cameras.midterm.yml"
    sources_path.write_text(
        """
sources:
  lab:
    camera_id: lab
    source_id: source_lab
    camera_name: lab
    uri: rtsp://lab/stream
    enabled: true
    adapter_type: gstreamer
    zmq_endpoint: dealer+connect:tcp://replay-service:5555
""".lstrip(),
        encoding="utf-8",
    )
    module_path.write_text("cameras: {}\n", encoding="utf-8")
    fake = FakeDockerClient()
    fake.containers = [
        {"Names": ["/video-analytics-source-source_lab"], "State": "exited"},
    ]

    def fail_converge(*, cameras):
        raise AssertionError(f"stopped dynamic source should not auto converge: {cameras}")

    monkeypatch.setattr(savant_supervisor_module, "converge_camera_sources", fail_converge)
    supervisor = SavantSupervisor(
        config=_config(
            module_config_path=str(module_path),
            sources_config_path=str(sources_path),
            source_convergence_cooldown_s=0.0,
            auto_repair_stopped_sources=False,
        ),
        docker_client=fake,  # type: ignore[arg-type]
        redis_client=FakeRedis(last_generated_id="119000-0", now_s=120),
    )

    result = supervisor.run_once(now=1000.0)

    assert result["action"] == "observe"
    convergence = result["source_convergence"]
    assert convergence["healthy"] is False
    assert convergence["stopped_adapters"] == ["video-analytics-source-source_lab"]
    assert convergence["auto_repair_required"] is False


def test_supervisor_can_opt_in_to_auto_repair_stopped_dynamic_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sources_path = tmp_path / "sources.generated.yml"
    module_path = tmp_path / "cameras.midterm.yml"
    sources_path.write_text(
        """
sources:
  lab:
    camera_id: lab
    source_id: source_lab
    camera_name: lab
    uri: rtsp://lab/stream
    enabled: true
    adapter_type: gstreamer
    zmq_endpoint: dealer+connect:tcp://replay-service:5555
""".lstrip(),
        encoding="utf-8",
    )
    module_path.write_text("cameras: {}\n", encoding="utf-8")
    fake = FakeDockerClient()
    fake.containers = [
        {"Names": ["/video-analytics-source-source_lab"], "State": "exited"},
    ]
    captured: dict[str, Any] = {}

    def fake_converge(*, cameras):
        captured["cameras"] = cameras
        return {"runtime_action": "source_converge", "dynamic_sources_started": ["source_lab"]}

    monkeypatch.setattr(savant_supervisor_module, "converge_camera_sources", fake_converge)
    supervisor = SavantSupervisor(
        config=_config(
            module_config_path=str(module_path),
            sources_config_path=str(sources_path),
            source_convergence_cooldown_s=0.0,
            auto_repair_stopped_sources=True,
        ),
        docker_client=fake,  # type: ignore[arg-type]
        redis_client=FakeRedis(last_generated_id="119000-0", now_s=120),
    )

    result = supervisor.run_once(now=1000.0)

    assert result["action"] == "source_converged"
    assert result["source_convergence"]["auto_repair_required"] is True
    assert captured["cameras"][0]["source_id"] == "source_lab"


def test_supervisor_reports_desired_and_missing_dynamic_sources(tmp_path: Path) -> None:
    sources_path = tmp_path / "sources.generated.yml"
    module_path = tmp_path / "cameras.midterm.yml"
    sources_path.write_text(
        """
sources:
  primary:
    camera_id: primary
    source_id: primary_rtsp
    uri: rtsp://primary/stream
    enabled: true
    adapter_type: gstreamer
    zmq_endpoint: dealer+connect:tcp://replay-service:5555
  lab:
    camera_id: lab
    source_id: source_lab
    uri: rtsp://lab/stream
    enabled: true
    adapter_type: gstreamer
    zmq_endpoint: dealer+connect:tcp://replay-service:5555
  disabled:
    camera_id: disabled
    source_id: source_disabled
    uri: rtsp://disabled/stream
    enabled: false
    adapter_type: gstreamer
    zmq_endpoint: dealer+connect:tcp://replay-service:5555
""".lstrip(),
        encoding="utf-8",
    )
    module_path.write_text(
        """
cameras:
  primary:
    name: Primary RTSP Camera
  lab:
    name: lab
  disabled:
    name: Disabled Camera
""".lstrip(),
        encoding="utf-8",
    )
    fake = FakeDockerClient()
    fake.containers = [
        {"Names": ["/video-analytics-midterm-source-adapter"], "State": "running"},
        {"Names": ["/video-analytics-source-source_disabled"], "State": "running"},
    ]
    supervisor = SavantSupervisor(
        config=_config(
            module_config_path=str(module_path),
            sources_config_path=str(sources_path),
        ),
        docker_client=fake,  # type: ignore[arg-type]
        redis_client=FakeRedis(),
    )

    snapshot = supervisor.snapshot()

    desired_lab = next(
        source for source in snapshot["desired_sources"] if source["source_id"] == "source_lab"
    )
    convergence = snapshot["source_convergence"]
    assert desired_lab["camera_name"] == "lab"
    assert desired_lab["expected_container_name"] == "video-analytics-source-source_lab"
    assert "video-analytics-source-source_lab" in convergence["missing_adapters"]
    assert "video-analytics-source-source_disabled" in convergence["stale_adapters"]
    assert convergence["healthy"] is False
