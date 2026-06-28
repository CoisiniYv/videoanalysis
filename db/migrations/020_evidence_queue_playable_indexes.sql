-- Migration 020: evidence-chain queue and playable-bundle indexes.
--
-- These indexes target downstream evidence-chain pressure after inference:
--   * recent playable evidence bundle listing ordered by event_created_at DESC;
--   * pending/deferred evidence task admission and worker queue inspection.
--   * active evidence admission counts by source and event_type.
--
-- Live-operation note:
--   CREATE INDEX CONCURRENTLY must not run inside a transaction block. Apply this
--   migration with psql/autocommit or another non-transactional migration path.
--   Do not apply it during an active pressure-test window.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_evidence_bundles_playable_recent
    ON evidence_bundles (event_created_at DESC)
    WHERE raw_clip_uri IS NOT NULL
      AND raw_clip_size_bytes > 0;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_evidence_tasks_pending_priority_created
    ON evidence_tasks (priority DESC, created_at ASC)
    WHERE status IN ('pending', 'materialization_deferred');

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_evidence_tasks_materialization_priority_created
    ON evidence_tasks (priority DESC, created_at ASC)
    WHERE status IN ('pending', 'materialization_pending', 'materialization_deferred');

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_evidence_tasks_active_source_status
    ON evidence_tasks (source_id, status)
    WHERE status IN (
        'pending',
        'materialization_pending',
        'waiting_proof',
        'queued',
        'replay_job_created',
        'replaying',
        'materializing',
        'finalizing',
        'materialization_deferred'
    );

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_evidence_tasks_active_event_type_status
    ON evidence_tasks (event_type, status)
    WHERE status IN (
        'pending',
        'materialization_pending',
        'waiting_proof',
        'queued',
        'replay_job_created',
        'replaying',
        'materializing',
        'finalizing',
        'materialization_deferred'
    );

COMMENT ON INDEX idx_evidence_bundles_playable_recent IS
    'Supports recent playable evidence bundle lists without scanning evidence_bundles.';

COMMENT ON INDEX idx_evidence_tasks_pending_priority_created IS
    'Supports evidence task pending/deferred priority queue inspection under pressure.';

COMMENT ON INDEX idx_evidence_tasks_materialization_priority_created IS
    'Supports evidence task materialization priority queue inspection under pressure.';

COMMENT ON INDEX idx_evidence_tasks_active_source_status IS
    'Supports event-worker evidence admission counts by source under pressure.';

COMMENT ON INDEX idx_evidence_tasks_active_event_type_status IS
    'Supports event-worker evidence admission counts by event type under pressure.';
