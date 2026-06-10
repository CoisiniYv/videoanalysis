"""TrajectoryRepository — read-only query for registered person trajectory.

Queries ``match_results`` with ``search_mode = 'registered_person_history'``
joined with ``persons`` and ``face_observations`` to produce trajectory
summaries for a registered person.

midterm: read-side harness only.  Does not write to any table.
"""

from __future__ import annotations

from typing import Any, List, Optional

import psycopg
from psycopg.rows import dict_row

_MIN_SIMILARITY = 0.0
_MAX_SIMILARITY = 1.0
_MIN_LIMIT = 1
_MAX_LIMIT = 1000
_DEFAULT_LIMIT = 100

_BASE_SELECT = """
SELECT
    mr.search_request_id,
    mr.query_person_id,
    p.name AS person_name,
    p.external_person_id,
    mr.query_gallery_embedding_id,
    mr.matched_observation_id,
    mr.matched_source_observation_id,
    mr.matched_camera_id,
    mr.matched_source_id,
    mr.matched_track_id,
    mr.matched_timestamp_ms,
    mr.matched_captured_at,
    mr.similarity,
    mr.rank,
    mr.nvr_reference,
    COALESCE(mr.snapshot_path, fo.snapshot_path) AS snapshot_path,
    COALESCE(mr.crop_path, fo.crop_path) AS crop_path
FROM match_results mr
JOIN persons p ON p.id = mr.query_person_id
LEFT JOIN face_observations fo ON fo.id = mr.matched_observation_id
"""


class TrajectoryRepository:
    """Read-only trajectory query for registered persons."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def get_person_trajectory(
        self,
        person_id: int,
        *,
        time_from_ms: int | None = None,
        time_to_ms: int | None = None,
        camera_id: str | None = None,
        min_similarity: float | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> List[dict[str, Any]]:
        """Return trajectory entries for a registered person.

        Only reads ``match_results`` with
        ``search_mode = 'registered_person_history'``.  Does not include
        ``gallery_match`` rows.

        Args:
            person_id: The registered person's id.
            time_from_ms: Optional start of time range (epoch ms).
            time_to_ms: Optional end of time range (epoch ms).
            camera_id: Optional camera_id filter.
            min_similarity: Optional minimum similarity [0.0, 1.0].
            limit: Maximum results (clamped to [1, 1000]).

        Returns:
            List of dicts ordered by matched_timestamp_ms DESC, rank ASC.
        """
        if min_similarity is not None:
            if not (_MIN_SIMILARITY <= min_similarity <= _MAX_SIMILARITY):
                raise ValueError(
                    f"min_similarity={min_similarity} outside "
                    f"[{_MIN_SIMILARITY}, {_MAX_SIMILARITY}]"
                )

        original_limit = limit
        limit = max(_MIN_LIMIT, min(limit, _MAX_LIMIT))

        where_parts = [
            "mr.search_mode = 'registered_person_history'",
            "mr.query_person_id = %(person_id)s",
            "mr.matched_observation_id IS NOT NULL",
        ]
        params: dict[str, Any] = {
            "person_id": person_id,
            "limit": limit,
        }

        if time_from_ms is not None:
            where_parts.append("mr.matched_timestamp_ms >= %(time_from_ms)s")
            params["time_from_ms"] = time_from_ms

        if time_to_ms is not None:
            where_parts.append("mr.matched_timestamp_ms <= %(time_to_ms)s")
            params["time_to_ms"] = time_to_ms

        if camera_id is not None:
            where_parts.append("mr.matched_camera_id = %(camera_id)s")
            params["camera_id"] = camera_id

        if min_similarity is not None:
            where_parts.append("mr.similarity >= %(min_similarity)s")
            params["min_similarity"] = min_similarity

        where_clause = "\n    AND ".join(where_parts)

        sql = f"""{_BASE_SELECT}
WHERE {where_clause}
ORDER BY mr.matched_timestamp_ms DESC, mr.rank ASC, mr.id DESC
LIMIT %(limit)s"""

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
