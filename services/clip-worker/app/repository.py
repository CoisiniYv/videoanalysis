"""Clip-worker repository — update media status in events table."""

from __future__ import annotations

import json
import logging
import uuid

import psycopg

logger = logging.getLogger(__name__)

EVIDENCE_STATES = {
    "manifest_ready",
    "materialization_pending",
    "materializing",
    "materialized",
    "materialization_deferred",
    "materialization_failed",
    "materialization_expired",
    "materialization_skipped",
    "pending",
    "waiting_proof",
    "queued",
    "replaying",
    "finalizing",
    "ready",
    "failed",
}

TERMINAL_EVIDENCE_STATES = {
    "failed",
    "generated",
    "generated_annotation_failed",
    "generated_corrupt",
    "generated_unverified",
    "materialization_expired",
    "materialization_failed",
    "materialization_skipped",
    "materialized",
    "media_deleted",
    "media_expired",
    "ready",
    "skipped_by_poc_limit",
}

_STATUS_TO_EVIDENCE_STATE = {
    "pending": "materialization_pending",
    "replay_job_created": "materializing",
    "generated": "materialized",
    "ready": "materialized",
    "generated_corrupt": "materialization_failed",
    "generated_unverified": "materialization_failed",
    "duration_guard_failed": "materialization_failed",
    "generated_annotation_failed": "materialization_failed",
    "skipped_by_poc_limit": "materialization_failed",
    "failed": "materialization_failed",
}


def evidence_state_for_status(status: str, override: str | None = None) -> str:
    """Map legacy clip status values onto the operator-visible state family."""
    if override:
        state = str(override)
    else:
        state = _STATUS_TO_EVIDENCE_STATE.get(str(status), str(status))
    return state if state in EVIDENCE_STATES else "failed"


def materialization_status_for_state(state: str) -> str:
    """Return the manifest-first materialization status for an evidence state."""
    if state in {
        "manifest_ready",
        "materialization_pending",
        "materializing",
        "materialized",
        "materialization_deferred",
        "materialization_failed",
        "materialization_expired",
        "materialization_skipped",
    }:
        return state
    if state in {"pending", "queued", "waiting_proof", "replaying", "finalizing"}:
        return "materialization_pending"
    if state == "ready":
        return "materialized"
    if state == "failed":
        return "materialization_failed"
    return "manifest_ready"


def _json_or_null(value: object) -> str:
    return json.dumps(value)


def _reason_for_state(status: str, state: str, reason: str, error_message: str) -> str:
    if reason:
        return reason
    if error_message:
        return error_message
    if status != state:
        return status
    return ""


def _replay_shard_value(replay_shard: dict | None, key: str) -> str:
    if not isinstance(replay_shard, dict):
        return ""
    return str(replay_shard.get(key) or "")


def expire_materialization_deadlines(pg_conn: psycopg.Connection) -> int:
    """Mark deferred or pending manifest-first tasks expired after TTL deadline."""
    if not callable(getattr(pg_conn, "cursor", None)):
        return 0
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                WITH expired AS (
                    UPDATE evidence_tasks
                    SET status = 'materialization_expired',
                        materialization_status = 'materialization_expired',
                        materialization_expired_reason = concat(
                            'materialization_deadline_expired:',
                            COALESCE(materialization_deadline_at::text, '')
                        ),
                        error_message = concat(
                            'materialization_deadline_expired:',
                            COALESCE(materialization_deadline_at::text, '')
                        ),
                        updated_at = now()
                    WHERE materialization_status IN (
                        'manifest_ready',
                        'materialization_pending',
                        'materialization_deferred'
                    )
                      AND materialization_deadline_at IS NOT NULL
                      AND materialization_deadline_at <= now()
                    RETURNING event_id, materialization_deadline_at
                )
                UPDATE events e
                SET media_status = 'materialization_expired',
                    payload = COALESCE(e.payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(e.payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'evidence_state', 'materialization_expired',
                                'evidence_reason',
                                    'materialization_deadline_expired',
                                'evidence_state_updated_at', now(),
                                'materialization_status',
                                    'materialization_expired',
                                'materialization_reason',
                                    'materialization_deadline_expired',
                                'materialization_expired_at', now()
                            )
                        ),
                    updated_at = now()
                WHERE e.id IN (SELECT event_id FROM expired)
                """
            )
            return int(getattr(cur, "rowcount", 0) or 0)
    except Exception:
        logger.exception("expire_materialization_deadlines failed")
        return 0


def get_evidence_diagnostics(pg_conn: psycopg.Connection, event_id: str) -> dict:
    """Return persisted clip-worker diagnostics for an event."""
    if not event_id:
        return {}
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return {}
    try:
        with cursor_factory() as cur:
            cur.execute(
                """
                SELECT payload->'media'->'evidence_diagnostics' AS diagnostics
                FROM events
                WHERE id = %(event_id)s::uuid
                """,
                {"event_id": event_id},
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("get_evidence_diagnostics failed event_id=%s", event_id)
        return {}
    if row is None:
        return {}
    if isinstance(row, dict):
        diagnostics = row.get("diagnostics")
    else:
        diagnostics = row[0] if row else None
    return diagnostics if isinstance(diagnostics, dict) else {}


def terminal_evidence_state(pg_conn: psycopg.Connection, event_id: str) -> str:
    """Return the terminal evidence state for an event, or empty when active."""
    if not event_id:
        return ""
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return ""
    try:
        with cursor_factory() as cur:
            cur.execute(
                """
                SELECT
                    e.media_status,
                    e.payload->'media'->>'evidence_state' AS evidence_state,
                    latest_task.status AS latest_task_status,
                    latest_task.materialization_status
                        AS latest_materialization_status
                FROM events e
                LEFT JOIN LATERAL (
                    SELECT et.status, et.materialization_status
                    FROM evidence_tasks et
                    WHERE et.event_id = e.id
                       OR et.source_event_id = e.source_event_id
                    ORDER BY et.updated_at DESC, et.created_at DESC, et.task_id DESC
                    LIMIT 1
                ) latest_task ON true
                WHERE e.id = %(event_id)s::uuid
                """,
                {"event_id": event_id},
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("terminal_evidence_state failed event_id=%s", event_id)
        return ""
    if row is None:
        return ""
    if isinstance(row, dict):
        values = (
            row.get("latest_materialization_status"),
            row.get("latest_task_status"),
            row.get("evidence_state"),
            row.get("media_status"),
        )
    else:
        values = tuple(row)
    for value in values:
        state = str(value or "")
        if state in TERMINAL_EVIDENCE_STATES:
            return state
    return ""


def record_request_target_exists(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    source_event_id: str = "",
) -> bool | None:
    """Return whether a record request still has a DB event/task target.

    ``None`` means the target could not be checked safely, so callers should
    keep the existing retry/failure behavior instead of acking the message.
    """
    event_id_text = str(event_id or "")
    source_event_id_text = str(source_event_id or "")
    if not event_id_text and not source_event_id_text:
        return None
    cursor_factory = getattr(pg_conn, "cursor", None)
    if not callable(cursor_factory):
        return None
    event_id_uuid = None
    if event_id_text:
        try:
            event_id_uuid = str(uuid.UUID(event_id_text))
        except ValueError:
            event_id_uuid = None
    try:
        with cursor_factory() as cur:
            cur.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM events
                        WHERE (
                            %(event_id_uuid)s::uuid IS NOT NULL
                            AND id = %(event_id_uuid)s::uuid
                        )
                        OR (
                            %(source_event_id)s::text <> ''
                            AND source_event_id = %(source_event_id)s::text
                        )
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM evidence_tasks
                        WHERE (
                            %(event_id_uuid)s::uuid IS NOT NULL
                            AND event_id = %(event_id_uuid)s::uuid
                        )
                        OR (
                            %(source_event_id)s::text <> ''
                            AND source_event_id = %(source_event_id)s::text
                        )
                    ) AS target_exists
                """,
                {
                    "event_id_uuid": event_id_uuid,
                    "source_event_id": source_event_id_text,
                },
            )
            row = cur.fetchone()
    except Exception:
        logger.exception(
            "record_request_target_exists failed event_id=%s source_event_id=%s",
            event_id_text,
            source_event_id_text,
        )
        return None
    if row is None:
        return None
    if isinstance(row, dict):
        value = row.get("target_exists")
    else:
        value = row[0] if row else None
    if value is None:
        return None
    return bool(value)


def update_clip_status(
    pg_conn: psycopg.Connection,
    event_id: str,
    status: str,
    replay_job_id: str = "",
    error_message: str = "",
    replay_job_request: dict | None = None,
    evidence_state: str | None = None,
    evidence_reason: str = "",
    request_id: str = "",
    attempt_count: int | None = None,
    diagnostics: dict | None = None,
    replay_shard: dict | None = None,
    materialization_deadline_at: str = "",
    quota_decision: dict | None = None,
    degrade_decision: dict | None = None,
) -> bool:
    """Set clip status and synchronize operator-visible evidence state."""
    if not event_id:
        return False
    state = evidence_state_for_status(status, evidence_state)
    materialization_state = materialization_status_for_state(state)
    reason = _reason_for_state(status, state, evidence_reason, error_message)
    try:
        with pg_conn.cursor() as cur:
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
                                'materialization_status', %(materialization_status)s::text,
                                'materialization_reason', NULLIF(%(evidence_reason)s::text, ''),
                                'materialization_deadline_at',
                                    NULLIF(%(materialization_deadline_at)s::text, ''),
                                'quota_decision', %(quota_decision)s::jsonb,
                                'degrade_decision', %(degrade_decision)s::jsonb,
                                'evidence_request_id', NULLIF(%(request_id)s::text, ''),
                                'evidence_attempt_count', %(attempt_count)s::int,
                                'evidence_diagnostics', %(diagnostics)s::jsonb,
                                'replay_shard_id', NULLIF(%(replay_shard_id)s::text, ''),
                                'replay_api_url', NULLIF(%(replay_api_url)s::text, ''),
                                'replay_job_sink_url', NULLIF(%(replay_job_sink_url)s::text, ''),
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
                    "evidence_state": state,
                    "materialization_status": materialization_state,
                    "evidence_reason": reason,
                    "request_id": request_id,
                    "attempt_count": attempt_count,
                    "diagnostics": _json_or_null(diagnostics),
                    "quota_decision": _json_or_null(quota_decision or {}),
                    "degrade_decision": _json_or_null(degrade_decision or {}),
                    "materialization_deadline_at": materialization_deadline_at,
                    "replay_shard_id": _replay_shard_value(replay_shard, "shard_id"),
                    "replay_api_url": _replay_shard_value(replay_shard, "replay_api_url"),
                    "replay_job_sink_url": _replay_shard_value(
                        replay_shard,
                        "replay_job_sink_url",
                    ),
                    "error_message": error_message,
                    "event_id": event_id,
                },
            )
            event_updated = cur.rowcount is not None and cur.rowcount > 0
            if replay_job_request is not None:
                cur.execute(
                    """
                    UPDATE events
                    SET payload = jsonb_set(
                            COALESCE(payload, '{}'::jsonb),
                            '{media,replay_job_request}',
                            %(request)s::jsonb
                        ),
                        updated_at = now()
                    WHERE id = %(event_id)s::uuid
                    """,
                    {
                        "request": json.dumps(replay_job_request),
                        "event_id": event_id,
                    },
                )
            if event_updated:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(materialization_status)s,
                        materialization_status = %(materialization_status)s,
                        error_message = CASE
                            WHEN %(evidence_reason)s::text != ''
                                THEN %(evidence_reason)s::text
                            ELSE error_message
                        END,
                        replay_shard_id = COALESCE(
                            NULLIF(%(replay_shard_id)s::text, ''),
                            replay_shard_id
                        ),
                        replay_api_url = COALESCE(
                            NULLIF(%(replay_api_url)s::text, ''),
                            replay_api_url
                        ),
                        replay_job_sink_url = COALESCE(
                            NULLIF(%(replay_job_sink_url)s::text, ''),
                            replay_job_sink_url
                        ),
                        materialization_deadline_at = COALESCE(
                            NULLIF(%(materialization_deadline_at)s::text, '')::timestamptz,
                            materialization_deadline_at
                        ),
                        materialization_attempt_count = GREATEST(
                            materialization_attempt_count,
                            COALESCE(%(attempt_count)s::int, materialization_attempt_count)
                        ),
                        materialization_defer_reason = CASE
                            WHEN %(materialization_status)s::text = 'materialization_deferred'
                                THEN %(evidence_reason)s::text
                            ELSE materialization_defer_reason
                        END,
                        materialization_failure_reason = CASE
                            WHEN %(materialization_status)s::text = 'materialization_failed'
                                THEN %(evidence_reason)s::text
                            ELSE materialization_failure_reason
                        END,
                        materialization_expired_reason = CASE
                            WHEN %(materialization_status)s::text = 'materialization_expired'
                                THEN %(evidence_reason)s::text
                            ELSE materialization_expired_reason
                        END,
                        quota_decision = CASE
                            WHEN %(quota_decision)s::jsonb <> '{}'::jsonb
                                THEN %(quota_decision)s::jsonb
                            ELSE quota_decision
                        END,
                        degrade_decision = CASE
                            WHEN %(degrade_decision)s::jsonb <> '{}'::jsonb
                                THEN %(degrade_decision)s::jsonb
                            ELSE degrade_decision
                        END,
                        retry_count = GREATEST(
                            retry_count,
                            COALESCE(%(attempt_count)s::int, retry_count)
                        ),
                        updated_at = now()
                    WHERE event_id = %(event_id)s::uuid
                    """,
                    {
                        "event_id": event_id,
                        "evidence_state": state,
                        "materialization_status": materialization_state,
                        "evidence_reason": reason,
                        "attempt_count": attempt_count,
                        "replay_shard_id": _replay_shard_value(
                            replay_shard,
                            "shard_id",
                        ),
                        "replay_api_url": _replay_shard_value(
                            replay_shard,
                            "replay_api_url",
                        ),
                        "replay_job_sink_url": _replay_shard_value(
                            replay_shard,
                            "replay_job_sink_url",
                        ),
                        "materialization_deadline_at": materialization_deadline_at,
                        "quota_decision": _json_or_null(quota_decision or {}),
                        "degrade_decision": _json_or_null(degrade_decision or {}),
                    },
                )
            return event_updated
    except Exception:
        logger.exception("update_clip_status failed event_id=%s status=%s", event_id, status)
        return False


def update_evidence_media_result(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    task_id: str | None,
    media_status: str,
    snapshot_status: str,
    metadata_status: str,
    clip_status: str,
    snapshot_path: str | None,
    metadata_path: str,
    output_root: str,
    clip_path: str | None = None,
    storage_fallback_used: bool = False,
    storage_fallback_reason: str | None = None,
    error_message: str | None = None,
) -> bool:
    """Update events and evidence_tasks after midterm metadata/snapshot output."""
    if not event_id:
        return False
    evidence_state = evidence_state_for_status(media_status)
    materialization_state = materialization_status_for_state(evidence_state)
    evidence_reason = error_message or (media_status if media_status != evidence_state else "")
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET snapshot_path = COALESCE(%(snapshot_path)s, snapshot_path),
                    clip_path = COALESCE(%(clip_path)s, clip_path),
                    media_status = %(media_status)s,
                    payload = COALESCE(payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'media',
                            COALESCE(payload->'media', '{}'::jsonb)
                            || jsonb_build_object(
                                'snapshot_status', %(snapshot_status)s::text,
                                'metadata_status', %(metadata_status)s::text,
                                'clip_status', %(clip_status)s::text,
                                'snapshot_path', %(snapshot_path)s::text,
                                'clip_path', %(clip_path)s::text,
                                'metadata_path', %(metadata_path)s::text,
                                'raw_clip_path', %(clip_path)s::text,
                                'annotated_clip_path', NULL,
                                'evidence_state', %(evidence_state)s::text,
                                'evidence_reason', NULLIF(%(evidence_reason)s::text, ''),
                                'evidence_state_updated_at', now(),
                                'materialization_status', %(materialization_status)s::text,
                                'materialization_reason', NULLIF(%(evidence_reason)s::text, ''),
                                'clip_error_message', %(clip_error_message)s::text,
                                'error_message', %(error_message)s::text
                            )
                        ),
                    updated_at = now()
                WHERE id = %(event_id)s::uuid
                """,
                {
                    "event_id": event_id,
                    "media_status": media_status,
                    "evidence_state": evidence_state,
                    "materialization_status": materialization_state,
                    "evidence_reason": evidence_reason,
                    "snapshot_status": snapshot_status,
                    "metadata_status": metadata_status,
                    "clip_status": clip_status,
                    "snapshot_path": snapshot_path,
                    "clip_path": clip_path,
                    "metadata_path": metadata_path,
                    "clip_error_message": error_message
                    if clip_status in ("failed", "not_implemented")
                    else None,
                    "error_message": error_message,
                },
            )
            event_updated = cur.rowcount is not None and cur.rowcount > 0

            if task_id or event_updated:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(materialization_status)s,
                        materialization_status = %(materialization_status)s,
                        snapshot_path = COALESCE(%(snapshot_path)s, snapshot_path),
                        clip_path = COALESCE(%(clip_path)s, clip_path),
                        metadata_path = %(metadata_path)s,
                        output_root = %(output_root)s,
                        storage_fallback_used = %(storage_fallback_used)s,
                        storage_fallback_reason = %(storage_fallback_reason)s,
                        error_message = %(evidence_reason)s,
                        updated_at = now()
                    WHERE (%(task_id)s IS NOT NULL AND task_id = %(task_id)s)
                       OR (%(task_id)s IS NULL AND event_id = %(event_id)s::uuid)
                    """,
                    {
                        "task_id": task_id,
                        "event_id": event_id,
                        "evidence_state": evidence_state,
                        "materialization_status": materialization_state,
                        "snapshot_path": snapshot_path,
                        "clip_path": clip_path,
                        "metadata_path": metadata_path,
                        "output_root": output_root,
                        "storage_fallback_used": storage_fallback_used,
                        "storage_fallback_reason": storage_fallback_reason,
                        "evidence_reason": evidence_reason,
                    },
                )
            return event_updated
    except Exception:
        logger.exception(
            "update_evidence_media_result failed event_id=%s status=%s",
            event_id,
            media_status,
        )
        return False
