"""Runtime overview aggregation for the 8090 operator portal."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen

from app.services.runtime_apply import (
    DEFAULT_CLIP_WORKER_CONTAINER,
    DEFAULT_COMPOSE_SOURCE_CONTAINER,
    DEFAULT_EVENT_WORKER_CONTAINER,
    DEFAULT_FACE_WORKER_CONTAINER,
    DEFAULT_MEDIA_WORKER_CONTAINER,
    DEFAULT_REPLAY_CONTAINER,
    DEFAULT_SAVANT_CONTAINER,
    DEFAULT_VIDEO_SINK_CONTAINER,
    DockerSocketClient,
)
from app.services.savant_supervisor import (
    DEFAULT_DYNAMIC_SOURCE_PREFIX,
    SavantSupervisorError,
    get_savant_supervisor_snapshot,
)


LOGGER = logging.getLogger(__name__)

DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
DEFAULT_SAVANT_METRICS_URL = "http://savant-security:8080/metrics"
DEFAULT_METRICS_TIMEOUT_S = 2.0
PROM_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)$"
)
PROM_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')
COUNTER_METRICS = {
    "va_savant_frames_seen_total",
    "va_savant_frame_annotations_exported_total",
    "va_savant_pose_stage_frames_total",
    "va_savant_pose_frames_with_person_total",
    "va_savant_pose_objects_total",
    "va_savant_face_stage_frames_total",
    "va_savant_face_frames_with_face_total",
    "va_savant_face_objects_total",
    "va_savant_adaface_embeddings_total",
    "va_savant_person_observations_exported_total",
    "va_savant_face_observations_exported_total",
}
GAUGE_METRICS = {
    "va_savant_effective_fps",
    "va_savant_last_frame_age_seconds",
}


class RuntimeOverviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeOverviewConfig:
    metrics_url: str = DEFAULT_SAVANT_METRICS_URL
    metrics_timeout_s: float = DEFAULT_METRICS_TIMEOUT_S
    docker_socket: str = DEFAULT_DOCKER_SOCKET
    savant_container: str = DEFAULT_SAVANT_CONTAINER
    replay_container: str = DEFAULT_REPLAY_CONTAINER
    compose_source_container: str = DEFAULT_COMPOSE_SOURCE_CONTAINER
    event_worker_container: str = DEFAULT_EVENT_WORKER_CONTAINER
    face_worker_container: str = DEFAULT_FACE_WORKER_CONTAINER
    video_sink_container: str = DEFAULT_VIDEO_SINK_CONTAINER
    clip_worker_container: str = DEFAULT_CLIP_WORKER_CONTAINER
    media_worker_container: str = DEFAULT_MEDIA_WORKER_CONTAINER
    dynamic_source_prefix: str = DEFAULT_DYNAMIC_SOURCE_PREFIX


def config_from_env() -> RuntimeOverviewConfig:
    return RuntimeOverviewConfig(
        metrics_url=os.getenv("RUNTIME_OVERVIEW_SAVANT_METRICS_URL", DEFAULT_SAVANT_METRICS_URL),
        metrics_timeout_s=_env_float("RUNTIME_OVERVIEW_METRICS_TIMEOUT_S", DEFAULT_METRICS_TIMEOUT_S),
        docker_socket=os.getenv(
            "RUNTIME_OVERVIEW_DOCKER_SOCKET",
            os.getenv("CAMERA_RUNTIME_DOCKER_SOCKET", DEFAULT_DOCKER_SOCKET),
        ),
        savant_container=os.getenv(
            "RUNTIME_OVERVIEW_SAVANT_CONTAINER",
            os.getenv("CAMERA_RUNTIME_SAVANT_CONTAINER", DEFAULT_SAVANT_CONTAINER),
        ),
        replay_container=os.getenv(
            "RUNTIME_OVERVIEW_REPLAY_CONTAINER",
            os.getenv("CAMERA_RUNTIME_REPLAY_CONTAINER", DEFAULT_REPLAY_CONTAINER),
        ),
        compose_source_container=os.getenv(
            "RUNTIME_OVERVIEW_COMPOSE_SOURCE_CONTAINER",
            os.getenv("CAMERA_RUNTIME_COMPOSE_SOURCE_CONTAINER", DEFAULT_COMPOSE_SOURCE_CONTAINER),
        ),
        event_worker_container=os.getenv(
            "RUNTIME_OVERVIEW_EVENT_WORKER_CONTAINER",
            os.getenv("CAMERA_RUNTIME_EVENT_WORKER_CONTAINER", DEFAULT_EVENT_WORKER_CONTAINER),
        ),
        face_worker_container=os.getenv(
            "RUNTIME_OVERVIEW_FACE_WORKER_CONTAINER",
            os.getenv("CAMERA_RUNTIME_FACE_WORKER_CONTAINER", DEFAULT_FACE_WORKER_CONTAINER),
        ),
        video_sink_container=os.getenv(
            "RUNTIME_OVERVIEW_VIDEO_SINK_CONTAINER",
            os.getenv("CAMERA_RUNTIME_VIDEO_SINK_CONTAINER", DEFAULT_VIDEO_SINK_CONTAINER),
        ),
        clip_worker_container=os.getenv(
            "RUNTIME_OVERVIEW_CLIP_WORKER_CONTAINER",
            os.getenv("CAMERA_RUNTIME_CLIP_WORKER_CONTAINER", DEFAULT_CLIP_WORKER_CONTAINER),
        ),
        media_worker_container=os.getenv(
            "RUNTIME_OVERVIEW_MEDIA_WORKER_CONTAINER",
            os.getenv("CAMERA_RUNTIME_MEDIA_WORKER_CONTAINER", DEFAULT_MEDIA_WORKER_CONTAINER),
        ),
        dynamic_source_prefix=os.getenv("RUNTIME_OVERVIEW_DYNAMIC_SOURCE_PREFIX", DEFAULT_DYNAMIC_SOURCE_PREFIX),
    )


def build_runtime_overview(
    *,
    config: RuntimeOverviewConfig | None = None,
    docker_client: DockerSocketClient | None = None,
    metrics_text: str | None = None,
    supervisor_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = config or config_from_env()
    metrics = (
        parse_savant_metrics(metrics_text)
        if metrics_text is not None
        else fetch_savant_metrics(cfg.metrics_url, timeout_s=cfg.metrics_timeout_s)
    )
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    containers = inspect_runtime_containers(client, cfg)
    supervisor = supervisor_snapshot if supervisor_snapshot is not None else _safe_supervisor_snapshot()
    health = summarize_runtime_health(metrics=metrics, containers=containers, supervisor=supervisor)
    return {
        "generated_at_epoch_s": int(time.time()),
        "metrics_url": _safe_metrics_url(cfg.metrics_url),
        "metrics": metrics,
        "containers": containers,
        "supervisor": supervisor,
        "health": health,
    }


def fetch_savant_metrics(url: str, *, timeout_s: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with urlopen(url, timeout=max(0.1, timeout_s)) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (OSError, URLError) as exc:
        return {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "fetch_seconds": round(time.monotonic() - started, 3),
            "sources": [],
            "global": {},
        }
    parsed = parse_savant_metrics(body)
    parsed["fetch_seconds"] = round(time.monotonic() - started, 3)
    return parsed


def parse_savant_metrics(text: str) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PROM_SAMPLE_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        if not name.startswith("va_savant_"):
            continue
        labels = _parse_prom_labels(match.group("labels") or "")
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        samples.append({"name": name, "labels": labels, "value": value})

    by_source: dict[str, dict[str, Any]] = {}
    global_metrics: dict[str, float] = {}
    required_seen: set[str] = set()
    for sample in samples:
        name = str(sample["name"])
        value = float(sample["value"])
        labels = sample["labels"]
        source_id = str(labels.get("source_id") or "")
        if source_id:
            row = by_source.setdefault(
                source_id,
                {
                    "source_id": source_id,
                    "counters": {},
                    "gauges": {},
                    "windows": {},
                },
            )
            window = str(labels.get("window") or "")
            if window:
                row["windows"].setdefault(window, {})[name] = value
            elif name in GAUGE_METRICS:
                row["gauges"][name] = value
            else:
                row["counters"][name] = value
            if name in COUNTER_METRICS or name in GAUGE_METRICS:
                required_seen.add(name)
        else:
            global_metrics[name] = value
            if name == "va_savant_sources_active":
                required_seen.add(name)

    sources = []
    for source_id, row in sorted(by_source.items()):
        gauges = row["gauges"]
        counters = row["counters"]
        sources.append(
            {
                "source_id": source_id,
                "effective_fps": gauges.get("va_savant_effective_fps"),
                "last_frame_age_seconds": gauges.get("va_savant_last_frame_age_seconds"),
                "frames_seen_total": counters.get("va_savant_frames_seen_total"),
                "frame_annotations_exported_total": counters.get(
                    "va_savant_frame_annotations_exported_total"
                ),
                "pose_objects_total": counters.get("va_savant_pose_objects_total"),
                "face_objects_total": counters.get("va_savant_face_objects_total"),
                "adaface_embeddings_total": counters.get("va_savant_adaface_embeddings_total"),
                "person_observations_exported_total": counters.get(
                    "va_savant_person_observations_exported_total"
                ),
                "face_observations_exported_total": counters.get(
                    "va_savant_face_observations_exported_total"
                ),
                "counters": counters,
                "gauges": gauges,
                "windows": row["windows"],
            }
        )

    return {
        "available": bool(samples),
        "sample_count": len(samples),
        "required_metric_names_seen": sorted(required_seen),
        "sources_active": global_metrics.get("va_savant_sources_active"),
        "global": global_metrics,
        "sources": sources,
    }


def inspect_runtime_containers(
    client: DockerSocketClient,
    config: RuntimeOverviewConfig,
) -> dict[str, Any]:
    fixed = {
        "savant": config.savant_container,
        "replay": config.replay_container,
        "compose_source": config.compose_source_container,
        "event_worker": config.event_worker_container,
        "face_worker": config.face_worker_container,
        "video_sink": config.video_sink_container,
        "clip_worker": config.clip_worker_container,
        "media_worker": config.media_worker_container,
    }
    rows = {role: _inspect_container_summary(client, name) for role, name in fixed.items()}
    dynamic_sources = _list_dynamic_source_containers(client, config.dynamic_source_prefix)
    return {
        "available": any(row.get("present") for row in rows.values()) or bool(dynamic_sources),
        "fixed": rows,
        "dynamic_sources": dynamic_sources,
    }


def summarize_runtime_health(
    *,
    metrics: dict[str, Any],
    containers: dict[str, Any],
    supervisor: dict[str, Any],
) -> dict[str, Any]:
    issues: list[str] = []
    if not metrics.get("available"):
        issues.append("savant_metrics_unavailable")
    for role, row in (containers.get("fixed") or {}).items():
        if row.get("present") and row.get("state") != "running":
            issues.append(f"{role}_not_running")
    source_rows = metrics.get("sources") or []
    stale_sources = [
        row.get("source_id")
        for row in source_rows
        if _float_or_none(row.get("last_frame_age_seconds")) is not None
        and _float_or_none(row.get("last_frame_age_seconds")) > 30
    ]
    if stale_sources:
        issues.append("source_frame_age_high")
    if supervisor.get("enabled") and not supervisor.get("savant_container_running", True):
        issues.append("savant_container_not_running")
    return {
        "ok": not issues,
        "issues": issues,
        "source_count": len(source_rows),
        "stale_sources": stale_sources,
    }


def _inspect_container_summary(client: DockerSocketClient, container_name: str) -> dict[str, Any]:
    if not container_name:
        return {"name": "", "present": False}
    try:
        _status, body = client.request(
            "GET",
            f"/containers/{quote(container_name, safe='')}/json",
            ok_statuses={200, 404},
        )
        doc = json.loads(body.decode("utf-8") or "{}")
    except Exception as exc:
        return {
            "name": container_name,
            "present": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(doc, dict) or not doc.get("Id"):
        return {"name": container_name, "present": False}
    state = doc.get("State") if isinstance(doc.get("State"), dict) else {}
    return {
        "name": container_name,
        "present": True,
        "id": str(doc.get("Id") or "")[:12],
        "state": str(state.get("Status") or ""),
        "running": bool(state.get("Running")),
        "health": str((state.get("Health") or {}).get("Status") or "") if isinstance(state.get("Health"), dict) else "",
        "restart_count": int(doc.get("RestartCount") or 0),
        "started_at": str(state.get("StartedAt") or ""),
        "finished_at": str(state.get("FinishedAt") or ""),
    }


def _list_dynamic_source_containers(
    client: DockerSocketClient,
    dynamic_source_prefix: str,
) -> list[dict[str, Any]]:
    try:
        _status, body = client.request("GET", "/containers/json?all=true", ok_statuses={200})
        doc = json.loads(body.decode("utf-8") or "[]")
    except Exception as exc:
        LOGGER.warning("failed to list dynamic source containers: %s", exc)
        return []
    rows: list[dict[str, Any]] = []
    for container in doc if isinstance(doc, list) else []:
        names = [str(name).lstrip("/") for name in container.get("Names") or []]
        name = next((item for item in names if item.startswith(dynamic_source_prefix)), "")
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "source_id": name[len(dynamic_source_prefix):],
                "state": str(container.get("State") or ""),
                "status": str(container.get("Status") or ""),
            }
        )
    return sorted(rows, key=lambda row: row["name"])


def _safe_supervisor_snapshot() -> dict[str, Any]:
    try:
        return get_savant_supervisor_snapshot()
    except (RuntimeError, SavantSupervisorError) as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def _parse_prom_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for key, value in PROM_LABEL_RE.findall(raw or ""):
        labels[key] = bytes(value, "utf-8").decode("unicode_escape")
    return labels


def _safe_metrics_url(url: str) -> str:
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _auth, _, host = rest.rpartition("@")
    return f"{scheme}://<redacted>@{host}" if scheme else f"<redacted>@{host}"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
