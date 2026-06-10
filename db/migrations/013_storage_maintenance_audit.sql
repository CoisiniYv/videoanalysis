-- Migration 013: storage maintenance preview, delete audit, and job items.
--
-- This migration is additive. It keeps business rows and evidence task status
-- intact while storage maintenance records destructive-operation previews and
-- per-item execution state.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS maintenance_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT,
    reason TEXT,
    delete_mode TEXT,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    preview_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    candidate_hash TEXT,
    preview_expires_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS maintenance_jobs_type_created_idx
    ON maintenance_jobs(job_type, created_at DESC);

CREATE INDEX IF NOT EXISTS maintenance_jobs_status_idx
    ON maintenance_jobs(status);

CREATE INDEX IF NOT EXISTS maintenance_jobs_preview_expiry_idx
    ON maintenance_jobs(preview_expires_at);

CREATE TABLE IF NOT EXISTS maintenance_job_items (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES maintenance_jobs(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    status TEXT NOT NULL,
    absolute_path TEXT,
    relative_path TEXT,
    resolved_path TEXT,
    size_bytes BIGINT,
    mtime_ns BIGINT,
    content_fingerprint TEXT,
    db_event_id UUID,
    db_task_id TEXT,
    media_status_at_preview TEXT,
    eligibility_status TEXT NOT NULL,
    skip_reason TEXT,
    trash_path TEXT,
    error_message TEXT,
    item_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (job_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS maintenance_job_items_job_status_idx
    ON maintenance_job_items(job_id, status);

CREATE INDEX IF NOT EXISTS maintenance_job_items_target_idx
    ON maintenance_job_items(target_type, target_id);

ALTER TABLE persons
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT,
    ADD COLUMN IF NOT EXISTS delete_reason TEXT;

ALTER TABLE person_gallery_embeddings
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT,
    ADD COLUMN IF NOT EXISTS delete_reason TEXT;
