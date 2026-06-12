"""API-owned Savant runtime supervisor.

This replaces the temporary standalone Docker CLI watchdog. The API container
already owns the 8090 management backend and already has Docker socket access,
so the supervisor uses the Docker Engine API directly and checks Redis from
Python.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from redis import Redis

from app.services.runtime_apply import DockerSocketClient, RuntimeApplyError


LOGGER = logging.getLogger(__name__)

DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
DEFAULT_SAVANT_CONTAINER = "video-analytics-midterm-savant"
DEFAULT_REPLAY_CONTAINER = "video-analytics-midterm-replay-service"
DEFAULT_COMPOSE_SOURCE_CONTAINER = "video-analytics-midterm-source-adapter"
DEFAULT_DYNAMIC_SOURCE_PREFIX = "video-analytics-source-"
DEFAULT_STATUS_FILE = "/opt/savant/status.txt"
DEFAULT_ANNOTATION_STREAM = "security.frame_annotations"
DEFAULT_REDIS_URL = "redis://redis:6379/0"


class SavantSupervisorError(RuntimeError):
    pass


@dataclass(frozen=True)
class SavantSupervisorConfig:
    enabled: bool = False
    docker_socket: str = DEFAULT_DOCKER_SOCKET
    redis_url: str = DEFAULT_REDIS_URL
    savant_container: str = DEFAULT_SAVANT_CONTAINER
    replay_container: str = DEFAULT_REPLAY_CONTAINER
    compose_source_container: str = DEFAULT_COMPOSE_SOURCE_CONTAINER
    dynamic_source_prefix: str = DEFAULT_DYNAMIC_SOURCE_PREFIX
    status_file: str = DEFAULT_STATUS_FILE
    annotation_stream: str = DEFAULT_ANNOTATION_STREAM
    poll_interval_s: float = 10.0
    stopped_grace_s: float = 20.0
    startup_grace_s: float = 1800.0
    stall_seconds: float = 120.0
    stall_check_enabled: bool = True
    restart_cooldown_s: float = 300.0
    restart_wait_s: float = 1800.0
    restart_replay: bool = False


def config_from_env() -> SavantSupervisorConfig:
    return SavantSupervisorConfig(
        enabled=_env_bool("SAVANT_SUPERVISOR_ENABLED", default=False),
        docker_socket=os.getenv("SAVANT_SUPERVISOR_DOCKER_SOCKET", DEFAULT_DOCKER_SOCKET),
        redis_url=os.getenv("REDIS_URL", DEFAULT_REDIS_URL),
        savant_container=os.getenv(
            "SAVANT_SUPERVISOR_SAVANT_CONTAINER",
            os.getenv("CAMERA_RUNTIME_SAVANT_CONTAINER", DEFAULT_SAVANT_CONTAINER),
        ),
        replay_container=os.getenv(
            "SAVANT_SUPERVISOR_REPLAY_CONTAINER",
            os.getenv("CAMERA_RUNTIME_REPLAY_CONTAINER", DEFAULT_REPLAY_CONTAINER),
        ),
        compose_source_container=os.getenv(
            "SAVANT_SUPERVISOR_COMPOSE_SOURCE_CONTAINER",
            os.getenv(
                "CAMERA_RUNTIME_COMPOSE_SOURCE_CONTAINER",
                DEFAULT_COMPOSE_SOURCE_CONTAINER,
            ),
        ),
        dynamic_source_prefix=os.getenv(
            "SAVANT_SUPERVISOR_DYNAMIC_SOURCE_PREFIX",
            DEFAULT_DYNAMIC_SOURCE_PREFIX,
        ),
        status_file=os.getenv("SAVANT_SUPERVISOR_STATUS_FILE", DEFAULT_STATUS_FILE),
        annotation_stream=os.getenv(
            "SAVANT_SUPERVISOR_ANNOTATION_STREAM",
            DEFAULT_ANNOTATION_STREAM,
        ),
        poll_interval_s=_env_float("SAVANT_SUPERVISOR_POLL_INTERVAL_S", 10.0),
        stopped_grace_s=_env_float("SAVANT_SUPERVISOR_STOPPED_GRACE_S", 20.0),
        startup_grace_s=_env_float("SAVANT_SUPERVISOR_STARTUP_GRACE_S", 1800.0),
        stall_seconds=_env_float("SAVANT_SUPERVISOR_STALL_SECONDS", 120.0),
        stall_check_enabled=_env_bool("SAVANT_SUPERVISOR_STALL_CHECK_ENABLED", default=True),
        restart_cooldown_s=_env_float("SAVANT_SUPERVISOR_RESTART_COOLDOWN_S", 300.0),
        restart_wait_s=_env_float("SAVANT_SUPERVISOR_RESTART_WAIT_S", 1800.0),
        restart_replay=_env_bool("SAVANT_SUPERVISOR_RESTART_REPLAY", default=False),
    )


class SavantSupervisor:
    def __init__(
        self,
        *,
        config: SavantSupervisorConfig,
        docker_client: DockerSocketClient | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self.config = config
        self.client = docker_client or DockerSocketClient(config.docker_socket)
        self.redis = redis_client or Redis.from_url(config.redis_url)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_restart_at = 0.0
        self._stopped_since: float | None = None
        self._starting_since: float | None = None
        self._boot_marker = self._savant_boot_marker() if config.enabled else ""
        self._last_recovery: dict[str, Any] | None = None
        self._last_status: dict[str, Any] = {}

    def start(self) -> bool:
        if not self.config.enabled:
            LOGGER.info("savant supervisor disabled")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="savant-supervisor",
            daemon=True,
        )
        self._thread.start()
        LOGGER.info("savant supervisor started")
        return True

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout_s)

    def snapshot(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {
                "enabled": False,
                "savant_container": self.config.savant_container,
                "annotation_stream": self.config.annotation_stream,
                "restart_replay": self.config.restart_replay,
                "last_recovery": self._last_recovery,
                "last_loop": self._last_status,
            }
        source_adapters = self.source_adapter_names()
        running_sources = self.source_adapter_names(running_only=True)
        module_status = self.savant_status()
        annotation_age = self.annotation_age_s()
        in_cooldown = (time.time() - self._last_restart_at) < self.config.restart_cooldown_s
        state = {
            "enabled": self.config.enabled,
            "savant_container": self.config.savant_container,
            "savant_container_running": self.container_running(self.config.savant_container),
            "savant_module_status": module_status,
            "source_adapters": source_adapters,
            "running_source_adapters": running_sources,
            "annotation_stream": self.config.annotation_stream,
            "annotation_age_s": annotation_age,
            "stall_check_enabled": self.config.stall_check_enabled,
            "restart_replay": self.config.restart_replay,
            "in_cooldown": in_cooldown,
            "last_recovery": self._last_recovery,
            "last_loop": self._last_status,
        }
        return state

    def run_once(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        status: dict[str, Any] = {"action": "observe", "now": now}

        if not self.container_running(self.config.savant_container):
            self._stopped_since = None
            self._starting_since = None
            status.update(action="wait_container_not_running")
            self._last_status = status
            return status

        marker = self._savant_boot_marker()
        if marker != self._boot_marker:
            self._boot_marker = marker
            self._stopped_since = None
            self._starting_since = None

        in_cooldown = (now - self._last_restart_at) < self.config.restart_cooldown_s
        module_status = self.savant_status()
        status.update(module_status=module_status, in_cooldown=in_cooldown)

        if module_status == "running":
            self._stopped_since = None
            self._starting_since = None
            if (
                self.config.stall_check_enabled
                and not in_cooldown
                and self.source_adapter_names(running_only=True)
            ):
                age = self.annotation_age_s()
                status["annotation_age_s"] = age
                if age is not None and age > self.config.stall_seconds:
                    recovery = self.recover(f"annotation_stall(age={age}s)", now=now)
                    status.update(action="recovered", recovery=recovery)
            self._last_status = status
            return status

        if module_status in {"stopping", "stopped"}:
            self._starting_since = None
            if self._stopped_since is None:
                self._stopped_since = now
            status["stopped_since"] = self._stopped_since
            if not in_cooldown and (now - self._stopped_since) >= self.config.stopped_grace_s:
                recovery = self.recover(f"module_{module_status}", now=now)
                status.update(action="recovered", recovery=recovery)
            self._last_status = status
            return status

        self._stopped_since = None
        if self._starting_since is None:
            self._starting_since = now
        status["starting_since"] = self._starting_since
        if not in_cooldown and (now - self._starting_since) >= self.config.startup_grace_s:
            recovery = self.recover(f"startup_timeout(status={module_status})", now=now)
            status.update(action="recovered", recovery=recovery)
        self._last_status = status
        return status

    def recover(
        self,
        reason: str,
        *,
        now: float | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            if (
                not force
                and (now - self._last_restart_at) < self.config.restart_cooldown_s
            ):
                return {
                    "recovered": False,
                    "reason": reason,
                    "skipped": "cooldown",
                    "last_recovery": self._last_recovery,
                }

            LOGGER.warning("savant supervisor recovery reason=%s", reason)
            restarted: list[str] = []
            self._restart_container(self.config.savant_container, timeout_s=30)
            restarted.append(self.config.savant_container)

            ready = self._wait_for_savant_running()

            if self.config.restart_replay:
                self._restart_container(self.config.replay_container, timeout_s=30)
                restarted.append(self.config.replay_container)

            source_adapters = self.source_adapter_names()
            for name in source_adapters:
                self._restart_container(name, timeout_s=15)
                restarted.append(name)

            self._last_restart_at = now
            self._stopped_since = None
            self._starting_since = None
            self._boot_marker = self._savant_boot_marker()
            self._last_recovery = {
                "recovered": True,
                "reason": reason,
                "at_epoch_s": now,
                "savant_ready": ready,
                "source_adapters": source_adapters,
                "restarted": restarted,
            }
            return dict(self._last_recovery)

    def container_running(self, container_name: str) -> bool:
        state = self._inspect_container(container_name).get("State")
        return isinstance(state, dict) and state.get("Status") == "running"

    def savant_status(self) -> str:
        text = self._docker_exec_text(
            self.config.savant_container,
            ["cat", self.config.status_file],
        ).strip().lower()
        if text in {"initializing", "starting", "running", "stopping", "stopped"}:
            return text
        return "unknown"

    def source_adapter_names(self, *, running_only: bool = False) -> list[str]:
        containers = self._list_containers()
        names: list[str] = []
        for container in containers:
            if running_only and str(container.get("State") or "") != "running":
                continue
            for raw_name in container.get("Names") or []:
                name = str(raw_name).lstrip("/")
                if self._is_source_adapter_name(name) and name not in names:
                    names.append(name)
        return names

    def annotation_age_s(self) -> int | None:
        try:
            info = self.redis.xinfo_stream(self.config.annotation_stream)
            last_id = _redis_mapping_get(info, "last-generated-id")
            last_ms = _stream_id_ms(last_id)
            if last_ms is None:
                return None
            now = self.redis.time()
            now_s = int(now[0] if isinstance(now, (tuple, list)) else now)
            return max(0, now_s - int(last_ms / 1000))
        except Exception as exc:
            LOGGER.warning("failed to inspect annotation stream %s: %s", self.config.annotation_stream, exc)
            return None

    def _run_loop(self) -> None:
        while not self._stop_event.wait(max(0.1, self.config.poll_interval_s)):
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("savant supervisor loop failed")

    def _is_source_adapter_name(self, name: str) -> bool:
        return (
            name == self.config.compose_source_container
            or name.startswith(self.config.dynamic_source_prefix)
        )

    def _restart_container(self, container_name: str, *, timeout_s: int) -> None:
        if not container_name:
            return
        self.client.request(
            "POST",
            f"/containers/{quote(container_name, safe='')}/restart?t={int(timeout_s)}",
            ok_statuses={204, 304, 404},
        )

    def _wait_for_savant_running(self) -> dict[str, Any]:
        started = time.monotonic()
        deadline = started + max(0.0, self.config.restart_wait_s)
        attempts = 0
        last_status = "unknown"
        while True:
            attempts += 1
            if self.container_running(self.config.savant_container):
                last_status = self.savant_status()
                if last_status == "running":
                    return {
                        "ready": True,
                        "status": last_status,
                        "wait_seconds": round(time.monotonic() - started, 3),
                        "attempts": attempts,
                    }
            if time.monotonic() >= deadline:
                return {
                    "ready": False,
                    "status": last_status,
                    "wait_seconds": round(time.monotonic() - started, 3),
                    "attempts": attempts,
                }
            time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))

    def _savant_boot_marker(self) -> str:
        doc = self._inspect_container(self.config.savant_container)
        state = doc.get("State") if isinstance(doc, dict) else {}
        return f"{doc.get('Id', '')}:{state.get('StartedAt', '') if isinstance(state, dict) else ''}"

    def _inspect_container(self, container_name: str) -> dict[str, Any]:
        if not container_name:
            return {}
        try:
            _status, body = self.client.request(
                "GET",
                f"/containers/{quote(container_name, safe='')}/json",
                ok_statuses={200, 404},
            )
            doc = json.loads(body.decode("utf-8") or "{}")
            return doc if isinstance(doc, dict) else {}
        except Exception as exc:
            LOGGER.warning("failed to inspect container %s: %s", container_name, exc)
            return {}

    def _list_containers(self) -> list[dict[str, Any]]:
        try:
            _status, body = self.client.request("GET", "/containers/json?all=true", ok_statuses={200})
            doc = json.loads(body.decode("utf-8") or "[]")
            return doc if isinstance(doc, list) else []
        except Exception as exc:
            LOGGER.warning("failed to list containers: %s", exc)
            return []

    def _docker_exec_text(self, container_name: str, command: list[str]) -> str:
        if not container_name:
            return ""
        try:
            _status, body = self.client.request(
                "POST",
                f"/containers/{quote(container_name, safe='')}/exec",
                body={
                    "AttachStdout": True,
                    "AttachStderr": True,
                    "Tty": True,
                    "Cmd": command,
                },
                ok_statuses={201, 404},
            )
            doc = json.loads(body.decode("utf-8") or "{}")
            exec_id = str(doc.get("Id") or "")
            if not exec_id:
                return ""
            _status, output = self.client.request(
                "POST",
                f"/exec/{quote(exec_id, safe='')}/start",
                body={"Detach": False, "Tty": True},
                ok_statuses={200, 201, 404},
            )
            return output.decode("utf-8", errors="replace")
        except Exception as exc:
            LOGGER.warning("failed to exec in container %s: %s", container_name, exc)
            return ""


_SUPERVISOR: SavantSupervisor | None = None


def start_savant_supervisor() -> bool:
    global _SUPERVISOR
    if _SUPERVISOR is None:
        config = config_from_env()
        _SUPERVISOR = SavantSupervisor(config=config)
    return _SUPERVISOR.start()


def stop_savant_supervisor() -> None:
    if _SUPERVISOR is not None:
        _SUPERVISOR.stop()


def get_savant_supervisor_snapshot() -> dict[str, Any]:
    supervisor = _ensure_supervisor()
    return supervisor.snapshot()


def trigger_savant_recovery(reason: str = "manual_api_recovery") -> dict[str, Any]:
    supervisor = _ensure_supervisor()
    return supervisor.recover(reason, force=True)


def _ensure_supervisor() -> SavantSupervisor:
    global _SUPERVISOR
    if _SUPERVISOR is None:
        _SUPERVISOR = SavantSupervisor(config=config_from_env())
    return _SUPERVISOR


def _redis_mapping_get(mapping: Any, key: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    return mapping.get(key) or mapping.get(key.encode("utf-8"))


def _stream_id_ms(value: Any) -> int | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    first = text.split("-", 1)[0]
    if not first.isdigit():
        return None
    return int(first)


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)
