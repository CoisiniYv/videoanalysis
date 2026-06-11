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

from redis import Redis
import yaml


DEFAULT_MODULE_CONFIG_PATH = "/app/modules/savant_security/config/cameras.midterm.yml"
DEFAULT_SOURCES_CONFIG_PATH = "/app/infra/generated/sources.generated.yml"
DEFAULT_ZMQ_ENDPOINT = "dealer+connect:tcp://replay-service:5555"
DEFAULT_NETWORK = "video-analytics-midterm_default"
DEFAULT_ADAPTER_IMAGE = "ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0"
DEFAULT_SAVANT_CONTAINER = "video-analytics-midterm-savant"
DEFAULT_REPLAY_CONTAINER = "video-analytics-midterm-replay-service"
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
SAVANT_READY_PATTERNS = (
    re.compile(r"\bPLAYING\b", re.IGNORECASE),
    re.compile(r"\bmodule\b.*\bstarted\b", re.IGNORECASE),
    re.compile(r"\bpipeline\b.*\bready\b", re.IGNORECASE),
    re.compile(r"\bpipeline\b.*\bstarted\b", re.IGNORECASE),
)

LOGGER = logging.getLogger(__name__)


class RuntimeApplyError(RuntimeError):
    pass


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
) -> dict[str, Any]:
    return _apply_camera_runtime_controlled(
        export_doc=export_doc,
        cameras=cameras,
        reason="camera_runtime_apply",
        action="apply",
    )


def restart_camera_runtime(
    *,
    export_doc: dict[str, Any],
    cameras: list[dict[str, Any]],
) -> dict[str, Any]:
    return _apply_camera_runtime_controlled(
        export_doc=export_doc,
        cameras=cameras,
        reason="controlled_runtime_restart",
        action="restart",
    )


def _apply_camera_runtime_controlled(
    *,
    export_doc: dict[str, Any],
    cameras: list[dict[str, Any]],
    reason: str,
    action: str,
) -> dict[str, Any]:
    if not _env_bool("CAMERA_RUNTIME_APPLY_ENABLED", default=False):
        raise RuntimeApplyError("camera runtime control is disabled")

    module_config_path = Path(
        os.getenv("CAMERA_RUNTIME_MODULE_CONFIG_PATH", DEFAULT_MODULE_CONFIG_PATH)
    )
    sources_config_path = Path(
        os.getenv("CAMERA_RUNTIME_SOURCES_CONFIG_PATH", DEFAULT_SOURCES_CONFIG_PATH)
    )
    zmq_endpoint = os.getenv("CAMERA_RUNTIME_ZMQ_ENDPOINT", DEFAULT_ZMQ_ENDPOINT)
    adapter_image = os.getenv("CAMERA_RUNTIME_ADAPTER_IMAGE", DEFAULT_ADAPTER_IMAGE)
    network = os.getenv("CAMERA_RUNTIME_DOCKER_NETWORK", DEFAULT_NETWORK)
    savant_container = os.getenv("CAMERA_RUNTIME_SAVANT_CONTAINER", DEFAULT_SAVANT_CONTAINER)
    replay_container = os.getenv("CAMERA_RUNTIME_REPLAY_CONTAINER", DEFAULT_REPLAY_CONTAINER)
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
    sources_doc = _build_sources_doc(cameras, zmq_endpoint=zmq_endpoint)
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
        "runtime_epoch_state_path": str(_runtime_epoch_state_path()),
        "runtime_epoch_root": str(_runtime_epoch_root()),
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
        "savant_restarted": savant_container,
        "management_containers_preserved": [
            os.getenv("CAMERA_RUNTIME_API_CONTAINER", "video-analytics-midterm-api"),
            os.getenv(
                "CAMERA_RUNTIME_VIEWER_CONTAINER",
                "video-analytics-midterm-evidence-viewer",
            ),
        ],
    }


def _build_sources_doc(cameras: list[dict[str, Any]], *, zmq_endpoint: str) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    for camera in cameras:
        camera_id = str(camera["id"])
        sources[camera_id] = {
            "camera_id": camera_id,
            "source_id": str(camera["source_id"]),
            "uri": str(camera["rtsp_url"]),
            "enabled": bool(camera.get("enabled", True)),
            "adapter_type": "gstreamer",
            "zmq_endpoint": zmq_endpoint,
        }
    return {"sources": sources}


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


def _stop_container(client: DockerSocketClient, container_name: str) -> None:
    if not container_name:
        return
    client.request(
        "POST",
        f"/containers/{quote(container_name, safe='')}/stop?t=10",
        ok_statuses={204, 304, 404},
    )


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
    return {
        "source_id": source_id,
        "camera_id": str(source.get("camera_id") or ""),
        "adapter_type": str(source.get("adapter_type") or ""),
        "enabled": bool(source.get("enabled")),
        "uri_host": _rtsp_uri_host(uri),
        "compose_source": source_id == compose_source_id,
        "dynamic_source": source_id != compose_source_id,
        "ffmpeg_timeout_ms": 20000 if uri.startswith(("rtsp://", "rtsps://")) else None,
        "restart_policy": "unless-stopped" if uri.startswith(("rtsp://", "rtsps://")) else "",
    }


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
) -> None:
    container_name = SOURCE_CONTAINER_PREFIX + source_id
    encoded_name = quote(container_name, safe="")
    delete_status, _ = client.request(
        "DELETE",
        f"/containers/{encoded_name}?force=true",
        ok_statuses={204, 404},
    )
    body = {
        "Image": adapter_image,
        "Entrypoint": ["/opt/savant/adapters/gst/sources/rtsp.sh"],
        "Env": [
            f"SOURCE_ID={source_id}",
            f"LOCATION={uri}",
            f"RTSP_URI={uri}",
            "RTSP_TRANSPORT=tcp",
            f"ZMQ_ENDPOINT={zmq_endpoint}",
            "SYNC_OUTPUT=true",
            "BUFFER_LEN=2000",
            "EOS_ON_START=false",
            "FFMPEG_TIMEOUT_MS=20000",
            "DOWNLOAD_PATH=/tmp/video-loop-cache",
        ],
        "HostConfig": {
            "NetworkMode": network,
            "RestartPolicy": {"Name": "unless-stopped"},
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
        "restart_policy": "unless-stopped",
        "ffmpeg_timeout_ms": 20000,
    }


def _write_yaml(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp_path.replace(path)


def _parse_http_response(response: bytes) -> tuple[int, bytes]:
    header, _, content = response.partition(b"\r\n\r\n")
    status_line = header.splitlines()[0].decode("ascii", errors="replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise RuntimeApplyError(f"invalid docker api response: {status_line}")
    return int(parts[1]), content


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
