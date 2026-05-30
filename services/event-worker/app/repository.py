"""EventRepository — idempotent PostgreSQL event insertion."""

from __future__ import annotations

import json
import uuid
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
    algorithm_type,
    algorithm_version,
    severity,
    confidence,
    start_ts_ms,
    end_ts_ms,
    start_ts,
    end_ts,
    event_ts_ms,
    frame_uuid,
    keyframe_uuid,
    snapshot_path,
    clip_path,
    snapshot_required,
    clip_required,
    evidence_policy,
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
    %(algorithm_type)s,
    %(algorithm_version)s,
    %(severity)s,
    %(confidence)s,
    %(start_ts_ms)s,
    %(end_ts_ms)s,
    to_timestamp(%(start_ts_ms)s::double precision / 1000.0),
    to_timestamp(%(end_ts_ms)s::double precision / 1000.0),
    %(event_ts_ms)s,
    %(frame_uuid)s,
    %(keyframe_uuid)s,
    %(snapshot_path)s,
    %(clip_path)s,
    %(snapshot_required)s,
    %(clip_required)s,
    %(evidence_policy)s::jsonb,
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

EVIDENCE_TASK_STATUSES = (
    "pending",
    "processing",
    "ready",
    "partial",
    "failed",
    "not_implemented",
)

_R3_1A_NOT_IMPLEMENTED_REASON = (
    "R3.1A behavior evidence MVP created the evidence task, but production "
    "snapshot/clip/metadata generation is not implemented in this deployment."
)

_R3_1B_NOT_IMPLEMENTED_REASON = (
    "R3.1B face match evidence MVP created the evidence task, but production "
    "snapshot/raw_clip/metadata generation is not implemented in this deployment."
)


def _not_implemented_reason(event: Dict[str, Any]) -> str:
    if event.get("algorithm_type") == "face_intelligence" or event.get(
        "event_type"
    ) in ("watchlist_hit", "live_search_hit"):
        return _R3_1B_NOT_IMPLEMENTED_REASON
    return _R3_1A_NOT_IMPLEMENTED_REASON


class EventRepository:
    """Idempotent event store backed by PostgreSQL."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def insert_event(self, event: Dict[str, Any]) -> str | None:
        """Insert *event* dict into the events table.

        Returns the UUID of the new row if inserted, or None if a duplicate
        ``source_event_id`` was skipped.
        """
        media = event.get("payload", {}).get("media", {})
        reason = _not_implemented_reason(event)
        params = {
            "source_event_id": event.get("source_event_id", ""),
            "event_type": event.get("event_type", ""),
            "camera_id": event.get("camera_id", ""),
            "source_id": event.get("source_id", ""),
            "track_id": str(event.get("track_id", "")),
            "person_id": event.get("person_id") or None,
            "algorithm_type": event.get("algorithm_type") or event.get("event_type", ""),
            "algorithm_version": event.get("algorithm_version"),
            "severity": event.get("severity", "medium"),
            "confidence": float(event.get("confidence", 0.0)),
            "start_ts_ms": int(event.get("start_ts_ms", 0)),
            "end_ts_ms": int(event.get("end_ts_ms") or event.get("start_ts_ms", 0)),
            "event_ts_ms": int(event.get("event_ts_ms", 0)),
            "frame_uuid": event.get("frame_uuid"),
            "keyframe_uuid": event.get("keyframe_uuid"),
            "snapshot_path": media.get("snapshot_path"),
            "clip_path": media.get("clip_path"),
            "snapshot_required": bool(event.get("snapshot_required", False)),
            "clip_required": bool(event.get("clip_required", False)),
            "evidence_policy": json.dumps(
                event.get("evidence_policy", {}), ensure_ascii=False
            ),
            "recording_strategy": media.get("recording_strategy", "reserved"),
            "media_status": media.get("snapshot_status", "not_implemented"),
            "status": "new",
            "payload": json.dumps(event.get("payload", {}), ensure_ascii=False),
        }

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_INSERT_SQL, params)
            row = cur.fetchone()
            return str(row["id"]) if row else None

    def create_evidence_task(
        self,
        event: Dict[str, Any],
        event_id: str,
    ) -> str | None:
        """Create one idempotent evidence task for an event.

        R3.1A records the task and marks media generation as
        ``not_implemented`` unless a later media worker claims and updates it.
        This keeps event ingestion idempotent and explicit: missing media is a
        known lifecycle state, not an empty or ambiguous row.
        """
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"evidence:{event_id}"))
        policy = event.get("evidence_policy") or {}
        if not isinstance(policy, dict):
            policy = {}
        reason = _not_implemented_reason(event)

        params = {
            "task_id": task_id,
            "event_id": event_id,
            "source_event_id": event.get("source_event_id", ""),
            "camera_id": event.get("camera_id", ""),
            "source_id": event.get("source_id", ""),
            "event_type": event.get("event_type", ""),
            "event_ts_ms": int(
                event.get("event_ts_ms") or event.get("start_ts_ms", 0)
            ),
            "task_type": "snapshot_clip",
            "snapshot_required": bool(event.get("snapshot_required", False)),
            "clip_required": bool(event.get("clip_required", False)),
            "pre_seconds": int(policy.get("pre_seconds", 5)),
            "post_seconds": int(policy.get("post_seconds", 10)),
            "status": "not_implemented",
            "error_message": reason,
        }

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO evidence_tasks (
                    task_id, event_id, source_event_id, camera_id, source_id,
                    event_type, event_ts_ms, task_type,
                    snapshot_required, clip_required,
                    pre_seconds, post_seconds, status, error_message
                ) VALUES (
                    %(task_id)s, %(event_id)s::uuid, %(source_event_id)s,
                    %(camera_id)s, %(source_id)s, %(event_type)s,
                    %(event_ts_ms)s, %(task_type)s,
                    %(snapshot_required)s, %(clip_required)s,
                    %(pre_seconds)s, %(post_seconds)s, %(status)s,
                    %(error_message)s
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    updated_at = evidence_tasks.updated_at
                RETURNING task_id
                """,
                params,
            )
            row = cur.fetchone()
            task_id_out = str(row["task_id"]) if row else None

        self.set_evidence_status(
            event_id=event_id,
            status="not_implemented",
            error_message=reason,
        )
        return task_id_out

    def set_evidence_status(
        self,
        *,
        event_id: str,
        status: str,
        error_message: str = "",
        snapshot_path: str | None = None,
        clip_path: str | None = None,
        metadata_path: str | None = None,
    ) -> bool:
        """Update event-level media status and media payload fields."""
        if status not in EVIDENCE_TASK_STATUSES:
            raise ValueError(f"unsupported evidence status: {status}")

        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET snapshot_path = COALESCE(%(snapshot_path)s, snapshot_path),
                    clip_path = COALESCE(%(clip_path)s, clip_path),
                    media_status = %(status)s,
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'snapshot_status', %(status)s::text,
                                'clip_status', %(status)s::text,
                                'metadata_status', %(status)s::text,
                                'metadata_path', %(metadata_path)s::text,
                                'error_message', %(error_message)s::text
                            )
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "event_id": event_id,
                    "status": status,
                    "error_message": error_message,
                    "snapshot_path": snapshot_path,
                    "clip_path": clip_path,
                    "metadata_path": metadata_path,
                },
            )
            return cur.rowcount is not None and cur.rowcount > 0

    def count_by_source_event_id(self, source_event_id: str) -> int:
        """Return the number of rows with the given *source_event_id*."""
        with self._conn.cursor() as cur:
            cur.execute(_COUNT_BY_SID_SQL, (source_event_id,))
            row = cur.fetchone()
            return row[0] if row else 0

    def event_exists(self, source_event_id: str) -> bool:
        """Return True if an event with *source_event_id* exists."""
        return self.count_by_source_event_id(source_event_id) > 0

    def get_media_clip_status(self, source_event_id: str) -> str | None:
        """Get payload->'media'->>'clip_status' for an event, or None."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT payload->'media'->>'clip_status' FROM events "
                "WHERE source_event_id = %s",
                (source_event_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def set_clip_status(
        self,
        event_id: str,
        status: str,
        replay_job_id: str = "",
        error_message: str = "",
    ) -> bool:
        """Set clip_status and optionally replay_job_id / error_message in payload.media.

        Returns True if a row was updated.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET payload = jsonb_set(
                        jsonb_set(
                            jsonb_set(
                                COALESCE(payload, '{}'::jsonb),
                                '{media,clip_status}',
                                %(status)s::jsonb
                            ),
                            '{media,recording_strategy}',
                            '"savant_replay"'::jsonb
                        ),
                        '{media,replay_job_id}',
                        %(replay_job_id)s::jsonb
                    ),
                    media_status = %(status_text)s,
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "status": json.dumps(status),
                    "status_text": status,
                    "replay_job_id": json.dumps(replay_job_id),
                    "event_id": event_id,
                },
            )
            if error_message:
                cur.execute(
                    """
                    UPDATE events
                    SET payload = jsonb_set(
                        COALESCE(payload, '{}'::jsonb),
                        '{media,error_message}',
                        %(error)s::jsonb
                    )
                    WHERE id = %(event_id)s::uuid
                    """,
                    {
                        "error": json.dumps(error_message),
                        "event_id": event_id,
                    },
                )
            return cur.rowcount is not None and cur.rowcount > 0
