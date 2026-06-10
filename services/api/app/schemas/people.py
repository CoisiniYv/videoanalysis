"""Pydantic models for people and face registration API responses."""

from __future__ import annotations

from datetime import datetime
import os
from typing import Any

from pydantic import BaseModel, Field


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


def _json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json

        return json.loads(value)
    return dict(value)


def _json_list(value: Any) -> list[Any] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        import json

        return json.loads(value)
    return list(value)


def _media_url(path: Any) -> str | None:
    if not path:
        return None
    value = str(path)
    if value.startswith("/media/"):
        return value
    media_root = os.getenv("MEDIA_ROOT", "/data/video-analytics/media").rstrip("/")
    if media_root and value.startswith(media_root + "/"):
        return "/media/" + value[len(media_root) + 1 :]
    return None


class PersonSummaryResponse(BaseModel):
    person_id: int
    name: str
    external_person_id: str | None = None
    description: str | None = None
    is_active: bool = True
    active_gallery_count: int = 0
    primary_gallery_embedding_id: int | None = None
    primary_source_image_path: str | None = None
    primary_source_image_url: str | None = None
    primary_registered_crop_path: str | None = None
    primary_registered_crop_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "PersonSummaryResponse":
        primary_source_image_path = row.get("primary_source_image_path")
        primary_registered_crop_path = row.get("primary_registered_crop_path")
        return cls(
            person_id=int(row["person_id"]),
            name=row["name"],
            external_person_id=row.get("external_person_id"),
            description=row.get("description"),
            is_active=bool(row.get("is_active", True)),
            active_gallery_count=int(row.get("active_gallery_count") or 0),
            primary_gallery_embedding_id=(
                int(row["primary_gallery_embedding_id"])
                if row.get("primary_gallery_embedding_id") is not None
                else None
            ),
            primary_source_image_path=primary_source_image_path,
            primary_source_image_url=_media_url(primary_source_image_path),
            primary_registered_crop_path=primary_registered_crop_path,
            primary_registered_crop_url=_media_url(primary_registered_crop_path),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


class PersonDetailResponse(BaseModel):
    person_id: int
    name: str
    external_person_id: str | None = None
    description: str | None = None
    is_active: bool = True
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "PersonDetailResponse":
        return cls(
            person_id=int(row["id"]),
            name=row["name"],
            external_person_id=row.get("external_person_id"),
            description=row.get("description"),
            is_active=bool(row.get("is_active", True)),
            payload=_json_dict(row.get("payload")),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


class GalleryEmbeddingResponse(BaseModel):
    gallery_embedding_id: int
    person_id: int
    source_type: str
    source_image_path: str | None = None
    source_image_url: str | None = None
    source_observation_id: str | None = None
    embedding_model: str = "adaface"
    model_version: str | None = None
    embedding_dim: int = 512
    embedding_norm: float | None = None
    quality: float | None = None
    face_bbox: list[Any] | None = None
    landmarks: list[Any] | None = None
    is_primary: bool = False
    is_active: bool = True
    payload: dict[str, Any] = Field(default_factory=dict)
    registered_crop_path: str | None = None
    registered_crop_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "GalleryEmbeddingResponse":
        payload = _json_dict(row.get("payload"))
        source_image_path = row.get("source_image_path")
        registered_crop_path = payload.get("registered_crop_path")
        return cls(
            gallery_embedding_id=int(row["id"]),
            person_id=int(row["person_id"]),
            source_type=row.get("source_type") or "manual_upload",
            source_image_path=source_image_path,
            source_image_url=_media_url(source_image_path),
            source_observation_id=row.get("source_observation_id"),
            embedding_model=row.get("embedding_model") or "adaface",
            model_version=row.get("model_version"),
            embedding_dim=int(row.get("embedding_dim") or 512),
            embedding_norm=(
                float(row["embedding_norm"])
                if row.get("embedding_norm") is not None
                else None
            ),
            quality=float(row["quality"]) if row.get("quality") is not None else None,
            face_bbox=_json_list(row.get("face_bbox")),
            landmarks=_json_list(row.get("landmarks")),
            is_primary=bool(row.get("is_primary", False)),
            is_active=bool(row.get("is_active", True)),
            payload=payload,
            registered_crop_path=registered_crop_path,
            registered_crop_url=_media_url(registered_crop_path),
            created_at=_iso(row.get("created_at")),
            updated_at=_iso(row.get("updated_at")),
        )


class PeopleListResponse(BaseModel):
    people: list[PersonSummaryResponse]
    total: int
    limit: int
    offset: int


class PersonWithGalleryResponse(BaseModel):
    person: PersonDetailResponse
    gallery: list[GalleryEmbeddingResponse]
