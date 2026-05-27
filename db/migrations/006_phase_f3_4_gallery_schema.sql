-- Migration 006: Phase F3.4 — Gallery schema
-- Creates persons, person_gallery_embeddings, match_results.
--
-- Idempotency:
--   Safe to rerun IF the old placeholder persons table from migration 001
--   has already been replaced by this migration's CREATE TABLE. All CREATE
--   statements use IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
--
--   NOT safe to rerun if the old migration-001 persons table (UUID PK,
--   external_id TEXT UNIQUE, display_name TEXT) still exists. In that case
--   the DO block below will raise an EXCEPTION with manual cutover
--   instructions. This is intentional: the old table has a different PK
--   type (UUID vs BIGSERIAL) and cannot be ALTERed safely.
--
--   If you are on a fresh DB (no migration 001 persons), this migration
--   runs cleanly with no manual steps.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Guard: detect old migration-001 persons table (UUID PK).
-- If present and empty, drop it automatically. If it has data, fail loudly.
DO $$
DECLARE
    old_pk_type TEXT;
    old_rowcount BIGINT;
BEGIN
    -- Check if persons exists at all
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'persons'
    ) THEN
        RETURN; -- No persons table yet, proceed with CREATE
    END IF;

    -- Check if it already has our target schema (BIGSERIAL PK)
    SELECT data_type INTO old_pk_type
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'persons'
      AND column_name = 'id';

    IF old_pk_type = 'bigint' THEN
        RETURN; -- Already migrated, CREATE IF NOT EXISTS will be a no-op
    END IF;

    -- Old table exists with non-BIGINT PK (likely UUID from migration 001).
    -- Check if it has data.
    EXECUTE 'SELECT count(*) FROM persons' INTO old_rowcount;

    IF old_rowcount = 0 THEN
        -- Empty old table: safe to drop and recreate
        RAISE NOTICE 'Dropping empty old persons table (PK type: %)', old_pk_type;
        DROP TABLE IF EXISTS person_gallery_embeddings CASCADE;
        DROP TABLE IF EXISTS match_results CASCADE;
        DROP TABLE IF EXISTS persons CASCADE;
    ELSE
        -- Has data: fail with clear instructions
        RAISE EXCEPTION
            'persons table has % rows with PK type %. '
            'Manual cutover required: '
            '1) BACKUP persons data; '
            '2) DROP TABLE persons CASCADE; '
            '3) Re-run migration 006.',
            old_rowcount, old_pk_type;
    END IF;
END $$;

-- 1. persons — registered person master data
CREATE TABLE IF NOT EXISTS persons (
    id                  BIGSERIAL PRIMARY KEY,
    name                TEXT NOT NULL,
    external_person_id  TEXT,
    description         TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_by          TEXT,
    updated_by          TEXT,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS persons_external_person_id_idx
    ON persons(external_person_id) WHERE external_person_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS persons_name_trgm_idx
    ON persons USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS persons_active_idx
    ON persons(is_active);

-- 2. person_gallery_embeddings — long-term registered gallery vectors
CREATE TABLE IF NOT EXISTS person_gallery_embeddings (
    id                      BIGSERIAL PRIMARY KEY,
    person_id               BIGINT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    source_type             TEXT NOT NULL DEFAULT 'manual_upload',
    source_image_path       TEXT,
    source_observation_id   TEXT REFERENCES face_observations(source_observation_id) ON DELETE SET NULL,
    embedding_model         TEXT NOT NULL DEFAULT 'adaface',
    model_version           TEXT,
    embedding_dim           INTEGER NOT NULL DEFAULT 512 CHECK (embedding_dim = 512),
    embedding               vector(512) NOT NULL,
    embedding_norm          DOUBLE PRECISION NOT NULL,
    quality                 DOUBLE PRECISION,
    face_bbox               JSONB,
    landmarks               JSONB,
    is_primary              BOOLEAN NOT NULL DEFAULT false,
    is_active               BOOLEAN NOT NULL DEFAULT true,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS person_gallery_person_active_idx
    ON person_gallery_embeddings(person_id, is_active);

CREATE UNIQUE INDEX IF NOT EXISTS person_gallery_one_primary_idx
    ON person_gallery_embeddings(person_id)
    WHERE is_primary = true AND is_active = true;

-- 3. match_results — short-lived derived search results
CREATE TABLE IF NOT EXISTS match_results (
    id                          BIGSERIAL PRIMARY KEY,
    search_request_id           UUID NOT NULL,
    search_mode                 TEXT NOT NULL,
    query_person_id             BIGINT REFERENCES persons(id) ON DELETE SET NULL,
    query_gallery_embedding_id  BIGINT REFERENCES person_gallery_embeddings(id) ON DELETE SET NULL,
    query_embedding_model       TEXT NOT NULL DEFAULT 'adaface',
    similarity_threshold        DOUBLE PRECISION,
    time_from                   TIMESTAMPTZ,
    time_to                     TIMESTAMPTZ,
    camera_scope                JSONB,
    matched_observation_id      UUID NOT NULL REFERENCES face_observations(id) ON DELETE CASCADE,
    matched_source_observation_id TEXT REFERENCES face_observations(source_observation_id) ON DELETE SET NULL,
    matched_camera_id           TEXT NOT NULL,
    matched_source_id           TEXT NOT NULL,
    matched_track_id            TEXT NOT NULL,
    matched_captured_at         TIMESTAMPTZ,
    matched_timestamp_ms        BIGINT,
    rank                        INTEGER NOT NULL,
    similarity                  DOUBLE PRECISION NOT NULL,
    face_confidence             DOUBLE PRECISION,
    quality                     DOUBLE PRECISION,
    snapshot_path               TEXT,
    crop_path                   TEXT,
    nvr_reference               JSONB,
    expires_at                  TIMESTAMPTZ NOT NULL,
    payload                     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(search_request_id, matched_observation_id)
);

CREATE INDEX IF NOT EXISTS match_results_request_idx
    ON match_results(search_request_id, rank);
CREATE INDEX IF NOT EXISTS match_results_query_person_idx
    ON match_results(query_person_id, created_at DESC);
CREATE INDEX IF NOT EXISTS match_results_observation_idx
    ON match_results(matched_observation_id);
CREATE INDEX IF NOT EXISTS match_results_expires_idx
    ON match_results(expires_at);
