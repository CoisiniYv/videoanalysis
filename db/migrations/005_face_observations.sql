-- Migration 005: Face observations table
-- Stores face observations with pgvector embeddings from the
-- security.face_observations Redis stream.
-- Safe to run repeatedly (uses IF NOT EXISTS).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS face_observations (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_observation_id   TEXT NOT NULL UNIQUE,
    camera_id               TEXT NOT NULL,
    source_id               TEXT NOT NULL,
    track_id                TEXT NOT NULL,
    timestamp_ms            BIGINT NOT NULL,
    captured_at             TIMESTAMPTZ,
    frame_num               INTEGER,
    person_bbox             JSONB,
    face_bbox               JSONB NOT NULL,
    landmarks               JSONB NOT NULL,
    face_confidence         DOUBLE PRECISION NOT NULL,
    quality                 DOUBLE PRECISION NOT NULL,
    detector_model          TEXT NOT NULL DEFAULT 'yolov8_face',
    embedding_model         TEXT NOT NULL DEFAULT 'adaface',
    model_version           TEXT,
    embedding_dim           INTEGER NOT NULL DEFAULT 512 CHECK (embedding_dim = 512),
    embedding               vector(512) NOT NULL,
    embedding_norm          DOUBLE PRECISION NOT NULL,
    reid_throttle_key       TEXT NOT NULL DEFAULT '',
    association_score       DOUBLE PRECISION,
    association_method      TEXT,
    camera_config_resolved  BOOLEAN NOT NULL DEFAULT false,
    snapshot_path           TEXT,
    crop_path               TEXT,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- btree indexes only (approximate vector index deferred)
CREATE INDEX IF NOT EXISTS idx_face_obs_source_observation_id ON face_observations(source_observation_id);
CREATE INDEX IF NOT EXISTS idx_face_obs_camera_id ON face_observations(camera_id);
CREATE INDEX IF NOT EXISTS idx_face_obs_source_id ON face_observations(source_id);
CREATE INDEX IF NOT EXISTS idx_face_obs_track_id ON face_observations(track_id);
CREATE INDEX IF NOT EXISTS idx_face_obs_timestamp_ms ON face_observations(timestamp_ms);
