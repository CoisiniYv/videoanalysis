"""Spec 33 Phase 4 non-blocking three-lane scheduler contracts."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
import sys
from threading import Event, current_thread
import time
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"
EVENT_ID = "11111111-1111-4111-8111-111111111111"


def test_finalizer_process_pool_uses_spawned_worker_processes() -> None:
    worker = _worker()
    pool = worker._FinalizerProcessPool(2)
    try:
        child_pids = {pool.submit(os.getpid).result(timeout=15) for _ in range(4)}
        snapshot = pool.snapshot()
    finally:
        pool.close(wait_for_running=True, cancel_futures=False)

    assert child_pids
    assert os.getpid() not in child_pids
    assert snapshot["mode"] == "spawn_process_pool"
    assert snapshot["workers"] == 2
    assert snapshot["submitted"] == 4
    assert snapshot["completed"] == 4
    assert snapshot["failed"] == 0


@pytest.fixture(autouse=True)
def _restore_import_state():
    original_path = list(sys.path)
    yield
    sys.path[:] = original_path
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


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


class _FakeProvider:
    def close(self) -> None:
        return None

    def snapshot(self) -> dict[str, object]:
        return {
            "state": "open",
            "max_size": 2,
            "in_use": 0,
            "peak_in_use": 0,
        }


def _resources(worker: Any, *, max_active: int = 2):
    runtime = worker.MaterializationResources(
        database_url="postgresql://unused",
        max_active=max_active,
        image_workers=1,
        remux_workers=1,
        finalizer_workers=1,
        image_queue_capacity=1,
        remux_queue_capacity=1,
        finalizer_queue_capacity=1,
        db_pool_enabled=False,
        source_limit=1,
        reserved_non_image=1,
    )
    runtime.db_pool = _FakeProvider()
    return runtime


def _cfg(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "materialization_finalizer_max_per_source_per_poll": 4,
        "materialization_finalizer_source_serial": False,
        "sink_scan_max_metadata_files": 100,
        "midterm_sink_stability_checks": 2,
        "cleanup_replay_sink_output_enabled": True,
        "replay_sink_output_max_bytes": 0,
        "rolling_cache_sources": (),
        "rolling_cache_materialization_processing_deadline_seconds": 30.0,
        "default_pre_seconds": 5.0,
        "snapshot_output_dir": "/tmp/snapshots",
        "annotated_output_dir": "/tmp/annotations",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_finalizer_discovery(monkeypatch: pytest.MonkeyPatch, worker: Any) -> None:
    monkeypatch.setattr(worker, "_materialization_schedule_rows", lambda *_a: {})
    monkeypatch.setattr(
        worker,
        "_sort_metadata_for_materialization",
        lambda metadata, _rows: list(metadata),
    )
    monkeypatch.setattr(worker, "_is_already_ready", lambda *_a, **_k: False)
    monkeypatch.setattr(worker, "_metadata_source_id", lambda *_a: "source-1")
    monkeypatch.setattr(worker, "_metadata_replay_shard_id", lambda *_a: "default")


def test_scheduler_v2_finalizer_admission_returns_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    scheduler = worker._FinalizerSchedulerV2(
        cfg=_cfg(),
        runtime_resources=runtime,
    )
    _patch_finalizer_discovery(monkeypatch, worker)
    started = Event()
    release = Event()

    def slow_job(*_args: object, **_kwargs: object) -> dict[str, object]:
        started.set()
        assert release.wait(timeout=2)
        return {"updated": 1, "processed": True, "status": "finalized"}

    monkeypatch.setattr(worker, "_run_finalizer_admission_v2", slow_job)
    processed: set[str] = set()
    before = time.monotonic()
    admitted = scheduler.admit_metadata(
        object(),
        sink_dir="/tmp/sink",
        metadata_files=[{"event_id": EVENT_ID, "_meta_dir": "/tmp/sink/event"}],
        scan_stats={},
        processed_dirs=processed,
        processed_state_path=None,
        candidate_dirs={},
        invalid_output_failures={},
        stability_checks=2,
        cleanup_replay_sink_output_enabled=True,
        replay_sink_output_max_bytes=0,
    )
    elapsed = time.monotonic() - before

    assert admitted == 1
    assert elapsed < 0.2
    assert started.wait(timeout=1)
    assert scheduler.snapshot()["active"] == 1
    assert runtime.work_budget.snapshot()["active"] == 1

    release.set()
    deadline = time.monotonic() + 2
    while scheduler.snapshot()["active"] and time.monotonic() < deadline:
        scheduler.drain_completed(object())
        time.sleep(0.01)
    assert scheduler.snapshot()["active"] == 0
    assert scheduler.snapshot()["updated_total"] == 1
    assert "/tmp/sink/event" in processed
    assert runtime.work_budget.snapshot()["active"] == 0
    runtime.close(wait=True)


def test_lifecycle_recovery_is_global_and_ignores_current_admission_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    observed: list[tuple[str, ...]] = []

    class Result:
        changed = 4
        ready_deadline_expired = 0
        running_sla_missed = 0
        handoff_recovered = 4
        lease_retry_scheduled = 0
        lease_deadline_expired = 0

    monkeypatch.setattr(
        worker,
        "recover_and_expire_rolling_tasks",
        lambda _conn, *, source_ids: observed.append(source_ids) or Result(),
    )

    updated = worker._recover_rolling_cache_lifecycle(
        object(),
        SimpleNamespace(rolling_cache_sources=("currently-admitted-only",)),
    )

    assert updated == 4
    assert observed == [()]


def test_rolling_off_still_runs_common_recovery_without_duplicate_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    calls: list[str] = []
    monkeypatch.setattr(
        worker,
        "_recover_rolling_cache_lifecycle",
        lambda *_a, **_k: calls.append("recovery") or 2,
    )
    cfg = SimpleNamespace(
        rolling_cache_enabled=False,
        rolling_cache_materialization_enabled=False,
    )

    assert worker._process_rolling_cache_tasks(object(), cfg) == 2
    assert calls == ["recovery"]
    assert (
        worker._process_rolling_cache_tasks(
            object(),
            cfg,
            recover_lifecycle=False,
        )
        == 0
    )
    assert calls == ["recovery"]


def test_run_loop_places_common_recovery_before_scheduler_flag_branches() -> None:
    worker = _worker()
    source = inspect.getsource(worker.run_worker)

    recovery_call = source.index(
        "recovery_updates = _recover_rolling_cache_lifecycle("
    )
    v2_branch = source.index("if scheduler_v2_enabled:")
    legacy_call = source.index("recover_lifecycle=False")

    assert recovery_call < v2_branch < legacy_call
    assert "lifecycle_recovery_due" in source


def test_finalizer_probe_and_stability_are_not_in_admission_thread() -> None:
    worker = _worker()
    admission_source = inspect.getsource(worker._FinalizerSchedulerV2.admit_metadata)
    execution_source = inspect.getsource(worker._run_finalizer_admission_v2)

    assert "_find_video_file(" not in admission_source
    assert "_sink_output_ready_for_finalizer(" not in admission_source
    assert "wait(" not in admission_source
    assert ".result(" not in admission_source
    assert "_find_video_file(" in execution_source
    assert "_sink_output_ready_for_finalizer(" in execution_source
    assert "_process_single_finalizer_job(" in execution_source


def test_finalizer_submit_failure_releases_every_reservation_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    scheduler = worker._FinalizerSchedulerV2(
        cfg=_cfg(),
        runtime_resources=runtime,
    )
    _patch_finalizer_discovery(monkeypatch, worker)
    retries: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scheduler,
        "_schedule_submit_retry",
        lambda _conn, *, event_id, reason, **_kwargs: retries.append(
            (event_id, reason)
        ),
    )
    monkeypatch.setattr(
        runtime.finalizer_lane,
        "_executor_submit",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("executor stopped")),
    )
    transferred = runtime.work_budget.try_acquire("remux", owner=EVENT_ID)
    assert transferred is not None
    transfer = worker._FinalizerHandoffTransfer(
        lease=worker.MaterializationLease(
            event_id=EVENT_ID,
            owner="remux-v2",
            token="token-1",
            generation=1,
            phase=worker.MaterializationPhase.FINALIZER_PENDING.value,
        ),
        work_permit=transferred,
    )

    admitted = scheduler.admit_metadata(
        object(),
        sink_dir="/tmp/sink",
        metadata_files=[{"event_id": EVENT_ID, "_meta_dir": "/tmp/sink/event"}],
        scan_stats={},
        processed_dirs=set(),
        processed_state_path=None,
        candidate_dirs=None,
        invalid_output_failures=None,
        stability_checks=1,
        cleanup_replay_sink_output_enabled=True,
        replay_sink_output_max_bytes=0,
        transferred_handoffs={EVENT_ID: transfer},
    )

    assert admitted == 0
    assert retries == [(EVENT_ID, "finalizer_lane_submit_failed:RuntimeError")]
    assert runtime.work_budget.snapshot()["active"] == 0
    assert runtime.source_slots.snapshot()["active"] == 0
    assert runtime.finalizer_lane.snapshot()["reserved"] == 0
    runtime.close(wait=True)


def test_finalizer_discovery_exception_releases_transferred_remux_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    scheduler = worker._FinalizerSchedulerV2(
        cfg=_cfg(),
        runtime_resources=runtime,
    )
    transferred = runtime.work_budget.try_acquire("remux", owner=EVENT_ID)
    assert transferred is not None
    transfers = {
        EVENT_ID: worker._FinalizerHandoffTransfer(
            lease=worker.MaterializationLease(
                event_id=EVENT_ID,
                owner="remux-v2",
                token="token-1",
                generation=1,
                phase=worker.MaterializationPhase.FINALIZER_PENDING.value,
            ),
            work_permit=transferred,
        )
    }
    retries: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scheduler,
        "_schedule_submit_retry",
        lambda _conn, *, event_id, reason, **_kwargs: retries.append(
            (event_id, reason)
        )
        or True,
    )
    monkeypatch.setattr(
        worker,
        "_materialization_schedule_rows",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )

    with pytest.raises(RuntimeError, match="db unavailable"):
        scheduler.admit_metadata(
            object(),
            sink_dir="/tmp/sink",
            metadata_files=[
                {"event_id": EVENT_ID, "_meta_dir": "/tmp/sink/event"}
            ],
            scan_stats={},
            processed_dirs=set(),
            processed_state_path=None,
            candidate_dirs=None,
            invalid_output_failures=None,
            stability_checks=1,
            cleanup_replay_sink_output_enabled=True,
            replay_sink_output_max_bytes=0,
            transferred_handoffs=transfers,
        )

    assert transfers == {}
    assert retries == [
        (EVENT_ID, "temporary_io_error:finalizer_admission_exception")
    ]
    assert transferred.released is True
    assert runtime.work_budget.snapshot()["active"] == 0
    runtime.close(wait=True)


def test_finalizer_lane_full_fenced_retries_every_transferred_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker, max_active=4)
    scheduler = worker._FinalizerSchedulerV2(
        cfg=_cfg(),
        runtime_resources=runtime,
    )
    _patch_finalizer_discovery(monkeypatch, worker)
    held_lane_slots = [
        runtime.finalizer_lane.try_reserve(),
        runtime.finalizer_lane.try_reserve(),
    ]
    assert all(slot is not None for slot in held_lane_slots)
    event_ids = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    ]
    transfers: dict[str, Any] = {}
    for index, event_id in enumerate(event_ids, start=1):
        permit = runtime.work_budget.try_acquire("remux", owner=event_id)
        assert permit is not None
        transfers[event_id] = worker._FinalizerHandoffTransfer(
            lease=worker.MaterializationLease(
                event_id=event_id,
                owner="remux-v2",
                token=f"token-{index}",
                generation=index,
                phase=worker.MaterializationPhase.FINALIZER_PENDING.value,
            ),
            work_permit=permit,
        )
    retries: list[tuple[str, str, str, bool]] = []

    def retry(
        _conn: object,
        *,
        event_id: str,
        reason: str,
        exact_lease: object = None,
        durable_handoff: bool = False,
    ) -> bool:
        retries.append(
            (
                event_id,
                reason,
                str(getattr(exact_lease, "token", "")),
                durable_handoff,
            )
        )
        return True

    monkeypatch.setattr(scheduler, "_schedule_submit_retry", retry)
    metadata = [
        {
            "event_id": event_id,
            "_meta_dir": f"/tmp/sink/{event_id}",
            "_finalizer_phase": {"handoff_persisted_at": "2026-07-20T00:00:00+00:00"},
        }
        for event_id in event_ids
    ]

    assert scheduler.admit_metadata(
        object(),
        sink_dir="/tmp/sink",
        metadata_files=metadata,
        scan_stats={},
        processed_dirs=set(),
        processed_state_path=None,
        candidate_dirs=None,
        invalid_output_failures=None,
        stability_checks=1,
        cleanup_replay_sink_output_enabled=True,
        replay_sink_output_max_bytes=0,
        transferred_handoffs=transfers,
    ) == 0

    assert transfers == {}
    assert [item[0] for item in retries] == event_ids
    assert all(item[1].startswith("capacity_unavailable:") for item in retries)
    assert [item[2] for item in retries] == ["token-1", "token-2", "token-3"]
    assert all(item[3] for item in retries)
    assert runtime.work_budget.snapshot()["active"] == 0
    assert scheduler.snapshot()["admission_rejected_total"] == 1
    for slot in held_lane_slots:
        assert slot is not None
        slot.cancel()
    runtime.close(wait=True)


@pytest.mark.parametrize("skip_reason", ("processed", "invalid", "already_ready"))
def test_finalizer_skip_paths_fenced_retry_transferred_handoff_before_wip_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skip_reason: str,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    scheduler = worker._FinalizerSchedulerV2(
        cfg=_cfg(),
        runtime_resources=runtime,
    )
    _patch_finalizer_discovery(monkeypatch, worker)
    meta_dir = tmp_path / "sink" / EVENT_ID
    processed_dirs = {str(meta_dir)} if skip_reason == "processed" else set()
    if skip_reason == "invalid":
        monkeypatch.setattr(
            worker,
            "_invalid_sink_output_marker_path",
            lambda _path: tmp_path,
        )
    if skip_reason == "already_ready":
        monkeypatch.setattr(worker, "_is_already_ready", lambda *_a, **_k: True)
        monkeypatch.setattr(worker, "_clear_sink_phase", lambda *_a, **_k: None)

    permit = runtime.work_budget.try_acquire("remux", owner=EVENT_ID)
    assert permit is not None
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="remux-v2",
        token="token-1",
        generation=1,
        phase=worker.MaterializationPhase.FINALIZER_PENDING.value,
    )
    transfers = {
        EVENT_ID: worker._FinalizerHandoffTransfer(
            lease=lease,
            work_permit=permit,
        )
    }
    observed: list[tuple[object, bool]] = []

    def retry(
        _conn: object,
        *,
        exact_lease: object = None,
        durable_handoff: bool = False,
        **_kwargs: object,
    ) -> bool:
        # The permit must remain held until the exact fenced CAS is attempted.
        observed.append((exact_lease, permit.released))
        return True

    monkeypatch.setattr(scheduler, "_schedule_submit_retry", retry)

    assert scheduler.admit_metadata(
        object(),
        sink_dir=str(tmp_path / "sink"),
        metadata_files=[{"event_id": EVENT_ID, "_meta_dir": str(meta_dir)}],
        scan_stats={},
        processed_dirs=processed_dirs,
        processed_state_path=None,
        candidate_dirs=None,
        invalid_output_failures=None,
        stability_checks=1,
        cleanup_replay_sink_output_enabled=True,
        replay_sink_output_max_bytes=0,
        transferred_handoffs=transfers,
    ) == 0

    assert transfers == {}
    assert observed == [(lease, False)]
    assert permit.released is True
    assert runtime.work_budget.snapshot()["active"] == 0
    runtime.close(wait=True)


def test_finalizer_submit_retry_preserves_unleased_handoff_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    scheduler = worker._FinalizerSchedulerV2(
        cfg=_cfg(),
        runtime_resources=runtime,
    )
    observed: list[tuple[str, str]] = []
    monkeypatch.setattr(worker, "current_lease", lambda *_a, **_k: None)
    monkeypatch.setattr(
        worker,
        "retry_unclaimed_finalizer_handoff",
        lambda _conn, *, event_id, reason, **_kwargs: observed.append(
            (event_id, reason)
        )
        or True,
    )
    monkeypatch.setattr(
        worker,
        "schedule_unclaimed_retry",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("durable handoff was demoted to waiting_ready")
        ),
    )

    assert scheduler._schedule_submit_retry(
        object(),
        event_id=EVENT_ID,
        reason="capacity_unavailable:finalizer_lane_full",
        durable_handoff=True,
    ) is True
    assert observed == [
        (EVENT_ID, "capacity_unavailable:finalizer_lane_full")
    ]
    runtime.close(wait=True)


def test_finalizer_lane_exception_schedules_retry_and_releases_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    scheduler = worker._FinalizerSchedulerV2(
        cfg=_cfg(),
        runtime_resources=runtime,
    )
    _patch_finalizer_discovery(monkeypatch, worker)
    retries: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scheduler,
        "_schedule_submit_retry",
        lambda _conn, *, event_id, reason, **_kwargs: retries.append(
            (event_id, reason)
        )
        or True,
    )
    monkeypatch.setattr(
        worker,
        "_run_finalizer_admission_v2",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("worker failed")),
    )

    assert scheduler.admit_metadata(
        object(),
        sink_dir="/tmp/sink",
        metadata_files=[{"event_id": EVENT_ID, "_meta_dir": "/tmp/sink/event"}],
        scan_stats={},
        processed_dirs=set(),
        processed_state_path=None,
        candidate_dirs=None,
        invalid_output_failures=None,
        stability_checks=1,
        cleanup_replay_sink_output_enabled=True,
        replay_sink_output_max_bytes=0,
    ) == 1
    deadline = time.monotonic() + 2
    while scheduler.snapshot()["active"] and time.monotonic() < deadline:
        scheduler.drain_completed(object())
        time.sleep(0.01)

    assert retries == [(EVENT_ID, "finalizer_lane_job_failed:RuntimeError")]
    assert scheduler.snapshot()["active"] == 0
    assert runtime.work_budget.snapshot()["active"] == 0
    assert runtime.source_slots.snapshot()["active"] == 0
    assert runtime.finalizer_lane.snapshot()["reserved"] == 0
    runtime.close(wait=True)


def test_finalizer_retry_scheduler_normalizes_unknown_reason_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    scheduler = worker._FinalizerSchedulerV2(
        cfg=_cfg(),
        runtime_resources=runtime,
    )
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="finalizer-v2-1",
        token="token-1",
        generation=1,
        phase=worker.MaterializationPhase.FINALIZING.value,
    )
    observed: list[str] = []
    monkeypatch.setattr(worker, "current_lease", lambda *_a, **_k: lease)
    monkeypatch.setattr(
        worker,
        "retry_finalizer_handoff",
        lambda *_a, reason, **_k: observed.append(reason) or True,
    )

    assert scheduler._schedule_submit_retry(
        object(),
        event_id=EVENT_ID,
        reason="media_worker_shutdown_forced",
    ) is True
    assert observed == ["temporary_io_error:media_worker_shutdown_forced"]
    runtime.close(wait=True)


def test_finalizer_force_stop_fences_running_and_queued_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker, max_active=2)
    scheduler = worker._FinalizerSchedulerV2(
        cfg=_cfg(),
        runtime_resources=runtime,
    )
    _patch_finalizer_discovery(monkeypatch, worker)
    monkeypatch.setattr(
        worker,
        "_metadata_source_id",
        lambda meta, _row: str(meta.get("source_id") or "unknown"),
    )
    started = Event()
    release = Event()

    def slow_job(*_args: object, **_kwargs: object) -> dict[str, object]:
        started.set()
        assert release.wait(timeout=2)
        return {"updated": 1, "processed": True, "status": "finalized"}

    monkeypatch.setattr(worker, "_run_finalizer_admission_v2", slow_job)
    retries: list[str] = []
    monkeypatch.setattr(
        scheduler,
        "_schedule_submit_retry",
        lambda _conn, *, event_id, reason, **_kwargs: retries.append(event_id)
        or True,
    )
    second_event = "22222222-2222-4222-8222-222222222222"
    metadata = [
        {
            "event_id": EVENT_ID,
            "source_id": "source-1",
            "_meta_dir": "/tmp/sink/event-1",
        },
        {
            "event_id": second_event,
            "source_id": "source-2",
            "_meta_dir": "/tmp/sink/event-2",
        },
    ]

    assert scheduler.admit_metadata(
        object(),
        sink_dir="/tmp/sink",
        metadata_files=metadata,
        scan_stats={},
        processed_dirs=set(),
        processed_state_path=None,
        candidate_dirs=None,
        invalid_output_failures=None,
        stability_checks=1,
        cleanup_replay_sink_output_enabled=True,
        replay_sink_output_max_bytes=0,
    ) == 2
    assert started.wait(timeout=1)
    scheduler.force_stop(object())

    assert set(retries) == {EVENT_ID, second_event}
    assert scheduler.snapshot()["active"] == 0
    assert scheduler.snapshot()["active_events"] == 0
    assert runtime.work_budget.snapshot()["active"] == 0
    assert runtime.source_slots.snapshot()["active"] == 0
    release.set()
    runtime.close(wait=True)
    assert runtime.finalizer_lane.snapshot()["reserved"] == 0


def test_rolling_image_reserves_lane_source_and_budget_before_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    scheduler = worker._ImageSchedulerV2(cfg=_cfg(), runtime_resources=runtime)
    row = {
        "event_id": EVENT_ID,
        "source_id": "source-1",
        "rolling_cache_ready_at": object(),
    }
    monkeypatch.setattr(
        worker,
        "_rolling_cache_image_candidate_tasks",
        lambda *_a, **_k: [row],
    )
    claim_snapshots: list[tuple[int, int, int]] = []

    def claim(*_args: object, **_kwargs: object):
        claim_snapshots.append(
            (
                int(runtime.image_lane.snapshot()["reserved"]),
                runtime.source_slots.snapshot()["active"],
                runtime.work_budget.snapshot()["active"],
            )
        )
        return worker.MaterializationLease(
            event_id=EVENT_ID,
            owner="image-v2",
            token="token-1",
            generation=1,
            phase=worker.MaterializationPhase.IMAGE_RUNNING.value,
        )

    monkeypatch.setattr(worker, "_claim_rolling_cache_task", claim)
    release = Event()
    monkeypatch.setattr(
        worker,
        "_run_rolling_image_job_v2",
        lambda **_kwargs: (
            release.wait(timeout=2)
            and {"updated": 1, "status": "materialized"}
        ),
    )

    assert scheduler.admit_rolling_images(object()) == 1
    assert claim_snapshots == [(1, 1, 1)]
    release.set()
    deadline = time.monotonic() + 2
    while scheduler.snapshot()["active"] and time.monotonic() < deadline:
        scheduler.drain_completed(object())
        time.sleep(0.01)
    assert runtime.work_budget.snapshot()["active"] == 0
    assert runtime.source_slots.snapshot()["active"] == 0
    runtime.close(wait=True)


def test_rolling_image_submit_and_retry_write_failures_release_all_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    scheduler = worker._ImageSchedulerV2(cfg=_cfg(), runtime_resources=runtime)
    row = {
        "event_id": EVENT_ID,
        "source_id": "source-1",
        "rolling_cache_ready_at": object(),
    }
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="image-v2",
        token="token-1",
        generation=1,
        phase=worker.MaterializationPhase.IMAGE_RUNNING.value,
    )
    monkeypatch.setattr(
        worker,
        "_rolling_cache_image_candidate_tasks",
        lambda *_a, **_k: [row],
    )
    monkeypatch.setattr(worker, "_claim_rolling_cache_task", lambda *_a, **_k: lease)
    monkeypatch.setattr(
        runtime.image_lane,
        "_executor_submit",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("executor stopped")),
    )
    monkeypatch.setattr(
        worker,
        "_defer_rolling_cache_task",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )

    assert scheduler.admit_rolling_images(object()) == 0
    assert runtime.work_budget.snapshot()["active"] == 0
    assert runtime.source_slots.snapshot()["active"] == 0
    assert runtime.image_lane.snapshot()["reserved"] == 0
    runtime.close(wait=True)


def test_remux_submit_and_retry_write_failures_release_all_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    runner = worker._RollingCacheMaterializationRunner(
        max_workers=1,
        runtime_resources=runtime,
    )
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="remux-v2",
        token="token-1",
        generation=1,
        phase=worker.MaterializationPhase.REMUX_RUNNING.value,
    )
    cfg = _cfg(
        rolling_cache_materialization_max_per_poll=1,
        rolling_cache_root="/tmp/rolling",
        rolling_cache_materialized_root="/tmp/materialized",
    )
    monkeypatch.setattr(
        worker,
        "_rolling_cache_candidate_tasks",
        lambda *_a, **_k: [{"event_id": EVENT_ID, "source_id": "source-1"}],
    )
    monkeypatch.setattr(
        worker,
        "_prepare_rolling_cache_job",
        lambda *_a, **_k: {
            "event_id": EVENT_ID,
            "source_id": "source-1",
            "lease": lease,
        },
    )
    monkeypatch.setattr(
        runtime.remux_lane,
        "_executor_submit",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("executor stopped")),
    )
    monkeypatch.setattr(
        worker,
        "_defer_rolling_cache_task",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )

    assert runner.process(object(), cfg) == 0
    assert runtime.work_budget.snapshot()["active"] == 0
    assert runtime.source_slots.snapshot()["active"] == 0
    assert runtime.remux_lane.snapshot()["reserved"] == 0
    runner.close()
    runtime.close(wait=True)


@pytest.mark.parametrize("indexed", (False, True))
def test_prepare_rolling_job_defers_index_discovery_to_remux_worker(
    monkeypatch: pytest.MonkeyPatch,
    indexed: bool,
) -> None:
    worker = _worker()
    lease = worker.MaterializationLease(
        event_id=EVENT_ID,
        owner="remux-v2",
        token="token-1",
        generation=1,
        phase=worker.MaterializationPhase.REMUX_RUNNING.value,
    )
    monkeypatch.setattr(worker, "_is_already_ready", lambda *_a: False)
    monkeypatch.setattr(worker, "_claim_rolling_cache_task", lambda *_a, **_k: lease)
    monkeypatch.setattr(
        worker,
        "_load_event_context",
        lambda *_a: {"event_id": EVENT_ID},
    )
    monkeypatch.setattr(
        worker,
        "_rolling_cache_window",
        lambda *_a: (1_000, 2_000, 1_500),
    )
    monkeypatch.setattr(
        worker,
        "_runtime_epoch_from_event_context",
        lambda *_a: "epoch-1",
    )
    monkeypatch.setattr(worker, "_rolling_cache_labels", lambda **_k: {"ok": True})
    segment_lookups: list[object] = []
    legacy_segments = [object()]

    def find_segments(**kwargs: object) -> list[object]:
        segment_lookups.append(kwargs)
        return legacy_segments

    monkeypatch.setattr(worker, "_find_rolling_segments", find_segments)
    index = object() if indexed else None
    job = worker._prepare_rolling_cache_job(
        object(),
        _cfg(
            rolling_cache_sources=(),
            sink_output_dir="/tmp/sink",
        ),
        {"event_id": EVENT_ID, "source_id": "source-1"},
        segment_cache={},
        segment_index=index,
    )

    assert job is not None
    if indexed:
        assert job["segments"] is None
        assert segment_lookups == []
    else:
        assert job["segments"] is legacy_segments
        assert len(segment_lookups) == 1
        assert segment_lookups[0]["segment_index"] is None


def test_legacy_derivative_flag_keeps_rolling_images_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    scheduler = worker._ImageSchedulerV2(cfg=_cfg(), runtime_resources=runtime)
    rolling_queries: list[object] = []

    monkeypatch.delenv("MEDIA_WORKER_LEGACY_DERIVATIVES_ENABLED", raising=False)
    assert worker._legacy_derivatives_enabled() is True
    monkeypatch.setenv("MEDIA_WORKER_LEGACY_DERIVATIVES_ENABLED", "false")
    monkeypatch.delenv("EVIDENCE_DB_INDEX_EXPANDED_ROWS_ENABLED", raising=False)
    monkeypatch.setattr(
        worker,
        "_rolling_cache_image_candidate_tasks",
        lambda conn, *_a, **_k: rolling_queries.append(conn) or [],
    )
    monkeypatch.setattr(
        worker,
        "_mark_not_required",
        lambda *_a: (_ for _ in ()).throw(
            AssertionError("legacy snapshot convergence must be skipped")
        ),
    )
    monkeypatch.setattr(
        worker,
        "_snapshot_needed",
        lambda *_a: (_ for _ in ()).throw(
            AssertionError("legacy snapshot discovery must be skipped")
        ),
    )
    monkeypatch.setattr(
        worker,
        "_annotation_needed",
        lambda *_a: (_ for _ in ()).throw(
            AssertionError("legacy annotation discovery must be skipped")
        ),
    )

    connection = object()
    assert scheduler.admit_rolling_images(connection) == 0
    assert rolling_queries == [connection]
    assert scheduler.admit_snapshots(connection) == 0
    assert scheduler.admit_annotations(connection) == 0
    assert worker._evidence_db_index_expanded_rows_enabled() is True
    runtime.close(wait=True)


@pytest.mark.parametrize(
    ("kind", "candidate_name", "admit_name", "runner_name"),
    (
        ("snapshot", "_snapshot_needed", "admit_snapshots", "_run_snapshot_job_v2"),
        (
            "annotation",
            "_annotation_needed",
            "admit_annotations",
            "_run_annotation_job_v2",
        ),
    ),
)
def test_snapshot_and_annotation_execute_on_image_lane(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    candidate_name: str,
    admit_name: str,
    runner_name: str,
) -> None:
    worker = _worker()
    runtime = _resources(worker)
    scheduler = worker._ImageSchedulerV2(cfg=_cfg(), runtime_resources=runtime)
    row = {
        "event_id": EVENT_ID,
        "clip_path": "/tmp/raw.mov",
        "snapshot_path": "/tmp/raw.jpg",
        "payload": {},
    }
    if kind == "snapshot":
        monkeypatch.setattr(worker, "_mark_not_required", lambda *_a: 0)
    monkeypatch.setattr(worker, candidate_name, lambda *_a: [row])
    thread_names: list[str] = []
    release = Event()

    def job(**_kwargs: object) -> dict[str, object]:
        thread_names.append(current_thread().name)
        assert release.wait(timeout=2)
        return {"updated": 1, "status": "ready"}

    monkeypatch.setattr(worker, runner_name, job)
    before = time.monotonic()
    assert getattr(scheduler, admit_name)(object()) == 1
    assert time.monotonic() - before < 0.2
    deadline = time.monotonic() + 1
    while not thread_names and time.monotonic() < deadline:
        time.sleep(0.01)
    assert thread_names and thread_names[0].startswith("media-image")
    release.set()
    deadline = time.monotonic() + 2
    while scheduler.snapshot()["active"] and time.monotonic() < deadline:
        scheduler.drain_completed(object())
        time.sleep(0.01)
    runtime.close(wait=True)


def test_scheduler_v2_has_no_unbounded_executor_or_batch_wait_contract() -> None:
    worker = _worker()
    runner_source = inspect.getsource(worker._RollingCacheMaterializationRunner)
    scheduler_source = inspect.getsource(worker._FinalizerSchedulerV2)
    image_source = inspect.getsource(worker._ImageSchedulerV2)

    assert "ThreadPoolExecutor(" not in scheduler_source
    assert "ThreadPoolExecutor(" not in image_source
    assert "as_completed(" not in scheduler_source
    assert "wait(" not in scheduler_source
    assert "as_completed(" not in image_source
    assert "finalizer_scheduler_v2.admit_metadata(" in runner_source

    compose = (ROOT / "infra" / "docker-compose.midterm.yml").read_text(
        encoding="utf-8"
    )
    env_contract = (ROOT / "infra" / "env" / "midterm.env").read_text(
        encoding="utf-8"
    )
    assert (
        'MEDIA_WORKER_SCHEDULER_V2_ENABLED: '
        '"${MEDIA_WORKER_SCHEDULER_V2_ENABLED:-true}"'
    ) in compose
    assert (
        'MEDIA_WORKER_DB_POOL_ENABLED: '
        '"${MEDIA_WORKER_DB_POOL_ENABLED:-true}"'
    ) in compose
    assert "MEDIA_WORKER_SCHEDULER_V2_ENABLED=true" in env_contract
    assert "MEDIA_WORKER_DB_POOL_ENABLED=true" in env_contract


def test_image_burst_cannot_consume_reserved_video_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()
    runtime = worker.MaterializationResources(
        database_url="postgresql://unused",
        max_active=4,
        image_workers=4,
        remux_workers=1,
        finalizer_workers=1,
        image_queue_capacity=4,
        finalizer_queue_capacity=1,
        db_pool_enabled=False,
        source_limit=1,
        reserved_non_image=1,
    )
    runtime.db_pool = _FakeProvider()
    image_scheduler = worker._ImageSchedulerV2(
        cfg=_cfg(),
        runtime_resources=runtime,
    )
    finalizer_scheduler = worker._FinalizerSchedulerV2(
        cfg=_cfg(),
        runtime_resources=runtime,
    )
    release = Event()
    snapshot_rows = [
        {
            "event_id": f"00000000-0000-4000-8000-{index:012d}",
            "clip_path": "/tmp/raw.mov",
        }
        for index in range(1, 120)
    ]
    monkeypatch.setattr(worker, "_mark_not_required", lambda *_a: 0)
    monkeypatch.setattr(worker, "_snapshot_needed", lambda *_a: snapshot_rows)
    monkeypatch.setattr(
        worker,
        "_run_snapshot_job_v2",
        lambda **_kwargs: (
            release.wait(timeout=2)
            and {"updated": 1, "status": "ready"}
        ),
    )
    _patch_finalizer_discovery(monkeypatch, worker)
    monkeypatch.setattr(
        worker,
        "_run_finalizer_admission_v2",
        lambda *_a, **_k: (
            release.wait(timeout=2)
            and {"updated": 1, "processed": True, "status": "finalized"}
        ),
    )

    assert image_scheduler.admit_snapshots(object()) == 3
    assert runtime.work_budget.snapshot() == {
        "max_active": 4,
        "active": 3,
        "image_active": 3,
        "remux_active": 0,
        "finalizer_active": 0,
        "other_active": 0,
        "reserved_non_image": 1,
    }
    assert finalizer_scheduler.admit_metadata(
        object(),
        sink_dir="/tmp/sink",
        metadata_files=[{"event_id": EVENT_ID, "_meta_dir": "/tmp/sink/video"}],
        scan_stats={},
        processed_dirs=set(),
        processed_state_path=None,
        candidate_dirs=None,
        invalid_output_failures=None,
        stability_checks=1,
        cleanup_replay_sink_output_enabled=True,
        replay_sink_output_max_bytes=0,
    ) == 1
    assert runtime.work_budget.snapshot()["active"] == 4
    assert runtime.work_budget.snapshot()["finalizer_active"] == 1

    release.set()
    deadline = time.monotonic() + 2
    while (
        image_scheduler.snapshot()["active"]
        or finalizer_scheduler.snapshot()["active"]
    ) and time.monotonic() < deadline:
        image_scheduler.drain_completed(object())
        finalizer_scheduler.drain_completed(object())
        time.sleep(0.01)
    assert runtime.work_budget.snapshot()["active"] == 0
    runtime.close(wait=True)
