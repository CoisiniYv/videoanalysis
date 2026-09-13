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
    ) -> list[dict[str, Any]]:
        self.calls.append({"limit": limit, "per_source_limit": per_source_limit})
        pending = [row for row in self.rows if not row["_submitted"]]

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
