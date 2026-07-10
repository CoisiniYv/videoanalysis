#!/usr/bin/env python3
"""Check whether midterm evidence queues are drained before shard changes."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from typing import Any

import psycopg
from redis import Redis


DEFAULT_DB_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6396/0"
ACTIVE_MATERIALIZATION_STATES = (
    "manifest_ready",
    "materialization_pending",
    "materializing",
    "finalizing",
)
ACTIVE_TASK_STATES = (
    "pending",
    "waiting_proof",
    "queued",
    "replay_job_created",
    "replaying",
    "materializing",
    "finalizing",
)


def _scalar(conn: psycopg.Connection, sql: str, params: dict[str, Any] | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        row = cur.fetchone()
    if row is None:
        return 0
    value = row[0] if not isinstance(row, dict) else next(iter(row.values()), 0)
    return int(value or 0)


def collect_db_snapshot(conn: psycopg.Connection) -> dict[str, int]:
    active_tasks = _scalar(
        conn,
        """
        SELECT count(*)
        FROM evidence_tasks
        WHERE COALESCE(materialization_status, status, '') = ANY(%(states)s)
        """,
        {"states": list(ACTIVE_MATERIALIZATION_STATES)},
    )
    active_replay_slots = _scalar(
        conn,
        """
        SELECT count(*)
        FROM evidence_tasks
        WHERE replay_slot_status = 'active'
          AND (
            replay_slot_deadline_at IS NULL
            OR replay_slot_deadline_at > now()
          )
        """,
    )
    active_event_media = _scalar(
        conn,
        """
        SELECT count(*)
        FROM events
        WHERE COALESCE(payload #>> '{media,materialization_status}', media_status, '') = ANY(%(states)s)
           OR COALESCE(payload #>> '{media,clip_status}', '') = ANY(%(task_states)s)
        """,
        {
            "states": list(ACTIVE_MATERIALIZATION_STATES),
            "task_states": list(ACTIVE_TASK_STATES),
        },
    )
    return {
        "active_evidence_tasks": active_tasks,
        "active_replay_slots": active_replay_slots,
        "active_event_media": active_event_media,
    }


def _stream_group(redis_client: Redis, stream: str, group: str) -> dict[str, int]:
    try:
        groups = redis_client.xinfo_groups(stream)
    except Exception:
        return {
            "record_request_group_pending": -1,
            "record_request_group_lag": -1,
        }
    for item in groups or []:
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        if name != group:
            continue
        pending = item.get("pending", 0)
        lag = item.get("lag", 0)
        return {
            "record_request_group_pending": int(pending or 0),
            "record_request_group_lag": int(lag or 0),
        }
    return {
        "record_request_group_pending": 0,
        "record_request_group_lag": 0,
    }


def collect_redis_snapshot(
    redis_client: Redis,
    *,
    stream: str,
    group: str,
) -> dict[str, int]:
    try:
        stream_len = int(redis_client.xlen(stream) or 0)
    except Exception:
        stream_len = -1
    return {
        "record_request_stream_len": stream_len,
        **_stream_group(redis_client, stream, group),
    }


def drain_complete(snapshot: dict[str, int]) -> bool:
    blocking_keys = (
        "active_evidence_tasks",
        "active_replay_slots",
        "record_request_group_pending",
        "record_request_group_lag",
    )
    return all(int(snapshot.get(key, 0) or 0) == 0 for key in blocking_keys)


def collect_snapshot(
    *,
    db_url: str,
    redis_url: str,
    stream: str,
    group: str,
) -> dict[str, Any]:
    with psycopg.connect(db_url) as conn:
        db_snapshot = collect_db_snapshot(conn)
    redis_client = Redis.from_url(redis_url, decode_responses=False)
    redis_snapshot = collect_redis_snapshot(redis_client, stream=stream, group=group)
    snapshot: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        **db_snapshot,
        **redis_snapshot,
    }
    snapshot["drain_complete"] = drain_complete(snapshot)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--stream", default="security.record_requests")
    parser.add_argument("--group", default="clip-workers")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--timeout-s", type=float, default=0.0)
    parser.add_argument("--poll-s", type=float, default=5.0)
    args = parser.parse_args(argv)

    deadline = time.monotonic() + max(0.0, args.timeout_s)
    snapshot: dict[str, Any] = {}
    while True:
        snapshot = collect_snapshot(
            db_url=args.db_url,
            redis_url=args.redis_url,
            stream=args.stream,
            group=args.group,
        )
        if snapshot["drain_complete"] or not args.wait or time.monotonic() >= deadline:
            break
        time.sleep(max(0.1, args.poll_s))

    print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if snapshot.get("drain_complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
