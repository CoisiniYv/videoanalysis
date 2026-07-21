"""Live end-to-end latency summary for the 8090 operator portal."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import psycopg
from redis import Redis

from app.config import get_settings
from app.services.runtime_overview import fetch_forwarder_metrics, fetch_savant_metrics


def build_runtime_latency() -> dict[str, Any]:
    now_s = time.time()
    annotation = _latest_annotation(now_s)
    database = _database_latency(now_s)
    branches = []
    for branch_id in ("a", "b"):
        forwarder = fetch_forwarder_metrics(
            f"http://replay-raw-fanout-{branch_id}:8081/metrics",
            timeout_s=1.0,
        )
        savant = fetch_savant_metrics(
            f"http://savant-{branch_id}:8080/metrics",
            timeout_s=1.0,
        )
        source_ages = [
            float(row.get("last_frame_age_seconds") or 0)
            for row in savant.get("sources") or []
        ]
        branches.append(
            {
                "branch_id": branch_id,
                "queue_depth": int((forwarder.get("global") or {}).get("queue_depth") or 0),
                "source_count": int(
                    (savant.get("global") or {}).get("va_savant_sources_active")
                    or len(savant.get("sources") or [])
                ),
                "max_source_receive_age_s": round(max(source_ages), 3) if source_ages else None,
                "available": bool(forwarder.get("available")) and bool(savant.get("available")),
            }
        )
    media_lag_s = annotation.get("media_lag_s")
    severity = _latency_severity(media_lag_s)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "generated_at_epoch_ms": int(now_s * 1000),
        "status": severity,
        "annotation": annotation,
        "database": database,
        "branches": branches,
        "thresholds": {
            "healthy_max_s": 10,
            "warning_max_s": 60,
        },
    }


def _latest_annotation(now_s: float) -> dict[str, Any]:
    client = Redis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"),
        socket_timeout=1,
        socket_connect_timeout=1,
    )
    try:
        rows = client.xrevrange("security.frame_annotations", count=1)
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    finally:
        client.close()
    if not rows:
        return {"available": False, "error": "annotation stream is empty"}
    stream_id, raw_fields = rows[0]
    fields = {
        _text(key): _text(value) for key, value in raw_fields.items()
    }
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(fields.get("data") or "{}")
    except json.JSONDecodeError:
        payload = {}
    timestamp_ms = _int_or_none(fields.get("timestamp_ms"))
    if timestamp_ms is None:
        timestamp_ms = _int_or_none(payload.get("timestamp_ms"))
    created_at = payload.get("created_at") or fields.get("created_at")
    stream_write_epoch_s = _stream_id_epoch(stream_id)
    created_at_epoch_s = _iso_epoch_or_none(created_at)
    write_epoch_s = created_at_epoch_s or stream_write_epoch_s
    return {
        "available": timestamp_ms is not None,
        "stream_id": _text(stream_id),
        "source_id": fields.get("source_id") or payload.get("source_id"),
        "media_timestamp_ms": timestamp_ms,
        "media_lag_s": round(max(0.0, now_s - timestamp_ms / 1000.0), 3)
        if timestamp_ms is not None
        else None,
        "annotation_write_age_s": round(max(0.0, now_s - write_epoch_s), 3)
        if write_epoch_s is not None
        else None,
    }


def _database_latency(now_s: float) -> dict[str, Any]:
    conn = psycopg.connect(get_settings().database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    event_ts_ms,
                    created_at,
                    extract(epoch FROM now() - created_at)
                FROM events
                WHERE event_ts_ms BETWEEN 946684800000 AND 4102444800000
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            event_ts_ms, latest_event_write, event_write_age_s = (
                cur.fetchone() or (None, None, None)
            )
            cur.execute(
                """
                SELECT
                    e.event_ts_ms,
                    b.created_at,
                    extract(epoch FROM now() - b.created_at)
                FROM evidence_bundles b
                JOIN events e ON e.id = b.event_id
                WHERE e.event_ts_ms BETWEEN 946684800000 AND 4102444800000
                  AND b.event_created_at IS NOT NULL
                ORDER BY b.event_created_at DESC
                LIMIT 1
                """
            )
            bundle_event_ts_ms, latest_bundle_write, bundle_write_age_s = (
                cur.fetchone() or (None, None, None)
            )
            cur.execute(
                """
                SELECT
                    count(*) FILTER (
                        WHERE status IN (
                            'pending', 'manifest_ready', 'materialization_pending',
                            'materialization_deferred'
                        )
                    ),
                    count(*) FILTER (WHERE status = 'materializing'),
                    min(created_at) FILTER (
                        WHERE status IN (
                            'pending', 'manifest_ready', 'materialization_pending',
                            'materialization_deferred', 'materializing'
                        )
                    )
                FROM evidence_tasks
                """
            )
            pending_count, materializing_count, oldest_active = cur.fetchone()
    finally:
        conn.close()
    return {
        "latest_event_media_timestamp_ms": int(event_ts_ms) if event_ts_ms is not None else None,
        "event_media_lag_s": round(max(0.0, now_s - int(event_ts_ms) / 1000.0), 3)
        if event_ts_ms is not None
        else None,
        "latest_event_write_at": _iso_text(latest_event_write),
        "event_write_age_s": _float_or_none(event_write_age_s),
        "latest_bundle_media_timestamp_ms": int(bundle_event_ts_ms)
        if bundle_event_ts_ms is not None
        else None,
        "bundle_event_lag_s": round(
            max(0.0, now_s - int(bundle_event_ts_ms) / 1000.0), 3
        )
        if bundle_event_ts_ms is not None
        else None,
        "latest_bundle_write_at": _iso_text(latest_bundle_write),
        "bundle_write_age_s": _float_or_none(bundle_write_age_s),
        "materialization_pending": int(pending_count or 0),
        "materializing": int(materializing_count or 0),
        "oldest_active_task_age_s": round(max(0.0, now_s - oldest_active.timestamp()), 3)
        if oldest_active is not None
        else None,
    }


def _latency_severity(value: Any) -> str:
    if value is None:
        return "unavailable"
    lag = float(value)
    if lag <= 10:
        return "healthy"
    if lag <= 60:
        return "warning"
    return "critical"


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _iso_epoch_or_none(value: Any) -> float | None:
    try:
        text = str(value or "").replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def _stream_id_epoch(value: Any) -> float | None:
    try:
        milliseconds = int(_text(value).split("-", 1)[0])
    except (TypeError, ValueError):
        return None
    return milliseconds / 1000.0 if milliseconds > 0 else None


def _iso_text(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
