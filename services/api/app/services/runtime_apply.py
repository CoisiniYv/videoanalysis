from __future__ import annotations

import json
import logging
import os
import re
import socket
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import psycopg
from psycopg.rows import dict_row
from redis import Redis
import yaml

from app.config import get_settings
from app.services.replay_shards import (
    ReplayShardConfigError,
    ReplayShardMap,
    load_replay_shard_map,
)
from app.algorithm_registry import (
    get_algorithm_support,
    runtime_apply_state_for_algorithm,
)


DEFAULT_MODULE_CONFIG_PATH = "/app/modules/savant_security/config/cameras.midterm.yml"
DEFAULT_SOURCES_CONFIG_PATH = "/app/infra/generated/sources.generated.yml"
DEFAULT_ZMQ_ENDPOINT = "dealer+connect:tcp://replay-service:5555"
DEFAULT_REPLAY_API_URL = "http://replay-service:8080"
DEFAULT_REPLAY_JOB_SINK_URL = "dealer+connect:tcp://video-file-sink:6666"
DEFAULT_NETWORK = "video-analytics-midterm_default"
DEFAULT_ADAPTER_IMAGE = "ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0"
DEFAULT_SAVANT_CONTAINER = "video-analytics-midterm-savant"
DEFAULT_REPLAY_CONTAINER = "video-analytics-midterm-replay-service"
DEFAULT_FORWARDER_CONTAINER = "video-analytics-midterm-analysis-forwarder"
DEFAULT_COMPOSE_SOURCE_CONTAINER = "video-analytics-midterm-source-adapter"
DEFAULT_EVENT_WORKER_CONTAINER = "video-analytics-midterm-event-worker"
DEFAULT_FACE_WORKER_CONTAINER = "video-analytics-midterm-face-worker"
DEFAULT_VIDEO_SINK_CONTAINER = "video-analytics-midterm-video-file-sink"
DEFAULT_CLIP_WORKER_CONTAINER = "video-analytics-midterm-clip-worker"
DEFAULT_MEDIA_WORKER_CONTAINER = "video-analytics-midterm-media-worker"
DEFAULT_VIDEO_SINK_IMAGE = "ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0"
DEFAULT_VIDEO_SINK_NETWORK_ALIAS = "video-file-sink"
DEFAULT_EPOCH_ROOT = "/data/video-analytics/media/replay-sink-output/midterm"
DEFAULT_REDIS_EPOCH_KEY = "video_analytics:midterm:runtime_epoch"
DEFAULT_EPOCH_RESET_FRAME_CACHE_STREAMS = "security.frame_annotations"
DEFAULT_SAVANT_READY_TIMEOUT_S = 300.0
DEFAULT_SAVANT_READY_POLL_INTERVAL_S = 2.0
SOURCE_CONTAINER_PREFIX = "video-analytics-source-"
RUNTIME_EPOCH_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SOURCE_ID_MAX_LENGTH = 96
DEFAULT_SOURCE_RESTART_POLICY = "no"
DEFAULT_SOURCE_EOS_ON_START = True
SAVANT_READY_PATTERNS = (
    re.compile(r"\bPLAYING\b", re.IGNORECASE),
    re.compile(r"\bmodule\b.*\bstarted\b", re.IGNORECASE),
    re.compile(r"\bpipeline\b.*\bready\b", re.IGNORECASE),
    re.compile(r"\bpipeline\b.*\bstarted\b", re.IGNORECASE),
)
RUNTIME_RESTART_BLOCKING_EVIDENCE_STATES = (
    "pending",
    "waiting_proof",
    "queued",
    "replay_job_created",
    "replaying",
    "materializing",
    "finalizing",
)
RUNTIME_RESTART_TERMINAL_EVIDENCE_STATES = (
    "materialization_expired",
    "materialization_failed",
    "materialization_skipped",
)
DEFAULT_EVIDENCE_GUARD_LIMIT = 12
DEFAULT_EVIDENCE_GUARD_STALE_AFTER_S = 900.0

LOGGER = logging.getLogger(__name__)


class RuntimeApplyError(RuntimeError):
    pass


class RuntimeApplyBlockedError(RuntimeApplyError):
    def __init__(self, message: str, *, details: dict[str, Any], status_code: int = 409) -> None:
        super().__init__(message)
        self.details = details
        self.status_code = status_code


class DockerSocketClient:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        ok_statuses: set[int] | None = None,
    ) -> tuple[int, bytes]:
        ok_statuses = ok_statuses or {200, 201, 204, 304}
        payload = json.dumps(body).encode("utf-8") if body is not None else b""
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: docker\r\n"
            "Connection: close\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "\r\n"
        ).encode("utf-8") + payload
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(30)
            sock.connect(self.socket_path)
            sock.sendall(request)
            response = bytearray()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
        status, content = _parse_http_response(bytes(response))
        if status not in ok_statuses:
            detail = content.decode("utf-8", errors="replace")[:500]
            raise RuntimeApplyError(f"docker api {method} {path} returned {status}: {detail}")
        return status, content


def apply_camera_runtime(
    *,
    export_doc: dict[str, Any],
    cameras: list[dict[str, Any]],
    force: bool = False,
) -> dict[str, Any]:
    return _apply_camera_runtime_controlled(
        export_doc=export_doc,
        cameras=cameras,
        reason="camera_runtime_apply",
        action="apply",
        force=force,
    )


def restart_camera_runtime(
    *,
    export_doc: dict[str, Any],
    cameras: list[dict[str, Any]],
    force: bool = False,
) -> dict[str, Any]:
    return _apply_camera_runtime_controlled(
        export_doc=export_doc,
        cameras=cameras,
        reason="controlled_runtime_restart",
        action="restart",
        force=force,
    )


def sync_camera_runtime_config_and_sources(
    *,
    export_doc: dict[str, Any],
    cameras: list[dict[str, Any]],
) -> dict[str, Any]:
    """Sync the module config snapshot and converge source adapters.

    This path does not restart Savant or create a new runtime epoch. It is used
    by 8090 camera CRUD/enable/disable actions so the on-disk config snapshot
    follows the operator source of truth while source containers are reconciled.
    """

    module_config_path = Path(
        os.getenv("CAMERA_RUNTIME_MODULE_CONFIG_PATH", DEFAULT_MODULE_CONFIG_PATH)
    )
    result = converge_camera_sources(cameras=cameras)
    runtime_epoch_id = _runtime_epoch_id_for_source_only_sync(module_config_path)
    if runtime_epoch_id:
        export_doc = _with_runtime_epoch(export_doc, runtime_epoch_id)
    _write_yaml(module_config_path, export_doc)
    result["module_config_path"] = str(module_config_path)
    result["module_config_synced"] = True
    result["runtime_epoch_id_preserved"] = runtime_epoch_id
    return result


def converge_camera_sources(
    *,
    cameras: list[dict[str, Any]],
) -> dict[str, Any]:
    """Converge dynamic source-adapter containers without restarting Savant.

    This path is intentionally narrower than ``apply_camera_runtime``: it
    updates ``sources.generated.yml`` and reconciles per-source adapter
    containers. The compose-owned source is still reconciled by starting or
    stopping its fixed container when the matching camera is enabled/disabled.
    """
    if not _env_bool("CAMERA_RUNTIME_APPLY_ENABLED", default=False):
        raise RuntimeApplyError("camera runtime control is disabled")

    sources_config_path = Path(
        os.getenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", DEFAULT_SOURCES_CONFIG_PATH)
    )
    zmq_endpoint = os.getenv("CAMERA_RUNTIME_ZMQ_ENDPOINT", DEFAULT_ZMQ_ENDPOINT)
    replay_shards = _load_runtime_replay_shards(zmq_endpoint)
    adapter_image = os.getenv("CAMERA_RUNTIME_ADAPTER_IMAGE", DEFAULT_ADAPTER_IMAGE)
    network = os.getenv("CAMERA_RUNTIME_DOCKER_NETWORK", DEFAULT_NETWORK)
    savant_container = os.getenv("CAMERA_RUNTIME_SAVANT_CONTAINER", DEFAULT_SAVANT_CONTAINER)
    compose_source_id = os.getenv("CAMERA_RUNTIME_COMPOSE_SOURCE_ID", "primary_rtsp")
    compose_source_container = os.getenv(
        "CAMERA_RUNTIME_COMPOSE_SOURCE_CONTAINER",
        DEFAULT_COMPOSE_SOURCE_CONTAINER,
    )
    docker_socket = os.getenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/var/run/docker.sock")

    client = DockerSocketClient(docker_socket)
    sources_doc = _build_sources_doc(cameras, replay_shards=replay_shards)
    _write_yaml(sources_config_path, sources_doc)

    plan = _source_only_convergence_plan(
        client,
        sources_doc=sources_doc,
        compose_source_id=compose_source_id,
        compose_source_container=compose_source_container,
    )
    starts_required = any(
        item["planned_action"] in {
            "create_start",
            "recreate_start",
            "start",
            "start_compose_enabled",
        }
        for item in plan
    )
    savant_ready: dict[str, Any] = {
        "savant_ready": None,
        "savant_ready_wait_seconds": 0.0,
        "savant_ready_reason": "not_required",
        "savant_ready_attempts": 0,
    }
    if starts_required:
        savant_ready = _wait_for_savant_ready(
            client,
            savant_container,
            timeout_s=_env_float(
                "CAMERA_RUNTIME_SAVANT_READY_TIMEOUT_S",
                DEFAULT_SAVANT_READY_TIMEOUT_S,
            ),
            poll_interval_s=_env_float(
                "CAMERA_RUNTIME_SAVANT_READY_POLL_INTERVAL_S",
                DEFAULT_SAVANT_READY_POLL_INTERVAL_S,
            ),
        )
        if not savant_ready["savant_ready"]:
            raise RuntimeApplyError(
                "savant not ready for source convergence after "
                f"{savant_ready['savant_ready_wait_seconds']:.1f}s: "
                f"{savant_ready['savant_ready_reason']}"
            )

    lifecycle: list[dict[str, Any]] = []
    dynamic_sources_started: list[str] = []
    dynamic_sources_recreated: list[str] = []
    dynamic_sources_stopped: list[str] = []
    dynamic_sources_kept: list[str] = []
    compose_sources_started: list[str] = []
    compose_sources_stopped: list[str] = []
    compose_sources_kept: list[str] = []
    sources_skipped: list[str] = []

    for item in plan:
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        source_id = str(item.get("source_id") or source.get("source_id") or "")
        action = str(item["planned_action"])
        diag = dict(item)
        diag.pop("source", None)

        if action in {"remove_disabled", "remove_stale"}:
            container_name = str(item.get("container_name") or "")
            delete_status = _remove_container(client, container_name)
            diag.update(action="removed", delete_status=delete_status)
            dynamic_sources_stopped.append(source_id or container_name)
        elif action == "stop_compose_disabled":
            container_name = str(item.get("container_name") or "")
            _stop_container(client, container_name)
            diag.update(action="stopped", container_name=container_name)
            compose_sources_stopped.append(source_id or container_name)
        elif action == "start_compose_enabled":
            start_status = _start_container(client, str(item["container_name"]))
            diag.update(action="started", start_status=start_status)
            compose_sources_started.append(source_id)
        elif action == "start":
            start_status = _start_container(client, str(item["container_name"]))
            diag.update(action="started", start_status=start_status)
            dynamic_sources_started.append(source_id)
        elif action in {"create_start", "recreate_start"}:
            dynamic_diag = _recreate_rtsp_adapter(
                client,
                source_id=source_id,
                uri=str(source["uri"]),
                zmq_endpoint=str(source["zmq_endpoint"]),
                adapter_image=adapter_image,
                network=network,
            )
            diag.update(dynamic_diag, action="created_started")
            dynamic_sources_started.append(source_id)
            if action == "recreate_start":
                dynamic_sources_recreated.append(source_id)
        elif action == "keep":
            diag.update(action="kept")
            if item.get("compose_source"):
                compose_sources_kept.append(source_id)
            else:
                dynamic_sources_kept.append(source_id)
        else:
            diag.update(action="skipped")
            if source_id:
                sources_skipped.append(source_id)
        lifecycle.append(diag)

    return {
        "runtime_action": "source_converge",
        "sources_config_path": str(sources_config_path),
        "savant_container": savant_container,
        **savant_ready,
        "sources_total": len(sources_doc["sources"]),
        "dynamic_sources_started": dynamic_sources_started,
        "dynamic_sources_recreated": dynamic_sources_recreated,
        "dynamic_sources_stopped": dynamic_sources_stopped,
        "dynamic_sources_kept": dynamic_sources_kept,
        "compose_sources_started": compose_sources_started,
        "compose_sources_stopped": compose_sources_stopped,
        "compose_sources_kept": compose_sources_kept,
        "sources_skipped": sources_skipped,
        "source_lifecycle": lifecycle,
        "replay_shards": replay_shards.to_dict(),
    }


def check_runtime_restart_evidence_guard(
    *,
    action: str,
    force: bool = False,
    limit: int = DEFAULT_EVIDENCE_GUARD_LIMIT,
) -> dict[str, Any]:
    """Block full runtime restarts while evidence tasks are still active."""

    blocking_states = list(RUNTIME_RESTART_BLOCKING_EVIDENCE_STATES)
    guard: dict[str, Any] = {
        "ok": True,
        "blocked": False,
        "forced": False,
        "action": action,
        "active_count": 0,
        "stale_count": 0,
        "blocking_states": blocking_states,
        "tasks": [],
        "stale_tasks": [],
    }
    if not _env_bool("CAMERA_RUNTIME_EVIDENCE_GUARD_ENABLED", default=True):
        return {
            **guard,
            "skipped": True,
            "skip_reason": "camera_runtime_evidence_guard_disabled",
        }

    try:
        active = _active_evidence_tasks_snapshot(
            limit=limit,
            states=blocking_states,
            stale_after_s=_env_float(
                "CAMERA_RUNTIME_EVIDENCE_GUARD_STALE_AFTER_S",
                DEFAULT_EVIDENCE_GUARD_STALE_AFTER_S,
            ),
        )
    except Exception as exc:
        details = {
            **guard,
            "ok": False,
            "blocked": not force,
            "guard_unavailable": True,
            "error": f"{type(exc).__name__}: {exc}",
            "force_parameter": "force=true",
        }
        if force:
            return {**details, "ok": True, "blocked": False, "forced": True}
        raise RuntimeApplyBlockedError(
            "runtime evidence guard unavailable; refusing to interrupt evidence workers",
            details=details,
            status_code=503,
        ) from exc

    guard.update(active)
    if int(guard.get("active_count") or 0) <= 0:
        return guard
    if force:
        return {**guard, "blocked": False, "forced": True}
    raise RuntimeApplyBlockedError(
        "runtime restart blocked because evidence tasks are still active",
        details={
            **guard,
            "ok": False,
            "blocked": True,
            "force_parameter": "force=true",
        },
        status_code=409,
    )


def _apply_camera_runtime_controlled(
    *,
    export_doc: dict[str, Any],
    cameras: list[dict[str, Any]],
    reason: str,
    action: str,
    force: bool,
) -> dict[str, Any]:
    if not _env_bool("CAMERA_RUNTIME_APPLY_ENABLED", default=False):
        raise RuntimeApplyError("camera runtime control is disabled")

    evidence_guard = check_runtime_restart_evidence_guard(action=action, force=force)

    module_config_path = Path(
        os.getenv("CAMERA_RUNTIME_MODULE_CONFIG_PATH", DEFAULT_MODULE_CONFIG_PATH)
    )
    sources_config_path = Path(
        os.getenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", DEFAULT_SOURCES_CONFIG_PATH)
    )
    zmq_endpoint = os.getenv("CAMERA_RUNTIME_ZMQ_ENDPOINT", DEFAULT_ZMQ_ENDPOINT)
    replay_shards = _load_runtime_replay_shards(zmq_endpoint)
    adapter_image = os.getenv("CAMERA_RUNTIME_ADAPTER_IMAGE", DEFAULT_ADAPTER_IMAGE)
    network = os.getenv("CAMERA_RUNTIME_DOCKER_NETWORK", DEFAULT_NETWORK)
    savant_container = os.getenv("CAMERA_RUNTIME_SAVANT_CONTAINER", DEFAULT_SAVANT_CONTAINER)
    replay_container = os.getenv("CAMERA_RUNTIME_REPLAY_CONTAINER", DEFAULT_REPLAY_CONTAINER)
    forwarder_container = os.getenv(
        "CAMERA_RUNTIME_FORWARDER_CONTAINER",
        DEFAULT_FORWARDER_CONTAINER,
    )
    compose_source_id = os.getenv("CAMERA_RUNTIME_COMPOSE_SOURCE_ID", "primary_rtsp")
    compose_source_container = os.getenv(
        "CAMERA_RUNTIME_COMPOSE_SOURCE_CONTAINER",
        DEFAULT_COMPOSE_SOURCE_CONTAINER,
    )
    docker_socket = os.getenv("CAMERA_RUNTIME_DOCKER_SOCKET", "/var/run/docker.sock")
    event_worker_container = os.getenv(
        "CAMERA_RUNTIME_EVENT_WORKER_CONTAINER",
        DEFAULT_EVENT_WORKER_CONTAINER,
    )
    face_worker_container = os.getenv(
        "CAMERA_RUNTIME_FACE_WORKER_CONTAINER",
        DEFAULT_FACE_WORKER_CONTAINER,
    )
    video_sink_container = os.getenv(
        "CAMERA_RUNTIME_VIDEO_SINK_CONTAINER",
        DEFAULT_VIDEO_SINK_CONTAINER,
    )
    clip_worker_container = os.getenv(
        "CAMERA_RUNTIME_CLIP_WORKER_CONTAINER",
        DEFAULT_CLIP_WORKER_CONTAINER,
    )
    media_worker_container = os.getenv(
        "CAMERA_RUNTIME_MEDIA_WORKER_CONTAINER",
        DEFAULT_MEDIA_WORKER_CONTAINER,
    )

    client = DockerSocketClient(docker_socket)
    sources_doc = _build_sources_doc(cameras, replay_shards=replay_shards)
    source_containers = _source_adapter_containers(
        client,
        sources_doc=sources_doc,
        compose_source_id=compose_source_id,
        compose_source_container=compose_source_container,
    )
    worker_containers = _dedupe(
        [
            event_worker_container,
            face_worker_container,
            clip_worker_container,
            media_worker_container,
        ]
    )

    _stop_containers(client, source_containers)
    _stop_containers(client, worker_containers)

    epoch_state = create_runtime_epoch_state(reason=reason)
    runtime_epoch_id = str(epoch_state["runtime_epoch_id"])
    export_doc = _with_runtime_epoch(export_doc, runtime_epoch_id)
    _write_yaml(module_config_path, export_doc)
    _write_yaml(sources_config_path, sources_doc)

    _recreate_video_file_sink(
        client,
        container_name=video_sink_container,
        runtime_epoch_id=runtime_epoch_id,
        network=network,
    )
    _restart_container(client, replay_container)
    _restart_container(client, forwarder_container)
    _restart_container(client, savant_container)
    savant_ready = _wait_for_savant_ready(
        client,
        savant_container,
        timeout_s=_env_float(
            "CAMERA_RUNTIME_SAVANT_READY_TIMEOUT_S",
            DEFAULT_SAVANT_READY_TIMEOUT_S,
        ),
        poll_interval_s=_env_float(
            "CAMERA_RUNTIME_SAVANT_READY_POLL_INTERVAL_S",
            DEFAULT_SAVANT_READY_POLL_INTERVAL_S,
        ),
    )
    if not savant_ready["savant_ready"]:
        raise RuntimeApplyError(
            "savant not ready after "
            f"{savant_ready['savant_ready_wait_seconds']:.1f}s: "
            f"{savant_ready['savant_ready_reason']}"
        )
    _start_containers(client, worker_containers)

    runtime_rule_report = _runtime_rule_report(export_doc, cameras)

    started_sources: list[str] = []
    compose_sources_started: list[str] = []
    skipped_sources: list[str] = []
    source_lifecycle: list[dict[str, Any]] = []
    for source in sources_doc["sources"].values():
        source_id = str(source.get("source_id", ""))
        base_diag = _source_lifecycle_base(source, compose_source_id=compose_source_id)
        if not source.get("enabled"):
            skipped_sources.append(source_id)
            source_lifecycle.append({**base_diag, "action": "skipped", "skip_reason": "disabled"})
            continue
        if source.get("adapter_type") != "gstreamer":
            skipped_sources.append(source_id)
            source_lifecycle.append(
                {**base_diag, "action": "skipped", "skip_reason": "unsupported_adapter"}
            )
            continue
        uri = str(source.get("uri") or "")
        if not uri.startswith(("rtsp://", "rtsps://")):
            skipped_sources.append(source_id)
            source_lifecycle.append(
                {**base_diag, "action": "skipped", "skip_reason": "non_rtsp_uri"}
            )
            continue
        if source_id == compose_source_id:
            start_status = _start_container(client, compose_source_container)
            compose_sources_started.append(source_id)
            source_lifecycle.append(
                {
                    **base_diag,
                    "action": "started",
                    "container_name": compose_source_container,
                    "compose_source": True,
                    "dynamic_source": False,
                    "start_status": start_status,
                }
            )
            LOGGER.info("started compose source adapter source_id=%s", source_id)
            continue
        dynamic_diag = _recreate_rtsp_adapter(
            client,
            source_id=source_id,
            uri=uri,
            zmq_endpoint=str(source["zmq_endpoint"]),
            adapter_image=adapter_image,
            network=network,
        )
        started_sources.append(source_id)
        source_lifecycle.append({**base_diag, **dynamic_diag, "action": "created_started"})
        LOGGER.info("created dynamic source adapter source_id=%s", source_id)
    return {
        "runtime_action": action,
        "module_config_path": str(module_config_path),
        "sources_config_path": str(sources_config_path),
        "runtime_epoch_id": runtime_epoch_id,
        "runtime_epoch": epoch_state,
        "runtime_epoch_state_path": str(_runtime_epoch_state_path()),
        "runtime_epoch_root": str(_runtime_epoch_root()),
        "evidence_restart_guard": evidence_guard,
        **savant_ready,
        "redis_frame_cache_streams_reset": list(
            epoch_state.get("redis_frame_cache_streams_reset") or []
        ),
        "redis_frame_cache_reset_count": int(
            epoch_state.get("redis_frame_cache_reset_count") or 0
        ),
        "video_sink_container": video_sink_container,
        "video_sink_dir_location": _video_sink_dir_location(runtime_epoch_id),
        "sources_total": len(sources_doc["sources"]),
        "source_containers_stopped": source_containers,
        "compose_sources_started": compose_sources_started,
        "dynamic_sources_started": started_sources,
        "sources_skipped": skipped_sources,
        "source_lifecycle": source_lifecycle,
        "workers_restarted": worker_containers,
        "replay_restarted": replay_container,
        "forwarder_restarted": forwarder_container,
        "savant_restarted": savant_container,
        "camera_ids": runtime_rule_report["camera_ids"],
        "source_ids": runtime_rule_report["source_ids"],
        "applied_cameras": runtime_rule_report["applied_cameras"],
        "configured_rules": runtime_rule_report["configured_rules"],
        "enabled_rules": runtime_rule_report["enabled_rules"],
        "applied_rules": runtime_rule_report["applied_rules"],
        "skipped_rules": runtime_rule_report["skipped_rules"],
        "unsupported_rules": runtime_rule_report["unsupported_rules"],
        "replay_shards": replay_shards.to_dict(),
        "management_containers_preserved": [
            os.getenv("CAMERA_RUNTIME_API_CONTAINER", "video-analytics-midterm-api"),
            os.getenv(
                "CAMERA_RUNTIME_VIEWER_CONTAINER",
                "video-analytics-midterm-evidence-viewer",
            ),
        ],
    }


def _runtime_rule_report(
    export_doc: dict[str, Any],
    cameras: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]] | list[str]]:
    camera_rows = export_doc.get("cameras") if isinstance(export_doc, dict) else {}
    camera_map = {
        str(camera.get("id") or camera.get("camera_id") or ""): camera
        for camera in cameras
        if isinstance(camera, dict)
    }
    applied_cameras: list[dict[str, Any]] = []
    configured_rules: list[dict[str, Any]] = []
    enabled_rules: list[dict[str, Any]] = []
    applied_rules: list[dict[str, Any]] = []
    skipped_rules: list[dict[str, Any]] = []
    unsupported_rules: list[dict[str, Any]] = []
    camera_ids: list[str] = []
    source_ids: list[str] = []

    if not isinstance(camera_rows, dict):
        return {
            "camera_ids": [],
            "source_ids": [],
            "applied_cameras": [],
            "configured_rules": [],
            "enabled_rules": [],
            "applied_rules": [],
            "skipped_rules": [],
            "unsupported_rules": [],
        }

    for camera_id, camera_doc in camera_rows.items():
        if not isinstance(camera_doc, dict):
            continue
        camera_id = str(camera_id or camera_doc.get("camera_id") or "")
        source_id = str(camera_doc.get("source_id") or "")
        camera_enabled = bool(camera_doc.get("enabled", True))
        runtime_camera = camera_map.get(camera_id, {})
        source_enabled = bool(runtime_camera.get("enabled", True))
        camera_ids.append(camera_id)
        if source_id:
            source_ids.append(source_id)
        summary = {
            "camera_id": camera_id,
            "source_id": source_id,
            "enabled": camera_enabled,
            "source_enabled": source_enabled,
            "applied_rule_ids": [],
            "skipped_rule_ids": [],
            "unsupported_rule_ids": [],
            "configured_rule_count": 0,
            "enabled_rule_count": 0,
            "applied_rule_count": 0,
            "skipped_rule_count": 0,
            "unsupported_rule_count": 0,
        }
        rules = camera_doc.get("rules") if isinstance(camera_doc.get("rules"), dict) else {}
        for rule_id, rule in rules.items():
            if not isinstance(rule, dict):
                continue
            algorithm_id = str(rule.get("algorithm_id") or rule.get("rule_type") or "")
            rule_enabled = bool(rule.get("enabled", True))
            state = runtime_apply_state_for_algorithm(
                algorithm_id,
                rule_enabled=rule_enabled,
                camera_enabled=camera_enabled and source_enabled,
            )
            support = get_algorithm_support(algorithm_id)
            row = {
                "camera_id": camera_id,
                "source_id": source_id,
                "rule_id": str(rule_id or rule.get("rule_id") or ""),
                "algorithm_id": algorithm_id,
                "rule_type": str(rule.get("rule_type") or ""),
                "enabled": rule_enabled,
                "support_status": state["support_status"],
                "support_status_reason": state["support_status_reason"],
                "runtime_apply_state": state["runtime_apply_state"],
                "runtime_consumed": state["runtime_consumed"],
                "runtime_skip_reason": state["runtime_skip_reason"],
                "rule_kind": rule.get("rule_kind"),
                "zone_id": rule.get("zone_id"),
                "line_id": rule.get("line_id"),
            }
            if support is not None:
                row.update(
                    {
                        "display_name": support.display_name,
                        "category": support.category,
                        "configurable": support.configurable,
                        "per_camera_gate": support.per_camera_gate,
                        "runtime_detecting": support.runtime_detecting,
                        "event_enabled": support.event_enabled,
                        "evidence_enabled": support.evidence_enabled,
                        "production_ready": support.production_ready,
                        "requires_runtime_apply": support.requires_runtime_apply,
                    }
                )
            configured_rules.append(row)
            summary["configured_rule_count"] += 1
            if rule_enabled:
                enabled_rules.append(row)
                summary["enabled_rule_count"] += 1
            if state["runtime_apply_state"] == "applied":
                applied_rules.append(row)
                summary["applied_rule_ids"].append(str(row["rule_id"]))
                summary["applied_rule_count"] += 1
            elif state["runtime_apply_state"] == "unsupported":
                unsupported_rules.append(row)
                summary["unsupported_rule_ids"].append(str(row["rule_id"]))
                summary["unsupported_rule_count"] += 1
            else:
                skipped_rules.append(row)
                summary["skipped_rule_ids"].append(str(row["rule_id"]))
                summary["skipped_rule_count"] += 1
        applied_cameras.append(summary)

    return {
        "camera_ids": camera_ids,
        "source_ids": source_ids,
        "applied_cameras": applied_cameras,
        "configured_rules": configured_rules,
        "enabled_rules": enabled_rules,
        "applied_rules": applied_rules,
        "skipped_rules": skipped_rules,
        "unsupported_rules": unsupported_rules,
    }


def _active_evidence_tasks_snapshot(
    *,
    limit: int,
    states: list[str],
    stale_after_s: float,
) -> dict[str, Any]:
    bounded_limit = max(1, min(int(limit or DEFAULT_EVIDENCE_GUARD_LIMIT), 100))
    stale_after_s = max(1.0, float(stale_after_s or DEFAULT_EVIDENCE_GUARD_STALE_AFTER_S))
    with psycopg.connect(
        get_settings().database_url,
        row_factory=dict_row,
        autocommit=True,
        connect_timeout=1,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH base AS (
                    SELECT
                        et.task_id,
                        et.event_id::text AS event_id,
                        et.source_event_id,
                        et.camera_id,
                        et.source_id,
                        et.event_type,
                        et.status,
                        et.materialization_status,
                        et.created_at,
                        et.updated_at,
                        et.error_message,
                        e.payload->'media'->>'evidence_state' AS event_evidence_state,
                        e.media_status,
                        e.payload->'media'->>'clip_status' AS event_clip_status,
                        (
                            et.status = ANY(%(terminal_states)s::text[])
                            OR et.materialization_status = ANY(%(terminal_states)s::text[])
                            OR e.payload->'media'->>'evidence_state' = ANY(%(terminal_states)s::text[])
                            OR e.media_status = ANY(%(terminal_states)s::text[])
                        ) AS terminal_evidence_state,
                        (
                            SELECT max(deadline_at)
                            FROM (
                                VALUES
                                    (et.materialization_deadline_at),
                                    (et.replay_deadline_at),
                                    (et.annotation_deadline_at)
                            ) AS deadlines(deadline_at)
                        ) AS latest_deadline_at
                    FROM evidence_tasks et
                    LEFT JOIN events e ON e.id = et.event_id
                ),
                candidates AS (
                    SELECT
                        *,
                        CASE
                            WHEN terminal_evidence_state THEN NULL
                            WHEN status = ANY(%(states)s::text[]) THEN status
                            WHEN materialization_status = ANY(%(states)s::text[])
                                THEN materialization_status
                            WHEN event_evidence_state = ANY(%(states)s::text[])
                                THEN event_evidence_state
                            WHEN media_status = ANY(%(states)s::text[]) THEN media_status
                            WHEN event_clip_status = ANY(%(states)s::text[])
                                THEN event_clip_status
                            ELSE NULL
                        END AS blocking_state
                    FROM base
                    WHERE
                        status = ANY(%(states)s::text[])
                        OR materialization_status = ANY(%(states)s::text[])
                        OR (
                            NOT terminal_evidence_state
                            AND (
                                event_evidence_state = ANY(%(states)s::text[])
                                OR media_status = ANY(%(states)s::text[])
                                OR event_clip_status = ANY(%(states)s::text[])
                            )
                        )
                ),
                marked AS (
                    SELECT
                        *,
                        (
                            COALESCE(updated_at, created_at) < now() - (%(stale_after_s)s * interval '1 second')
                            AND (latest_deadline_at IS NULL OR latest_deadline_at < now())
                        ) AS stale
                    FROM candidates
                    WHERE blocking_state IS NOT NULL
                )
                SELECT
                    *,
                    COUNT(*) FILTER (WHERE NOT stale) OVER() AS active_count,
                    COUNT(*) FILTER (WHERE stale) OVER() AS stale_count,
                    EXTRACT(EPOCH FROM (now() - COALESCE(updated_at, created_at))) AS age_seconds
                FROM marked
                ORDER BY stale ASC, COALESCE(updated_at, created_at) DESC, task_id DESC
                LIMIT %(limit)s
                """,
                {
                    "states": states,
                    "terminal_states": list(RUNTIME_RESTART_TERMINAL_EVIDENCE_STATES),
                    "limit": bounded_limit,
                    "stale_after_s": stale_after_s,
                },
            )
            rows = cur.fetchall()

    active_count = int(rows[0]["active_count"]) if rows else 0
    stale_count = int(rows[0]["stale_count"]) if rows else 0
    tasks = [_evidence_guard_task(row) for row in rows if not row.get("stale")]
    stale_tasks = [_evidence_guard_task(row) for row in rows if row.get("stale")]
    return {
        "active_count": active_count,
        "stale_count": stale_count,
        "stale_after_s": stale_after_s,
        "tasks": tasks,
        "stale_tasks": stale_tasks,
    }


def _evidence_guard_task(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": _text(row.get("task_id")),
        "event_id": _text(row.get("event_id")),
        "source_event_id": _text(row.get("source_event_id")),
        "camera_id": _text(row.get("camera_id")),
        "source_id": _text(row.get("source_id")),
        "event_type": _text(row.get("event_type")),
        "status": _text(row.get("status")),
        "materialization_status": _text(row.get("materialization_status")),
        "event_evidence_state": _text(row.get("event_evidence_state")),
        "media_status": _text(row.get("media_status")),
        "event_clip_status": _text(row.get("event_clip_status")),
        "blocking_state": _text(row.get("blocking_state")),
        "latest_deadline_at": _datetime_text(row.get("latest_deadline_at")),
        "stale": bool(row.get("stale")),
        "created_at": _datetime_text(row.get("created_at")),
        "updated_at": _datetime_text(row.get("updated_at")),
        "age_seconds": _float_or_none(row.get("age_seconds")),
        "error_message": _text(row.get("error_message")),
    }


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _datetime_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return _text(value)


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _load_runtime_replay_shards(default_zmq_endpoint: str) -> ReplayShardMap:
    try:
        return load_replay_shard_map(
            default_replay_api_url=os.getenv("REPLAY_API_URL", DEFAULT_REPLAY_API_URL),
            default_in_stream_endpoint=default_zmq_endpoint,
            default_replay_job_sink_url=os.getenv(
                "REPLAY_JOB_SINK_URL",
                DEFAULT_REPLAY_JOB_SINK_URL,
            ),
        )
    except ReplayShardConfigError as exc:
        raise RuntimeApplyError(str(exc)) from exc


def _build_sources_doc(
    cameras: list[dict[str, Any]],
    *,
    replay_shards: ReplayShardMap,
) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    for camera in cameras:
        camera_id = str(camera["id"])
        source_id = validate_source_id(str(camera["source_id"]))
        enabled = bool(camera.get("enabled", True))
        try:
            replay_shard = replay_shards.shard_for_source(source_id)
        except ReplayShardConfigError:
            if enabled:
                raise
            replay_shard = replay_shards.shard_by_id(replay_shards.default_shard_id)
        source = {
            "camera_id": camera_id,
            "source_id": source_id,
            "uri": str(camera["rtsp_url"]),
            "enabled": enabled,
            "adapter_type": "gstreamer",
            "zmq_endpoint": replay_shard.in_stream_endpoint,
            "replay_shard_id": replay_shard.shard_id,
        }
        camera_name = str(camera.get("name") or "")
        if camera_name:
            source["camera_name"] = camera_name
        sources[camera_id] = source
    return {"sources": sources}


def validate_source_id(value: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."}:
        raise RuntimeApplyError(f"unsafe source_id: {value!r}")
    if len(text) > SOURCE_ID_MAX_LENGTH:
        raise RuntimeApplyError(f"unsafe source_id length: {value!r}")
    if not SOURCE_ID_RE.fullmatch(text):
        raise RuntimeApplyError(f"unsafe source_id: {value!r}")
    return text


def generate_runtime_epoch_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"midterm-{stamp}-{secrets.token_hex(4)}"


def validate_runtime_epoch_id(value: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise RuntimeApplyError(f"unsafe runtime_epoch_id: {value!r}")
    if not RUNTIME_EPOCH_RE.fullmatch(text):
        raise RuntimeApplyError(f"unsafe runtime_epoch_id: {value!r}")
    return text


def create_runtime_epoch_state(
    *,
    reason: str,
    created_by: str = "api.runtime_apply",
    runtime_epoch_id: str | None = None,
) -> dict[str, Any]:
    epoch_id = validate_runtime_epoch_id(runtime_epoch_id or generate_runtime_epoch_id())
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    state_path = _runtime_epoch_state_path()
    previous = _read_runtime_epoch_state(state_path)
    state = {
        "runtime_epoch_id": epoch_id,
        "created_at": created_at,
        "created_by": created_by,
        "reason": reason,
        "previous_runtime_epoch_id": previous.get("runtime_epoch_id", ""),
    }
    epoch_dir = _runtime_epoch_root() / "epochs" / epoch_id
    epoch_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(state_path, state)
    try:
        redis_client = Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
        redis_client.set(
            os.getenv("RUNTIME_EPOCH_REDIS_KEY", DEFAULT_REDIS_EPOCH_KEY),
            epoch_id,
        )
        state["redis_epoch_published"] = True
        reset_streams, reset_count = _reset_runtime_epoch_frame_cache(redis_client)
        state["redis_frame_cache_streams_reset"] = reset_streams
        state["redis_frame_cache_reset_count"] = reset_count
    except Exception:
        state["redis_epoch_published"] = False
        state["redis_frame_cache_streams_reset"] = []
        state["redis_frame_cache_reset_count"] = 0
    _atomic_write_json(state_path, state)
    return state


def _reset_runtime_epoch_frame_cache(redis_client: Any) -> tuple[list[str], int]:
    streams = _runtime_epoch_frame_cache_streams()
    if not streams:
        return [], 0
    deleted = redis_client.delete(*streams)
    try:
        deleted_count = int(deleted or 0)
    except (TypeError, ValueError):
        deleted_count = 0
    return streams, deleted_count


def _runtime_epoch_frame_cache_streams() -> list[str]:
    raw = os.getenv(
        "RUNTIME_EPOCH_RESET_FRAME_CACHE_STREAMS",
        DEFAULT_EPOCH_RESET_FRAME_CACHE_STREAMS,
    )
    streams: list[str] = []
    for item in str(raw or "").split(","):
        stream = item.strip()
        if stream and stream not in streams:
            streams.append(stream)
    return streams


def _runtime_epoch_root() -> Path:
    return Path(os.getenv("RUNTIME_EPOCH_ROOT", DEFAULT_EPOCH_ROOT))


def _runtime_epoch_state_path() -> Path:
    return Path(
        os.getenv(
            "RUNTIME_EPOCH_STATE_PATH",
            str(_runtime_epoch_root() / ".current_epoch.json"),
        )
    )


def _read_runtime_epoch_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise RuntimeApplyError(f"failed to read runtime epoch state: {exc}") from exc


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _with_runtime_epoch(export_doc: dict[str, Any], runtime_epoch_id: str) -> dict[str, Any]:
    updated = dict(export_doc)
    updated["runtime_epoch_id"] = runtime_epoch_id
    cameras = updated.get("cameras")
    if isinstance(cameras, dict):
        updated_cameras: dict[str, Any] = {}
        for camera_id, camera in cameras.items():
            if isinstance(camera, dict):
                camera_doc = dict(camera)
                camera_doc["runtime_epoch_id"] = runtime_epoch_id
                updated_cameras[camera_id] = camera_doc
            else:
                updated_cameras[camera_id] = camera
        updated["cameras"] = updated_cameras
    return updated


def _runtime_epoch_id_for_source_only_sync(module_config_path: Path) -> str:
    for candidate in (
        _read_runtime_epoch_id_from_yaml(module_config_path),
        str(_read_runtime_epoch_state(_runtime_epoch_state_path()).get("runtime_epoch_id") or ""),
    ):
        if not candidate:
            continue
        return validate_runtime_epoch_id(candidate)
    return ""


def _read_runtime_epoch_id_from_yaml(path: Path) -> str:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return ""
    except Exception as exc:
        raise RuntimeApplyError(f"failed to read runtime module config: {exc}") from exc
    if not isinstance(data, dict):
        return ""
    return str(data.get("runtime_epoch_id") or "")


def _stop_container(client: DockerSocketClient, container_name: str) -> None:
    if not container_name:
        return
    client.request(
        "POST",
        f"/containers/{quote(container_name, safe='')}/stop?t=10",
        ok_statuses={204, 304, 404},
    )


def _remove_container(client: DockerSocketClient, container_name: str) -> int | None:
    if not container_name:
        return None
    status, _ = client.request(
        "DELETE",
        f"/containers/{quote(container_name, safe='')}?force=true",
        ok_statuses={204, 404},
    )
    return status


def _stop_containers(client: DockerSocketClient, container_names: list[str]) -> None:
    for container_name in container_names:
        _stop_container(client, container_name)


def _start_container(client: DockerSocketClient, container_name: str) -> int | None:
    if not container_name:
        return None
    status, _body = client.request(
        "POST",
        f"/containers/{quote(container_name, safe='')}/start",
        ok_statuses={204, 304, 404},
    )
    return status


def _start_containers(client: DockerSocketClient, container_names: list[str]) -> None:
    for container_name in container_names:
        _start_container(client, container_name)


def _restart_container(client: DockerSocketClient, container_name: str) -> None:
    if not container_name:
        return
    client.request(
        "POST",
        f"/containers/{quote(container_name, safe='')}/restart?t=10",
        ok_statuses={204, 304, 404},
    )


def _wait_for_savant_ready(
    client: DockerSocketClient,
    container_name: str,
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> dict[str, Any]:
    timeout_s = max(0.0, float(timeout_s))
    poll_interval_s = max(0.0, float(poll_interval_s))
    started = time.monotonic()
    deadline = started + timeout_s
    last_reason = "no readiness signal observed"
    attempts = 0

    while True:
        attempts += 1
        inspect_doc = _inspect_container(client, container_name)
        logs = _container_logs(
            client,
            container_name,
            since_epoch=_container_started_at_epoch(inspect_doc),
        )
        ready_reason = _savant_ready_reason_from_logs(logs)
        if ready_reason:
            return _savant_ready_result(True, started, ready_reason, attempts)
        if logs:
            last_reason = "logs_without_ready_marker"

        health = _container_health_status(inspect_doc)
        if health == "healthy":
            return _savant_ready_result(
                True,
                started,
                f"docker_health:{health}",
                attempts,
            )
        if health:
            last_reason = f"docker_health:{health}"

        if time.monotonic() >= deadline:
            return _savant_ready_result(False, started, last_reason, attempts)
        time.sleep(min(poll_interval_s, max(0.0, deadline - time.monotonic())))


def _savant_ready_result(
    ready: bool,
    started_monotonic: float,
    reason: str,
    attempts: int,
) -> dict[str, Any]:
    return {
        "savant_ready": ready,
        "savant_ready_wait_seconds": round(max(0.0, time.monotonic() - started_monotonic), 3),
        "savant_ready_reason": reason,
        "savant_ready_attempts": attempts,
    }


def _inspect_container(client: DockerSocketClient, container_name: str) -> dict[str, Any]:
    if not container_name:
        return {}
    try:
        _status, body = client.request(
            "GET",
            f"/containers/{quote(container_name, safe='')}/json",
            ok_statuses={200, 404},
        )
        doc = json.loads(body.decode("utf-8") or "{}")
        return doc if isinstance(doc, dict) else {}
    except Exception as exc:
        LOGGER.warning("failed to inspect container %s: %s", container_name, exc)
        return {}


def _container_health_status(inspect_doc: dict[str, Any]) -> str:
    state = inspect_doc.get("State") if isinstance(inspect_doc, dict) else {}
    health = state.get("Health") if isinstance(state, dict) else {}
    status = health.get("Status") if isinstance(health, dict) else ""
    return str(status or "")


def _container_started_at_epoch(inspect_doc: dict[str, Any]) -> int | None:
    state = inspect_doc.get("State") if isinstance(inspect_doc, dict) else {}
    raw = str(state.get("StartedAt") or "") if isinstance(state, dict) else ""
    if not raw or raw.startswith("0001-01-01"):
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        if "." in raw:
            prefix, suffix = raw.split(".", 1)
            fraction, zone = suffix, ""
            for marker in ("+", "-"):
                if marker in suffix:
                    fraction, zone = suffix.split(marker, 1)
                    zone = marker + zone
                    break
            raw = f"{prefix}.{fraction[:6]}{zone}"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int(parsed.timestamp()))
    except Exception:
        return None


def _container_logs(
    client: DockerSocketClient,
    container_name: str,
    *,
    since_epoch: int | None = None,
) -> str:
    if not container_name:
        return ""
    since_query = f"&since={since_epoch}" if since_epoch is not None else ""
    try:
        _status, body = client.request(
            "GET",
            f"/containers/{quote(container_name, safe='')}/logs?stdout=1&stderr=1&timestamps=1&tail=200{since_query}",
            ok_statuses={200, 404},
        )
        return body.decode("utf-8", errors="replace")
    except Exception as exc:
        LOGGER.warning("failed to read logs for container %s: %s", container_name, exc)
        return ""


def _savant_ready_reason_from_logs(logs: str) -> str:
    for pattern in SAVANT_READY_PATTERNS:
        match = pattern.search(logs or "")
        if match:
            return f"log:{match.group(0)[:80]}"
    return ""


def _source_adapter_containers(
    client: DockerSocketClient,
    *,
    sources_doc: dict[str, Any],
    compose_source_id: str,
    compose_source_container: str,
) -> list[str]:
    containers: list[str] = [compose_source_container]
    for source in sources_doc.get("sources", {}).values():
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id") or "")
        if not source_id or source_id == compose_source_id:
            continue
        containers.append(SOURCE_CONTAINER_PREFIX + source_id)
    containers.extend(_discover_source_adapter_containers(client))
    return _dedupe(containers)


def _discover_source_adapter_containers(client: DockerSocketClient) -> list[str]:
    try:
        _, body = client.request("GET", "/containers/json?all=true", ok_statuses={200})
        containers = json.loads(body.decode("utf-8") or "[]")
    except Exception:
        return []
    names: list[str] = []
    for container in containers if isinstance(containers, list) else []:
        for raw_name in container.get("Names") or []:
            name = str(raw_name).lstrip("/")
            if name.startswith(SOURCE_CONTAINER_PREFIX):
                names.append(name)
    return names


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _video_sink_dir_location(runtime_epoch_id: str) -> str:
    return (
        "/media/replay-sink-output/midterm/epochs/"
        f"{validate_runtime_epoch_id(runtime_epoch_id)}/%source_id%/%src_filename%/"
    )


def _source_lifecycle_base(
    source: dict[str, Any],
    *,
    compose_source_id: str,
) -> dict[str, Any]:
    source_id = str(source.get("source_id") or "")
    uri = str(source.get("uri") or "")
    base = {
        "source_id": source_id,
        "camera_id": str(source.get("camera_id") or ""),
        "adapter_type": str(source.get("adapter_type") or ""),
        "enabled": bool(source.get("enabled")),
        "uri_host": _rtsp_uri_host(uri),
        "compose_source": source_id == compose_source_id,
        "dynamic_source": source_id != compose_source_id,
        "ffmpeg_timeout_ms": 20000 if uri.startswith(("rtsp://", "rtsps://")) else None,
        "restart_policy": (
            _source_restart_policy_name() if uri.startswith(("rtsp://", "rtsps://")) else ""
        ),
        "eos_on_start": _source_eos_on_start(),
    }
    camera_name = str(source.get("camera_name") or "")
    if camera_name:
        base["camera_name"] = camera_name
    return base


def _rtsp_uri_host(uri: str) -> str:
    try:
        return urlsplit(uri).hostname or ""
    except Exception:
        return ""


def _recreate_video_file_sink(
    client: DockerSocketClient,
    *,
    container_name: str,
    runtime_epoch_id: str,
    network: str,
) -> None:
    encoded_name = quote(container_name, safe="")
    network_alias = os.getenv(
        "CAMERA_RUNTIME_VIDEO_SINK_NETWORK_ALIAS",
        DEFAULT_VIDEO_SINK_NETWORK_ALIAS,
    ) or DEFAULT_VIDEO_SINK_NETWORK_ALIAS
    client.request(
        "DELETE",
        f"/containers/{encoded_name}?force=true",
        ok_statuses={204, 404},
    )
    body = {
        "Image": os.getenv("CAMERA_RUNTIME_VIDEO_SINK_IMAGE", DEFAULT_VIDEO_SINK_IMAGE),
        "Entrypoint": ["python", "/opt/savant/adapters/gst/sinks/video_files.py"],
        "Env": [
            f"LOGLEVEL={os.getenv('LOGLEVEL', 'INFO')}",
            "ZMQ_ENDPOINT=router+bind:tcp://0.0.0.0:6666",
            f"DIR_LOCATION={_video_sink_dir_location(runtime_epoch_id)}",
            "CHUNK_SIZE=0",
            "METADATA_JSON_FORMAT=native",
        ],
        "HostConfig": {
            "Binds": [
                f"{os.getenv('MEDIA_ROOT', '/data/video-analytics/media')}:/media:rw",
            ],
            "NetworkMode": network,
            "RestartPolicy": {"Name": "unless-stopped"},
        },
        "NetworkingConfig": {
            "EndpointsConfig": {
                network: {
                    "Aliases": [network_alias],
                }
            }
        },
    }
    client.request(
        "POST",
        f"/containers/create?name={quote(container_name, safe='')}",
        body=body,
        ok_statuses={201},
    )
    client.request("POST", f"/containers/{encoded_name}/start", ok_statuses={204, 304})


def _recreate_rtsp_adapter(
    client: DockerSocketClient,
    *,
    source_id: str,
    uri: str,
    zmq_endpoint: str,
    adapter_image: str,
    network: str,
) -> dict[str, Any]:
    container_name = SOURCE_CONTAINER_PREFIX + source_id
    encoded_name = quote(container_name, safe="")
    delete_status, _ = client.request(
        "DELETE",
        f"/containers/{encoded_name}?force=true",
        ok_statuses={204, 404},
    )
    restart_policy_name = _source_restart_policy_name()
    eos_on_start = _source_eos_on_start()
    body = {
        "Image": adapter_image,
        "Entrypoint": ["/opt/savant/adapters/gst/sources/rtsp.sh"],
        "Env": [
            f"SOURCE_ID={source_id}",
            f"LOCATION={uri}",
            f"RTSP_URI={uri}",
            "RTSP_TRANSPORT=tcp",
            f"ZMQ_ENDPOINT={zmq_endpoint}",
            "SYNC_OUTPUT=false",
            "BUFFER_LEN=2000",
            f"EOS_ON_START={str(eos_on_start).lower()}",
            "FFMPEG_TIMEOUT_MS=20000",
            "DOWNLOAD_PATH=/tmp/video-loop-cache",
        ],
        "HostConfig": {
            "NetworkMode": network,
            "RestartPolicy": {"Name": restart_policy_name},
        },
    }
    create_status, _ = client.request(
        "POST",
        f"/containers/create?name={quote(container_name, safe='')}",
        body=body,
        ok_statuses={201},
    )
    start_status, _ = client.request(
        "POST",
        f"/containers/{encoded_name}/start",
        ok_statuses={204, 304},
    )
    return {
        "container_name": container_name,
        "compose_source": False,
        "dynamic_source": True,
        "delete_status": delete_status,
        "create_status": create_status,
        "start_status": start_status,
        "restart_policy": restart_policy_name,
        "eos_on_start": eos_on_start,
        "ffmpeg_timeout_ms": 20000,
    }


def _source_restart_policy_name() -> str:
    value = os.getenv("CAMERA_RUNTIME_SOURCE_RESTART_POLICY", DEFAULT_SOURCE_RESTART_POLICY)
    value = str(value or "").strip().lower()
    if value in {"", "none", "no", "disabled", "false", "0"}:
        return "no"
    if value in {"unless-stopped", "on-failure", "always"}:
        return value
    return DEFAULT_SOURCE_RESTART_POLICY


def _source_eos_on_start() -> bool:
    return _env_bool("CAMERA_RUNTIME_SOURCE_EOS_ON_START", default=DEFAULT_SOURCE_EOS_ON_START)


def _source_only_convergence_plan(
    client: DockerSocketClient,
    *,
    sources_doc: dict[str, Any],
    compose_source_id: str,
    compose_source_container: str,
) -> list[dict[str, Any]]:
    actual_dynamic = _source_adapter_state_by_name(client)
    configured_dynamic_names: set[str] = set()
    plan: list[dict[str, Any]] = []

    for source in sources_doc.get("sources", {}).values():
        if not isinstance(source, dict):
            continue
        base = _source_lifecycle_base(source, compose_source_id=compose_source_id)
        source_id = str(source.get("source_id") or "")
        if not source_id:
            plan.append({**base, "planned_action": "skip", "skip_reason": "missing_source_id"})
            continue
        compose_source_requires_dynamic = (
            source_id == compose_source_id
            and str(source.get("zmq_endpoint") or "")
            != os.getenv("CAMERA_RUNTIME_ZMQ_ENDPOINT", DEFAULT_ZMQ_ENDPOINT)
        )
        if source_id == compose_source_id and not compose_source_requires_dynamic:
            actual = _inspect_container(client, compose_source_container)
            actual_state = str(((actual.get("State") or {}) if isinstance(actual, dict) else {}).get("Status") or "")
            diag = {
                **base,
                "source": source,
                "container_name": compose_source_container,
                "actual_state": actual_state,
                "actual_present": bool(actual),
            }
            if source.get("enabled"):
                if actual_state == "running":
                    plan.append({**diag, "planned_action": "keep"})
                else:
                    plan.append({**diag, "planned_action": "start_compose_enabled"})
            else:
                plan.append({**diag, "planned_action": "stop_compose_disabled"})
            continue
        if compose_source_requires_dynamic:
            actual = _inspect_container(client, compose_source_container)
            actual_state = str(((actual.get("State") or {}) if isinstance(actual, dict) else {}).get("Status") or "")
            if actual_state == "running":
                plan.append(
                    {
                        **base,
                        "source": source,
                        "container_name": compose_source_container,
                        "actual_state": actual_state,
                        "actual_present": bool(actual),
                        "planned_action": "stop_compose_disabled",
                        "skip_reason": "compose_source_replaced_by_shard_dynamic_source",
                    }
                )

        container_name = SOURCE_CONTAINER_PREFIX + source_id
        configured_dynamic_names.add(container_name)
        actual = actual_dynamic.get(container_name)
        actual_state = str((actual or {}).get("state") or "")
        diag = {
            **base,
            "source": source,
            "container_name": container_name,
            "actual_state": actual_state,
            "actual_present": actual is not None,
        }

        if not source.get("enabled"):
            if actual is None:
                plan.append({**diag, "planned_action": "skip", "skip_reason": "disabled_absent"})
            else:
                plan.append({**diag, "planned_action": "remove_disabled"})
            continue

        if source.get("adapter_type") != "gstreamer":
            plan.append({**diag, "planned_action": "skip", "skip_reason": "unsupported_adapter"})
            continue
        uri = str(source.get("uri") or "")
        if not uri.startswith(("rtsp://", "rtsps://")):
            plan.append({**diag, "planned_action": "skip", "skip_reason": "non_rtsp_uri"})
            continue

        if actual is None:
            plan.append({**diag, "planned_action": "create_start"})
            continue

        inspect_doc = _inspect_container(client, container_name)
        if not _rtsp_adapter_container_matches(inspect_doc, source):
            plan.append({**diag, "planned_action": "recreate_start"})
        elif actual_state == "running":
            plan.append({**diag, "planned_action": "keep"})
        else:
            plan.append({**diag, "planned_action": "start"})

    for container_name, actual in actual_dynamic.items():
        if container_name in configured_dynamic_names:
            continue
        plan.append(
            {
                "source_id": "",
                "camera_id": "",
                "adapter_type": "",
                "enabled": False,
                "uri_host": "",
                "compose_source": False,
                "dynamic_source": True,
                "ffmpeg_timeout_ms": None,
                "restart_policy": "",
                "container_name": container_name,
                "actual_state": str(actual.get("state") or ""),
                "actual_present": True,
                "planned_action": "remove_stale",
                "skip_reason": "not_configured",
            }
        )
    return plan


def _source_adapter_state_by_name(client: DockerSocketClient) -> dict[str, dict[str, Any]]:
    try:
        _, body = client.request("GET", "/containers/json?all=true", ok_statuses={200})
        containers = json.loads(body.decode("utf-8") or "[]")
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for container in containers if isinstance(containers, list) else []:
        for raw_name in container.get("Names") or []:
            name = str(raw_name).lstrip("/")
            if name.startswith(SOURCE_CONTAINER_PREFIX):
                out[name] = {
                    "name": name,
                    "state": str(container.get("State") or ""),
                }
    return out


def _rtsp_adapter_container_matches(inspect_doc: dict[str, Any], source: dict[str, Any]) -> bool:
    env = _container_env_map(inspect_doc)
    uri = str(source.get("uri") or "")
    restart_policy = (
        (inspect_doc.get("HostConfig") or {}).get("RestartPolicy") or {}
        if isinstance(inspect_doc, dict)
        else {}
    )
    return (
        env.get("SOURCE_ID") == str(source.get("source_id") or "")
        and env.get("ZMQ_ENDPOINT") == str(source.get("zmq_endpoint") or "")
        and env.get("RTSP_URI", env.get("LOCATION", "")) == uri
        and env.get("EOS_ON_START") == str(_source_eos_on_start()).lower()
        and env.get("FFMPEG_TIMEOUT_MS") == "20000"
        and restart_policy.get("Name") == _source_restart_policy_name()
    )


def _container_env_map(inspect_doc: dict[str, Any]) -> dict[str, str]:
    config = inspect_doc.get("Config") if isinstance(inspect_doc, dict) else {}
    values = config.get("Env") if isinstance(config, dict) else []
    env: dict[str, str] = {}
    for item in values or []:
        key, sep, value = str(item).partition("=")
        if sep:
            env[key] = value
    return env


def _write_yaml(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp_path.replace(path)


def _parse_http_response(response: bytes) -> tuple[int, bytes]:
    header, separator, content = response.partition(b"\r\n\r\n")
    if not separator:
        raise RuntimeApplyError("invalid docker api response: missing header terminator")
    status_line = header.splitlines()[0].decode("ascii", errors="replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise RuntimeApplyError(f"invalid docker api response: {status_line}")
    headers: dict[str, list[str]] = {}
    for line in header.splitlines()[1:]:
        if b":" not in line:
            continue
        name, value = line.split(b":", 1)
        key = name.decode("ascii", errors="replace").strip().lower()
        headers.setdefault(key, []).append(value.decode("ascii", errors="replace").strip())
    transfer_encoding = ",".join(headers.get("transfer-encoding", [])).lower()
    if "chunked" in transfer_encoding:
        content = _decode_http_chunked_body(content)
    elif headers.get("content-length"):
        try:
            content_length = int(headers["content-length"][-1])
        except ValueError as exc:
            raise RuntimeApplyError("invalid docker api response: bad content-length") from exc
        content = content[:content_length]
    return int(parts[1]), content


def _decode_http_chunked_body(content: bytes) -> bytes:
    output = bytearray()
    position = 0
    while True:
        line_end = content.find(b"\r\n", position)
        if line_end < 0:
            raise RuntimeApplyError("invalid docker api response: bad chunk header")
        size_text = content[position:line_end].split(b";", 1)[0].strip()
        try:
            chunk_size = int(size_text, 16)
        except ValueError as exc:
            raise RuntimeApplyError("invalid docker api response: bad chunk size") from exc
        position = line_end + 2
        if chunk_size == 0:
            return bytes(output)
        chunk_end = position + chunk_size
        if chunk_end > len(content):
            raise RuntimeApplyError("invalid docker api response: truncated chunk")
        output.extend(content[position:chunk_end])
        position = chunk_end
        if content[position:position + 2] != b"\r\n":
            raise RuntimeApplyError("invalid docker api response: missing chunk terminator")
        position += 2


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
