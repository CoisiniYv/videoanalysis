-- Migration 026: match rolling-cache claim ordering for ready-at tasks.
--
-- Migration 025 added the ready-at column and a lookup index. The media-worker
-- claim query orders by priority first, then ready_at, then created_at; keep a
-- matching partial index so a larger evidence_tasks table can claim ready work
-- without sorting the active task subset on every poll.

CREATE INDEX IF NOT EXISTS evidence_tasks_ready_claim_order_idx
    ON evidence_tasks(priority DESC, materialization_ready_at, created_at)
    WHERE materialization_status IN (
        'manifest_ready',
        'materialization_pending',
        'pending'
    );
