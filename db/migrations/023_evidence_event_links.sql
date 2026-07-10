-- Migration 023: evidence event coverage links.
--
-- A dense burst on one source can contain many event rows that are covered by
-- the same evidence clip. Keep every event searchable without creating a
-- per-event Replay/materialization job for each duplicate window.

CREATE TABLE IF NOT EXISTS evidence_event_links (
    event_id UUID PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    bundle_event_id UUID NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'covered_by',
    reason TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT evidence_event_links_not_self
        CHECK (event_id <> bundle_event_id)
);

CREATE INDEX IF NOT EXISTS evidence_event_links_bundle_event_idx
    ON evidence_event_links(bundle_event_id);

CREATE INDEX IF NOT EXISTS evidence_event_links_relation_idx
    ON evidence_event_links(relation);

COMMENT ON TABLE evidence_event_links IS
    'Maps events covered by another evidence bundle to the bundle event. This is not materialization_skipped.';
