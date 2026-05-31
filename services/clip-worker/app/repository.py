"""Clip-worker repository — update media status in events table."""

from __future__ import annotations

import json
import logging

import psycopg

logger = logging.getLogger(__name__)

_SET_CLIP_STATUS_SQL = """
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
"""

_SET_CLIP_ERROR_SQL = """
UPDATE events
SET payload = jsonb_set(
        COALESCE(payload, '{}'::jsonb),
        '{media,error_message}',
        %(error)s::jsonb
    ),
    updated_at = now()
WHERE id = %(event_id)s::uuid
"""


def update_clip_status(
    pg_conn: psycopg.Connection,
    event_id: str,
    status: str,
    replay_job_id: str = "",
    error_message: str = "",
    replay_job_request: dict | None = None,
) -> bool:
    """Set clip_status and optionally replay_job_id / error_message."""
    if not event_id:
        return False
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                _SET_CLIP_STATUS_SQL,
                {
                    "status": json.dumps(status),
                    "status_text": status,
                    "replay_job_id": json.dumps(replay_job_id),
                    "event_id": event_id,
                },
            )
            if error_message:
                cur.execute(
                    _SET_CLIP_ERROR_SQL,
                    {"error": json.dumps(error_message), "event_id": event_id},
                )
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
            return cur.rowcount is not None and cur.rowcount > 0
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
    """Update events and evidence_tasks after R3.2A metadata/snapshot output."""
    if not event_id:
        return False
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

            if task_id:
                cur.execute(
                    """
                    UPDATE evidence_tasks
                    SET status = %(media_status)s,
                        snapshot_path = COALESCE(%(snapshot_path)s, snapshot_path),
                        clip_path = COALESCE(%(clip_path)s, clip_path),
                        metadata_path = %(metadata_path)s,
                        output_root = %(output_root)s,
                        storage_fallback_used = %(storage_fallback_used)s,
                        storage_fallback_reason = %(storage_fallback_reason)s,
                        error_message = %(error_message)s,
                        updated_at = now()
                    WHERE task_id = %(task_id)s
                    """,
                    {
                        "task_id": task_id,
                        "media_status": media_status,
                        "snapshot_path": snapshot_path,
                        "clip_path": clip_path,
                        "metadata_path": metadata_path,
                        "output_root": output_root,
                        "storage_fallback_used": storage_fallback_used,
                        "storage_fallback_reason": storage_fallback_reason,
                        "error_message": error_message,
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
