"""Real-PostgreSQL contract tests for Spec 33/34 Phase 1 transitions.

Set ``MATERIALIZATION_REPOSITORY_TEST_DATABASE_URL`` to a disposable database
that has migrations 001-030 applied.  The suite uses unique rows and removes
them after each test; it never targets the normal runtime database by default.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import sys
import uuid

import psycopg
from psycopg.rows import dict_row
import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "services/media-worker/app/materialization_repository.py"
SPEC = importlib.util.spec_from_file_location(
    "phase1_materialization_repository_contract",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
repository = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = repository
SPEC.loader.exec_module(repository)

CLIP_MODULE_PATH = ROOT / "services/clip-worker/app/repository.py"
CLIP_SPEC = importlib.util.spec_from_file_location(
    "phase1_clip_repository_contract",
    CLIP_MODULE_PATH,
)
assert CLIP_SPEC and CLIP_SPEC.loader
clip_repository = importlib.util.module_from_spec(CLIP_SPEC)
sys.modules[CLIP_SPEC.name] = clip_repository
CLIP_SPEC.loader.exec_module(clip_repository)


@pytest.fixture()
def conn():
    database_url = os.getenv("MATERIALIZATION_REPOSITORY_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("disposable lifecycle-v2 PostgreSQL URL is not configured")
    connection = psycopg.connect(database_url, autocommit=True, row_factory=dict_row)
    if not repository.supports_lifecycle_v2(connection, refresh=True):
        connection.close()
        pytest.skip("Migration 029 is not applied to the disposable database")
    created_events: list[str] = []
    yield connection, created_events
    with connection.cursor() as cur:
        if created_events:
            cur.execute(
                "DELETE FROM events WHERE id = ANY(%(event_ids)s::uuid[])",
                {"event_ids": created_events},
            )
    repository.clear_schema_capability_cache()
    clip_repository.clear_lifecycle_schema_capability_cache()
    connection.close()


def _seed_task(
    connection: psycopg.Connection,
    created_events: list[str],
    *,
    status: str = "materialization_pending",
    phase: str = "waiting_ready",
    owner: str = "rolling",
    deadline_sql: str = "now() + interval '1 hour'",
    ready_sql: str = "now() - interval '1 second'",
    lease_owner: str | None = None,
    lease_token: str | None = None,
    lease_generation: int = 0,
    lease_expiry_sql: str = "NULL",
    handoff: dict | None = None,
) -> str:
    event_id = str(uuid.uuid4())
    created_events.append(event_id)
    source_event_id = f"phase1-contract:{event_id}"
    task_id = f"phase1-contract-{event_id}"
    handoff = handoff or {}
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events (
                id, source_event_id, event_type, camera_id, source_id,
                event_ts_ms, clip_required, payload
            ) VALUES (
                %(event_id)s::uuid, %(source_event_id)s, 'intrusion',
                'camera-phase1', 'source-phase1', 1783828800000, true,
                '{"runtime_epoch_id":"epoch-phase1"}'::jsonb
            )
            """,
            {"event_id": event_id, "source_event_id": source_event_id},
        )
        cur.execute(
            f"""
            INSERT INTO evidence_tasks (
                task_id, event_id, source_event_id, camera_id, source_id,
                event_type, event_ts_ms, task_type, clip_required,
                status, materialization_status, materialization_phase,
                materialization_phase_updated_at, materialization_owner,
                materialization_ready_at, materialization_deadline_at,
                materialization_next_attempt_at, runtime_epoch_id,
                materialization_lease_owner, materialization_lease_token,
                materialization_lease_generation,
                materialization_lease_expires_at,
                materialization_lease_heartbeat_at, materialization_handoff
            ) VALUES (
                %(task_id)s, %(event_id)s::uuid, %(source_event_id)s,
                'camera-phase1', 'source-phase1', 'intrusion', 1783828800000,
                'snapshot_clip', true, %(status)s, %(status)s, %(phase)s,
                now(), %(owner)s, {ready_sql}, {deadline_sql}, NULL,
                'epoch-phase1', %(lease_owner)s, %(lease_token)s,
                %(lease_generation)s, {lease_expiry_sql},
                CASE WHEN %(lease_token)s::text IS NULL THEN NULL ELSE now() END,
                %(handoff)s::jsonb
            )
            """,
            {
                "task_id": task_id,
                "event_id": event_id,
                "source_event_id": source_event_id,
                "status": status,
                "phase": phase,
                "owner": owner,
                "lease_owner": lease_owner,
                "lease_token": lease_token,
                "lease_generation": lease_generation,
                "handoff": psycopg.types.json.Jsonb(handoff),
            },
        )
    return event_id


def _task(connection: psycopg.Connection, event_id: str) -> dict:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT * FROM evidence_tasks WHERE event_id = %(event_id)s::uuid",
            {"event_id": event_id},
        )
        row = cur.fetchone()
    assert row
    return dict(row)


def _event(connection: psycopg.Connection, event_id: str) -> dict:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT * FROM events WHERE id = %(event_id)s::uuid",
            {"event_id": event_id},
        )
        row = cur.fetchone()
    assert row
    return dict(row)


def test_fenced_claim_retry_and_stale_owner_are_independent_of_ready_time(conn) -> None:
    connection, created_events = conn
    event_id = _seed_task(connection, created_events)
    ready_at = _task(connection, event_id)["materialization_ready_at"]

    first = repository.claim_rolling_task(
        connection,
        event_id=event_id,
        worker_id="worker-a",
        phase="remux_running",
        lease_seconds=30,
    )
    assert first is not None
    assert repository.claim_rolling_task(
        connection,
        event_id=event_id,
        worker_id="worker-b",
        phase="remux_running",
        lease_seconds=30,
    ) is None

    stale = repository.MaterializationLease(
        event_id=event_id,
        owner="worker-b",
        token=first.token,
        generation=first.generation,
        phase=first.phase,
    )
    assert repository.heartbeat_lease(connection, stale, lease_seconds=30) is False
    assert repository.retry_rolling_task(
        connection,
        stale,
        reason="coverage_not_complete",
    ) is False
    assert repository.fail_rolling_task(
        connection,
        stale,
        reason="stable_metadata_invalid",
    ) is False

    assert repository.heartbeat_lease(connection, first, lease_seconds=30) is True
    assert repository.retry_rolling_task(
        connection,
        first,
        reason="coverage_not_complete:post_gap_ns=100",
        retry_hint_s=0.5,
    ) is True
    retried = _task(connection, event_id)
    assert retried["materialization_status"] == "materialization_pending"
    assert retried["materialization_phase"] == "waiting_coverage"
    assert retried["materialization_retry_reason"] == "coverage_not_complete"
    assert retried["materialization_ready_at"] == ready_at
    assert retried["materialization_next_attempt_at"] is not None
    assert retried["materialization_lease_token"] is None

    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE evidence_tasks
            SET materialization_next_attempt_at = now() - interval '1 second'
            WHERE event_id = %(event_id)s::uuid
            """,
            {"event_id": event_id},
        )
    second = repository.claim_rolling_task(
        connection,
        event_id=event_id,
        worker_id="worker-b",
        phase="remux_running",
        lease_seconds=30,
    )
    assert second is not None
    assert second.generation > first.generation
    assert repository.fail_rolling_task(
        connection,
        first,
        reason="stable_metadata_invalid",
    ) is False
    assert repository.fail_rolling_task(
        connection,
        second,
        reason="stable_metadata_invalid",
    ) is True
    terminal = _task(connection, event_id)
    assert terminal["materialization_status"] == "materialization_failed"
    assert terminal["materialization_phase"] == "terminal"
    assert terminal["materialization_lease_token"] is None
    assert terminal["materialization_failure_reason"] == "stable_metadata_invalid"

    unknown_reason_event_id = _seed_task(connection, created_events)
    unknown_reason_lease = repository.claim_rolling_task(
        connection,
        event_id=unknown_reason_event_id,
        worker_id="worker-image",
        phase="image_running",
        lease_seconds=30,
    )
    assert unknown_reason_lease is not None
    assert repository.fail_rolling_task(
        connection,
        unknown_reason_lease,
        reason="face_image_no_rolling_cache_segments",
    ) is True
    unknown_reason_terminal = _task(connection, unknown_reason_event_id)
    assert unknown_reason_terminal["materialization_failure_reason"] == (
        "face_image_no_rolling_cache_segments"
    )


def test_durable_handoff_survives_retry_and_fences_previous_owner(conn) -> None:
    connection, created_events = conn
    event_id = _seed_task(connection, created_events)
    rolling = repository.claim_rolling_task(
        connection,
        event_id=event_id,
        worker_id="rolling-a",
        phase="remux_running",
        lease_seconds=30,
    )
    assert rolling is not None
    handoff = {
        "attempt_token": rolling.token,
        "source_id": "source-phase1",
        "runtime_epoch_id": "epoch-phase1",
        "requested_window": {"start_pts": 1, "end_pts": 2},
        "selected_segment_ids": ["segment-1"],
        "staging_path": "/tmp/phase1-attempt",
        "canonical_path": "/tmp/phase1-attempt/raw_clip.mov",
        "size": 123,
        "mtime_ns": 456,
    }
    assert repository.persist_finalizer_handoff(
        connection,
        rolling,
        sink_output_path="/tmp/phase1-attempt",
        handoff=handoff,
        lease_seconds=30,
    ) is True

    claimed = repository.claim_finalizer_task(
        connection,
        event_id=event_id,
        sink_output_path="/tmp/phase1-attempt",
        worker_id="finalizer-a",
        lease_seconds=30,
    )
    assert claimed["status"] == "claimed"
    finalizer = claimed["lease"]
    assert isinstance(finalizer, repository.MaterializationLease)
    assert repository.fail_rolling_task(
        connection,
        rolling,
        reason="stable_metadata_invalid",
    ) is False
    assert repository.retry_finalizer_handoff(
        connection,
        finalizer,
        reason="db_unavailable",
        retry_hint_s=0.5,
    ) is True
    pending = _task(connection, event_id)
    assert pending["materialization_phase"] == "finalizer_pending"
    assert pending["materialization_handoff"]["attempt_token"] == rolling.token
    assert pending["materialization_lease_token"] is None

    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE evidence_tasks
            SET materialization_next_attempt_at = now() - interval '1 second'
            WHERE event_id = %(event_id)s::uuid
            """,
            {"event_id": event_id},
        )
    recoverable = repository.recoverable_finalizer_handoffs(connection, limit=10)
    assert len(recoverable) == 1
    assert recoverable[0]["event_id"] == event_id
    assert recoverable[0]["sink_output_path"] == "/tmp/phase1-attempt"
    reclaimed = repository.claim_finalizer_task(
        connection,
        event_id=event_id,
        sink_output_path="/tmp/phase1-attempt",
        worker_id="finalizer-b",
        lease_seconds=30,
    )
    assert reclaimed["status"] == "claimed"
    winner = reclaimed["lease"]
    assert isinstance(winner, repository.MaterializationLease)
    assert repository.recoverable_finalizer_handoffs(connection, limit=10) == []
    assert winner.generation > finalizer.generation
    assert repository.fail_rolling_task(
        connection,
        finalizer,
        reason="stable_metadata_invalid",
    ) is False
    assert repository.fail_rolling_task(
        connection,
        winner,
        reason="stable_metadata_invalid",
    ) is True


def test_deadline_and_lease_recovery_decision_table(conn) -> None:
    connection, created_events = conn
    ready_expired = _seed_task(
        connection,
        created_events,
        deadline_sql="now() - interval '1 second'",
    )
    running_valid = _seed_task(
        connection,
        created_events,
        status="materializing",
        phase="remux_running",
        deadline_sql="now() - interval '1 second'",
        lease_owner="worker-valid",
        lease_token="token-valid",
        lease_generation=1,
        lease_expiry_sql="now() + interval '1 minute'",
    )
    handoff = {
        "attempt_token": "token-handoff",
        "source_id": "source-phase1",
        "runtime_epoch_id": "epoch-phase1",
        "requested_window": {"start_pts": 1, "end_pts": 2},
        "selected_segment_ids": ["segment-1"],
        "staging_path": "/tmp/handoff",
        "canonical_path": "/tmp/handoff/raw_clip.mov",
        "size": 1,
        "mtime_ns": 1,
    }
    handoff_recovery = _seed_task(
        connection,
        created_events,
        status="materializing",
        phase="finalizing",
        deadline_sql="now() - interval '1 second'",
        lease_owner="worker-old",
        lease_token="token-handoff",
        lease_generation=2,
        lease_expiry_sql="now() - interval '1 second'",
        handoff=handoff,
    )
    lease_retry = _seed_task(
        connection,
        created_events,
        status="materializing",
        phase="remux_running",
        deadline_sql="now() + interval '1 hour'",
        lease_owner="worker-old",
        lease_token="token-retry",
        lease_generation=3,
        lease_expiry_sql="now() - interval '1 second'",
    )
    lease_expired = _seed_task(
        connection,
        created_events,
        status="materializing",
        phase="remux_running",
        deadline_sql="now() - interval '1 second'",
        lease_owner="worker-old",
        lease_token="token-expired",
        lease_generation=4,
        lease_expiry_sql="now() - interval '1 second'",
    )

    result = repository.recover_and_expire_rolling_tasks(
        connection,
        source_ids=("source-phase1",),
    )
    assert result.ready_deadline_expired == 1
    assert result.running_sla_missed == 1
    assert result.handoff_recovered == 1
    assert result.lease_retry_scheduled == 1
    assert result.lease_deadline_expired == 1

    assert _task(connection, ready_expired)["materialization_status"] == (
        "materialization_expired"
    )
    assert _event(connection, ready_expired)["media_status"] == (
        "materialization_expired"
    )
    valid = _task(connection, running_valid)
    assert valid["materialization_status"] == "materializing"
    assert valid["materialization_lease_token"] == "token-valid"
    assert valid["materialization_audit"]["business_deadline"]["status"] == (
        "missed_during_valid_lease"
    )
    recovered = _task(connection, handoff_recovery)
    assert recovered["materialization_phase"] == "finalizer_pending"
    assert recovered["materialization_lease_token"] is None
    recovered_event = _event(connection, handoff_recovery)
    assert recovered_event["media_status"] == "materializing"
    assert recovered_event["payload"]["media"]["materialization_phase"] == (
        "finalizer_pending"
    )
    assert _task(connection, lease_retry)["materialization_status"] == (
        "materialization_pending"
    )
    assert _event(connection, lease_retry)["media_status"] == (
        "materialization_pending"
    )
    assert _task(connection, lease_expired)["materialization_status"] == (
        "materialization_expired"
    )


def test_terminal_deferred_is_never_claimable(conn) -> None:
    connection, created_events = conn
    event_id = _seed_task(
        connection,
        created_events,
        status="materialization_deferred",
        phase="terminal",
        owner="terminal",
    )
    assert repository.claim_rolling_task(
        connection,
        event_id=event_id,
        worker_id="worker-a",
        phase="remux_running",
        lease_seconds=30,
    ) is None
    result = repository.claim_finalizer_task(
        connection,
        event_id=event_id,
        sink_output_path="/tmp/none",
        worker_id="worker-a",
        lease_seconds=30,
    )
    assert result["status"] == "terminal"
    assert result["claimed"] is False


def test_finalizer_completion_is_task_first_and_fenced(conn) -> None:
    connection, created_events = conn
    event_id = _seed_task(connection, created_events)
    rolling = repository.claim_rolling_task(
        connection,
        event_id=event_id,
        worker_id="rolling-a",
        phase="remux_running",
        lease_seconds=30,
    )
    assert rolling is not None
    handoff = {
        "attempt_token": rolling.token,
        "source_id": "source-phase1",
        "runtime_epoch_id": "epoch-phase1",
        "requested_window": {"start_pts": 1, "end_pts": 2},
        "selected_segment_ids": ["segment-1"],
        "staging_path": "/tmp/finalizer-fence",
        "canonical_path": "/tmp/finalizer-fence/raw_clip.mov",
        "size": 123,
        "mtime_ns": 456,
    }
    assert repository.persist_finalizer_handoff(
        connection,
        rolling,
        sink_output_path="/tmp/finalizer-fence",
        handoff=handoff,
        lease_seconds=30,
    )
    claim = repository.claim_finalizer_task(
        connection,
        event_id=event_id,
        sink_output_path="/tmp/finalizer-fence",
        worker_id="finalizer-a",
        lease_seconds=30,
    )
    winner = claim["lease"]
    assert isinstance(winner, repository.MaterializationLease)
    stale = repository.MaterializationLease(
        event_id=event_id,
        owner="finalizer-stale",
        token=winner.token,
        generation=winner.generation,
        phase=winner.phase,
    )

    assert repository.complete_finalizer_task(
        connection,
        stale,
        materialization_status="materialized",
        clip_path="/tmp/stale/raw_clip.mov",
        metadata_path="/tmp/stale/metadata.json",
        output_root="/tmp/stale",
    ) is False
    active = _task(connection, event_id)
    assert active["materialization_status"] == "materializing"
    assert active["materialization_lease_owner"] == "finalizer-a"
    assert _event(connection, event_id)["media_status"] == "materializing"

    assert repository.complete_finalizer_task(
        connection,
        winner,
        materialization_status="materialized",
        clip_path="/tmp/winner/raw_clip.mov",
        metadata_path="/tmp/winner/metadata.json",
        output_root="/tmp/winner",
    ) is True
    terminal = _task(connection, event_id)
    assert terminal["materialization_status"] == "materialized"
    assert terminal["materialization_phase"] == "terminal"
    assert terminal["materialization_owner"] == "terminal"
    assert terminal["materialization_lease_token"] is None
    assert terminal["clip_path"] == "/tmp/winner/raw_clip.mov"
    event = _event(connection, event_id)
    assert event["media_status"] == "materialized"
    assert event["payload"]["media"]["materialization_phase"] == "terminal"

    assert repository.record_cleanup_outcome(
        connection,
        event_id=event_id,
        status="cleanup_pending",
        sink_output_path="/tmp/finalizer-fence",
        error="temporary_io_error:device busy",
    ) is True
    assert repository.pending_cleanup_tasks(connection, limit=10) == [
        {
            "event_id": event_id,
            "sink_output_path": "/tmp/finalizer-fence",
            "materialization_status": "materialized",
            "attempt_count": 1,
        }
    ]
    still_terminal = _task(connection, event_id)
    assert still_terminal["materialization_status"] == "materialized"
    assert still_terminal["cleanup_audit"]["sink_output"]["status"] == (
        "cleanup_pending"
    )

    assert repository.record_cleanup_outcome(
        connection,
        event_id=event_id,
        status="deleted",
        sink_output_path="/tmp/finalizer-fence",
        deleted_bytes=123,
    ) is True
    assert repository.pending_cleanup_tasks(connection, limit=10) == []
    cleaned = _task(connection, event_id)
    assert cleaned["materialization_status"] == "materialized"
    assert cleaned["cleanup_audit"]["sink_output"]["status"] == "deleted"
    assert cleaned["cleanup_audit"]["sink_output"]["attempt_count"] == 2


def test_clip_transition_is_task_first_idempotent_and_replay_owned(conn) -> None:
    connection, created_events = conn
    event_id = _seed_task(
        connection,
        created_events,
        owner="replay",
    )
    ready_at = _task(connection, event_id)["materialization_ready_at"]

    assert clip_repository.update_clip_status(
        connection,
        event_id,
        "replay_job_created",
        replay_job_id="replay-job-phase1",
        replay_job_request={"configuration": {"resulting_stream_id": "stream-1"}},
        request_id="request-phase1",
        attempt_count=2,
    ) is True
    running = _task(connection, event_id)
    assert running["materialization_status"] == "materializing"
    assert running["materialization_phase"] == "waiting_ready"
    assert running["materialization_owner"] == "replay"
    assert running["replay_job_id"] == "replay-job-phase1"
    assert running["materialization_ready_at"] == ready_at
    with connection.cursor() as cur:
        cur.execute(
            "SELECT media_status, payload FROM events WHERE id = %(event_id)s::uuid",
            {"event_id": event_id},
        )
        event_row = cur.fetchone()
    assert event_row["media_status"] == "materializing"
    assert event_row["payload"]["media"]["replay_job_id"] == (
        "replay-job-phase1"
    )

    assert clip_repository.update_clip_status(
        connection,
        event_id,
        "failed",
        error_message="missing source_id in record_request",
    ) is True
    terminal = _task(connection, event_id)
    assert terminal["materialization_status"] == "materialization_failed"
    assert terminal["materialization_phase"] == "terminal"
    assert terminal["materialization_owner"] == "terminal"
    assert terminal["materialization_failure_reason"] == "missing_source_id"

    # Repeating the same transition repairs a crash between task CAS and event
    # projection, while a contradictory terminal transition is fenced out.
    assert clip_repository.update_clip_status(
        connection,
        event_id,
        "failed",
        error_message="missing source_id in record_request",
    ) is True
    assert clip_repository.update_clip_status(
        connection,
        event_id,
        "pending",
        evidence_state="materialization_deferred",
        evidence_reason="covered_by_existing_evidence",
    ) is False

    rolling_event_id = _seed_task(connection, created_events, owner="rolling")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT media_status, payload FROM events WHERE id = %(event_id)s::uuid",
            {"event_id": rolling_event_id},
        )
        before = dict(cur.fetchone())
    assert clip_repository.update_clip_status(
        connection,
        rolling_event_id,
        "failed",
        error_message="clip must not own rolling task",
    ) is False
    assert _task(connection, rolling_event_id)["materialization_status"] == (
        "materialization_pending"
    )
    with connection.cursor() as cur:
        cur.execute(
            "SELECT media_status, payload FROM events WHERE id = %(event_id)s::uuid",
            {"event_id": rolling_event_id},
        )
        after = dict(cur.fetchone())
    assert after == before
