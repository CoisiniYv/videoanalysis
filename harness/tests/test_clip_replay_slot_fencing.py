"""Spec 34 Phase 3 fenced Replay slot and durable handoff contracts."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any
import uuid

import psycopg
from psycopg.rows import dict_row
import pytest


ROOT = Path(__file__).resolve().parents[2]
CLIP_ROOT = ROOT / "services" / "clip-worker"
MIGRATION = ROOT / "db/migrations/031_replay_slot_fencing.sql"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _activate():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    path = str(CLIP_ROOT)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    from app import repository
    from app import replay_admission_repository

    return repository, replay_admission_repository


class _Cursor:
    def __init__(self, rows: list[Any] | None = None, *, rowcount: int = 1) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = list(rows or [])
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.calls.append((sql, params or {}))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _Connection:
    def __init__(self, rows: list[Any] | None = None, *, rowcount: int = 1) -> None:
        self.cursor_obj = _Cursor(rows, rowcount=rowcount)

    def cursor(self):
        return self.cursor_obj


def _acquired_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "global_count": 0,
        "shard_count": 0,
        "source_count": 0,
        "deny_reason": "",
        "acquired": True,
        "event_updated": True,
        "slot_owner": "clip-a",
        "slot_token": "slot-token-1",
        "slot_generation": 1,
        "create_state": "reserved",
        "replay_job_id": "",
        "resulting_stream_id": "",
        "plan_hash": "plan-1",
    }
    row.update(overrides)
    return row


def test_migration_031_adds_owner_token_generation_and_recovery_indexes() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for column in (
        "replay_slot_owner",
        "replay_slot_token",
        "replay_slot_generation",
        "replay_create_state",
        "replay_create_started_at",
        "replay_create_committed_at",
        "replay_plan_hash",
        "replay_request_id",
        "replay_delivery_id",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in source
    assert "evidence_tasks_replay_slot_fence_idx" in source
    assert "evidence_tasks_replay_create_recovery_idx" in source
    assert "WHERE replay_slot_status = 'active'" in source


def test_fenced_acquire_binds_owner_token_generation_and_plan() -> None:
    repository, _adapter = _activate()
    connection = _Connection(rows=[_acquired_row()])

    result = repository.try_acquire_fenced_replay_slot(
        connection,
        event_id="00000000-0000-4000-8000-000000000301",
        owner="clip-a",
        slot_token="slot-token-1",
        request_id="request-1",
        delivery_id="1-0",
        plan_hash="plan-1",
        source_id="source-1",
        camera_id="camera-1",
        replay_shard={"shard_id": "replay-a"},
        sink_instance="video-file-sink",
        replay_duration_seconds_effective=10.0,
        replay_duration_effective_reason="window",
        timeout_budget_s=120.0,
        max_global=8,
        max_per_shard=4,
        max_per_source=1,
    )

    assert result and result["acquired"] is True
    assert result["slot_token"] == "slot-token-1"
    assert result["slot_generation"] == 1
    sql, params = connection.cursor_obj.calls[0]
    assert "pg_advisory_xact_lock" in sql
    assert "replay_slot_owner = %(owner)s" in sql
    assert "replay_slot_token = %(slot_token)s" in sql
    assert "replay_slot_generation + 1" in sql
    assert "replay_plan_hash = %(plan_hash)s" in sql
    assert params["owner"] == "clip-a"
    assert params["delivery_id"] == "1-0"


def test_takeover_increments_generation_with_expected_fence() -> None:
    repository, _adapter = _activate()
    connection = _Connection(
        rows=[
            {
                "claimed": True,
                "event_updated": True,
                "slot_owner": "clip-b",
                "slot_token": "slot-token-1",
                "slot_generation": 2,
                "create_state": "submitting",
                "replay_job_id": "",
                "resulting_stream_id": "",
                "plan_hash": "plan-1",
            }
        ]
    )

    result = repository.takeover_fenced_replay_slot(
        connection,
        event_id="00000000-0000-4000-8000-000000000302",
        owner="clip-b",
        expected_token="slot-token-1",
        expected_generation=1,
        delivery_id="1-0",
    )

    assert result and result["claimed"] is True
    assert result["slot_generation"] == 2
    sql, params = connection.cursor_obj.calls[0]
    assert "replay_slot_generation = replay_slot_generation + 1" in sql
    assert "replay_slot_token = %(expected_token)s" in sql
    assert "replay_slot_generation = %(expected_generation)s" in sql
    assert "replay_job_id IS NULL" in sql
    assert params["expected_generation"] == 1


def test_replay_create_and_handoff_writes_are_fenced_and_atomic() -> None:
    repository, _adapter = _activate()
    started = _Connection(rowcount=1)
    assert repository.mark_replay_create_started(
        started,
        event_id="00000000-0000-4000-8000-000000000303",
        owner="clip-a",
        slot_token="slot-token-1",
        slot_generation=1,
        plan_hash="plan-1",
        replay_job_request={
            "configuration": {
                "resulting_stream_id": "replay-event-303",
                "labels": {"replay_slot_token": "slot-token-1"},
            }
        },
    )
    started_sql = started.cursor_obj.calls[0][0]
    assert "replay_create_state = 'submitting'" in started_sql
    assert "replay_slot_owner = %(owner)s" in started_sql
    assert "replay_slot_token = %(slot_token)s" in started_sql
    assert "replay_slot_generation = %(slot_generation)s" in started_sql
    assert "'planned_request'" in started_sql
    assert "'replay_job_request'" in started_sql

    committed = _Connection(
        rows=[{"task_updated": True, "event_updated": True}]
    )
    assert repository.commit_replay_job_handoff(
        committed,
        event_id="00000000-0000-4000-8000-000000000303",
        owner="clip-a",
        slot_token="slot-token-1",
        slot_generation=1,
        plan_hash="plan-1",
        replay_job_id="job-1",
        resulting_stream_id="replay-event-303",
        replay_job_request={"configuration": {"labels": {"event_id": "x"}}},
        diagnostics={"replay_job_create_ms": 4},
        replay_shard={"shard_id": "replay-a"},
    )
    sql, params = committed.cursor_obj.calls[0]
    assert "WITH target_event AS" in sql
    assert "updated_task AS" in sql
    assert "updated_event AS" in sql
    assert "replay_create_state = 'committed'" in sql
    assert "replay_slot_owner = %(owner)s" in sql
    assert "replay_slot_token = %(slot_token)s" in sql
    assert "replay_slot_generation = %(slot_generation)s" in sql
    assert "replay_plan_hash = %(plan_hash)s" in sql
    assert params["replay_job_id"] == "job-1"


def test_permanent_replay_rejection_aborts_task_event_and_slot_atomically() -> None:
    repository, _adapter = _activate()
    connection = _Connection(
        rows=[{"task_updated": True, "event_updated": True}]
    )

    assert repository.abort_fenced_replay_create(
        connection,
        event_id="00000000-0000-4000-8000-000000000304",
        owner="clip-a",
        slot_token="slot-token-1",
        slot_generation=1,
        plan_hash="plan-1",
        reason="Replay HTTP status 400",
        diagnostics={"replay_submission_code": "permanent_rejected"},
    )

    sql, params = connection.cursor_obj.calls[0]
    assert "WITH aborted_task AS" in sql
    assert "aborted_event AS" in sql
    assert "materialization_status = 'materialization_failed'" in sql
    assert "replay_create_state = 'aborted'" in sql
    assert "replay_slot_status = 'released'" in sql
    assert "et.replay_slot_owner = %(owner)s" in sql
    assert "et.replay_slot_token = %(slot_token)s" in sql
    assert "et.replay_slot_generation = %(slot_generation)s" in sql
    assert "et.replay_plan_hash = %(plan_hash)s" in sql
    assert params["reason_code"] == "replay_permanent_rejected"


def test_named_adapter_never_drops_the_fence(monkeypatch) -> None:
    _repository, adapter_module = _activate()
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        adapter_module.repository,
        "try_acquire_fenced_replay_slot",
        lambda _conn, **kwargs: calls.append(("acquire", kwargs))
        or _acquired_row(),
    )
    monkeypatch.setattr(
        adapter_module.repository,
        "mark_replay_create_started",
        lambda _conn, **kwargs: calls.append(("start", kwargs)) or True,
    )
    monkeypatch.setattr(
        adapter_module.repository,
        "commit_replay_job_handoff",
        lambda _conn, **kwargs: calls.append(("commit", kwargs)) or True,
    )
    admission = adapter_module.ReplayAdmissionRepository(object())
    reservation = admission.acquire_fenced(
        event_id="event-1",
        owner="clip-a",
        slot_token="slot-token-1",
        plan_hash="plan-1",
    )
    assert reservation and reservation.acquired
    assert admission.mark_submitting(
        reservation,
        replay_job_request={"configuration": {}},
    )
    assert admission.commit_handoff(
        reservation,
        replay_job_id="job-1",
        resulting_stream_id="result-1",
        replay_job_request={},
        diagnostics={},
        replay_shard={},
    )
    for name, kwargs in calls[1:]:
        assert kwargs["owner"] == "clip-a", name
        assert kwargs["slot_token"] == "slot-token-1", name
        assert kwargs["slot_generation"] == 1, name


@pytest.fixture()
def real_connection():
    database_url = os.getenv("CLIP_REPLAY_SLOT_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("fenced Replay slot PostgreSQL URL is not configured")
    connection = psycopg.connect(database_url, row_factory=dict_row)
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) = 9 AS ready
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'evidence_tasks'
              AND column_name IN (
                  'replay_slot_owner', 'replay_slot_token',
                  'replay_slot_generation', 'replay_create_state',
                  'replay_create_started_at', 'replay_create_committed_at',
                  'replay_plan_hash', 'replay_request_id', 'replay_delivery_id'
              )
            """
        )
        row = cur.fetchone()
        ready = bool(row and row["ready"])
    if not ready:
        connection.close()
        pytest.skip("Migration 031 is not applied")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _seed_real_task(connection: psycopg.Connection) -> str:
    event_id = str(uuid.uuid4())
    source_event_id = f"phase3-fence:{event_id}"
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events (
                id, source_event_id, event_type, camera_id, source_id,
                event_ts_ms, clip_required, payload
            ) VALUES (
                %(event_id)s::uuid, %(source_event_id)s, 'intrusion',
                'camera-phase3', 'source-phase3', 1783828800000, true,
                '{"runtime_epoch_id":"epoch-phase3"}'::jsonb
            )
            """,
            {"event_id": event_id, "source_event_id": source_event_id},
        )
        cur.execute(
            """
            INSERT INTO evidence_tasks (
                task_id, event_id, source_event_id, camera_id, source_id,
                event_type, event_ts_ms, task_type, clip_required, status,
                materialization_status, materialization_phase,
                materialization_owner, runtime_epoch_id
            ) VALUES (
                %(task_id)s, %(event_id)s::uuid, %(source_event_id)s,
                'camera-phase3', 'source-phase3', 'intrusion', 1783828800000,
                'snapshot_clip', true, 'materialization_pending',
                'materialization_pending', 'waiting_ready', 'replay',
                'epoch-phase3'
            )
            """,
            {
                "task_id": f"phase3-fence-{event_id}",
                "event_id": event_id,
                "source_event_id": source_event_id,
            },
        )
    return event_id


def test_real_postgres_stale_owner_cannot_commit_after_takeover(
    real_connection,
) -> None:
    repository, _adapter = _activate()
    event_id = _seed_real_task(real_connection)
    first = repository.try_acquire_fenced_replay_slot(
        real_connection,
        event_id=event_id,
        owner="clip-a",
        slot_token=f"slot-{event_id}",
        request_id="request-1",
        delivery_id="1-0",
        plan_hash="plan-1",
        source_id="source-phase3",
        camera_id="camera-phase3",
        replay_shard={"shard_id": "replay-a"},
        sink_instance="video-file-sink",
        replay_duration_seconds_effective=10.0,
        replay_duration_effective_reason="window",
        timeout_budget_s=120.0,
        max_global=8,
        max_per_shard=4,
        max_per_source=2,
    )
    assert first and first["acquired"] is True
    assert repository.mark_replay_create_started(
        real_connection,
        event_id=event_id,
        owner="clip-a",
        slot_token=str(first["slot_token"]),
        slot_generation=int(first["slot_generation"]),
        plan_hash="plan-1",
    )
    second = repository.takeover_fenced_replay_slot(
        real_connection,
        event_id=event_id,
        owner="clip-b",
        expected_token=str(first["slot_token"]),
        expected_generation=int(first["slot_generation"]),
        delivery_id="1-0",
    )
    assert second and second["claimed"] is True
    common = {
        "event_id": event_id,
        "slot_token": str(first["slot_token"]),
        "plan_hash": "plan-1",
        "replay_job_id": "job-1",
        "resulting_stream_id": f"replay-event-{event_id}",
        "replay_job_request": {},
        "diagnostics": {},
        "replay_shard": {"shard_id": "replay-a"},
    }
    assert repository.commit_replay_job_handoff(
        real_connection,
        owner="clip-a",
        slot_generation=int(first["slot_generation"]),
        **common,
    ) is False
    assert repository.commit_replay_job_handoff(
        real_connection,
        owner="clip-b",
        slot_generation=int(second["slot_generation"]),
        **common,
    ) is True
    state = repository.get_replay_slot_state(real_connection, event_id=event_id)
    assert state
    assert state["replay_job_id"] == "job-1"
    assert state["replay_create_state"] == "committed"
