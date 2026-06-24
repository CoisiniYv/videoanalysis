"""Runtime container start/stop controls for the 8090 operator portal."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from app.services.runtime_apply import DockerSocketClient, RuntimeApplyError


DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
DEFAULT_DATABASE_CONTAINER = "phase0-postgres"
DEFAULT_REDIS_CONTAINER = "video-analytics-midterm-redis"

DEFAULT_SINGLE_START_CONTAINERS = (
    DEFAULT_DATABASE_CONTAINER,
    DEFAULT_REDIS_CONTAINER,
    "video-analytics-midterm-replay-service",
    "video-analytics-midterm-video-file-sink",
    "video-analytics-midterm-analysis-forwarder",
    "video-analytics-midterm-savant",
    "video-analytics-midterm-event-worker",
    "video-analytics-midterm-face-worker",
    "video-analytics-midterm-clip-worker",
    "video-analytics-midterm-media-worker",
    "video-analytics-midterm-source-adapter",
)
DEFAULT_SINGLE_STOP_CONTAINERS = (
    "video-analytics-midterm-source-adapter",
    "video-analytics-midterm-event-worker",
    "video-analytics-midterm-face-worker",
    "video-analytics-midterm-clip-worker",
    "video-analytics-midterm-media-worker",
    "video-analytics-midterm-savant",
    "video-analytics-midterm-analysis-forwarder",
    "video-analytics-midterm-replay-service",
    "video-analytics-midterm-video-file-sink",
)
DEFAULT_DUAL_CONTAINERS = (
    "video-analytics-midterm-analysis-forwarder-a",
    "video-analytics-midterm-analysis-forwarder-b",
    "video-analytics-midterm-replay-a",
    "video-analytics-midterm-replay-b",
    "video-analytics-midterm-savant-a",
    "video-analytics-midterm-savant-b",
    "video-analytics-midterm-video-file-sink-a",
    "video-analytics-midterm-video-file-sink-b",
)


class RuntimeControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeControlConfig:
    docker_socket: str
    single_start_containers: tuple[str, ...]
    single_stop_containers: tuple[str, ...]
    dual_containers: tuple[str, ...]
    management_containers: tuple[str, ...]


def config_from_env() -> RuntimeControlConfig:
    database_container = os.getenv("RUNTIME_CONTROL_DATABASE_CONTAINER", DEFAULT_DATABASE_CONTAINER)
    redis_container = os.getenv("RUNTIME_CONTROL_REDIS_CONTAINER", DEFAULT_REDIS_CONTAINER)
    return RuntimeControlConfig(
        docker_socket=os.getenv(
            "RUNTIME_CONTROL_DOCKER_SOCKET",
            os.getenv("CAMERA_RUNTIME_DOCKER_SOCKET", DEFAULT_DOCKER_SOCKET),
        ),
        single_start_containers=_env_list(
            "RUNTIME_CONTROL_SINGLE_START_CONTAINERS",
            DEFAULT_SINGLE_START_CONTAINERS,
        ),
        single_stop_containers=_env_list(
            "RUNTIME_CONTROL_SINGLE_STOP_CONTAINERS",
            DEFAULT_SINGLE_STOP_CONTAINERS,
        ),
        dual_containers=_env_list("RUNTIME_CONTROL_DUAL_CONTAINERS", DEFAULT_DUAL_CONTAINERS),
        management_containers=_dedupe(
            (
                database_container,
                redis_container,
                os.getenv("RUNTIME_CONTROL_API_CONTAINER", "video-analytics-midterm-api"),
                os.getenv(
                    "RUNTIME_CONTROL_VIEWER_CONTAINER",
                    "video-analytics-midterm-evidence-viewer",
                ),
            )
        ),
    )


def runtime_control_status(
    *,
    config: RuntimeControlConfig | None = None,
    docker_client: DockerSocketClient | None = None,
) -> dict[str, Any]:
    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    single_names = _dedupe(cfg.single_stop_containers)
    return {
        "mode": "single",
        "single": _inspect_group(client, single_names),
        "dual": _inspect_group(client, cfg.dual_containers),
        "management": _inspect_group(client, cfg.management_containers),
    }


def start_single_runtime(
    *,
    config: RuntimeControlConfig | None = None,
    docker_client: DockerSocketClient | None = None,
) -> dict[str, Any]:
    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    actions = [_container_action(client, name, "start") for name in cfg.single_start_containers]
    return {
        "runtime_action": "single_start",
        "actions": actions,
        "status": runtime_control_status(config=cfg, docker_client=client),
    }


def stop_single_runtime(
    *,
    config: RuntimeControlConfig | None = None,
    docker_client: DockerSocketClient | None = None,
) -> dict[str, Any]:
    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    actions = [_container_action(client, name, "stop") for name in cfg.single_stop_containers]
    return {
        "runtime_action": "single_stop",
        "actions": actions,
        "status": runtime_control_status(config=cfg, docker_client=client),
    }


def restart_single_runtime(
    *,
    config: RuntimeControlConfig | None = None,
    docker_client: DockerSocketClient | None = None,
) -> dict[str, Any]:
    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    actions = [_container_action(client, name, "restart") for name in cfg.single_stop_containers]
    return {
        "runtime_action": "single_restart",
        "actions": actions,
        "status": runtime_control_status(config=cfg, docker_client=client),
    }


def stop_dual_runtime(
    *,
    config: RuntimeControlConfig | None = None,
    docker_client: DockerSocketClient | None = None,
) -> dict[str, Any]:
    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    actions = [_container_action(client, name, "stop") for name in cfg.dual_containers]
    return {
        "runtime_action": "dual_stop",
        "actions": actions,
        "status": runtime_control_status(config=cfg, docker_client=client),
    }


def _container_action(client: DockerSocketClient, container_name: str, action: str) -> dict[str, Any]:
    name = str(container_name or "").strip()
    if not name:
        return {"container": name, "action": action, "ok": False, "error": "empty container name"}
    if action == "start":
        path = f"/containers/{quote(name, safe='')}/start"
        ok_statuses = {204, 304, 404}
    elif action == "stop":
        path = f"/containers/{quote(name, safe='')}/stop?t=10"
        ok_statuses = {204, 304, 404}
    elif action == "restart":
        path = f"/containers/{quote(name, safe='')}/restart?t=10"
        ok_statuses = {204, 304, 404}
    else:
        raise RuntimeControlError(f"unsupported runtime container action: {action}")
    try:
        status, body = client.request("POST", path, ok_statuses=ok_statuses)
    except RuntimeApplyError as exc:
        raise RuntimeControlError(str(exc)) from exc
    result: dict[str, Any] = {
        "container": name,
        "action": action,
        "status": status,
        "ok": status in {204, 304},
    }
    if status == 404:
        result["missing"] = True
    if body:
        result["message"] = body.decode("utf-8", errors="replace")[:500]
    result["state"] = _inspect_container(client, name)
    return result


def _inspect_group(client: DockerSocketClient, names: tuple[str, ...]) -> list[dict[str, Any]]:
    return [_inspect_container(client, name) for name in names]


def _inspect_container(client: DockerSocketClient, container_name: str) -> dict[str, Any]:
    name = str(container_name or "").strip()
    if not name:
        return {"name": name, "present": False, "state": "missing"}
    try:
        status, body = client.request(
            "GET",
            f"/containers/{quote(name, safe='')}/json",
            ok_statuses={200, 404},
        )
    except RuntimeApplyError as exc:
        raise RuntimeControlError(str(exc)) from exc
    if status == 404:
        return {"name": name, "present": False, "state": "missing"}
    try:
        doc = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeControlError(f"invalid docker inspect payload for {name}: {exc}") from exc
    state = doc.get("State") if isinstance(doc, dict) else {}
    health = state.get("Health") if isinstance(state, dict) else {}
    return {
        "name": name,
        "present": True,
        "state": str(state.get("Status") or "unknown") if isinstance(state, dict) else "unknown",
        "running": bool(state.get("Running")) if isinstance(state, dict) else False,
        "restarting": bool(state.get("Restarting")) if isinstance(state, dict) else False,
        "health": str(health.get("Status") or "") if isinstance(health, dict) else "",
        "restart_count": int(doc.get("RestartCount") or 0) if isinstance(doc, dict) else 0,
    }


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return _dedupe(default)
    return _dedupe(tuple(item.strip() for item in raw.split(",") if item.strip()))


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return tuple(result)
