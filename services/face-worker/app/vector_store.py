"""FaceVectorStore — pgvector similarity search over face_observations and gallery.

F3.2: exact cosine distance search harness over face_observations.
F3.4: gallery search over person_gallery_embeddings.

Similarity metric:
  similarity = 1 - cosine_distance  (via pgvector ``<=>`` operator)
  Useful range near [0, 1] for L2-normalized AdaFace embeddings (norm ~1.0).
  This is NOT a calibrated identity probability.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import psycopg
from pgvector.psycopg import Vector, register_vector
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

_EMBEDDING_DIM = 512
_MIN_NORM = 0.90
_MAX_NORM = 1.10
_MIN_TOP_K = 1
_MAX_TOP_K = 100
_MIN_SIMILARITY = 0.0
_MAX_SIMILARITY = 1.0

# Gallery search SELECT (no embedding vector by default).
_GALLERY_METADATA_SELECT = """
SELECT
    pge.id,
    pge.person_id,
    p.name AS person_name,
    p.external_person_id,
    pge.source_type,
    pge.embedding_model,
    pge.is_primary,
    pge.quality,
    1 - (pge.embedding <=> %(query_embedding)s) AS similarity,
    pge.embedding <=> %(query_embedding)s AS distance,
    pge.created_at
"""

_GALLERY_EMBEDDING_SELECT = """
SELECT
    pge.id,
    pge.person_id,
    p.name AS person_name,
    p.external_person_id,
    pge.source_type,
    pge.embedding_model,
    pge.is_primary,
    pge.quality,
    pge.embedding,
    1 - (pge.embedding <=> %(query_embedding)s) AS similarity,
    pge.embedding <=> %(query_embedding)s AS distance,
    pge.created_at
"""

# SELECT returning metadata only (no embedding vector).
_METADATA_SELECT = """
SELECT
    source_observation_id,
    camera_id,
    source_id,
    track_id,
    timestamp_ms,
    face_bbox,
    landmarks,
    face_confidence,
    quality,
    1 - (embedding <=> %(query_embedding)s) AS similarity,
    embedding <=> %(query_embedding)s AS distance,
    created_at
"""

# SELECT including the embedding vector (only when include_embedding=True).
_EMBEDDING_SELECT = """
SELECT
    source_observation_id,
    camera_id,
    source_id,
    track_id,
    timestamp_ms,
    face_bbox,
    landmarks,
    face_confidence,
    quality,
    embedding,
    1 - (embedding <=> %(query_embedding)s) AS similarity,
    embedding <=> %(query_embedding)s AS distance,
    created_at
"""


def _validate_query_embedding(embedding: object) -> list[float]:
    """Validate and normalize a query embedding.

    Returns a ``list[float]`` suitable for pgvector search.
    Raises ``ValueError`` for any validation failure.
    """
    if not isinstance(embedding, (list, tuple)):
        raise ValueError(
            f"embedding must be list or tuple, got {type(embedding).__name__}"
        )

    length = len(embedding)
    if length != _EMBEDDING_DIM:
        raise ValueError(
            f"embedding length={length}, expected {_EMBEDDING_DIM}"
        )

    converted: list[float] = []
    sq_sum = 0.0

    for i, val in enumerate(embedding):
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(
                f"embedding[{i}] invalid type={type(val).__name__} "
                f"value={repr(val)}"
            )
        f = float(val)
        if not math.isfinite(f):
            raise ValueError(
                f"embedding[{i}] not finite: {f}"
            )
        converted.append(f)
        sq_sum += f * f

    norm = math.sqrt(sq_sum)
    if not (_MIN_NORM <= norm <= _MAX_NORM):
        raise ValueError(
            f"embedding L2 norm={norm:.6f} outside "
            f"[{_MIN_NORM}, {_MAX_NORM}] — expected L2-normalized AdaFace embedding"
        )

    return converted


class FaceVectorStore:
    """pgvector similarity search over the ``face_observations`` table.

    Uses exact cosine distance (``<=>``) — no approximate index in F3.2.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        register_vector(conn)

    def search_similar_faces(
        self,
        embedding: list[float],
        top_k: int = 10,
        min_similarity: float | None = None,
        camera_scope: list[str] | None = None,
        *,
        include_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        """Return topK most similar face observations for *embedding*.

        Args:
            embedding: 512-d L2-normalized query vector (AdaFace).
            top_k: Number of results (clamped to [1, 100]).
            min_similarity: Optional cosine similarity threshold [0.0, 1.0].
            camera_scope: Optional list of camera_ids to restrict search.
            include_embedding: If True, include the 512-d embedding in results.

        Returns:
            List of dicts ordered by descending similarity. Each dict contains
            metadata fields plus ``similarity`` and ``distance``.
        """
        # Validate and normalise query embedding
        query_vector = _validate_query_embedding(embedding)

        # Clamp / reject parameters
        _original_top_k = top_k
        top_k = max(_MIN_TOP_K, min(top_k, _MAX_TOP_K))
        if _original_top_k != top_k:
            logger.warning("top_k clamped from %s to %s", _original_top_k, top_k)

        if min_similarity is not None:
            if not (0.0 <= min_similarity <= 1.0):
                raise ValueError(
                    f"min_similarity={min_similarity} outside [0.0, 1.0]"
                )

        # Empty camera_scope = no cameras to search
        if camera_scope is not None and len(camera_scope) == 0:
            return []

        # Build dynamic SQL
        sql, params = self._build_query(
            min_similarity=min_similarity,
            camera_scope=camera_scope,
            include_embedding=include_embedding,
        )

        params["query_embedding"] = Vector(query_vector)
        params["top_k"] = top_k

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        return list(rows)

    def _build_query(
        self,
        min_similarity: float | None,
        camera_scope: list[str] | None,
        include_embedding: bool,
    ) -> tuple[str, dict[str, Any]]:
        """Build parameterized SQL with dynamic WHERE clause.

        Returns ``(sql, params)``. The caller adds ``query_embedding`` and
        ``top_k`` to *params* before execution.
        """
        select = _EMBEDDING_SELECT if include_embedding else _METADATA_SELECT

        where_parts = ["embedding IS NOT NULL"]
        params: dict[str, Any] = {}

        if min_similarity is not None:
            where_parts.append(
                "1 - (embedding <=> %(query_embedding)s) >= %(min_similarity)s"
            )
            params["min_similarity"] = min_similarity

        if camera_scope is not None:
            where_parts.append(
                "camera_id = ANY(%(camera_scope)s::text[])"
            )
            params["camera_scope"] = camera_scope

        where_clause = "\n    AND ".join(where_parts)

        sql = f"""{select}
FROM face_observations
WHERE {where_clause}
ORDER BY embedding <=> %(query_embedding)s
LIMIT %(top_k)s"""

        return sql, params

    def search_gallery(
        self,
        embedding: list[float],
        *,
        top_k: int = 10,
        min_similarity: float | None = None,
        person_ids: list[int] | None = None,
        include_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        """Return topK most similar gallery embeddings for *embedding*.

        Searches ``person_gallery_embeddings`` joined with ``persons``.

        Args:
            embedding: 512-d L2-normalized query vector (AdaFace).
            top_k: Number of results (clamped to [1, 100]).
            min_similarity: Optional cosine similarity threshold [0.0, 1.0].
            person_ids: Optional list of person_ids to restrict search.
            include_embedding: If True, include the 512-d embedding in results.

        Returns:
            List of dicts ordered by descending similarity.
        """
        query_vector = _validate_query_embedding(embedding)

        original = top_k
        top_k = max(_MIN_TOP_K, min(top_k, _MAX_TOP_K))
        if original != top_k:
            logger.warning("top_k clamped from %s to %s", original, top_k)

        if min_similarity is not None:
            if not (_MIN_SIMILARITY <= min_similarity <= _MAX_SIMILARITY):
                raise ValueError(
                    f"min_similarity={min_similarity} outside "
                    f"[{_MIN_SIMILARITY}, {_MAX_SIMILARITY}]"
                )

        if person_ids is not None and len(person_ids) == 0:
            return []

        select = (
            _GALLERY_EMBEDDING_SELECT if include_embedding
            else _GALLERY_METADATA_SELECT
        )
        where_parts = [
            "p.is_active = true",
            "pge.is_active = true",
            "pge.embedding IS NOT NULL",
        ]
        params: dict[str, Any] = {}

        if min_similarity is not None:
            where_parts.append(
                "1 - (pge.embedding <=> %(query_embedding)s) >= %(min_similarity)s"
            )
            params["min_similarity"] = min_similarity

        if person_ids is not None:
            where_parts.append(
                "pge.person_id = ANY(%(person_ids)s::bigint[])"
            )
            params["person_ids"] = person_ids

        where_clause = "\n    AND ".join(where_parts)

        sql = f"""{select}
FROM person_gallery_embeddings pge
JOIN persons p ON p.id = pge.person_id
WHERE {where_clause}
ORDER BY pge.embedding <=> %(query_embedding)s
LIMIT %(top_k)s"""

        params["query_embedding"] = Vector(query_vector)
        params["top_k"] = top_k

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        return list(rows)
