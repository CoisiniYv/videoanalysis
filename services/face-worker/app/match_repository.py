"""MatchResultRepository — PostgreSQL CRUD for ``match_results``.

Stores gallery-match search results.  Follows the
FaceObservationRepository / GalleryRepository pattern
(psycopg.Connection, dict_row, pgvector).

midterm gallery_match semantics:
  - query side: face_observation (query_observation_id / query_source_observation_id)
  - target side: person_gallery_embeddings (query_gallery_embedding_id)
  - matched_observation_id: NULL (no historical observation target)
  - idempotency: UNIQUE(search_request_id, query_gallery_embedding_id)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row

from app.vector_store import register_vector_if_supported

# For gallery_match: conflict target is (search_request_id, query_gallery_embedding_id).
_INSERT_GALLERY_MATCH_SQL = """
INSERT INTO match_results (
    search_request_id, search_mode,
    query_observation_id, query_source_observation_id,
    query_person_id, query_gallery_embedding_id,
    query_embedding_model,
    similarity_threshold, time_from, time_to, camera_scope,
    matched_observation_id, matched_source_observation_id,
    matched_camera_id, matched_source_id, matched_track_id,
    matched_captured_at, matched_timestamp_ms,
    rank, similarity,
    face_confidence, quality,
    snapshot_path, crop_path, nvr_reference,
    expires_at, payload
) VALUES (
    %(search_request_id)s, %(search_mode)s,
    %(query_observation_id)s, %(query_source_observation_id)s,
    %(query_person_id)s, %(query_gallery_embedding_id)s,
    %(query_embedding_model)s,
    %(similarity_threshold)s, %(time_from)s, %(time_to)s,
    %(camera_scope)s::jsonb,
    %(matched_observation_id)s, %(matched_source_observation_id)s,
    %(matched_camera_id)s, %(matched_source_id)s, %(matched_track_id)s,
    %(matched_captured_at)s, %(matched_timestamp_ms)s,
    %(rank)s, %(similarity)s,
    %(face_confidence)s, %(quality)s,
    %(snapshot_path)s, %(crop_path)s, %(nvr_reference)s::jsonb,
    %(expires_at)s, %(payload)s::jsonb
)
ON CONFLICT (search_request_id, query_gallery_embedding_id) DO NOTHING
RETURNING id
"""

_GET_BY_REQUEST_SQL = """
SELECT id, search_request_id, search_mode,
       query_observation_id, query_source_observation_id,
       query_person_id, query_gallery_embedding_id,
       query_embedding_model,
       similarity_threshold, time_from, time_to, camera_scope,
       matched_observation_id, matched_source_observation_id,
       matched_camera_id, matched_source_id, matched_track_id,
       matched_captured_at, matched_timestamp_ms,
       rank, similarity,
       face_confidence, quality,
       snapshot_path, crop_path, nvr_reference,
       expires_at, payload,
       created_at
FROM match_results
WHERE search_request_id = %(search_request_id)s
ORDER BY rank
"""

_DELETE_EXPIRED_SQL = """
DELETE FROM match_results
WHERE expires_at < now()
"""


def _to_jsonb(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False)


class MatchResultRepository:
    """CRUD operations for ``match_results``."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        register_vector_if_supported(conn)

    def insert_gallery_match_result(self, data: Dict[str, Any]) -> int | None:
        """Insert a gallery_match result row.

        Idempotency key: (search_request_id, query_gallery_embedding_id).
        Returns the new row's BIGINT id, or None if a duplicate was skipped.

        For gallery_match:
          - query_observation_id / query_source_observation_id: the input observation
          - query_gallery_embedding_id: the gallery embedding that matched
          - matched_observation_id: NULL (no historical observation target)
        """
        params = {
            "search_request_id": data["search_request_id"],
            "search_mode": data.get("search_mode", "gallery_match"),
            "query_observation_id": data.get("query_observation_id"),
            "query_source_observation_id": data.get("query_source_observation_id"),
            "query_person_id": data.get("query_person_id"),
            "query_gallery_embedding_id": data["query_gallery_embedding_id"],
            "query_embedding_model": data.get("query_embedding_model", "adaface"),
            "similarity_threshold": data.get("similarity_threshold"),
            "time_from": data.get("time_from"),
            "time_to": data.get("time_to"),
            "camera_scope": _to_jsonb(data.get("camera_scope")),
            "matched_observation_id": None,  # gallery_match has no matched observation
            "matched_source_observation_id": None,
            "matched_camera_id": None,
            "matched_source_id": None,
            "matched_track_id": None,
            "matched_captured_at": None,
            "matched_timestamp_ms": None,
            "rank": data.get("rank", 0),
            "similarity": data.get("similarity", 0.0),
            "face_confidence": data.get("face_confidence"),
            "quality": data.get("quality"),
            "snapshot_path": data.get("snapshot_path"),
            "crop_path": data.get("crop_path"),
            "nvr_reference": _to_jsonb(data.get("nvr_reference")),
            "expires_at": data["expires_at"],
            "payload": _to_jsonb(data.get("payload")) or "{}",
        }

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_INSERT_GALLERY_MATCH_SQL, params)
            row = cur.fetchone()
            return int(row["id"]) if row else None

    def get_by_search_request(
        self, search_request_id: str,
    ) -> List[Dict[str, Any]]:
        """Return all match results for a search_request_id, ordered by rank."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _GET_BY_REQUEST_SQL,
                {"search_request_id": search_request_id},
            )
            return list(cur.fetchall())

    def delete_expired(self) -> int:
        """Delete all expired match_results rows. Returns number deleted."""
        with self._conn.cursor() as cur:
            cur.execute(_DELETE_EXPIRED_SQL)
            return cur.rowcount
