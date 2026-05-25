"""PostgreSQL queries for evidence-worker."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row


def find_events_needing_evidence(
    conn: psycopg.Connection, limit: int = 5
) -> List[Dict[str, Any]]:
    """Return intrusion events that need evidence generated.

    Criteria: event_type='intrusion' AND snapshot_path IS NULL.
    Most recent events first.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, source_event_id, event_type, camera_id, source_id,
                   track_id, confidence, event_ts_ms, payload
            FROM events
            WHERE event_type = 'intrusion'
              AND snapshot_path IS NULL
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def update_event_evidence(
    conn: psycopg.Connection,
    event_id: str,
    snapshot_path: str,
    annotated_snapshot_path: str,
    clip_path: str,
    frame_num: int,
    track_id: str,
) -> bool:
    """Write evidence paths and status to the event row.

    snapshot_path and clip_path go to top-level columns.
    annotated_snapshot_path and status fields go to payload.media JSONB.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE events
            SET snapshot_path = %(snapshot_path)s,
                clip_path = %(clip_path)s,
                payload = jsonb_set(
                    jsonb_set(
                        jsonb_set(
                            jsonb_set(
                                jsonb_set(
                                    jsonb_set(
                                        COALESCE(payload, '{}'::jsonb),
                                        '{media,annotated_snapshot_path}',
                                        %(annotated_path)s::jsonb
                                    ),
                                    '{media,snapshot_status}',
                                    '"ready"'::jsonb
                                ),
                                '{media,clip_status}',
                                '"ready"'::jsonb
                            ),
                            '{media,annotated_snapshot_status}',
                            '"ready"'::jsonb
                        ),
                        '{media,evidence_frame_num}',
                        %(frame_num)s::jsonb
                    ),
                    '{media,evidence_track_id}',
                    %(track_id)s::jsonb
                ),
                updated_at = now()
            WHERE id = %(event_id)s::uuid
            """,
            {
                "snapshot_path": snapshot_path,
                "clip_path": clip_path,
                "annotated_path": json.dumps(annotated_snapshot_path),
                "frame_num": json.dumps(frame_num),
                "track_id": json.dumps(int(track_id) if track_id else 0),
                "event_id": event_id,
            },
        )
        return cur.rowcount is not None and cur.rowcount > 0
