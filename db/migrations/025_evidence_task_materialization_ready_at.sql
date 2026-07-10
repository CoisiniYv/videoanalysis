-- Migration 025: evidence materialization ready-at scheduling.
--
-- Additive only. The ready-at timestamp separates unavoidable media wait time
-- (post window plus rolling segment close/grace) from actual materialization
-- worker time, so workers only claim tasks that can do real work.

ALTER TABLE evidence_tasks
    ADD COLUMN IF NOT EXISTS materialization_ready_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS evidence_tasks_materialization_ready_idx
    ON evidence_tasks(materialization_ready_at, priority DESC, created_at)
    WHERE materialization_status IN (
        'manifest_ready',
        'materialization_pending',
        'pending'
    );

COMMENT ON COLUMN evidence_tasks.materialization_ready_at IS
    'Earliest time media materialization may claim the task; excludes natural post-window and segment-close wait from worker occupancy.';
