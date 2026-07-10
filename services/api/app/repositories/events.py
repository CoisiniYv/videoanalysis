"""EventRepository — read-only queries against the events table."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import psycopg
from psycopg.rows import dict_row


EVENT_CATEGORY_TYPES = {
    "identity": ("watchlist_hit", "live_search_hit"),
    "evidence": (
        "intrusion",
        "wall_climb_suspicious",
        "loitering",
        "running",
        "fall",
        "crowd_gathering",
    ),
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
        """List evidence candidates from the materialized evidence DB index."""

        where_clauses = [
            """
            (
                COALESCE(eb.media_status, '') NOT IN ('media_deleted', 'media_expired')
                AND (
                    COALESCE(eb.raw_clip_uri, '') <> ''
                    OR COALESCE(eb.summary->>'playback_kind', '') = 'image'
                    OR COALESCE(eb.media_status, '') = 'image_ready'
                    OR EXISTS (
                        SELECT 1
                        FROM evidence_artifacts image_artifact
                        WHERE image_artifact.event_id = eb.event_id
                          AND image_artifact.artifact_type IN (
                              'face_crop', 'full_frame', 'annotated_frame'
                          )
                          AND COALESCE(image_artifact.uri, '') <> ''
                    )
                )
            )
            """
        ]
        params: dict[str, Any] = {
            "limit": max(1, int(limit)),
            "offset": max(0, int(offset)),
        }

        if event_type:
            where_clauses.append("eb.event_type = %(event_type)s")
            params["event_type"] = event_type

        if event_category and event_category != "all" and not event_type:
            event_types = EVENT_CATEGORY_TYPES.get(event_category, ())
            if event_types:
                where_clauses.append("eb.event_type = ANY(%(event_category_types)s)")
                params["event_category_types"] = list(event_types)
            else:
                where_clauses.append("false")

        if source_id:
            where_clauses.append(
                """
                (
                    eb.source_id ILIKE %(source_id_like)s
                    OR eb.camera_id::text ILIKE %(source_id_like)s
                    OR c.id::text ILIKE %(source_id_like)s
                    OR c.source_id ILIKE %(source_id_like)s
                    OR c.name ILIKE %(source_id_like)s
                    OR eb.camera_name ILIKE %(source_id_like)s
                )
                """
            )
            params["source_id_like"] = f"%{source_id}%"

        if camera_id:
            where_clauses.append("eb.camera_id ILIKE %(camera_id_like)s")
            params["camera_id_like"] = f"%{camera_id}%"

        if event_id:
            where_clauses.append(
                "(eb.event_id::text ILIKE %(event_id_like)s OR eb.source_event_id ILIKE %(event_id_like)s)"
            )
            params["event_id_like"] = f"%{event_id}%"

        if person:
            where_clauses.append(
                """
                (
                    eb.summary->>'external_person_id' ILIKE %(person_like)s
                    OR eb.summary->'matched_person'->>'external_person_id' ILIKE %(person_like)s
                    OR eb.summary->'person'->>'name' ILIKE %(person_like)s
                    OR eb.summary::text ILIKE %(person_like)s
                )
                """
            )
            params["person_like"] = f"%{person}%"

        if clip_status:
            where_clauses.append(
                """
                COALESCE(
                    eb.summary->>'clip_status',
                    eb.evidence_state,
                    eb.media_status,
                    latest_task.status,
                    ''
                ) ILIKE %(clip_status_like)s
                """
            )
            params["clip_status_like"] = f"%{clip_status}%"

        where_sql = " AND ".join(f"({clause})" for clause in where_clauses)
        latest_task_join_sql = """
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
                WHERE et.event_id = eb.event_id
                   OR et.source_event_id = eb.source_event_id
                ORDER BY et.updated_at DESC, et.created_at DESC, et.task_id DESC
                LIMIT 1
            ) latest_task ON true
        """
        count_from_sql = f"""
            FROM evidence_bundles eb
            LEFT JOIN cameras c
              ON c.id::text = eb.camera_id
              OR c.source_id = eb.source_id
            {latest_task_join_sql if clip_status else ""}
            WHERE {where_sql}
        """
        data_from_sql = f"""
            FROM evidence_bundles eb
            LEFT JOIN cameras c
              ON c.id::text = eb.camera_id
              OR c.source_id = eb.source_id
            LEFT JOIN LATERAL (
                SELECT uri
                FROM evidence_artifacts ea
                WHERE ea.event_id = eb.event_id
                  AND ea.artifact_type = 'overlay_annotations'
                LIMIT 1
            ) overlay_artifact ON true
            LEFT JOIN LATERAL (
                SELECT uri
                FROM evidence_artifacts ea
                WHERE ea.event_id = eb.event_id
                  AND ea.artifact_type = 'face_crop'
                LIMIT 1
            ) face_crop_artifact ON true
            LEFT JOIN LATERAL (
                SELECT uri
                FROM evidence_artifacts ea
                WHERE ea.event_id = eb.event_id
                  AND ea.artifact_type = 'full_frame'
                LIMIT 1
            ) full_frame_artifact ON true
            LEFT JOIN LATERAL (
                SELECT uri
                FROM evidence_artifacts ea
                WHERE ea.event_id = eb.event_id
                  AND ea.artifact_type = 'annotated_frame'
                LIMIT 1
            ) annotated_frame_artifact ON true
            {latest_task_join_sql}
            LEFT JOIN LATERAL (
                SELECT COUNT(*)::int AS task_count
                FROM evidence_tasks et
                WHERE et.event_id = eb.event_id
                   OR et.source_event_id = eb.source_event_id
            ) task_counts ON true
            WHERE {where_sql}
        """
        count_query = f"SELECT COUNT(*) AS total {count_from_sql}"
        data_query = f"""
            SELECT
                eb.event_id::text AS event_id,
                eb.source_event_id,
                eb.event_type,
                eb.camera_id,
                eb.source_id,
                COALESCE(eb.camera_name, c.name) AS camera_name,
                NULL::text AS person_id,
                eb.media_status,
                NULL::text AS status,
                eb.event_created_at AS created_at,
                eb.updated_at,
                eb.event_created_at AS start_ts,
                NULL::bigint AS event_ts_ms,
                jsonb_build_object(
                    'camera_name', COALESCE(eb.camera_name, c.name),
                    'media', jsonb_strip_nulls(
                        jsonb_build_object(
                            'clip_status', eb.media_status,
                            'evidence_state', eb.evidence_state,
                            'evidence_reason', eb.evidence_reason,
                            'annotation_status', eb.annotation_status,
                            'annotation_lines', eb.annotation_count,
                            'visual_evidence_status', eb.visual_evidence_status,
                            'frontend_overlay_required', eb.frontend_overlay_required,
                            'playback_kind', eb.summary->>'playback_kind',
                            'image_status', eb.summary->>'image_status',
                            'face_crop_uri', COALESCE(face_crop_artifact.uri, eb.summary->>'face_crop_uri'),
                            'full_frame_uri', COALESCE(full_frame_artifact.uri, eb.summary->>'full_frame_uri'),
                            'annotated_frame_uri', COALESCE(annotated_frame_artifact.uri, eb.summary->>'annotated_frame_uri'),
                            'matched_objects', eb.matched_objects,
                            'unknown_objects', eb.unknown_objects,
                            'summary', eb.summary,
                            'materialization', eb.materialization
                        )
                    )
                ) AS payload,
                eb.raw_clip_uri AS clip_path,
                COALESCE(eb.summary->>'clip_status', eb.media_status, latest_task.status) AS clip_status,
                COALESCE(
                    eb.raw_clip_uri,
                    latest_task.clip_path
                ) AS raw_clip_path,
                COALESCE(
                    latest_task.metadata_path
                ) AS metadata_path,
                eb.summary->>'evidence_dir' AS evidence_dir,
                eb.summary->>'summary_json_path' AS summary_json_path,
                COALESCE(overlay_artifact.uri, eb.summary->>'annotations_jsonl_path') AS annotations_jsonl_path,
                COALESCE(face_crop_artifact.uri, eb.summary->>'face_crop_uri') AS face_crop_uri,
                COALESCE(full_frame_artifact.uri, eb.summary->>'full_frame_uri') AS full_frame_uri,
                COALESCE(annotated_frame_artifact.uri, eb.summary->>'annotated_frame_uri') AS annotated_frame_uri,
                COALESCE(task_counts.task_count, 0) AS evidence_task_count,
                latest_task.status AS latest_task_status,
                latest_task.materialization_status AS latest_materialization_status,
                latest_task.materialization_deadline_at AS latest_materialization_deadline_at
            {data_from_sql}
            ORDER BY eb.event_created_at DESC, eb.event_id DESC
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

    def get_evidence_bundle_index(self, event_id: str) -> Dict[str, Any] | None:
        query = """
            SELECT
                eb.*,
                c.name AS camera_table_name,
                raw_artifact.uri AS raw_clip_artifact_uri,
                overlay_artifact.uri AS overlay_artifact_uri,
                timeline_artifact.uri AS timeline_artifact_uri,
                face_crop_artifact.uri AS face_crop_uri,
                full_frame_artifact.uri AS full_frame_uri,
                annotated_frame_artifact.uri AS annotated_frame_uri
            FROM evidence_bundles eb
            LEFT JOIN cameras c
              ON c.id::text = eb.camera_id
              OR c.source_id = eb.source_id
            LEFT JOIN evidence_artifacts raw_artifact
              ON raw_artifact.event_id = eb.event_id
             AND raw_artifact.artifact_type = 'raw_clip'
            LEFT JOIN evidence_artifacts overlay_artifact
              ON overlay_artifact.event_id = eb.event_id
             AND overlay_artifact.artifact_type = 'overlay_annotations'
            LEFT JOIN evidence_artifacts timeline_artifact
              ON timeline_artifact.event_id = eb.event_id
             AND timeline_artifact.artifact_type = 'sink_timeline'
            LEFT JOIN evidence_artifacts face_crop_artifact
              ON face_crop_artifact.event_id = eb.event_id
             AND face_crop_artifact.artifact_type = 'face_crop'
            LEFT JOIN evidence_artifacts full_frame_artifact
              ON full_frame_artifact.event_id = eb.event_id
             AND full_frame_artifact.artifact_type = 'full_frame'
            LEFT JOIN evidence_artifacts annotated_frame_artifact
              ON annotated_frame_artifact.event_id = eb.event_id
             AND annotated_frame_artifact.artifact_type = 'annotated_frame'
            WHERE eb.event_id::text = %(event_id)s
               OR eb.source_event_id = %(event_id)s
            LIMIT 1
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {"event_id": event_id})
            return cur.fetchone()

    def list_evidence_overlay_records(self, event_id: str) -> List[Dict[str, Any]]:
        query = """
            SELECT eos.record
            FROM evidence_overlay_segments eos
            JOIN evidence_bundles eb ON eb.event_id = eos.event_id
            WHERE eb.event_id::text = %(event_id)s
               OR eb.source_event_id = %(event_id)s
            ORDER BY eos.clip_frame_index ASC
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {"event_id": event_id})
            return [row["record"] for row in cur.fetchall() if isinstance(row.get("record"), dict)]

    def list_evidence_timeline_records(self, event_id: str) -> List[Dict[str, Any]]:
        query = """
            SELECT
                eft.clip_frame_index,
                eft.frame_uuid,
                eft.frame_pts,
                eft.frame_dts,
                eft.duration_ns,
                eft.timestamp_ms,
                eft.width,
                eft.height,
                eft.source_id,
                eft.camera_id,
                eft.stream_session_id,
                eft.keyframe_uuid,
                eft.metadata
            FROM evidence_frame_timeline eft
            JOIN evidence_bundles eb ON eb.event_id = eft.event_id
            WHERE eb.event_id::text = %(event_id)s
               OR eb.source_event_id = %(event_id)s
            ORDER BY eft.clip_frame_index ASC
        """
        records: list[dict[str, Any]] = []
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, {"event_id": event_id})
            for row in cur.fetchall():
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                record = dict(metadata)
                record.setdefault("clip_frame_index", row.get("clip_frame_index"))
                record.setdefault("frame_uuid", row.get("frame_uuid"))
                record.setdefault("frame_pts", row.get("frame_pts"))
                record.setdefault("pts", row.get("frame_pts"))
                record.setdefault("frame_dts", row.get("frame_dts"))
                record.setdefault("dts", row.get("frame_dts"))
                record.setdefault("duration", row.get("duration_ns"))
                record.setdefault("timestamp_ms", row.get("timestamp_ms"))
                record.setdefault("width", row.get("width"))
                record.setdefault("height", row.get("height"))
                record.setdefault("source_id", row.get("source_id"))
                record.setdefault("camera_id", row.get("camera_id"))
                record.setdefault("stream_session_id", row.get("stream_session_id"))
                record.setdefault("keyframe_uuid", row.get("keyframe_uuid"))
                records.append(record)
        return records

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
