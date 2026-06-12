-- Migration 014: targeted indexes for media/evidence worker query shapes.
--
-- These indexes cover the hot JSONB predicates used by media-worker:
--   * generated clip promotion by payload.media.clip_status;
--   * snapshot queue discovery by clip_status/snapshot_required/snapshot_status;
--   * annotation queue discovery by snapshot_status/annotated_snapshot_status.
--
-- The indexes are intentionally expression/partial indexes instead of schema
-- rewrites. They keep inserts cheap for events that have no media payload yet
-- while making worker queue scans planner-visible.

CREATE INDEX IF NOT EXISTS events_media_clip_status_with_clip_idx
    ON events ((payload -> 'media' ->> 'clip_status'))
    WHERE clip_path IS NOT NULL
      AND clip_path <> '';

CREATE INDEX IF NOT EXISTS events_media_snapshot_queue_idx
    ON events (
        (payload -> 'media' ->> 'clip_status'),
        (payload -> 'media' ->> 'snapshot_required'),
        (payload -> 'media' ->> 'snapshot_status')
    )
    WHERE clip_path IS NOT NULL
      AND clip_path <> ''
      AND (payload -> 'media' ->> 'clip_status') IN ('ready', 'generated');

CREATE INDEX IF NOT EXISTS events_media_snapshot_not_required_idx
    ON events (
        (payload -> 'media' ->> 'snapshot_status'),
        (payload -> 'media' ->> 'snapshot_required')
    )
    WHERE (payload -> 'media' ->> 'clip_status') IN ('ready', 'generated');

CREATE INDEX IF NOT EXISTS events_media_annotation_queue_idx
    ON events ((payload -> 'media' ->> 'annotated_snapshot_status'))
    WHERE (payload -> 'media' ->> 'snapshot_status') = 'ready'
      AND snapshot_path IS NOT NULL
      AND snapshot_path <> '';

COMMENT ON INDEX events_media_clip_status_with_clip_idx IS
    'Supports media-worker generated clip promotion and snapshot queue scans.';

COMMENT ON INDEX events_media_snapshot_queue_idx IS
    'Supports media-worker snapshot queue discovery over payload.media fields.';

COMMENT ON INDEX events_media_snapshot_not_required_idx IS
    'Supports media-worker mark-not-required scan over snapshot flags.';

COMMENT ON INDEX events_media_annotation_queue_idx IS
    'Supports media-worker annotated snapshot queue discovery.';
