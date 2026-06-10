-- Migration 007: Phase F3.5 — match_results gallery_match semantics
--
-- Fixes the semantic mismatch where gallery_match rows incorrectly used
-- matched_observation_id (designed for observation-vs-observation search)
-- to store the query observation id.
--
-- Changes:
--   1. Add query_observation_id UUID FK -> face_observations(id)
--   2. Add query_source_observation_id TEXT FK -> face_observations(source_observation_id)
--   3. Add UNIQUE(search_request_id, query_gallery_embedding_id) for gallery_match idempotency
--   4. Add index on query_observation_id
--   5. Relax matched_observation_id to NULLABLE (gallery_match has no matched observation)
--   6. Relax matched_camera_id / matched_source_id / matched_track_id to NULLABLE
--      (gallery entries have no camera/track context)
--
-- Idempotency:
--   Safe to rerun. All ALTERs use IF NOT EXISTS or are idempotent.
--   No data is deleted. Existing rows are not modified.

-- 1. Add query observation columns
ALTER TABLE match_results
    ADD COLUMN IF NOT EXISTS query_observation_id UUID
    REFERENCES face_observations(id) ON DELETE SET NULL;

ALTER TABLE match_results
    ADD COLUMN IF NOT EXISTS query_source_observation_id TEXT
    REFERENCES face_observations(source_observation_id) ON DELETE SET NULL;

-- 2. Add gallery_match idempotency constraint
-- This allows multiple rows per search_request_id (one per gallery embedding)
-- while preventing duplicate writes of the same gallery target.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'match_results_search_request_gallery_embedding_key'
    ) THEN
        ALTER TABLE match_results
            ADD CONSTRAINT match_results_search_request_gallery_embedding_key
            UNIQUE (search_request_id, query_gallery_embedding_id);
    END IF;
END $$;

-- 3. Add index on query_observation_id
CREATE INDEX IF NOT EXISTS match_results_query_observation_idx
    ON match_results(query_observation_id);

-- 4. Relax matched_observation_id to NULLABLE
-- For gallery_match, there is no matched historical observation.
-- For registered_person_history / temporary_face_history, it remains NOT NULL.
ALTER TABLE match_results
    ALTER COLUMN matched_observation_id DROP NOT NULL;

-- 5. Relax matched_* context columns to NULLABLE
-- Gallery embeddings have no camera/track context.
-- For observation-vs-observation search, these remain populated.
ALTER TABLE match_results
    ALTER COLUMN matched_camera_id DROP NOT NULL;

ALTER TABLE match_results
    ALTER COLUMN matched_source_id DROP NOT NULL;

ALTER TABLE match_results
    ALTER COLUMN matched_track_id DROP NOT NULL;

-- 6. Add comment documenting the two search modes
COMMENT ON COLUMN match_results.matched_observation_id IS
    'FK to face_observations. NULL for gallery_match (target is a gallery embedding, '
    'not a historical observation). Populated for registered_person_history / '
    'temporary_face_history (observation-vs-observation search).';

COMMENT ON COLUMN match_results.query_observation_id IS
    'FK to face_observations(id). The input observation for gallery_match. '
    'NULL for modes where the query is a person or temporary upload.';

COMMENT ON COLUMN match_results.query_gallery_embedding_id IS
    'FK to person_gallery_embeddings(id). The gallery target for gallery_match. '
    'NULL for observation-vs-observation search modes.';
