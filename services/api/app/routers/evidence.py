"""Database-backed evidence index endpoints for the 8090 operator portal."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request

from app.db import get_conn
from app.repositories.events import EventRepository


router = APIRouter(prefix="/api/v1/evidence", tags=["evidence"])

EPOCH_MS_MIN = 946684800000
EPOCH_MS_MAX = 4102444800000
RAW_CLIP_UNAVAILABLE_STATUSES = {
    "duration_guard_failed",
    "failed",
    "manifest_ready",
    "materialization_pending",
    "materializing",
    "materialization_deferred",
    "materialization_failed",
    "materialization_expired",
    "materialization_skipped",
    "media_deleted",
    "media_expired",
    "not_implemented",
    "pending",
    "queued",
    "waiting_proof",
    "replaying",
    "finalizing",
}
PRODUCTION_ANNOTATIONS_FILE = "annotations.frame_cache.identity.jsonl"


def _request_id(request: Request) -> str:
    return str(uuid.uuid4())


def _repo() -> EventRepository:
    conn = get_conn()
    try:
        yield EventRepository(conn)
    finally:
        conn.close()


def _ok(data: object, request_id: str) -> dict:
    return {"data": data, "error": None, "request_id": request_id}


@router.get("/health")
def evidence_health(request_id: str = Depends(_request_id)) -> dict:
    return _ok({"status": "ok", "index_source": "database"}, request_id)


@router.get("/bundles")
def evidence_bundles(
    event_type: Optional[str] = Query(None),
    event_category: Optional[str] = Query(None),
    source_id: Optional[str] = Query(None),
    camera_id: Optional[str] = Query(None),
    event_id: Optional[str] = Query(None),
    person: Optional[str] = Query(None),
    clip_status: Optional[str] = Query(None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    repo: EventRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    rows, total = repo.list_evidence_bundle_summaries(
        event_type=event_type,
        event_category=event_category,
        source_id=source_id,
        camera_id=camera_id,
        event_id=event_id,
        person=person,
        clip_status=clip_status,
        limit=limit,
        offset=offset,
    )
    return _ok(
        {
            "bundles": [_bundle_summary_from_row(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "index_source": "database",
            "warnings": [],
        },
        request_id,
    )


def _bundle_summary_from_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(row.get("payload"))
    media = _dict(payload.get("media"))
    summary = _dict(media.get("summary"))
    event_id = str(row.get("event_id") or "")
    raw_clip_path = _text(row.get("raw_clip_path"))
    media_status = _text(row.get("media_status"))
    clip_status = (
        _text(row.get("clip_status"))
        or _text(media.get("clip_status"))
        or media_status
        or "not_implemented"
    )
    media_deleted = media_status in {"media_deleted", "media_expired"} or bool(
        _text(_dict(payload.get("maintenance")).get("deleted_at"))
    )
    alarm_time, alarm_source = _alarm_machine_time(row, payload, media)
    matched_objects = _int_or_none(
        media.get("matched_objects")
        or media.get("known_face_count")
        or media.get("person_count")
        or summary.get("matched_objects")
    )
    unknown_objects = _int_or_none(
        media.get("unknown_objects")
        or media.get("unknown_face_count")
        or summary.get("unknown_objects")
    )
    return {
        "event_id": event_id,
        "source_event_id": row.get("source_event_id") or "",
        "event_type": row.get("event_type") or "",
        "source_id": row.get("source_id") or "",
        "camera_id": row.get("camera_id") or "",
        "camera_name": _camera_name(payload, media, row),
        "alarm_machine_time": alarm_time,
        "alarm_machine_time_source": alarm_source,
        "raw_clip_available": bool(
            raw_clip_path
            and not media_deleted
            and clip_status not in RAW_CLIP_UNAVAILABLE_STATUSES
        ),
        "raw_clip_name": _basename(raw_clip_path),
        "raw_clip_url": f"/api/bundles/{event_id}/media/raw_clip" if raw_clip_path else None,
        "annotations_available": _is_production_annotations_path(
            row.get("annotations_jsonl_path")
        ),
        "annotation_lines": _int_or_none(media.get("annotation_lines")),
        "clip_status": clip_status,
        "visual_evidence_status": _text(media.get("visual_evidence_status")),
        "frontend_overlay_required": _bool_or_none(media.get("frontend_overlay_required")),
        "matched_objects": matched_objects,
        "unknown_objects": unknown_objects,
        "created_at": _iso(row.get("created_at")),
        "media_status": media_status,
        "evidence_state": _text(media.get("evidence_state")) or media_status,
        "evidence_reason": _text(media.get("evidence_reason") or media.get("error_message")),
        "materialization_status": (
            _text(media.get("materialization_status"))
            or _text(row.get("latest_materialization_status"))
        ),
        "materialization_reason": _text(media.get("materialization_reason")),
        "materialization_deadline_at": (
            _text(media.get("materialization_deadline_at"))
            or _text(row.get("latest_materialization_deadline_at"))
        ),
        "quota_decision": _dict(media.get("quota_decision")),
        "degrade_decision": _dict(media.get("degrade_decision")),
        "evidence_task_count": _int_or_none(row.get("evidence_task_count")) or 0,
        "latest_task_status": row.get("latest_task_status"),
        "index_source": "database",
        "warnings": [],
    }


def _alarm_machine_time(
    row: dict[str, Any],
    payload: dict[str, Any],
    media: dict[str, Any],
) -> tuple[str | None, str | None]:
    for source, value in (
        ("payload.alarm_machine_time", payload.get("alarm_machine_time")),
        ("media.alarm_machine_time", media.get("alarm_machine_time")),
        ("events.created_at", row.get("created_at")),
        ("events.start_ts", row.get("start_ts")),
    ):
        text = _iso(value)
        if text:
            return text, source

    for source, value in (
        ("payload.event_ts_ms", payload.get("event_ts_ms")),
        ("events.event_ts_ms", row.get("event_ts_ms")),
    ):
        iso_value = _iso_from_epoch_ms(value)
        if iso_value:
            return iso_value, source

    return None, None


def _camera_name(
    payload: dict[str, Any],
    media: dict[str, Any],
    row: dict[str, Any] | None = None,
) -> str | None:
    camera = _dict(payload.get("camera"))
    row = row or {}
    for value in (
        payload.get("camera_name"),
        media.get("camera_name"),
        camera.get("name"),
        row.get("camera_name"),
    ):
        text = _text(value)
        if text:
            return text
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_production_annotations_path(value: Any) -> bool:
    text = _text(value)
    if not text:
        return False
    return text.rsplit("/", 1)[-1] == PRODUCTION_ANNOTATIONS_FILE


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _iso_from_epoch_ms(value: Any) -> str | None:
    try:
        raw = str(value).strip()
        if not raw:
            return None
        epoch_ms = int(float(raw))
    except (TypeError, ValueError):
        return None
    if EPOCH_MS_MIN <= epoch_ms <= EPOCH_MS_MAX:
        return (
            datetime.fromtimestamp(epoch_ms / 1000, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return None


def _basename(path: str) -> str | None:
    if not path:
        return None
    return path.rstrip("/").rsplit("/", 1)[-1] or None


def _int_or_none(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None
