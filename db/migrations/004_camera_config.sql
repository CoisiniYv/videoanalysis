-- Migration 004: Camera / Zone / Rule configuration MVP
--
-- The 001 placeholders for cameras / camera_zones / camera_rules used
-- UUID PKs and a "polygon"/"name"/"stream_url" shape that never got
-- wired anywhere (events.camera_id is plain TEXT, never an FK into
-- cameras). This migration drops the dead placeholder tables and recreates
-- them per the active camera configuration schema.
--
-- This migration does NOT touch events, audit_logs, persons, or any
-- media/face schema.

-- Drop dependent → parent order. IF EXISTS so the migration is safe
-- to re-run even if 001 has not been applied on a fresh DB.
DROP TABLE IF EXISTS camera_rules;
DROP TABLE IF EXISTS camera_zones;
DROP TABLE IF EXISTS cameras;

CREATE TABLE cameras (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    rtsp_url    TEXT NOT NULL,
    site_id     TEXT,
    location    TEXT,
    gpu_id      INTEGER NOT NULL DEFAULT 0,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cameras_source_id_unique UNIQUE (source_id)
);

CREATE INDEX IF NOT EXISTS cameras_enabled_idx ON cameras(enabled);

CREATE TABLE camera_zones (
    id          BIGSERIAL PRIMARY KEY,
    camera_id   TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    zone_name   TEXT NOT NULL,
    zone_type   TEXT NOT NULL,
    points      JSONB NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT camera_zones_camera_zone_unique UNIQUE (camera_id, zone_name)
);

CREATE INDEX IF NOT EXISTS camera_zones_camera_idx ON camera_zones(camera_id);

CREATE TABLE camera_rules (
    id          BIGSERIAL PRIMARY KEY,
    camera_id   TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    rule_type   TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    config      JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Midterm default: one rule per (camera, rule_type). Future extensions can
    -- relax this to (camera, rule_type, rule_name) when multiple
    -- instances are needed.
    CONSTRAINT camera_rules_camera_type_unique UNIQUE (camera_id, rule_type)
);

CREATE INDEX IF NOT EXISTS camera_rules_camera_type_idx ON camera_rules(camera_id, rule_type);
