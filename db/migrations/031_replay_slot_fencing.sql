-- Migration 031: fenced Replay admission ownership and durable create handoff.
--
-- Additive and upgrade-safe.  Legacy rows remain readable while Clip
-- Coordinator V2 uses owner + logical token + generation CAS for every
-- create-side transition.  The token identifies one logical Replay attempt
-- across clip-worker ownership transfer; generation fences a stale process.

ALTER TABLE evidence_tasks
    ADD COLUMN IF NOT EXISTS replay_slot_owner TEXT,
    ADD COLUMN IF NOT EXISTS replay_slot_token TEXT,
    ADD COLUMN IF NOT EXISTS replay_slot_generation BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS replay_create_state TEXT,
    ADD COLUMN IF NOT EXISTS replay_create_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS replay_create_committed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS replay_plan_hash TEXT,
    ADD COLUMN IF NOT EXISTS replay_request_id TEXT,
    ADD COLUMN IF NOT EXISTS replay_delivery_id TEXT;

CREATE INDEX IF NOT EXISTS evidence_tasks_replay_slot_fence_idx
    ON evidence_tasks(
        event_id,
        replay_slot_token,
        replay_slot_generation
    )
    WHERE replay_slot_status = 'active';

CREATE INDEX IF NOT EXISTS evidence_tasks_replay_create_recovery_idx
    ON evidence_tasks(
        replay_create_state,
        replay_slot_deadline_at,
        replay_slot_owner
    )
    WHERE replay_slot_status = 'active'
      AND replay_job_id IS NULL;

COMMENT ON COLUMN evidence_tasks.replay_slot_owner IS
    'Current clip-worker owner allowed to mutate the Replay create side.';

COMMENT ON COLUMN evidence_tasks.replay_slot_token IS
    'Stable logical Replay slot token propagated through Replay labels and media handoff.';

COMMENT ON COLUMN evidence_tasks.replay_slot_generation IS
    'Monotonic ownership generation used with owner and token to fence stale clip workers.';

COMMENT ON COLUMN evidence_tasks.replay_create_state IS
    'Replay create state: reserved, submitting, committed, retryable, uncertain, or aborted.';

COMMENT ON COLUMN evidence_tasks.replay_plan_hash IS
    'Canonical pure Replay plan hash bound before the external create call.';
