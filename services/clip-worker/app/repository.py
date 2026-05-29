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
            return cur.rowcount is not None and cur.rowcount > 0
    except Exception:
        logger.exception("update_clip_status failed event_id=%s status=%s", event_id, status)
        return False
