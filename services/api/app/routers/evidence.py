"""Database-backed evidence index endpoints for the 8090 operator portal."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request

from libs.evidence_lifecycle import ACTIVE_MATERIALIZATION_STATUSES

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
RAW_CLIP_UNAVAILABLE_STATUSES.update(ACTIVE_MATERIALIZATION_STATUSES)
PRODUCTION_ANNOTATIONS_FILE = "annotations.frame_cache.identity.jsonl"
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/data/video-analytics/media"))


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


@router.get("")
@router.get("/bundles")
def evidence_bundles(
    event_type: Optional[str] = Query(None),
    event_category: Optional[str] = Query("evidence"),
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


@router.get("/bundles/{event_id}")
def evidence_bundle_manifest(
    event_id: str,
    repo: EventRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    row = repo.get_evidence_bundle_index(event_id)
    if row is None:
        return {
            "data": None,
            "error": {"message": f"evidence bundle not found: {event_id}", "code": 404},
            "request_id": request_id,
        }
    return _ok(_bundle_manifest_from_index_row(row), request_id)


@router.get("/bundles/{event_id}/annotations")
def evidence_bundle_annotations(
    event_id: str,
    include_records: bool = Query(default=True),
    repo: EventRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    row = repo.get_evidence_bundle_index(event_id)
    if row is None:
        return {
            "data": None,
            "error": {"message": f"evidence bundle not found: {event_id}", "code": 404},
            "request_id": request_id,
        }
    records = [
        record
        for record in repo.list_evidence_overlay_records(event_id)
        if _dict(record).get("displayable") is not False
    ]
    record_count = len(records)
    fallback_used = False
    fallback_reason = None
    annotation_source_kind = "database_overlay_segments"
    if not records:
        artifact_result = _load_artifact_records(
            row.get("overlay_artifact_uri"),
            include_records=include_records,
            filter_displayable=True,
        )
        records = artifact_result["records"]
        record_count = artifact_result["count"]
        if record_count:
            fallback_used = True
            fallback_reason = "database_overlay_segments_empty"
            annotation_source_kind = "filesystem_overlay_artifact"
    if not include_records:
        records = []
    summary = _dict(row.get("summary"))
    sidecar_summary = _dict(summary.get("sidecar_summary"))
    return _ok(
        {
            "event_id": str(row.get("event_id") or event_id),
            "count": record_count,
            "raw_count": record_count,
            "records": records,
            "annotations": records,
            "annotation_source": "database" if not fallback_used else "filesystem",
            "annotation_source_kind": annotation_source_kind,
            "requested_source": "database",
            "annotation_file": None,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "preview": False,
            "production_ready": sidecar_summary.get("production_ready"),
            "timeline_domain": sidecar_summary.get("timeline_domain"),
            "sidecar_type": sidecar_summary.get("sidecar_type"),
            "canonical_clip": sidecar_summary.get("canonical_clip"),
            "production_file_overwritten": False,
            **_visual_evidence_summary_from_index(row),
            "warnings": [],
            "index_source": "database",
        },
        request_id,
    )


@router.get("/bundles/{event_id}/sink-metadata")
def evidence_bundle_sink_metadata(
    event_id: str,
    repo: EventRepository = Depends(_repo),
    request_id: str = Depends(_request_id),
) -> dict:
    row = repo.get_evidence_bundle_index(event_id)
    if row is None:
        return {
            "data": None,
            "error": {"message": f"evidence bundle not found: {event_id}", "code": 404},
            "request_id": request_id,
        }
    records = repo.list_evidence_timeline_records(event_id)
    fallback_used = False
    fallback_reason = None
    if not records:
        artifact_result = _load_artifact_records(row.get("timeline_artifact_uri"))
        records = artifact_result["records"]
        fallback_used = bool(artifact_result["count"])
        fallback_reason = "database_timeline_empty" if fallback_used else None
    return _ok(
        {
            "event_id": str(row.get("event_id") or event_id),
            "count": len(records),
            "records": records,
            "warnings": [],
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "index_source": "database" if not fallback_used else "filesystem",
        },
        request_id,
    )


def _identity_context_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    matched_person = _dict(summary.get("matched_person"))
    person = _dict(summary.get("person"))
    match = _dict(summary.get("match"))
    observation = _dict(summary.get("observation"))
    person_id = _int_or_none(
        matched_person.get("person_id")
        or person.get("person_id")
        or summary.get("person_id")
    )
    person_name = _text(
        matched_person.get("name")
        or person.get("name")
        or summary.get("person_name")
    )
    external_person_id = _text(
        matched_person.get("external_person_id")
        or person.get("external_person_id")
        or summary.get("external_person_id")
    )
    source_observation_id = _text(
        summary.get("source_observation_id")
        or match.get("source_observation_id")
        or observation.get("source_observation_id")
    )
    person_track_id = _text(
        observation.get("person_track_id")
        or observation.get("track_id")
        or summary.get("person_track_id")
        or summary.get("track_id")
    )
    matched = dict(matched_person or person)
    if person_id is not None:
        matched["person_id"] = person_id
    if person_name:
        matched["name"] = person_name
    if external_person_id:
        matched["external_person_id"] = external_person_id
    return {
        "matched_person": matched or None,
        "person_id": person_id,
        "person_name": person_name or None,
        "external_person_id": external_person_id or None,
        "source_observation_id": source_observation_id or None,
        "person_track_id": person_track_id or None,
    }


def _bundle_summary_from_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(row.get("payload"))
    media = _dict(payload.get("media"))
    summary = _dict(media.get("summary"))
    event_id = str(row.get("event_id") or "")
    raw_clip_path = _text(row.get("raw_clip_path"))
    media_status = _text(row.get("media_status"))
    playback_kind = (
        _text(media.get("playback_kind"))
        or _text(summary.get("playback_kind"))
        or ("image" if media_status == "image_ready" else "video")
    )
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
    face_crop_url = _media_url_from_raw_clip_uri(
        _text(row.get("face_crop_uri")) or _text(media.get("face_crop_uri")) or _text(summary.get("face_crop_uri"))
    )
    full_frame_url = _media_url_from_raw_clip_uri(
        _text(row.get("full_frame_uri")) or _text(media.get("full_frame_uri")) or _text(summary.get("full_frame_uri"))
    )
    annotated_frame_url = _media_url_from_raw_clip_uri(
        _text(row.get("annotated_frame_uri")) or _text(media.get("annotated_frame_uri")) or _text(summary.get("annotated_frame_uri"))
    )
    image_available = playback_kind == "image" and bool(
        face_crop_url or full_frame_url or annotated_frame_url
    )
    warnings = []
    if playback_kind == "image" and not image_available and media_status != "image_pending":
        warnings.append("image_evidence_missing_artifact")
    identity_context = _identity_context_from_summary(summary)
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
            and playback_kind != "image"
        ),
        "playback_kind": playback_kind,
        "image_available": image_available,
        "face_crop_url": face_crop_url,
        "full_frame_url": full_frame_url,
        "annotated_frame_url": annotated_frame_url,
        "raw_clip_name": _basename(raw_clip_path),
        "raw_clip_url": _media_url_from_raw_clip_uri(raw_clip_path),
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
        "evidence_reason": _text(
            media.get("evidence_reason")
            or media.get("error_message")
            or row.get("evidence_reason")
        ),
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
        **identity_context,
        "index_source": "database",
        "warnings": warnings,
    }


def _bundle_manifest_from_index_row(row: dict[str, Any]) -> dict[str, Any]:
    summary = _dict(row.get("summary"))
    materialization = _dict(row.get("materialization"))
    sidecar_summary = _dict(summary.get("sidecar_summary"))
    event_id = str(row.get("event_id") or "")
    raw_clip_uri = _text(row.get("raw_clip_uri") or row.get("raw_clip_artifact_uri"))
    media_status = _text(row.get("media_status"))
    playback_kind = (
        _text(summary.get("playback_kind"))
        or ("image" if media_status == "image_ready" else "video")
    )
    face_crop_uri = _text(row.get("face_crop_uri") or summary.get("face_crop_uri"))
    full_frame_uri = _text(row.get("full_frame_uri") or summary.get("full_frame_uri"))
    annotated_frame_uri = _text(
        row.get("annotated_frame_uri") or summary.get("annotated_frame_uri")
    )
    face_crop_url = _media_url_from_raw_clip_uri(face_crop_uri)
    full_frame_url = _media_url_from_raw_clip_uri(full_frame_uri)
    annotated_frame_url = _media_url_from_raw_clip_uri(annotated_frame_uri)
    image_available = playback_kind == "image" and bool(
        face_crop_url or full_frame_url or annotated_frame_url
    )
    warnings = []
    if playback_kind == "image" and not image_available and media_status != "image_pending":
        warnings.append("image_evidence_missing_artifact")
    materialization_status = (
        _text(summary.get("materialization_status"))
        or _text(summary.get("clip_status"))
        or media_status
    )
    raw_clip_unavailable_reason = _raw_clip_unavailable_reason_for_status(
        materialization_status
    )
    raw_clip_playable = bool(
        raw_clip_uri and not raw_clip_unavailable_reason and playback_kind != "image"
    )
    identity_context = _identity_context_from_summary(summary)
    return {
        "event_id": event_id,
        "playback_kind": playback_kind,
        "image_available": image_available,
        "face_crop_url": face_crop_url,
        "full_frame_url": full_frame_url,
        "annotated_frame_url": annotated_frame_url,
        "camera_name": _text(row.get("camera_name") or row.get("camera_table_name")),
        "alarm_machine_time": _iso(row.get("alarm_machine_time") or row.get("event_created_at")),
        "alarm_machine_time_source": "evidence_bundles.alarm_machine_time",
        "metadata": {
            "event": {
                "event_id": event_id,
                "source_event_id": row.get("source_event_id") or "",
                "event_type": row.get("event_type") or "",
                "source_id": row.get("source_id") or "",
                "camera_id": row.get("camera_id") or "",
                "camera_name": row.get("camera_name") or row.get("camera_table_name") or "",
                "person_id": identity_context["person_id"],
                "person_name": identity_context["person_name"],
                "external_person_id": identity_context["external_person_id"],
                "source_observation_id": identity_context["source_observation_id"],
                "created_at": _iso(row.get("event_created_at")),
            },
            "media": {
                "raw_clip_path": raw_clip_uri,
                "playback_kind": playback_kind,
                "face_crop_path": face_crop_uri,
                "full_frame_path": full_frame_uri,
                "annotated_frame_path": annotated_frame_uri,
                "materialization_status": materialization_status,
                "materialization_reason": row.get("evidence_reason") or "",
                "db_index_status": "ready",
            },
            "annotations": {
                "frontend_overlay_required": row.get("frontend_overlay_required"),
            },
        },
        "summary": summary,
        "raw_clip_url": _media_url_from_raw_clip_uri(raw_clip_uri)
        if raw_clip_playable
        else None,
        "raw_clip_name": _basename(raw_clip_uri),
        "materialization_status": materialization_status,
        "materialization_reason": row.get("evidence_reason") or "",
        "materialization_deadline_at": materialization.get("materialization_deadline_at"),
        "quota_decision": {},
        "degrade_decision": {},
        "raw_clip_unavailable_reason": raw_clip_unavailable_reason,
        "annotations_url": f"/api/v1/evidence/bundles/{event_id}/annotations",
        "sink_metadata_url": f"/api/v1/evidence/bundles/{event_id}/sink-metadata",
        "available_files": ["raw_clip.mov"] if raw_clip_uri else [],
        "sidecar_available": bool(row.get("overlay_artifact_uri")),
        "production_sidecar_ready": sidecar_summary.get("production_ready") is True,
        "production_sidecar_not_ready_reason": None,
        "auto_requires_production_sidecar": False,
        "watchlist_auto_requires_production_sidecar": False,
        "default_annotation_source": "database",
        "default_annotation_source_kind": "database_overlay_segments",
        "default_annotation_file": None,
        "sidecar_summary_path": None,
        **_visual_evidence_summary_from_index(row),
        **identity_context,
        "warnings": warnings,
        "index_source": "database",
    }


def _visual_evidence_summary_from_index(row: dict[str, Any]) -> dict[str, Any]:
    summary = _dict(row.get("summary"))
    sidecar_summary = _dict(summary.get("sidecar_summary"))
    visual_status = (
        _text(row.get("visual_evidence_status"))
        or _text(sidecar_summary.get("visual_binding_status"))
        or "verified"
    )
    reason = (
        _text(sidecar_summary.get("visual_binding_reason"))
        or _text(row.get("evidence_reason"))
        or "database_overlay_segments"
    )
    frame_identity_method = (
        _text(sidecar_summary.get("frame_identity_method"))
        or ("frame_uuid" if sidecar_summary.get("rows_matched_by_frame_uuid") else "")
    )
    return {
        "event_status": row.get("event_type") or sidecar_summary.get("event_type"),
        "visual_evidence_status": "verified" if visual_status == "verified" else visual_status,
        "reason": reason,
        "visual_binding_status": visual_status,
        "visual_binding_reason": reason,
        "source_observation_id": (
            sidecar_summary.get("source_observation_id")
            or _dict(sidecar_summary.get("event_anchor")).get("source_observation_id")
        ),
        "frame_identity_method": frame_identity_method or None,
        "frame_identity_confidence": (
            sidecar_summary.get("frame_identity_confidence")
            or ("high" if frame_identity_method == "frame_uuid" else None)
        ),
        "trigger_face_row_exists": bool(sidecar_summary.get("trigger_face_row_exists")),
        "trigger_face_row_passed_freshness_guard": bool(
            sidecar_summary.get("trigger_face_row_passed_freshness_guard")
        ),
    }


def _raw_clip_unavailable_reason_for_status(status: str | None) -> str | None:
    text = _text(status)
    if text in RAW_CLIP_UNAVAILABLE_STATUSES:
        return f"raw_clip_unavailable:{text}"
    return None


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


def _load_artifact_records(
    value: Any,
    *,
    include_records: bool = True,
    filter_displayable: bool = False,
) -> dict[str, Any]:
    path_text = _text(value)
    if not path_text:
        return {"count": 0, "records": []}
    path = _resolve_artifact_path(path_text)
    if not path.is_file():
        return {"count": 0, "records": []}
    try:
        if include_records:
            text = path.read_text(encoding="utf-8")
        else:
            count = 0
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict) and (
                        not filter_displayable
                        or _dict(payload).get("displayable") is not False
                    ):
                        count += 1
            return {"count": count, "records": []}
    except OSError:
        return {"count": 0, "records": []}
    stripped = text.lstrip()
    if not stripped:
        return {"count": 0, "records": []}
    if stripped[0] == "[":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"count": 0, "records": []}
        records = [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
        if filter_displayable:
            records = [
                record
                for record in records
                if _dict(record).get("displayable") is not False
            ]
        return {"count": len(records), "records": records}
    if stripped[0] == "{":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("frames"), list):
            records = [item for item in payload["frames"] if isinstance(item, dict)]
        elif isinstance(payload, dict):
            records = [payload]
        else:
            records = None
        if records is not None:
            if filter_displayable:
                records = [
                    record
                    for record in records
                    if _dict(record).get("displayable") is not False
                ]
            return {"count": len(records), "records": records}
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and (
            not filter_displayable or _dict(payload).get("displayable") is not False
        ):
            records.append(payload)
    return {"count": len(records), "records": records}


def _resolve_artifact_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_file():
        return path
    if path_text.startswith("/media/"):
        mapped = MEDIA_ROOT / path_text.removeprefix("/media/").lstrip("/")
        if mapped.is_file():
            return mapped
    if path_text.startswith("/evidence/"):
        mapped = MEDIA_ROOT / "evidence" / path_text.removeprefix("/evidence/").lstrip("/")
        if mapped.is_file():
            return mapped
    return path


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


def _media_url_from_raw_clip_uri(uri: str) -> str | None:
    text = _text(uri)
    if not text:
        return None
    if text.startswith("/media/"):
        return text
    marker = "/media/"
    if marker in text:
        return marker + text.split(marker, 1)[1].lstrip("/")
    return None


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
