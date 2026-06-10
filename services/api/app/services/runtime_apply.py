from __future__ import annotations

import json
import os
import re
import socket
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
DEFAULT_EPOCH_ROOT = "/data/video-analytics/media/replay-sink-output/midterm"
DEFAULT_REDIS_EPOCH_KEY = "video_analytics:midterm:runtime_epoch"
SOURCE_CONTAINER_PREFIX = "video-analytics-source-"
RUNTIME_EPOCH_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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
    _start_containers(client, worker_containers)

    started_sources: list[str] = []
    compose_sources_started: list[str] = []
    skipped_sources: list[str] = []
    for source in sources_doc["sources"].values():
        if not source.get("enabled"):
            skipped_sources.append(str(source.get("source_id", "")))
            continue
        if source.get("adapter_type") != "gstreamer":
            skipped_sources.append(str(source.get("source_id", "")))
            continue
        uri = str(source.get("uri") or "")
        if not uri.startswith(("rtsp://", "rtsps://")):
            skipped_sources.append(str(source.get("source_id", "")))
            continue
        source_id = str(source["source_id"])
        if source_id == compose_source_id:
            _start_container(client, compose_source_container)
            compose_sources_started.append(source_id)
            continue
        _recreate_rtsp_adapter(
            client,
            source_id=source_id,
            uri=uri,
            zmq_endpoint=str(source["zmq_endpoint"]),
            adapter_image=adapter_image,
            network=network,
        )
        started_sources.append(source_id)
    return {
        "runtime_action": action,
        "module_config_path": str(module_config_path),
        "sources_config_path": str(sources_config_path),
        "runtime_epoch_id": runtime_epoch_id,
        "runtime_epoch_state_path": str(_runtime_epoch_state_path()),
        "runtime_epoch_root": str(_runtime_epoch_root()),
        "video_sink_container": video_sink_container,
        "video_sink_dir_location": _video_sink_dir_location(runtime_epoch_id),
        "sources_total": len(sources_doc["sources"]),
        "source_containers_stopped": source_containers,
        "compose_sources_started": compose_sources_started,
        "dynamic_sources_started": started_sources,
        "sources_skipped": skipped_sources,
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
        Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0")).set(
            os.getenv("RUNTIME_EPOCH_REDIS_KEY", DEFAULT_REDIS_EPOCH_KEY),
            epoch_id,
        )
        state["redis_epoch_published"] = True
    except Exception:
        state["redis_epoch_published"] = False
    return state


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


def _start_container(client: DockerSocketClient, container_name: str) -> None:
    if not container_name:
        return
    client.request(
        "POST",
        f"/containers/{quote(container_name, safe='')}/start",
        ok_statuses={204, 304, 404},
    )


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


def _recreate_video_file_sink(
    client: DockerSocketClient,
    *,
    container_name: str,
    runtime_epoch_id: str,
    network: str,
) -> None:
    encoded_name = quote(container_name, safe="")
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
    client.request(
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
    client.request(
        "POST",
        f"/containers/create?name={quote(container_name, safe='')}",
        body=body,
        ok_statuses={201},
    )
    client.request("POST", f"/containers/{encoded_name}/start", ok_statuses={204, 304})


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
