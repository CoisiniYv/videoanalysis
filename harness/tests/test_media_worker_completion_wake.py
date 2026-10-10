"""Lane completions wake the media-worker scheduler instead of waiting a tick.

Admission and completion draining run on scheduler ticks. With a fixed 1 s
rolling tick and a one-worker remux lane, a finished remux waited for the next
tick before the next clip could start, so the lane delivered at most one clip
per tick (two ticks when a remux crossed a tick boundary). The uos157 60-source
hour measured ~0.75-0.85 clips/s against 0.82-0.86 clips/s of demand, and the
evidence backlog grew without bound. See
docs/midterm_evidence_remux_wake_2026-10-11.md.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from threading import Event, Thread

import pytest


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"
WORKER_SOURCE = MEDIA_WORKER_ROOT / "app" / "worker.py"
COMPOSE = ROOT / "infra" / "docker-compose.midterm.yml"
ENV_FILE = ROOT / "infra" / "env" / "midterm.env"
PRESSURE_SCRIPT = ROOT / "scripts" / "runtime" / "run_midterm_pressure60.py"


@pytest.fixture(autouse=True)
def _restore_import_state():
    original_path = list(sys.path)
    yield
    sys.path[:] = original_path
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _activate_media_worker() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    root = str(MEDIA_WORKER_ROOT)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)


def _resources_module():
    _activate_media_worker()
    import app.materialization_scheduler as resources

    return resources


def test_completion_signal_returns_notified_lanes_once() -> None:
    resources = _resources_module()
    signal = resources.CompletionSignal()

    signal.notify("remux")
    signal.notify("finalizer")
    signal.notify("remux")
    assert signal.wait(0) == frozenset({"remux", "finalizer"})
    # Consumed: the next wait times out empty instead of spinning.
    started = time.monotonic()
    assert signal.wait(0.05) == frozenset()
    assert time.monotonic() - started >= 0.04


def test_completion_signal_wakes_a_sleeping_scheduler_promptly() -> None:
    resources = _resources_module()
    signal = resources.CompletionSignal()
    result: dict[str, object] = {}

    def sleeper() -> None:
        started = time.monotonic()
        result["lanes"] = signal.wait(5.0)
        result["waited_s"] = time.monotonic() - started

    thread = Thread(target=sleeper)
    thread.start()
    time.sleep(0.05)
    signal.notify("remux")
    thread.join(timeout=2)

    assert result["lanes"] == frozenset({"remux"})
    assert result["waited_s"] < 1.0


def test_lane_completion_notifies_even_when_the_job_fails() -> None:
    resources = _resources_module()
    seen: list[str] = []
    lane = resources.BoundedExecutorLane(
        name="remux",
        max_workers=1,
        on_task_done=seen.append,
    )
    ok = lane.try_reserve()
    assert ok is not None
    assert ok.submit(lambda: "clip").result(timeout=2) == "clip"

    def broken() -> None:
        raise RuntimeError("ffmpeg failed")

    failed = lane.try_reserve()
    assert failed is not None
    future = failed.submit(broken)
    with pytest.raises(RuntimeError):
        future.result(timeout=2)
    deadline = time.monotonic() + 2
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == ["remux", "remux"]
    lane.close(wait=True)


def test_lane_completion_observer_errors_never_break_the_job() -> None:
    resources = _resources_module()

    def observer(_lane: str) -> None:
        raise RuntimeError("observer failed")

    lane = resources.BoundedExecutorLane(
        name="finalizer",
        max_workers=1,
        on_task_done=observer,
    )
    reservation = lane.try_reserve()
    assert reservation is not None
    assert reservation.submit(lambda: 7).result(timeout=2) == 7
    assert lane.snapshot()["reserved"] == 0
    lane.close(wait=True)


def test_runtime_lanes_report_completion_and_the_future_is_already_done() -> None:
    resources = _resources_module()
    runtime = resources.MaterializationResources(
        database_url="postgresql://unused",
        max_active=4,
        remux_workers=1,
    )
    reservation = runtime.remux_lane.try_reserve()
    assert reservation is not None
    release = Event()
    future = reservation.submit(lambda: release.wait(2) and "clip")

    assert runtime.completion_signal.wait(0.05) == frozenset()
    release.set()
    assert runtime.completion_signal.wait(2.0) == frozenset({"remux"})
    # The scheduler drains with future.done(); the wake must not race it.
    assert future.done()
    assert future.result() == "clip"
    runtime.close(wait=True)

    disabled = resources.MaterializationResources(
        database_url="postgresql://unused",
        max_active=0,
    )
    # Disabled materialization still exposes a signal so the loop can wait on it.
    assert disabled.completion_signal.wait(0) == frozenset()
    disabled.close(wait=True)


def test_remux_completion_pulls_the_next_rolling_admission_forward() -> None:
    worker = _resources_module()
    now = 100.0

    assert worker.rolling_poll_after_wake(
        100.9, frozenset({"remux"}), now=now, enabled=True
    ) == pytest.approx(now)
    # Other lanes are drained every tick already; they do not force admission.
    assert worker.rolling_poll_after_wake(
        100.9, frozenset({"finalizer", "image"}), now=now, enabled=True
    ) == pytest.approx(100.9)
    # An admission already due is never pushed later.
    assert worker.rolling_poll_after_wake(
        99.5, frozenset({"remux"}), now=now, enabled=True
    ) == pytest.approx(99.5)
    # Kill switch restores the fixed tick.
    assert worker.rolling_poll_after_wake(
        100.9, frozenset({"remux"}), now=now, enabled=False
    ) == pytest.approx(100.9)


def test_scheduler_wait_uses_the_signal_and_falls_back_to_sleep() -> None:
    resources = worker = _resources_module()
    runtime = resources.MaterializationResources(
        database_url="postgresql://unused",
        max_active=0,
    )
    runtime.completion_signal.notify("remux")
    started = time.monotonic()
    assert worker.scheduler_wait(runtime, 5.0, enabled=True) == frozenset({"remux"})
    assert time.monotonic() - started < 1.0

    runtime.completion_signal.notify("remux")
    started = time.monotonic()
    assert worker.scheduler_wait(runtime, 0.05, enabled=False) == frozenset()
    assert time.monotonic() - started >= 0.04
    runtime.close(wait=True)


def _drive_one_worker_remux_lane(
    resources, *, enabled: bool, jobs: int, job_s: float, poll_s: float
) -> float:
    """Miniature of the main loop: drain + admit only on rolling polls."""

    runtime = resources.MaterializationResources(
        database_url="postgresql://unused",
        max_active=4,
        remux_workers=1,
    )
    pending, done, inflight = jobs, 0, []
    next_poll = 0.0
    started = time.monotonic()
    while done < jobs:
        now = time.monotonic()
        if now >= next_poll:
            next_poll = now + poll_s
            for future in [f for f in inflight if f.done()]:
                inflight.remove(future)
                done += 1
            if pending and not inflight:
                reservation = runtime.remux_lane.try_reserve()
                assert reservation is not None
                inflight.append(reservation.submit(time.sleep, job_s))
                pending -= 1
        sleep_s = max(0.001, min(poll_s, next_poll - time.monotonic()))
        lanes = resources.scheduler_wait(runtime, sleep_s, enabled=enabled)
        next_poll = resources.rolling_poll_after_wake(
            next_poll, lanes, now=time.monotonic(), enabled=enabled
        )
    elapsed = time.monotonic() - started
    runtime.close(wait=True)
    return elapsed


def test_one_worker_lane_is_no_longer_capped_at_one_job_per_poll() -> None:
    resources = _resources_module()
    kwargs = dict(jobs=8, job_s=0.01, poll_s=0.1)

    fixed_tick = _drive_one_worker_remux_lane(resources, enabled=False, **kwargs)
    woken = _drive_one_worker_remux_lane(resources, enabled=True, **kwargs)

    # Fixed tick: every job costs at least one poll interval.
    assert fixed_tick >= 0.7
    # Wake-up: the lane is refilled as soon as a job finishes.
    assert woken < 0.45
    assert woken < fixed_tick / 2


def test_pressure_runner_exposes_the_completion_wake_ab_switch() -> None:
    spec = importlib.util.spec_from_file_location("pressure60_wake", PRESSURE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["pressure60_wake"] = module
    spec.loader.exec_module(module)

    assert module.PressureConfig.__dataclass_fields__[
        "media_worker_completion_wake"
    ].default is True
    assert module.parse_args([]).media_worker_completion_wake == "on"
    assert module.parse_args(
        ["--media-worker-completion-wake", "off"]
    ).media_worker_completion_wake == "off"
    source = PRESSURE_SCRIPT.read_text(encoding="utf-8")
    block = source[source.index("def configure_rolling_cache_workers_for_pressure") :]
    block = block[: block.index('"ROLLING_CACHE_ENABLED"')]
    assert '"MEDIA_WORKER_COMPLETION_WAKE_ENABLED"' in block


def test_main_loop_waits_on_lane_completion_instead_of_a_blind_sleep() -> None:
    source = WORKER_SOURCE.read_text(encoding="utf-8")
    loop = source[source.index("    next_rolling_cache_poll_at = 0.0") :]
    loop = loop[: loop.index("    finally:\n        runtime_resources.shutdown.begin_draining()")]

    assert "time.sleep(sleep_s)" not in loop
    assert "scheduler_wait(" in loop
    assert "rolling_poll_after_wake(" in loop
    assert "wake_lanes=%s" in loop
    assert 'MEDIA_WORKER_COMPLETION_WAKE_ENABLED' in source


def test_completion_wake_is_on_by_default_in_the_midterm_deployment() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    assert (
        'MEDIA_WORKER_COMPLETION_WAKE_ENABLED: '
        '"${MEDIA_WORKER_COMPLETION_WAKE_ENABLED:-true}"'
    ) in compose
    assert "MEDIA_WORKER_COMPLETION_WAKE_ENABLED=true" in ENV_FILE.read_text(
        encoding="utf-8"
    )
