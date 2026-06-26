"""Pydantic models for storage maintenance APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


DeleteMode = Literal["trash", "permanent"]


class EvidenceDeletePreviewRequest(BaseModel):
    event_ids: list[str] = Field(default_factory=list)
    time_from: datetime | None = None
    time_to: datetime | None = None
    older_than_days: int | None = Field(default=None, ge=0)
    event_type: str | None = None
    event_category: str | None = None
    camera_id: str | None = None
    source_id: str | None = None
    clip_status: str | None = None
    visual_evidence_status: str | None = None
    has_raw_clip: bool | None = None
    allow_stale_pending_tasks: bool = False
    delete_mode: DeleteMode = "trash"
    max_items: int = Field(default=1000, ge=1, le=10000)
    operator: str | None = None
    reason: str | None = None


class EvidenceDeleteExecuteRequest(BaseModel):
    preview_id: str
    confirm_token: str
    delete_mode: DeleteMode = "trash"
    reason: str
    operator: str = "operator"
    candidate_hash: str | None = None


class MaintenanceExecutionControlRequest(BaseModel):
    enabled: bool
    reason: str = Field(min_length=1, max_length=500)
    operator: str = "operator"


class PeopleDeletePreviewRequest(BaseModel):
    person_ids: list[int] = Field(default_factory=list)
    external_person_ids: list[str] = Field(default_factory=list)
    include_gallery: bool = True
    created_from: datetime | None = None
    created_to: datetime | None = None
    older_than_days: int | None = Field(default=None, ge=0)
    operator: str | None = None
    reason: str | None = None


class PeopleDeleteExecuteRequest(BaseModel):
    preview_id: str
    confirm_token: str
    reason: str
    operator: str = "operator"
    candidate_hash: str | None = None


class GalleryDeletePreviewRequest(BaseModel):
    gallery_embedding_ids: list[int] = Field(default_factory=list)
    person_ids: list[int] = Field(default_factory=list)
    created_from: datetime | None = None
    created_to: datetime | None = None
    older_than_days: int | None = Field(default=None, ge=0)
    only_inactive: bool = False
    operator: str | None = None
    reason: str | None = None


class GalleryDeleteExecuteRequest(BaseModel):
    preview_id: str
    confirm_token: str
    reason: str
    operator: str = "operator"
    candidate_hash: str | None = None


class FaceMediaOrphansPreviewRequest(BaseModel):
    older_than_days: int | None = Field(default=7, ge=0)
    allow_inactive_reference_cleanup: bool = False
    inactive_reference_retention_days: int = Field(default=30, ge=0)
    delete_mode: DeleteMode = "trash"
    max_items: int = Field(default=1000, ge=1, le=10000)
    operator: str | None = None
    reason: str | None = None


class FaceMediaCleanupExecuteRequest(BaseModel):
    preview_id: str
    confirm_token: str
    delete_mode: DeleteMode = "trash"
    reason: str
    operator: str = "operator"
    candidate_hash: str | None = None


class JobDetailQuery(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    status: str | None = None


def iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
