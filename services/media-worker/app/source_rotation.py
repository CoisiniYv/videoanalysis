"""Durable per-source scheduling rotation for the evidence lanes.

Ranking candidates per source makes one scheduler window fair across the
sources it can hold. It does not bound how long a camera waits: the ranking is
recomputed every poll and remembers nothing, so with more cameras than window
slots the camera carrying the deepest backlog supplies the oldest rank-1 row
every poll and quieter cameras are never selected.

This module supplies the missing memory. The scheduler asks which sources to
serve this poll, fetches candidates only from those sources, and advances a
source's cursor when one of its tasks is actually claimed. A rejected claim or
a coverage deferral is not service and does not advance anything.

The cursor lives in PostgreSQL rather than in scheduler memory so that several
scheduler processes converge on one rotation instead of each keeping a private
view and believing itself fair.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import psycopg
from psycopg.rows import dict_row


REMUX_LANE = "remux"
IMAGE_LANE = "image"

# A source that has never been served sorts ahead of every served source.
NEVER_SERVED_SEQ = -1


@dataclass(frozen=True)
class SourceTurn:
    """One source the scheduler should draw candidates from this poll."""

    source_id: str
    top_priority: int
    served_seq: int
    ready_tasks: int

    @property
    def never_served(self) -> bool:
        return self.served_seq <= NEVER_SERVED_SEQ


def rotation_supported(conn: psycopg.Connection) -> bool:
    """True when migration 033 is applied.

    The scheduler falls back to plain per-source ranking when it is not, so a
    database that has not been migrated keeps working -- less fairly, but it
    keeps working.
    """

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('evidence_source_rotation') IS NOT NULL")
        row = cur.fetchone()
    return bool(row and row[0])


def _ready_sources_sql(*, task_predicate: str) -> str:
    return f"""
        WITH ready AS (
            SELECT
                COALESCE(
                    NULLIF(et.source_id, ''),
                    NULLIF(et.replay_source_id, ''),
                    ''
                ) AS source_id,
                MAX(et.priority) AS top_priority,
                MIN(
                    COALESCE(
                        et.materialization_next_attempt_at,
                        et.materialization_ready_at
                    )
                ) AS oldest_due_at,
                COUNT(*) AS ready_tasks
            FROM evidence_tasks et
            WHERE et.materialization_status = ANY(%(statuses)s)
              AND {task_predicate}
              AND (
                  %(sources_empty)s
                  OR COALESCE(et.source_id, et.replay_source_id, '')
                     = ANY(%(sources)s)
              )
              AND et.materialization_ready_at IS NOT NULL
              AND et.materialization_ready_at <= now()
              AND COALESCE(
                    et.materialization_next_attempt_at,
                    et.materialization_ready_at
                  ) <= now()
              AND COALESCE(et.materialization_owner, 'rolling') = 'rolling'
              AND NOT (
                  COALESCE(
                      NULLIF(et.source_id, ''),
                      NULLIF(et.replay_source_id, ''),
                      ''
                  ) = ANY(%(excluded_sources)s)
              )
            GROUP BY 1
        )
        SELECT
            ready.source_id,
            ready.top_priority,
            ready.ready_tasks,
            COALESCE(rotation.served_seq, %(never_served)s) AS served_seq
        FROM ready
        LEFT JOIN evidence_source_rotation rotation
               ON rotation.lane = %(lane)s
              AND rotation.source_id = ready.source_id
        ORDER BY
            ready.top_priority DESC,
            served_seq ASC,
            ready.oldest_due_at ASC,
            ready.source_id ASC
        LIMIT %(limit)s
    """


def next_source_turns(
    conn: psycopg.Connection,
    *,
    lane: str,
    statuses: Sequence[str],
    limit: int,
    configured_sources: Sequence[str] = (),
    excluded_sources: Iterable[str] = (),
    task_predicate: str,
) -> list[SourceTurn]:
    """Pick the sources whose turn it is, least recently served first.

    `excluded_sources` are sources the caller already knows it cannot run --
    typically those holding every one of their execution slots. Leaving them in
    would spend the turn on rows that are certain to be rejected.

    Priority still wins outright: a source with high-priority work is served
    before the rotation is consulted, so an urgent event is never made to wait
    its turn behind routine clips.
    """

    if limit <= 0:
        return []
    params = {
        "statuses": list(statuses),
        "sources": list(configured_sources),
        "sources_empty": not bool(configured_sources),
        "excluded_sources": list(excluded_sources),
        "lane": str(lane),
        "never_served": NEVER_SERVED_SEQ,
        "limit": max(1, int(limit)),
    }
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_ready_sources_sql(task_predicate=task_predicate), params)
        rows = cur.fetchall()
    return [
        SourceTurn(
            source_id=str(row["source_id"]),
            top_priority=int(row["top_priority"] or 0),
            served_seq=int(row["served_seq"]),
            ready_tasks=int(row["ready_tasks"] or 0),
        )
        for row in rows
    ]


def mark_served(
    conn: psycopg.Connection,
    *,
    lane: str,
    source_id: str,
) -> None:
    """Advance one source's cursor after a task of that source was claimed.

    Call this only once the claim succeeded. Marking on selection instead would
    let a source that cannot actually run -- no free slot, footage not covered
    yet -- consume its turn and fall to the back of the rotation anyway.

    `nextval` is non-transactional, so concurrent schedulers always get
    distinct, increasing values and cannot collide onto one cursor position.
    """

    key = str(source_id or "").strip()
    if not key:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO evidence_source_rotation (
                lane, source_id, served_seq, served_count, last_served_at, updated_at
            )
            VALUES (
                %(lane)s,
                %(source_id)s,
                nextval('evidence_source_rotation_seq'),
                1,
                now(),
                now()
            )
            ON CONFLICT (lane, source_id) DO UPDATE
            SET served_seq = nextval('evidence_source_rotation_seq'),
                served_count = evidence_source_rotation.served_count + 1,
                last_served_at = now(),
                updated_at = now()
            """,
            {"lane": str(lane), "source_id": key},
        )


def rotation_snapshot(
    conn: psycopg.Connection,
    *,
    lane: str,
    limit: int = 64,
) -> list[dict[str, object]]:
    """Least-recently-served sources first, for operator diagnostics."""

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT source_id, served_seq, served_count, last_served_at
            FROM evidence_source_rotation
            WHERE lane = %(lane)s
            ORDER BY served_seq ASC
            LIMIT %(limit)s
            """,
            {"lane": str(lane), "limit": max(1, int(limit))},
        )
        return [dict(row) for row in cur.fetchall()]
