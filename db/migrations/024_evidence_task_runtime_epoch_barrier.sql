-- Migration 024: promote evidence_tasks.runtime_epoch_id for runtime epoch barrier.
--
-- Additive only. Runtime apply/restart paths can now query and terminalize
-- non-terminal evidence tasks by first-class runtime epoch instead of
-- repeatedly re-parsing event payload JSON.

ALTER TABLE evidence_tasks
    ADD COLUMN IF NOT EXISTS runtime_epoch_id TEXT;

UPDATE evidence_tasks et
SET runtime_epoch_id = COALESCE(
        NULLIF(et.runtime_epoch_id, ''),
        NULLIF(e.payload->>'runtime_epoch_id', ''),
        NULLIF(e.payload->'media'->>'runtime_epoch_id', '')
    )
FROM events e
WHERE e.id = et.event_id
  AND COALESCE(NULLIF(et.runtime_epoch_id, ''), '') = '';

CREATE INDEX IF NOT EXISTS evidence_tasks_runtime_epoch_active_idx
    ON evidence_tasks(runtime_epoch_id, source_id, replay_shard_id, updated_at DESC)
    WHERE (
        materialization_status IN (
            'manifest_ready',
            'materialization_pending',
            'materializing'
        )
        OR status IN (
            'pending',
            'waiting_proof',
            'queued',
            'replay_job_created',
            'replaying',
            'materializing',
            'finalizing'
        )
        OR (
            materialization_status = 'materialization_deferred'
            AND COALESCE(materialization_defer_reason, '') = ''
        )
    );

COMMENT ON COLUMN evidence_tasks.runtime_epoch_id IS
    'First-class runtime epoch captured at task creation for restart/apply barrier checks and orphan detection.';
