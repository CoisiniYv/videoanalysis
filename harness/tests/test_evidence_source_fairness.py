"""Per-camera fairness contracts for evidence admission and remux scheduling.

Two defects let a busy camera crowd every other camera out of evidence
production under sustained load:

* the remux candidate query ordered tasks globally and applied ``LIMIT`` before
  any per-source check, so a deep backlog on one camera could fill the whole
  candidate window with rows that the source cap then rejected -- leaving remux
  workers idle while other cameras had ready tasks;
* event-worker admission counted only the legacy compatibility ``status``
  values, so V2 ``materialization_pending`` tasks were invisible to the
  per-source cap and the cap fired only once a task happened to be claimed.

These tests pin the fixed behaviour: candidate selection is fair across
sources, and the per-source knob bounds concurrent execution rather than
silently dropping evidence.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _activate(service: str, module_name: str):
    service_root = str(REPO_ROOT / "services" / service)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if service_root in sys.path:
        sys.path.remove(service_root)
    sys.path.insert(0, service_root)
    return importlib.import_module(module_name)


@pytest.fixture(autouse=True)
def _restore_import_state():
    original_path = list(sys.path)
    _RELEASE_REMUX.clear()
    yield
    _RELEASE_REMUX.set()
    sys.path[:] = original_path
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


class _RecordingCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params: dict[str, Any] = {}

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[dict[str, Any]]:
        return []

    def fetchone(self) -> tuple[int]:
        return (0,)


class _RecordingConn:
    def __init__(self) -> None:
        self.cursor_obj = _RecordingCursor()

    def cursor(self, *_args: Any, **_kwargs: Any) -> _RecordingCursor:
        return self.cursor_obj


def _cfg(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "rolling_cache_sources": (),
        "rolling_cache_materialization_max_per_poll": 16,
        "materialization_source_limit": 4,
        "rolling_cache_root": "/tmp/rolling-cache",
        "rolling_cache_materialized_root": "/tmp/rolling-cache-materialized",
        "rolling_cache_materialization_processing_deadline_seconds": 30.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_remux_candidate_query_ranks_per_source_before_the_global_limit() -> None:
    """The DB, not the post-fetch source cap, must do the fair selection."""

    worker = _activate("media-worker", "app.worker")
    conn = _RecordingConn()

    assert worker._rolling_cache_candidate_tasks(
        conn,
        _cfg(),
        limit=5,
        per_source_limit=4,
    ) == []

    sql = conn.cursor_obj.sql
    # Fairness is computed inside the query so that LIMIT truncates a
    # round-robin over sources, not one source's backlog.
    assert "ROW_NUMBER() OVER" in sql
    assert "PARTITION BY fair_source_key" in sql
    assert "AS source_rank" in sql
    assert "source_rank <= %(per_source_limit)s" in sql
    assert conn.cursor_obj.params["per_source_limit"] == 4

    # Priority classes still win outright; within a class the interleave is by
    # per-source rank, and only then by deadline/readiness/age.
    order_by = sql.split("ORDER BY")[-1]
    assert order_by.index("priority DESC") < order_by.index("source_rank ASC")
    assert order_by.index("source_rank ASC") < order_by.index(
        "materialization_due_at ASC"
    )
    assert order_by.index("materialization_due_at ASC") < order_by.index(
        "rolling_cache_ready_at ASC"
    )
    assert order_by.index("rolling_cache_ready_at ASC") < order_by.index(
        "task_created_at ASC"
    )


def test_remux_candidate_query_without_source_cap_still_interleaves() -> None:
    """Callers with no execution-side source cap keep the fair ordering."""

    worker = _activate("media-worker", "app.worker")
    conn = _RecordingConn()

    worker._rolling_cache_candidate_tasks(conn, _cfg(), limit=8)

    sql = conn.cursor_obj.sql
    assert "source_rank ASC" in sql
    # A no-cap caller must not silently drop a source's deeper backlog.
    assert conn.cursor_obj.params["per_source_limit"] >= 8


class _FairCandidateTable:
    """In-process stand-in for the remux candidate query.

    ``per_source_limit=None`` reproduces the pre-fix query: one global ordering
    truncated by ``LIMIT``. An integer reproduces the fixed query: rank within
    each source, then interleave sources inside a priority class.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        _conn: object,
        _cfg: object,
        *,
        limit: int | None = None,
        per_source_limit: int | None = None,
        turn_sources: Any = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "limit": limit,
                "per_source_limit": per_source_limit,
                "turn_sources": tuple(turn_sources or ()),
            }
        )
        pending = [row for row in self.rows if not row["_submitted"]]
        if turn_sources:
            allowed = set(turn_sources)
            pending = [row for row in pending if row["source_id"] in allowed]

        def within_source(row: dict[str, Any]) -> tuple[Any, ...]:
            return (-row["priority"], row["due"], row["task_created_at"])

        if per_source_limit is None:
            ordered = sorted(pending, key=within_source)
        else:
            ranked: list[tuple[int, dict[str, Any]]] = []
            by_source: dict[str, list[dict[str, Any]]] = {}
            for row in pending:
                by_source.setdefault(row["source_id"], []).append(row)
            for source_rows in by_source.values():
                for rank, row in enumerate(sorted(source_rows, key=within_source), 1):
                    if rank <= per_source_limit:
                        ranked.append((rank, row))
            ordered = [
                row
                for _rank, row in sorted(
                    ranked,
                    key=lambda item: (
                        -item[1]["priority"],
                        item[0],
                        item[1]["due"],
                        item[1]["task_created_at"],
                    ),
                )
            ]
        return [dict(row) for row in ordered[: max(1, int(limit or 1))]]


def _ready_rows(counts: dict[str, int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    created = 0
    for source_id, count in counts.items():
        for index in range(count):
            created += 1
            rows.append(
                {
                    "event_id": f"{source_id}-{index}",
                    "source_id": source_id,
                    "replay_source_id": source_id,
                    "priority": 50,
                    # camera-A's backlog is strictly older, so a global
                    # ordering puts every one of its tasks ahead of the rest.
                    "due": 0 if source_id == "camera-A" else 1000,
                    "task_created_at": created,
                    "rolling_cache_ready_lag_ms": 0,
                    "_submitted": False,
                }
            )
    return rows


def _runner_with_blocking_remux(
    worker: Any,
    monkeypatch: pytest.MonkeyPatch,
    table: _FairCandidateTable,
    *,
    remux_workers: int,
    source_limit: int,
):
    runtime = worker.MaterializationResources(
        database_url="postgresql://unused",
        max_active=remux_workers,
        image_workers=1,
        remux_workers=remux_workers,
        finalizer_workers=1,
        source_limit=source_limit,
        reserved_non_image=remux_workers,
    )
    monkeypatch.setattr(worker, "_rolling_cache_candidate_tasks", table)

    def prepare(_conn, _cfg, row, **_kwargs):
        for stored in table.rows:
            if stored["event_id"] == row["event_id"]:
                stored["_submitted"] = True
        return {"event_id": row["event_id"], "lease": None}

    monkeypatch.setattr(worker, "_prepare_rolling_cache_job", prepare)
    monkeypatch.setattr(
        worker,
        "_materialize_rolling_cache_job",
        lambda **_kwargs: _block_until_released(),
    )
    runner = worker._RollingCacheMaterializationRunner(
        max_workers=remux_workers,
        runtime_resources=runtime,
    )
    return runner, runtime


_RELEASE_REMUX = Event()


def _block_until_released() -> dict[str, object]:
    """Occupy a remux worker for the duration of the assertions."""

    _RELEASE_REMUX.wait(10)
    return {"updated": 0}


def _submitted_sources(runner: Any) -> list[str]:
    return [
        str(event_id).rsplit("-", 1)[0]
        for event_id, *_rest in runner._futures.values()
    ]


def test_busy_camera_backlog_does_not_starve_other_cameras_of_remux_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deep backlog on one camera must not consume the candidate window.

    camera-A has 20 ready tasks that all sort ahead of camera-B/camera-C under a
    global ordering. With five remux workers and a per-source cap of four, the
    old query returned five camera-A rows: four were admitted, the fifth was
    rejected by the source cap, and camera-B/camera-C never entered the window.
    """

    worker = _activate("media-worker", "app.worker")
    table = _FairCandidateTable(
        _ready_rows({"camera-A": 20, "camera-B": 1, "camera-C": 1})
    )
    runner, runtime = _runner_with_blocking_remux(
        worker,
        monkeypatch,
        table,
        remux_workers=5,
        source_limit=4,
    )
    try:
        runner.process(_RecordingConn(), _cfg())
        submitted = _submitted_sources(runner)

        assert "camera-B" in submitted
        assert "camera-C" in submitted
        # Every remux worker is doing useful work rather than idling behind a
        # source-capped camera.
        assert len(submitted) == 5
        assert submitted.count("camera-A") <= 4
    finally:
        runner.force_stop(_RecordingConn())
        runtime.close(wait=False)


def test_candidate_window_covers_sources_already_at_their_execution_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-fetch past rows that the source cap will certainly reject.

    Once camera-A holds all four of its source slots, a single free worker must
    still reach camera-B. Sizing the candidate window by free capacity alone
    (``available == 1``) returns one camera-A row, it is rejected, and camera-B
    waits out another poll -- forever, while camera-A keeps producing.
    """

    worker = _activate("media-worker", "app.worker")
    table = _FairCandidateTable(_ready_rows({"camera-A": 20}))
    runner, runtime = _runner_with_blocking_remux(
        worker,
        monkeypatch,
        table,
        remux_workers=5,
        source_limit=4,
    )
    try:
        runner.process(_RecordingConn(), _cfg())
        assert _submitted_sources(runner) == ["camera-A"] * 4

        # camera-B becomes ready only after camera-A has taken its four slots.
        table.rows.extend(_ready_rows({"camera-B": 1}))
        runner.process(_RecordingConn(), _cfg())

        assert "camera-B" in _submitted_sources(runner)
        assert table.calls[-1]["limit"] >= runner.max_workers
    finally:
        runner.force_stop(_RecordingConn())
        runtime.close(wait=False)


class _CountingCursor:
    """Answers admission counts from a tiny in-memory evidence_tasks table."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.sql = ""
        self.params: dict[str, Any] = {}

    def __enter__(self) -> "_CountingCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.sql = sql
        self.params = params

    def fetchone(self) -> tuple[int]:
        statuses = set(self.params.get("statuses") or ())
        materialization_statuses = set(
            self.params.get("materialization_statuses") or ()
        )
        source_id = self.params.get("source_id")
        event_type = self.params.get("event_type")
        total = 0
        for row in self.rows:
            if source_id and row.get("source_id") != source_id:
                continue
            if event_type and row.get("event_type") != event_type:
                continue
            if (
                row.get("status") in statuses
                or row.get("materialization_status") in materialization_statuses
            ):
                total += 1
        return (total,)


class _CountingConn:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.cursor_obj = _CountingCursor(rows)

    def cursor(self, *_args: Any, **_kwargs: Any) -> _CountingCursor:
        return self.cursor_obj


def _v2_pending(source_id: str, count: int, event_type: str = "intrusion"):
    return [
        {
            "source_id": source_id,
            "event_type": event_type,
            "status": "materialization_pending",
            "materialization_status": "materialization_pending",
        }
        for _ in range(count)
    ]


def test_admission_count_sees_v2_materialization_pending_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rolling-evidence V2 writes ``materialization_pending``, not a legacy status.

    Counting only ``ACTIVE_COMPATIBILITY_TASK_STATUSES`` made every queued V2
    task invisible, so the global and per-event-type backlog guards only saw a
    task during the brief window in which it was claimed.
    """

    _activate("event-worker", "app.repository")
    from app import repository

    conn = _CountingConn(_v2_pending("camera-A", 4))
    assert repository._active_admission_count(conn, source_id="camera-A") == 4

    sql = conn.cursor_obj.sql
    assert "materialization_status" in sql
    assert "materialization_pending" in (
        conn.cursor_obj.params.get("materialization_statuses") or ()
    )


def test_global_backlog_guard_counts_queued_v2_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL", "3")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE", "0")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE", "")
    _activate("event-worker", "app.repository")
    from app import repository

    decision = repository._evidence_admission_decision(
        _CountingConn(_v2_pending("camera-A", 3)),
        {"event_type": "intrusion"},
        initial_status="materialization_pending",
        source_id="camera-A",
        event_type="intrusion",
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "admission_global_active_limit_reached"
    assert decision["observed"] == 3


def test_per_source_limit_bounds_execution_not_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MAX_ACTIVE_PER_SOURCE`` is a concurrency cap, not an evidence drop.

    Denying admission here wrote ``materialization_skipped`` -- the evidence was
    never produced at all. The per-source bound belongs to the media-worker
    ``SourceSlotRegistry``, which limits how many of a camera's tasks run at
    once while every accepted task still reaches a terminal, explainable state.
    """

    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL", "0")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE", "1")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE", "")
    monkeypatch.delenv("EVIDENCE_ADMISSION_SOURCE_LIMIT_MODE", raising=False)
    _activate("event-worker", "app.repository")
    from app import repository

    decision = repository._evidence_admission_decision(
        _CountingConn(_v2_pending("camera-A", 9)),
        {"event_type": "intrusion"},
        initial_status="materialization_pending",
        source_id="camera-A",
        event_type="intrusion",
    )

    assert decision["allowed"] is True
    assert decision["source_limit_mode"] == "execution_concurrency"
    assert decision["source_observed"] == 9


def test_legacy_per_source_skip_mode_remains_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old drop-on-cap behaviour stays reachable as an explicit opt-in."""

    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL", "0")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE", "1")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE", "")
    monkeypatch.setenv("EVIDENCE_ADMISSION_SOURCE_LIMIT_MODE", "skip")
    _activate("event-worker", "app.repository")
    from app import repository

    decision = repository._evidence_admission_decision(
        _CountingConn(_v2_pending("camera-A", 2)),
        {"event_type": "intrusion"},
        initial_status="materialization_pending",
        source_id="camera-A",
        event_type="intrusion",
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "admission_source_active_limit_reached"
    assert decision["observed"] == 2


def test_legacy_per_source_skip_mode_never_drops_high_priority_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watchlist hit must not be discarded because a routine clip is running."""

    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL", "0")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE", "1")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE", "")
    monkeypatch.setenv("EVIDENCE_ADMISSION_SOURCE_LIMIT_MODE", "skip")
    _activate("event-worker", "app.repository")
    from app import repository

    decision = repository._evidence_admission_decision(
        _CountingConn(_v2_pending("camera-A", 2)),
        {"event_type": "watchlist_hit"},
        initial_status="materialization_pending",
        source_id="camera-A",
        event_type="watchlist_hit",
    )

    assert decision["allowed"] is True
    assert decision["source_limit_bypassed_for_priority"] is True


# --------------------------------------------------------------------------
# Opt-in proof against a real PostgreSQL planner.
#
# Set EVIDENCE_FAIRNESS_TEST_DATABASE_URL to a disposable database. The test
# builds its own fixture schema covering only the columns the candidate query
# reads, so it needs no migration state, and drops it again afterwards.
# --------------------------------------------------------------------------

_FIXTURE_SCHEMA = "evidence_fairness_fixture"

_FIXTURE_DDL = f"""
DROP SCHEMA IF EXISTS {_FIXTURE_SCHEMA} CASCADE;
CREATE SCHEMA {_FIXTURE_SCHEMA};
CREATE TABLE {_FIXTURE_SCHEMA}.events (
    id uuid PRIMARY KEY,
    payload jsonb,
    frame_uuid text,
    created_at timestamptz DEFAULT now()
);
CREATE TABLE {_FIXTURE_SCHEMA}.evidence_bundles (event_id uuid PRIMARY KEY);
CREATE TABLE {_FIXTURE_SCHEMA}.evidence_tasks (
    event_id uuid PRIMARY KEY,
    source_id text,
    replay_source_id text,
    camera_id text,
    event_type text,
    event_ts_ms bigint,
    pre_seconds numeric,
    post_seconds numeric,
    replay_window jsonb,
    priority int,
    created_at timestamptz DEFAULT now(),
    materialization_status text,
    task_type text,
    clip_required boolean,
    materialization_ready_at timestamptz,
    materialization_next_attempt_at timestamptz,
    materialization_owner text,
    replay_slot_status text
);
"""

# The rotation ledger is applied from the real migration so the fixture cannot
# drift from what production runs.
_ROTATION_MIGRATION = (
    REPO_ROOT / "db" / "migrations" / "033_evidence_source_rotation.sql"
)


@pytest.fixture()
def fairness_db():
    import os
    import uuid

    database_url = os.getenv("EVIDENCE_FAIRNESS_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("EVIDENCE_FAIRNESS_TEST_DATABASE_URL is not configured")
    psycopg = pytest.importorskip("psycopg")

    conn = psycopg.connect(database_url, autocommit=True)
    conn.execute(_FIXTURE_DDL)
    conn.execute(f"SET search_path TO {_FIXTURE_SCHEMA}")
    conn.execute(_ROTATION_MIGRATION.read_text(encoding="utf-8"))

    def add(source_id: str, count: int, *, priority: int = 50, age_s: int = 0) -> None:
        for _ in range(count):
            event_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO events (id, payload, frame_uuid)"
                " VALUES (%s, '{}'::jsonb, 'frame')",
                (event_id,),
            )
            conn.execute(
                """
                INSERT INTO evidence_tasks (
                    event_id, source_id, replay_source_id, camera_id, event_type,
                    event_ts_ms, pre_seconds, post_seconds, replay_window, priority,
                    materialization_status, task_type, clip_required,
                    materialization_ready_at, materialization_owner, created_at
                ) VALUES (
                    %s, %s, %s, %s, 'intrusion', 0, 5, 10, '{}'::jsonb, %s,
                    'materialization_pending', 'video', true,
                    now() - (%s || ' seconds')::interval, 'rolling',
                    now() - (%s || ' seconds')::interval
                )
                """,
                (event_id, source_id, source_id, source_id, priority, age_s, age_s),
            )

    try:
        yield conn, add
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {_FIXTURE_SCHEMA} CASCADE")
        conn.close()


def test_real_postgres_candidate_window_round_robins_across_cameras(
    fairness_db,
) -> None:
    """The planner, not just the SQL text, must produce a fair window."""

    worker = _activate("media-worker", "app.worker")
    conn, add = fairness_db

    # camera-A's backlog is strictly older, so a global ordering would place
    # all twenty of its tasks ahead of camera-B and camera-C.
    add("camera-A", 20, age_s=600)
    add("camera-B", 1, age_s=10)
    add("camera-C", 1, age_s=10)

    rows = worker._rolling_cache_candidate_tasks(
        conn,
        _cfg(),
        limit=5,
        per_source_limit=4,
    )
    sources = [row["source_id"] for row in rows]

    assert len(rows) == 5
    assert "camera-B" in sources
    assert "camera-C" in sources
    assert sources.count("camera-A") <= 4

    ranks: dict[str, list[int]] = {}
    for row in worker._rolling_cache_candidate_tasks(
        conn,
        _cfg(),
        limit=30,
        per_source_limit=4,
    ):
        ranks.setdefault(str(row["source_id"]), []).append(int(row["source_rank"]))
    assert ranks["camera-A"] == [1, 2, 3, 4]
    assert ranks["camera-B"] == [1]


def test_real_postgres_priority_class_still_outranks_the_round_robin(
    fairness_db,
) -> None:
    worker = _activate("media-worker", "app.worker")
    conn, add = fairness_db

    add("camera-A", 20, age_s=600)
    add("camera-B", 1, age_s=10)
    add("camera-D", 1, priority=100, age_s=1)

    rows = worker._rolling_cache_candidate_tasks(
        conn,
        _cfg(),
        limit=5,
        per_source_limit=4,
    )

    assert rows[0]["source_id"] == "camera-D"
    assert rows[0]["priority"] == 100


def test_real_postgres_uncapped_window_keeps_every_sources_backlog(
    fairness_db,
) -> None:
    """A caller with no execution-side cap must not lose a source's tasks."""

    worker = _activate("media-worker", "app.worker")
    conn, add = fairness_db

    add("camera-A", 20, age_s=600)
    add("camera-B", 1, age_s=10)

    rows = worker._rolling_cache_candidate_tasks(conn, _cfg(), limit=30)
    sources = [row["source_id"] for row in rows]

    assert len(rows) == 21
    assert sources.count("camera-A") == 20
    assert sources.count("camera-B") == 1


# --------------------------------------------------------------------------
# Boundary cases for the fairness claim.
#
# Per-source ranking makes a SINGLE candidate window fair. It does not by
# itself bound how long a camera waits across many polls: ROW_NUMBER has no
# memory of which sources were served last time. These tests pin where the
# guarantee holds and where it does not.
# --------------------------------------------------------------------------


def test_high_priority_source_at_its_cap_does_not_hide_a_normal_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rank cap, not luck, is what leaves room for the second camera.

    camera-A holds all four of its slots and has twenty high-priority tasks
    queued behind them. Ranking caps A's contribution at `per_source_limit`
    rows, so the fifth row of a five-row window belongs to camera-B and the one
    free worker can be used. Without the cap, A's priority would fill the
    window and the slot would idle.
    """

    worker = _activate("media-worker", "app.worker")
    rows = _ready_rows({"camera-A": 20, "camera-B": 1})
    for row in rows:
        if row["source_id"] == "camera-A":
            row["priority"] = 100
    table = _FairCandidateTable(rows)
    runner, runtime = _runner_with_blocking_remux(
        worker,
        monkeypatch,
        table,
        remux_workers=5,
        source_limit=4,
    )
    try:
        runner.process(_RecordingConn(), _cfg())
        submitted = _submitted_sources(runner)

        assert submitted.count("camera-A") == 4
        assert "camera-B" in submitted
    finally:
        runner.force_stop(_RecordingConn())
        runtime.close(wait=False)


def test_rows_rejected_after_fetch_are_deferred_not_retried_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `max_workers` window bound covers source-cap rejections only.

    A row can also be rejected after the fetch because its footage is not
    covered yet. That consumes window slots the bound does not account for, so
    a poll can admit less than its free capacity. It is self-correcting rather
    than a permanent block only because the prepare path defers the task,
    pushing `materialization_next_attempt_at` forward so the row leaves the
    candidate set on the next poll. If that defer is ever removed, the same
    rows return every poll and the window is blocked for good.
    """

    worker = _activate("media-worker", "app.worker")
    source = inspect.getsource(worker._prepare_rolling_cache_job)

    assert "RollingCacheCoverageMiss" in source
    assert "_defer_rolling_cache_task" in source
    # The coverage-miss handler must defer before giving the row up.
    miss_handler = source.split("except RollingCacheCoverageMiss")[1]
    assert "_defer_rolling_cache_task" in miss_handler.split("return None")[0]


# Controlled-condition bound: 40 sources, 5 slots per round, same priority,
# every source continuously runnable and every claim completing immediately.
# Eight rounds is ceil(40 / 5) -- a true rotation cannot need more, and any
# regression that reintroduces backlog-driven selection blows straight past it.
# This counts SCHEDULING ROUNDS, not seconds, and says nothing about how long a
# clip takes to produce.
_FAIR_ROUNDS_BOUND = 8


def _rotate_once(worker: Any, conn: Any, *, slots: int, source_cap: int) -> list[str]:
    """One scheduler round: pick turns, fetch from them, claim, mark served."""

    from app import source_rotation

    turns = source_rotation.next_source_turns(
        conn,
        lane=source_rotation.REMUX_LANE,
        statuses=worker.ROLLING_CACHE_TASK_STATUSES,
        limit=slots,
        task_predicate=(
            "COALESCE(et.task_type, '') <> 'image_only'"
            " AND COALESCE(et.clip_required, false) = true"
        ),
    )
    turn_sources = tuple(turn.source_id for turn in turns)
    if not turn_sources:
        return []
    rows = worker._rolling_cache_candidate_tasks(
        conn,
        _cfg(),
        limit=slots,
        per_source_limit=source_cap,
        turn_sources=turn_sources,
    )
    served: list[str] = []
    for row in rows:
        source_id = str(row["source_id"])
        conn.execute(
            "UPDATE evidence_tasks SET materialization_status = 'materialized'"
            " WHERE event_id = %s",
            (row["event_id"],),
        )
        source_rotation.mark_served(
            conn,
            lane=source_rotation.REMUX_LANE,
            source_id=source_id,
        )
        served.append(source_id)
    return served


def test_real_postgres_every_camera_is_served_within_the_rotation_bound(
    fairness_db,
) -> None:
    """Bounded wait when there are far more cameras than scheduler slots.

    Five cameras carry a 100-task backlog and would, under pure age ordering,
    supply the oldest candidate forever; thirty-five carry one newer task each.
    Nothing here is blocked by execution capacity -- each round completes
    exactly what it selected -- so only selection order decides the outcome.
    """

    worker = _activate("media-worker", "app.worker")
    conn, add = fairness_db

    for index in range(1, 6):
        add(f"camera-{index:02d}", 100, age_s=600)
    for index in range(6, 41):
        add(f"camera-{index:02d}", 1, age_s=60)

    all_cameras = {f"camera-{index:02d}" for index in range(1, 41)}
    served: set[str] = set()
    rounds_used = 0
    for round_index in range(1, _FAIR_ROUNDS_BOUND + 1):
        picked = _rotate_once(worker, conn, slots=5, source_cap=4)
        if not picked:
            break
        rounds_used = round_index
        served.update(picked)
        if all_cameras <= served:
            break

    missing = sorted(all_cameras - served)
    assert not missing, (
        f"{len(missing)} of 40 cameras were not served within "
        f"{_FAIR_ROUNDS_BOUND} rounds: {missing[:5]}..."
    )
    assert rounds_used <= _FAIR_ROUNDS_BOUND


def test_real_postgres_rotation_does_not_advance_on_a_refused_claim(
    fairness_db,
) -> None:
    """Selection is not service.

    A camera whose row is rejected by the execution cap, or deferred because
    its footage is not covered yet, must keep its place in the rotation. If
    selection advanced the cursor, such a camera would rotate to the back
    forever while never producing anything.
    """

    _activate("media-worker", "app.worker")
    from app import source_rotation

    conn, add = fairness_db
    add("camera-A", 3, age_s=60)
    add("camera-B", 3, age_s=60)

    def turn_order() -> list[str]:
        return [
            turn.source_id
            for turn in source_rotation.next_source_turns(
                conn,
                lane=source_rotation.REMUX_LANE,
                statuses=("manifest_ready", "materialization_pending"),
                limit=10,
                task_predicate="true",
            )
        ]

    before = turn_order()
    assert before == ["camera-A", "camera-B"]

    # A poll that selects camera-A but never claims anything for it.
    assert turn_order()[0] == "camera-A"
    assert turn_order() == before

    # Only a real claim moves it.
    source_rotation.mark_served(
        conn,
        lane=source_rotation.REMUX_LANE,
        source_id="camera-A",
    )
    assert turn_order() == ["camera-B", "camera-A"]


def test_real_postgres_image_and_remux_lanes_rotate_independently(
    fairness_db,
) -> None:
    """A clip being remuxed for a camera is not a snapshot for that camera."""

    _activate("media-worker", "app.worker")
    from app import source_rotation

    conn, add = fairness_db
    add("camera-A", 1, age_s=60)
    add("camera-B", 1, age_s=60)

    source_rotation.mark_served(
        conn,
        lane=source_rotation.REMUX_LANE,
        source_id="camera-A",
    )

    def order(lane: str) -> list[str]:
        return [
            turn.source_id
            for turn in source_rotation.next_source_turns(
                conn,
                lane=lane,
                statuses=("manifest_ready", "materialization_pending"),
                limit=10,
                task_predicate="true",
            )
        ]

    assert order(source_rotation.REMUX_LANE) == ["camera-B", "camera-A"]
    # The image lane has served nobody, so its order is untouched.
    assert order(source_rotation.IMAGE_LANE) == ["camera-A", "camera-B"]


def test_real_postgres_high_priority_source_skips_the_rotation_queue(
    fairness_db,
) -> None:
    """Urgency outranks the round-robin; a watchlist hit does not wait a turn."""

    _activate("media-worker", "app.worker")
    from app import source_rotation

    conn, add = fairness_db
    add("camera-A", 1, age_s=600)
    add("camera-B", 1, age_s=300)
    add("camera-Z", 1, priority=100, age_s=1)

    # camera-Z was served most recently, so pure rotation would put it last.
    for source_id in ("camera-A", "camera-B", "camera-Z"):
        source_rotation.mark_served(
            conn,
            lane=source_rotation.REMUX_LANE,
            source_id=source_id,
        )

    turns = source_rotation.next_source_turns(
        conn,
        lane=source_rotation.REMUX_LANE,
        statuses=("manifest_ready", "materialization_pending"),
        limit=3,
        task_predicate="true",
    )
    assert [turn.source_id for turn in turns][0] == "camera-Z"


def test_real_postgres_starved_cameras_actually_reach_their_business_deadline(
    fairness_db,
) -> None:
    """Connect starvation to the symptom, with time actually advancing.

    "Not served for sixty rounds" is not the same claim as "expired". This test
    advances each task's `materialization_deadline_at` past `now()` and then
    applies the same predicate the recovery pass uses, so the outcome is an
    expiry count per camera rather than an inference from round numbers.

    Without rotation the busy cameras hold the window and every quiet camera
    expires unserved. With rotation every camera is served before the deadline
    is reached.
    """

    worker = _activate("media-worker", "app.worker")
    conn, add = fairness_db
    conn.execute(
        "ALTER TABLE evidence_tasks"
        " ADD COLUMN IF NOT EXISTS materialization_deadline_at timestamptz"
    )

    for index in range(1, 6):
        add(f"camera-{index:02d}", 100, age_s=600)
    for index in range(6, 41):
        add(f"camera-{index:02d}", 1, age_s=60)
    # Every task is 60s from its 300s business deadline at the start.
    conn.execute(
        "UPDATE evidence_tasks"
        " SET materialization_deadline_at = now() + interval '60 seconds'"
    )

    def expired_per_camera() -> dict[str, int]:
        # The predicate the rolling recovery pass uses for ready-but-unclaimed
        # tasks that have run out of business time.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_id, COUNT(*)
                FROM evidence_tasks
                WHERE materialization_status IN (
                          'manifest_ready', 'materialization_pending'
                      )
                  AND materialization_deadline_at IS NOT NULL
                  AND materialization_deadline_at <= now()
                GROUP BY source_id
                """
            )
            return {str(row[0]): int(row[1]) for row in cur.fetchall()}

    served: set[str] = set()
    for _ in range(_FAIR_ROUNDS_BOUND):
        picked = _rotate_once(worker, conn, slots=5, source_cap=4)
        if not picked:
            break
        served.update(picked)

    # Time passes: everything still queued is now past its deadline.
    conn.execute(
        "UPDATE evidence_tasks"
        " SET materialization_deadline_at = now() - interval '1 second'"
        " WHERE materialization_status IN"
        " ('manifest_ready', 'materialization_pending')"
    )
    expired = expired_per_camera()

    quiet = {f"camera-{index:02d}" for index in range(6, 41)}
    # A quiet camera had exactly one task; being served means it has none left
    # to expire. That is the difference between producing evidence and
    # producing `business_deadline_expired`.
    assert quiet <= served
    quiet_expired = {camera for camera in quiet if expired.get(camera, 0) > 0}
    assert not quiet_expired, (
        f"{len(quiet_expired)} quiet cameras expired without ever being "
        f"scheduled: {sorted(quiet_expired)[:5]}..."
    )


# --------------------------------------------------------------------------
# Admission floor.
#
# Scheduler fairness cannot recover a task that was never enqueued, so the
# global backlog guard needs a per-source floor of its own. One evidence task
# is one event's evidence request -- evidence_tasks is keyed by event_id and a
# task is either image_only or video, never both -- so a reservation counted in
# tasks is a reservation counted in events.
# --------------------------------------------------------------------------


class _PoolCursor:
    """Serves both the per-source count and the shared-pool aggregate."""

    def __init__(self, per_source: dict[str, int], reserved: int) -> None:
        self.per_source = per_source
        self.reserved = reserved
        self.sql = ""
        self.params: dict[str, Any] = {}

    def __enter__(self) -> "_PoolCursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.sql = sql
        self.params = params

    def fetchone(self) -> tuple[int]:
        if "GREATEST" in self.sql:
            reserved = int(self.params.get("reserved", 0))
            return (
                sum(max(0, active - reserved) for active in self.per_source.values()),
            )
        source_id = self.params.get("source_id")
        if source_id:
            return (self.per_source.get(str(source_id), 0),)
        return (sum(self.per_source.values()),)


class _PoolConn:
    def __init__(self, per_source: dict[str, int], reserved: int = 1) -> None:
        self.cursor_obj = _PoolCursor(per_source, reserved)

    def cursor(self, *_args: Any, **_kwargs: Any) -> _PoolCursor:
        return self.cursor_obj


def _decide(repository: Any, conn: Any, source_id: str, event_type: str = "intrusion"):
    return repository._evidence_admission_decision(
        conn,
        {"event_type": event_type},
        initial_status="materialization_pending",
        source_id=source_id,
        event_type=event_type,
    )


@pytest.fixture()
def reservation_env(monkeypatch: pytest.MonkeyPatch):
    sources = [f"camera-{index:02d}" for index in range(1, 41)]
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL", "240")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE", "0")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE", "")
    monkeypatch.setenv("EVIDENCE_ADMISSION_RESERVED_PER_SOURCE", "1")
    monkeypatch.setenv("EVIDENCE_ADMISSION_RESERVED_SOURCES", ",".join(sources))
    _activate("event-worker", "app.repository")
    from app import repository

    return repository, sources


def test_a_quiet_camera_keeps_its_slot_when_a_busy_camera_floods_the_backlog(
    reservation_env,
) -> None:
    """The floor is what the symptom needs: a busy camera must not spend it."""

    repository, sources = reservation_env
    # 240 global - (40 sources x 1 reserved) = 200 shared. Six cameras holding
    # 40 each charge 6 x 39 = 234 to the shared pool, exhausting it.
    flooded = {f"camera-{index:02d}": 40 for index in range(1, 7)}
    conn = _PoolConn(flooded, reserved=1)

    busy = _decide(repository, conn, "camera-01")
    assert busy["allowed"] is False
    assert busy["reason"] == "admission_shared_pool_exhausted"

    quiet = _decide(repository, conn, "camera-30")
    assert quiet["allowed"] is True
    assert quiet["reason"] == "admitted_source_reservation"
    assert quiet["source_reserved"] == 1


def test_a_camera_past_its_reservation_competes_only_for_the_shared_pool(
    reservation_env,
) -> None:
    repository, _sources = reservation_env
    # 240 global - (40 sources x 1 reserved) = 200 shared. One camera holding
    # 150 has charged 149 to the shared pool and is still under it.
    conn = _PoolConn({"camera-01": 150}, reserved=1)

    decision = _decide(repository, conn, "camera-01")
    assert decision["allowed"] is True

    # Two cameras together exceeding the shared remainder are refused, while
    # every untouched camera keeps its floor.
    conn = _PoolConn({"camera-01": 150, "camera-02": 60}, reserved=1)
    assert _decide(repository, conn, "camera-01")["allowed"] is False
    assert _decide(repository, conn, "camera-40")["allowed"] is True


def test_event_type_budget_cannot_take_back_the_reserved_slot(
    reservation_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A floor granted and then revoked one check later is not a floor."""

    repository, _sources = reservation_env
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE", "intrusion:1")
    conn = _PoolConn({"camera-01": 40}, reserved=1)

    decision = _decide(repository, conn, "camera-30")
    assert decision["allowed"] is True
    assert decision["reason"] == "admitted_source_reservation"


def test_reservation_is_a_floor_not_a_ceiling(reservation_env) -> None:
    """Concurrency can only over-admit, never revoke someone else's floor.

    Admission checks and inserts are not one transaction, so two concurrent
    requests for the same camera can both observe an empty reservation and both
    be admitted. That overshoots the shared pool by one -- acceptable for an
    overload guard -- but it cannot consume another camera's reservation,
    because the shared-pool total subtracts every source's floor before summing.
    """

    repository, _sources = reservation_env
    # camera-01 raced itself to 2 outstanding with a reservation of 1.
    conn = _PoolConn({"camera-01": 2}, reserved=1)

    # It is charged for exactly the overshoot, not for its reserved task.
    assert repository._shared_admission_used(conn, reserved_per_source=1) == 1
    # Every other camera's floor is untouched.
    assert _decide(repository, conn, "camera-07")["reason"] == (
        "admitted_source_reservation"
    )


def test_reservation_off_restores_the_plain_global_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL", "10")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE", "0")
    monkeypatch.setenv("EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE", "")
    monkeypatch.setenv("EVIDENCE_ADMISSION_RESERVED_PER_SOURCE", "0")
    monkeypatch.delenv("EVIDENCE_ADMISSION_RESERVED_SOURCES", raising=False)
    _activate("event-worker", "app.repository")
    from app import repository

    decision = _decide(repository, _PoolConn({"camera-01": 10}, reserved=0), "camera-01")
    assert decision["allowed"] is False
    assert decision["reason"] == "admission_global_active_limit_reached"
