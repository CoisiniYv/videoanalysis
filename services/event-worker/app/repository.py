"""EventRepository — idempotent PostgreSQL event insertion."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import psycopg
from psycopg.rows import dict_row


_INSERT_SQL = """
INSERT INTO events (
    source_event_id,
    event_type,
    camera_id,
    source_id,
    track_id,
    person_id,
    severity,
    confidence,
    start_ts,
    end_ts,
    event_ts_ms,
    frame_uuid,
    keyframe_uuid,
    snapshot_path,
    clip_path,
    recording_strategy,
    media_status,
    status,
    payload
) VALUES (
    %(source_event_id)s,
    %(event_type)s,
    %(camera_id)s,
    %(source_id)s,
    %(track_id)s,
    %(person_id)s,
    %(severity)s,
    %(confidence)s,
    to_timestamp(%(start_ts_ms)s::double precision / 1000.0),
    to_timestamp(%(end_ts_ms)s::double precision / 1000.0),
    %(event_ts_ms)s,
    %(frame_uuid)s,
    %(keyframe_uuid)s,
    %(snapshot_path)s,
    %(clip_path)s,
    %(recording_strategy)s,
    %(media_status)s,
    %(status)s,
    %(payload)s::jsonb
)
ON CONFLICT (source_event_id) DO NOTHING
RETURNING id
"""

_SELECT_BY_SID_SQL = """
SELECT id, source_event_id FROM events WHERE source_event_id = %s
"""

_COUNT_BY_SID_SQL = """
SELECT COUNT(*) FROM events WHERE source_event_id = %s
"""


class EventRepository:
    """Idempotent event store backed by PostgreSQL."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def insert_event(self, event: Dict[str, Any]) -> bool:
        """Insert *event* dict into the events table.

        Returns True if a new row was inserted, False if a duplicate
        ``source_event_id`` was skipped.
        """
        media = event.get("payload", {}).get("media", {})
        params = {
            "source_event_id": event.get("source_event_id", ""),
            "event_type": event.get("event_type", ""),
            "camera_id": event.get("camera_id", ""),
            "source_id": event.get("source_id", ""),
            "track_id": int(event.get("track_id", 0)),
            "person_id": event.get("person_id") or None,
            "severity": event.get("severity", "medium"),
            "confidence": float(event.get("confidence", 0.0)),
            "start_ts_ms": int(event.get("start_ts_ms", 0)),
            "end_ts_ms": int(event.get("end_ts_ms", 0)),
            "event_ts_ms": int(event.get("event_ts_ms", 0)),
            "frame_uuid": event.get("frame_uuid"),
            "keyframe_uuid": event.get("keyframe_uuid"),
            "snapshot_path": media.get("snapshot_path"),
            "clip_path": media.get("clip_path"),
            "recording_strategy": media.get("recording_strategy", "reserved"),
            "media_status": media.get("snapshot_status", "not_implemented"),
            "status": "new",
            "payload": json.dumps(event.get("payload", {}), ensure_ascii=False),
        }

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_INSERT_SQL, params)
            inserted = cur.fetchone()
            return inserted is not None

    def count_by_source_event_id(self, source_event_id: str) -> int:
        """Return the number of rows with the given *source_event_id*."""
        with self._conn.cursor() as cur:
            cur.execute(_COUNT_BY_SID_SQL, (source_event_id,))
            row = cur.fetchone()
            return row[0] if row else 0

    def event_exists(self, source_event_id: str) -> bool:
        """Return True if an event with *source_event_id* exists."""
        return self.count_by_source_event_id(source_event_id) > 0
