-- Migration 028: image-only face evidence lookup helpers.
--
-- Face watchlist/search evidence is stored as image artifacts plus a searchable
-- evidence_bundles row. Video raw_clip remains filesystem-only for behavior
-- evidence.

CREATE INDEX IF NOT EXISTS evidence_bundles_playback_kind_idx
    ON evidence_bundles ((summary->>'playback_kind'), event_created_at DESC);

CREATE INDEX IF NOT EXISTS evidence_artifacts_face_image_idx
    ON evidence_artifacts(event_id, artifact_type)
    WHERE artifact_type IN ('face_crop', 'full_frame', 'annotated_frame');
