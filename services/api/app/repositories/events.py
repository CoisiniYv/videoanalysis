"""EventRepository — read-only queries against the events table."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import psycopg
from psycopg.rows import dict_row


EVENT_CATEGORY_TYPES = {
    "identity": ("watchlist_hit", "live_search_hit"),
    "perimeter": ("intrusion", "wall_climb_suspicious"),
    "behavior": ("loitering", "running", "fall"),
    "crowd": ("crowd_gathering",),
}

EVENTS_WITH_CAMERA_SQL = """
    SELECT e.*, c.name AS camera_name
    FROM events e
    LEFT JOIN cameras c
      ON c.id::text = e.camera_id
      OR c.source_id = e.source_id
"""


class EventRepository:
    """Read-only repository for events."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # list recent
    # ------------------------------------------------------------------

    def list_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        query = f"""
            {EVENTS_WITH_CAMERA_SQL}
            ORDER BY e.created_at DESC
            LIMIT %(limit)s
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {"limit": limit})
            return cur.fetchall()

    # ------------------------------------------------------------------
    # list with filters
    # ------------------------------------------------------------------

    def list_events(
        self,
        *,
        event_type: str | None = None,
        camera_id: str | None = None,
        track_id: str | None = None,
        status: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Dict[str, Any]], int]:
        where_clauses: list[str] = []
        params: dict[str, Any] = {}

        if event_type:
            where_clauses.append("e.event_type = %(event_type)s")
            params["event_type"] = event_type

        if camera_id:
            where_clauses.append("e.camera_id = %(camera_id)s")
            params["camera_id"] = camera_id

        if track_id:
            where_clauses.append("e.track_id = %(track_id)s")
            params["track_id"] = track_id

        if status:
            where_clauses.append("e.status = %(status)s")
            params["status"] = status

        if start:
            where_clauses.append("e.start_ts >= %(start)s::timestamptz")
            params["start"] = start

        if end:
            where_clauses.append("e.start_ts <= %(end)s::timestamptz")
            params["end"] = end

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        # Count total
        count_query = f"SELECT COUNT(*) AS total FROM events e {where_sql}"
        count_params = {k: v for k, v in params.items()}

        # Fetch page
        params["limit"] = limit
        params["offset"] = offset
        data_query = f"""
            {EVENTS_WITH_CAMERA_SQL}
            {where_sql}
            ORDER BY e.created_at DESC
            LIMIT %(limit)s OFFSET %(offset)s
        """

        with self._conn.cursor() as cur:
            cur.execute(count_query, count_params)
            total_row = cur.fetchone()
            if total_row is None:
                total = 0
            elif isinstance(total_row, dict):
                total = int(total_row.get("total", 0) or 0)
            else:
                total = int(total_row[0] or 0)

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(data_query, params)
            rows = cur.fetchall()

        return rows, total

    def list_evidence_bundle_summaries(
        self,
        *,
        event_type: str | None = None,
        event_category: str | None = None,
        source_id: str | None = None,
        camera_id: str | None = None,
        event_id: str | None = None,
        person: str | None = None,
        clip_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Dict[str, Any]], int]:
        """List evidence candidates from PostgreSQL, not evidence directories."""

        where_clauses = [
            """
            (
                COALESCE(e.media_status, '') NOT IN ('media_deleted', 'media_expired')
                AND COALESCE(e.payload->'maintenance'->>'deleted_at', '') = ''
            )
            """,
            """
            (
                COALESCE(e.clip_path, '') <> ''
                OR COALESCE(e.media_status, 'not_implemented') <> 'not_implemented'
                OR COALESCE(e.payload->'media'->>'raw_clip_path', '') <> ''
                OR COALESCE(e.payload->'media'->>'metadata_path', '') <> ''
                OR COALESCE(e.payload->'media'->>'evidence_dir', '') <> ''
                OR COALESCE(e.payload->'media'->>'evidence_bundle_path', '') <> ''
                OR COALESCE(e.payload->'evidence'->>'bundle_path', '') <> ''
                OR COALESCE(e.payload->>'evidence_bundle_path', '') <> ''
                OR latest_task.task_id IS NOT NULL
            )
            """
        ]
        params: dict[str, Any] = {
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }

        if event_type:
            where_clauses.append("e.event_type = %(event_type)s")
            params["event_type"] = event_type

        if event_category and event_category != "all":
            event_types = EVENT_CATEGORY_TYPES.get(event_category, ())
            if event_types:
                where_clauses.append("e.event_type = ANY(%(event_category_types)s)")
                params["event_category_types"] = list(event_types)
            else:
                where_clauses.append("false")

        if source_id:
            where_clauses.append("e.source_id ILIKE %(source_id_like)s")
            params["source_id_like"] = f"%{source_id}%"

        if camera_id:
            where_clauses.append("e.camera_id ILIKE %(camera_id_like)s")
            params["camera_id_like"] = f"%{camera_id}%"

        if event_id:
            where_clauses.append(
                "(e.id::text ILIKE %(event_id_like)s OR e.source_event_id ILIKE %(event_id_like)s)"
            )
            params["event_id_like"] = f"%{event_id}%"

        if person:
            where_clauses.append(
                """
                (
                    e.person_id::text ILIKE %(person_like)s
                    OR e.payload->>'external_person_id' ILIKE %(person_like)s
                    OR e.payload->'matched_person'->>'external_person_id' ILIKE %(person_like)s
                    OR e.payload->'person'->>'name' ILIKE %(person_like)s
                    OR e.payload::text ILIKE %(person_like)s
                )
                """
            )
            params["person_like"] = f"%{person}%"

        if clip_status:
            where_clauses.append(
                """
                COALESCE(
                    e.payload->'media'->>'clip_status',
                    e.media_status,
                    latest_task.status,
                    ''
                ) ILIKE %(clip_status_like)s
                """
            )
            params["clip_status_like"] = f"%{clip_status}%"

        where_sql = " AND ".join(f"({clause})" for clause in where_clauses)
        from_sql = f"""
            FROM events e
            LEFT JOIN cameras c
              ON c.id::text = e.camera_id
              OR c.source_id = e.source_id
            LEFT JOIN LATERAL (
                SELECT
                    et.task_id,
                    et.status,
                    et.materialization_status,
                    et.materialization_deadline_at,
                    et.clip_path,
                    et.metadata_path,
                    et.updated_at
                FROM evidence_tasks et
                WHERE et.event_id = e.id
                   OR et.source_event_id = e.source_event_id
                ORDER BY et.updated_at DESC, et.created_at DESC, et.task_id DESC
                LIMIT 1
            ) latest_task ON true
            LEFT JOIN LATERAL (
                SELECT COUNT(*)::int AS task_count
                FROM evidence_tasks et
                WHERE et.event_id = e.id
                   OR et.source_event_id = e.source_event_id
            ) task_counts ON true
            WHERE {where_sql}
        """
        count_query = f"SELECT COUNT(*) AS total {from_sql}"
        data_query = f"""
            SELECT
                e.id::text AS event_id,
                e.source_event_id,
                e.event_type,
                e.camera_id,
                e.source_id,
                c.name AS camera_name,
                e.person_id,
                e.media_status,
                e.status,
                e.created_at,
                e.updated_at,
                e.start_ts,
                e.event_ts_ms,
                e.payload,
                e.clip_path,
                COALESCE(e.payload->'media'->>'clip_status', e.media_status, latest_task.status) AS clip_status,
                COALESCE(
                    e.payload->'media'->>'raw_clip_path',
                    e.clip_path,
                    latest_task.clip_path
                ) AS raw_clip_path,
                COALESCE(
                    e.payload->'media'->>'metadata_path',
                    latest_task.metadata_path
                ) AS metadata_path,
                COALESCE(
                    e.payload->'media'->>'evidence_dir',
                    e.payload->'media'->>'evidence_bundle_path',
                    e.payload->'evidence'->>'bundle_path',
                    e.payload->>'evidence_bundle_path'
                ) AS evidence_dir,
                e.payload->'media'->>'summary_json_path' AS summary_json_path,
                e.payload->'media'->>'annotations_jsonl_path' AS annotations_jsonl_path,
                COALESCE(task_counts.task_count, 0) AS evidence_task_count,
                latest_task.status AS latest_task_status,
                latest_task.materialization_status AS latest_materialization_status,
                latest_task.materialization_deadline_at AS latest_materialization_deadline_at
            {from_sql}
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT %(limit)s OFFSET %(offset)s
        """

        count_params = {k: v for k, v in params.items() if k not in {"limit", "offset"}}
        with self._conn.cursor() as cur:
            cur.execute(count_query, count_params)
            total_row = cur.fetchone()
            if total_row is None:
                total = 0
            elif isinstance(total_row, dict):
                total = int(total_row.get("total", 0) or 0)
            else:
                total = int(total_row[0] or 0)

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(data_query, params)
            rows = cur.fetchall()

        return rows, total

    # ------------------------------------------------------------------
    # get by id (UUID)
    # ------------------------------------------------------------------

    def get_by_id(self, event_id: str) -> Dict[str, Any] | None:
        try:
            uid = uuid.UUID(event_id)
        except (ValueError, TypeError):
            return None

        query = f"""
            {EVENTS_WITH_CAMERA_SQL}
            WHERE e.id = %(id)s
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {"id": str(uid)})
            return cur.fetchone()

    # ------------------------------------------------------------------
    # get by source_event_id
    # ------------------------------------------------------------------

    def get_by_source_event_id(self, source_event_id: str) -> Dict[str, Any] | None:
        query = f"""
            {EVENTS_WITH_CAMERA_SQL}
            WHERE e.source_event_id = %(sid)s
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {"sid": source_event_id})
            return cur.fetchone()

    # ------------------------------------------------------------------
    # resolve event_id (UUID or source_event_id)
    # ------------------------------------------------------------------

    def get_by_id_or_sid(self, event_id: str) -> Dict[str, Any] | None:
        row = self.get_by_id(event_id)
        if row is None:
            row = self.get_by_source_event_id(event_id)
        return row

    # ------------------------------------------------------------------
    # evidence tasks
    # ------------------------------------------------------------------

    def list_evidence_tasks(self, event_id: str) -> List[Dict[str, Any]]:
        row = self.get_by_id_or_sid(event_id)
        if row is None:
            return []

        query = """
            SELECT *
            FROM evidence_tasks
            WHERE event_id = %(event_id)s
               OR source_event_id = %(source_event_id)s
            ORDER BY created_at DESC, task_id DESC
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                query,
                {
                    "event_id": str(row["id"]),
                    "source_event_id": row["source_event_id"],
                },
            )
            return cur.fetchall()

    # ------------------------------------------------------------------
    # status update with transition validation
    # ------------------------------------------------------------------

    VALID_TRANSITIONS = {
        "new": {"acknowledged", "confirmed", "false_positive", "resolved"},
        "acknowledged": {"confirmed", "false_positive", "resolved"},
        "confirmed": {"resolved"},
        "false_positive": {"resolved"},
        "resolved": set(),
    }

    def update_status(
        self,
        event_id: str,
        new_status: str,
    ) -> Dict[str, Any] | None:
        """Atomically update event status.

        Returns the updated row, or None if the event is not found.
        Raises ValueError for invalid status transitions.
        """
        row = self.get_by_id_or_sid(event_id)
        if row is None:
            return None

        current = row["status"]
        allowed = self.VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition: {current} -> {new_status}. "
                f"Allowed: {sorted(allowed) if allowed else ['none (terminal)']}"
            )

        query = """
            UPDATE events
            SET status = %(status)s, updated_at = now()
            WHERE id = %(id)s
            RETURNING *
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {"status": new_status, "id": row["id"]})
            updated = cur.fetchone()

        if updated is None:
            return None
        return self.get_by_id(str(updated["id"]))
