"""Runtime overview aggregation for the 8090 operator portal."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings
from app.services.runtime_apply import (
    DEFAULT_CLIP_WORKER_CONTAINER,
    DEFAULT_COMPOSE_SOURCE_CONTAINER,
    DEFAULT_EVENT_WORKER_CONTAINER,
    DEFAULT_FACE_WORKER_CONTAINER,
    DEFAULT_FORWARDER_CONTAINER,
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
DEFAULT_FORWARDER_METRICS_URL = "http://analysis-forwarder:8081/metrics"
DEFAULT_METRICS_TIMEOUT_S = 2.0
DEFAULT_RESTART_RATE_WARN_PER_MIN = 1.0
DEFAULT_RESTART_COUNT_WARN_THRESHOLD = 10
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
_RESTART_RATE_CACHE: dict[str, dict[str, float]] = {}


class RuntimeOverviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeOverviewConfig:
    metrics_url: str = DEFAULT_SAVANT_METRICS_URL
    forwarder_metrics_url: str = DEFAULT_FORWARDER_METRICS_URL
    metrics_timeout_s: float = DEFAULT_METRICS_TIMEOUT_S
    docker_socket: str = DEFAULT_DOCKER_SOCKET
    savant_container: str = DEFAULT_SAVANT_CONTAINER
    replay_container: str = DEFAULT_REPLAY_CONTAINER
    forwarder_container: str = DEFAULT_FORWARDER_CONTAINER
    compose_source_container: str = DEFAULT_COMPOSE_SOURCE_CONTAINER
    event_worker_container: str = DEFAULT_EVENT_WORKER_CONTAINER
    face_worker_container: str = DEFAULT_FACE_WORKER_CONTAINER
    video_sink_container: str = DEFAULT_VIDEO_SINK_CONTAINER
    clip_worker_container: str = DEFAULT_CLIP_WORKER_CONTAINER
    media_worker_container: str = DEFAULT_MEDIA_WORKER_CONTAINER
    dynamic_source_prefix: str = DEFAULT_DYNAMIC_SOURCE_PREFIX
    restart_rate_warn_per_min: float = DEFAULT_RESTART_RATE_WARN_PER_MIN
    restart_count_warn_threshold: int = DEFAULT_RESTART_COUNT_WARN_THRESHOLD
    evidence_recent_limit: int = 12


def config_from_env() -> RuntimeOverviewConfig:
    return RuntimeOverviewConfig(
        metrics_url=os.getenv("RUNTIME_OVERVIEW_SAVANT_METRICS_URL", DEFAULT_SAVANT_METRICS_URL),
        forwarder_metrics_url=os.getenv(
            "RUNTIME_OVERVIEW_FORWARDER_METRICS_URL",
            DEFAULT_FORWARDER_METRICS_URL,
        ),
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
        forwarder_container=os.getenv(
            "RUNTIME_OVERVIEW_FORWARDER_CONTAINER",
            os.getenv("CAMERA_RUNTIME_FORWARDER_CONTAINER", DEFAULT_FORWARDER_CONTAINER),
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
        restart_rate_warn_per_min=_env_float(
            "RUNTIME_OVERVIEW_RESTART_RATE_WARN_PER_MIN",
            DEFAULT_RESTART_RATE_WARN_PER_MIN,
        ),
        restart_count_warn_threshold=_env_int(
            "RUNTIME_OVERVIEW_RESTART_COUNT_WARN_THRESHOLD",
            DEFAULT_RESTART_COUNT_WARN_THRESHOLD,
        ),
        evidence_recent_limit=_env_int("RUNTIME_OVERVIEW_EVIDENCE_RECENT_LIMIT", 12),
    )


def build_runtime_overview(
    *,
    config: RuntimeOverviewConfig | None = None,
    docker_client: DockerSocketClient | None = None,
    metrics_text: str | None = None,
    forwarder_metrics_text: str | None = None,
    evidence_summary: dict[str, Any] | None = None,
    supervisor_snapshot: dict[str, Any] | None = None,
    now_epoch_s: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now_epoch_s is None else float(now_epoch_s)
    cfg = config or config_from_env()
    metrics = (
        parse_savant_metrics(metrics_text)
        if metrics_text is not None
        else fetch_savant_metrics(cfg.metrics_url, timeout_s=cfg.metrics_timeout_s)
    )
    forwarder = (
        parse_forwarder_metrics(forwarder_metrics_text)
        if forwarder_metrics_text is not None
        else fetch_forwarder_metrics(
            cfg.forwarder_metrics_url,
            timeout_s=cfg.metrics_timeout_s,
        )
    )
    client = docker_client or DockerSocketClient(cfg.docker_socket)
    containers = inspect_runtime_containers(client, cfg)
    annotate_container_restart_rates(
        containers,
        now_epoch_s=now,
        rate_warn_per_min=cfg.restart_rate_warn_per_min,
        count_warn_threshold=cfg.restart_count_warn_threshold,
    )
    supervisor = supervisor_snapshot if supervisor_snapshot is not None else _safe_supervisor_snapshot()
    evidence = (
        evidence_summary
        if evidence_summary is not None
        else summarize_evidence_runtime(limit=cfg.evidence_recent_limit)
    )
    health = summarize_runtime_health(metrics=metrics, containers=containers, supervisor=supervisor)
    return {
        "generated_at_epoch_s": int(now),
        "metrics_url": _safe_metrics_url(cfg.metrics_url),
        "forwarder_metrics_url": _safe_metrics_url(cfg.forwarder_metrics_url),
        "metrics": metrics,
        "forwarder": forwarder,
        "containers": containers,
        "supervisor": supervisor,
        "evidence": evidence,
        "health": health,
    }


def summarize_evidence_runtime(*, limit: int = 12) -> dict[str, Any]:
    """Return recent evidence state aggregates for the operator runtime page."""
    started = time.monotonic()
    try:
        conn = psycopg.connect(
            get_settings().database_url,
            row_factory=dict_row,
            autocommit=True,
            connect_timeout=1,
        )
    except Exception as exc:
        return {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "fetch_seconds": round(time.monotonic() - started, 3),
            "state_counts": [],
            "recent": [],
            "recent_failures": [],
        }

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COALESCE(
                            payload->'media'->>'evidence_state',
                            media_status,
                            payload->'media'->>'clip_status',
                            'not_implemented'
                        ) AS evidence_state,
                        COUNT(*) AS count
                    FROM events
                    WHERE created_at >= now() - interval '3 hours'
                      AND (
                        clip_required = true
                        OR snapshot_required = true
                        OR payload->'media'->>'clip_required' = 'true'
                      )
                    GROUP BY evidence_state
                    ORDER BY evidence_state
                    """
                )
                state_counts = [
                    {"state": str(row["evidence_state"]), "count": int(row["count"])}
                    for row in cur.fetchall()
                ]

                cur.execute(
                    """
                    SELECT
                        e.id,
                        e.source_event_id,
                        e.source_id,
                        e.camera_id,
                        e.event_type,
                        e.created_at,
                        e.updated_at,
                        COALESCE(
                            e.payload->'media'->>'evidence_state',
                            e.media_status,
                            e.payload->'media'->>'clip_status',
                            'not_implemented'
                        ) AS evidence_state,
                        COALESCE(
                            e.payload->'media'->>'evidence_reason',
                            e.payload->'media'->>'error_message',
                            t.error_message,
                            ''
                        ) AS evidence_reason,
                        e.payload->'media'->>'replay_job_id' AS replay_job_id,
                        t.status AS task_status,
                        t.updated_at AS task_updated_at
                    FROM events e
                    LEFT JOIN LATERAL (
                        SELECT status, error_message, updated_at
                        FROM evidence_tasks
                        WHERE event_id = e.id
                        ORDER BY created_at DESC, task_id DESC
                        LIMIT 1
                    ) t ON true
                    WHERE e.created_at >= now() - interval '3 hours'
                      AND (
                        e.clip_required = true
                        OR e.snapshot_required = true
                        OR e.payload->'media'->>'clip_required' = 'true'
                      )
                    ORDER BY e.created_at DESC
                    LIMIT %(limit)s
                    """,
                    {"limit": max(1, int(limit))},
                )
                recent = [_evidence_row(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT
                        e.id,
                        e.source_event_id,
                        e.source_id,
                        e.camera_id,
                        e.event_type,
                        e.created_at,
                        e.updated_at,
                        COALESCE(
                            e.payload->'media'->>'evidence_state',
                            e.media_status,
                            e.payload->'media'->>'clip_status',
                            'failed'
                        ) AS evidence_state,
                        COALESCE(
                            e.payload->'media'->>'evidence_reason',
                            e.payload->'media'->>'error_message',
                            t.error_message,
                            ''
                        ) AS evidence_reason,
                        e.payload->'media'->>'replay_job_id' AS replay_job_id,
                        t.status AS task_status,
                        t.updated_at AS task_updated_at
                    FROM events e
                    LEFT JOIN LATERAL (
                        SELECT status, error_message, updated_at
                        FROM evidence_tasks
                        WHERE event_id = e.id
                        ORDER BY created_at DESC, task_id DESC
                        LIMIT 1
                    ) t ON true
                    WHERE e.created_at >= now() - interval '3 hours'
                      AND COALESCE(
                        e.payload->'media'->>'evidence_state',
                        e.media_status,
                        e.payload->'media'->>'clip_status'
                      ) = 'failed'
                    ORDER BY e.updated_at DESC
                    LIMIT %(limit)s
                    """,
                    {"limit": max(1, int(limit))},
                )
                failures = [_evidence_row(row) for row in cur.fetchall()]
    except Exception as exc:
        return {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "fetch_seconds": round(time.monotonic() - started, 3),
            "state_counts": [],
            "recent": [],
            "recent_failures": [],
        }
    finally:
        conn.close()

    return {
        "available": True,
        "fetch_seconds": round(time.monotonic() - started, 3),
        "state_counts": state_counts,
        "recent": recent,
        "recent_failures": failures,
    }


def _evidence_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(row.get("id") or ""),
        "source_event_id": str(row.get("source_event_id") or ""),
        "source_id": str(row.get("source_id") or ""),
        "camera_id": str(row.get("camera_id") or ""),
        "event_type": str(row.get("event_type") or ""),
        "created_at": _iso_or_none(row.get("created_at")),
        "updated_at": _iso_or_none(row.get("updated_at")),
        "age_seconds": _age_seconds(row.get("created_at")),
        "evidence_state": str(row.get("evidence_state") or "not_implemented"),
        "evidence_reason": str(row.get("evidence_reason") or ""),
        "replay_job_id": str(row.get("replay_job_id") or ""),
        "task_status": str(row.get("task_status") or ""),
        "task_updated_at": _iso_or_none(row.get("task_updated_at")),
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


def fetch_forwarder_metrics(url: str, *, timeout_s: float) -> dict[str, Any]:
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
    parsed = parse_forwarder_metrics(body)
    parsed["fetch_seconds"] = round(time.monotonic() - started, 3)
    return parsed


def parse_forwarder_metrics(text: str) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PROM_SAMPLE_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        if not name.startswith("va_forwarder_"):
            continue
        labels = _parse_prom_labels(match.group("labels") or "")
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        samples.append({"name": name, "labels": labels, "value": value})

    by_source: dict[str, dict[str, float]] = {}
    global_metrics: dict[str, float] = {}
    for sample in samples:
        name = str(sample["name"])
        value = float(sample["value"])
        source_id = str(sample["labels"].get("source_id") or "")
        if source_id:
            by_source.setdefault(source_id, {})[name] = value
        else:
            global_metrics[name] = value

    sources = [
        {
            "source_id": source_id,
            "frames_seen_total": values.get("va_forwarder_frames_seen_total"),
            "frames_forwarded_total": values.get("va_forwarder_frames_forwarded_total"),
            "frames_dropped_total": values.get("va_forwarder_frames_dropped_total"),
            "savant_send_failures_total": values.get(
                "va_forwarder_savant_send_failures_total"
            ),
        }
        for source_id, values in sorted(by_source.items())
    ]
    return {
        "available": bool(samples),
        "sample_count": len(samples),
        "global": {
            "queue_depth": global_metrics.get("va_forwarder_queue_depth"),
            "running": global_metrics.get("va_forwarder_running"),
        },
        "sources": sources,
    }


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
        "analysis_forwarder": config.forwarder_container,
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
    restart_count_high, restart_rate_high = _restart_warning_containers(containers)
    if restart_count_high:
        issues.append("container_restart_count_high")
    if restart_rate_high:
        issues.append("container_restart_rate_high")
    active_source_count = _float_or_none(metrics.get("sources_active"))
    return {
        "ok": not issues,
        "issues": issues,
        "source_count": int(active_source_count) if active_source_count is not None else len(source_rows),
        "stale_sources": stale_sources,
        "restart_count_high_containers": restart_count_high,
        "restart_rate_high_containers": restart_rate_high,
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
        rows.append(_dynamic_source_summary(client, name, dynamic_source_prefix, container))
    return sorted(rows, key=lambda row: row["name"])


def annotate_container_restart_rates(
    containers: dict[str, Any],
    *,
    now_epoch_s: float,
    rate_warn_per_min: float,
    count_warn_threshold: int,
) -> None:
    for row in (containers.get("fixed") or {}).values():
        _annotate_restart_row(
            row,
            now_epoch_s=now_epoch_s,
            rate_warn_per_min=rate_warn_per_min,
            count_warn_threshold=count_warn_threshold,
        )
    for row in containers.get("dynamic_sources") or []:
        _annotate_restart_row(
            row,
            now_epoch_s=now_epoch_s,
            rate_warn_per_min=rate_warn_per_min,
            count_warn_threshold=count_warn_threshold,
        )


def _dynamic_source_summary(
    client: DockerSocketClient,
    name: str,
    dynamic_source_prefix: str,
    list_row: dict[str, Any],
) -> dict[str, Any]:
    summary = _inspect_container_summary(client, name)
    if summary.get("present"):
        row = summary
    else:
        row = {
            "name": name,
            "present": True,
            "state": str(list_row.get("State") or ""),
            "status": str(list_row.get("Status") or ""),
        }
        if summary.get("error"):
            row["error"] = summary["error"]
    row["source_id"] = name[len(dynamic_source_prefix):] if name.startswith(dynamic_source_prefix) else name
    row["status"] = str(list_row.get("Status") or row.get("status") or "")
    return row


def _annotate_restart_row(
    row: dict[str, Any],
    *,
    now_epoch_s: float,
    rate_warn_per_min: float,
    count_warn_threshold: int,
) -> None:
    restart_count = _int_or_none(row.get("restart_count"))
    row["restart_rate_per_min"] = None
    row["restart_count_delta"] = None
    row["restart_rate_window_seconds"] = None
    row["restart_count_warning"] = False
    row["restart_rate_warning"] = False
    row["restart_warning"] = False
    if restart_count is None:
        return

    key = str(row.get("name") or row.get("id") or "")
    previous = _RESTART_RATE_CACHE.get(key) if key else None
    if previous:
        elapsed = max(0.0, now_epoch_s - float(previous.get("observed_at", 0.0)))
        previous_count = int(previous.get("restart_count", restart_count))
        if elapsed > 0 and restart_count >= previous_count:
            delta = restart_count - previous_count
            rate = (delta * 60.0) / elapsed
            row["restart_count_delta"] = delta
            row["restart_rate_window_seconds"] = round(elapsed, 3)
            row["restart_rate_per_min"] = round(rate, 3)
            row["restart_rate_warning"] = delta > 0 and rate >= rate_warn_per_min

    if key:
        _RESTART_RATE_CACHE[key] = {
            "restart_count": float(restart_count),
            "observed_at": float(now_epoch_s),
        }
    row["restart_count_warning"] = (
        count_warn_threshold > 0 and restart_count >= count_warn_threshold
    )
    row["restart_warning"] = bool(row["restart_count_warning"] or row["restart_rate_warning"])


def _restart_warning_containers(containers: dict[str, Any]) -> tuple[list[str], list[str]]:
    count_high: list[str] = []
    rate_high: list[str] = []
    rows = list((containers.get("fixed") or {}).values()) + list(containers.get("dynamic_sources") or [])
    for row in rows:
        name = str(row.get("name") or "")
        if not name:
            continue
        if row.get("restart_count_warning"):
            count_high.append(name)
        if row.get("restart_rate_warning"):
            rate_high.append(name)
    return sorted(count_high), sorted(rate_high)


def _reset_restart_rate_cache_for_tests() -> None:
    _RESTART_RATE_CACHE.clear()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _age_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        observed = value
    else:
        try:
            observed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return round(max(0.0, (datetime.now(timezone.utc) - observed).total_seconds()), 1)


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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
