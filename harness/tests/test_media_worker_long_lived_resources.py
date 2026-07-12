"""Spec 33 Phase 3 long-lived resource and shutdown contracts."""

from __future__ import annotations

from concurrent.futures import Future
from contextlib import contextmanager
import sys
from pathlib import Path
from threading import Event
from threading import Thread
import time
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"


@pytest.fixture(autouse=True)
def _restore_import_state():
    original_path = list(sys.path)
    yield
    sys.path[:] = original_path
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _resources_module():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    root = str(MEDIA_WORKER_ROOT)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    import app.materialization_scheduler as resources

    return resources


def test_work_budget_is_global_move_only_and_reserves_non_image_capacity() -> None:
    resources = _resources_module()
    budget = resources.WorkBudget(max_active=4, reserved_non_image=1)

    images = [budget.try_acquire("image", owner=f"image-{index}") for index in range(3)]
    assert all(permit is not None for permit in images)
    assert budget.try_acquire("image", owner="image-overflow") is None

    remux = budget.try_acquire("remux", owner="video-1")
    assert remux is not None
    assert budget.try_acquire("finalizer", owner="overflow") is None
    remux.move_to("finalizer")

    snapshot = budget.snapshot()
    assert snapshot["active"] == 4
    assert snapshot["image_active"] == 3
    assert snapshot["remux_active"] == 0
    assert snapshot["finalizer_active"] == 1

    remux.release()
    remux.release()
    for permit in images:
        assert permit is not None
        permit.release()
    assert budget.snapshot()["active"] == 0


def test_work_budget_zero_disables_all_materialization() -> None:
    resources = _resources_module()
    budget = resources.WorkBudget(max_active=0)

    assert budget.try_acquire("image", owner="disabled") is None
    assert budget.try_acquire("remux", owner="disabled") is None
    assert budget.snapshot() == {
        "max_active": 0,
        "active": 0,
        "image_active": 0,
        "remux_active": 0,
        "finalizer_active": 0,
        "other_active": 0,
        "reserved_non_image": 0,
    }


def test_bounded_lane_reserves_capacity_before_submit_and_reuses_one_executor() -> None:
    resources = _resources_module()
    lane = resources.BoundedExecutorLane(
        name="finalizer",
        max_workers=1,
        queue_capacity=1,
    )
    first_started = Event()
    release_first = Event()

    def first_job() -> str:
        first_started.set()
        assert release_first.wait(timeout=2)
        return "first"

    first = lane.try_reserve()
    second = lane.try_reserve()
    assert first is not None
    assert second is not None
    assert lane.try_reserve() is None

    first_future = first.submit(first_job)
    assert first_started.wait(timeout=2)
    second_future = second.submit(lambda: "second")
    snapshot = lane.snapshot()
    assert snapshot["capacity"] == 2
    assert snapshot["reserved"] <= 2
    assert snapshot["executor_create_count"] == 1

    release_first.set()
    assert first_future.result(timeout=2) == "first"
    assert second_future.result(timeout=2) == "second"
    assert lane.snapshot()["reserved"] == 0

    third = lane.try_reserve()
    assert third is not None
    third.cancel()
    lane.close(wait=True)
    assert lane.snapshot()["state"] == "closed"


def test_source_slots_are_process_lifetime_keyed_and_bounded() -> None:
    resources = _resources_module()
    slots = resources.SourceSlotRegistry(per_source_limit=1)

    first = slots.try_acquire("source-1")
    assert first is not None
    assert slots.try_acquire("source-1") is None
    other = slots.try_acquire("source-2")
    assert other is not None
    assert slots.snapshot()["known_sources"] == 2

    first.release()
    again = slots.try_acquire("source-1")
    assert again is not None
    assert again.generation > first.generation
    again.release()
    other.release()
    assert slots.snapshot()["active"] == 0


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeConnection:
    def cursor(self, *_args: object, **_kwargs: object) -> _FakeCursor:
        return _FakeCursor()

    def transaction(self, *_args: object, **_kwargs: object) -> _FakeTransaction:
        return _FakeTransaction()


class _FakePool:
    instances: list["_FakePool"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.opened = False
        self.closed = False
        self.checkout_count = 0
        self.connection_object = _FakeConnection()
        self.__class__.instances.append(self)

    def open(self, *, wait: bool = False, timeout: float | None = None) -> None:
        self.opened = True

    @contextmanager
    def connection(self, *, timeout: float | None = None):
        self.checkout_count += 1
        yield self.connection_object

    def close(self, *, timeout: float | None = None) -> None:
        self.closed = True


def test_connection_provider_is_bounded_observable_and_reused() -> None:
    resources = _resources_module()
    _FakePool.instances.clear()
    provider = resources.PooledConnectionProvider(
        "postgresql://unused",
        max_size=4,
        timeout_s=0.5,
        pool_factory=_FakePool,
    )

    with provider.connection() as first:
        assert provider.snapshot()["in_use"] == 1
    with provider.connection() as second:
        assert first is second

    snapshot = provider.snapshot()
    assert snapshot["max_size"] == 4
    assert snapshot["in_use"] == 0
    assert snapshot["checkout_count"] == 2
    assert len(_FakePool.instances) == 1
    assert _FakePool.instances[0].kwargs["max_size"] == 4

    provider.close()
    assert _FakePool.instances[0].closed is True
    assert provider.snapshot()["state"] == "closed"


def test_scoped_connection_checks_out_only_for_cursor_or_transaction() -> None:
    resources = _resources_module()
    provider = resources.PooledConnectionProvider(
        "postgresql://unused",
        max_size=2,
        pool_factory=_FakePool,
    )
    proxy = provider.scoped_connection()

    assert provider.snapshot()["in_use"] == 0
    with proxy.cursor():
        assert provider.snapshot()["in_use"] == 1
    assert provider.snapshot()["in_use"] == 0

    with proxy.transaction():
        assert provider.snapshot()["in_use"] == 1
        with proxy.cursor():
            assert provider.snapshot()["in_use"] == 1
    assert provider.snapshot()["in_use"] == 0
    assert provider.snapshot()["checkout_count"] == 2
    provider.close()


def test_resource_runtime_clamps_lanes_and_creates_nothing_when_disabled() -> None:
    resources = _resources_module()
    _FakePool.instances.clear()
    runtime = resources.MaterializationResources(
        database_url="postgresql://unused",
        max_active=4,
        image_workers=32,
        remux_workers=8,
        finalizer_workers=32,
        image_queue_capacity=32,
        remux_queue_capacity=32,
        finalizer_queue_capacity=32,
        db_pool_enabled=True,
        db_pool_timeout_s=1,
        shutdown_grace_s=45,
        source_limit=1,
        pool_factory=_FakePool,
    )

    snapshot = runtime.snapshot()
    assert snapshot["work_budget"]["max_active"] == 4
    assert snapshot["image_lane"]["max_workers"] == 4
    assert snapshot["remux_lane"]["max_workers"] == 4
    assert snapshot["finalizer_lane"]["max_workers"] == 4
    assert snapshot["db_pool"]["max_size"] == 4
    assert len(_FakePool.instances) == 1
    runtime.close(wait=True)

    _FakePool.instances.clear()
    disabled = resources.MaterializationResources(
        database_url="postgresql://unused",
        max_active=0,
        image_workers=32,
        remux_workers=32,
        finalizer_workers=32,
        db_pool_enabled=True,
        pool_factory=_FakePool,
    )
    disabled_snapshot = disabled.snapshot()
    assert disabled_snapshot["image_lane"] is None
    assert disabled_snapshot["remux_lane"] is None
    assert disabled_snapshot["finalizer_lane"] is None
    assert disabled_snapshot["db_pool"] is None
    assert _FakePool.instances == []
    disabled.close(wait=True)


def test_shutdown_state_machine_first_signal_drains_second_signal_forces() -> None:
    resources = _resources_module()
    now = [100.0]
    shutdown = resources.ShutdownController(
        grace_seconds=45,
        clock=lambda: now[0],
    )

    assert shutdown.admission_open is True
    assert shutdown.request(signal_number=15) == resources.ShutdownPhase.QUIESCING
    assert shutdown.admission_open is False
    assert shutdown.begin_draining() == resources.ShutdownPhase.DRAINING
    now[0] = 144.9
    assert shutdown.grace_expired() is False
    now[0] = 145.0
    assert shutdown.grace_expired() is True
    assert shutdown.begin_stopping() == resources.ShutdownPhase.STOPPING
    assert shutdown.mark_stopped() == resources.ShutdownPhase.STOPPED

    forced = resources.ShutdownController(grace_seconds=45, clock=lambda: 1.0)
    forced.request(signal_number=15)
    assert forced.request(signal_number=2) == resources.ShutdownPhase.STOPPING
    assert forced.force_requested is True


def test_lane_submit_failure_returns_reservation_capacity() -> None:
    resources = _resources_module()

    class _RejectingExecutor:
        def submit(self, *_args: Any, **_kwargs: Any) -> Future[Any]:
            raise RuntimeError("executor stopping")

        def shutdown(self, **_kwargs: Any) -> None:
            return None

    lane = resources.BoundedExecutorLane(
        name="remux",
        max_workers=1,
        queue_capacity=0,
        executor_factory=lambda **_kwargs: _RejectingExecutor(),
    )
    reservation = lane.try_reserve()
    assert reservation is not None
    try:
        reservation.submit(lambda: None)
    except RuntimeError as exc:
        assert str(exc) == "executor stopping"
    else:
        raise AssertionError("submit unexpectedly succeeded")
    assert lane.snapshot()["reserved"] == 0
    lane.close(wait=False)


def test_lease_heartbeat_enforces_fence_loss_and_max_attempt_age() -> None:
    resources = _resources_module()
    now = [10.0]
    beats: list[tuple[object, float]] = []

    def heartbeat(payload: object, lease_seconds: float) -> bool:
        beats.append((payload, lease_seconds))
        return payload != "lost"

    supervisor = resources.LeaseHeartbeatSupervisor(
        heartbeat=heartbeat,
        interval_s=5,
        max_attempt_age_s=12,
        clock=lambda: now[0],
        autostart=False,
    )
    healthy = supervisor.register("healthy", payload="lease-1", lease_seconds=30)
    lost = supervisor.register("lost", payload="lost", lease_seconds=30)

    now[0] = 15.0
    supervisor.tick_once()
    assert healthy.healthy is True
    assert lost.lost is True
    assert beats == [("lease-1", 30.0), ("lost", 30.0)]

    now[0] = 22.0
    supervisor.tick_once()
    assert healthy.expired is True
    assert healthy.healthy is False
    snapshot = supervisor.snapshot()
    assert snapshot["active"] == 0
    assert snapshot["lost_total"] == 1
    assert snapshot["expired_total"] == 1
    supervisor.close()


def test_managed_process_registry_terms_then_bounds_forced_shutdown() -> None:
    resources = _resources_module()
    from app.subprocess_control import ManagedProcessRegistry

    registry = ManagedProcessRegistry()
    result: list[object] = []

    def run() -> None:
        result.append(
            registry.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture_output=True,
                text=True,
            )
        )

    thread = Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while registry.snapshot()["active"] != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert registry.snapshot()["active"] == 1

    registry.terminate_all(term_timeout_s=0.2)
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert result and result[0].returncode != 0
    snapshot = registry.snapshot()
    assert snapshot["active"] == 0
    assert snapshot["term_total"] >= 1


def test_first_shutdown_signal_allows_grace_then_forces_active_processes() -> None:
    resources = _resources_module()
    runtime = resources.MaterializationResources(
        database_url="postgresql://unused",
        max_active=1,
        shutdown_grace_s=0.05,
        shutdown_kill_timeout_s=0.05,
    )
    result: list[object] = []

    thread = Thread(
        target=lambda: result.append(
            runtime.processes.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture_output=True,
                text=True,
            )
        ),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 2
    while runtime.processes.snapshot()["active"] != 1 and time.monotonic() < deadline:
        time.sleep(0.01)

    runtime.request_shutdown(signal_number=15)
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert runtime.shutdown.force_requested is True
    assert result and result[0].returncode != 0
    runtime.close(wait=False, force=True)


def test_phase3_dependency_compose_and_managed_subprocess_contracts() -> None:
    pool_requirements = (
        MEDIA_WORKER_ROOT / "requirements.pool.txt"
    ).read_text(encoding="utf-8")
    dockerfile = (MEDIA_WORKER_ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "infra" / "docker-compose.midterm.yml").read_text(
        encoding="utf-8"
    )
    managed_modules = (
        "worker.py",
        "rolling_cache.py",
        "snapshot.py",
        "post_savant_evidence_bundle.py",
        "post_savant_video_integrity.py",
        "canonical_timeline.py",
    )

    assert "psycopg-pool" in pool_requirements
    assert "requirements.pool.txt" in dockerfile
    assert "MEDIA_WORKER_DB_POOL_ENABLED" in compose
    assert "MEDIA_WORKER_SHUTDOWN_GRACE_S" in compose
    assert "stop_grace_period: 70s" in compose
    for module_name in managed_modules:
        source = (MEDIA_WORKER_ROOT / "app" / module_name).read_text(
            encoding="utf-8"
        )
        assert "subprocess.run(" not in source, module_name
        assert "run_managed_subprocess(" in source, module_name
