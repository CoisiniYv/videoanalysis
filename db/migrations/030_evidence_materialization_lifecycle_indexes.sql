-- Migration 030: rebuild materialization hot-path indexes for lifecycle v2.
--
-- This file is deliberately non-transactional because PostgreSQL does not
-- allow CREATE/DROP INDEX CONCURRENTLY inside a transaction block.

DROP INDEX CONCURRENTLY IF EXISTS evidence_tasks_materialization_ready_idx;
CREATE INDEX CONCURRENTLY evidence_tasks_materialization_ready_idx
    ON evidence_tasks(
        COALESCE(materialization_next_attempt_at, materialization_ready_at),
        priority DESC,
        created_at
    )
    WHERE materialization_status IN (
        'manifest_ready', 'materialization_pending'
    )
      AND materialization_ready_at IS NOT NULL;

DROP INDEX CONCURRENTLY IF EXISTS evidence_tasks_ready_claim_order_idx;
CREATE INDEX CONCURRENTLY evidence_tasks_ready_claim_order_idx
    ON evidence_tasks(
        priority DESC,
        COALESCE(materialization_next_attempt_at, materialization_ready_at),
        materialization_ready_at,
        created_at
    )
    WHERE materialization_status IN (
        'manifest_ready', 'materialization_pending'
    )
      AND materialization_ready_at IS NOT NULL;

DROP INDEX CONCURRENTLY IF EXISTS evidence_tasks_materialization_deadline_idx;
CREATE INDEX CONCURRENTLY evidence_tasks_materialization_deadline_idx
    ON evidence_tasks(materialization_deadline_at, materialization_status)
    WHERE materialization_status IN (
        'manifest_ready', 'materialization_pending', 'materializing'
    )
      AND materialization_deadline_at IS NOT NULL;

DROP INDEX CONCURRENTLY IF EXISTS evidence_tasks_runtime_epoch_active_idx;
CREATE INDEX CONCURRENTLY evidence_tasks_runtime_epoch_active_idx
    ON evidence_tasks(
        runtime_epoch_id,
        source_id,
        materialization_status,
        updated_at DESC
    )
    WHERE materialization_status IN (
        'manifest_ready', 'materialization_pending', 'materializing'
    )
      AND materialization_ready_at IS NOT NULL
      AND COALESCE(source_id, '') <> ''
      AND COALESCE(runtime_epoch_id, '') <> '';

DROP INDEX CONCURRENTLY IF EXISTS evidence_tasks_materialization_lease_expiry_idx;
CREATE INDEX CONCURRENTLY evidence_tasks_materialization_lease_expiry_idx
    ON evidence_tasks(materialization_lease_expires_at, materialization_lease_generation)
    WHERE materialization_status = 'materializing'
      AND materialization_lease_token IS NOT NULL;

DROP INDEX CONCURRENTLY IF EXISTS evidence_tasks_finalizer_pending_idx;
CREATE INDEX CONCURRENTLY evidence_tasks_finalizer_pending_idx
    ON evidence_tasks(priority DESC, materialization_phase_updated_at, created_at)
    WHERE materialization_status = 'materializing'
      AND materialization_phase = 'finalizer_pending'
      AND materialization_handoff <> '{}'::jsonb;

DROP INDEX CONCURRENTLY IF EXISTS idx_evidence_tasks_pending_priority_created;
CREATE INDEX CONCURRENTLY idx_evidence_tasks_pending_priority_created
    ON evidence_tasks(priority DESC, created_at ASC)
    WHERE status IN ('pending', 'materialization_pending');

DROP INDEX CONCURRENTLY IF EXISTS idx_evidence_tasks_materialization_priority_created;
CREATE INDEX CONCURRENTLY idx_evidence_tasks_materialization_priority_created
    ON evidence_tasks(
        priority DESC,
        COALESCE(materialization_next_attempt_at, materialization_ready_at),
        created_at ASC
    )
    WHERE materialization_status IN (
        'manifest_ready', 'materialization_pending'
    )
      AND materialization_ready_at IS NOT NULL;

DROP INDEX CONCURRENTLY IF EXISTS idx_evidence_tasks_active_source_status;
CREATE INDEX CONCURRENTLY idx_evidence_tasks_active_source_status
    ON evidence_tasks(source_id, status)
    WHERE materialization_status IN (
        'manifest_ready', 'materialization_pending', 'materializing'
    )
       OR status IN (
           'pending', 'waiting_proof', 'queued', 'replay_job_created',
           'replaying', 'materializing', 'finalizing'
       );

DROP INDEX CONCURRENTLY IF EXISTS idx_evidence_tasks_active_event_type_status;
CREATE INDEX CONCURRENTLY idx_evidence_tasks_active_event_type_status
    ON evidence_tasks(event_type, status)
    WHERE materialization_status IN (
        'manifest_ready', 'materialization_pending', 'materializing'
    )
       OR status IN (
           'pending', 'waiting_proof', 'queued', 'replay_job_created',
           'replaying', 'materializing', 'finalizing'
       );

COMMENT ON INDEX evidence_tasks_materialization_ready_idx IS
    'Lifecycle v2 runnable ordering; deferred is terminal and retry uses next_attempt_at.';
COMMENT ON INDEX evidence_tasks_materialization_lease_expiry_idx IS
    'Lifecycle v2 fenced rolling lease recovery ordering.';
COMMENT ON INDEX evidence_tasks_finalizer_pending_idx IS
    'Lifecycle v2 durable remux-to-finalizer recovery queue.';
