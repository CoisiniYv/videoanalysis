"""Inference topology controls for the 8090 operator portal."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import yaml

from app.config import get_settings
from app.repositories.cameras import CameraRepository
from app.schemas.cameras import build_export_doc
from app.services.runtime_apply import (
    DEFAULT_ADAPTER_IMAGE,
    DEFAULT_COMPOSE_SOURCE_CONTAINER,
    DEFAULT_FORWARDER_CONTAINER,
    DEFAULT_MODULE_CONFIG_PATH,
    DEFAULT_NETWORK,
    DEFAULT_REPLAY_CONTAINER,
    DEFAULT_SAVANT_CONTAINER,
    DEFAULT_SAVANT_READY_POLL_INTERVAL_S,
    DEFAULT_SAVANT_READY_TIMEOUT_S,
    DEFAULT_SOURCES_CONFIG_PATH,
    DEFAULT_ZMQ_ENDPOINT,
    SOURCE_CONTAINER_PREFIX,
    DockerSocketClient,
    RuntimeApplyBlockedError,
    RuntimeApplyError,
    check_runtime_restart_evidence_guard,
    create_runtime_epoch_state,
    restart_camera_runtime,
    validate_source_id,
    _discover_source_adapter_containers,
    _recreate_rtsp_adapter,
    _start_container,
    _stop_container,
    _wait_for_savant_ready,
    _with_runtime_epoch,
)
from app.services.runtime_overview import (
    fetch_forwarder_metrics,
    fetch_savant_metrics,
)
from app.services.runtime_performance import (
    RuntimePerformanceError,
    _recreate_container_with_env,
)


CONFIG_VERSION = 1
DEFAULT_CONFIG_PATH = "/data/video-analytics/media/.runtime/topology_config.json"
DEFAULT_REPLAY_SHARDS_PATH = "/data/video-analytics/media/.runtime/replay_shards.topology.json"
DEFAULT_TOPOLOGY_MODE = "auto"
DEFAULT_SHARD_STRATEGY = "balanced"
DEFAULT_STREAMS_PER_BRANCH = 30
VALID_TOPOLOGY_MODES = {"auto", "single", "dual_same_gpu", "dual_dual_gpu"}
VALID_SHARD_STRATEGIES = {"balanced", "gpu_id", "manual"}

DUAL_BRANCH_IDS = ("a", "b")


class RuntimeTopologyError(RuntimeError):
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


class RuntimeTopologyBlockedError(RuntimeTopologyError):
    pass


@dataclass(frozen=True)
class BranchConfig:
    branch_id: str
    gpu_id: int
    savant_batch_size: int
    pose_batch_size: int
    face_detector_batch_size: int
    face_embedding_batch_size: int
    max_parallel_streams: int
    analysis_fps: str
    analysis_min_fps: str
    savant_max_fps: str
    savant_min_fps: str
    batched_push_timeout: int


@dataclass(frozen=True)
class RuntimeTopologyConfig:
    config_path: Path
    replay_shards_path: Path
    docker_socket: str
    module_config_path: Path
    sources_config_path: Path
    network: str
    adapter_image: str
    single_savant_container: str
    single_forwarder_container: str
    single_replay_container: str
    compose_source_container: str
    savant_ready_timeout_s: float
    savant_ready_poll_interval_s: float


def config_from_env() -> RuntimeTopologyConfig:
    return RuntimeTopologyConfig(
        config_path=Path(os.getenv("RUNTIME_TOPOLOGY_CONFIG_PATH", DEFAULT_CONFIG_PATH)),
        replay_shards_path=Path(
            os.getenv("RUNTIME_TOPOLOGY_REPLAY_SHARDS_PATH", DEFAULT_REPLAY_SHARDS_PATH)
        ),
        docker_socket=os.getenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/var/run/docker.sock"),
        module_config_path=Path(
            os.getenv("CAMERA_RUNTIME_MODULE_CONFIG_PATH", DEFAULT_MODULE_CONFIG_PATH)
        ),
        sources_config_path=Path(
            os.getenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", DEFAULT_SOURCES_CONFIG_PATH)
        ),
        network=os.getenv("CAMERA_RUNTIME_DOCKER_NETWORK", DEFAULT_NETWORK),
        adapter_image=os.getenv("CAMERA_RUNTIME_ADAPTER_IMAGE", DEFAULT_ADAPTER_IMAGE),
        single_savant_container=os.getenv("CAMERA_RUNTIME_SAVANT_CONTAINER", DEFAULT_SAVANT_CONTAINER),
        single_forwarder_container=os.getenv(
            "CAMERA_RUNTIME_FORWARDER_CONTAINER",
            DEFAULT_FORWARDER_CONTAINER,
        ),
        single_replay_container=os.getenv("CAMERA_RUNTIME_REPLAY_CONTAINER", DEFAULT_REPLAY_CONTAINER),
        compose_source_container=os.getenv(
            "CAMERA_RUNTIME_COMPOSE_SOURCE_CONTAINER",
            DEFAULT_COMPOSE_SOURCE_CONTAINER,
        ),
        savant_ready_timeout_s=_env_float(
            "CAMERA_RUNTIME_SAVANT_READY_TIMEOUT_S",
            DEFAULT_SAVANT_READY_TIMEOUT_S,
        ),
        savant_ready_poll_interval_s=_env_float(
            "CAMERA_RUNTIME_SAVANT_READY_POLL_INTERVAL_S",
            DEFAULT_SAVANT_READY_POLL_INTERVAL_S,
        ),
    )


DEFAULT_BRANCH = {
    "savant_batch_size": 4,
    "pose_batch_size": 4,
    "face_detector_batch_size": 4,
    "face_embedding_batch_size": 16,
    "max_parallel_streams": 64,
    "analysis_fps": "8/1",
    "analysis_min_fps": "2/1",
    "savant_max_fps": "8/1",
    "savant_min_fps": "2/1",
    "batched_push_timeout": 40000,
}


DEFAULT_CONFIG = {
    "topology_mode": DEFAULT_TOPOLOGY_MODE,
    "shard_strategy": DEFAULT_SHARD_STRATEGY,
    "streams_per_branch": DEFAULT_STREAMS_PER_BRANCH,
    "manual_assignments": {},
    "branches": {
        "a": {**DEFAULT_BRANCH, "gpu_id": 0},
        "b": {**DEFAULT_BRANCH, "gpu_id": 1},
    },
}


FIELDS = [
    {
        "key": "topology_mode",
        "kind": "select",
        "label": "拓扑模式",
        "options": [
            {"value": "auto", "label": "自动"},
            {"value": "single", "label": "单分支"},
            {"value": "dual_same_gpu", "label": "双分支同卡"},
            {"value": "dual_dual_gpu", "label": "双分支双卡"},
        ],
    },
    {
        "key": "shard_strategy",
        "kind": "select",
        "label": "分片策略",
        "options": [
            {"value": "balanced", "label": "按数量均分"},
            {"value": "gpu_id", "label": "按摄像头 GPU"},
            {"value": "manual", "label": "手动覆盖"},
        ],
    },
    {"key": "streams_per_branch", "kind": "int", "label": "每分支目标路数", "min": 1, "max": 120},
]

BRANCH_FIELDS = [
    {"key": "gpu_id", "kind": "int", "label": "GPU", "min": 0, "max": 15},
    {"key": "savant_batch_size", "kind": "int", "label": "Savant batch", "min": 1, "max": 128},
    {"key": "pose_batch_size", "kind": "int", "label": "姿态 batch", "min": 1, "max": 128},
    {"key": "face_detector_batch_size", "kind": "int", "label": "人脸检测 batch", "min": 1, "max": 128},
    {"key": "face_embedding_batch_size", "kind": "int", "label": "AdaFace batch", "min": 1, "max": 128},
    {"key": "max_parallel_streams", "kind": "int", "label": "并行流上限", "min": 1, "max": 256},
    {"key": "analysis_fps", "kind": "fps", "label": "Forwarder FPS"},
    {"key": "analysis_min_fps", "kind": "fps", "label": "Forwarder 最小 FPS"},
    {"key": "savant_max_fps", "kind": "fps", "label": "Savant FPS"},
    {"key": "savant_min_fps", "kind": "fps", "label": "Savant 最小 FPS"},
    {"key": "batched_push_timeout", "kind": "int", "label": "Batched push timeout", "min": 0, "max": 1000000},
]


def get_runtime_topology_config(
    *,
    config: RuntimeTopologyConfig | None = None,
    docker_client: DockerSocketClient | None = None,
) -> dict[str, Any]:
    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    saved_doc = _read_saved_doc(cfg.config_path)
    saved_config = _normalize_config(
        saved_doc.get("config") if isinstance(saved_doc.get("config"), dict) else {},
        partial=True,
    )
    cameras, _export_doc = _runtime_docs(include_disabled=True)
    plan = build_topology_plan(saved_config, cameras, available_gpus=_available_gpus())
    return _summary(cfg, client, saved_doc=saved_doc, saved_config=saved_config, plan=plan)


def save_runtime_topology_config(
    payload: dict[str, Any],
    *,
    config: RuntimeTopologyConfig | None = None,
    docker_client: DockerSocketClient | None = None,
) -> dict[str, Any]:
    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    normalized = _normalize_config(payload, partial=True)
    doc = {
        "version": CONFIG_VERSION,
        "updated_at": _now_text(),
        "config": normalized,
    }
    _write_saved_doc(cfg.config_path, doc)
    cameras, _export_doc = _runtime_docs(include_disabled=True)
    plan = build_topology_plan(normalized, cameras, available_gpus=_available_gpus())
    return _summary(cfg, client, saved_doc=doc, saved_config=normalized, plan=plan)


def apply_runtime_topology_config(
    *,
    force: bool = False,
    config: RuntimeTopologyConfig | None = None,
    docker_client: DockerSocketClient | None = None,
) -> dict[str, Any]:
    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    status_before = get_runtime_topology_config(config=cfg, docker_client=client)
    saved_config = status_before["saved_config"]
    cameras, export_doc = _runtime_docs(include_disabled=True)
    plan = build_topology_plan(saved_config, cameras, available_gpus=_available_gpus())
    evidence_guard = _check_evidence_guard(action="runtime_topology_apply", force=force)

    if plan["effective_mode"] == "single":
        _stop_dual_runtime(client)
        result = restart_camera_runtime(export_doc=export_doc, cameras=cameras, force=force)
        status_after = get_runtime_topology_config(config=cfg, docker_client=client)
        return {
            "runtime_action": "topology_apply",
            "changed": True,
            "mode": "single",
            "evidence_restart_guard": evidence_guard,
            "single_runtime": result,
            "status": status_after,
        }

    result = _apply_dual_topology(
        cfg,
        client,
        saved_config=saved_config,
        cameras=cameras,
        export_doc=export_doc,
        plan=plan,
    )
    status_after = get_runtime_topology_config(config=cfg, docker_client=client)
    return {
        "runtime_action": "topology_apply",
        "changed": True,
        "mode": plan["effective_mode"],
        "evidence_restart_guard": evidence_guard,
        **result,
        "status": status_after,
    }


def build_topology_plan(
    config: dict[str, Any],
    cameras: list[dict[str, Any]],
    *,
    available_gpus: list[str] | None = None,
) -> dict[str, Any]:
    enabled_cameras = [camera for camera in cameras if bool(camera.get("enabled", True))]
    requested_mode = str(config.get("topology_mode") or DEFAULT_TOPOLOGY_MODE)
    streams_per_branch = int(config.get("streams_per_branch") or DEFAULT_STREAMS_PER_BRANCH)
    if requested_mode == "auto":
        effective_mode = "single" if len(enabled_cameras) <= streams_per_branch else "dual_auto"
    else:
        effective_mode = requested_mode
    dual = effective_mode in {"dual_auto", "dual_same_gpu", "dual_dual_gpu"}
    gpu_ids = available_gpus or []
    branches = _branch_configs(config, effective_mode=effective_mode, dual=dual, available_gpus=gpu_ids)
    assignments = _assign_cameras(enabled_cameras, config=config, branches=branches, dual=dual)
    branch_rows: list[dict[str, Any]] = []
    for branch in branches:
        sources = assignments.get(branch.branch_id, [])
        branch_rows.append(
            {
                "branch_id": branch.branch_id,
                "gpu_id": branch.gpu_id,
                "source_count": len(sources),
                "source_ids": [str(camera.get("source_id") or "") for camera in sources],
                "camera_names": [str(camera.get("name") or camera.get("source_id") or "") for camera in sources],
                "config": branch.__dict__,
                "containers": _branch_containers(branch.branch_id, dual=dual),
            }
        )
    return {
        "requested_mode": requested_mode,
        "effective_mode": effective_mode,
        "dual": dual,
        "shard_strategy": str(config.get("shard_strategy") or DEFAULT_SHARD_STRATEGY),
        "streams_per_branch": streams_per_branch,
        "enabled_source_count": len(enabled_cameras),
        "total_camera_count": len(cameras),
        "available_gpus": gpu_ids,
        "branches": branch_rows,
        "replay_shards": _replay_shards_doc(branch_rows) if dual else None,
        "sources": _sources_doc(enabled_cameras, assignments=assignments, dual=dual),
    }


def _apply_dual_topology(
    cfg: RuntimeTopologyConfig,
    client: DockerSocketClient,
    *,
    saved_config: dict[str, Any],
    cameras: list[dict[str, Any]],
    export_doc: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    preflight = _preflight(cfg, client, plan=plan)
    fatal = [item for item in preflight["checks"] if not item["ok"] and item.get("fatal")]
    if fatal:
        raise RuntimeTopologyError(
            "runtime topology preflight failed",
            status_code=409,
            details={"preflight": preflight},
        )

    _stop_all_source_adapters(cfg, client)
    _stop_container(client, cfg.single_forwarder_container)
    _stop_container(client, cfg.single_savant_container)
    _stop_container(client, cfg.compose_source_container)

    epoch_state = create_runtime_epoch_state(reason="runtime_topology_apply", created_by="api.runtime_topology")
    runtime_epoch_id = str(epoch_state["runtime_epoch_id"])
    export_doc = _with_runtime_epoch(export_doc, runtime_epoch_id)
    _write_yaml(cfg.module_config_path, export_doc)
    _write_yaml(cfg.sources_config_path, plan["sources"])
    _write_json(cfg.replay_shards_path, plan["replay_shards"] or {})

    actions: list[dict[str, Any]] = []
    for branch in plan["branches"]:
        branch_id = str(branch["branch_id"])
        branch_config = branch["config"]
        savant_container = f"video-analytics-midterm-savant-{branch_id}"
        forwarder_container = f"video-analytics-midterm-analysis-forwarder-{branch_id}"
        replay_container = f"video-analytics-midterm-replay-{branch_id}"
        video_sink_container = f"video-analytics-midterm-video-file-sink-{branch_id}"
        savant_env = _savant_env(branch_config, mode=str(plan["effective_mode"]))
        forwarder_env = _forwarder_env(branch_config)
        try:
            actions.append(
                _recreate_container_with_env(
                    client,
                    savant_container,
                    savant_env,
                    force_start=True,
                )
            )
            ready = _wait_for_savant_ready(
                client,
                savant_container,
                timeout_s=cfg.savant_ready_timeout_s,
                poll_interval_s=cfg.savant_ready_poll_interval_s,
            )
            actions.append({"container": savant_container, "action": "wait_ready", **ready})
            if not ready.get("savant_ready"):
                raise RuntimeTopologyError(
                    f"{savant_container} not ready after topology apply",
                    status_code=503,
                    details=ready,
                )
            actions.append(
                _recreate_container_with_env(
                    client,
                    forwarder_container,
                    forwarder_env,
                    force_start=True,
                )
            )
            actions.append({"container": replay_container, "action": "start", "status": _start_container(client, replay_container)})
            actions.append({"container": video_sink_container, "action": "start", "status": _start_container(client, video_sink_container)})
        except RuntimePerformanceError as exc:
            raise RuntimeTopologyError(str(exc), status_code=exc.status_code, details=exc.details) from exc
        except RuntimeApplyError as exc:
            raise RuntimeTopologyError(str(exc), status_code=503) from exc

    source_lifecycle = _start_sources_from_plan(cfg, client, sources_doc=plan["sources"])
    return {
        "runtime_epoch_id": runtime_epoch_id,
        "runtime_epoch": epoch_state,
        "module_config_path": str(cfg.module_config_path),
        "sources_config_path": str(cfg.sources_config_path),
        "replay_shards_path": str(cfg.replay_shards_path),
        "preflight": preflight,
        "actions": actions,
        "source_lifecycle": source_lifecycle,
    }


def _summary(
    cfg: RuntimeTopologyConfig,
    client: DockerSocketClient,
    *,
    saved_doc: dict[str, Any],
    saved_config: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    return {
        "config_path": str(cfg.config_path),
        "replay_shards_path": str(cfg.replay_shards_path),
        "source": "file" if saved_doc else "default",
        "version": saved_doc.get("version") if saved_doc else None,
        "updated_at": saved_doc.get("updated_at") if saved_doc else "",
        "saved_config": saved_config,
        "defaults": DEFAULT_CONFIG,
        "fields": FIELDS,
        "branch_fields": BRANCH_FIELDS,
        "plan": plan,
        "preflight": _preflight(cfg, client, plan=plan),
        "runtime": _runtime_status(client, plan=plan),
    }


def _runtime_status(client: DockerSocketClient, *, plan: dict[str, Any]) -> dict[str, Any]:
    branches = []
    for branch in plan.get("branches") or []:
        branch_id = str(branch.get("branch_id") or "")
        dual = bool(plan.get("dual"))
        containers = _branch_containers(branch_id, dual=dual)
        container_status = {
            key: _inspect_container(client, name)
            for key, name in containers.items()
            if name
        }
        metrics = _branch_metrics(branch_id, dual=dual)
        branches.append(
            {
                "branch_id": branch_id,
                "source_count": branch.get("source_count"),
                "containers": container_status,
                "metrics": metrics,
            }
        )
    return {"branches": branches}


def _branch_metrics(branch_id: str, *, dual: bool) -> dict[str, Any]:
    if dual:
        suffix = branch_id
        savant_url = f"http://savant-{suffix}:8080/metrics"
        forwarder_url = f"http://analysis-forwarder-{suffix}:8081/metrics"
    else:
        savant_url = "http://savant-security:8080/metrics"
        forwarder_url = "http://analysis-forwarder:8081/metrics"
    return {
        "savant": fetch_savant_metrics(savant_url, timeout_s=1.0),
        "forwarder": fetch_forwarder_metrics(forwarder_url, timeout_s=1.0),
    }


def _preflight(
    cfg: RuntimeTopologyConfig,
    client: DockerSocketClient,
    *,
    plan: dict[str, Any],
) -> dict[str, Any]:
    available_gpus = list(plan.get("available_gpus") or _available_gpus())
    checks: list[dict[str, Any]] = []
    dual = bool(plan.get("dual"))
    if dual:
        for branch in plan.get("branches") or []:
            branch_id = str(branch.get("branch_id") or "")
            for role, container_name in _branch_containers(branch_id, dual=True).items():
                if role == "source":
                    continue
                inspect_doc = _inspect_container(client, container_name)
                checks.append(
                    {
                        "name": f"{branch_id}_{role}_container_present",
                        "ok": bool(inspect_doc.get("present")),
                        "fatal": True,
                        "container": container_name,
                        "state": inspect_doc.get("state"),
                    }
                )
            source_count = int(branch.get("source_count") or 0)
            max_streams = int((branch.get("config") or {}).get("max_parallel_streams") or 0)
            checks.append(
                {
                    "name": f"{branch_id}_source_count_within_parallel_limit",
                    "ok": source_count <= max_streams,
                    "fatal": True,
                    "source_count": source_count,
                    "max_parallel_streams": max_streams,
                }
            )
            gpu_id = int((branch.get("config") or {}).get("gpu_id") or 0)
            checks.append(
                {
                    "name": f"{branch_id}_gpu_known",
                    "ok": not available_gpus or str(gpu_id) in available_gpus,
                    "fatal": False,
                    "gpu_id": gpu_id,
                    "available_gpus": available_gpus,
                }
            )
    else:
        for name in (cfg.single_savant_container, cfg.single_forwarder_container):
            inspect_doc = _inspect_container(client, name)
            checks.append(
                {
                    "name": f"{name}_present",
                    "ok": bool(inspect_doc.get("present")),
                    "fatal": True,
                    "container": name,
                    "state": inspect_doc.get("state"),
                }
            )
    return {"ok": all(item["ok"] or not item.get("fatal") for item in checks), "checks": checks}


def _normalize_config(payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeTopologyError("topology config body must be an object", status_code=400)
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    unknown = sorted(set(payload) - {"topology_mode", "shard_strategy", "streams_per_branch", "branches", "manual_assignments"})
    if unknown:
        raise RuntimeTopologyError(
            "unknown topology config fields",
            status_code=400,
            details={"fields": unknown},
        )
    if "topology_mode" in payload:
        mode = str(payload["topology_mode"] or "").strip()
        if mode not in VALID_TOPOLOGY_MODES:
            raise RuntimeTopologyError(f"invalid topology mode: {mode}", status_code=400)
        config["topology_mode"] = mode
    if "shard_strategy" in payload:
        strategy = str(payload["shard_strategy"] or "").strip()
        if strategy not in VALID_SHARD_STRATEGIES:
            raise RuntimeTopologyError(f"invalid shard strategy: {strategy}", status_code=400)
        config["shard_strategy"] = strategy
    if "streams_per_branch" in payload:
        config["streams_per_branch"] = _bounded_int(payload["streams_per_branch"], 1, 120)
    if isinstance(payload.get("manual_assignments"), dict):
        config["manual_assignments"] = {
            str(source_id): str(branch_id)
            for source_id, branch_id in payload["manual_assignments"].items()
            if str(branch_id) in set(DUAL_BRANCH_IDS)
        }
    raw_branches = payload.get("branches") if isinstance(payload.get("branches"), dict) else {}
    for branch_id in DUAL_BRANCH_IDS:
        raw = raw_branches.get(branch_id) if isinstance(raw_branches, dict) else None
        if not isinstance(raw, dict):
            continue
        branch = dict(config["branches"][branch_id])
        for field in BRANCH_FIELDS:
            key = field["key"]
            if key not in raw:
                continue
            if field["kind"] == "int":
                branch[key] = _bounded_int(raw[key], int(field.get("min", 0)), int(field.get("max", 1000000)))
            elif field["kind"] == "fps":
                branch[key] = _normalize_fps(raw[key])
        config["branches"][branch_id] = branch
    if partial:
        return config
    return config


def _branch_configs(
    config: dict[str, Any],
    *,
    effective_mode: str,
    dual: bool,
    available_gpus: list[str],
) -> list[BranchConfig]:
    branches = config.get("branches") if isinstance(config.get("branches"), dict) else {}
    if not dual:
        branch_raw = dict(branches.get("a") or DEFAULT_CONFIG["branches"]["a"])
        return [_branch_config("single", branch_raw, gpu_id=int(branch_raw.get("gpu_id", 0)))]
    auto_gpu_ids = _auto_branch_gpu_ids(branches, available_gpus) if effective_mode == "dual_auto" else {}
    rows = []
    for branch_id in DUAL_BRANCH_IDS:
        branch_raw = dict(branches.get(branch_id) or DEFAULT_CONFIG["branches"][branch_id])
        if branch_id in auto_gpu_ids:
            branch_raw["gpu_id"] = auto_gpu_ids[branch_id]
        if effective_mode == "dual_same_gpu":
            branch_raw["gpu_id"] = int((branches.get("a") or {}).get("gpu_id", branch_raw.get("gpu_id", 0)))
        rows.append(_branch_config(branch_id, branch_raw, gpu_id=int(branch_raw.get("gpu_id", 0))))
    if effective_mode == "dual_same_gpu":
        gpu_id = rows[0].gpu_id
        rows = [
            BranchConfig(**{**row.__dict__, "gpu_id": gpu_id})
            for row in rows
        ]
    return rows


def _auto_branch_gpu_ids(branches: dict[str, Any], available_gpus: list[str]) -> dict[str, int]:
    parsed: list[int] = []
    for raw in available_gpus:
        try:
            parsed.append(int(str(raw).strip()))
        except ValueError:
            continue
    if len(parsed) >= 2:
        return {"a": parsed[0], "b": parsed[1]}
    if len(parsed) == 1:
        return {"a": parsed[0], "b": parsed[0]}
    fallback = int((branches.get("a") or {}).get("gpu_id", DEFAULT_CONFIG["branches"]["a"]["gpu_id"]))
    return {"a": fallback, "b": fallback}


def _branch_config(branch_id: str, raw: dict[str, Any], *, gpu_id: int) -> BranchConfig:
    return BranchConfig(
        branch_id=branch_id,
        gpu_id=gpu_id,
        savant_batch_size=int(raw.get("savant_batch_size", 4)),
        pose_batch_size=int(raw.get("pose_batch_size", 4)),
        face_detector_batch_size=int(raw.get("face_detector_batch_size", 4)),
        face_embedding_batch_size=int(raw.get("face_embedding_batch_size", 16)),
        max_parallel_streams=int(raw.get("max_parallel_streams", 64)),
        analysis_fps=str(raw.get("analysis_fps", "8/1")),
        analysis_min_fps=str(raw.get("analysis_min_fps", "2/1")),
        savant_max_fps=str(raw.get("savant_max_fps", "8/1")),
        savant_min_fps=str(raw.get("savant_min_fps", "2/1")),
        batched_push_timeout=int(raw.get("batched_push_timeout", 40000)),
    )


def _assign_cameras(
    cameras: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    branches: list[BranchConfig],
    dual: bool,
) -> dict[str, list[dict[str, Any]]]:
    if not dual:
        return {branches[0].branch_id: cameras}
    result = {branch.branch_id: [] for branch in branches}
    branch_ids = [branch.branch_id for branch in branches]
    manual = config.get("manual_assignments") if isinstance(config.get("manual_assignments"), dict) else {}
    strategy = str(config.get("shard_strategy") or DEFAULT_SHARD_STRATEGY)
    if strategy == "manual":
        for index, camera in enumerate(cameras):
            source_id = str(camera.get("source_id") or "")
            branch_id = str(manual.get(source_id) or branch_ids[index % len(branch_ids)])
            if branch_id not in result:
                branch_id = branch_ids[index % len(branch_ids)]
            result[branch_id].append(camera)
        return result
    if strategy == "gpu_id":
        gpu_to_branch = {branch.gpu_id: branch.branch_id for branch in branches}
        for index, camera in enumerate(cameras):
            branch_id = gpu_to_branch.get(int(camera.get("gpu_id") or 0), branch_ids[index % len(branch_ids)])
            result[branch_id].append(camera)
        return result
    for index, camera in enumerate(cameras):
        result[branch_ids[index % len(branch_ids)]].append(camera)
    return result


def _sources_doc(
    cameras: list[dict[str, Any]],
    *,
    assignments: dict[str, list[dict[str, Any]]],
    dual: bool,
) -> dict[str, Any]:
    source_branch: dict[str, str] = {}
    for branch_id, rows in assignments.items():
        for camera in rows:
            source_branch[str(camera.get("source_id") or "")] = branch_id
    sources: dict[str, dict[str, Any]] = {}
    for camera in cameras:
        camera_id = str(camera["id"])
        source_id = validate_source_id(str(camera["source_id"]))
        branch_id = source_branch.get(source_id, "single")
        if dual:
            endpoint = f"dealer+connect:tcp://replay-{branch_id}:5555"
            replay_shard_id = f"replay-{branch_id}"
        else:
            endpoint = DEFAULT_ZMQ_ENDPOINT
            replay_shard_id = "default"
        sources[camera_id] = {
            "camera_id": camera_id,
            "source_id": source_id,
            "uri": str(camera["rtsp_url"]),
            "enabled": bool(camera.get("enabled", True)),
            "adapter_type": "gstreamer",
            "zmq_endpoint": endpoint,
            "replay_shard_id": replay_shard_id,
            "camera_name": str(camera.get("name") or ""),
        }
    return {"sources": sources}


def _replay_shards_doc(branches: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "default_shard_id": "replay-a",
        "shards": [
            {
                "shard_id": f"replay-{branch['branch_id']}",
                "replay_api_url": f"http://replay-{branch['branch_id']}:8080",
                "in_stream_endpoint": f"dealer+connect:tcp://replay-{branch['branch_id']}:5555",
                "replay_job_sink_url": f"dealer+connect:tcp://video-file-sink-{branch['branch_id']}:6666",
                "source_ids": list(branch.get("source_ids") or []),
            }
            for branch in branches
        ],
    }


def _branch_containers(branch_id: str, *, dual: bool) -> dict[str, str]:
    if not dual:
        return {
            "savant": DEFAULT_SAVANT_CONTAINER,
            "forwarder": DEFAULT_FORWARDER_CONTAINER,
            "replay": DEFAULT_REPLAY_CONTAINER,
        }
    return {
        "savant": f"video-analytics-midterm-savant-{branch_id}",
        "forwarder": f"video-analytics-midterm-analysis-forwarder-{branch_id}",
        "replay": f"video-analytics-midterm-replay-{branch_id}",
        "video_sink": f"video-analytics-midterm-video-file-sink-{branch_id}",
    }


def _savant_env(branch_config: dict[str, Any], *, mode: str) -> dict[str, str]:
    gpu_id = str(int(branch_config.get("gpu_id") or 0))
    env = {
        "NVIDIA_VISIBLE_DEVICES": gpu_id,
        "CUDA_VISIBLE_DEVICES": "0",
        "BATCH_SIZE": str(branch_config["savant_batch_size"]),
        "POSE_BATCH_SIZE": str(branch_config["pose_batch_size"]),
        "FACE_DETECTOR_BATCH_SIZE": str(branch_config["face_detector_batch_size"]),
        "FACE_EMBEDDING_BATCH_SIZE": str(branch_config["face_embedding_batch_size"]),
        "MAX_PARALLEL_STREAMS": str(branch_config["max_parallel_streams"]),
        "BATCHED_PUSH_TIMEOUT": str(branch_config["batched_push_timeout"]),
        "MAX_FPS": str(branch_config["savant_max_fps"]),
        "MIN_FPS": str(branch_config["savant_min_fps"]),
    }
    if mode == "dual_same_gpu":
        env["CUDA_VISIBLE_DEVICES"] = "0"
    return env


def _forwarder_env(branch_config: dict[str, Any]) -> dict[str, str]:
    return {
        "ANALYSIS_FPS": str(branch_config["analysis_fps"]),
        "ANALYSIS_MIN_FPS": str(branch_config["analysis_min_fps"]),
    }


def _start_sources_from_plan(
    cfg: RuntimeTopologyConfig,
    client: DockerSocketClient,
    *,
    sources_doc: dict[str, Any],
) -> list[dict[str, Any]]:
    lifecycle: list[dict[str, Any]] = []
    for source in sources_doc.get("sources", {}).values():
        if not isinstance(source, dict) or not source.get("enabled"):
            continue
        source_id = validate_source_id(str(source.get("source_id") or ""))
        uri = str(source.get("uri") or "")
        if not uri.startswith(("rtsp://", "rtsps://")):
            lifecycle.append({"source_id": source_id, "action": "skipped", "skip_reason": "non_rtsp_uri"})
            continue
        diag = _recreate_rtsp_adapter(
            client,
            source_id=source_id,
            uri=uri,
            zmq_endpoint=str(source["zmq_endpoint"]),
            adapter_image=cfg.adapter_image,
            network=cfg.network,
        )
        lifecycle.append({"source_id": source_id, **diag, "action": "created_started"})
    return lifecycle


def _stop_all_source_adapters(cfg: RuntimeTopologyConfig, client: DockerSocketClient) -> None:
    names = [cfg.compose_source_container, *_discover_source_adapter_containers(client)]
    for name in sorted(set(names)):
        _stop_container(client, name)
        if name.startswith(SOURCE_CONTAINER_PREFIX):
            client.request("DELETE", f"/containers/{quote(name, safe='')}?force=true", ok_statuses={204, 404})


def _stop_dual_runtime(client: DockerSocketClient) -> None:
    for branch_id in DUAL_BRANCH_IDS:
        for container_name in _branch_containers(branch_id, dual=True).values():
            _stop_container(client, container_name)


def _runtime_docs(*, include_disabled: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conn = psycopg.connect(get_settings().database_url)
    try:
        repo = CameraRepository(conn)
        cameras = repo.list_cameras() if include_disabled else repo.list_cameras(enabled=True)
        camera_ids = [camera["id"] for camera in cameras]
        zones_by_camera = repo.list_zones_for_cameras(camera_ids)
        rules_by_camera = repo.list_rules_for_cameras(camera_ids)
        return cameras, build_export_doc(cameras, zones_by_camera, rules_by_camera)
    finally:
        conn.close()


def _inspect_container(client: DockerSocketClient, container_name: str) -> dict[str, Any]:
    try:
        status, body = client.request(
            "GET",
            f"/containers/{quote(container_name, safe='')}/json",
            ok_statuses={200, 404},
        )
    except RuntimeApplyError as exc:
        return {"name": container_name, "present": False, "state": "unavailable", "error": str(exc)}
    if status == 404:
        return {"name": container_name, "present": False, "state": "missing"}
    doc = json.loads(body.decode("utf-8") or "{}")
    state = doc.get("State") if isinstance(doc, dict) else {}
    return {
        "name": container_name,
        "present": True,
        "state": str(state.get("Status") or "unknown") if isinstance(state, dict) else "unknown",
        "running": bool(state.get("Running")) if isinstance(state, dict) else False,
        "health": str(((state.get("Health") or {}) if isinstance(state, dict) else {}).get("Status") or ""),
    }


def _available_gpus() -> list[str]:
    raw = os.getenv("RUNTIME_TOPOLOGY_AVAILABLE_GPUS")
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader,nounits"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _check_evidence_guard(*, action: str, force: bool) -> dict[str, Any]:
    try:
        return check_runtime_restart_evidence_guard(action=action, force=force)
    except RuntimeApplyBlockedError as exc:
        raise RuntimeTopologyBlockedError(str(exc), status_code=exc.status_code, details=exc.details) from exc
    except RuntimeApplyError as exc:
        raise RuntimeTopologyError(str(exc), status_code=503) from exc


def _bounded_int(value: Any, min_value: int, max_value: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeTopologyError(f"invalid integer value: {value!r}", status_code=400) from exc
    if number < min_value or number > max_value:
        raise RuntimeTopologyError(
            f"integer value out of range: {number} not in [{min_value}, {max_value}]",
            status_code=400,
        )
    return number


def _normalize_fps(value: Any) -> str:
    text = str(value or "").strip()
    parts = text.split("/")
    if len(parts) != 2:
        raise RuntimeTopologyError(f"invalid fps value: {value!r}", status_code=400)
    numerator, denominator = parts
    try:
        if int(numerator) < 0 or int(denominator) <= 0:
            raise ValueError
    except ValueError as exc:
        raise RuntimeTopologyError(f"invalid fps value: {value!r}", status_code=400) from exc
    return f"{int(numerator)}/{int(denominator)}"


def _read_saved_doc(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise RuntimeTopologyError(f"failed to read topology config: {exc}", status_code=503) from exc


def _write_saved_doc(path: Path, doc: dict[str, Any]) -> None:
    _write_json(path, doc)


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _write_yaml(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, path)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
