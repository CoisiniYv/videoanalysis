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
        "metadata_status": "not_implemented",
        "metadata_path": None,
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
    algorithm_type: str = ""
    algorithm_version: Optional[str] = None
    severity: str = "medium"
    confidence: float = 0.0
    start_ts_ms: int = 0
    end_ts_ms: Optional[int] = None
    start_ts: Optional[str] = None
    end_ts: Optional[str] = None
    event_ts_ms: int = 0
    frame_uuid: Optional[str] = None
    keyframe_uuid: Optional[str] = None
    status: str = "new"
    snapshot_path: Optional[str] = None
    annotated_snapshot_path: Optional[str] = None
    clip_path: Optional[str] = None
    annotated_clip_path: Optional[str] = None
    metadata_path: Optional[str] = None
    snapshot_url: Optional[str] = None
    annotated_snapshot_url: Optional[str] = None
    clip_url: Optional[str] = None
    annotated_clip_url: Optional[str] = None
    metadata_url: Optional[str] = None
    recording_strategy: str = "reserved"
    media_status: str = "not_implemented"
    snapshot_required: bool = False
    clip_required: bool = False
    evidence_policy: Dict[str, Any] = Field(default_factory=dict)
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
        annotated_clip_path = (
            row.get("annotated_clip_path")
            or media.get("annotated_clip_path")
        )
        metadata_path = row.get("metadata_path") or media.get("metadata_path")

        def _media_url(path: str | None) -> str | None:
            if not path:
                return None
            p = str(path)
            if p.startswith(f"{media_base_url}/"):
                return p
            host_media_root = "/data/video-analytics/media"
            if p.startswith(f"{host_media_root}/"):
                return f"{media_base_url}{p[len(host_media_root):]}"
            if p.startswith("/"):
                return f"{media_base_url}{p}"
            return f"{media_base_url}/{p}"

        annotated_clip_url = _media_url(annotated_clip_path)
        clip_url = _media_url(clip_path)
        snapshot_url = _media_url(snapshot_path)
        annotated_snapshot_url = _media_url(annotated_snapshot_path)
        metadata_url = _media_url(metadata_path)

        return cls(
            id=str(row.get("id", "")),
            source_event_id=row.get("source_event_id", ""),
            event_type=row.get("event_type", ""),
            camera_id=row.get("camera_id", ""),
            source_id=row.get("source_id", ""),
            track_id=str(row.get("track_id", "")),
            person_id=row.get("person_id"),
            algorithm_type=row.get("algorithm_type", ""),
            algorithm_version=row.get("algorithm_version"),
            severity=row.get("severity", "medium"),
            confidence=float(row.get("confidence", 0.0)),
            start_ts_ms=int(row.get("start_ts_ms", 0) or 0),
            end_ts_ms=row.get("end_ts_ms"),
            start_ts=_iso(row.get("start_ts")),
            end_ts=_iso(row.get("end_ts")),
            event_ts_ms=int(row.get("event_ts_ms", 0)),
            frame_uuid=row.get("frame_uuid"),
            keyframe_uuid=row.get("keyframe_uuid"),
            status=row.get("status", "new"),
            snapshot_path=snapshot_path,
            annotated_snapshot_path=annotated_snapshot_path,
            clip_path=clip_path,
            annotated_clip_path=annotated_clip_path,
            metadata_path=metadata_path,
            snapshot_url=snapshot_url,
            annotated_snapshot_url=annotated_snapshot_url,
            clip_url=clip_url,
            annotated_clip_url=annotated_clip_url,
            metadata_url=metadata_url,
            recording_strategy=row.get("recording_strategy", "reserved"),
            media_status=row.get("media_status", "not_implemented"),
            snapshot_required=bool(row.get("snapshot_required", False)),
            clip_required=bool(row.get("clip_required", False)),
            evidence_policy=row.get("evidence_policy") or payload.get("evidence_policy", {}),
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


class EvidenceTaskResponse(BaseModel):
    task_id: str
    event_id: str
    source_event_id: str
    camera_id: str = ""
    source_id: str = ""
    event_type: str = ""
    event_ts_ms: int = 0
    task_type: str = "snapshot_clip"
    snapshot_required: bool = False
    clip_required: bool = False
    pre_seconds: int = 5
    post_seconds: int = 5
    status: str = "pending"
    snapshot_path: Optional[str] = None
    clip_path: Optional[str] = None
    metadata_path: Optional[str] = None
    output_root: Optional[str] = None
    storage_fallback_used: bool = False
    storage_fallback_reason: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: dict) -> "EvidenceTaskResponse":
        return cls(
            task_id=str(row.get("task_id", "")),
            event_id=str(row.get("event_id", "")),
            source_event_id=row.get("source_event_id", ""),
            camera_id=row.get("camera_id", ""),
            source_id=row.get("source_id", ""),
            event_type=row.get("event_type", ""),
            event_ts_ms=int(row.get("event_ts_ms", 0) or 0),
            task_type=row.get("task_type", "snapshot_clip"),
            snapshot_required=bool(row.get("snapshot_required", False)),
            clip_required=bool(row.get("clip_required", False)),
            pre_seconds=int(row.get("pre_seconds", 5) or 5),
            post_seconds=int(row.get("post_seconds", 5) or 5),
            status=row.get("status", "pending"),
            snapshot_path=row.get("snapshot_path"),
            clip_path=row.get("clip_path"),
            metadata_path=row.get("metadata_path"),
            output_root=row.get("output_root"),
            storage_fallback_used=bool(row.get("storage_fallback_used", False)),
            storage_fallback_reason=row.get("storage_fallback_reason"),
            retry_count=int(row.get("retry_count", 0) or 0),
            max_retries=int(row.get("max_retries", 3) or 3),
            claimed_by=row.get("claimed_by"),
            claimed_at=_iso(row.get("claimed_at")),
            error_message=row.get("error_message"),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


class EventEvidenceResponse(BaseModel):
    event_id: str
    source_event_id: str
    event_type: str
    media_status: str = "not_implemented"
    snapshot_status: str = "not_implemented"
    clip_status: str = "not_implemented"
    metadata_status: str = "not_implemented"
    snapshot_path: Optional[str] = None
    clip_path: Optional[str] = None
    raw_clip_path: Optional[str] = None
    annotated_clip_path: Optional[str] = None
    metadata_path: Optional[str] = None
    snapshot_url: Optional[str] = None
    clip_url: Optional[str] = None
    raw_clip_url: Optional[str] = None
    annotated_clip_url: Optional[str] = None
    metadata_url: Optional[str] = None
    clip_error_message: Optional[str] = None
    error_message: Optional[str] = None
    event: EventResponse
    evidence_tasks: List[EvidenceTaskResponse] = Field(default_factory=list)
    evidence_detail: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_event_and_tasks(
        cls,
        event: EventResponse,
        evidence_tasks: List[EvidenceTaskResponse],
        evidence_detail: Dict[str, Any] | None = None,
    ) -> "EventEvidenceResponse":
        first_task = evidence_tasks[0] if evidence_tasks else None
        metadata_path = event.metadata_path or (
            first_task.metadata_path if first_task else None
        )
        metadata_url = event.metadata_url
        if metadata_path and not metadata_url:
            path = str(metadata_path)
            if path.startswith("/media/"):
                metadata_url = path
            elif path.startswith("/"):
                metadata_url = f"/media{path}"
            else:
                metadata_url = f"/media/{path}"
        error_message = event.media.get("error_message") if event.media else None
        if not error_message and first_task:
            error_message = first_task.error_message
        clip_error_message = event.media.get("clip_error_message") if event.media else None
        raw_clip_path = event.media.get("raw_clip_path") if event.media else None
        annotated_clip_path = event.media.get("annotated_clip_path") if event.media else None

        return cls(
            event_id=event.id,
            source_event_id=event.source_event_id,
            event_type=event.event_type,
            media_status=event.media_status,
            snapshot_status=event.media.get("snapshot_status", "not_implemented"),
            clip_status=event.media.get("clip_status", "not_implemented"),
            metadata_status=event.media.get("metadata_status", "not_implemented"),
            snapshot_path=event.snapshot_path,
            clip_path=event.clip_path,
            raw_clip_path=raw_clip_path,
            annotated_clip_path=annotated_clip_path,
            metadata_path=metadata_path,
            snapshot_url=event.snapshot_url,
            clip_url=event.clip_url,
            raw_clip_url=event.clip_url if raw_clip_path == event.clip_path else None,
            annotated_clip_url=event.annotated_clip_url,
            metadata_url=metadata_url,
            clip_error_message=clip_error_message,
            error_message=error_message,
            event=event,
            evidence_tasks=evidence_tasks,
            evidence_detail=evidence_detail or {},
        )
