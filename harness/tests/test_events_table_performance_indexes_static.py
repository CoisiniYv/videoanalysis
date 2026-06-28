from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migrations" / "018_events_table_performance_indexes.sql"
API_REPOSITORY = ROOT / "services" / "api" / "app" / "repositories" / "events.py"
EVENT_WORKER_REPOSITORY = ROOT / "services" / "event-worker" / "app" / "repository.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_events_table_performance_migration_targets_confirmed_query_shapes() -> None:
    migration = _text(MIGRATION)
    api_repository = _text(API_REPOSITORY)
    event_worker_repository = _text(EVENT_WORKER_REPOSITORY)

    assert "idx_events_created_at_desc" in migration
    assert "ON events (created_at DESC)" in migration
    assert "ORDER BY e.created_at DESC" in api_repository

    assert "idx_events_event_type_created_at_desc" in migration
    assert "ON events (event_type, created_at DESC)" in migration
    assert "e.event_type = %(event_type)s" in api_repository

    assert "idx_events_source_type_created_at_desc_unsuppressed" in migration
    assert "ON events (source_id, event_type, created_at DESC)" in migration
    assert "has_event_type_since_ts_ms" in event_worker_repository

    assert "idx_events_camera_type_ts_desc_unsuppressed" in migration
    assert "ON events (camera_id, event_type, event_ts_ms DESC)" in migration
    assert "ORDER BY event_ts_ms DESC" in event_worker_repository


def test_events_table_performance_indexes_are_live_safe_and_additive() -> None:
    migration = _text(MIGRATION)

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in migration
    assert "must not run inside a transaction block" in migration
    assert "Do not apply it during an active pressure-test window" in migration
    assert "ALTER TABLE" not in migration
    assert "DROP " not in migration
    assert "WHERE COALESCE(status, 'new') <> 'suppressed'" in migration
    assert "COMMENT ON INDEX idx_events_camera_type_ts_desc_unsuppressed" in migration
