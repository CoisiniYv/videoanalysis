"""PostgreSQL outbox helpers for Qdrant gallery synchronization."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row


def claim_outbox_rows(
    conn: psycopg.Connection,
    *,
    claimed_by: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Claim pending/retry rows using SKIP LOCKED and return them in order."""
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE gallery_vector_sync_outbox
                SET status = 'processing',
                    attempts = attempts + 1,
                    claimed_at = now(),
                    claimed_by = %(claimed_by)s,
                    updated_at = now()
                WHERE id IN (
                    SELECT id
                    FROM gallery_vector_sync_outbox
                    WHERE status IN ('pending', 'retry')
                      AND next_attempt_at <= now()
                    ORDER BY created_at, id
                    LIMIT %(limit)s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                {"claimed_by": claimed_by, "limit": max(1, int(limit))},
            )
            return [dict(row) for row in cur.fetchall()]


def mark_outbox_completed(conn: psycopg.Connection, row_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE gallery_vector_sync_outbox
            SET status = 'completed',
                processed_at = now(),
                updated_at = now(),
                last_error = NULL
            WHERE id = %(id)s
            """,
            {"id": row_id},
        )


def mark_outbox_failed(
    conn: psycopg.Connection,
    row_id: int,
    *,
    error: str,
    attempts: int,
    max_attempts: int,
) -> None:
    terminal = attempts >= max_attempts
    status = "poisoned" if terminal else "retry"
    delay_seconds = min(300, int(math.pow(2, max(0, attempts - 1))))
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE gallery_vector_sync_outbox
            SET status = %(status)s,
                last_error = %(error)s,
                next_attempt_at = CASE
                    WHEN %(terminal)s THEN next_attempt_at
                    ELSE now() + (%(delay_seconds)s || ' seconds')::interval
                END,
                updated_at = now()
            WHERE id = %(id)s
            """,
            {
                "id": row_id,
                "status": status,
                "error": error[:2000],
                "terminal": terminal,
                "delay_seconds": delay_seconds,
            },
        )


def active_gallery_rows(
    conn: psycopg.Connection,
    *,
    after_id: int = 0,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    register_vector(conn)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                pge.id,
                pge.person_id,
                p.name AS person_name,
                p.external_person_id,
                pge.source_type,
                pge.embedding_model,
                pge.model_version,
                pge.embedding,
                pge.is_primary,
                pge.is_active,
                pge.quality,
                pge.created_at,
                pge.updated_at
            FROM person_gallery_embeddings pge
            JOIN persons p ON p.id = pge.person_id
            WHERE p.is_active = true
              AND pge.is_active = true
              AND pge.embedding IS NOT NULL
              AND pge.id > %(after_id)s
            ORDER BY pge.id
            LIMIT %(limit)s
            """,
            {"after_id": after_id, "limit": max(1, int(limit))},
        )
        return [dict(row) for row in cur.fetchall()]


def gallery_row_by_id(conn: psycopg.Connection, gallery_embedding_id: int) -> dict[str, Any] | None:
    register_vector(conn)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                pge.id,
                pge.person_id,
                p.name AS person_name,
                p.external_person_id,
                pge.source_type,
                pge.embedding_model,
                pge.model_version,
                pge.embedding,
                pge.is_primary,
                pge.is_active,
                pge.quality,
                pge.created_at,
                pge.updated_at
            FROM person_gallery_embeddings pge
            JOIN persons p ON p.id = pge.person_id
            WHERE p.id = pge.person_id
              AND p.is_active = true
              AND pge.is_active = true
              AND pge.embedding IS NOT NULL
              AND pge.id = %(id)s
            """,
            {"id": gallery_embedding_id},
        )
        row = cur.fetchone()
        return dict(row) if row else None


def active_gallery_count(conn: psycopg.Connection) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT count(*) AS count
            FROM person_gallery_embeddings pge
            JOIN persons p ON p.id = pge.person_id
            WHERE p.is_active = true
              AND pge.is_active = true
              AND pge.embedding IS NOT NULL
            """
        )
        return int(cur.fetchone()["count"])


def outbox_status_summary(conn: psycopg.Connection) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT status, count(*) AS count
            FROM gallery_vector_sync_outbox
            GROUP BY status
            ORDER BY status
            """
        )
        counts = {str(row["status"]): int(row["count"]) for row in cur.fetchall()}
        cur.execute(
            """
                SELECT
                    count(*) FILTER (WHERE status IN ('pending', 'retry', 'processing')) AS active,
                    EXTRACT(EPOCH FROM (
                        now() - min(created_at)
                            FILTER (WHERE status IN ('pending', 'retry', 'processing'))
                    )) AS oldest_active_age_s,
                    EXTRACT(EPOCH FROM (
                        now() - max(processed_at)
                            FILTER (WHERE status = 'completed')
                    )) AS newest_completed_age_s
                FROM gallery_vector_sync_outbox
                """
        )
        row = dict(cur.fetchone())
    row["status_counts"] = counts
    row["created_at"] = datetime.now(timezone.utc).isoformat()
    return row
