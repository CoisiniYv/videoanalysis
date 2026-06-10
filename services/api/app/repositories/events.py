"""EventRepository — read-only queries against the events table."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row


class EventRepository:
    """Read-only repository for events."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # list recent
    # ------------------------------------------------------------------

    def list_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        query = """
            SELECT * FROM events
            ORDER BY created_at DESC
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
            where_clauses.append("event_type = %(event_type)s")
            params["event_type"] = event_type

        if camera_id:
            where_clauses.append("camera_id = %(camera_id)s")
            params["camera_id"] = camera_id

        if track_id:
            where_clauses.append("track_id = %(track_id)s")
            params["track_id"] = track_id

        if status:
            where_clauses.append("status = %(status)s")
            params["status"] = status

        if start:
            where_clauses.append("start_ts >= %(start)s::timestamptz")
            params["start"] = start

        if end:
            where_clauses.append("start_ts <= %(end)s::timestamptz")
            params["end"] = end

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        # Count total
        count_query = f"SELECT COUNT(*) AS total FROM events {where_sql}"
        count_params = {k: v for k, v in params.items()}

        # Fetch page
        params["limit"] = limit
        params["offset"] = offset
        data_query = f"""
            SELECT * FROM events
            {where_sql}
            ORDER BY created_at DESC
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

    # ------------------------------------------------------------------
    # get by id (UUID)
    # ------------------------------------------------------------------

    def get_by_id(self, event_id: str) -> Dict[str, Any] | None:
        try:
            uid = uuid.UUID(event_id)
        except (ValueError, TypeError):
            return None

        query = "SELECT * FROM events WHERE id = %(id)s"
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {"id": str(uid)})
            return cur.fetchone()

    # ------------------------------------------------------------------
    # get by source_event_id
    # ------------------------------------------------------------------

    def get_by_source_event_id(self, source_event_id: str) -> Dict[str, Any] | None:
        query = "SELECT * FROM events WHERE source_event_id = %(sid)s"
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
            return cur.fetchone()
