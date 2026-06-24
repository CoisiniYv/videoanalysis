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

_INSERT_PERSON_BBOX_OBSERVATION_SQL = """
INSERT INTO person_bbox_observations (
    source_observation_id,
    source_id,
    camera_id,
    track_id,
    timestamp_ms,
    frame_pts,
    frame_num,
    person_bbox,
    person_confidence,
    gate_status,
    payload
) VALUES (
    %(source_observation_id)s,
    %(source_id)s,
    %(camera_id)s,
    %(track_id)s,
    %(timestamp_ms)s,
    %(frame_pts)s,
    %(frame_num)s,
    %(person_bbox)s::jsonb,
    %(person_confidence)s,
    %(gate_status)s,
    %(payload)s::jsonb
)
ON CONFLICT (source_observation_id) DO NOTHING
RETURNING id
"""

EVIDENCE_TASK_STATUSES = (
    "pending",
    "waiting_proof",
    "queued",
    "replaying",
    "finalizing",
    "processing",
    "ready",
    "partial",
    "failed",
    "not_implemented",
    "materialization_skipped",
)

OPERATOR_EVIDENCE_STATES = {
    "pending",
    "waiting_proof",
    "queued",
    "replaying",
    "finalizing",
    "ready",
    "failed",
    "not_implemented",
    "materialization_skipped",
}

_STATUS_TO_EVIDENCE_STATE = {
    "replay_job_created": "replaying",
    "generated": "ready",
    "generated_corrupt": "failed",
    "generated_unverified": "failed",
    "duration_guard_failed": "failed",
    "generated_annotation_failed": "failed",
    "skipped_by_poc_limit": "failed",
}


def _evidence_state_for_status(status: str) -> str:
    state = _STATUS_TO_EVIDENCE_STATE.get(str(status), str(status))
    return state if state in OPERATOR_EVIDENCE_STATES else "failed"

_MIDTERM_BEHAVIOR_NOT_IMPLEMENTED_REASON = (
    "Midterm behavior evidence created the evidence task, but production "
    "snapshot/clip/metadata generation is not implemented in this deployment."
)

_MIDTERM_FACE_MATCH_NOT_IMPLEMENTED_REASON = (
    "Midterm face match evidence created the evidence task, but production "
    "snapshot/raw_clip/metadata generation is not implemented in this deployment."
)


def _not_implemented_reason(event: Dict[str, Any]) -> str:
    if event.get("algorithm_type") == "face_intelligence" or event.get(
        "event_type"
    ) in ("watchlist_hit", "live_search_hit"):
        return _MIDTERM_FACE_MATCH_NOT_IMPLEMENTED_REASON
    return _MIDTERM_BEHAVIOR_NOT_IMPLEMENTED_REASON


def _evidence_task_initial_status(event: Dict[str, Any]) -> tuple[str, str]:
    """Return (status, error_message) for a new evidence task.

    For watchlist_hit / live_search_hit and intrusion, the recording
    pipeline (record_request -> clip-worker -> media-worker) can handle
    evidence generation, so the task starts as 'pending' with no error.
    Reserved behavior events still start as 'not_implemented'.
    """
    event_type = event.get("event_type", "")
    algorithm_type = event.get("algorithm_type", "")
    if algorithm_type == "face_intelligence" or event_type in (
        "watchlist_hit",
        "live_search_hit",
        "intrusion",
    ):
        return "pending", ""
    return "not_implemented", _MIDTERM_BEHAVIOR_NOT_IMPLEMENTED_REASON


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

    def insert_person_bbox_observation(self, observation: Dict[str, Any]) -> str | None:
        """Idempotently insert one accepted person bbox observation."""

        payload = observation.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        person_bbox = observation.get("person_bbox")
        params = {
            "source_observation_id": observation.get("source_observation_id", ""),
            "source_id": observation.get("source_id", ""),
            "camera_id": observation.get("camera_id", ""),
            "track_id": observation.get("track_id") or None,
            "timestamp_ms": int(observation.get("timestamp_ms", 0)),
            "frame_pts": observation.get("frame_pts"),
            "frame_num": observation.get("frame_num"),
            "person_bbox": json.dumps(person_bbox, ensure_ascii=False),
            "person_confidence": observation.get("person_confidence"),
            "gate_status": observation.get("gate_status") or "accepted",
            "payload": json.dumps(payload, ensure_ascii=False),
        }

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_INSERT_PERSON_BBOX_OBSERVATION_SQL, params)
            row = cur.fetchone()
            return str(row["id"]) if row else None

    def create_evidence_task(
        self,
        event: Dict[str, Any],
        event_id: str,
    ) -> str | None:
        """Create one idempotent evidence task for an event.

        For watchlist_hit / live_search_hit and intrusion the task starts
        as 'pending' because the recording pipeline (record_request ->
        clip-worker -> media-worker) can handle evidence generation.
        """
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"evidence:{event_id}"))
        policy = event.get("evidence_policy") or {}
        if not isinstance(policy, dict):
            policy = {}
        initial_status, error_message = _evidence_task_initial_status(event)

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
            "status": initial_status,
            "error_message": error_message,
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

        # Only mark the event's media status immediately for not_implemented.
        # For pending tasks, the media-worker will update the status after
        # generating the bundle. Setting clip_status="pending" here would
        # block the record_request gate in _handle_event.
        if initial_status == "not_implemented":
            self.set_evidence_status(
                event_id=event_id,
                status=initial_status,
                error_message=error_message,
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

        evidence_state = _evidence_state_for_status(status)
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
                                'evidence_state', %(evidence_state)s::text,
                                'evidence_reason', NULLIF(%(error_message)s::text, ''),
                                'evidence_state_updated_at', now(),
                                'error_message', %(error_message)s::text
                            )
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "event_id": event_id,
                    "status": status,
                    "evidence_state": evidence_state,
                    "error_message": error_message,
                    "snapshot_path": snapshot_path,
                    "clip_path": clip_path,
                    "metadata_path": metadata_path,
                },
            )
            updated = cur.rowcount is not None and cur.rowcount > 0
            if updated:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(status)s,
                        error_message = CASE
                            WHEN %(error_message)s::text != ''
                                THEN %(error_message)s::text
                            ELSE error_message
                        END,
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                    """,
                    {
                        "event_id": event_id,
                        "status": evidence_state,
                        "error_message": error_message,
                    },
                )
            return updated

    def count_by_source_event_id(self, source_event_id: str) -> int:
        """Return the number of rows with the given *source_event_id*."""
        with self._conn.cursor() as cur:
            cur.execute(_COUNT_BY_SID_SQL, (source_event_id,))
            row = cur.fetchone()
            return row[0] if row else 0

    def event_exists(self, source_event_id: str) -> bool:
        """Return True if an event with *source_event_id* exists."""
        return self.count_by_source_event_id(source_event_id) > 0

    def get_camera_alert_policy(self, camera_id: str) -> dict[str, Any]:
        """Return cameras.alert_policy for *camera_id*, or {} when absent."""
        if not camera_id:
            return {}
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT alert_policy FROM cameras WHERE id = %s",
                    (camera_id,),
                )
                row = cur.fetchone()
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            return {}
        if not row:
            return {}
        policy = row[0]
        if isinstance(policy, str):
            try:
                return json.loads(policy)
            except json.JSONDecodeError:
                return {}
        return policy if isinstance(policy, dict) else {}

    def get_last_unsuppressed_alert_ts_ms(
        self,
        camera_id: str,
        *,
        exclude_source_event_id: str,
        current_event_ts_ms: int,
        event_type: str = "",
        algorithm_type: str = "",
        cooldown_scope: str = "algorithm",
    ) -> int | None:
        """Return the most recent non-suppressed event timestamp for a scope."""
        row = self.get_last_unsuppressed_alert(
            camera_id,
            exclude_source_event_id=exclude_source_event_id,
            current_event_ts_ms=current_event_ts_ms,
            event_type=event_type,
            algorithm_type=algorithm_type,
            cooldown_scope=cooldown_scope,
        )
        if not row:
            return None
        value = row.get("event_ts_ms")
        return int(value) if value is not None else None

    def get_last_unsuppressed_alert(
        self,
        camera_id: str,
        *,
        exclude_source_event_id: str,
        current_event_ts_ms: int,
        event_type: str = "",
        algorithm_type: str = "",
        cooldown_scope: str = "algorithm",
    ) -> dict[str, Any] | None:
        """Return the most recent non-suppressed event row for a scope."""
        if not camera_id:
            return None
        scope = str(cooldown_scope or "algorithm").strip().lower()
        if scope in {"global", "camera", "camera_global"}:
            scope_filter = ""
            scope_params: dict[str, object] = {}
        elif scope in {"event", "event_type"}:
            if not event_type:
                return None
            scope_filter = "AND event_type = %(event_type)s"
            scope_params = {"event_type": event_type}
        else:
            key = algorithm_type or event_type
            if not key:
                return None
            scope_filter = (
                "AND COALESCE(NULLIF(algorithm_type, ''), event_type) = "
                "%(algorithm_key)s"
            )
            scope_params = {"algorithm_key": key}
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT event_ts_ms, event_type, algorithm_type
                FROM events
                WHERE camera_id = %(camera_id)s
                  AND source_event_id <> %(exclude_source_event_id)s
                  AND COALESCE(status, 'new') <> 'suppressed'
                  AND (%(current_event_ts_ms)s <= 0
                       OR event_ts_ms <= %(current_event_ts_ms)s)
                  {scope_filter}
                ORDER BY event_ts_ms DESC
                LIMIT 1
                """,
                {
                    "camera_id": camera_id,
                    "exclude_source_event_id": exclude_source_event_id,
                    "current_event_ts_ms": current_event_ts_ms,
                    **scope_params,
                },
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "event_ts_ms": row[0],
            "event_type": row[1],
            "algorithm_type": row[2],
        }

    def has_event_type_since_ts_ms(
        self,
        *,
        source_id: str,
        event_type: str,
        since_ts_ms: int,
    ) -> bool:
        """Return True if an unsuppressed event type was created after a timestamp."""
        if not source_id or not event_type:
            return False
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM events
                WHERE source_id = %s
                  AND event_type = %s
                  AND created_at >= to_timestamp(%s::double precision / 1000.0)
                  AND COALESCE(status, 'new') <> 'suppressed'
                LIMIT 1
                """,
                (source_id, event_type, since_ts_ms),
            )
            return cur.fetchone() is not None

    def mark_event_suppressed(
        self,
        event_id: str,
        *,
        reason: str,
        policy: dict[str, Any],
        last_alert_ts_ms: int | None,
        cooldown_scope: str = "algorithm",
        cooldown_key: str = "",
        last_alert_event_type: str | None = None,
        last_alert_algorithm_type: str | None = None,
    ) -> bool:
        """Mark an already-inserted event as suppressed by alert policy."""
        payload = {
            "decision": "suppressed",
            "reason": reason,
            "policy": policy or {},
            "last_alert_ts_ms": last_alert_ts_ms,
            "cooldown_scope": cooldown_scope,
            "cooldown_key": cooldown_key,
            "last_alert_event_type": last_alert_event_type,
            "last_alert_algorithm_type": last_alert_algorithm_type,
        }
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET status = 'suppressed',
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'alert_policy',
                            %(payload)s::jsonb
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {"event_id": event_id, "payload": json.dumps(payload)},
            )
            return cur.rowcount is not None and cur.rowcount > 0

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

    def get_evidence_task_status(self, event_id: str) -> str | None:
        """Get the current evidence task status for an event, or None."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT status
                FROM evidence_tasks
                WHERE event_id = %s::uuid
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (event_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def mark_evidence_materialization_skipped(
        self,
        event_id: str,
        *,
        reason: str,
    ) -> bool:
        """Mark an evidence task terminal when recording policy skips generation."""
        reason_text = reason or "recording_policy_skipped"
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET media_status = 'materialization_skipped',
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'clip_status', 'materialization_skipped',
                                'metadata_status', 'materialization_skipped',
                                'evidence_state', 'materialization_skipped',
                                'evidence_reason', %(reason)s::text,
                                'evidence_state_updated_at', now(),
                                'materialization_status', 'materialization_skipped',
                                'materialization_reason', %(reason)s::text,
                                'error_message', %(reason)s::text
                            ))
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {"event_id": event_id, "reason": reason_text},
            )
            updated = cur.rowcount is not None and cur.rowcount > 0
            if updated:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = 'materialization_skipped',
                        materialization_status = 'materialization_skipped',
                        materialization_defer_reason = %(reason)s::text,
                        error_message = %(reason)s::text,
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                    """,
                    {"event_id": event_id, "reason": reason_text},
                )
            return updated

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
        evidence_state = _evidence_state_for_status(status)
        evidence_reason = error_message or (status if status != evidence_state else "")
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_strip_nulls(jsonb_build_object(
                                'clip_status', %(status_text)s::text,
                                'recording_strategy', 'savant_replay',
                                'replay_job_id', NULLIF(%(replay_job_id)s::text, ''),
                                'evidence_state', %(evidence_state)s::text,
                                'evidence_reason', NULLIF(%(evidence_reason)s::text, ''),
                                'evidence_state_updated_at', now(),
                                'error_message', NULLIF(%(error_message)s::text, '')
                            ))
                        ),
                    media_status = %(evidence_state)s,
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "status_text": status,
                    "replay_job_id": replay_job_id,
                    "evidence_state": evidence_state,
                    "evidence_reason": evidence_reason,
                    "error_message": error_message,
                    "event_id": event_id,
                },
            )
            updated = cur.rowcount is not None and cur.rowcount > 0
            if updated:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(evidence_state)s,
                        error_message = CASE
                            WHEN %(evidence_reason)s::text != ''
                                THEN %(evidence_reason)s::text
                            ELSE error_message
                        END,
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                    """,
                    {
                        "evidence_state": evidence_state,
                        "evidence_reason": evidence_reason,
                        "event_id": event_id,
                    },
                )
            return updated
