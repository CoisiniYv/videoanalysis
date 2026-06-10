#!/usr/bin/env python3
"""Persist a C2.6R watchlist event through the event-worker query path.

The C2.7 proof uses an isolated Redis event stream, the event-worker
``_process_batch`` one-message path, PostgreSQL ``events`` persistence, and
repository/API query verification. It does not run Replay, does not reconstruct
evidence geometry, and does not persist embeddings or image bytes in
``events.payload``.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = ROOT / "scripts" / "tools"
EVENT_WORKER_ROOT = ROOT / "services" / "event-worker"
API_ROOT = ROOT / "services" / "api"
for path in (TOOLS_ROOT, EVENT_WORKER_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import psycopg  # noqa: E402
import redis  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import build_c2_watchlist_evidence_bundle as c25  # noqa: E402
from app.redis_consumer import RedisStreamConsumer  # noqa: E402
from app.repository import EventRepository  # noqa: E402
from app.worker import _process_batch  # noqa: E402


RESULT_PASS = "PASS_C2_7_EVENT_WORKER_PERSISTENCE_API_QUERY_READY"
RESULT_REPOSITORY_ONLY = "PARTIAL_C2_7_REPOSITORY_ONLY_QUERY_READY"
RESULT_ENTRYPOINT_ONLY = "PARTIAL_C2_7_EVENT_WORKER_ENTRYPOINT_ONLY"
RESULT_FAIL = "FAIL_C2_7_EVENT_PERSISTENCE_BLOCKED"
EVENT_TYPE = "watchlist_hit"

PERSISTED_EVENT_FILE = "persisted_watchlist_event.json"
QUERIED_EVENT_FILE = "queried_event.json"
DB_EVENT_ROW_FILE = "db_event_row.json"
REPOSITORY_RESPONSE_FILE = "repository_response.json"
API_RESPONSE_FILE = "api_response.json"
C2_7_SUMMARY_FILE = "c2_7_event_worker_persistence_summary.json"

DEFAULT_DATABASE_URL = "postgresql://video:video@127.0.0.1:5432/video_analytics"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6395/0"
DEFAULT_INPUT_EVENT = Path(
    "/data/video-analytics/media/evidence/"
    "c2_6r_redis_watchlist_20260607T221035/redis_watchlist_event.json"
)
DEFAULT_INPUT_BUNDLE = Path(
    "/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035"
)
DEFAULT_AUDIT_PATH = Path(
    "/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035"
)
DEFAULT_INPUT_STREAM = "c2_7.security.events.test"
DEFAULT_CONSUMER_GROUP = "c2_7_event_worker_test"
DEFAULT_CONSUMER_NAME = "c2_7-one-message"
DEFAULT_CAPTURE_MODE = "stable_post_savant_sink_time_crop"

FORBIDDEN_PAYLOAD_KEYS = c25.FORBIDDEN_EVENT_KEYS | {
    "crop",
    "crop_image",
    "embedding_list",
    "image",
    "image_base64",
}


@dataclass(frozen=True)
class RedisPersistenceProof:
    input_stream: str
    consumer_group: str
    consumer_name: str
    input_message_id: str
    duplicate_message_id: str
    cleanup_requested: bool
    cleanup_status: str
    first_inserted: int
    first_duplicates: int
    duplicate_inserted: int
    duplicate_duplicates: int


@dataclass(frozen=True)
class BuildResult:
    result_marker: str
    output_dir: Path
    persisted_event_path: Path
    queried_event_path: Path
    db_event_row_path: Path
    repository_response_path: Path
    api_response_path: Path | None
    c2_7_summary_path: Path
    event: dict[str, Any]
    db_row: dict[str, Any]
    repository_response: dict[str, Any]
    api_response: dict[str, Any] | None
    summary: dict[str, Any]


class RedisInfraGap(RuntimeError):
    """Raised when isolated Redis event streams are unavailable."""


def build_c2_7_event_worker_persistence(
    *,
    input_event_path: Path,
    output_dir: Path,
    input_bundle: Path,
    audit_path: Path,
    database_url: str,
    redis_url: str,
    input_stream: str,
    consumer_group: str,
    consumer_name: str,
    source_event_id: str | None = None,
    ensure_event_schema: bool = False,
    cleanup_streams: bool = True,
    overwrite: bool = False,
) -> BuildResult:
    input_event_path = input_event_path.resolve(strict=False)
    output_dir = output_dir.resolve(strict=False)
    input_bundle = input_bundle.resolve(strict=False)
    audit_path = audit_path.resolve(strict=False)
    if not input_event_path.is_file():
        raise FileNotFoundError(f"input event missing: {input_event_path}")
    if not input_bundle.is_dir():
        raise FileNotFoundError(f"input bundle missing: {input_bundle}")

    input_event = c25._read_json(input_event_path)
    event = build_persistable_c2_7_event(
        input_event,
        input_bundle=input_bundle,
        audit_path=audit_path,
        output_dir=output_dir,
        source_event_id=source_event_id,
    )
    assert_no_forbidden_persisted_payload(event)

    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}")
        if output_dir.is_dir():
            for child in output_dir.iterdir():
                if child.is_dir():
                    import shutil

                    shutil.rmtree(child)
                else:
                    child.unlink()
        else:
            output_dir.unlink()
            output_dir.mkdir(parents=True, exist_ok=True)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    redis_client = redis.Redis.from_url(redis_url, decode_responses=False)
    try:
        redis_client.ping()
    except Exception as exc:
        raise RedisInfraGap(f"redis_unavailable:{exc}") from exc

    with psycopg.connect(database_url, autocommit=True) as conn:
        schema_alignment = ensure_event_worker_schema(conn) if ensure_event_schema else {
            "schema_alignment_checked": False,
            "schema_alignment_applied": False,
            "schema_alignment_reason": "not_requested",
        }
        repo = EventRepository(conn)
        proof = run_isolated_event_worker_redis_proof(
            redis_client=redis_client,
            repo=repo,
            event=event,
            input_stream=input_stream,
            consumer_group=consumer_group,
            consumer_name=consumer_name,
            cleanup_streams=cleanup_streams,
        )
        db_row = fetch_event_row(conn, str(event["source_event_id"]))
        if db_row is None:
            raise RuntimeError("persisted_event_row_missing")
        row_count = count_events_by_source_event_id(conn, str(event["source_event_id"]))
        evidence_task = fetch_evidence_task(conn, str(event["source_event_id"]))

    repository_response = query_api_repository_subprocess(
        database_url=database_url,
        source_event_id=str(event["source_event_id"]),
    )
    api_response = query_api_testclient_subprocess(
        database_url=database_url,
        source_event_id=str(event["source_event_id"]),
    )

    repository_query_verified = bool(
        repository_response.get("verified") is True
        and c25._dict(repository_response.get("row")).get("source_event_id")
        == event["source_event_id"]
    )
    api_query_verified = bool(
        api_response.get("verified") is True if api_response is not None else False
    )
    summary = build_c2_7_summary(
        event=event,
        db_row=db_row,
        proof=proof,
        schema_alignment=schema_alignment,
        row_count=row_count,
        evidence_task=evidence_task,
        input_event_path=input_event_path,
        input_bundle=input_bundle,
        output_dir=output_dir,
        repository_query_verified=repository_query_verified,
        api_query_verified=api_query_verified,
        api_response=api_response,
    )
    validate_c2_7_output(
        summary=summary,
        event=event,
        db_row=db_row,
        repository_response=repository_response,
        api_response=api_response,
    )
    result_marker = (
        RESULT_PASS
        if summary["api_query_verified"]
        else RESULT_REPOSITORY_ONLY
    )
    summary["result_marker"] = result_marker

    persisted_event_path = output_dir / PERSISTED_EVENT_FILE
    queried_event_path = output_dir / QUERIED_EVENT_FILE
    db_event_row_path = output_dir / DB_EVENT_ROW_FILE
    repository_response_path = output_dir / REPOSITORY_RESPONSE_FILE
    api_response_path = output_dir / API_RESPONSE_FILE
    c2_7_summary_path = output_dir / C2_7_SUMMARY_FILE
    queried_payload = (
        c25._dict(c25._dict(api_response).get("json")).get("data")
        if api_query_verified
        else c25._dict(repository_response.get("row"))
    )

    c25._write_json(persisted_event_path, event)
    c25._write_json(queried_event_path, queried_payload)
    c25._write_json(db_event_row_path, db_row)
    c25._write_json(repository_response_path, repository_response)
    if api_response is not None:
        c25._write_json(api_response_path, api_response)
    c25._write_json(c2_7_summary_path, summary)

    return BuildResult(
        result_marker=result_marker,
        output_dir=output_dir,
        persisted_event_path=persisted_event_path,
        queried_event_path=queried_event_path,
        db_event_row_path=db_event_row_path,
        repository_response_path=repository_response_path,
        api_response_path=api_response_path if api_response is not None else None,
        c2_7_summary_path=c2_7_summary_path,
        event=event,
        db_row=db_row,
        repository_response=repository_response,
        api_response=api_response,
        summary=summary,
    )


def build_persistable_c2_7_event(
    event: dict[str, Any],
    *,
    input_bundle: Path,
    audit_path: Path,
    output_dir: Path,
    source_event_id: str | None,
) -> dict[str, Any]:
    patched = copy.deepcopy(event)
    source_observation_id = str(patched.get("source_observation_id") or "")
    person_id = int(patched.get("person_id") or 0)
    if not source_event_id:
        source_event_id = (
            f"c2_7:persisted:watchlist_hit:{source_observation_id}:"
            f"{person_id}:{output_dir.name}"
        )
    evidence = {
        "bundle_path": str(input_bundle),
        "audit_path": str(audit_path),
        "capture_mode": DEFAULT_CAPTURE_MODE,
        "workaround_used": True,
        "event_style_replay_job_passed": False,
    }
    patched.update(
        {
            "schema_version": "1.0",
            "event_type": EVENT_TYPE,
            "source_event_id": source_event_id,
            "evidence": evidence,
            "severity": patched.get("severity") or "high",
        }
    )
    payload = sanitize_persisted_payload(c25._dict(patched.get("payload")))
    payload.update(
        {
            "source_observation_id": source_observation_id,
            "person_id": person_id,
            "external_person_id": patched.get("external_person_id"),
            "gallery_embedding_id": patched.get("gallery_embedding_id"),
            "match_result_id": patched.get("match_result_id"),
            "similarity": patched.get("similarity"),
            "threshold": patched.get("threshold"),
            "watchlist_rule_id": patched.get("watchlist_rule_id"),
            "identity_source": "face_worker_pgvector_match",
            "watchlist_match_source": "face_worker_consumer",
            "embedding_included": False,
            "image_bytes_included": False,
            "crop_bytes_included": False,
            "primary_identity_join_key": "source_observation_id",
            "track_id_join_warning": True,
            "evidence": evidence,
            "evidence_bundle_path": str(input_bundle),
            "evidence_capture_mode": DEFAULT_CAPTURE_MODE,
            "workaround_used": True,
            "event_style_replay_job_passed": False,
            "fallback_used": False,
            "legacy_used_for_visual_binding": False,
            "allow_db_annotation_fallback": False,
            "allow_legacy_annotation_fallback": False,
            "event_worker_persistence_phase": "C2.7",
        }
    )
    media = c25._dict(payload.get("media"))
    media.update(
        {
            "evidence_bundle_path": str(input_bundle),
            "evidence_audit_path": str(audit_path),
            "evidence_capture_mode": DEFAULT_CAPTURE_MODE,
            "workaround_used": True,
            "event_style_replay_job_passed": False,
        }
    )
    payload["media"] = media
    patched["payload"] = sanitize_persisted_payload(payload)
    return patched


def run_isolated_event_worker_redis_proof(
    *,
    redis_client: redis.Redis,
    repo: EventRepository,
    event: dict[str, Any],
    input_stream: str,
    consumer_group: str,
    consumer_name: str,
    cleanup_streams: bool,
) -> RedisPersistenceProof:
    redis_client.delete(input_stream)
    consumer = RedisStreamConsumer(
        redis_client,
        input_stream,
        consumer_group,
        consumer_name,
        start_id="0",
    )
    consumer.ensure_group()
    input_message_id = _decode_redis_id(redis_client.xadd(input_stream, _stream_fields(event)))
    messages = consumer.read_new(count=1, block_ms=1000)
    if len(messages) != 1:
        raise RuntimeError(f"event_worker_input_read_count={len(messages)}")
    first_inserted, first_duplicates = _process_batch(messages, repo, consumer)
    if first_inserted != 1:
        raise RuntimeError(f"event_worker_first_insert_count={first_inserted}")

    duplicate_message_id = _decode_redis_id(redis_client.xadd(input_stream, _stream_fields(event)))
    duplicate_messages = consumer.read_new(count=1, block_ms=1000)
    if len(duplicate_messages) != 1:
        raise RuntimeError(f"event_worker_duplicate_read_count={len(duplicate_messages)}")
    duplicate_inserted, duplicate_duplicates = _process_batch(
        duplicate_messages,
        repo,
        consumer,
    )
    if duplicate_inserted != 0 or duplicate_duplicates != 1:
        raise RuntimeError(
            "event_worker_duplicate_outcome_invalid:"
            f"inserted={duplicate_inserted},duplicates={duplicate_duplicates}"
        )

    cleanup_status = "not_requested"
    if cleanup_streams:
        deleted = redis_client.delete(input_stream)
        cleanup_status = f"deleted_{deleted}_streams"

    return RedisPersistenceProof(
        input_stream=input_stream,
        consumer_group=consumer_group,
        consumer_name=consumer_name,
        input_message_id=input_message_id,
        duplicate_message_id=duplicate_message_id,
        cleanup_requested=cleanup_streams,
        cleanup_status=cleanup_status,
        first_inserted=first_inserted,
        first_duplicates=first_duplicates,
        duplicate_inserted=duplicate_inserted,
        duplicate_duplicates=duplicate_duplicates,
    )


def ensure_event_worker_schema(conn: psycopg.Connection) -> dict[str, Any]:
    """Align an empty legacy events table to the current event-worker contract.

    The C2.7 runtime DB can still contain the earliest Phase 0 ``events`` table
    shape. This helper refuses to alter a non-empty events table and only uses
    additive/type-alignment DDL needed by ``EventRepository.insert_event()`` and
    API event queries.
    """

    required_columns = {
        "source_event_id",
        "event_type",
        "camera_id",
        "source_id",
        "track_id",
        "person_id",
        "severity",
        "confidence",
        "start_ts_ms",
        "end_ts_ms",
        "start_ts",
        "end_ts",
        "event_ts_ms",
        "frame_uuid",
        "keyframe_uuid",
        "snapshot_path",
        "clip_path",
        "snapshot_required",
        "clip_required",
        "evidence_policy",
        "recording_strategy",
        "media_status",
        "status",
        "payload",
        "created_at",
        "updated_at",
        "algorithm_type",
        "algorithm_version",
    }
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT to_regclass('public.events') AS table_name")
        has_events = cur.fetchone()["table_name"] is not None
        if not has_events:
            raise RuntimeError("events_table_missing")
        cur.execute("SELECT COUNT(*) AS count FROM events")
        event_count = int(cur.fetchone()["count"])
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'events'
            """
        )
        columns = {row["column_name"]: row["data_type"] for row in cur.fetchall()}
        cur.execute("SELECT to_regclass('public.evidence_tasks') AS table_name")
        has_evidence_tasks = cur.fetchone()["table_name"] is not None

    aligned = (
        required_columns.issubset(columns)
        and columns.get("camera_id") == "text"
        and columns.get("person_id") == "integer"
        and has_evidence_tasks
    )
    if aligned:
        return {
            "schema_alignment_checked": True,
            "schema_alignment_applied": False,
            "schema_alignment_reason": "already_aligned",
            "events_row_count_before_alignment": event_count,
        }
    if event_count != 0:
        raise RuntimeError(
            "event_schema_alignment_requires_empty_events_table:"
            f"rows={event_count}"
        )

    ddl_statements = [
        "ALTER TABLE events DROP CONSTRAINT IF EXISTS events_camera_id_fkey",
        "ALTER TABLE events DROP CONSTRAINT IF EXISTS events_person_id_fkey",
        "ALTER TABLE events ALTER COLUMN camera_id TYPE TEXT USING camera_id::text",
        "ALTER TABLE events ALTER COLUMN camera_id SET DEFAULT ''",
        "UPDATE events SET camera_id = '' WHERE camera_id IS NULL",
        "ALTER TABLE events ALTER COLUMN camera_id SET NOT NULL",
        "ALTER TABLE events ALTER COLUMN person_id TYPE INTEGER USING NULL",
        "ALTER TABLE events ALTER COLUMN source_event_id SET DEFAULT ''",
        "UPDATE events SET source_event_id = '' WHERE source_event_id IS NULL",
        "ALTER TABLE events ALTER COLUMN source_event_id SET NOT NULL",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS source_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS track_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS severity TEXT NOT NULL DEFAULT 'medium'",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS start_ts TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS end_ts TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS event_ts_ms BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS frame_uuid TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS keyframe_uuid TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS recording_strategy TEXT NOT NULL DEFAULT 'reserved'",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS media_status TEXT NOT NULL DEFAULT 'not_implemented'",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'new'",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS algorithm_type TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS algorithm_version TEXT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS start_ts_ms BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS end_ts_ms BIGINT",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS snapshot_required BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS clip_required BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS evidence_policy JSONB NOT NULL DEFAULT '{}'::jsonb",
        "CREATE UNIQUE INDEX IF NOT EXISTS events_source_event_id_unique ON events(source_event_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_events_camera_id_phase2e ON events(camera_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_start_ts ON events(start_ts)",
        "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status)",
        "CREATE INDEX IF NOT EXISTS idx_events_algorithm_type ON events(algorithm_type)",
        "CREATE INDEX IF NOT EXISTS idx_events_start_ts_ms ON events(start_ts_ms)",
        """
        CREATE TABLE IF NOT EXISTS evidence_tasks (
            task_id TEXT PRIMARY KEY,
            event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            source_event_id TEXT NOT NULL,
            camera_id TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT '',
            event_ts_ms BIGINT NOT NULL DEFAULT 0,
            task_type TEXT NOT NULL DEFAULT 'snapshot_clip',
            snapshot_required BOOLEAN NOT NULL DEFAULT FALSE,
            clip_required BOOLEAN NOT NULL DEFAULT FALSE,
            pre_seconds INTEGER NOT NULL DEFAULT 5,
            post_seconds INTEGER NOT NULL DEFAULT 10,
            status TEXT NOT NULL DEFAULT 'pending',
            snapshot_path TEXT,
            clip_path TEXT,
            metadata_path TEXT,
            output_root TEXT,
            storage_fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
            storage_fallback_reason TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 3,
            claimed_by TEXT,
            claimed_at TIMESTAMPTZ,
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT evidence_tasks_source_event_type_unique
                UNIQUE (source_event_id, task_type)
        )
        """,
        "CREATE INDEX IF NOT EXISTS evidence_tasks_event_id_idx ON evidence_tasks(event_id)",
        "CREATE INDEX IF NOT EXISTS evidence_tasks_source_event_id_idx ON evidence_tasks(source_event_id)",
        "CREATE INDEX IF NOT EXISTS evidence_tasks_status_idx ON evidence_tasks(status)",
        "CREATE INDEX IF NOT EXISTS evidence_tasks_claimed_at_idx ON evidence_tasks(claimed_at)",
        "CREATE INDEX IF NOT EXISTS evidence_tasks_event_status_idx ON evidence_tasks(event_id, status)",
    ]
    with conn.cursor() as cur:
        for statement in ddl_statements:
            cur.execute(statement)
    return {
        "schema_alignment_checked": True,
        "schema_alignment_applied": True,
        "schema_alignment_reason": "empty_legacy_events_table_aligned_for_c2_7",
        "events_row_count_before_alignment": event_count,
    }


def fetch_event_row(conn: psycopg.Connection, source_event_id: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id::text AS id, source_event_id, event_type, camera_id,
                   source_id, track_id, person_id, algorithm_type,
                   algorithm_version, severity, confidence, start_ts_ms,
                   end_ts_ms, start_ts::text AS start_ts, end_ts::text AS end_ts,
                   event_ts_ms, frame_uuid, keyframe_uuid, snapshot_path,
                   clip_path, snapshot_required, clip_required, evidence_policy,
                   recording_strategy, media_status, status, payload,
                   created_at::text AS created_at, updated_at::text AS updated_at
            FROM events
            WHERE source_event_id = %s
            """,
            (source_event_id,),
        )
        row = cur.fetchone()
    return _jsonable(row) if row else None


def count_events_by_source_event_id(conn: psycopg.Connection, source_event_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM events WHERE source_event_id = %s", (source_event_id,))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def fetch_evidence_task(conn: psycopg.Connection, source_event_id: str) -> dict[str, Any] | None:
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT task_id, event_id::text AS event_id, source_event_id,
                       event_type, status, created_at::text AS created_at,
                       updated_at::text AS updated_at
                FROM evidence_tasks
                WHERE source_event_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (source_event_id,),
            )
            row = cur.fetchone()
    except Exception:
        return None
    return _jsonable(row) if row else None


def query_api_repository_subprocess(*, database_url: str, source_event_id: str) -> dict[str, Any]:
    code = r"""
import json
import os
import sys
from pathlib import Path

api_root, database_url, source_event_id = sys.argv[1:4]
sys.path.insert(0, api_root)
os.environ["DATABASE_URL"] = database_url

import psycopg
from psycopg.rows import dict_row
from app.repositories.events import EventRepository

def encode(value):
    import datetime
    import decimal
    import uuid
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(type(value).__name__)

with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
    row = EventRepository(conn).get_by_source_event_id(source_event_id)
payload = {"verified": row is not None, "row": row}
print(json.dumps(payload, default=encode, sort_keys=True))
"""
    return _run_json_subprocess(code, database_url, source_event_id)


def query_api_testclient_subprocess(*, database_url: str, source_event_id: str) -> dict[str, Any]:
    code = r"""
import json
import os
import sys
from urllib.parse import quote

api_root, database_url, source_event_id = sys.argv[1:4]
sys.path.insert(0, api_root)
os.environ["DATABASE_URL"] = database_url

from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    response = client.get("/api/v1/events/" + quote(source_event_id, safe=":"))
body = response.json()
payload = {
    "verified": response.status_code == 200
        and isinstance(body, dict)
        and isinstance(body.get("data"), dict)
        and body["data"].get("source_event_id") == source_event_id,
    "status_code": response.status_code,
    "json": body,
}
print(json.dumps(payload, sort_keys=True))
"""
    return _run_json_subprocess(code, database_url, source_event_id)


def _run_json_subprocess(code: str, database_url: str, source_event_id: str) -> dict[str, Any]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    proc = subprocess.run(
        [sys.executable, "-c", code, str(API_ROOT), database_url, source_event_id],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "verified": False,
            "returncode": proc.returncode,
            "stderr": proc.stderr,
            "stdout": proc.stdout,
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "verified": False,
            "returncode": proc.returncode,
            "stderr": proc.stderr,
            "stdout": proc.stdout,
            "error": f"json_decode:{exc}",
        }
    return payload if isinstance(payload, dict) else {"verified": False, "payload": payload}


def build_c2_7_summary(
    *,
    event: dict[str, Any],
    db_row: dict[str, Any],
    proof: RedisPersistenceProof,
    schema_alignment: dict[str, Any],
    row_count: int,
    evidence_task: dict[str, Any] | None,
    input_event_path: Path,
    input_bundle: Path,
    output_dir: Path,
    repository_query_verified: bool,
    api_query_verified: bool,
    api_response: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = c25._dict(event.get("payload"))
    db_payload = c25._dict(db_row.get("payload"))
    evidence = c25._dict(payload.get("evidence"))
    return {
        "result_marker": RESULT_PASS if api_query_verified else RESULT_REPOSITORY_ONLY,
        "input_c2_6r_event": str(input_event_path),
        "input_evidence_bundle": str(input_bundle),
        "output_dir": str(output_dir),
        "execution_mode": "Redis stream event-worker one-message consumer",
        "schema_alignment": schema_alignment,
        "redis_event_stream_verified": True,
        "event_worker_one_message_verified": True,
        "event_worker_persistence_verified": True,
        "repository_query_verified": repository_query_verified,
        "api_query_verified": api_query_verified,
        "live_api_runtime_verified": False,
        "source_event_id": event.get("source_event_id"),
        "event_id": db_row.get("id"),
        "db_event_id": db_row.get("id"),
        "event_type": event.get("event_type"),
        "event_status": db_row.get("status"),
        "camera_id": event.get("camera_id"),
        "source_id": event.get("source_id"),
        "track_id": event.get("track_id"),
        "source_observation_id": event.get("source_observation_id"),
        "person_id": event.get("person_id"),
        "external_person_id": event.get("external_person_id"),
        "gallery_embedding_id": event.get("gallery_embedding_id"),
        "match_result_id": event.get("match_result_id"),
        "similarity": event.get("similarity"),
        "threshold": event.get("threshold"),
        "watchlist_rule_id": event.get("watchlist_rule_id"),
        "created_at": db_row.get("created_at"),
        "evidence_task": evidence_task,
        "evidence_bundle_path": evidence.get("bundle_path"),
        "evidence_audit_path": evidence.get("audit_path"),
        "payload_has_embedding": bool(list(_find_forbidden_keys(db_payload, only={"embedding", "embeddings", "embedding_vector", "embedding_values", "embedding_list"}))),
        "payload_has_image_bytes": bool(list(_find_forbidden_keys(db_payload, only={"image_bytes", "crop_bytes", "frame_bytes", "raw_frame", "base64", "base64_image", "image_base64"}))),
        "payload_has_forbidden_keys": bool(list(_find_forbidden_keys(db_payload))),
        "evidence_capture_mode": payload.get("evidence_capture_mode"),
        "workaround_used": payload.get("workaround_used"),
        "event_style_replay_job_passed": payload.get("event_style_replay_job_passed"),
        "track_id_join_warning": payload.get("track_id_join_warning"),
        "primary_identity_join_key": payload.get("primary_identity_join_key"),
        "fallback_used": payload.get("fallback_used"),
        "legacy_used_for_visual_binding": payload.get("legacy_used_for_visual_binding"),
        "allow_db_annotation_fallback": payload.get("allow_db_annotation_fallback"),
        "allow_legacy_annotation_fallback": payload.get("allow_legacy_annotation_fallback"),
        "redis_input_stream": proof.input_stream,
        "redis_consumer_group": proof.consumer_group,
        "redis_consumer_name": proof.consumer_name,
        "redis_input_message_id": proof.input_message_id,
        "redis_duplicate_message_id": proof.duplicate_message_id,
        "redis_cleanup_requested": proof.cleanup_requested,
        "redis_cleanup_status": proof.cleanup_status,
        "duplicate_replay_attempted": True,
        "duplicate_count_after_replay": row_count,
        "idempotency_verified": row_count == 1 and proof.duplicate_duplicates == 1,
        "first_inserted": proof.first_inserted,
        "first_duplicates": proof.first_duplicates,
        "duplicate_inserted": proof.duplicate_inserted,
        "duplicate_duplicates": proof.duplicate_duplicates,
        "api_status_code": c25._dict(api_response).get("status_code") if api_response else None,
        "persisted_watchlist_event_json": str(output_dir / PERSISTED_EVENT_FILE),
        "queried_event_json": str(output_dir / QUERIED_EVENT_FILE),
        "db_event_row_json": str(output_dir / DB_EVENT_ROW_FILE),
        "repository_response_json": str(output_dir / REPOSITORY_RESPONSE_FILE),
        "api_response_json": str(output_dir / API_RESPONSE_FILE) if api_response else "",
        "c2_7_event_worker_persistence_summary_json": str(output_dir / C2_7_SUMMARY_FILE),
        "limitations": [
            "stable_sink_workaround_still_active",
            "event_style_replay_not_passed",
            "not_broad_recognition_accuracy_test",
            "isolated_event_worker_one_message_consumer_not_infinite_loop",
            "live_api_runtime_not_verified",
        ],
    }


def validate_c2_7_output(
    *,
    summary: dict[str, Any],
    event: dict[str, Any],
    db_row: dict[str, Any],
    repository_response: dict[str, Any],
    api_response: dict[str, Any] | None,
) -> None:
    assert_no_forbidden_persisted_payload(event)
    assert_no_forbidden_persisted_payload({"payload": db_row.get("payload") or {}})
    if event.get("event_type") != EVENT_TYPE:
        raise RuntimeError("event_type_not_watchlist_hit")
    for key in ("source_event_id", "source_observation_id", "person_id", "gallery_embedding_id", "match_result_id", "watchlist_rule_id"):
        if event.get(key) in (None, ""):
            raise RuntimeError(f"event_missing_{key}")
    if c25._dict(event.get("payload")).get("primary_identity_join_key") != "source_observation_id":
        raise RuntimeError("primary_identity_join_key_not_source_observation_id")
    if c25._dict(event.get("payload")).get("track_id_join_warning") is not True:
        raise RuntimeError("track_id_join_warning_required")
    if c25._dict(event.get("payload")).get("event_style_replay_job_passed") is not False:
        raise RuntimeError("event_style_replay_job_passed_not_false")
    evidence = c25._dict(c25._dict(event.get("payload")).get("evidence"))
    if not evidence.get("bundle_path"):
        raise RuntimeError("evidence_bundle_path_missing")
    if db_row.get("source_event_id") != event.get("source_event_id"):
        raise RuntimeError("db_row_source_event_id_mismatch")
    if db_row.get("event_type") != EVENT_TYPE:
        raise RuntimeError("db_row_event_type_invalid")
    if c25._dict(db_row.get("payload")).get("evidence_capture_mode") != DEFAULT_CAPTURE_MODE:
        raise RuntimeError("db_payload_capture_mode_missing")
    if summary.get("event_worker_persistence_verified") is not True:
        raise RuntimeError("event_worker_persistence_not_verified")
    if summary.get("redis_event_stream_verified") is not True:
        raise RuntimeError("redis_event_stream_not_verified")
    if summary.get("repository_query_verified") is not True:
        raise RuntimeError("repository_query_not_verified")
    if summary.get("idempotency_verified") is not True:
        raise RuntimeError("idempotency_not_verified")
    if int(summary.get("duplicate_count_after_replay") or 0) != 1:
        raise RuntimeError("duplicate_row_count_not_1")
    if summary.get("payload_has_embedding") is not False:
        raise RuntimeError("payload_has_embedding")
    if summary.get("payload_has_image_bytes") is not False:
        raise RuntimeError("payload_has_image_bytes")
    if summary.get("evidence_capture_mode") != DEFAULT_CAPTURE_MODE:
        raise RuntimeError("summary_capture_mode_invalid")
    if summary.get("workaround_used") is not True:
        raise RuntimeError("summary_workaround_not_true")
    if summary.get("event_style_replay_job_passed") is not False:
        raise RuntimeError("summary_replay_status_invalid")
    if summary.get("fallback_used") is not False:
        raise RuntimeError("summary_fallback_used")
    if summary.get("legacy_used_for_visual_binding") is not False:
        raise RuntimeError("summary_legacy_used")
    repo_row = c25._dict(repository_response.get("row"))
    if repo_row.get("source_event_id") != event.get("source_event_id"):
        raise RuntimeError("repository_response_source_event_id_mismatch")
    if api_response is not None and api_response.get("verified") is True:
        api_data = c25._dict(c25._dict(api_response.get("json")).get("data"))
        if api_data.get("source_event_id") != event.get("source_event_id"):
            raise RuntimeError("api_response_source_event_id_mismatch")


def assert_no_forbidden_persisted_payload(event: dict[str, Any]) -> None:
    payload = c25._dict(event.get("payload"))
    hits = sorted(set(_find_forbidden_keys(payload)))
    if hits:
        raise RuntimeError(f"persisted_event_payload_contains_forbidden_keys:{','.join(hits)}")


def sanitize_persisted_payload(value: Any) -> Any:
    """Remove fields that must never be persisted in ``events.payload``."""

    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_PAYLOAD_KEYS:
                continue
            sanitized[str(key)] = sanitize_persisted_payload(nested)
        return sanitized
    if isinstance(value, list):
        return [sanitize_persisted_payload(item) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if ";base64," in lowered or lowered.startswith("data:image"):
            return ""
    return value


def _find_forbidden_keys(
    value: Any,
    *,
    parent_key: str = "",
    only: set[str] | None = None,
) -> Iterable[str]:
    forbidden = only or FORBIDDEN_PAYLOAD_KEYS
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            if lowered in forbidden:
                yield parent_key + str(key)
            yield from _find_forbidden_keys(
                nested,
                parent_key=parent_key + str(key) + ".",
                only=only,
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _find_forbidden_keys(
                nested,
                parent_key=f"{parent_key}{index}.",
                only=only,
            )
    elif isinstance(value, str):
        lowered = value.lower()
        if ";base64," in lowered or lowered.startswith("data:image"):
            yield parent_key.rstrip(".") or "value"


def _stream_fields(event: dict[str, Any]) -> dict[str, str]:
    return {
        "type": "security_event",
        "source_event_id": str(event["source_event_id"]),
        "event_type": str(event["event_type"]),
        "camera_id": str(event.get("camera_id") or ""),
        "track_id": str(event.get("track_id") or ""),
        "severity": str(event.get("severity") or "medium"),
        "data": json.dumps(event, ensure_ascii=False),
    }


def _decode_redis_id(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _jsonable(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    try:
        import uuid

        if isinstance(value, uuid.UUID):
            return str(value)
    except Exception:
        pass
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _result_payload(result: BuildResult) -> dict[str, Any]:
    summary = result.summary
    return {
        "result_marker": result.result_marker,
        "output_dir": str(result.output_dir),
        "persisted_watchlist_event_json": str(result.persisted_event_path),
        "queried_event_json": str(result.queried_event_path),
        "db_event_row_json": str(result.db_event_row_path),
        "repository_response_json": str(result.repository_response_path),
        "api_response_json": str(result.api_response_path) if result.api_response_path else "",
        "c2_7_event_worker_persistence_summary_json": str(result.c2_7_summary_path),
        "source_event_id": summary.get("source_event_id"),
        "event_id": summary.get("event_id"),
        "event_type": summary.get("event_type"),
        "repository_query_verified": summary.get("repository_query_verified"),
        "api_query_verified": summary.get("api_query_verified"),
        "idempotency_verified": summary.get("idempotency_verified"),
        "duplicate_count_after_replay": summary.get("duplicate_count_after_replay"),
        "redis_input_stream": summary.get("redis_input_stream"),
        "redis_input_message_id": summary.get("redis_input_message_id"),
        "redis_duplicate_message_id": summary.get("redis_duplicate_message_id"),
        "redis_cleanup_status": summary.get("redis_cleanup_status"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-event", type=Path, default=DEFAULT_INPUT_EVENT)
    parser.add_argument("--input-bundle", type=Path, default=DEFAULT_INPUT_BUNDLE)
    parser.add_argument("--audit-path", type=Path, default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--input-stream", default=DEFAULT_INPUT_STREAM)
    parser.add_argument("--consumer-group", default=DEFAULT_CONSUMER_GROUP)
    parser.add_argument("--consumer-name", default=DEFAULT_CONSUMER_NAME)
    parser.add_argument("--source-event-id")
    parser.add_argument("--ensure-event-schema", action="store_true", default=False)
    parser.add_argument("--no-cleanup-streams", action="store_true", default=False)
    parser.add_argument("--overwrite", action="store_true", default=False)
    args = parser.parse_args(argv)

    try:
        result = build_c2_7_event_worker_persistence(
            input_event_path=args.input_event,
            output_dir=args.output_dir,
            input_bundle=args.input_bundle,
            audit_path=args.audit_path,
            database_url=args.database_url,
            redis_url=args.redis_url,
            input_stream=args.input_stream,
            consumer_group=args.consumer_group,
            consumer_name=args.consumer_name,
            source_event_id=args.source_event_id,
            ensure_event_schema=args.ensure_event_schema,
            cleanup_streams=not args.no_cleanup_streams,
            overwrite=args.overwrite,
        )
    except RedisInfraGap as exc:
        payload = {
            "result_marker": RESULT_ENTRYPOINT_ONLY,
            "reason": str(exc),
            "output_dir": str(args.output_dir),
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 3
    except Exception as exc:
        payload = {
            "result_marker": RESULT_FAIL,
            "reason": f"{type(exc).__name__}:{exc}",
            "output_dir": str(args.output_dir),
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    print(json.dumps(_result_payload(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
