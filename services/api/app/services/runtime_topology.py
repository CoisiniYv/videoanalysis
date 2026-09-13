"""Inference topology controls for the 8090 operator portal."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
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
VALID_RUNTIME_PROFILES = {"custom", "production_t4_40", "local_4090_60"}
VALID_PIPELINE_MODES = {"inference_only", "full_evidence"}

DUAL_BRANCH_IDS = ("a", "b")
MPS_CONTAINER = "video-analytics-midterm-cuda-mps-operator"
ROI_WORKER_CONTAINER = "video-analytics-midterm-adaface-roi-worker"
EVENT_WORKER_CONTAINER = "video-analytics-midterm-event-worker"
MEDIA_WORKER_CONTAINER = "video-analytics-midterm-media-worker"
MPS_PIPE_DIRECTORY = "/tmp/video-analytics-mps/pipe"
MPS_LOG_DIRECTORY = "/tmp/video-analytics-mps/log"


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


ProgressCallback = Callable[[dict[str, Any]], None]


def _emit_progress(
    callback: ProgressCallback | None,
    *,
    phase: str,
    percent: int,
    message: str,
    **details: Any,
) -> None:
    if callback is None:
        return
    callback(
        {
            "phase": phase,
            "percent": max(0, min(100, int(percent))),
            "message": message,
            **details,
        }
    )


@dataclass(frozen=True)
class BranchConfig:
    branch_id: str
    gpu_id: int
    savant_batch_size: int
    pose_batch_size: int
    face_detector_batch_size: int
    face_embedding_batch_size: int
    face_infer_interval: int
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
    "face_infer_interval": 7,
    "max_parallel_streams": 64,
    "analysis_fps": "8/1",
    "analysis_min_fps": "2/1",
    "savant_max_fps": "8/1",
    "savant_min_fps": "2/1",
    "batched_push_timeout": 40000,
}


DEFAULT_CONFIG = {
    "runtime_profile": "custom",
    "pipeline_mode": "inference_only",
    "topology_mode": DEFAULT_TOPOLOGY_MODE,
    "shard_strategy": DEFAULT_SHARD_STRATEGY,
    "streams_per_branch": DEFAULT_STREAMS_PER_BRANCH,
    "manual_assignments": {},
    "branches": {
        "a": {**DEFAULT_BRANCH, "gpu_id": 0},
        "b": {**DEFAULT_BRANCH, "gpu_id": 1},
    },
}


RUNTIME_PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "production_t4_40": {
        "pipeline_mode": "full_evidence",
        "expected_source_count": 40,
        "streams_per_branch": 20,
        "cuda_mps_enabled": True,
        "mps_savant_percentage": 45,
        "mps_adaface_percentage": 10,
        "recording_cooldown_seconds": 60,
        # Forty synchronized sources can create 40-80 evidence tasks in one
        # burst.  Keep enough independent remux/finalizer slots to drain that
        # burst without allowing the pending age to cross the deadline.
        "media_worker_max_active": 10,
        "media_worker_remux_workers": 5,
        "media_worker_finalizer_workers": 5,
        "materialization_ready_segment_grace_seconds": 1,
        "frame_cache_sidecar_max_scan": 500,
        "cpuset_cpus": {
            "savant_a": "0-2,8-10",
            "savant_b": "3-5,11-13",
            "forwarder_a": "6,14",
            "forwarder_b": "6,14",
            "roi_worker": "6,14",
            "event_worker": "15",
            "media_worker": "6-7,14-15",
            "rolling_sink_a": "0-15",
            "rolling_sink_b": "0-15",
        },
        "roi_batch_timeout_ms": 200,
        "rolling_prefill_seconds": 25,
        # Ten minutes covers 5+5 evidence plus bounded queue recovery without
        # making the online segment catalog retain an unnecessary 15 minutes.
        "rolling_retention_seconds": 600,
        "branch": {
            "gpu_id": 0,
            "savant_batch_size": 4,
            "pose_batch_size": 4,
            "face_detector_batch_size": 4,
            "face_embedding_batch_size": 16,
            "face_infer_interval": 3,
            "max_parallel_streams": 64,
            "analysis_fps": "4/1",
            "analysis_min_fps": "99/25",
            "savant_max_fps": "4/1",
            "savant_min_fps": "99/25",
            "batched_push_timeout": 10000,
        },
    },
    "local_4090_60": {
        "pipeline_mode": "full_evidence",
        "expected_source_count": 60,
        "streams_per_branch": 30,
        "cuda_mps_enabled": False,
        "mps_savant_percentage": 0,
        "mps_adaface_percentage": 0,
        "recording_cooldown_seconds": 30,
        "media_worker_max_active": 12,
        "media_worker_remux_workers": 8,
        "media_worker_finalizer_workers": 16,
        "materialization_ready_segment_grace_seconds": 1,
        "frame_cache_sidecar_max_scan": 500,
        "cpuset_cpus": {},
        "roi_batch_timeout_ms": 40,
        "rolling_prefill_seconds": 25,
        "rolling_retention_seconds": 600,
        "branch": {
            "gpu_id": 0,
            "savant_batch_size": 4,
            "pose_batch_size": 4,
            "face_detector_batch_size": 4,
            "face_embedding_batch_size": 16,
            "face_infer_interval": 7,
            "max_parallel_streams": 64,
            "analysis_fps": "8/1",
            "analysis_min_fps": "198/25",
            "savant_max_fps": "8/1",
            "savant_min_fps": "198/25",
            "batched_push_timeout": 40000,
        },
    },
}


FIELDS = [
    {
        "key": "runtime_profile",
        "kind": "select",
        "label": "运行预设",
        "options": [
            {"value": "production_t4_40", "label": "生产 T4 40 路完整链路"},
            {"value": "local_4090_60", "label": "本机 4090 60 路完整链路"},
            {"value": "custom", "label": "自定义"},
        ],
    },
    {
        "key": "pipeline_mode",
        "kind": "select",
        "label": "链路范围",
        "options": [
            {"value": "full_evidence", "label": "完整推理、轨迹和证据"},
            {"value": "inference_only", "label": "仅推理拓扑"},
        ],
    },
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
    {"key": "face_infer_interval", "kind": "int", "label": "人脸推理 interval", "min": 0, "max": 120},
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
    camera_selection: dict[str, Any] | None = None,
    config: RuntimeTopologyConfig | None = None,
    docker_client: DockerSocketClient | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    _emit_progress(
        progress_callback,
        phase="validation",
        percent=3,
        message="正在读取运行预设和摄像头配置",
    )
    cfg = config or config_from_env()
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    status_before = get_runtime_topology_config(config=cfg, docker_client=client)
    saved_config = status_before["saved_config"]
    cameras_before, _export_doc_before = _runtime_docs(include_disabled=True)
    projected_cameras = _project_camera_selection(camera_selection, cameras_before)
    projected_plan = build_topology_plan(
        saved_config,
        projected_cameras,
        available_gpus=_available_gpus(),
    )
    evidence_guard = _check_evidence_guard(action="runtime_topology_apply", force=force)
    _emit_progress(
        progress_callback,
        phase="preflight",
        percent=8,
        message="正在检查证据任务、双分支容器和模型缓存",
    )
    if camera_selection is not None:
        projected_preflight = _preflight(cfg, client, plan=projected_plan)
        fatal = [
            item
            for item in projected_preflight["checks"]
            if not item["ok"] and item.get("fatal")
        ]
        if fatal:
            source_ids, disable_unselected = _normalize_camera_selection(
                camera_selection
            )
            raise RuntimeTopologyError(
                "runtime topology preflight failed",
                status_code=409,
                details={
                    "preflight": projected_preflight,
                    "camera_selection": {
                        "applied": False,
                        "source_ids": source_ids,
                        "selected_count": len(source_ids),
                        "disable_unselected": disable_unselected,
                        "reason": "preflight_failed",
                    },
                },
            )
    selection_result = _stage_camera_selection(camera_selection)
    cooldown_result = _apply_profile_cooldown(
        runtime_profile=str(saved_config.get("runtime_profile") or "custom"),
        cooldown_seconds=int(projected_plan.get("recording_cooldown_seconds") or 0),
    )
    _emit_progress(
        progress_callback,
        phase="camera_selection",
        percent=12,
        message="摄像头选择已锁定，准备切换运行拓扑",
    )
    cameras, export_doc = _runtime_docs(include_disabled=True)
    plan = build_topology_plan(saved_config, cameras, available_gpus=_available_gpus())

    if plan["effective_mode"] == "single":
        _emit_progress(
            progress_callback,
            phase="single_runtime",
            percent=30,
            message="正在恢复日常单分支运行",
        )
        _stop_dual_runtime(client)
        pipeline_restore = _restore_single_pipeline_workers(client)
        result = restart_camera_runtime(export_doc=export_doc, cameras=cameras, force=force)
        status_after = get_runtime_topology_config(config=cfg, docker_client=client)
        if selection_result is not None:
            selection_result = {**selection_result, "runtime_applied": True}
        _emit_progress(
            progress_callback,
            phase="complete",
            percent=100,
            message="日常单分支已就绪",
        )
        return {
            "runtime_action": "topology_apply",
            "changed": True,
            "mode": "single",
            "camera_selection": selection_result,
            "profile_cooldown": cooldown_result,
            "evidence_restart_guard": evidence_guard,
            "single_runtime": result,
            "pipeline_restore": pipeline_restore,
            "status": status_after,
        }

    try:
        result = _apply_dual_topology(
            cfg,
            client,
            saved_config=saved_config,
            cameras=cameras,
            export_doc=export_doc,
            plan=plan,
            progress_callback=progress_callback,
        )
    except RuntimeTopologyError as exc:
        details = dict(exc.details)
        if selection_result is not None:
            details["camera_selection"] = {
                **selection_result,
                "runtime_applied": False,
            }
        raise RuntimeTopologyError(
            str(exc),
            status_code=exc.status_code,
            details=details,
        ) from exc
    status_after = get_runtime_topology_config(config=cfg, docker_client=client)
    if selection_result is not None:
        selection_result = {**selection_result, "runtime_applied": True}
    return {
        "runtime_action": "topology_apply",
        "changed": True,
        "mode": plan["effective_mode"],
        "camera_selection": selection_result,
        "profile_cooldown": cooldown_result,
        "evidence_restart_guard": evidence_guard,
        **result,
        "status": status_after,
    }


def _project_camera_selection(
    selection: dict[str, Any] | None,
    cameras: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and project a staged selection before any database mutation."""
    if selection is None:
        return cameras
    source_ids, disable_unselected = _normalize_camera_selection(selection)
    by_source_id = {
        str(camera.get("source_id") or ""): camera for camera in cameras
    }
    missing = [source_id for source_id in source_ids if source_id not in by_source_id]
    if missing:
        raise RuntimeTopologyError(
            "selected camera source_id was not found",
            status_code=404,
            details={"missing_source_ids": missing},
        )
    invalid = [
        source_id
        for source_id in source_ids
        if not str(by_source_id[source_id].get("rtsp_url") or "").strip()
    ]
    if invalid:
        raise RuntimeTopologyError(
            "selected camera has no video URL",
            status_code=422,
            details={"invalid_source_ids": invalid},
        )
    selected = set(source_ids)
    projected: list[dict[str, Any]] = []
    for camera in cameras:
        row = dict(camera)
        source_id = str(row.get("source_id") or "")
        if disable_unselected:
            row["enabled"] = source_id in selected
        elif source_id in selected:
            row["enabled"] = True
        projected.append(row)
    return projected


def _normalize_camera_selection(
    selection: dict[str, Any],
) -> tuple[list[str], bool]:
    if not isinstance(selection, dict):
        raise RuntimeTopologyError(
            "camera_selection must be an object",
            status_code=400,
        )
    unknown = sorted(set(selection) - {"source_ids", "disable_unselected"})
    if unknown:
        raise RuntimeTopologyError(
            "unknown camera selection fields",
            status_code=400,
            details={"fields": unknown},
        )
    raw_source_ids = selection.get("source_ids")
    if not isinstance(raw_source_ids, list):
        raise RuntimeTopologyError(
            "camera_selection.source_ids must be an array",
            status_code=400,
        )
    source_ids = [str(source_id).strip() for source_id in raw_source_ids]
    if not source_ids:
        raise RuntimeTopologyError(
            "camera_selection.source_ids must select at least one camera",
            status_code=400,
        )
    if any(not source_id for source_id in source_ids):
        raise RuntimeTopologyError(
            "camera_selection.source_ids cannot contain empty values",
            status_code=400,
        )
    if len(set(source_ids)) != len(source_ids):
        raise RuntimeTopologyError(
            "camera_selection.source_ids contains duplicates",
            status_code=400,
        )
    return source_ids, bool(selection.get("disable_unselected", True))


def _stage_camera_selection(
    selection: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Stage the operator's camera selection without invoking camera CRUD sync.

    The normal camera enable endpoint intentionally remains a day-2 CRUD path.
    The camera-first quick-start path uses this small transaction so a group of
    sources cannot briefly enter the existing single-branch runtime one by one.
    """
    if selection is None:
        return None
    source_ids, disable_unselected = _normalize_camera_selection(selection)
    conn = psycopg.connect(get_settings().database_url)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source_id FROM cameras WHERE enabled ORDER BY source_id"
                )
                previous_enabled_source_ids = [
                    str(row[0]) for row in cur.fetchall()
                ]
                cur.execute(
                    """
                    SELECT source_id, rtsp_url, input_type
                    FROM cameras
                    WHERE source_id = ANY(%(source_ids)s)
                    FOR UPDATE
                    """,
                    {"source_ids": source_ids},
                )
                rows = cur.fetchall()
                found = {str(row[0]) for row in rows}
                missing = [source_id for source_id in source_ids if source_id not in found]
                if missing:
                    raise RuntimeTopologyError(
                        "selected camera source_id was not found",
                        status_code=404,
                        details={"missing_source_ids": missing},
                    )
                invalid = [
                    str(row[0])
                    for row in rows
                    if not str(row[1] or "").strip()
                ]
                if invalid:
                    raise RuntimeTopologyError(
                        "selected camera has no video URL",
                        status_code=422,
                        details={"invalid_source_ids": invalid},
                    )
                if disable_unselected:
                    cur.execute(
                        """
                        UPDATE cameras
                        SET enabled = (source_id = ANY(%(source_ids)s)),
                            updated_at = now()
                        """,
                        {"source_ids": source_ids},
                    )
                elif source_ids:
                    cur.execute(
                        """
                        UPDATE cameras
                        SET enabled = TRUE, updated_at = now()
                        WHERE source_id = ANY(%(source_ids)s)
                        """,
                        {"source_ids": source_ids},
                    )
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM cameras WHERE enabled")
            enabled_count = int(cur.fetchone()[0])
        return {
            "applied": True,
            "source_ids": source_ids,
            "selected_count": len(source_ids),
            "enabled_count": enabled_count,
            "previous_enabled_source_ids": previous_enabled_source_ids,
            "previous_enabled_count": len(previous_enabled_source_ids),
            "disable_unselected": disable_unselected,
            "runtime_sync": "deferred_to_topology_apply",
        }
    finally:
        conn.close()


def _apply_profile_cooldown(
    *, runtime_profile: str, cooldown_seconds: int
) -> dict[str, Any]:
    """Merge a named profile's cooldown into enabled cameras and rules.

    Only the two cooldown keys are updated. Existing alert-policy and rule
    configuration fields remain untouched so an operator profile apply cannot
    erase zones, thresholds, evidence policy, or other private configuration.
    """
    if runtime_profile not in RUNTIME_PROFILE_PRESETS or cooldown_seconds < 0:
        return {
            "applied": False,
            "runtime_profile": runtime_profile,
            "cooldown_seconds": cooldown_seconds,
        }
    conn = psycopg.connect(get_settings().database_url)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE cameras
                    SET alert_policy = jsonb_set(
                            COALESCE(alert_policy, '{}'::jsonb),
                            '{global_alert_cooldown_s}',
                            to_jsonb(%(cooldown)s::integer),
                            true
                        ),
                        updated_at = now()
                    WHERE enabled
                    """,
                    {"cooldown": cooldown_seconds},
                )
                camera_count = int(cur.rowcount or 0)
                cur.execute(
                    """
                    UPDATE camera_rules AS rule
                    SET config = jsonb_set(
                            COALESCE(rule.config, '{}'::jsonb),
                            '{cooldown_s}',
                            to_jsonb(%(cooldown)s::integer),
                            true
                        ),
                        updated_at = now()
                    FROM cameras AS camera
                    WHERE rule.camera_id = camera.id
                      AND camera.enabled
                      AND rule.enabled
                    """,
                    {"cooldown": cooldown_seconds},
                )
                rule_count = int(cur.rowcount or 0)
        return {
            "applied": True,
            "runtime_profile": runtime_profile,
            "cooldown_seconds": cooldown_seconds,
            "camera_count": camera_count,
            "rule_count": rule_count,
            "merge_only": True,
        }
    finally:
        conn.close()


def _profile_host_config(
    plan: dict[str, Any], role: str
) -> dict[str, Any] | None:
    cpuset = str((plan.get("cpuset_cpus") or {}).get(role) or "").strip()
    return {"CpusetCpus": cpuset} if cpuset else None


def build_topology_plan(
    config: dict[str, Any],
    cameras: list[dict[str, Any]],
    *,
    available_gpus: list[str] | None = None,
) -> dict[str, Any]:
    enabled_cameras = [camera for camera in cameras if bool(camera.get("enabled", True))]
    runtime_profile = str(config.get("runtime_profile") or "custom")
    preset = RUNTIME_PROFILE_PRESETS.get(runtime_profile) or {}
    pipeline_mode = str(config.get("pipeline_mode") or "inference_only")
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
                "containers": _branch_containers(
                    branch.branch_id,
                    dual=dual,
                    full_pipeline=pipeline_mode == "full_evidence" and dual,
                ),
            }
        )
    return {
        "runtime_profile": runtime_profile,
        "pipeline_mode": pipeline_mode,
        "full_pipeline": pipeline_mode == "full_evidence" and dual,
        "expected_source_count": int(preset.get("expected_source_count") or 0),
        "cuda_mps_enabled": bool(preset.get("cuda_mps_enabled")),
        "mps_savant_percentage": int(preset.get("mps_savant_percentage") or 0),
        "mps_adaface_percentage": int(preset.get("mps_adaface_percentage") or 0),
        "recording_cooldown_seconds": int(preset.get("recording_cooldown_seconds") or 0),
        "media_worker_max_active": int(preset.get("media_worker_max_active") or 4),
        "media_worker_remux_workers": int(preset.get("media_worker_remux_workers") or 1),
        "media_worker_finalizer_workers": int(
            preset.get("media_worker_finalizer_workers") or 4
        ),
        "materialization_ready_segment_grace_seconds": int(
            preset.get("materialization_ready_segment_grace_seconds") or 0
        ),
        "frame_cache_sidecar_max_scan": int(
            preset.get("frame_cache_sidecar_max_scan") or 500
        ),
        "cpuset_cpus": dict(preset.get("cpuset_cpus") or {}),
        "roi_batch_timeout_ms": int(preset.get("roi_batch_timeout_ms") or 40),
        "rolling_prefill_seconds": int(preset.get("rolling_prefill_seconds") or 25),
        "rolling_retention_seconds": int(
            preset.get("rolling_retention_seconds") or 600
        ),
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
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    _emit_progress(
        progress_callback,
        phase="preflight",
        percent=14,
        message="双分支结构预检通过，正在停止旧 source 和单分支",
    )
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
    if plan.get("full_pipeline"):
        _emit_progress(
            progress_callback,
            phase="pipeline_workers",
            percent=20,
            message="正在启动 MPS、ROI AdaFace 和 evidence worker",
        )
        actions.extend(
            _prepare_full_pipeline(
                client,
                plan=plan,
                runtime_epoch_id=runtime_epoch_id,
            )
        )
    for branch_index, branch in enumerate(plan["branches"]):
        branch_id = str(branch["branch_id"])
        branch_label = branch_id.upper()
        branch_start_percent = 28 + branch_index * 18
        _emit_progress(
            progress_callback,
            phase=f"branch_{branch_id}",
            percent=branch_start_percent,
            message=f"正在初始化分支 {branch_label} 的双 YOLO/TensorRT",
            branch_id=branch_id,
        )
        branch_config = branch["config"]
        savant_container = f"video-analytics-midterm-savant-{branch_id}"
        full_pipeline = bool(plan.get("full_pipeline"))
        forwarder_container = (
            f"video-analytics-midterm-replay-raw-fanout-{branch_id}"
            if full_pipeline
            else f"video-analytics-midterm-analysis-forwarder-{branch_id}"
        )
        replay_container = f"video-analytics-midterm-replay-{branch_id}"
        video_sink_container = f"video-analytics-midterm-video-file-sink-{branch_id}"
        rolling_sink_container = (
            f"video-analytics-midterm-rolling-cache-sink-{branch_id}"
        )
        savant_env = _savant_env(
            branch_config,
            mode=str(plan["effective_mode"]),
            full_pipeline=full_pipeline,
            cuda_mps_enabled=bool(plan.get("cuda_mps_enabled")),
            mps_percentage=int(plan.get("mps_savant_percentage") or 0),
        )
        forwarder_env = _forwarder_env(
            branch_config,
            branch_id=branch_id,
            full_pipeline=full_pipeline,
        )
        try:
            actions.append(
                _recreate_container_with_env(
                    client,
                    savant_container,
                    savant_env,
                    force_start=True,
                    host_config_updates=_profile_host_config(
                        plan, f"savant_{branch_id}"
                    ),
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
            _emit_progress(
                progress_callback,
                phase=f"branch_{branch_id}",
                percent=branch_start_percent + 9,
                message=f"分支 {branch_label} 推理已就绪，正在启动 raw fanout、Replay 和 rolling sink",
                branch_id=branch_id,
            )
            actions.append(
                _recreate_container_with_env(
                    client,
                    forwarder_container,
                    forwarder_env,
                    force_start=True,
                    host_config_updates=_profile_host_config(
                        plan, f"forwarder_{branch_id}"
                    ),
                )
            )
            if full_pipeline:
                actions.append(
                    _wait_for_container_ready(
                        client,
                        forwarder_container,
                        timeout_s=30,
                        require_healthy=True,
                    )
                )
            actions.append({"container": replay_container, "action": "start", "status": _start_container(client, replay_container)})
            actions.append({"container": video_sink_container, "action": "start", "status": _start_container(client, video_sink_container)})
            if full_pipeline:
                actions.append(
                    _recreate_container_with_env(
                        client,
                        rolling_sink_container,
                        {
                            "ROLLING_CACHE_RUNTIME_EPOCH_ID": runtime_epoch_id,
                            "ROLLING_CACHE_ROOT": "/media/rolling-cache",
                            "ROLLING_CACHE_SEGMENT_SECONDS": "4",
                            "ROLLING_CACHE_RETENTION_SECONDS": str(
                                int(plan.get("rolling_retention_seconds") or 600)
                            ),
                        },
                        force_start=True,
                        host_config_updates=_profile_host_config(
                            plan, f"rolling_sink_{branch_id}"
                        ),
                    )
                )
                legacy_forwarder = (
                    f"video-analytics-midterm-analysis-forwarder-{branch_id}"
                )
                _stop_container(client, legacy_forwarder)
                actions.append(
                    {
                        "container": legacy_forwarder,
                        "action": "stop_unused_legacy_forwarder",
                    }
                )
        except RuntimePerformanceError as exc:
            raise RuntimeTopologyError(str(exc), status_code=exc.status_code, details=exc.details) from exc
        except RuntimeApplyError as exc:
            raise RuntimeTopologyError(str(exc), status_code=503) from exc

    _emit_progress(
        progress_callback,
        phase="sources",
        percent=64,
        message="双分支容器已启动，正在接入所选摄像头",
    )
    source_lifecycle = _start_sources_from_plan(cfg, client, sources_doc=plan["sources"])
    evidence_activation: dict[str, Any] | None = None
    source_convergence = (
        _wait_for_source_convergence(
            plan,
            timeout_s=_env_float(
                "RUNTIME_TOPOLOGY_SOURCE_READY_TIMEOUT_S", 300.0
            ),
            progress_callback=progress_callback,
        )
        if plan.get("full_pipeline")
        else {"ready": True, "branches": []}
    )
    if plan.get("full_pipeline"):
        _emit_progress(
            progress_callback,
            phase="rolling_cache_ready",
            percent=78,
            message="视频源已收敛，正在确认双 rolling-cache sink",
        )
        for branch_id in DUAL_BRANCH_IDS:
            actions.append(
                _wait_for_container_ready(
                    client,
                    f"video-analytics-midterm-rolling-cache-sink-{branch_id}",
                    timeout_s=90,
                    require_healthy=True,
                )
            )
        prefill_seconds = int(plan.get("rolling_prefill_seconds") or 25)
        if prefill_seconds > 0:
            for elapsed in range(prefill_seconds):
                remaining = prefill_seconds - elapsed
                _emit_progress(
                    progress_callback,
                    phase="rolling_cache_prefill",
                    percent=84 + int(10 * elapsed / max(1, prefill_seconds)),
                    message=f"rolling-cache 正在预热，还需约 {remaining} 秒",
                    rolling_cache={
                        "prefill_seconds": prefill_seconds,
                        "remaining_seconds": remaining,
                    },
                )
                time.sleep(1)
            _emit_progress(
                progress_callback,
                phase="rolling_cache_prefill",
                percent=94,
                message="rolling-cache 预热完成，正在开放 evidence 任务",
                rolling_cache={
                    "prefill_seconds": prefill_seconds,
                    "remaining_seconds": 0,
                },
            )
        activation_ts_ms = int(time.time() * 1000)
        activation_action = _recreate_container_with_env(
            client,
            EVENT_WORKER_CONTAINER,
            {
                "EVIDENCE_TASK_CREATION_ENABLED": "true",
                "EVIDENCE_TASK_EVENT_NOT_BEFORE_TS_MS": str(activation_ts_ms),
                "EVIDENCE_TASK_EVENT_NOT_AFTER_TS_MS": "0",
            },
            force_start=True,
        )
        actions.append(activation_action)
        evidence_activation = {
            "prefill_seconds": prefill_seconds,
            "event_not_before_ts_ms": activation_ts_ms,
            "event_worker_action": activation_action,
        }
    _emit_progress(
        progress_callback,
        phase="evidence_activation",
        percent=97,
        message="正在检查完整 evidence 链最终状态",
    )
    convergence = _full_pipeline_convergence(client, plan=plan)
    if plan.get("full_pipeline") and not convergence["ready"]:
        raise RuntimeTopologyError(
            "full evidence pipeline did not converge after topology apply",
            status_code=503,
            details={"convergence": convergence},
        )
    return {
        "runtime_epoch_id": runtime_epoch_id,
        "runtime_epoch": epoch_state,
        "module_config_path": str(cfg.module_config_path),
        "sources_config_path": str(cfg.sources_config_path),
        "replay_shards_path": str(cfg.replay_shards_path),
        "preflight": preflight,
        "actions": actions,
        "source_lifecycle": source_lifecycle,
        "source_convergence": source_convergence,
        "evidence_activation": evidence_activation,
        "pipeline_convergence": convergence,
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
        "profile_presets": RUNTIME_PROFILE_PRESETS,
        "plan": plan,
        "preflight": _preflight(cfg, client, plan=plan),
        "runtime": _runtime_status(client, plan=plan),
    }


def _runtime_status(client: DockerSocketClient, *, plan: dict[str, Any]) -> dict[str, Any]:
    branches = []
    for branch in plan.get("branches") or []:
        branch_id = str(branch.get("branch_id") or "")
        dual = bool(plan.get("dual"))
        containers = _branch_containers(
            branch_id,
            dual=dual,
            full_pipeline=bool(plan.get("full_pipeline")),
        )
        container_status = {
            key: _inspect_container(client, name)
            for key, name in containers.items()
            if name
        }
        metrics = _branch_metrics(
            branch_id,
            dual=dual,
            full_pipeline=bool(plan.get("full_pipeline")),
        )
        branches.append(
            {
                "branch_id": branch_id,
                "source_count": branch.get("source_count"),
                "containers": container_status,
                "metrics": metrics,
            }
        )
    pipeline_containers: dict[str, dict[str, Any]] = {}
    if plan.get("full_pipeline"):
        names = {
            "roi_worker": ROI_WORKER_CONTAINER,
            "event_worker": EVENT_WORKER_CONTAINER,
            "media_worker": MEDIA_WORKER_CONTAINER,
        }
        if plan.get("cuda_mps_enabled"):
            names["cuda_mps"] = MPS_CONTAINER
        pipeline_containers = {
            role: _inspect_container(client, name) for role, name in names.items()
        }
    branch_pipeline_states = [
        state
        for branch in branches
        for state in (branch.get("containers") or {}).values()
    ]
    pipeline_ready = bool(pipeline_containers) and all(
        item.get("running")
        and item.get("state") == "running"
        and item.get("health") not in {"starting", "unhealthy"}
        for item in [*pipeline_containers.values(), *branch_pipeline_states]
    )
    return {
        "branches": branches,
        "pipeline": {
            "mode": plan.get("pipeline_mode"),
            "profile": plan.get("runtime_profile"),
            "containers": pipeline_containers,
            "ready": pipeline_ready if plan.get("full_pipeline") else None,
        },
    }


def _branch_metrics(
    branch_id: str,
    *,
    dual: bool,
    full_pipeline: bool = False,
) -> dict[str, Any]:
    if dual:
        suffix = branch_id
        savant_url = f"http://savant-{suffix}:8080/metrics"
        forwarder_url = (
            f"http://replay-raw-fanout-{suffix}:8081/metrics"
            if full_pipeline
            else f"http://analysis-forwarder-{suffix}:8081/metrics"
        )
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
    expected_source_count = int(plan.get("expected_source_count") or 0)
    if expected_source_count:
        actual_source_count = int(plan.get("enabled_source_count") or 0)
        checks.append(
            {
                "name": "profile_enabled_source_count",
                "ok": actual_source_count == expected_source_count,
                "fatal": True,
                "expected_source_count": expected_source_count,
                "source_count": actual_source_count,
            }
        )
    if dual:
        for branch in plan.get("branches") or []:
            branch_id = str(branch.get("branch_id") or "")
            for role, container_name in _branch_containers(
                branch_id,
                dual=True,
                full_pipeline=bool(plan.get("full_pipeline")),
            ).items():
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
        if plan.get("full_pipeline"):
            savant_b_model_root_raw = os.getenv(
                "RUNTIME_TOPOLOGY_SAVANT_B_MODEL_ROOT"
            )
            if savant_b_model_root_raw:
                savant_b_model_root = Path(savant_b_model_root_raw)
                required_models = (
                    "yolo26_pose/yolo26_pose.dynamic.raw56.onnx",
                    "yolov8_face/yolov8n-face.dynamic.onnx",
                    "adaface/adaface_ir50_webface4m.onnx",
                )
                missing_models = [
                    relative_path
                    for relative_path in required_models
                    if not (savant_b_model_root / relative_path).is_file()
                ]
                checks.append(
                    {
                        "name": "savant_b_model_cache_ready",
                        "ok": not missing_models,
                        "fatal": True,
                        "model_root": str(savant_b_model_root),
                        "missing_models": missing_models,
                    }
                )
            pipeline_names = {
                "roi_worker": ROI_WORKER_CONTAINER,
                "event_worker": EVENT_WORKER_CONTAINER,
                "media_worker": MEDIA_WORKER_CONTAINER,
            }
            if plan.get("cuda_mps_enabled"):
                pipeline_names["cuda_mps"] = MPS_CONTAINER
            for role, container_name in pipeline_names.items():
                inspect_doc = _inspect_container(client, container_name)
                checks.append(
                    {
                        "name": f"full_pipeline_{role}_container_present",
                        "ok": bool(inspect_doc.get("present")),
                        "fatal": True,
                        "container": container_name,
                        "state": inspect_doc.get("state"),
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
    unknown = sorted(
        set(payload)
        - {
            "runtime_profile",
            "pipeline_mode",
            "topology_mode",
            "shard_strategy",
            "streams_per_branch",
            "branches",
            "manual_assignments",
        }
    )
    if unknown:
        raise RuntimeTopologyError(
            "unknown topology config fields",
            status_code=400,
            details={"fields": unknown},
        )
    if "runtime_profile" in payload:
        runtime_profile = str(payload["runtime_profile"] or "").strip()
        if runtime_profile not in VALID_RUNTIME_PROFILES:
            raise RuntimeTopologyError(
                f"invalid runtime profile: {runtime_profile}", status_code=400
            )
        config["runtime_profile"] = runtime_profile
    if "pipeline_mode" in payload:
        pipeline_mode = str(payload["pipeline_mode"] or "").strip()
        if pipeline_mode not in VALID_PIPELINE_MODES:
            raise RuntimeTopologyError(
                f"invalid pipeline mode: {pipeline_mode}", status_code=400
            )
        config["pipeline_mode"] = pipeline_mode
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
    preset = RUNTIME_PROFILE_PRESETS.get(str(config.get("runtime_profile") or "custom"))
    if preset:
        config["pipeline_mode"] = str(preset["pipeline_mode"])
        config["topology_mode"] = "dual_same_gpu"
        if config.get("shard_strategy") not in {"balanced", "manual"}:
            config["shard_strategy"] = "balanced"
        config["streams_per_branch"] = int(preset["streams_per_branch"])
        branch_preset = dict(preset["branch"])
        config["branches"] = {
            branch_id: dict(branch_preset) for branch_id in DUAL_BRANCH_IDS
        }
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
        face_infer_interval=int(raw.get("face_infer_interval", 7)),
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


def _branch_containers(
    branch_id: str,
    *,
    dual: bool,
    full_pipeline: bool = False,
) -> dict[str, str]:
    if not dual:
        return {
            "savant": DEFAULT_SAVANT_CONTAINER,
            "forwarder": DEFAULT_FORWARDER_CONTAINER,
            "replay": DEFAULT_REPLAY_CONTAINER,
        }
    containers = {
        "savant": f"video-analytics-midterm-savant-{branch_id}",
        "forwarder": (
            f"video-analytics-midterm-replay-raw-fanout-{branch_id}"
            if full_pipeline
            else f"video-analytics-midterm-analysis-forwarder-{branch_id}"
        ),
        "replay": f"video-analytics-midterm-replay-{branch_id}",
        "video_sink": f"video-analytics-midterm-video-file-sink-{branch_id}",
    }
    if full_pipeline:
        containers["rolling_sink"] = (
            f"video-analytics-midterm-rolling-cache-sink-{branch_id}"
        )
    return containers


def _savant_env(
    branch_config: dict[str, Any],
    *,
    mode: str,
    full_pipeline: bool = False,
    cuda_mps_enabled: bool = False,
    mps_percentage: int = 0,
) -> dict[str, str | None]:
    gpu_id = str(int(branch_config.get("gpu_id") or 0))
    env = {
        "NVIDIA_VISIBLE_DEVICES": gpu_id,
        "CUDA_VISIBLE_DEVICES": "0",
        "BATCH_SIZE": str(branch_config["savant_batch_size"]),
        "POSE_BATCH_SIZE": str(branch_config["pose_batch_size"]),
        "FACE_DETECTOR_BATCH_SIZE": str(branch_config["face_detector_batch_size"]),
        "FACE_EMBEDDING_BATCH_SIZE": str(branch_config["face_embedding_batch_size"]),
        "FACE_INFER_INTERVAL": str(branch_config["face_infer_interval"]),
        "FACE_EMBEDDING_INFER_INTERVAL": str(branch_config["face_infer_interval"]),
        "MAX_PARALLEL_STREAMS": str(branch_config["max_parallel_streams"]),
        "BATCHED_PUSH_TIMEOUT": str(branch_config["batched_push_timeout"]),
        "MAX_FPS": str(branch_config["savant_max_fps"]),
        "MIN_FPS": str(branch_config["savant_min_fps"]),
    }
    if mode == "dual_same_gpu":
        env["CUDA_VISIBLE_DEVICES"] = "0"
    if full_pipeline:
        env.update(
            {
                "FACE_ROI_EXPORT_ENABLED": "true",
                "FACE_ROI_STREAM": "security.face_rois",
                "FACE_ROI_STREAM_MAXLEN": "20000",
                "FACE_ROI_QUEUE_MAXSIZE": "4096",
                "FACE_ROI_TTL_MS": "5000",
                "FACE_ROI_JPEG_QUALITY": "95",
                "ADAFACE_INPUT_OBJECT": "disabled.face",
                "FACE_OBSERVATION_EXPORT_ENABLED": "false",
                "OUTPUT_FRAME": "null",
                "FRAME_ANNOTATION_SOURCE_STREAM_ENABLED": "true",
                "FRAME_ANNOTATION_STREAM_MODE": "dual",
                "FRAME_ANNOTATION_SOURCE_REDIS_MAXLEN": "10000",
                "SAVANT_STAGE_METRICS_ENABLED": "true",
            }
        )
    if cuda_mps_enabled:
        env.update(
            {
                "CUDA_MPS_PIPE_DIRECTORY": MPS_PIPE_DIRECTORY,
                "CUDA_MPS_LOG_DIRECTORY": MPS_LOG_DIRECTORY,
                "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(mps_percentage),
            }
        )
    else:
        env.update(
            {
                "CUDA_MPS_PIPE_DIRECTORY": None,
                "CUDA_MPS_LOG_DIRECTORY": None,
                "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": None,
            }
        )
    return env


def _forwarder_env(
    branch_config: dict[str, Any],
    *,
    branch_id: str = "",
    full_pipeline: bool = False,
) -> dict[str, str]:
    env = {
        "ANALYSIS_FPS": str(branch_config["analysis_fps"]),
        "ANALYSIS_MIN_FPS": str(branch_config["analysis_min_fps"]),
    }
    if full_pipeline:
        env.update(
            {
                "FORWARDER_OUT_ENDPOINT": (
                    f"dealer+connect:tcp://savant-{branch_id}:5557"
                ),
                "FORWARDER_RAW_OUT_ENDPOINT": "pub+bind:tcp://0.0.0.0:5560",
                "FORWARDER_SAMPLER_ENABLED": "true",
                "FORWARDER_QUEUE_MAX_SIZE": "8192",
                "FORWARDER_RECEIVE_HWM": "10000",
                "FORWARDER_SEND_TIMEOUT_MS": "2000",
                "FORWARDER_SEND_RETRIES": "3",
                "FORWARDER_SEND_HWM": "1000",
            }
        )
    return env


def _prepare_full_pipeline(
    client: DockerSocketClient,
    *,
    plan: dict[str, Any],
    runtime_epoch_id: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    cuda_mps_enabled = bool(plan.get("cuda_mps_enabled"))
    if cuda_mps_enabled:
        actions.append(
            {
                "container": MPS_CONTAINER,
                "action": "start",
                "status": _start_container(client, MPS_CONTAINER),
            }
        )
        actions.append(
            _wait_for_container_ready(
                client, MPS_CONTAINER, timeout_s=30, require_healthy=True
            )
        )
    else:
        _stop_container(client, MPS_CONTAINER)
        actions.append({"container": MPS_CONTAINER, "action": "stop_not_required"})

    roi_env: dict[str, str | None] = {
        "FACE_ROI_STREAM": "security.face_rois",
        "FACE_ROI_CONSUMER_GROUP": "adaface-roi-workers",
        "FACE_ROI_CONSUMER_NAME": "adaface-roi-worker-1",
        "FACE_ROI_TTL_MS": "5000",
        "FACE_ROI_BATCH_TIMEOUT_MS": str(plan.get("roi_batch_timeout_ms") or 40),
        "FACE_EMBEDDING_BATCH_SIZE": "16",
    }
    if cuda_mps_enabled:
        roi_env.update(
            {
                "CUDA_MPS_PIPE_DIRECTORY": MPS_PIPE_DIRECTORY,
                "CUDA_MPS_LOG_DIRECTORY": MPS_LOG_DIRECTORY,
                "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": str(
                    plan.get("mps_adaface_percentage") or 10
                ),
            }
        )
    else:
        roi_env.update(
            {
                "CUDA_MPS_PIPE_DIRECTORY": None,
                "CUDA_MPS_LOG_DIRECTORY": None,
                "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": None,
            }
        )
    actions.append(
        _recreate_container_with_env(
            client,
            ROI_WORKER_CONTAINER,
            roi_env,
            force_start=True,
            host_config_updates=_profile_host_config(plan, "roi_worker"),
        )
    )
    actions.append(
        _wait_for_container_ready(
            client, ROI_WORKER_CONTAINER, timeout_s=180, require_healthy=True
        )
    )

    source_ids = [
        str(source_id)
        for branch in plan.get("branches") or []
        for source_id in branch.get("source_ids") or []
    ]
    cooldown_seconds = int(plan.get("recording_cooldown_seconds") or 30)
    ready_segment_grace_s = max(
        0,
        int(plan.get("materialization_ready_segment_grace_seconds") or 0),
    )
    event_env: dict[str, str | None] = {
        "ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS": "true",
        "EVIDENCE_DENSITY_PROFILE": "high_density",
        "EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL": "0",
        "EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE": "0",
        "EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE": "0",
        "EVIDENCE_TASK_CREATION_ENABLED": "false",
        "EVIDENCE_TASK_EVENT_NOT_BEFORE_TS_MS": "0",
        "EVIDENCE_TASK_EVENT_NOT_AFTER_TS_MS": "0",
        "EVIDENCE_TASK_GATE_REDIS_KEY": None,
        "RECORDING_COOLDOWN_SECONDS": str(cooldown_seconds),
        "RECORDING_COOLDOWN_SCOPE": "algorithm",
        "DEFAULT_PRE_SECONDS": "5",
        "DEFAULT_POST_SECONDS": "5",
        "RECORDING_PRE_SECONDS": "5",
        "RECORDING_POST_SECONDS": "5",
        # Segment-index coverage, not a fixed timer, is authoritative for the
        # closing post-event fragment. A short grace avoids premature churn
        # while removing the old nine-second unconditional wait.
        "EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS": str(
            ready_segment_grace_s
        ),
    }
    actions.append(
        _recreate_container_with_env(
            client,
            EVENT_WORKER_CONTAINER,
            event_env,
            force_start=True,
            host_config_updates=_profile_host_config(plan, "event_worker"),
        )
    )

    remux_workers = int(plan.get("media_worker_remux_workers") or 1)
    max_active = int(plan.get("media_worker_max_active") or 4)
    finalizer_workers = int(plan.get("media_worker_finalizer_workers") or 4)
    sidecar_max_scan = int(plan.get("frame_cache_sidecar_max_scan") or 500)
    media_env = {
        "MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE": str(max_active),
        "MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT": str(
            max(4, min(8, remux_workers))
        ),
        "MEDIA_WORKER_IMAGE_WORKERS": "4",
        "MEDIA_WORKER_IMAGE_QUEUE_CAPACITY": "4",
        "MEDIA_WORKER_REMUX_QUEUE_CAPACITY": str(remux_workers),
        "MEDIA_WORKER_FINALIZER_WORKERS": str(finalizer_workers),
        "MEDIA_WORKER_FINALIZER_PROCESS_WORKERS": str(finalizer_workers),
        "MEDIA_WORKER_FINALIZER_QUEUE_CAPACITY": str(finalizer_workers),
        "MEDIA_WORKER_SCHEDULER_V2_ENABLED": "true",
        "MEDIA_WORKER_DB_POOL_ENABLED": "true",
        "MEDIA_WORKER_SEGMENT_INDEX_ENABLED": "true",
        "MEDIA_WORKER_SEGMENT_INDEX_RECONCILE_INTERVAL_S": "60",
        "MEDIA_WORKER_SEGMENT_INDEX_ROW_CACHE_ENTRIES": "2048",
        "MEDIA_WORKER_SEGMENT_INDEX_ROW_CACHE_MAX_BYTES": "268435456",
        "MEDIA_WORKER_LEGACY_DERIVATIVES_ENABLED": "false",
        "EVIDENCE_DENSITY_PROFILE": "high_density",
        "ROLLING_CACHE_ENABLED": "true",
        "ROLLING_CACHE_MATERIALIZATION_ENABLED": "true",
        "ROLLING_CACHE_SOURCES": ",".join(source_ids),
        "ROLLING_CACHE_ROOT": "/media/rolling-cache",
        "ROLLING_CACHE_MATERIALIZED_ROOT": "/media/rolling-cache-materialized",
        "ROLLING_CACHE_RETENTION_SECONDS": str(
            int(plan.get("rolling_retention_seconds") or 600)
        ),
        "ROLLING_CACHE_SEGMENT_SECONDS": "4",
        "ROLLING_CACHE_FALLBACK_TO_REPLAY": "false",
        "ROLLING_CACHE_MATERIALIZATION_MAX_PER_POLL": str(max_active),
        "ROLLING_CACHE_MATERIALIZATION_WORKERS": str(remux_workers),
        "ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S": "1",
        "ROLLING_CACHE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS": str(
            ready_segment_grace_s
        ),
        "ROLLING_CACHE_MATERIALIZATION_PROCESSING_DEADLINE_SECONDS": "120",
        "FRAME_CACHE_SIDECAR_RANGE_CACHE_BUCKET_MS": "10000",
        "FRAME_CACHE_SIDECAR_RANGE_CACHE_TTL_S": "900",
        "FRAME_CACHE_SIDECAR_RANGE_CACHE_MAX_ENTRIES": "64",
        "FRAME_CACHE_SIDECAR_MAX_SCAN": str(sidecar_max_scan),
        "FRAME_CACHE_SIDECAR_SCAN_HARD_LIMIT": str(sidecar_max_scan),
        "FRAME_CACHE_SIDECAR_SOURCE_STREAM_ENABLED": "true",
        "FRAME_CACHE_SIDECAR_SOURCE_STREAM_FALLBACK_GLOBAL": "false",
        "EVIDENCE_DB_INDEX_EXPANDED_ROWS_ENABLED": "true",
        "DEFAULT_PRE_SECONDS": "5",
        "DEFAULT_POST_SECONDS": "5",
        "RUNTIME_EPOCH_ID": runtime_epoch_id,
    }
    actions.append(
        _recreate_container_with_env(
            client,
            MEDIA_WORKER_CONTAINER,
            media_env,
            force_start=True,
            host_config_updates=_profile_host_config(plan, "media_worker"),
        )
    )
    return actions


def _restore_single_pipeline_workers(
    client: DockerSocketClient,
) -> list[dict[str, Any]]:
    event_env: dict[str, str | None] = {
        "ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS": "false",
        "EVIDENCE_DENSITY_PROFILE": "normal",
        "EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL": "240",
        "EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE": "1",
        "EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE": "intrusion:40",
        "EVIDENCE_TASK_CREATION_ENABLED": "true",
        "EVIDENCE_TASK_EVENT_NOT_BEFORE_TS_MS": "0",
        "EVIDENCE_TASK_EVENT_NOT_AFTER_TS_MS": "0",
        "EVIDENCE_TASK_GATE_REDIS_KEY": None,
        "RECORDING_COOLDOWN_SECONDS": "30",
        "RECORDING_COOLDOWN_SCOPE": "algorithm",
        "EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS": "0",
    }
    media_env = {
        "EVIDENCE_DENSITY_PROFILE": "normal",
        "ROLLING_CACHE_ENABLED": "false",
        "ROLLING_CACHE_MATERIALIZATION_ENABLED": "false",
        "ROLLING_CACHE_SOURCES": "",
        "ROLLING_CACHE_FALLBACK_TO_REPLAY": "true",
        "MEDIA_WORKER_LEGACY_DERIVATIVES_ENABLED": "true",
        "MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE": "4",
        "MEDIA_WORKER_REMUX_QUEUE_CAPACITY": "1",
        "ROLLING_CACHE_MATERIALIZATION_WORKERS": "1",
    }
    actions = [
        _recreate_container_with_env(
            client, EVENT_WORKER_CONTAINER, event_env, force_start=True
        ),
        _recreate_container_with_env(
            client, MEDIA_WORKER_CONTAINER, media_env, force_start=True
        ),
    ]
    return actions


def _wait_for_container_ready(
    client: DockerSocketClient,
    container_name: str,
    *,
    timeout_s: float,
    require_healthy: bool,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, timeout_s)
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _inspect_container(client, container_name)
        health = str(last.get("health") or "")
        running = bool(last.get("running")) and last.get("state") == "running"
        if running and (not require_healthy or health == "healthy"):
            return {
                "container": container_name,
                "action": "wait_ready",
                "ready": True,
                "state": last,
            }
        if health == "unhealthy" or last.get("state") in {"dead", "exited"}:
            break
        time.sleep(0.5)
    raise RuntimeTopologyError(
        f"{container_name} did not become ready",
        status_code=503,
        details={"container": container_name, "state": last},
    )


def _full_pipeline_convergence(
    client: DockerSocketClient, *, plan: dict[str, Any]
) -> dict[str, Any]:
    if not plan.get("full_pipeline"):
        return {"ready": True, "mode": "inference_only", "containers": {}}
    names = {
        "roi_worker": ROI_WORKER_CONTAINER,
        "event_worker": EVENT_WORKER_CONTAINER,
        "media_worker": MEDIA_WORKER_CONTAINER,
    }
    if plan.get("cuda_mps_enabled"):
        names["cuda_mps"] = MPS_CONTAINER
    for branch_id in DUAL_BRANCH_IDS:
        names.update(
            {
                f"savant_{branch_id}": f"video-analytics-midterm-savant-{branch_id}",
                f"forwarder_{branch_id}": f"video-analytics-midterm-replay-raw-fanout-{branch_id}",
                f"replay_{branch_id}": f"video-analytics-midterm-replay-{branch_id}",
                f"rolling_sink_{branch_id}": f"video-analytics-midterm-rolling-cache-sink-{branch_id}",
            }
        )
    states = {role: _inspect_container(client, name) for role, name in names.items()}
    failed = [
        role
        for role, state in states.items()
        if not state.get("running")
        or state.get("state") != "running"
        or state.get("health") in {"starting", "unhealthy"}
    ]
    return {
        "ready": not failed,
        "mode": "full_evidence",
        "profile": plan.get("runtime_profile"),
        "failed": failed,
        "containers": states,
    }


def _wait_for_source_convergence(
    plan: dict[str, Any],
    *,
    timeout_s: float,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, timeout_s)
    stable_samples = 0
    snapshots: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        branch_rows: list[dict[str, Any]] = []
        ready = True
        for branch in plan.get("branches") or []:
            branch_id = str(branch.get("branch_id") or "")
            expected = int(branch.get("source_count") or 0)
            metrics = _branch_metrics(
                branch_id, dual=True, full_pipeline=True
            )
            savant = metrics.get("savant") or {}
            forwarder = metrics.get("forwarder") or {}
            savant_sources = int(
                (savant.get("global") or {}).get("va_savant_sources_active")
                or len(savant.get("sources") or [])
            )
            forwarder_sources = len(forwarder.get("sources") or [])
            branch_ready = (
                expected == 0
                or (savant_sources >= expected and forwarder_sources >= expected)
            )
            ready = ready and branch_ready
            branch_rows.append(
                {
                    "branch_id": branch_id,
                    "expected": expected,
                    "savant_sources": savant_sources,
                    "forwarder_sources": forwarder_sources,
                    "ready": branch_ready,
                }
            )
        snapshots.append(
            {
                "elapsed_s": round(max(0.0, timeout_s - (deadline - time.monotonic())), 3),
                "branches": branch_rows,
                "ready": ready,
            }
        )
        snapshots = snapshots[-10:]
        elapsed_s = max(0.0, timeout_s - (deadline - time.monotonic()))
        branch_summary = ", ".join(
            f"{row['branch_id'].upper()} {row['savant_sources']}/{row['expected']}"
            for row in branch_rows
        )
        _emit_progress(
            progress_callback,
            phase="source_convergence",
            percent=min(77, 68 + int(elapsed_s / 30)),
            message=f"正在等待摄像头进入双分支：{branch_summary}",
            source_convergence={"branches": branch_rows},
        )
        stable_samples = stable_samples + 1 if ready else 0
        if stable_samples >= 2:
            return {
                "ready": True,
                "stable_samples": stable_samples,
                "branches": branch_rows,
                "snapshots": snapshots,
            }
        time.sleep(2.0)
    raise RuntimeTopologyError(
        "full pipeline sources did not converge",
        status_code=503,
        details={"source_convergence": snapshots[-1] if snapshots else {}},
    )


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
        names = {
            *_branch_containers(branch_id, dual=True).values(),
            *_branch_containers(
                branch_id, dual=True, full_pipeline=True
            ).values(),
        }
        for container_name in names:
            _stop_container(client, container_name)
    for container_name in (ROI_WORKER_CONTAINER, MPS_CONTAINER):
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
