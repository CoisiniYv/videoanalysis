"""Spec 33 Phase 2 single-finalizer resource and fence contracts."""

from __future__ import annotations

import sys
import inspect
import json
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"
EVENT_ID = "11111111-1111-4111-8111-111111111111"


def _worker():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    root = str(MEDIA_WORKER_ROOT)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    import app.worker as worker

    return worker


def _permit(worker: Any):
    guard = worker._MaterializationGuard(1)
    permit = worker._HeldMaterializationPermit.acquire(guard, required=True)
    assert permit.acquired is True
    return guard, permit


def _finalize(worker: Any, permit: Any):
    return worker._finalize_one(
        object(),
        event_id=EVENT_ID,
        meta={"job_id": "job-1"},
        meta_dir="/tmp/sink/event-1",
        metadata_file="/tmp/sink/event-1/metadata.json",
        sink_dir="/tmp/sink",
        evidence_output_dir="/tmp/evidence",
        finalizer_worker_id="finalizer-test",
        source_id="source-1",
        replay_shard_id="default",
        phase_diagnostics={},
        guardrails={},
        permit=permit,
        materialization_timeout_s=30,
        cleanup_replay_sink_output_enabled=True,
        cleanup_replay_sink_output_statuses=("ready",),
        schedule_row={},
        materialization_pacer=None,
        scan_stats={},
    )


@pytest.mark.parametrize(
    ("claim_status", "processed"),
    (("terminal", True), ("busy", False)),
)
def test_terminal_and_busy_release_permit(
    monkeypatch: pytest.MonkeyPatch,
    claim_status: str,
    processed: bool,
) -> None:
    worker = _worker()
    guard, permit = _permit(worker)
    monkeypatch.setattr(
        worker,
        "_claim_media_finalization",
        lambda *_args, **_kwargs: {
            "status": claim_status,
            "claimed": False,
            "lease": None,
        },
    )

    result = _finalize(worker, permit)

    assert result.claim_status == claim_status
    assert result.processed is processed
    assert permit.released is True
    assert guard.snapshot()["active"] == 0


def test_missing_cleans_orphan_before_releasing_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    guard, permit = _permit(worker)
    observed_active: list[int] = []
    monkeypatch.setattr(
        worker,
        "_claim_media_finalization",
        lambda *_args, **_kwargs: {
            "status": "missing",
            "claimed": False,
            "lease": None,
        },
    )

    def cleanup(**_kwargs: Any) -> dict[str, object]:
        observed_active.append(guard.snapshot()["active"])
        return {"status": "deleted", "deleted_bytes": 12}

    monkeypatch.setattr(worker, "_cleanup_orphan_sink_output", cleanup)

    result = _finalize(worker, permit)

    assert result.processed is True
    assert result.cleanup_status == "deleted"
    assert observed_active == [1]
    assert guard.snapshot()["active"] == 0


def test_unexpected_exception_releases_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    guard, permit = _permit(worker)
    monkeypatch.setattr(
        worker,
        "_claim_media_finalization",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    result = _finalize(worker, permit)

    assert result.updated == 0
    assert permit.released is True
    assert guard.snapshot()["active"] == 0


def test_finalizer_exception_persists_failure_before_releasing_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    guard, permit = _permit(worker)
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="finalizer-test",
        token="token-1",
        generation=1,
        phase=worker.MaterializationPhase.FINALIZING.value,
    )
    failures: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worker,
        "_claim_media_finalization",
        lambda *_args, **_kwargs: {
            "status": "claimed",
            "claimed": True,
            "lease": lease,
        },
    )
    monkeypatch.setattr(worker, "_set_event_evidence_state", lambda *_a, **_k: None)
    monkeypatch.setattr(
        worker,
        "_finalize_post_savant_evidence_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad media")),
    )

    def fail(_conn: object, **kwargs: Any) -> bool:
        assert guard.snapshot()["active"] == 1
        failures.append(kwargs)
        return True

    monkeypatch.setattr(worker, "_mark_media_finalize_failed", fail)

    result = _finalize(worker, permit)

    assert result.processed is True
    assert failures[0]["lease"] == lease
    assert guard.snapshot()["active"] == 0


def test_stale_terminal_cas_stops_publish_follow_on_and_releases_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    guard, permit = _permit(worker)
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="finalizer-test",
        token="token-1",
        generation=1,
        phase=worker.MaterializationPhase.FINALIZING.value,
    )
    monkeypatch.setattr(
        worker,
        "_claim_media_finalization",
        lambda *_args, **_kwargs: {
            "status": "claimed",
            "claimed": True,
            "lease": lease,
        },
    )
    monkeypatch.setattr(worker, "_set_event_evidence_state", lambda *_a, **_k: None)
    monkeypatch.setattr(
        worker,
        "_finalize_post_savant_evidence_bundle",
        lambda *_args, **_kwargs: {
            "raw_clip": "/tmp/evidence/raw_clip.mov",
            "metadata": "/tmp/evidence/metadata.json",
            "evidence_dir": "/tmp/evidence",
            "clip_status": "ready",
        },
    )
    monkeypatch.setattr(
        worker,
        "_publish_finalizer_attempt",
        lambda *_args, bundle, **_kwargs: bundle,
    )
    monkeypatch.setattr(worker, "complete_finalizer_task", lambda *_a, **_k: False)
    monkeypatch.setattr(
        worker,
        "_persist_finalized_event_details",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("stale owner reached event projection")
        ),
    )

    result = _finalize(worker, permit)

    assert result.terminal_committed is False
    assert result.processed is False
    assert guard.snapshot()["active"] == 0


def test_success_holds_permit_through_terminal_index_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    guard, permit = _permit(worker)
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="finalizer-test",
        token="token-1",
        generation=1,
        phase=worker.MaterializationPhase.FINALIZING.value,
    )
    call_order: list[str] = []
    monkeypatch.setattr(
        worker,
        "_claim_media_finalization",
        lambda *_args, **_kwargs: {
            "status": "claimed",
            "claimed": True,
            "lease": lease,
        },
    )
    monkeypatch.setattr(worker, "_set_event_evidence_state", lambda *_a, **_k: None)
    monkeypatch.setattr(
        worker,
        "_finalize_post_savant_evidence_bundle",
        lambda *_args, **_kwargs: {
            "raw_clip": "/tmp/evidence/raw_clip.mov",
            "metadata": "/tmp/evidence/metadata.json",
            "evidence_dir": "/tmp/evidence",
            "clip_status": "ready",
            "materialization_metrics": {},
        },
    )
    monkeypatch.setattr(
        worker,
        "_publish_finalizer_attempt",
        lambda *_args, bundle, **_kwargs: bundle,
    )

    def held(name: str, result: object):
        def callback(*_args: Any, **_kwargs: Any):
            assert guard.snapshot()["active"] == 1
            call_order.append(name)
            return result

        return callback

    monkeypatch.setattr(worker, "complete_finalizer_task", held("terminal", True))
    monkeypatch.setattr(worker, "_record_replay_slot_finalization_duration", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "_log_finalize_one_metrics", lambda **_k: None)
    monkeypatch.setattr(
        worker,
        "_persist_finalized_event_details",
        held("event", True),
    )
    monkeypatch.setattr(worker, "_index_finalized_bundle", held("index", True))
    monkeypatch.setattr(
        worker,
        "_attempt_terminal_sink_cleanup",
        held("cleanup", {"status": "deleted", "deleted_bytes": 1}),
    )

    result = _finalize(worker, permit)

    assert result.updated == 1
    assert result.processed is True
    assert result.terminal_committed is True
    assert call_order == ["terminal", "event", "index", "cleanup"]
    assert permit.released is True
    assert guard.snapshot()["active"] == 0


def test_pool_worker_calls_finalize_one_without_recursive_sink_scan() -> None:
    worker = _worker()
    source = inspect.getsource(worker._process_single_finalizer_job)

    assert "_finalize_one(" in source
    assert "_process_sink_output(" not in source
    assert "_MaterializationGuard(1)" not in source


def test_single_finalizer_boundary_defaults_on_and_has_rollback_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    monkeypatch.delenv("MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED", raising=False)
    assert worker._single_finalizer_boundary_enabled() is True
    monkeypatch.setenv("MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED", "false")
    assert worker._single_finalizer_boundary_enabled() is False


def test_rollback_flag_bypasses_v2_pool_even_with_multiple_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    monkeypatch.setenv(
        worker.POST_SAVANT_FINALIZER_ENV,
        worker.POST_SAVANT_REPLAY_EVIDENCE_TOPOLOGY,
    )
    monkeypatch.setenv("MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED", "false")
    monkeypatch.setattr(
        worker,
        "_process_sink_output_with_finalizer_pool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rollback mode entered the V2 finalizer pool")
        ),
    )

    updated = worker._process_sink_output(
        object(),
        "/tmp/sink",
        set(),
        metadata_files_override=[],
        materialization_finalizer_workers=16,
        materialization_database_url="postgresql://unused",
    )

    assert updated == 0


def test_enabled_flag_routes_multiple_workers_to_v2_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    monkeypatch.setenv(
        worker.POST_SAVANT_FINALIZER_ENV,
        worker.POST_SAVANT_REPLAY_EVIDENCE_TOPOLOGY,
    )
    monkeypatch.setenv("MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED", "true")
    monkeypatch.setattr(
        worker,
        "_process_sink_output_with_finalizer_pool",
        lambda *_args, **_kwargs: 7,
    )

    updated = worker._process_sink_output(
        object(),
        "/tmp/sink",
        set(),
        metadata_files_override=[],
        materialization_finalizer_workers=16,
        materialization_database_url="postgresql://unused",
    )

    assert updated == 7


def test_recoverable_handoff_rebuilds_metadata_from_immutable_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = _worker()
    sink_dir = tmp_path / "materialized"
    sink_dir.mkdir()
    video = sink_dir / "video.mov"
    video.write_bytes(b"video")
    metadata_path = sink_dir / "metadata.json"
    metadata_path.write_text(
        json.dumps({"labels": {"event_id": EVENT_ID}}) + "\n",
        encoding="utf-8",
    )
    identity = video.stat()
    monkeypatch.setattr(
        worker,
        "recoverable_finalizer_handoffs",
        lambda *_args, **_kwargs: [
            {
                "event_id": EVENT_ID,
                "sink_output_path": str(sink_dir),
                "source_id": "source-1",
                "runtime_epoch_id": "epoch-1",
                "materialization_lease_generation": 2,
                "materialization_handoff": {
                    "attempt_token": "token-1",
                    "source_id": "source-1",
                    "runtime_epoch_id": "epoch-1",
                    "staging_path": str(sink_dir),
                    "canonical_path": str(video),
                    "size": identity.st_size,
                    "mtime_ns": identity.st_mtime_ns,
                    "device": identity.st_dev,
                    "inode": identity.st_ino,
                },
            }
        ],
    )

    recovered = worker._recoverable_finalizer_metadata(object())

    assert len(recovered) == 1
    assert recovered[0]["_meta_dir"] == str(sink_dir)
    assert recovered[0]["labels"]["event_id"] == EVENT_ID
    assert recovered[0]["_finalizer_phase"]["attempt_token"] == "token-1"


def test_invalid_handoff_is_terminalized_without_deleting_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = _worker()
    video = tmp_path / "video.mov"
    video.write_bytes(b"video")
    failures: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worker,
        "recoverable_finalizer_handoffs",
        lambda *_args, **_kwargs: [
            {
                "event_id": EVENT_ID,
                "sink_output_path": str(tmp_path),
                "source_id": "source-1",
                "runtime_epoch_id": "epoch-1",
                "materialization_handoff": {
                    "attempt_token": "token-invalid",
                    "source_id": "source-1",
                    "runtime_epoch_id": "epoch-1",
                    "staging_path": str(tmp_path),
                    "canonical_path": str(video),
                    "size": 999,
                    "mtime_ns": video.stat().st_mtime_ns,
                },
            }
        ],
    )
    monkeypatch.setattr(
        worker,
        "fail_unclaimed_task",
        lambda _conn, **kwargs: failures.append(kwargs) or True,
    )

    assert worker._recoverable_finalizer_metadata(object()) == []
    assert failures[0]["event_id"] == EVENT_ID
    assert video.exists()


def test_cross_epoch_handoff_is_terminalized_without_deleting_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = _worker()
    video = tmp_path / "video.mov"
    video.write_bytes(b"video")
    identity = video.stat()
    failures: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worker,
        "recoverable_finalizer_handoffs",
        lambda *_args, **_kwargs: [
            {
                "event_id": EVENT_ID,
                "sink_output_path": str(tmp_path),
                "source_id": "source-1",
                "runtime_epoch_id": "epoch-current",
                "materialization_handoff": {
                    "attempt_token": "token-old",
                    "source_id": "source-1",
                    "runtime_epoch_id": "epoch-old",
                    "staging_path": str(tmp_path),
                    "canonical_path": str(video),
                    "size": identity.st_size,
                    "mtime_ns": identity.st_mtime_ns,
                    "device": identity.st_dev,
                    "inode": identity.st_ino,
                },
            }
        ],
    )
    monkeypatch.setattr(
        worker,
        "fail_unclaimed_task",
        lambda _conn, **kwargs: failures.append(kwargs) or True,
    )

    assert worker._recoverable_finalizer_metadata(object()) == []
    assert failures[0]["event_id"] == EVENT_ID
    assert video.read_bytes() == b"video"


def test_cleanup_failure_is_persisted_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    persisted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worker,
        "_cleanup_processed_sink_output",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("device busy")),
    )
    monkeypatch.setattr(
        worker,
        "record_cleanup_outcome",
        lambda _conn, **kwargs: persisted.append(kwargs) or True,
    )

    result = worker._attempt_terminal_sink_cleanup(
        object(),
        event_id=EVENT_ID,
        meta_dir="/tmp/sink/event-1",
        sink_root="/tmp/sink",
        clip_status="ready",
        enabled=True,
        allowed_statuses=("ready",),
    )

    assert result["status"] == "cleanup_pending"
    assert persisted[0]["status"] == "cleanup_pending"
    assert persisted[0]["sink_output_path"] == "/tmp/sink/event-1"


def test_cleanup_recovery_uses_durable_pending_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worker,
        "pending_cleanup_tasks",
        lambda *_args, **_kwargs: [
            {
                "event_id": EVENT_ID,
                "sink_output_path": "/tmp/sink/event-1",
            }
        ],
    )
    monkeypatch.setattr(
        worker,
        "_attempt_terminal_sink_cleanup",
        lambda _conn, **kwargs: calls.append(kwargs)
        or {"status": "deleted", "deleted_bytes": 1},
    )

    recovered = worker._recover_pending_sink_cleanups(
        object(),
        sink_root="/tmp/sink",
        enabled=True,
        allowed_statuses=("ready",),
    )

    assert recovered == 1
    assert calls[0]["event_id"] == EVENT_ID


def test_publish_attempt_renames_atomically_after_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = _worker()
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="finalizer-test",
        token="token-1",
        generation=3,
        phase=worker.MaterializationPhase.FINALIZING.value,
    )
    attempt = worker._finalizer_attempt_dir(
        str(tmp_path),
        event_id=EVENT_ID,
        lease=lease,
    )
    attempt.mkdir(parents=True)
    paths = {
        "raw_clip": attempt / "raw_clip.mov",
        "metadata": attempt / "metadata.json",
        "sink_metadata": attempt / "sink_metadata.json",
        "annotations_jsonl": attempt / "annotations.frame_cache.identity.jsonl",
        "summary": attempt / "summary.json",
    }
    for key, path in paths.items():
        if key in {"metadata", "summary"}:
            path.write_text(
                json.dumps({"path": str(attempt / "raw_clip.mov")}),
                encoding="utf-8",
            )
        else:
            path.write_bytes(b"x")
    bundle = {key: str(path) for key, path in paths.items()}
    bundle["evidence_dir"] = str(attempt)
    monkeypatch.setattr(worker, "heartbeat_lease", lambda *_a, **_k: True)

    published = worker._publish_finalizer_attempt(
        object(),
        lease=lease,
        event_id=EVENT_ID,
        evidence_output_dir=str(tmp_path),
        attempt_dir=attempt,
        bundle=bundle,
        lease_seconds=30,
    )

    canonical = tmp_path / EVENT_ID
    assert published is not None
    assert published["evidence_dir"] == str(canonical)
    assert published["raw_clip"] == str(canonical / "raw_clip.mov")
    assert canonical.is_dir()
    assert not attempt.exists()
    metadata = json.loads((canonical / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["path"] == str(canonical / "raw_clip.mov")


def test_publish_fence_loss_deletes_only_own_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = _worker()
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="stale",
        token="token-stale",
        generation=1,
        phase=worker.MaterializationPhase.FINALIZING.value,
    )
    attempt = worker._finalizer_attempt_dir(
        str(tmp_path),
        event_id=EVENT_ID,
        lease=lease,
    )
    attempt.mkdir(parents=True)
    (attempt / "raw_clip.mov").write_bytes(b"stale")
    canonical = tmp_path / EVENT_ID
    canonical.mkdir()
    (canonical / "raw_clip.mov").write_bytes(b"winner")
    monkeypatch.setattr(worker, "heartbeat_lease", lambda *_a, **_k: False)

    published = worker._publish_finalizer_attempt(
        object(),
        lease=lease,
        event_id=EVENT_ID,
        evidence_output_dir=str(tmp_path),
        attempt_dir=attempt,
        bundle={
            "evidence_dir": str(attempt),
            "raw_clip": str(attempt / "raw_clip.mov"),
        },
        lease_seconds=30,
    )

    assert published is None
    assert not attempt.exists()
    assert (canonical / "raw_clip.mov").read_bytes() == b"winner"


def test_publish_converges_existing_complete_canonical_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    worker = _worker()
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="recovered",
        token="token-recovered",
        generation=4,
        phase=worker.MaterializationPhase.FINALIZING.value,
    )
    attempt = worker._finalizer_attempt_dir(
        str(tmp_path),
        event_id=EVENT_ID,
        lease=lease,
    )
    canonical = tmp_path / EVENT_ID
    attempt.mkdir(parents=True)
    canonical.mkdir()
    names = {
        "raw_clip": "raw_clip.mov",
        "metadata": "metadata.json",
        "sink_metadata": "sink_metadata.json",
        "annotations_jsonl": "annotations.frame_cache.identity.jsonl",
        "summary": "summary.json",
    }
    for name in names.values():
        (attempt / name).write_bytes(b"new-attempt")
        (canonical / name).write_bytes(b"existing-winner")
    bundle = {
        key: str(attempt / name)
        for key, name in names.items()
    }
    bundle["evidence_dir"] = str(attempt)
    monkeypatch.setattr(worker, "heartbeat_lease", lambda *_a, **_k: True)

    published = worker._publish_finalizer_attempt(
        object(),
        lease=lease,
        event_id=EVENT_ID,
        evidence_output_dir=str(tmp_path),
        attempt_dir=attempt,
        bundle=bundle,
        lease_seconds=30,
    )

    assert published is not None
    assert not attempt.exists()
    assert (canonical / "raw_clip.mov").read_bytes() == b"existing-winner"


class _PoolConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _pool_job(worker: Any):
    return worker._FinalizerJob(
        meta={"job_id": "job-1"},
        meta_dir="/tmp/sink/event-1",
        metadata_file="/tmp/sink/event-1/metadata.json",
        event_id=EVENT_ID,
        source_id="source-1",
        replay_shard_id="default",
        worker_id="finalizer-1",
        phase_diagnostics={},
        schedule_row={},
    )


def test_pool_job_uses_shared_guard_and_releases_after_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    conn = _PoolConn()
    guard = worker._MaterializationGuard(1)
    observed_active: list[int] = []
    monkeypatch.setattr(worker.psycopg, "connect", lambda *_a, **_k: conn)
    monkeypatch.setattr(
        worker,
        "_storage_quota_decision",
        lambda **_kwargs: {"overall_level": "normal"},
    )

    def finalize(_conn: object, *, permit: Any, **_kwargs: Any):
        observed_active.append(guard.snapshot()["active"])
        permit.release()
        return worker._FinalizeOneResult(updated=1, processed=True)

    monkeypatch.setattr(worker, "_finalize_one", finalize)

    result = worker._process_single_finalizer_job(
        _pool_job(worker),
        sink_dir="/tmp/sink",
        database_url="postgresql://unused",
        scan_stats={},
        source_lock=worker.Lock(),
        evidence_output_dir="/tmp/evidence",
        sink_scan_max_metadata_files=None,
        materialization_guard=guard,
        materialization_timeout_s=30,
        materialization_max_backlog=0,
        materialization_throttle_sleep_s=0,
        materialization_throttle_deadline_guard_s=0,
        evidence_final_root_max_bytes=0,
        evidence_incoming_root_max_bytes=0,
        replay_sink_output_max_bytes=0,
        evidence_storage_warning_ratio=0.8,
        evidence_storage_critical_ratio=0.9,
        evidence_storage_hard_ratio=1.0,
        cleanup_replay_sink_output_enabled=True,
        cleanup_replay_sink_output_statuses=("ready",),
    )

    assert result == {"updated": 1, "processed": True}
    assert observed_active == [1]
    assert guard.snapshot()["active"] == 0
    assert conn.closed is True


def test_pool_job_does_not_claim_when_shared_guard_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    conn = _PoolConn()
    guard = worker._MaterializationGuard(0)
    retries: list[dict[str, Any]] = []
    monkeypatch.setattr(worker.psycopg, "connect", lambda *_a, **_k: conn)
    monkeypatch.setattr(
        worker,
        "_storage_quota_decision",
        lambda **_kwargs: {"overall_level": "normal"},
    )
    monkeypatch.setattr(
        worker,
        "_mark_media_materialization_retry",
        lambda _conn, **kwargs: retries.append(kwargs) or True,
    )
    monkeypatch.setattr(
        worker,
        "_finalize_one",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("disabled guard reached finalizer claim")
        ),
    )

    result = worker._process_single_finalizer_job(
        _pool_job(worker),
        sink_dir="/tmp/sink",
        database_url="postgresql://unused",
        scan_stats={},
        source_lock=worker.Lock(),
        evidence_output_dir="/tmp/evidence",
        sink_scan_max_metadata_files=None,
        materialization_guard=guard,
        materialization_timeout_s=30,
        materialization_max_backlog=0,
        materialization_throttle_sleep_s=0,
        materialization_throttle_deadline_guard_s=0,
        evidence_final_root_max_bytes=0,
        evidence_incoming_root_max_bytes=0,
        replay_sink_output_max_bytes=0,
        evidence_storage_warning_ratio=0.8,
        evidence_storage_critical_ratio=0.9,
        evidence_storage_hard_ratio=1.0,
        cleanup_replay_sink_output_enabled=True,
        cleanup_replay_sink_output_statuses=("ready",),
    )

    assert result == {"updated": 0, "processed": False}
    assert retries[0]["reason"] == "materialization_concurrency_limit_exceeded"
    assert guard.snapshot()["active"] == 0
    assert conn.closed is True
