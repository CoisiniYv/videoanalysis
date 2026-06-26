-- Migration 015: manifest-first evidence materialization state.
--
-- Additive only. Existing evidence task rows remain valid, while new workers can
-- record explicit media materialization state, deadlines, shard routing,
-- quota/degrade decisions, and cleanup audit metadata.

ALTER TABLE evidence_tasks
    ADD COLUMN IF NOT EXISTS materialization_status TEXT NOT NULL DEFAULT 'manifest_ready',
    ADD COLUMN IF NOT EXISTS materialization_policy TEXT NOT NULL DEFAULT 'priority',
    ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS replay_shard_id TEXT,
    ADD COLUMN IF NOT EXISTS replay_api_url TEXT,
    ADD COLUMN IF NOT EXISTS replay_job_sink_url TEXT,
    ADD COLUMN IF NOT EXISTS replay_source_id TEXT,
    ADD COLUMN IF NOT EXISTS replay_window JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS sink_output_path TEXT,
    ADD COLUMN IF NOT EXISTS replay_deadline_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS annotation_deadline_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS materialization_deadline_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS materialization_attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS materialization_defer_reason TEXT,
    ADD COLUMN IF NOT EXISTS materialization_failure_reason TEXT,
    ADD COLUMN IF NOT EXISTS materialization_expired_reason TEXT,
    ADD COLUMN IF NOT EXISTS materialization_audit JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS cleanup_audit JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS quota_decision JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS degrade_decision JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS last_materialization_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS evidence_tasks_materialization_status_idx
    ON evidence_tasks(materialization_status);

CREATE INDEX IF NOT EXISTS evidence_tasks_materialization_deadline_idx
    ON evidence_tasks(materialization_deadline_at)
    WHERE materialization_status IN (
        'manifest_ready',
        'materialization_pending',
        'materialization_deferred'
    );

CREATE INDEX IF NOT EXISTS evidence_tasks_materialization_shard_source_idx
    ON evidence_tasks(replay_shard_id, replay_source_id, materialization_status);

UPDATE evidence_tasks
SET materialization_status = CASE
        WHEN status IN (
            'ready',
            'generated',
            'generated_unverified',
            'generated_annotation_failed',
            'materialized'
        )
            THEN 'materialized'
        WHEN status IN (
            'failed',
            'duration_guard_failed',
            'generated_corrupt',
            'materialization_failed'
        )
            THEN 'materialization_failed'
        WHEN status IN (
            'materialization_expired',
            'media_expired'
        )
            THEN 'materialization_expired'
        WHEN status IN (
            'pending',
            'claimed',
            'processing',
            'queued',
            'waiting_proof',
            'replaying',
            'finalizing',
            'materialization_pending'
        )
            THEN 'materialization_pending'
        WHEN status IN (
            'materializing'
        )
            THEN 'materializing'
        WHEN status IN (
            'materialization_deferred'
        )
            THEN status
        ELSE materialization_status
    END,
    last_materialization_at = CASE
        WHEN status IN (
            'ready',
            'generated',
            'generated_unverified',
            'generated_annotation_failed',
            'materialized'
        )
            THEN COALESCE(last_materialization_at, updated_at)
        ELSE last_materialization_at
    END
WHERE materialization_status = 'manifest_ready'
  AND status IS NOT NULL
  AND status <> 'manifest_ready';

COMMENT ON COLUMN evidence_tasks.materialization_deadline_at IS
    'Earliest deadline from Replay video TTL and frame-annotation TTL for deferred media materialization.';
