-- Migration 027: keep terminal deferred tasks out of ready-at claim indexes.
--
-- materialization_deferred with a reason is terminal for rolling-cache evidence.
-- It must not stay in active claim/drain predicates, otherwise a covered-but-not
-- playable row can be reclaimed forever and block pressure-run drain.

DROP INDEX IF EXISTS evidence_tasks_materialization_ready_idx;
DROP INDEX IF EXISTS evidence_tasks_ready_claim_order_idx;

CREATE INDEX IF NOT EXISTS evidence_tasks_materialization_ready_idx
    ON evidence_tasks(materialization_ready_at, priority DESC, created_at)
    WHERE materialization_status IN (
        'manifest_ready',
        'materialization_pending',
        'pending'
    );

CREATE INDEX IF NOT EXISTS evidence_tasks_ready_claim_order_idx
    ON evidence_tasks(priority DESC, materialization_ready_at, created_at)
    WHERE materialization_status IN (
        'manifest_ready',
        'materialization_pending',
        'pending'
    );
