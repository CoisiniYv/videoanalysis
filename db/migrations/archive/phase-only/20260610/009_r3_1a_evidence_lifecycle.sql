-- Migration 009: R3.1A behavior evidence task lifecycle fields.
--
-- This migration is additive. It does not change the Savant pipeline, Redis
-- producers, or event/observation semantics.

ALTER TABLE evidence_tasks
    ADD COLUMN IF NOT EXISTS metadata_path TEXT,
    ADD COLUMN IF NOT EXISTS output_root TEXT,
    ADD COLUMN IF NOT EXISTS storage_fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS storage_fallback_reason TEXT,
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_retries INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS claimed_by TEXT,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS evidence_tasks_claimed_at_idx
    ON evidence_tasks(claimed_at);

CREATE INDEX IF NOT EXISTS evidence_tasks_event_status_idx
    ON evidence_tasks(event_id, status);
