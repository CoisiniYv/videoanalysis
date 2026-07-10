"""C2.15B completion-aware Replay admission tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_DIR = ROOT / "services" / "media-worker"
CLIP_HELPERS = ROOT / "harness" / "tests" / "test_clip_worker_queue_safety.py"


def _load_clip_helpers():
    spec = importlib.util.spec_from_file_location("clip_worker_queue_safety", CLIP_HELPERS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_clip_helpers = _load_clip_helpers()
_FakeRedis = _clip_helpers._FakeRedis
_FakeReplay = _clip_helpers._FakeReplay
_activate_clip = _clip_helpers._activate
_clip_config = _clip_helpers._clip_config
_request = _clip_helpers._request


def _activate_media() -> None:
    path = str(MEDIA_WORKER_DIR)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


class _Cursor:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rowcount = 1
        self.rows = rows or []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.calls.append((sql, params or {}))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _Conn:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.cursor_obj = _Cursor(rows)

    def cursor(self) -> _Cursor:
        return self.cursor_obj


def test_replay_job_creation_acquires_db_backed_slot(monkeypatch) -> None:
    _activate_clip()
    import app.worker as worker

    redis_client = _FakeRedis([_request("201")])
    updates: list[dict[str, Any]] = []
    admission_attempts: list[dict[str, Any]] = []
    recorded: list[dict[str, Any]] = []

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        updates.append({"event_id": event_id, "status": status, **kwargs})
        return True

    def fake_try_acquire(_pg_conn, **kwargs):
        admission_attempts.append(kwargs)
        return {
            "acquired": True,
            "reason": "",
            "error_message": "",
            "counts": {
                "replay_active_global_count": 0,
                "replay_active_shard_count": 0,
                "replay_active_source_count": 0,
            },
            "quota_decision": {},
        }

    def fake_record(_pg_conn, **kwargs):
        recorded.append(kwargs)
        return True

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)
    monkeypatch.setattr(
        worker,
        "active_replay_slot_counts",
        lambda *_args, **_kwargs: {
            "replay_active_global_count": 0,
            "replay_active_shard_count": 0,
            "replay_active_source_count": 0,
        },
    )
    monkeypatch.setattr(worker, "try_acquire_replay_slot", fake_try_acquire)
    monkeypatch.setattr(worker, "record_replay_job_for_slot", fake_record)

    worker.run_worker(
        _clip_config(max_concurrent_jobs=1, pending_claim_count=0),
        redis_client,
        object(),
    )

    assert updates[-1]["status"] == "replay_job_created"
    assert admission_attempts
    assert admission_attempts[0]["replay_duration_seconds_effective"] == 10.0
    assert admission_attempts[0]["replay_duration_effective_reason"] == (
        "fallback_pre_seconds_plus_post_seconds"
    )
    assert admission_attempts[0]["max_global"] == 1
    assert recorded
    assert recorded[0]["replay_job_id"] == "replay-job-1"
    assert recorded[0]["resulting_stream_id"].startswith("replay-event-")


def test_replay_job_creation_is_skipped_when_atomic_admission_denies(
    monkeypatch,
) -> None:
    _activate_clip()
    import app.worker as worker

    redis_client = _FakeRedis([_request("202")])
    queued: list[dict[str, Any]] = []

    def fake_try_acquire(_pg_conn, **_kwargs):
        return {
            "acquired": False,
            "reason": "max_concurrent_per_source_reached",
            "error_message": (
                "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE reached"
            ),
            "counts": {
                "replay_active_global_count": 2,
                "replay_active_shard_count": 1,
                "replay_active_source_count": 1,
            },
            "quota_decision": {
                "scope": "source_concurrency",
                "limit": 1,
                "observed": 1,
                "admission_mode": "atomic",
            },
        }

    def fake_queue(_pg_conn, **kwargs):
        queued.append(kwargs)

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "try_acquire_replay_slot", fake_try_acquire)
    monkeypatch.setattr(worker, "_queue_clip_request", fake_queue)
    monkeypatch.setattr(
        worker,
        "active_replay_slot_counts",
        lambda *_args, **_kwargs: {
            "replay_active_global_count": 0,
            "replay_active_shard_count": 0,
            "replay_active_source_count": 0,
        },
    )

    worker.run_worker(
        _clip_config(
            max_concurrent_jobs=10,
            evidence_materialization_max_concurrency=10,
            evidence_materialization_max_concurrency_per_source=1,
            pending_claim_count=0,
        ),
        redis_client,
        object(),
    )

    assert queued
    assert queued[0]["reason"] == "max_concurrent_per_source_reached"
    assert queued[0]["active_source_count"] == 1
    assert queued[0]["quota_decision"]["admission_mode"] == "atomic"
    assert _FakeReplay.instances
    assert _FakeReplay.instances[0].jobs == []


def test_terminal_replay_slot_admission_is_acked_without_queue(
    monkeypatch,
) -> None:
    _activate_clip()
    import app.worker as worker

    redis_client = _FakeRedis([_request("209")])
    queued: list[dict[str, Any]] = []

    def fake_try_acquire(_pg_conn, **_kwargs):
        return {
            "acquired": False,
            "reason": "replay_slot_terminal_state",
            "error_message": "replay_slot_terminal_state",
            "counts": {
                "replay_active_global_count": 0,
                "replay_active_shard_count": 0,
                "replay_active_source_count": 0,
            },
            "quota_decision": {
                "scope": "replay_admission",
                "admission_mode": "atomic",
            },
        }

    def fake_queue(_pg_conn, **kwargs):
        queued.append(kwargs)

    _FakeReplay.instances.clear()
    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _FakeReplay)
    monkeypatch.setattr(worker, "try_acquire_replay_slot", fake_try_acquire)
    monkeypatch.setattr(worker, "_queue_clip_request", fake_queue)
    monkeypatch.setattr(
        worker,
        "active_replay_slot_counts",
        lambda *_args, **_kwargs: {
            "replay_active_global_count": 0,
            "replay_active_shard_count": 0,
            "replay_active_source_count": 0,
        },
    )

    worker.run_worker(
        _clip_config(
            max_concurrent_jobs=10,
            evidence_materialization_max_concurrency=10,
            evidence_materialization_max_concurrency_per_source=1,
            pending_claim_count=0,
        ),
        redis_client,
        object(),
    )

    assert redis_client.acked == ["1-0"]
    assert queued == []
    assert _FakeReplay.instances
    assert _FakeReplay.instances[0].jobs == []


def test_replay_job_creation_exception_releases_reserved_slot(monkeypatch) -> None:
    _activate_clip()
    import app.worker as worker

    redis_client = _FakeRedis([_request("203")])
    released: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []

    class _RaisingReplay(_FakeReplay):
        def create_job(self, **_kwargs):
            raise RuntimeError("replay unavailable")

    def fake_try_acquire(_pg_conn, **_kwargs):
        return {
            "acquired": True,
            "reason": "",
            "error_message": "",
            "counts": {
                "replay_active_global_count": 0,
                "replay_active_shard_count": 0,
                "replay_active_source_count": 0,
            },
            "quota_decision": {},
        }

    def fake_release(_pg_conn, **kwargs):
        released.append(kwargs)
        return True

    def fake_update_clip_status(_pg_conn, event_id, status, **kwargs):
        statuses.append({"event_id": event_id, "status": status, **kwargs})
        return True

    worker.shutdown_requested = False
    monkeypatch.setattr(worker, "ReplayClient", _RaisingReplay)
    monkeypatch.setattr(worker, "try_acquire_replay_slot", fake_try_acquire)
    monkeypatch.setattr(worker, "release_replay_slot", fake_release)
    monkeypatch.setattr(worker, "update_clip_status", fake_update_clip_status)
    monkeypatch.setattr(
        worker,
        "active_replay_slot_counts",
        lambda *_args, **_kwargs: {
            "replay_active_global_count": 0,
            "replay_active_shard_count": 0,
            "replay_active_source_count": 0,
        },
    )

    worker.run_worker(
        _clip_config(max_concurrent_jobs=1, pending_claim_count=0),
        redis_client,
        object(),
    )

    assert released == [
        {
            "event_id": "00000000-0000-4000-8000-000000000203",
            "release_reason": "replay_job_create_exception",
        }
    ]
    assert statuses[-1]["status"] == "failed"
    assert "replay unavailable" in statuses[-1]["error_message"]
    assert redis_client.acked == ["1-0"]


def test_db_active_slot_count_blocks_low_priority_admission() -> None:
    _activate_clip()
    import app.worker as worker

    decision = worker._clip_gate_decision(
        _clip_config(evidence_materialization_max_concurrency=1),
        jobs_created=0,
        active_jobs=[],
        active_counts={
            "replay_active_global_count": 1,
            "replay_active_shard_count": 0,
            "replay_active_source_count": 0,
        },
        shard_id="default",
        source_id="source-1",
        camera_id="camera-1",
        cooldown_gate_ts_ms=1_780_000_000_000,
        last_job_by_camera={},
        event_type_counts={},
        event_type="intrusion",
    )

    assert decision.allowed is False
    assert decision.reason == "max_concurrent_reached"
    assert decision.quota_decision["observed"] == 1


def test_db_active_slot_count_blocks_priority_at_hard_concurrency() -> None:
    _activate_clip()
    import app.worker as worker

    decision = worker._clip_gate_decision(
        _clip_config(evidence_materialization_max_concurrency=1),
        jobs_created=0,
        active_jobs=[],
        active_counts={
            "replay_active_global_count": 1,
            "replay_active_shard_count": 0,
            "replay_active_source_count": 0,
        },
        shard_id="default",
        source_id="source-1",
        camera_id="camera-1",
        cooldown_gate_ts_ms=1_780_000_000_000,
        last_job_by_camera={},
        event_type_counts={},
        event_type="watchlist_hit",
    )

    assert decision.allowed is False
    assert decision.reason == "max_concurrent_reached"
    assert decision.quota_decision["observed"] == 1


def test_effective_duration_prefers_duration_override_over_pre_post() -> None:
    _activate_clip()
    import app.worker as worker

    timing = worker._effective_replay_slot_timing(
        _clip_config(
            media_poll_interval_s=5.0,
            midterm_sink_stability_checks=2,
            evidence_replay_sink_stability_budget_s=60.0,
            evidence_replay_finalizer_budget_s=30.0,
            evidence_replay_slot_grace_s=5.0,
        ),
        {"duration_seconds_override": 45},
        pre_seconds=5,
        post_seconds=5,
        offset_seconds_override=None,
        duration_seconds_override=45.0,
    )

    assert timing.replay_duration_seconds_effective == 45.0
    assert timing.replay_duration_effective_reason == "duration_seconds_override"
    assert timing.timeout_budget_s == 150.0


def test_effective_duration_uses_offset_plus_post_when_longer_than_pre_post() -> None:
    _activate_clip()
    import app.worker as worker

    timing = worker._effective_replay_slot_timing(
        _clip_config(evidence_replay_slot_grace_s=0.0),
        {},
        pre_seconds=5,
        post_seconds=5,
        offset_seconds_override=22.0,
        duration_seconds_override=27.0,
    )

    assert timing.replay_duration_seconds_effective == 27.0
    assert timing.replay_duration_effective_reason == (
        "computed_offset_seconds_override_plus_post_seconds"
    )
    assert timing.timeout_budget_s == 27.0


def test_repository_acquire_slot_writes_active_lifecycle_fields() -> None:
    _activate_clip()
    from app.repository import acquire_replay_slot

    conn = _Conn()

    assert acquire_replay_slot(
        conn,
        event_id="00000000-0000-4000-8000-000000000201",
        replay_job_id="job-201",
        source_id="source-1",
        camera_id="camera-1",
        replay_shard={
            "shard_id": "replay-a",
            "replay_api_url": "http://replay-a:8080",
            "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-a:6666",
        },
        sink_instance="video-file-sink-a",
        resulting_stream_id="replay-event-201",
        replay_duration_seconds_effective=45.0,
        replay_duration_effective_reason="duration_seconds_override",
        timeout_budget_s=150.0,
    )

    sql = "\n".join(call[0] for call in conn.cursor_obj.calls)
    params = conn.cursor_obj.calls[0][1]
    assert "replay_slot_status = 'active'" in sql
    assert "replay_slot_deadline_at" in sql
    assert "duration_effective_reason',\n                                %(duration_reason)s::text" in sql
    assert params["replay_job_id"] == "job-201"
    assert params["resulting_stream_id"] == "replay-event-201"
    assert params["timeout_s"] == 150.0


def test_repository_try_acquire_slot_is_atomic_and_returns_quota_counts() -> None:
    _activate_clip()
    from app.repository import try_acquire_replay_slot

    conn = _Conn(
        rows=[
            {
                "global_count": 2,
                "shard_count": 1,
                "source_count": 0,
                "deny_reason": "",
                "acquired": True,
                "event_updated": True,
            }
        ]
    )

    result = try_acquire_replay_slot(
        conn,
        event_id="00000000-0000-4000-8000-000000000204",
        source_id="source-1",
        camera_id="camera-1",
        replay_shard={
            "shard_id": "replay-a",
            "replay_api_url": "http://replay-a:8080",
            "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-a:6666",
        },
        sink_instance="video-file-sink-a",
        replay_duration_seconds_effective=15.0,
        replay_duration_effective_reason="duration_seconds_override",
        timeout_budget_s=120.0,
        max_global=10,
        max_per_shard=4,
        max_per_source=1,
    )

    sql = "\n".join(call[0] for call in conn.cursor_obj.calls)
    params = conn.cursor_obj.calls[-1][1]
    assert result is not None
    assert result["acquired"] is True
    assert result["counts"] == {
        "replay_active_global_count": 2,
        "replay_active_shard_count": 1,
        "replay_active_source_count": 0,
    }
    assert "pg_advisory_xact_lock" in sql
    assert "decision AS" in sql
    assert "reserved AS" in sql
    assert "decision.deny_reason = ''" in sql
    assert params["max_global"] == 10
    assert params["max_per_source"] == 1


def test_repository_try_acquire_slot_returns_atomic_deny_reason() -> None:
    _activate_clip()
    from app.repository import try_acquire_replay_slot

    conn = _Conn(
        rows=[
            {
                "global_count": 3,
                "shard_count": 2,
                "source_count": 1,
                "deny_reason": "max_concurrent_per_source_reached",
                "acquired": False,
                "event_updated": False,
            }
        ]
    )

    result = try_acquire_replay_slot(
        conn,
        event_id="00000000-0000-4000-8000-000000000205",
        source_id="source-1",
        camera_id="camera-1",
        replay_shard={"shard_id": "replay-a"},
        sink_instance="video-file-sink-a",
        replay_duration_seconds_effective=15.0,
        replay_duration_effective_reason="duration_seconds_override",
        timeout_budget_s=120.0,
        max_global=10,
        max_per_shard=4,
        max_per_source=1,
    )

    assert result is not None
    assert result["acquired"] is False
    assert result["reason"] == "max_concurrent_per_source_reached"
    assert result["quota_decision"] == {
        "scope": "source_concurrency",
        "source_id": "source-1",
        "limit": 1,
        "observed": 1,
        "admission_mode": "atomic",
    }


def test_repository_try_acquire_slot_does_not_reopen_released_slot() -> None:
    _activate_clip()
    from app.repository import try_acquire_replay_slot

    conn = _Conn(
        rows=[
            {
                "global_count": 0,
                "shard_count": 0,
                "source_count": 0,
                "deny_reason": "replay_slot_terminal_state",
                "acquired": False,
                "event_updated": False,
            }
        ]
    )

    result = try_acquire_replay_slot(
        conn,
        event_id="00000000-0000-4000-8000-000000000207",
        source_id="source-1",
        camera_id="camera-1",
        replay_shard={"shard_id": "replay-a"},
        sink_instance="video-file-sink-a",
        replay_duration_seconds_effective=15.0,
        replay_duration_effective_reason="duration_seconds_override",
        timeout_budget_s=35.0,
        max_global=10,
        max_per_shard=4,
        max_per_source=1,
    )

    sql = "\n".join(call[0] for call in conn.cursor_obj.calls)
    assert result is not None
    assert result["acquired"] is False
    assert result["reason"] == "replay_slot_terminal_state"
    assert "target AS" in sql
    assert "current_slot_status IN ('released', 'timeout')" in sql
    assert "COALESCE(et.replay_slot_status, '') NOT IN" in sql


def test_repository_record_replay_job_for_slot_updates_active_slot() -> None:
    _activate_clip()
    from app.repository import record_replay_job_for_slot

    conn = _Conn(rows=[{"task_updated": True, "event_updated": True}])

    assert record_replay_job_for_slot(
        conn,
        event_id="00000000-0000-4000-8000-000000000206",
        replay_job_id="job-206",
        resulting_stream_id="replay-event-206",
    )

    sql = conn.cursor_obj.calls[0][0]
    params = conn.cursor_obj.calls[0][1]
    assert "WHERE event_id = %(event_id)s::uuid" in sql
    assert "replay_slot_status = 'active'" in sql
    assert "task_updated" in sql
    assert params["replay_job_id"] == "job-206"
    assert params["resulting_stream_id"] == "replay-event-206"


def test_active_slot_count_recovers_from_db_after_restart() -> None:
    _activate_clip()
    from app.repository import active_replay_slot_counts

    conn = _Conn(rows=[{"global_count": 3, "shard_count": 2, "source_count": 1}])

    counts = active_replay_slot_counts(
        conn,
        shard_id="replay-a",
        source_id="source-1",
    )

    assert counts == {
        "replay_active_global_count": 3,
        "replay_active_shard_count": 2,
        "replay_active_source_count": 1,
    }
    sql = "\n".join(call[0] for call in conn.cursor_obj.calls)
    assert "replay_slot_status = 'timeout'" in sql
    assert "replay_slot_status = 'active'" in sql


def test_timeout_release_records_timeout_reason() -> None:
    _activate_clip()
    from app.repository import release_timed_out_replay_slots

    conn = _Conn()

    assert release_timed_out_replay_slots(conn) == 1
    sql = conn.cursor_obj.calls[0][0]
    assert "replay_slot_status = 'timeout'" in sql
    assert "replay_slot_release_reason = 'timeout'" in sql
    assert "replay_slot_timeout_budget_s" in sql


def test_media_worker_releases_slot_on_sink_video_stable() -> None:
    _activate_media()
    from app import worker

    conn = _Conn()

    assert worker._release_replay_slot_for_sink_stable(
        conn,
        event_id="00000000-0000-4000-8000-000000000202",
        phase_diagnostics={
            "sink_video_first_seen_at": "1970-01-01T00:00:10+00:00",
            "sink_video_stable_at": "1970-01-01T00:00:41+00:00",
        },
    )

    sql = conn.cursor_obj.calls[0][0]
    params = conn.cursor_obj.calls[0][1]
    assert "replay_slot_status = 'released'" in sql
    assert "replay_slot_release_reason = 'sink_video_stable'" in sql
    assert params["sink_video_to_stable_ms"] == 31000
