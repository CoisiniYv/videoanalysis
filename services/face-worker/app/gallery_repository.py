"""GalleryRepository — PostgreSQL CRUD for ``person_gallery_embeddings``.

Manages long-term registered gallery vectors.  Follows the
FaceObservationRepository pattern (psycopg.Connection, dict_row, pgvector).
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional

import psycopg
from pgvector.psycopg import Vector
from psycopg.rows import dict_row

from app.vector_store import register_vector_if_supported

_EMBEDDING_DIM = 512
_MIN_NORM = 0.90
_MAX_NORM = 1.10

_INSERT_SQL = """
INSERT INTO person_gallery_embeddings (
    person_id, source_type, source_image_path,
    source_observation_id,
    embedding_model, model_version,
    embedding_dim, embedding, embedding_norm,
    quality, face_bbox, landmarks,
    is_primary, is_active, payload
) VALUES (
    %(person_id)s, %(source_type)s, %(source_image_path)s,
    %(source_observation_id)s,
    %(embedding_model)s, %(model_version)s,
    %(embedding_dim)s, %(embedding)s, %(embedding_norm)s,
    %(quality)s, %(face_bbox)s::jsonb, %(landmarks)s::jsonb,
    %(is_primary)s, %(is_active)s, %(payload)s::jsonb
)
RETURNING id
"""

_GET_BY_ID_SQL = """
SELECT id, person_id, source_type, source_image_path,
       source_observation_id,
       embedding_model, model_version,
       embedding_dim, embedding_norm,
       quality, face_bbox, landmarks,
       is_primary, is_active, payload,
       created_at, updated_at
FROM person_gallery_embeddings
WHERE id = %(id)s
"""

_LIST_BY_PERSON_SQL = """
SELECT id, person_id, source_type, source_image_path,
       source_observation_id,
       embedding_model, model_version,
       embedding_dim, embedding_norm,
       quality, face_bbox, landmarks,
       is_primary, is_active, payload,
       created_at, updated_at
FROM person_gallery_embeddings
WHERE person_id = %(person_id)s AND is_active = true
ORDER BY is_primary DESC, id
"""

_DEACTIVATE_SQL = """
UPDATE person_gallery_embeddings SET
    is_active = false,
    updated_at = now()
WHERE id = %(id)s
RETURNING id
"""


def _to_jsonb(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False)


def _validate_embedding(embedding: list | tuple) -> list[float]:
    """Validate and convert an embedding for gallery storage.

    Raises ValueError for any validation failure.
    """
    if not isinstance(embedding, (list, tuple)):
        raise ValueError(
            f"embedding must be list or tuple, got {type(embedding).__name__}"
        )
    if len(embedding) != _EMBEDDING_DIM:
        raise ValueError(
            f"embedding length={len(embedding)}, expected {_EMBEDDING_DIM}"
        )

    converted: list[float] = []
    sq_sum = 0.0
    for i, val in enumerate(embedding):
        if isinstance(val, bool):
            raise ValueError(
                f"embedding[{i}] invalid embedding element type: bool"
            )
        try:
            f = float(val)
        except (TypeError, ValueError):
            raise ValueError(
                f"embedding[{i}] invalid embedding element type: {type(val).__name__}"
            )
        if not math.isfinite(f):
            raise ValueError(f"embedding[{i}] not finite: {f}")
        converted.append(f)
        sq_sum += f * f

    norm = math.sqrt(sq_sum)
    if not (_MIN_NORM <= norm <= _MAX_NORM):
        raise ValueError(
            f"embedding L2 norm={norm:.6f} outside "
            f"[{_MIN_NORM}, {_MAX_NORM}]"
        )
    return converted


class GalleryRepository:
    """CRUD operations for ``person_gallery_embeddings``."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        register_vector_if_supported(conn)

    def add_embedding(
        self,
        person_id: int,
        embedding: list[float],
        *,
        source_type: str = "manual_upload",
        source_image_path: str | None = None,
        source_observation_id: str | None = None,
        embedding_model: str = "adaface",
        model_version: str | None = None,
        quality: float | None = None,
        face_bbox: list | None = None,
        landmarks: list | None = None,
        is_primary: bool = False,
        is_active: bool = True,
        payload: dict | None = None,
    ) -> int:
        """Add a gallery embedding for a person.

        Returns the new row's BIGINT id.
        Raises ValueError if embedding validation fails.
        """
        vec = _validate_embedding(embedding)
        norm = math.sqrt(sum(x * x for x in vec))

        params = {
            "person_id": person_id,
            "source_type": source_type,
            "source_image_path": source_image_path,
            "source_observation_id": source_observation_id,
            "embedding_model": embedding_model,
            "model_version": model_version,
            "embedding_dim": _EMBEDDING_DIM,
            "embedding": Vector(vec),
            "embedding_norm": norm,
            "quality": quality,
            "face_bbox": _to_jsonb(face_bbox),
            "landmarks": _to_jsonb(landmarks),
            "is_primary": is_primary,
            "is_active": is_active,
            "payload": _to_jsonb(payload) if payload else "{}",
        }
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_INSERT_SQL, params)
            row = cur.fetchone()
            return int(row["id"])

    def get_by_id(self, gallery_id: int) -> dict | None:
        """Return gallery embedding row by id, or None if not found.

        Does NOT return the embedding vector (too large for general use).
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_GET_BY_ID_SQL, {"id": gallery_id})
            return cur.fetchone()

    def list_by_person(self, person_id: int) -> List[Dict[str, Any]]:
        """Return all active gallery embeddings for a person.

        Primary embeddings first, then by id.  Does NOT return vectors.
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_LIST_BY_PERSON_SQL, {"person_id": person_id})
            return list(cur.fetchall())

    def deactivate(self, gallery_id: int) -> bool:
        """Soft-deactivate a gallery embedding. Returns True if updated."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_DEACTIVATE_SQL, {"id": gallery_id})
            return cur.fetchone() is not None
