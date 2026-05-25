"""Pydantic response models for event endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _iso(ts: Any) -> str | None:
    """Convert a datetime or ISO string to ISO 8601 string.  Returns None for None."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


def _safe_media(payload: dict | None) -> dict:
    """Derive a safe MVP media dict from *payload*.

    If ``payload.media`` exists it is returned as-is, with missing fields
    filled from the default fallback.  Otherwise a minimal reserved/
    not_implemented fallback is returned.
    """
    fallback = {
        "snapshot_status": "not_implemented",
        "clip_status": "not_implemented",
        "recording_strategy": "reserved",
        "replay_job_id": None,
        "sink_output_path": None,
        "error_message": None,
    }
    if payload and isinstance(payload.get("media"), dict):
        merged = {**fallback, **payload["media"]}
        return merged
    return fallback


class EventResponse(BaseModel):
    id: str
    source_event_id: str
    event_type: str
    camera_id: str
    source_id: str = ""
    track_id: str = ""
    person_id: Optional[int] = None
    severity: str = "medium"
    confidence: float = 0.0
    start_ts: Optional[str] = None
    end_ts: Optional[str] = None
    event_ts_ms: int = 0
    frame_uuid: Optional[str] = None
    keyframe_uuid: Optional[str] = None
    status: str = "new"
    snapshot_path: Optional[str] = None
    annotated_snapshot_path: Optional[str] = None
    clip_path: Optional[str] = None
    snapshot_url: Optional[str] = None
    annotated_snapshot_url: Optional[str] = None
    clip_url: Optional[str] = None
    recording_strategy: str = "reserved"
    media_status: str = "not_implemented"
    media: Dict[str, Any] = Field(default_factory=dict)
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: dict, media_base_url: str = "/media") -> EventResponse:
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            import json

            payload = json.loads(payload)

        media = _safe_media(payload)

        clip_path = row.get("clip_path")
        snapshot_path = row.get("snapshot_path") or media.get("snapshot_path")
        annotated_snapshot_path = (
            row.get("annotated_snapshot_path")
            or media.get("annotated_snapshot_path")
        )

        def _media_url(path: str | None) -> str | None:
            if not path:
                return None
            p = str(path)
            if p.startswith(f"{media_base_url}/"):
                return p
            if p.startswith("/"):
                return f"{media_base_url}{p}"
            return f"{media_base_url}/{p}"

        clip_url = _media_url(clip_path)
        snapshot_url = _media_url(snapshot_path)
        annotated_snapshot_url = _media_url(annotated_snapshot_path)

        return cls(
            id=str(row.get("id", "")),
            source_event_id=row.get("source_event_id", ""),
            event_type=row.get("event_type", ""),
            camera_id=row.get("camera_id", ""),
            source_id=row.get("source_id", ""),
            track_id=str(row.get("track_id", "")),
            person_id=row.get("person_id"),
            severity=row.get("severity", "medium"),
            confidence=float(row.get("confidence", 0.0)),
            start_ts=_iso(row.get("start_ts")),
            end_ts=_iso(row.get("end_ts")),
            event_ts_ms=int(row.get("event_ts_ms", 0)),
            frame_uuid=row.get("frame_uuid"),
            keyframe_uuid=row.get("keyframe_uuid"),
            status=row.get("status", "new"),
            snapshot_path=snapshot_path,
            annotated_snapshot_path=annotated_snapshot_path,
            clip_path=clip_path,
            snapshot_url=snapshot_url,
            annotated_snapshot_url=annotated_snapshot_url,
            clip_url=clip_url,
            recording_strategy=row.get("recording_strategy", "reserved"),
            media_status=row.get("media_status", "not_implemented"),
            media=media,
            payload=payload,
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


class EventListResponse(BaseModel):
    events: List[EventResponse]
    total: int
    limit: int
    offset: int


class StatusUpdateRequest(BaseModel):
    operator: str = ""
    comment: str = ""
