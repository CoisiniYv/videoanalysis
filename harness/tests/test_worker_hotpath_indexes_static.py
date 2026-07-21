"""Worker hot-path migration and scheduler cadence contracts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migrations" / "032_worker_hotpath_regression_indexes.sql"
MEDIA_CONFIG = ROOT / "services" / "media-worker" / "app" / "config.py"
MEDIA_WORKER = ROOT / "services" / "media-worker" / "app" / "worker.py"


def test_worker_hotpath_indexes_are_concurrent_additive_and_predicate_aligned() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in sql
    assert "evidence_tasks_cleanup_pending_idx" in sql
    assert "cleanup_audit->'sink_output'->>'status' = 'cleanup_pending'" in sql
    assert "idx_events_camera_algorithm_ts_desc_unsuppressed" in sql
    assert "COALESCE(NULLIF(algorithm_type, ''), event_type)" in sql
    assert "DROP " not in sql
    assert "DELETE " not in sql


def test_cleanup_recovery_has_an_independent_low_frequency_cadence() -> None:
    config = MEDIA_CONFIG.read_text(encoding="utf-8")
    worker = MEDIA_WORKER.read_text(encoding="utf-8")

    assert "MEDIA_WORKER_CLEANUP_RECOVERY_POLL_INTERVAL_S" in config
    assert '"30"' in config
    assert "next_cleanup_recovery_poll_at" in worker
    assert "cleanup_recovery_due" in worker
    assert "cleanup_rows_scanned" in worker
    assert "cleanup_retry_pending" in worker
