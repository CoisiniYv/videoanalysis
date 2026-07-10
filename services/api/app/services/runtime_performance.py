"""Runtime performance controls for the 8090 operator portal."""

from __future__ import annotations

import copy
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.services.runtime_apply import (
    DockerSocketClient,
    RuntimeApplyBlockedError,
    RuntimeApplyError,
    check_runtime_restart_evidence_guard,
    _wait_for_savant_ready,
)


DEFAULT_CONFIG_PATH = "/data/video-analytics/media/.runtime/performance_config.json"
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
DEFAULT_FORWARDER_CONTAINER = "video-analytics-midterm-analysis-forwarder"
DEFAULT_SAVANT_CONTAINER = "video-analytics-midterm-savant"
DEFAULT_SAVANT_READY_TIMEOUT_S = 300.0
DEFAULT_SAVANT_READY_POLL_INTERVAL_S = 2.0
FPS_RE = re.compile(r"^\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?$")
CONFIG_VERSION = 1


class RuntimePerformanceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 503,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


class RuntimePerformanceBlockedError(RuntimePerformanceError):
    pass


@dataclass(frozen=True)
class PerformanceField:
    key: str
    env: str
    target: str
    kind: str
    default: Any
    label: str
    min_value: int | None = None
    max_value: int | None = None


FIELDS: tuple[PerformanceField, ...] = (
    PerformanceField(
        key="forwarder_sampler_enabled",
        env="FORWARDER_SAMPLER_ENABLED",
        target="forwarder",
        kind="bool",
        default=True,
        label="Forwarder 采样",
    ),
    PerformanceField(
        key="analysis_fps",
        env="ANALYSIS_FPS",
        target="forwarder",
        kind="fps",
        default="8/1",
        label="Forwarder 最大 FPS",
    ),
    PerformanceField(
        key="analysis_min_fps",
        env="ANALYSIS_MIN_FPS",
        target="forwarder",
        kind="fps",
        default="2/1",
        label="Forwarder 最小 FPS",
    ),
    PerformanceField(
        key="forwarder_queue_max_size",
        env="FORWARDER_QUEUE_MAX_SIZE",
        target="forwarder",
        kind="int",
        default=2048,
        label="Forwarder 队列上限",
        min_value=1,
        max_value=20000,
    ),
    PerformanceField(
        key="forwarder_send_timeout_ms",
        env="FORWARDER_SEND_TIMEOUT_MS",
        target="forwarder",
        kind="int",
        default=2000,
        label="Forwarder 发送超时 ms",
        min_value=1,
        max_value=30000,
    ),
    PerformanceField(
        key="forwarder_send_retries",
        env="FORWARDER_SEND_RETRIES",
        target="forwarder",
        kind="int",
        default=3,
        label="Forwarder 发送重试",
        min_value=0,
        max_value=20,
    ),
    PerformanceField(
        key="forwarder_send_hwm",
        env="FORWARDER_SEND_HWM",
        target="forwarder",
        kind="int",
        default=1000,
        label="Forwarder 发送 HWM",
        min_value=1,
        max_value=20000,
    ),
    PerformanceField(
        key="ingress_fps_gate_enabled",
        env="INGRESS_FPS_GATE_ENABLED",
        target="savant",
        kind="bool",
        default=True,
        label="Savant 入流限速",
    ),
    PerformanceField(
        key="savant_max_fps",
        env="MAX_FPS",
        target="savant",
        kind="fps",
        default="8/1",
        label="Savant 最大 FPS",
    ),
    PerformanceField(
        key="savant_min_fps",
        env="MIN_FPS",
        target="savant",
        kind="fps",
        default="2/1",
        label="Savant 最小 FPS",
    ),
    PerformanceField(
        key="savant_batch_size",
        env="BATCH_SIZE",
        target="savant",
        kind="int",
        default=4,
        label="Savant pipeline batch",
        min_value=1,
        max_value=128,
    ),
    PerformanceField(
        key="pose_batch_size",
        env="POSE_BATCH_SIZE",
        target="savant",
        kind="int",
        default=4,
        label="姿态 batch",
        min_value=1,
        max_value=128,
    ),
    PerformanceField(
        key="face_detector_batch_size",
        env="FACE_DETECTOR_BATCH_SIZE",
        target="savant",
        kind="int",
        default=4,
        label="人脸检测 batch",
        min_value=1,
        max_value=128,
    ),
    PerformanceField(
        key="face_embedding_batch_size",
        env="FACE_EMBEDDING_BATCH_SIZE",
        target="savant",
        kind="int",
        default=16,
        label="AdaFace batch",
        min_value=1,
        max_value=128,
    ),
    PerformanceField(
        key="max_parallel_streams",
        env="MAX_PARALLEL_STREAMS",
        target="savant",
        kind="int",
        default=64,
        label="Savant 并行流上限",
        min_value=1,
        max_value=256,
    ),
    PerformanceField(
        key="pose_infer_interval",
        env="POSE_INFER_INTERVAL",
        target="savant",
        kind="int",
        default=1,
        label="姿态 interval",
        min_value=0,
        max_value=30,
    ),
    PerformanceField(
        key="face_infer_interval",
        env="FACE_INFER_INTERVAL",
        target="savant",
        kind="int",
        default=7,
        label="人脸 interval",
        min_value=0,
        max_value=30,
    ),
    PerformanceField(
        key="face_embedding_infer_interval",
        env="FACE_EMBEDDING_INFER_INTERVAL",
        target="savant",
        kind="int",
        default=7,
        label="AdaFace interval",
        min_value=0,
        max_value=30,
    ),
    PerformanceField(
        key="savant_redis_socket_timeout_ms",
        env="SAVANT_REDIS_EXPORTER_SOCKET_TIMEOUT_MS",
        target="savant",
        kind="int",
        default=500,
        label="Savant Redis 超时 ms",
        min_value=1,
        max_value=30000,
    ),
    PerformanceField(
        key="savant_redis_connect_timeout_ms",
        env="SAVANT_REDIS_EXPORTER_CONNECT_TIMEOUT_MS",
        target="savant",
        kind="int",
        default=500,
        label="Savant Redis 连接超时 ms",
        min_value=1,
        max_value=30000,
    ),
    PerformanceField(
        key="savant_redis_queue_maxsize",
        env="SAVANT_REDIS_EXPORTER_QUEUE_MAXSIZE",
        target="savant",
        kind="int",
        default=8192,
        label="Savant Redis 队列上限",
        min_value=1,
        max_value=100000,
    ),
    PerformanceField(
        key="savant_redis_write_retries",
        env="SAVANT_REDIS_EXPORTER_WRITE_RETRIES",
        target="savant",
        kind="int",
        default=10,
        label="Savant Redis 写入重试",
        min_value=0,
        max_value=100,
    ),
    PerformanceField(
        key="savant_redis_retry_sleep_ms",
        env="SAVANT_REDIS_EXPORTER_RETRY_SLEEP_MS",
        target="savant",
        kind="int",
        default=20,
        label="Savant Redis 重试间隔 ms",
        min_value=0,
        max_value=5000,
    ),
    PerformanceField(
        key="frame_annotation_write_timeout_ms",
        env="FRAME_ANNOTATION_WRITE_TIMEOUT_MS",
        target="savant",
        kind="int",
        default=500,
        label="Frame annotation 写入超时 ms",
        min_value=1,
        max_value=30000,
    ),
    PerformanceField(
        key="frame_annotation_redis_queue_maxsize",
        env="FRAME_ANNOTATION_REDIS_QUEUE_MAXSIZE",
        target="savant",
        kind="int",
        default=8192,
        label="Frame annotation Redis 队列上限",
        min_value=1,
        max_value=100000,
    ),
    PerformanceField(
        key="batched_push_timeout",
        env="BATCHED_PUSH_TIMEOUT",
        target="savant",
        kind="int",
        default=40000,
        label="Batched push timeout",
        min_value=0,
        max_value=1_000_000,
    ),
)
FIELDS_BY_KEY = {field.key: field for field in FIELDS}
DEFAULT_CONFIG = {field.key: field.default for field in FIELDS}


@dataclass(frozen=True)
class RuntimePerformanceConfig:
    config_path: Path
    docker_socket: str
    forwarder_container: str
    savant_container: str
    savant_ready_timeout_s: float
    savant_ready_poll_interval_s: float


def config_from_env() -> RuntimePerformanceConfig:
    return RuntimePerformanceConfig(
        config_path=Path(os.getenv("RUNTIME_PERFORMANCE_CONFIG_PATH", DEFAULT_CONFIG_PATH)),
        docker_socket=os.getenv(
            "RUNTIME_PERFORMANCE_DOCKER_SOCKET",
            os.getenv(
                "RUNTIME_CONTROL_DOCKER_SOCKET",
                os.getenv("CAMERA_RUNTIME_DOCKER_SOCKET", DEFAULT_DOCKER_SOCKET),
            ),
        ),
        forwarder_container=os.getenv(
            "RUNTIME_PERFORMANCE_FORWARDER_CONTAINER",
            os.getenv("CAMERA_RUNTIME_FORWARDER_CONTAINER", DEFAULT_FORWARDER_CONTAINER),
        ),
        savant_container=os.getenv(
            "RUNTIME_PERFORMANCE_SAVANT_CONTAINER",
            os.getenv("CAMERA_RUNTIME_SAVANT_CONTAINER", DEFAULT_SAVANT_CONTAINER),
        ),
        savant_ready_timeout_s=_env_float(
            "RUNTIME_PERFORMANCE_SAVANT_READY_TIMEOUT_S",
            _env_float("CAMERA_RUNTIME_SAVANT_READY_TIMEOUT_S", DEFAULT_SAVANT_READY_TIMEOUT_S),
        ),
        savant_ready_poll_interval_s=_env_float(
            "RUNTIME_PERFORMANCE_SAVANT_READY_POLL_INTERVAL_S",
            _env_float(
                "CAMERA_RUNTIME_SAVANT_READY_POLL_INTERVAL_S",
                DEFAULT_SAVANT_READY_POLL_INTERVAL_S,
            ),
        ),
    )


def get_runtime_performance_config(
    *,
    config: RuntimePerformanceConfig | None = None,
    docker_client: DockerSocketClient | None = None,
) -> dict[str, Any]:
    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    runtime = _runtime_snapshot(cfg, client)
    saved_doc = _read_saved_doc(cfg.config_path)
    saved_config = saved_doc.get("config") if isinstance(saved_doc.get("config"), dict) else None
    source = "file" if saved_config is not None else "runtime"
    if saved_config is None:
        saved_config = _runtime_config_from_snapshot(runtime)
    if not saved_config:
        saved_config = dict(DEFAULT_CONFIG)
        source = "default"
    normalized = _normalize_config(saved_config, partial=False)
    return _summary(
        cfg,
        runtime=runtime,
        saved_doc=saved_doc,
        saved_config=normalized,
        source=source,
    )


def save_runtime_performance_config(
    payload: dict[str, Any],
    *,
    config: RuntimePerformanceConfig | None = None,
    docker_client: DockerSocketClient | None = None,
) -> dict[str, Any]:
    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    normalized = _normalize_config(payload, partial=False)
    doc = {
        "version": CONFIG_VERSION,
        "updated_at": _now_text(),
        "config": normalized,
    }
    _write_saved_doc(cfg.config_path, doc)
    runtime = _runtime_snapshot(cfg, client)
    return _summary(
        cfg,
        runtime=runtime,
        saved_doc=doc,
        saved_config=normalized,
        source="file",
    )


def apply_runtime_performance_config(
    *,
    force: bool = False,
    config: RuntimePerformanceConfig | None = None,
    docker_client: DockerSocketClient | None = None,
) -> dict[str, Any]:
    if not _runtime_performance_apply_enabled():
        raise RuntimePerformanceError("runtime performance apply is disabled", status_code=403)

    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    status_before = get_runtime_performance_config(config=cfg, docker_client=client)
    saved_config = status_before["saved_config"]
    pending_targets = {
        str(item["target"])
        for item in status_before["diff"]
        if item.get("pending") and item.get("runtime_value") is not None
    }
    if not pending_targets:
        return {
            "runtime_action": "performance_config_apply",
            "changed": False,
            "pending_targets": [],
            "actions": [],
            "evidence_restart_guard": None,
            "status": status_before,
        }

    evidence_guard = _check_evidence_restart_guard(
        action="runtime_performance_apply",
        force=force,
    )
    env_updates = _env_updates_for_config(saved_config)
    actions: list[dict[str, Any]] = []
    if "savant" in pending_targets and "forwarder" in pending_targets:
        try:
            _stop_container(client, cfg.forwarder_container)
            actions.append(
                _recreate_container_with_env(
                    client,
                    cfg.savant_container,
                    env_updates["savant"],
                )
            )
            actions.append(
                _wait_savant_ready_action(
                    client,
                    cfg.savant_container,
                    timeout_s=cfg.savant_ready_timeout_s,
                    poll_interval_s=cfg.savant_ready_poll_interval_s,
                )
            )
            actions.append(
                _recreate_container_with_env(
                    client,
                    cfg.forwarder_container,
                    env_updates["forwarder"],
                    force_start=True,
                )
            )
        except RuntimePerformanceError:
            raise
        except RuntimeApplyError as exc:
            raise RuntimePerformanceError(str(exc), status_code=503) from exc
    else:
        try:
            if "savant" in pending_targets:
                actions.append(
                    _recreate_container_with_env(
                        client,
                        cfg.savant_container,
                        env_updates["savant"],
                    )
                )
                actions.append(
                    _wait_savant_ready_action(
                        client,
                        cfg.savant_container,
                        timeout_s=cfg.savant_ready_timeout_s,
                        poll_interval_s=cfg.savant_ready_poll_interval_s,
                    )
                )
            if "forwarder" in pending_targets:
                actions.append(
                    _recreate_container_with_env(
                        client,
                        cfg.forwarder_container,
                        env_updates["forwarder"],
                    )
                )
        except RuntimePerformanceError:
            raise
        except RuntimeApplyError as exc:
            raise RuntimePerformanceError(str(exc), status_code=503) from exc

    status_after = get_runtime_performance_config(config=cfg, docker_client=client)
    return {
        "runtime_action": "performance_config_apply",
        "changed": True,
        "pending_targets": sorted(pending_targets),
        "actions": actions,
        "evidence_restart_guard": evidence_guard,
        "status": status_after,
    }


def _summary(
    cfg: RuntimePerformanceConfig,
    *,
    runtime: dict[str, Any],
    saved_doc: dict[str, Any],
    saved_config: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    diff = _diff(saved_config, runtime)
    return {
        "config_path": str(cfg.config_path),
        "source": source,
        "version": saved_doc.get("version") if saved_doc else None,
        "updated_at": saved_doc.get("updated_at") if saved_doc else "",
        "saved_config": saved_config,
        "defaults": dict(DEFAULT_CONFIG),
        "fields": [_field_doc(field) for field in FIELDS],
        "runtime": runtime,
        "diff": diff,
        "restart_required": any(item["pending"] for item in diff),
        "targets": {
            "forwarder": cfg.forwarder_container,
            "savant": cfg.savant_container,
        },
    }


def _field_doc(field: PerformanceField) -> dict[str, Any]:
    doc = {
        "key": field.key,
        "env": field.env,
        "target": field.target,
        "kind": field.kind,
        "label": field.label,
        "default": field.default,
    }
    if field.min_value is not None:
        doc["min"] = field.min_value
    if field.max_value is not None:
        doc["max"] = field.max_value
    return doc


def _runtime_snapshot(
    cfg: RuntimePerformanceConfig,
    client: DockerSocketClient,
) -> dict[str, Any]:
    return {
        "forwarder": _container_runtime_snapshot(
            client,
            cfg.forwarder_container,
            target="forwarder",
        ),
        "savant": _container_runtime_snapshot(
            client,
            cfg.savant_container,
            target="savant",
        ),
    }


def _container_runtime_snapshot(
    client: DockerSocketClient,
    container_name: str,
    *,
    target: str,
) -> dict[str, Any]:
    try:
        status, body = client.request(
            "GET",
            f"/containers/{quote(container_name, safe='')}/json",
            ok_statuses={200, 404},
        )
    except Exception as exc:
        return {
            "container": container_name,
            "target": target,
            "present": False,
            "running": False,
            "state": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "env": {},
            "config": {},
        }
    if status == 404:
        return {
            "container": container_name,
            "target": target,
            "present": False,
            "running": False,
            "state": "missing",
            "env": {},
            "config": {},
        }
    try:
        inspect_doc = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        return {
            "container": container_name,
            "target": target,
            "present": False,
            "running": False,
            "state": "invalid",
            "error": str(exc),
            "env": {},
            "config": {},
        }
    env = _container_env_map(inspect_doc)
    state = inspect_doc.get("State") if isinstance(inspect_doc, dict) else {}
    selected_env = {field.env: env.get(field.env) for field in FIELDS if field.target == target}
    return {
        "container": container_name,
        "target": target,
        "present": True,
        "running": bool(state.get("Running")) if isinstance(state, dict) else False,
        "state": str(state.get("Status") or "unknown") if isinstance(state, dict) else "unknown",
        "env": selected_env,
        "config": _config_from_env_map(env, target=target),
    }


def _runtime_config_from_snapshot(runtime: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for target in ("forwarder", "savant"):
        target_config = runtime.get(target, {}).get("config")
        if isinstance(target_config, dict):
            config.update({key: value for key, value in target_config.items() if value is not None})
    return config


def _config_from_env_map(env: dict[str, str], *, target: str) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for field in FIELDS:
        if field.target != target:
            continue
        value = env.get(field.env)
        if value is None:
            continue
        config[field.key] = _normalize_field(field, value)
    return config


def _diff(saved_config: dict[str, Any], runtime: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for field in FIELDS:
        runtime_value = runtime.get(field.target, {}).get("config", {}).get(field.key)
        saved_value = saved_config.get(field.key, field.default)
        rows.append(
            {
                "key": field.key,
                "label": field.label,
                "env": field.env,
                "target": field.target,
                "saved_value": saved_value,
                "runtime_value": runtime_value,
                "pending": runtime_value is not None
                and _comparison_value(field, saved_value) != _comparison_value(field, runtime_value),
            }
        )
    return rows


def _env_updates_for_config(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    updates = {"forwarder": {}, "savant": {}}
    for field in FIELDS:
        updates[field.target][field.env] = _env_value(field, config[field.key])
    return updates


def _normalize_config(payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimePerformanceError("performance config body must be an object", status_code=400)
    config = dict(DEFAULT_CONFIG)
    unknown = sorted(set(payload) - set(FIELDS_BY_KEY))
    if unknown:
        raise RuntimePerformanceError(
            "unknown performance config fields",
            status_code=400,
            details={"unknown_fields": unknown},
        )
    for key, value in payload.items():
        config[key] = _normalize_field(FIELDS_BY_KEY[key], value)
    if partial:
        return {key: config[key] for key in payload}
    return config


def _normalize_field(field: PerformanceField, value: Any) -> Any:
    if field.kind == "bool":
        return _normalize_bool(value, field=field)
    if field.kind == "int":
        return _normalize_int(value, field=field)
    if field.kind == "fps":
        return _normalize_fps(value, field=field)
    raise RuntimePerformanceError(f"unsupported performance field kind: {field.kind}")


def _normalize_bool(value: Any, *, field: PerformanceField) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise RuntimePerformanceError(
        f"{field.key} must be a boolean",
        status_code=400,
        details={"field": field.key},
    )


def _normalize_int(value: Any, *, field: PerformanceField) -> int:
    if isinstance(value, bool):
        raise RuntimePerformanceError(
            f"{field.key} must be an integer",
            status_code=400,
            details={"field": field.key},
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimePerformanceError(
            f"{field.key} must be an integer",
            status_code=400,
            details={"field": field.key},
        ) from exc
    if field.min_value is not None and parsed < field.min_value:
        raise RuntimePerformanceError(
            f"{field.key} must be >= {field.min_value}",
            status_code=400,
            details={"field": field.key, "min": field.min_value},
        )
    if field.max_value is not None and parsed > field.max_value:
        raise RuntimePerformanceError(
            f"{field.key} must be <= {field.max_value}",
            status_code=400,
            details={"field": field.key, "max": field.max_value},
        )
    return parsed


def _normalize_fps(value: Any, *, field: PerformanceField) -> str:
    text = str(value).strip()
    if not text or not FPS_RE.match(text):
        raise RuntimePerformanceError(
            f"{field.key} must be an FPS value like 8/1 or 8",
            status_code=400,
            details={"field": field.key},
        )
    if "/" in text:
        _num, denom = text.split("/", 1)
        if float(denom) <= 0:
            raise RuntimePerformanceError(
                f"{field.key} FPS denominator must be > 0",
                status_code=400,
                details={"field": field.key},
            )
    elif float(text) < 0:
        raise RuntimePerformanceError(
            f"{field.key} FPS must be >= 0",
            status_code=400,
            details={"field": field.key},
        )
    return text


def _comparison_value(field: PerformanceField, value: Any) -> Any:
    if value is None:
        return None
    normalized = _normalize_field(field, value)
    if field.kind == "bool":
        return bool(normalized)
    if field.kind == "int":
        return int(normalized)
    return str(normalized)


def _env_value(field: PerformanceField, value: Any) -> str:
    normalized = _normalize_field(field, value)
    if field.kind == "bool":
        return "true" if normalized else "false"
    return str(normalized)


def _read_saved_doc(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimePerformanceError(
            f"failed to parse performance config: {exc}",
            status_code=500,
        ) from exc
    if not isinstance(data, dict):
        raise RuntimePerformanceError("performance config file must contain an object")
    return data


def _write_saved_doc(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _container_env_map(inspect_doc: dict[str, Any]) -> dict[str, str]:
    config = inspect_doc.get("Config") if isinstance(inspect_doc, dict) else {}
    values = config.get("Env") if isinstance(config, dict) else []
    env: dict[str, str] = {}
    for item in values or []:
        key, sep, value = str(item).partition("=")
        if sep:
            env[key] = value
    return env


def _recreate_container_with_env(
    client: DockerSocketClient,
    container_name: str,
    env_updates: dict[str, str],
    *,
    force_start: bool = False,
    restart_policy_name: str | None = None,
) -> dict[str, Any]:
    inspect_doc = _inspect_required(client, container_name)
    state = inspect_doc.get("State") if isinstance(inspect_doc, dict) else {}
    was_running = bool(state.get("Running")) if isinstance(state, dict) else False
    should_start = force_start or was_running
    backup_name = f"{container_name}.perf-backup-{int(time.time())}"
    create_body = _create_body_from_inspect(
        inspect_doc,
        env_updates,
        restart_policy_name=restart_policy_name,
    )
    encoded = quote(container_name, safe="")
    backup_encoded = quote(backup_name, safe="")
    renamed = False
    created = False
    try:
        _stop_container(client, container_name)
        client.request(
            "DELETE",
            f"/containers/{backup_encoded}?force=true",
            ok_statuses={204, 404},
        )
        client.request(
            "POST",
            f"/containers/{encoded}/rename?name={quote(backup_name, safe='')}",
            ok_statuses={204},
        )
        renamed = True
        create_status, _ = client.request(
            "POST",
            f"/containers/create?name={encoded}",
            body=create_body,
            ok_statuses={201},
        )
        created = True
        start_status = None
        if should_start:
            start_status, _ = client.request(
                "POST",
                f"/containers/{encoded}/start",
                ok_statuses={204, 304},
            )
        client.request(
            "DELETE",
            f"/containers/{backup_encoded}?force=true",
            ok_statuses={204, 404},
        )
    except Exception:
        if created:
            try:
                client.request(
                    "DELETE",
                    f"/containers/{encoded}?force=true",
                    ok_statuses={204, 404},
                )
            except Exception:
                pass
        if renamed:
            try:
                client.request(
                    "POST",
                    f"/containers/{backup_encoded}/rename?name={quote(container_name, safe='')}",
                    ok_statuses={204},
                )
                if should_start:
                    client.request(
                        "POST",
                        f"/containers/{encoded}/start",
                        ok_statuses={204, 304},
                    )
            except Exception:
                pass
        raise
    return {
        "container": container_name,
        "target_env": sorted(env_updates),
        "action": "recreated",
        "was_running": was_running,
        "start_requested": should_start,
        "create_status": create_status,
        "start_status": start_status,
        "restart_policy": restart_policy_name,
    }


def _inspect_required(client: DockerSocketClient, container_name: str) -> dict[str, Any]:
    status, body = client.request(
        "GET",
        f"/containers/{quote(container_name, safe='')}/json",
        ok_statuses={200, 404},
    )
    if status == 404:
        raise RuntimePerformanceError(
            f"runtime container not found: {container_name}",
            status_code=404,
            details={"container": container_name},
        )
    try:
        doc = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimePerformanceError(
            f"invalid docker inspect payload for {container_name}: {exc}",
            status_code=503,
        ) from exc
    if not isinstance(doc, dict):
        raise RuntimePerformanceError(
            f"invalid docker inspect payload for {container_name}",
            status_code=503,
        )
    return doc


def _create_body_from_inspect(
    inspect_doc: dict[str, Any],
    env_updates: dict[str, str],
    *,
    restart_policy_name: str | None = None,
) -> dict[str, Any]:
    config = inspect_doc.get("Config") if isinstance(inspect_doc, dict) else {}
    if not isinstance(config, dict):
        raise RuntimePerformanceError("docker inspect payload missing Config")
    create_config_keys = (
        "Image",
        "Cmd",
        "Entrypoint",
        "Env",
        "Labels",
        "ExposedPorts",
        "WorkingDir",
        "User",
        "Tty",
        "OpenStdin",
        "AttachStdin",
        "AttachStdout",
        "AttachStderr",
        "StopSignal",
        "StopTimeout",
        "Healthcheck",
        "Shell",
    )
    body = {
        key: copy.deepcopy(config[key])
        for key in create_config_keys
        if key in config and config[key] not in (None, "")
    }
    body["Env"] = _merge_env_list(config.get("Env") or [], env_updates)
    host_config = inspect_doc.get("HostConfig")
    if isinstance(host_config, dict):
        body["HostConfig"] = copy.deepcopy(host_config)
        if restart_policy_name is not None:
            body["HostConfig"]["RestartPolicy"] = {"Name": restart_policy_name}
    networking_config = _networking_config_from_inspect(inspect_doc)
    if networking_config:
        body["NetworkingConfig"] = networking_config
    return body


def _merge_env_list(values: list[Any], updates: dict[str, str]) -> list[str]:
    env: dict[str, str] = {}
    order: list[str] = []
    for item in values:
        key, sep, value = str(item).partition("=")
        if not sep:
            continue
        if key not in env:
            order.append(key)
        env[key] = value
    for key, value in updates.items():
        if key not in env:
            order.append(key)
        env[key] = value
    return [f"{key}={env[key]}" for key in order]


def _networking_config_from_inspect(inspect_doc: dict[str, Any]) -> dict[str, Any]:
    network_settings = inspect_doc.get("NetworkSettings")
    networks = network_settings.get("Networks") if isinstance(network_settings, dict) else {}
    endpoints: dict[str, Any] = {}
    for name, network in (networks or {}).items():
        if not isinstance(network, dict):
            continue
        endpoint: dict[str, Any] = {}
        aliases = network.get("Aliases")
        if aliases:
            endpoint["Aliases"] = list(aliases)
        links = network.get("Links")
        if links:
            endpoint["Links"] = list(links)
        ipam = network.get("IPAMConfig")
        if ipam:
            endpoint["IPAMConfig"] = copy.deepcopy(ipam)
        endpoints[str(name)] = endpoint
    return {"EndpointsConfig": endpoints} if endpoints else {}


def _stop_container(client: DockerSocketClient, container_name: str) -> None:
    if not container_name:
        return
    client.request(
        "POST",
        f"/containers/{quote(container_name, safe='')}/stop?t=10",
        ok_statuses={204, 304, 404},
    )


def _wait_savant_ready_action(
    client: DockerSocketClient,
    container_name: str,
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> dict[str, Any]:
    result = _wait_for_savant_ready(
        client,
        container_name,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
    )
    if not result.get("savant_ready"):
        raise RuntimePerformanceError(
            "savant not ready after performance config apply",
            status_code=503,
            details=result,
        )
    return {"container": container_name, "action": "wait_ready", **result}


def _check_evidence_restart_guard(*, action: str, force: bool) -> dict[str, Any]:
    try:
        return check_runtime_restart_evidence_guard(action=action, force=force)
    except RuntimeApplyBlockedError as exc:
        raise RuntimePerformanceBlockedError(
            str(exc),
            status_code=exc.status_code,
            details=exc.details,
        ) from exc
    except RuntimeApplyError as exc:
        raise RuntimePerformanceError(str(exc), status_code=503) from exc


def _runtime_performance_apply_enabled() -> bool:
    raw = os.getenv("RUNTIME_PERFORMANCE_APPLY_ENABLED")
    if raw is None:
        raw = os.getenv("CAMERA_RUNTIME_APPLY_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
