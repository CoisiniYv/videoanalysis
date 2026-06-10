-- Migration 008: R3 unified event, evidence, and algorithm-rule contracts.
--
-- This migration extends the existing Phase 2E/C1 tables. It does not replace
-- the Savant pipeline, Redis producer, face_observations, or media workers.

ALTER TABLE events
    ADD COLUMN IF NOT EXISTS algorithm_type TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS algorithm_version TEXT,
    ADD COLUMN IF NOT EXISTS start_ts_ms BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS end_ts_ms BIGINT,
    ADD COLUMN IF NOT EXISTS snapshot_required BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS clip_required BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS evidence_policy JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_events_algorithm_type ON events(algorithm_type);
CREATE INDEX IF NOT EXISTS idx_events_start_ts_ms ON events(start_ts_ms);

ALTER TABLE camera_rules
    DROP CONSTRAINT IF EXISTS camera_rules_camera_type_unique;

ALTER TABLE camera_rules
    ADD COLUMN IF NOT EXISTS zone_id TEXT,
    ADD COLUMN IF NOT EXISTS line_id TEXT,
    ADD COLUMN IF NOT EXISTS evidence_policy JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS camera_rules_camera_rule_idx ON camera_rules(camera_id, id);
CREATE INDEX IF NOT EXISTS camera_rules_camera_algorithm_idx ON camera_rules(camera_id, rule_type);

CREATE TABLE IF NOT EXISTS evidence_tasks (
    task_id           TEXT PRIMARY KEY,
    event_id          UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    source_event_id   TEXT NOT NULL,
    camera_id         TEXT NOT NULL DEFAULT '',
    source_id         TEXT NOT NULL DEFAULT '',
    event_type        TEXT NOT NULL DEFAULT '',
    event_ts_ms       BIGINT NOT NULL DEFAULT 0,
    task_type         TEXT NOT NULL DEFAULT 'snapshot_clip',
    snapshot_required BOOLEAN NOT NULL DEFAULT FALSE,
    clip_required     BOOLEAN NOT NULL DEFAULT FALSE,
    pre_seconds       INTEGER NOT NULL DEFAULT 5,
    post_seconds      INTEGER NOT NULL DEFAULT 10,
    status            TEXT NOT NULL DEFAULT 'pending',
    snapshot_path     TEXT,
    clip_path         TEXT,
    error_message     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT evidence_tasks_source_event_type_unique
        UNIQUE (source_event_id, task_type)
);

CREATE INDEX IF NOT EXISTS evidence_tasks_event_id_idx ON evidence_tasks(event_id);
CREATE INDEX IF NOT EXISTS evidence_tasks_source_event_id_idx ON evidence_tasks(source_event_id);
CREATE INDEX IF NOT EXISTS evidence_tasks_status_idx ON evidence_tasks(status);
