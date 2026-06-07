"""Dataclass definitions for C1J frame evidence metadata contracts.

The validation module accepts plain dictionaries because Redis, media-worker,
and future Savant exporters will exchange JSON-compatible payloads. These
dataclasses document the canonical shape without adding a runtime dependency on
Pydantic or any service framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SCHEMA_VERSION = "1.0"
FRAME_ANNOTATION_MESSAGE_TYPE = "frame_annotation"
IDENTITY_PATCH_MESSAGE_TYPE = "identity_patch"

ObjectType = Literal["person", "face", "behavior_event"]
EventType = Literal["watchlist_hit", "live_search_hit", "gallery_match"]

ALLOWED_OBJECT_TYPES = {"person", "face", "behavior_event"}
ALLOWED_IDENTITY_EVENT_TYPES = {"watchlist_hit", "live_search_hit", "gallery_match"}


@dataclass(frozen=True)
class BBox:
    format: Literal["xyxy"]
    xyxy: list[float]
    confidence: float | None = None
    coordinate_space: Literal["pixel"] = "pixel"


@dataclass(frozen=True)
class FrameAnnotationObject:
    object_id: str
    object_type: ObjectType
    bbox: BBox
    track_id: str | None = None
    quality: dict[str, Any] = field(default_factory=dict)
    source_observation_id: str | None = None
    label: dict[str, Any] | None = None
    annotation_role: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FrameAnnotationMessage:
    schema_version: str
    producer: str
    message_type: Literal["frame_annotation"]
    source_id: str
    camera_id: str
    frame_pts: int | None
    frame_uuid: str | None
    frame_num: int | None
    timestamp_ms: int | None
    objects: list[FrameAnnotationObject]
    ttl_seconds: int
    created_at: str | None = None


@dataclass(frozen=True)
class IdentityPatchMessage:
    schema_version: str
    message_type: Literal["identity_patch"]
    source_id: str
    camera_id: str
    source_observation_id: str
    event_type: EventType
    event_id: str | int | None = None
    person_id: str | int | None = None
    external_person_id: str | None = None
    display_name: str | None = None
    similarity: float | None = None
    threshold: float | None = None
    frame_uuid: str | None = None
    frame_pts: int | None = None
    timestamp_ms: int | None = None
    bbox: list[float] | None = None
    created_at: str | None = None
