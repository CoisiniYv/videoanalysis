"""Contracts for same-source evidence coverage merge."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVENT_WORKER_DIR = str(ROOT / "services" / "event-worker")
if EVENT_WORKER_DIR in sys.path:
    sys.path.remove(EVENT_WORKER_DIR)
sys.path.insert(0, EVENT_WORKER_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app import repository  # noqa: E402


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._row: tuple[Any, ...] | None = None

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: object | None = None) -> None:
        self._conn.executed.append((sql, params))
        if "SELECT COALESCE(link.bundle_event_id" in sql:
            self._row = (self._conn.parent_event_id,)
        elif "SELECT event_ts_ms, pre_seconds, post_seconds" in sql:
            self._row = self._conn.parent_task_row
        else:
            self._row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _Conn:
    def __init__(
        self,
        *,
        parent_event_id: str = "00000000-0000-4000-8000-000000000001",
        parent_task_row: dict[str, Any] | None = None,
    ) -> None:
        self.parent_event_id = parent_event_id
        self.parent_task_row = parent_task_row
        self.executed: list[tuple[str, object | None]] = []

    def cursor(self, *_args: object, **_kwargs: object) -> _Cursor:
        return _Cursor(self)


def test_coverage_parent_lookup_is_disabled_by_default(monkeypatch: Any) -> None:
    monkeypatch.delenv("EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED", raising=False)
    conn = _Conn()

    parent = repository._coverage_parent_event_id(
        conn,
        event_id="00000000-0000-4000-8000-000000000002",
        source_id="source_lab",
        event_type="intrusion",
        event_ts_ms=1_765_000_000_000,
    )

    assert parent is None
    assert conn.executed == []


def test_coverage_parent_lookup_uses_same_source_merge_type_window(monkeypatch: Any) -> None:
    monkeypatch.setenv("EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED", "true")
    monkeypatch.setenv("EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS", "30")
    monkeypatch.setenv("EVIDENCE_EVENT_COVERAGE_EVENT_TYPES", "intrusion")
    conn = _Conn(parent_event_id="00000000-0000-4000-8000-000000000003")

    parent = repository._coverage_parent_event_id(
        conn,
        event_id="00000000-0000-4000-8000-000000000002",
        source_id="source_lab",
        event_type="intrusion",
        event_ts_ms=1_765_000_000_000,
    )

    assert parent == "00000000-0000-4000-8000-000000000003"
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "et.source_id = %(source_id)s" in sql
    assert "et.event_type = ANY(%(event_types)s)" in sql
    assert "evidence_event_links" in sql
    assert isinstance(params, dict)
    assert params["event_types"] == ["intrusion"]
    assert params["start_ms"] == 1_765_000_000_000 - 30_000
    assert params["end_ms"] == 1_765_000_000_000 + 30_000


def test_coverage_parent_extension_respects_max_duration(monkeypatch: Any) -> None:
    monkeypatch.setenv("EVIDENCE_COVERAGE_PARENT_MAX_DURATION_SECONDS", "20")
    conn = _Conn(
        parent_task_row={
            "event_ts_ms": 1_765_000_000_000,
            "pre_seconds": 5,
            "post_seconds": 5,
            "materialization_status": "materialization_pending",
        }
    )

    result = repository._extend_coverage_parent_window(
        conn,
        parent_event_id="00000000-0000-4000-8000-000000000003",
        child_event_ts_ms=1_765_000_035_000,
        child_pre_seconds=5,
        child_post_seconds=5,
    )

    assert result["reason"] == "parent_max_duration_exceeded"
    assert result["required_post_seconds"] == 40
    assert result["max_duration_seconds"] == 20
    assert not any("UPDATE evidence_tasks" in sql for sql, _params in conn.executed)


def test_evidence_event_link_migration_and_alias_path_are_contract_aligned() -> None:
    migration = (ROOT / "db" / "migrations" / "023_evidence_event_links.sql").read_text()
    worker = (ROOT / "services" / "media-worker" / "app" / "worker.py").read_text()
    repository_source = (ROOT / "services" / "event-worker" / "app" / "repository.py").read_text()

    assert "CREATE TABLE IF NOT EXISTS evidence_event_links" in migration
    assert "event_id UUID PRIMARY KEY" in migration
    assert "bundle_event_id UUID NOT NULL" in migration
    assert "CHECK (event_id <> bundle_event_id)" in migration
    assert "def _upsert_covered_event_aliases" in worker
    assert "def _reconcile_covered_event_aliases" in worker
    assert "INSERT INTO evidence_bundles" in worker
    assert "INSERT INTO evidence_artifacts" in worker
    assert "INSERT INTO evidence_frame_timeline" in worker
    assert "INSERT INTO evidence_overlay_segments" in worker
    assert "materialized_alias" in worker
    assert "child_bundle.event_id IS NULL" in worker
    assert "materialization_defer_reason" in repository_source
    assert "covered_by_existing_evidence" in repository_source
    assert "materialization_skipped" not in worker[
        worker.index("def _upsert_covered_event_aliases") : worker.index(
            "def _prune_success_evidence_sidecars"
        )
    ]
