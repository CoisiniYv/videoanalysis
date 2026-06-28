-- Migration 019: narrower media-worker events queue indexes.
--
-- Migration 014 added broad expression indexes for media-worker paths, but
-- PostgreSQL still prefers full events scans for the small periodic queue
-- discovery/update predicates. These partial indexes make each queue predicate
-- directly planner-visible without changing evidence materialization behavior.
--
-- Live-operation note:
--   CREATE INDEX CONCURRENTLY must not run inside a transaction block. Apply this
--   migration with psql/autocommit or another non-transactional migration path.
--   Do not apply it during an active pressure-test window.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_media_generated_clip_queue
    ON events (id)
    WHERE (payload -> 'media' ->> 'clip_status') = 'generated'
      AND clip_path IS NOT NULL
      AND clip_path <> '';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_media_clean_clip_snapshot_queue
    ON events (id)
    WHERE (payload -> 'media' ->> 'clip_status') IN ('ready', 'generated')
      AND clip_path IS NOT NULL
      AND clip_path <> '';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_media_snapshot_not_required_queue
    ON events (id)
    WHERE (payload -> 'media' ->> 'clip_status') IN ('ready', 'generated')
      AND (
          COALESCE(payload -> 'media' ->> 'snapshot_required', 'false') = 'false'
          OR (
              payload -> 'media' ? 'snapshot_required'
              AND payload -> 'media' ->> 'snapshot_required' = 'false'
          )
      )
      AND COALESCE(payload -> 'media' ->> 'snapshot_status', 'not_implemented')
          NOT IN ('ready', 'not_required');

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_media_annotation_pending_queue
    ON events (id)
    WHERE payload -> 'media' ->> 'snapshot_status' = 'ready'
      AND snapshot_path IS NOT NULL
      AND snapshot_path <> ''
      AND COALESCE(payload -> 'media' ->> 'annotated_snapshot_status', 'not_implemented')
          <> 'ready';

COMMENT ON INDEX idx_events_media_generated_clip_queue IS
    'Supports media-worker generated clip promotion without scanning events.';

COMMENT ON INDEX idx_events_media_clean_clip_snapshot_queue IS
    'Supports media-worker snapshot queue discovery for ready/generated clips.';

COMMENT ON INDEX idx_events_media_snapshot_not_required_queue IS
    'Supports media-worker snapshot not-required status convergence.';

COMMENT ON INDEX idx_events_media_annotation_pending_queue IS
    'Supports media-worker annotation queue discovery for ready snapshots.';
