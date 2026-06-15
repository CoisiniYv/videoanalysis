"""Clip-worker repository — update media status in events table."""

from __future__ import annotations

import json
import logging

import psycopg

logger = logging.getLogger(__name__)

EVIDENCE_STATES = {
    "pending",
    "waiting_proof",
    "queued",
    "replaying",
    "finalizing",
    "ready",
    "failed",
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


def evidence_state_for_status(status: str, override: str | None = None) -> str:
    """Map legacy clip status values onto the operator-visible state family."""
    if override:
        state = str(override)
    else:
        state = _STATUS_TO_EVIDENCE_STATE.get(str(status), str(status))
    return state if state in EVIDENCE_STATES else "failed"


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
) -> bool:
    """Set clip status and synchronize operator-visible evidence state."""
    if not event_id:
        return False
    state = evidence_state_for_status(status, evidence_state)
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
                                'evidence_request_id', NULLIF(%(request_id)s::text, ''),
                                'evidence_attempt_count', %(attempt_count)s::int,
                                'evidence_diagnostics', %(diagnostics)s::jsonb,
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
                    "evidence_reason": reason,
                    "request_id": request_id,
                    "attempt_count": attempt_count,
                    "diagnostics": _json_or_null(diagnostics),
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
                    SET status = %(evidence_state)s,
                        error_message = CASE
                            WHEN %(evidence_reason)s::text != ''
                                THEN %(evidence_reason)s::text
                            ELSE error_message
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
                        "evidence_reason": reason,
                        "attempt_count": attempt_count,
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
                    SET status = %(evidence_state)s,
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
