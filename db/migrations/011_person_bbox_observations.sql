-- Migration 011: accepted person bbox observations.
-- Lightweight per-frame/person bbox stream for evidence context overlays.
-- No keypoints, embeddings, image bytes, identity, or trajectory data.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS person_bbox_observations (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_observation_id   TEXT NOT NULL UNIQUE,
    source_id               TEXT NOT NULL,
    camera_id               TEXT NOT NULL,
    track_id                TEXT,
    timestamp_ms            BIGINT NOT NULL,
    frame_pts               BIGINT,
    frame_num               INTEGER,
    person_bbox             JSONB NOT NULL,
    person_confidence       DOUBLE PRECISION,
    gate_status             TEXT NOT NULL DEFAULT 'accepted',
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_person_bbox_obs_source_ts
    ON person_bbox_observations(source_id, timestamp_ms);

CREATE INDEX IF NOT EXISTS idx_person_bbox_obs_camera_ts
    ON person_bbox_observations(camera_id, timestamp_ms);

CREATE INDEX IF NOT EXISTS idx_person_bbox_obs_track_ts
    ON person_bbox_observations(track_id, timestamp_ms);

CREATE INDEX IF NOT EXISTS idx_person_bbox_obs_created_at
    ON person_bbox_observations(created_at);
