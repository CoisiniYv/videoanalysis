from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migrations" / "014_worker_media_indexes.sql"
QUEUE_MIGRATION = ROOT / "db" / "migrations" / "019_media_worker_events_queue_indexes.sql"
EVIDENCE_QUEUE_MIGRATION = ROOT / "db" / "migrations" / "020_evidence_queue_playable_indexes.sql"
EPOCH_BARRIER_MIGRATION = ROOT / "db" / "migrations" / "024_evidence_task_runtime_epoch_barrier.sql"
READY_CLAIM_MIGRATION = ROOT / "db" / "migrations" / "026_evidence_task_ready_claim_order_idx.sql"
READY_DEFERRED_MIGRATION = (
    ROOT / "db" / "migrations" / "027_evidence_task_ready_indexes_terminal_deferred.sql"
)
LIFECYCLE_INDEX_MIGRATION = (
    ROOT / "db" / "migrations" / "030_evidence_materialization_lifecycle_indexes.sql"
)
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


def test_epoch_barrier_migration_promotes_runtime_epoch_and_active_index() -> None:
    migration = _text(EPOCH_BARRIER_MIGRATION)

    assert "ADD COLUMN IF NOT EXISTS runtime_epoch_id TEXT" in migration
    assert "UPDATE evidence_tasks et" in migration
    assert "payload->>'runtime_epoch_id'" in migration
    assert "payload->'media'->>'runtime_epoch_id'" in migration
    assert "evidence_tasks_runtime_epoch_active_idx" in migration
    assert "ON evidence_tasks(runtime_epoch_id, source_id, replay_shard_id, updated_at DESC)" in migration
    assert "materialization_status IN (" in migration
    assert "status IN (" in migration
    assert "materialization_status = 'materialization_deferred'" in migration


def test_ready_claim_index_matches_rolling_cache_claim_order() -> None:
    migration = _text(READY_CLAIM_MIGRATION)
    deferred_fix_migration = _text(READY_DEFERRED_MIGRATION)
    lifecycle_index_migration = _text(LIFECYCLE_INDEX_MIGRATION)
    worker = _text(MEDIA_WORKER)

    assert "CREATE INDEX IF NOT EXISTS evidence_tasks_ready_claim_order_idx" in migration
    assert "ON evidence_tasks(priority DESC, materialization_ready_at, created_at)" in migration
    assert "materialization_status IN (" in migration
    assert "'materialization_deferred'" not in migration
    assert "DROP INDEX IF EXISTS evidence_tasks_ready_claim_order_idx" in deferred_fix_migration
    assert "'materialization_deferred'" not in deferred_fix_migration
    assert "DROP INDEX CONCURRENTLY IF EXISTS evidence_tasks_ready_claim_order_idx" in lifecycle_index_migration
    assert "COALESCE(materialization_next_attempt_at, materialization_ready_at)" in lifecycle_index_migration
    assert "materialization_ready_at <= now()" in worker
    assert "materialization_due_at ASC" in worker
    assert "rolling_cache_ready_at ASC" in worker
    assert "task_created_at ASC" in worker
