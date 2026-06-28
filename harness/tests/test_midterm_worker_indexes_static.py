from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migrations" / "014_worker_media_indexes.sql"
QUEUE_MIGRATION = ROOT / "db" / "migrations" / "019_media_worker_events_queue_indexes.sql"
EVIDENCE_QUEUE_MIGRATION = ROOT / "db" / "migrations" / "020_evidence_queue_playable_indexes.sql"
MEDIA_WORKER = ROOT / "services" / "media-worker" / "app" / "worker.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_media_worker_index_migration_targets_worker_query_shapes() -> None:
    migration = _text(MIGRATION)
    worker = _text(MEDIA_WORKER)

    assert "events_media_clip_status_with_clip_idx" in migration
    assert "events_media_snapshot_queue_idx" in migration
    assert "events_media_snapshot_not_required_idx" in migration
    assert "events_media_annotation_queue_idx" in migration

    assert "(payload -> 'media' ->> 'clip_status')" in migration
    assert "(payload -> 'media' ->> 'snapshot_status')" in migration
    assert "(payload -> 'media' ->> 'snapshot_required')" in migration
    assert "(payload -> 'media' ->> 'annotated_snapshot_status')" in migration

    assert "payload -> 'media' ->> 'clip_status'" in worker
    assert "payload -> 'media' ->> 'snapshot_status'" in worker
    assert "payload -> 'media' ->> 'snapshot_required'" in worker
    assert "payload -> 'media' ->> 'annotated_snapshot_status'" in worker


def test_media_worker_indexes_are_partial_and_additive() -> None:
    migration = _text(MIGRATION)

    assert "CREATE INDEX IF NOT EXISTS" in migration
    assert "ALTER TABLE" not in migration
    assert "DROP " not in migration
    assert "WHERE clip_path IS NOT NULL" in migration
    assert "AND clip_path <> ''" in migration
    assert "WHERE (payload -> 'media' ->> 'snapshot_status') = 'ready'" in migration
    assert "COMMENT ON INDEX events_media_snapshot_queue_idx" in migration


def test_media_worker_queue_indexes_target_periodic_events_scans() -> None:
    migration = _text(QUEUE_MIGRATION)
    worker = _text(MEDIA_WORKER)

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in migration
    assert "must not run inside a transaction block" in migration
    assert "Do not apply it during an active pressure-test window" in migration
    assert "ALTER TABLE" not in migration
    assert "DROP " not in migration

    assert "idx_events_media_generated_clip_queue" in migration
    assert "(payload -> 'media' ->> 'clip_status') = 'generated'" in migration
    assert "_promote_generated_clips_to_ready" in worker

    assert "idx_events_media_clean_clip_snapshot_queue" in migration
    assert "(payload -> 'media' ->> 'clip_status') IN ('ready', 'generated')" in migration
    assert "_snapshot_needed" in worker

    assert "idx_events_media_snapshot_not_required_queue" in migration
    assert "snapshot_required" in migration
    assert "_mark_not_required" in worker

    assert "idx_events_media_annotation_pending_queue" in migration
    assert "annotated_snapshot_status" in migration
    assert "_annotation_needed" in worker


def test_evidence_queue_indexes_target_downstream_pressure_queries() -> None:
    migration = _text(EVIDENCE_QUEUE_MIGRATION)
    worker = _text(MEDIA_WORKER)

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in migration
    assert "must not run inside a transaction block" in migration
    assert "Do not apply it during an active pressure-test window" in migration
    assert "ALTER TABLE" not in migration
    assert "DROP " not in migration

    assert "idx_evidence_bundles_playable_recent" in migration
    assert "ON evidence_bundles (event_created_at DESC)" in migration
    assert "raw_clip_uri IS NOT NULL" in migration
    assert "raw_clip_size_bytes > 0" in migration

    assert "idx_evidence_tasks_pending_priority_created" in migration
    assert "ON evidence_tasks (priority DESC, created_at ASC)" in migration
    assert "status IN ('pending', 'materialization_deferred')" in migration
    assert "idx_evidence_tasks_materialization_priority_created" in migration
    assert "status IN ('pending', 'materialization_pending', 'materialization_deferred')" in migration
    assert "idx_evidence_tasks_active_source_status" in migration
    assert "ON evidence_tasks (source_id, status)" in migration
    assert "idx_evidence_tasks_active_event_type_status" in migration
    assert "ON evidence_tasks (event_type, status)" in migration
    assert "_materialization_backlog_depth" in worker
