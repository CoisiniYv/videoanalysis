-- Migration 002: SecurityEvent-aligned events table
-- Replaces the placeholder events table with the canonical SecurityEvent schema.
-- Safe to run repeatedly (uses IF EXISTS / IF NOT EXISTS).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Drop dependent tables first
DROP TABLE IF EXISTS tracks;

-- Recreate events with SecurityEvent v1.0 schema
DROP TABLE IF EXISTS events;
CREATE TABLE events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_event_id TEXT NOT NULL UNIQUE,
    event_type      TEXT NOT NULL,
    camera_id       TEXT NOT NULL DEFAULT '',
    source_id       TEXT NOT NULL DEFAULT '',
    track_id        TEXT NOT NULL DEFAULT '',
    person_id       INTEGER,
    severity        TEXT NOT NULL DEFAULT 'medium',
    confidence      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    start_ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    end_ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_ts_ms     BIGINT NOT NULL DEFAULT 0,
    frame_uuid      TEXT,
    keyframe_uuid   TEXT,
    snapshot_path   TEXT,
    clip_path       TEXT,
    recording_strategy  TEXT NOT NULL DEFAULT 'reserved',
    media_status    TEXT NOT NULL DEFAULT 'not_implemented',
    status          TEXT NOT NULL DEFAULT 'new',
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_events_source_event_id ON events(source_event_id);
CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_camera_id ON events(camera_id);
CREATE INDEX IF NOT EXISTS idx_events_start_ts ON events(start_ts);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
