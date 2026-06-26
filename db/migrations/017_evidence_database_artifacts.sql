-- Migration 017: database-backed evidence bundle/artifact index.
--
-- Additive only. raw_clip media remains on filesystem/object storage; these
-- tables store searchable summaries, artifact URIs, and optional overlay/timeline
-- rows for later 8090 detail-path migration.

CREATE TABLE IF NOT EXISTS evidence_bundles (
    event_id UUID PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    source_event_id TEXT,
    camera_id TEXT,
    source_id TEXT,
    camera_name TEXT,
    event_type TEXT,
    event_created_at TIMESTAMPTZ,
    alarm_machine_time TIMESTAMPTZ,
    media_status TEXT NOT NULL DEFAULT 'not_implemented',
    evidence_state TEXT,
    evidence_reason TEXT,
    raw_clip_uri TEXT,
    raw_clip_size_bytes BIGINT,
    raw_clip_duration_seconds DOUBLE PRECISION,
    raw_clip_sha256 TEXT,
    raw_clip_content_type TEXT DEFAULT 'video/quicktime',
    annotation_status TEXT,
    annotation_count INTEGER,
    matched_objects INTEGER,
    unknown_objects INTEGER,
    visual_evidence_status TEXT,
    frontend_overlay_required BOOLEAN DEFAULT TRUE,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    materialization JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS evidence_bundles_source_created_idx
    ON evidence_bundles(source_id, event_created_at DESC);

CREATE INDEX IF NOT EXISTS evidence_bundles_camera_created_idx
    ON evidence_bundles(camera_id, event_created_at DESC);

CREATE INDEX IF NOT EXISTS evidence_bundles_type_created_idx
    ON evidence_bundles(event_type, event_created_at DESC);

CREATE INDEX IF NOT EXISTS evidence_bundles_media_status_idx
    ON evidence_bundles(media_status);

CREATE INDEX IF NOT EXISTS evidence_bundles_camera_name_idx
    ON evidence_bundles(camera_name);

CREATE TABLE IF NOT EXISTS evidence_artifacts (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL,
    uri TEXT,
    storage_backend TEXT NOT NULL DEFAULT 'filesystem',
    content_type TEXT,
    compression TEXT,
    size_bytes BIGINT,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, artifact_type)
);

CREATE INDEX IF NOT EXISTS evidence_artifacts_type_idx
    ON evidence_artifacts(artifact_type);

CREATE TABLE IF NOT EXISTS evidence_frame_timeline (
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    clip_frame_index INTEGER NOT NULL,
    frame_uuid TEXT,
    frame_pts BIGINT,
    frame_dts BIGINT,
    duration_ns BIGINT,
    timestamp_ms BIGINT,
    width INTEGER,
    height INTEGER,
    source_id TEXT,
    camera_id TEXT,
    stream_session_id TEXT,
    keyframe_uuid TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (event_id, clip_frame_index)
);

CREATE INDEX IF NOT EXISTS evidence_frame_timeline_uuid_idx
    ON evidence_frame_timeline(event_id, frame_uuid);

CREATE INDEX IF NOT EXISTS evidence_frame_timeline_pts_idx
    ON evidence_frame_timeline(event_id, frame_pts);

CREATE TABLE IF NOT EXISTS evidence_overlay_segments (
    event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    clip_frame_index INTEGER NOT NULL,
    frame_uuid TEXT,
    frame_pts BIGINT,
    t_ms INTEGER,
    object_count INTEGER NOT NULL DEFAULT 0,
    objects JSONB NOT NULL DEFAULT '[]'::jsonb,
    record JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, clip_frame_index)
);

CREATE INDEX IF NOT EXISTS evidence_overlay_segments_uuid_idx
    ON evidence_overlay_segments(event_id, frame_uuid);

COMMENT ON TABLE evidence_bundles IS
    'Searchable evidence summary for 8090 list/detail paths. raw_clip_uri points to filesystem/object storage; video bytes are not stored in PostgreSQL.';

COMMENT ON TABLE evidence_artifacts IS
    'Artifact registry for evidence bundle files and DB-backed sidecars.';
