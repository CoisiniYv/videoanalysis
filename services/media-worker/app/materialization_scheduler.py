"""Long-lived, bounded resources for evidence materialization.

This module intentionally contains no evidence business logic.  PostgreSQL is
still the durable queue; the objects below only bound process-local execution
and make ownership explicit before Scheduler V2 is enabled.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from enum import Enum
import logging
from threading import BoundedSemaphore, Event, Lock, Thread, local
import time
from typing import Any, Callable, Iterator

from app.subprocess_control import ManagedProcessRegistry


logger = logging.getLogger(__name__)


class ShutdownPhase(str, Enum):
    RUNNING = "running"
    QUIESCING = "quiescing"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"


class ShutdownController:
    """Thread-safe first-signal drain / second-signal force state machine."""

    def __init__(
        self,
        *,
        grace_seconds: float = 45.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.grace_seconds = max(0.0, float(grace_seconds))
        self._clock = clock
        self._phase = ShutdownPhase.RUNNING
        self._requested_at: float | None = None
        self._deadline: float | None = None
        self._signal_count = 0
        self._last_signal: int | None = None
        self._force_requested = False
        self._lock = Lock()

    @property
    def phase(self) -> ShutdownPhase:
        with self._lock:
            return self._phase

    @property
    def admission_open(self) -> bool:
        with self._lock:
            return self._phase is ShutdownPhase.RUNNING

    @property
    def force_requested(self) -> bool:
        with self._lock:
            return self._force_requested

    def request(self, *, signal_number: int | None = None) -> ShutdownPhase:
        with self._lock:
            self._signal_count += 1
            self._last_signal = signal_number
            if self._phase is ShutdownPhase.RUNNING:
                now = self._clock()
                self._requested_at = now
                self._deadline = now + self.grace_seconds
                self._phase = ShutdownPhase.QUIESCING
            elif self._phase is not ShutdownPhase.STOPPED:
                self._force_requested = True
                self._phase = ShutdownPhase.STOPPING
            return self._phase

    def begin_draining(self) -> ShutdownPhase:
        with self._lock:
            if self._phase is ShutdownPhase.QUIESCING:
                self._phase = ShutdownPhase.DRAINING
            return self._phase

    def grace_expired(self) -> bool:
        with self._lock:
            return bool(
                self._deadline is not None
                and self._clock() >= self._deadline
            )

    def begin_stopping(self, *, force: bool = False) -> ShutdownPhase:
        with self._lock:
            if force:
                self._force_requested = True
            if self._phase is not ShutdownPhase.STOPPED:
                self._phase = ShutdownPhase.STOPPING
            return self._phase

    def mark_stopped(self) -> ShutdownPhase:
        with self._lock:
            self._phase = ShutdownPhase.STOPPED
            return self._phase

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "phase": self._phase.value,
                "grace_seconds": self.grace_seconds,
                "requested_at_monotonic": self._requested_at,
                "deadline_monotonic": self._deadline,
                "signal_count": self._signal_count,
                "last_signal": self._last_signal,
                "force_requested": self._force_requested,
            }


class WorkPermit:
    """Move-only ownership of one process-wide materialization WIP slot."""

    def __init__(
        self,
        budget: "WorkBudget",
        *,
        permit_id: int,
        lane: str,
        owner: str,
    ) -> None:
        self._budget = budget
        self.permit_id = permit_id
        self.lane = lane
        self.owner = owner
        self._released = False
        self._lock = Lock()

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def move_to(self, lane: str, *, owner: str | None = None) -> "WorkPermit":
        lane = _normalise_lane(lane)
        with self._lock:
            if self._released:
                raise RuntimeError("cannot move a released work permit")
            old_lane = self.lane
            if old_lane != lane:
                self._budget._move(self.permit_id, old_lane=old_lane, new_lane=lane)
                self.lane = lane
            if owner is not None:
                self.owner = str(owner)
        return self

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            lane = self.lane
        self._budget._release(self.permit_id, lane=lane)

    def __enter__(self) -> "WorkPermit":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _normalise_lane(lane: str) -> str:
    value = str(lane or "other").strip().lower()
    return value if value in {"image", "remux", "finalizer"} else "other"


class WorkBudget:
    """One shared end-to-end WIP budget for every materialization lane."""

    def __init__(self, max_active: int, *, reserved_non_image: int = 1) -> None:
        self.max_active = max(0, int(max_active))
        self.reserved_non_image = (
            min(max(0, int(reserved_non_image)), self.max_active - 1)
            if self.max_active >= 2
            else 0
        )
        self._active = 0
        self._by_lane = {
            "image": 0,
            "remux": 0,
            "finalizer": 0,
            "other": 0,
        }
        self._permit_lanes: dict[int, str] = {}
        self._compatibility_permits: list[WorkPermit] = []
        self._next_permit_id = 1
        self._lock = Lock()

    def try_acquire(self, lane: str, *, owner: str = "") -> WorkPermit | None:
        lane = _normalise_lane(lane)
        with self._lock:
            if self.max_active <= 0 or self._active >= self.max_active:
                return None
            if (
                lane == "image"
                and self.reserved_non_image > 0
                and self._active >= self.max_active - self.reserved_non_image
            ):
                return None
            permit_id = self._next_permit_id
            self._next_permit_id += 1
            self._active += 1
            self._by_lane[lane] += 1
            self._permit_lanes[permit_id] = lane
        return WorkPermit(
            self,
            permit_id=permit_id,
            lane=lane,
            owner=str(owner),
        )

    def acquire(self) -> bool:
        """Compatibility bridge for the Phase 2 guard call sites.

        New scheduler code retains the returned ``WorkPermit`` from
        ``try_acquire``.  This bridge exists only while the legacy synchronous
        admission branch remains available through Phase 7.
        """
        permit = self.try_acquire("finalizer", owner="legacy-guard")
        if permit is None:
            return False
        with self._lock:
            self._compatibility_permits.append(permit)
        return True

    def release(self) -> None:
        with self._lock:
            permit = (
                self._compatibility_permits.pop()
                if self._compatibility_permits
                else None
            )
        if permit is not None:
            permit.release()

    def _move(self, permit_id: int, *, old_lane: str, new_lane: str) -> None:
        with self._lock:
            current = self._permit_lanes.get(permit_id)
            if current is None:
                raise RuntimeError("work permit is no longer active")
            if current != old_lane:
                raise RuntimeError("work permit lane ownership changed")
            self._by_lane[old_lane] = max(0, self._by_lane[old_lane] - 1)
            self._by_lane[new_lane] += 1
            self._permit_lanes[permit_id] = new_lane

    def _release(self, permit_id: int, *, lane: str) -> None:
        with self._lock:
            current = self._permit_lanes.pop(permit_id, None)
            if current is None:
                return
            self._active = max(0, self._active - 1)
            self._by_lane[current] = max(0, self._by_lane[current] - 1)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "max_active": self.max_active,
                "active": self._active,
                "image_active": self._by_lane["image"],
                "remux_active": self._by_lane["remux"],
                "finalizer_active": self._by_lane["finalizer"],
                "other_active": self._by_lane["other"],
                "reserved_non_image": self.reserved_non_image,
            }


class _LaneTicket:
    def __init__(self, lane: "BoundedExecutorLane") -> None:
        self.lane = lane
        self.submitted = False
        self.started = False
        self.released = False
        self.lock = Lock()

    def mark_submitted(self) -> None:
        with self.lock:
            if self.released:
                raise RuntimeError("lane reservation already released")
            if self.submitted:
                raise RuntimeError("lane reservation already submitted")
            self.submitted = True
        self.lane._mark_submitted()

    def mark_started(self) -> None:
        with self.lock:
            if self.released:
                return
            self.started = True
        self.lane._mark_started()

    def release(self) -> None:
        with self.lock:
            if self.released:
                return
            self.released = True
            submitted = self.submitted
            started = self.started
        self.lane._release_ticket(submitted=submitted, started=started)


class LaneReservation:
    """A bounded lane slot that must exist before durable DB claim."""

    def __init__(self, lane: "BoundedExecutorLane", ticket: _LaneTicket) -> None:
        self._lane = lane
        self._ticket = ticket

    def submit(
        self,
        function: Callable[..., Any],
        /,
        *args: object,
        **kwargs: object,
    ) -> Future[Any]:
        self._ticket.mark_submitted()

        def run() -> Any:
            self._ticket.mark_started()
            try:
                return function(*args, **kwargs)
            finally:
                self._ticket.release()

        try:
            future = self._lane._executor_submit(run)
        except BaseException:
            self._ticket.release()
            raise

        def release_cancelled(done: Future[Any]) -> None:
            if done.cancelled():
                self._ticket.release()

        future.add_done_callback(release_cancelled)
        return future

    def cancel(self) -> None:
        self._ticket.release()

    def __enter__(self) -> "LaneReservation":
        return self

    def __exit__(self, *_exc: object) -> None:
        with self._ticket.lock:
            submitted = self._ticket.submitted
        if not submitted:
            self.cancel()


class BoundedIoGate:
    """Process-lifetime semaphore with cumulative wait/service diagnostics."""

    def __init__(
        self,
        *,
        name: str,
        limit: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = str(name)
        self.limit = int(limit)
        if self.limit < 1:
            raise ValueError("bounded I/O gate limit must be positive")
        self._clock = clock
        self._semaphore = BoundedSemaphore(self.limit)
        self._lock = Lock()
        self._active = 0
        self._waiting = 0
        self._active_peak = 0
        self._acquired_total = 0
        self._wait_events_total = 0
        self._wait_ms_total = 0.0
        self._wait_ms_max = 0.0
        self._service_ms_total = 0.0
        self._service_ms_max = 0.0

    @contextmanager
    def slot(self) -> Iterator[dict[str, int | float]]:
        wait_started_at = self._clock()
        contended = not self._semaphore.acquire(blocking=False)
        if contended:
            with self._lock:
                self._waiting += 1
            try:
                self._semaphore.acquire()
            finally:
                with self._lock:
                    self._waiting = max(0, self._waiting - 1)
        wait_ms = max(0.0, (self._clock() - wait_started_at) * 1000)
        service_started_at = self._clock()
        with self._lock:
            self._active += 1
            self._active_peak = max(self._active_peak, self._active)
            self._acquired_total += 1
            self._wait_ms_total += wait_ms
            self._wait_ms_max = max(self._wait_ms_max, wait_ms)
            if contended:
                self._wait_events_total += 1
            active_at_acquire = self._active
        timing: dict[str, int | float] = {
            "limit": self.limit,
            "wait_ms": round(wait_ms, 3),
            "service_ms": 0.0,
            "active_at_acquire": active_at_acquire,
        }
        try:
            yield timing
        finally:
            service_ms = max(0.0, (self._clock() - service_started_at) * 1000)
            timing["service_ms"] = round(service_ms, 3)
            with self._lock:
                self._active = max(0, self._active - 1)
                self._service_ms_total += service_ms
                self._service_ms_max = max(self._service_ms_max, service_ms)
            self._semaphore.release()

    def snapshot(self) -> dict[str, int | float | str]:
        with self._lock:
            return {
                "name": self.name,
                "limit": self.limit,
                "active": self._active,
                "waiting": self._waiting,
                "active_peak": self._active_peak,
                "acquired_total": self._acquired_total,
                "wait_events_total": self._wait_events_total,
                "wait_ms_total": round(self._wait_ms_total, 3),
                "wait_ms_max": round(self._wait_ms_max, 3),
                "service_ms_total": round(self._service_ms_total, 3),
                "service_ms_max": round(self._service_ms_max, 3),
            }


class BoundedExecutorLane:
    """One long-lived executor with explicit bounded pre-submit admission."""

    def __init__(
        self,
        *,
        name: str,
        max_workers: int,
        queue_capacity: int = 0,
        executor_factory: Callable[..., Any] = ThreadPoolExecutor,
    ) -> None:
        self.name = str(name)
        self.max_workers = max(1, int(max_workers))
        self.queue_capacity = max(0, int(queue_capacity))
        self.capacity = self.max_workers + self.queue_capacity
        self._semaphore = BoundedSemaphore(self.capacity)
        self._lock = Lock()
        self._state = "running"
        self._reserved = 0
        self._queued = 0
        self._active = 0
        self._submitted_total = 0
        self._rejected_total = 0
        self._executor_create_count = 1
        self._executor = executor_factory(
            max_workers=self.max_workers,
            thread_name_prefix=f"media-{self.name}",
        )

    def try_reserve(self) -> LaneReservation | None:
        with self._lock:
            if self._state != "running":
                self._rejected_total += 1
                return None
        if not self._semaphore.acquire(blocking=False):
            with self._lock:
                self._rejected_total += 1
            return None
        with self._lock:
            if self._state != "running":
                self._semaphore.release()
                self._rejected_total += 1
                return None
            self._reserved += 1
        return LaneReservation(self, _LaneTicket(self))

    def _mark_submitted(self) -> None:
        with self._lock:
            self._queued += 1
            self._submitted_total += 1

    def _mark_started(self) -> None:
        with self._lock:
            self._queued = max(0, self._queued - 1)
            self._active += 1

    def _release_ticket(self, *, submitted: bool, started: bool) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - 1)
            if submitted and not started:
                self._queued = max(0, self._queued - 1)
            if started:
                self._active = max(0, self._active - 1)
        self._semaphore.release()

    def _executor_submit(self, function: Callable[[], Any]) -> Future[Any]:
        with self._lock:
            if self._state != "running":
                raise RuntimeError(f"{self.name} lane is not accepting work")
        return self._executor.submit(function)

    def close(self, *, wait: bool, cancel_futures: bool = True) -> None:
        with self._lock:
            if self._state == "closed":
                return
            self._state = "closing"
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        with self._lock:
            self._state = "closed"

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "name": self.name,
                "state": self._state,
                "max_workers": self.max_workers,
                "queue_capacity": self.queue_capacity,
                "capacity": self.capacity,
                "reserved": self._reserved,
                "queued": self._queued,
                "active": self._active,
                "submitted_total": self._submitted_total,
                "rejected_total": self._rejected_total,
                "executor_create_count": self._executor_create_count,
            }


class SourcePermit:
    def __init__(
        self,
        registry: "SourceSlotRegistry",
        source_id: str,
        generation: int,
    ) -> None:
        self._registry = registry
        self.source_id = source_id
        self.generation = generation
        self._released = False
        self._lock = Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._registry._release(self.source_id)

    def __enter__(self) -> "SourcePermit":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class SourceSlotRegistry:
    """Process-lifetime keyed source caps shared across scheduler ticks."""

    def __init__(self, *, per_source_limit: int = 1) -> None:
        self.per_source_limit = max(1, int(per_source_limit))
        self._active: dict[str, int] = {}
        self._generation: dict[str, int] = {}
        self._lock = Lock()

    def try_acquire(self, source_id: str) -> SourcePermit | None:
        key = str(source_id or "__unknown__")
        with self._lock:
            active = self._active.get(key, 0)
            if active >= self.per_source_limit:
                return None
            generation = self._generation.get(key, 0) + 1
            self._generation[key] = generation
            self._active[key] = active + 1
        return SourcePermit(self, key, generation)

    def _release(self, source_id: str) -> None:
        with self._lock:
            active = self._active.get(source_id, 0)
            self._active[source_id] = max(0, active - 1)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "per_source_limit": self.per_source_limit,
                "known_sources": len(self._generation),
                "active_sources": sum(1 for value in self._active.values() if value),
                "active": sum(self._active.values()),
            }


class LeaseHeartbeatHandle:
    def __init__(
        self,
        *,
        key: str,
        payload: object,
        lease_seconds: float,
        started_at: float,
        next_due_at: float,
    ) -> None:
        self.key = key
        self.payload = payload
        self.lease_seconds = lease_seconds
        self.started_at = started_at
        self.next_due_at = next_due_at
        self.last_heartbeat_at: float | None = None
        self.lost = False
        self.expired = False
        self.unregistered = False
        self._lock = Lock()

    @property
    def healthy(self) -> bool:
        with self._lock:
            return not (self.lost or self.expired or self.unregistered)

    def _mark_heartbeat(self, now: float, *, next_due_at: float) -> None:
        with self._lock:
            if self.lost or self.expired or self.unregistered:
                return
            self.last_heartbeat_at = now
            self.next_due_at = next_due_at

    def _mark_lost(self) -> None:
        with self._lock:
            self.lost = True

    def _mark_expired(self) -> None:
        with self._lock:
            self.expired = True

    def _mark_unregistered(self) -> None:
        with self._lock:
            self.unregistered = True


class LeaseHeartbeatSupervisor:
    """One process-lifetime heartbeat loop for all active fenced leases."""

    def __init__(
        self,
        *,
        heartbeat: Callable[[object, float], bool],
        interval_s: float,
        max_attempt_age_s: float,
        clock: Callable[[], float] = time.monotonic,
        autostart: bool = True,
    ) -> None:
        self.interval_s = max(0.1, float(interval_s))
        self.max_attempt_age_s = max(self.interval_s, float(max_attempt_age_s))
        self._heartbeat = heartbeat
        self._clock = clock
        self._handles: dict[str, LeaseHeartbeatHandle] = {}
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._heartbeat_total = 0
        self._lost_total = 0
        self._expired_total = 0
        self._error_total = 0
        if autostart:
            self._thread = Thread(
                target=self._run,
                name="media-lease-heartbeat",
                daemon=True,
            )
            self._thread.start()

    def register(
        self,
        key: str,
        *,
        payload: object,
        lease_seconds: float,
    ) -> LeaseHeartbeatHandle:
        now = self._clock()
        handle = LeaseHeartbeatHandle(
            key=str(key),
            payload=payload,
            lease_seconds=max(1.0, float(lease_seconds)),
            started_at=now,
            next_due_at=now + self.interval_s,
        )
        with self._lock:
            previous = self._handles.pop(handle.key, None)
            if previous is not None:
                previous._mark_unregistered()
            self._handles[handle.key] = handle
        return handle

    def unregister(self, handle: LeaseHeartbeatHandle | None) -> None:
        if handle is None:
            return
        with self._lock:
            current = self._handles.get(handle.key)
            if current is handle:
                self._handles.pop(handle.key, None)
        handle._mark_unregistered()

    def tick_once(self) -> None:
        now = self._clock()
        with self._lock:
            handles = list(self._handles.values())
        for handle in handles:
            if not handle.healthy:
                self._remove(handle)
                continue
            if now - handle.started_at >= self.max_attempt_age_s:
                handle._mark_expired()
                with self._lock:
                    self._expired_total += 1
                self._remove(handle)
                continue
            if now < handle.next_due_at:
                continue
            try:
                alive = bool(
                    self._heartbeat(handle.payload, handle.lease_seconds)
                )
            except Exception:
                logger.exception("materialization_lease_heartbeat_failed key=%s", handle.key)
                alive = False
                with self._lock:
                    self._error_total += 1
            if not alive:
                handle._mark_lost()
                with self._lock:
                    self._lost_total += 1
                self._remove(handle)
                continue
            handle._mark_heartbeat(now, next_due_at=now + self.interval_s)
            with self._lock:
                self._heartbeat_total += 1

    def _remove(self, handle: LeaseHeartbeatHandle) -> None:
        with self._lock:
            if self._handles.get(handle.key) is handle:
                self._handles.pop(handle.key, None)

    def _run(self) -> None:
        poll_s = min(1.0, max(0.1, self.interval_s / 2.0))
        while not self._stop.wait(poll_s):
            self.tick_once()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_s + 1.0))
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            handle._mark_unregistered()

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "active": len(self._handles),
                "interval_s": self.interval_s,
                "max_attempt_age_s": self.max_attempt_age_s,
                "heartbeat_total": self._heartbeat_total,
                "lost_total": self._lost_total,
                "expired_total": self._expired_total,
                "error_total": self._error_total,
            }


class PooledConnectionProvider:
    """Observable bounded psycopg pool used only by materialization jobs."""

    def __init__(
        self,
        database_url: str,
        *,
        max_size: int,
        timeout_s: float = 5.0,
        pool_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.database_url = str(database_url)
        self.max_size = max(1, int(max_size))
        self.timeout_s = max(0.05, float(timeout_s))
        if pool_factory is None:
            try:
                from psycopg_pool import ConnectionPool as pool_factory  # type: ignore
            except ImportError as exc:  # pragma: no cover - image contract test covers it
                raise RuntimeError(
                    "MEDIA_WORKER_DB_POOL_ENABLED requires psycopg_pool"
                ) from exc
        self._lock = Lock()
        self._state = "opening"
        self._in_use = 0
        self._peak_in_use = 0
        self._checkout_count = 0
        self._checkout_timeout_count = 0
        self._checkout_error_count = 0
        self._checkout_wait_ms = 0
        self._reset_count = 0
        self._pool = pool_factory(
            conninfo=self.database_url,
            min_size=0,
            max_size=self.max_size,
            timeout=self.timeout_s,
            kwargs={"autocommit": True},
            open=False,
            name="media-worker-jobs",
            reset=self._on_reset,
        )
        self._pool.open(wait=True, timeout=self.timeout_s)
        self._state = "open"

    @contextmanager
    def connection(self) -> Iterator[Any]:
        started = time.monotonic()
        try:
            manager = self._pool.connection(timeout=self.timeout_s)
            with manager as connection:
                waited_ms = int((time.monotonic() - started) * 1000)
                with self._lock:
                    self._checkout_count += 1
                    self._checkout_wait_ms += waited_ms
                    self._in_use += 1
                    self._peak_in_use = max(self._peak_in_use, self._in_use)
                try:
                    yield connection
                finally:
                    with self._lock:
                        self._in_use = max(0, self._in_use - 1)
        except Exception as exc:
            if type(exc).__name__ in {"PoolTimeout", "TimeoutError"}:
                with self._lock:
                    self._checkout_timeout_count += 1
            else:
                with self._lock:
                    self._checkout_error_count += 1
            raise

    def _on_reset(self, _connection: object) -> None:
        with self._lock:
            self._reset_count += 1

    def close(self, *, timeout_s: float = 5.0) -> None:
        with self._lock:
            if self._state == "closed":
                return
            self._state = "closing"
        try:
            self._pool.close(timeout=max(0.0, float(timeout_s)))
        except TypeError:
            self._pool.close()
        with self._lock:
            self._state = "closed"

    def scoped_connection(self) -> "ScopedConnectionProxy":
        """Return a thread-local proxy that checks out only for DB scopes."""
        return ScopedConnectionProxy(self)

    def snapshot(self) -> dict[str, int | str]:
        try:
            pool_stats = dict(self._pool.get_stats())
        except (AttributeError, TypeError):
            pool_stats = {}
        with self._lock:
            return {
                "state": self._state,
                "max_size": self.max_size,
                "in_use": self._in_use,
                "peak_in_use": self._peak_in_use,
                "checkout_count": self._checkout_count,
                "checkout_timeout_count": self._checkout_timeout_count,
                "checkout_error_count": self._checkout_error_count,
                "checkout_wait_ms": self._checkout_wait_ms,
                "reset_count": self._reset_count,
                "connections_created": int(pool_stats.get("connections_num") or 0),
                "connections_lost": int(pool_stats.get("connections_lost") or 0),
                "connection_errors": int(
                    pool_stats.get("connections_errors") or 0
                ),
                "pool_size": int(pool_stats.get("pool_size") or 0),
                "pool_available": int(pool_stats.get("pool_available") or 0),
            }


class ScopedConnectionProxy:
    """Psycopg-shaped cursor/transaction facade over short pool checkouts.

    Callers can retain this facade while doing ffmpeg or filesystem work: no
    physical PostgreSQL connection is retained between ``cursor()`` scopes.
    A ``transaction()`` scope pins exactly one connection for its nested
    cursors and releases it immediately after commit/rollback.
    """

    def __init__(self, provider: PooledConnectionProvider) -> None:
        self._provider = provider
        self._local = local()

    @contextmanager
    def cursor(self, *args: object, **kwargs: object) -> Iterator[Any]:
        pinned = getattr(self._local, "connection", None)
        if pinned is not None:
            with pinned.cursor(*args, **kwargs) as cursor:
                yield cursor
            return
        with self._provider.connection() as connection:
            with connection.cursor(*args, **kwargs) as cursor:
                yield cursor

    @contextmanager
    def transaction(self, *args: object, **kwargs: object) -> Iterator[None]:
        pinned = getattr(self._local, "connection", None)
        if pinned is not None:
            with pinned.transaction(*args, **kwargs):
                yield
            return
        with self._provider.connection() as connection:
            self._local.connection = connection
            try:
                with connection.transaction(*args, **kwargs):
                    yield
            finally:
                del self._local.connection


class MaterializationResources:
    """Process-lifetime owner for WIP, lanes, source caps, pool and shutdown."""

    def __init__(
        self,
        *,
        database_url: str,
        max_active: int,
        image_workers: int = 1,
        remux_workers: int = 1,
        finalizer_workers: int = 1,
        image_queue_capacity: int = 0,
        remux_queue_capacity: int = 0,
        finalizer_queue_capacity: int = 0,
        db_pool_enabled: bool = False,
        db_pool_timeout_s: float = 5.0,
        db_index_io_concurrency: int = 0,
        shutdown_grace_s: float = 45.0,
        shutdown_kill_timeout_s: float = 5.0,
        source_limit: int = 1,
        reserved_non_image: int = 1,
        pool_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.max_active = max(0, int(max_active))
        self.work_budget = WorkBudget(
            self.max_active,
            reserved_non_image=reserved_non_image,
        )
        self.source_slots = SourceSlotRegistry(per_source_limit=source_limit)
        self.shutdown = ShutdownController(grace_seconds=shutdown_grace_s)
        self.shutdown_kill_timeout_s = max(0.0, float(shutdown_kill_timeout_s))
        self.processes = ManagedProcessRegistry()
        self.image_lane: BoundedExecutorLane | None = None
        self.remux_lane: BoundedExecutorLane | None = None
        self.finalizer_lane: BoundedExecutorLane | None = None
        self.db_index_io_gate: BoundedIoGate | None = None
        self.db_pool: PooledConnectionProvider | None = None
        self.lease_heartbeats: LeaseHeartbeatSupervisor | None = None
        self._closed = False
        self._lock = Lock()
        self._shutdown_watch_stop = Event()
        self._shutdown_watch_thread: Thread | None = None

        if self.max_active <= 0:
            return

        def workers(value: int) -> int:
            return min(self.max_active, max(1, int(value)))

        def queue(value: int) -> int:
            return min(self.max_active, max(0, int(value)))

        try:
            requested_db_index_io_concurrency = int(db_index_io_concurrency)
            effective_db_index_io_concurrency = workers(
                requested_db_index_io_concurrency
                if requested_db_index_io_concurrency > 0
                else self.max_active
            )
            self.db_index_io_gate = BoundedIoGate(
                name="finalizer-db-index",
                limit=effective_db_index_io_concurrency,
            )
            if db_pool_enabled:
                self.db_pool = PooledConnectionProvider(
                    database_url,
                    max_size=self.max_active,
                    timeout_s=db_pool_timeout_s,
                    pool_factory=pool_factory,
                )
            self.image_lane = BoundedExecutorLane(
                name="image",
                max_workers=workers(image_workers),
                queue_capacity=queue(image_queue_capacity),
            )
            self.remux_lane = BoundedExecutorLane(
                name="remux",
                max_workers=workers(remux_workers),
                queue_capacity=queue(remux_queue_capacity),
            )
            self.finalizer_lane = BoundedExecutorLane(
                name="finalizer",
                max_workers=workers(finalizer_workers),
                queue_capacity=queue(finalizer_queue_capacity),
            )
        except Exception:
            for lane in (self.image_lane, self.remux_lane, self.finalizer_lane):
                if lane is not None:
                    lane.close(wait=False, cancel_futures=True)
            if self.db_pool is not None:
                self.db_pool.close()
            raise

    @property
    def admission_open(self) -> bool:
        return self.max_active > 0 and self.shutdown.admission_open

    def request_shutdown(self, *, signal_number: int | None = None) -> ShutdownPhase:
        phase = self.shutdown.request(signal_number=signal_number)
        if phase is ShutdownPhase.QUIESCING:
            with self._lock:
                if self._shutdown_watch_thread is None:
                    self._shutdown_watch_thread = Thread(
                        target=self._watch_shutdown_grace,
                        name="media-shutdown-grace",
                        daemon=True,
                    )
                    self._shutdown_watch_thread.start()
        elif self.shutdown.force_requested:
            self.processes.terminate_all(term_timeout_s=0.0)
        return phase

    def _watch_shutdown_grace(self) -> None:
        if self._shutdown_watch_stop.wait(self.shutdown.grace_seconds):
            return
        if self.shutdown.phase is ShutdownPhase.STOPPED:
            return
        self.shutdown.begin_stopping(force=True)
        logger.warning(
            "media_worker_shutdown_grace_expired grace_seconds=%s active_processes=%s",
            self.shutdown.grace_seconds,
            self.processes.snapshot()["active"],
        )
        self.processes.terminate_all(
            term_timeout_s=self.shutdown_kill_timeout_s,
        )

    def start_lease_heartbeats(
        self,
        *,
        heartbeat: Callable[[object, float], bool],
        interval_s: float,
        max_attempt_age_s: float,
    ) -> LeaseHeartbeatSupervisor | None:
        if self.max_active <= 0:
            return None
        if self.lease_heartbeats is not None:
            return self.lease_heartbeats
        self.lease_heartbeats = LeaseHeartbeatSupervisor(
            heartbeat=heartbeat,
            interval_s=interval_s,
            max_attempt_age_s=max_attempt_age_s,
        )
        return self.lease_heartbeats

    def close(self, *, wait: bool, force: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.shutdown.begin_stopping(force=force)
        if force:
            self.processes.terminate_all(
                term_timeout_s=self.shutdown_kill_timeout_s,
            )
        if self.lease_heartbeats is not None:
            self.lease_heartbeats.close()
        # Stop admission first, then execution lanes, then DB resources.
        for lane in (self.image_lane, self.remux_lane, self.finalizer_lane):
            if lane is not None:
                lane.close(wait=wait and not force, cancel_futures=True)
        if self.db_pool is not None:
            self.db_pool.close()
        self.shutdown.mark_stopped()
        self._shutdown_watch_stop.set()
        watch_thread = self._shutdown_watch_thread
        if watch_thread is not None:
            watch_thread.join(timeout=1.0)

    def snapshot(self) -> dict[str, object]:
        return {
            "work_budget": self.work_budget.snapshot(),
            "source_slots": self.source_slots.snapshot(),
            "shutdown": self.shutdown.snapshot(),
            "processes": self.processes.snapshot(),
            "image_lane": self.image_lane.snapshot() if self.image_lane else None,
            "remux_lane": self.remux_lane.snapshot() if self.remux_lane else None,
            "finalizer_lane": (
                self.finalizer_lane.snapshot() if self.finalizer_lane else None
            ),
            "db_index_io_gate": (
                self.db_index_io_gate.snapshot()
                if self.db_index_io_gate is not None
                else None
            ),
            "db_pool": self.db_pool.snapshot() if self.db_pool else None,
            "lease_heartbeats": (
                self.lease_heartbeats.snapshot()
                if self.lease_heartbeats is not None
                else None
            ),
        }
