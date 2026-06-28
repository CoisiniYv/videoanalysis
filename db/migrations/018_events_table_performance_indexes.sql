-- Migration 018: targeted events-table indexes for midterm operator/runtime hot paths.
--
-- These indexes match the query shapes documented in
-- specs/25_midterm_events_table_performance_plan.md:
--   * recent event list ordered by created_at DESC;
--   * event_type-filtered pagination ordered by created_at DESC;
--   * event-worker source/type recent existence checks;
--   * event-worker non-suppressed cooldown lookup by camera/type/event_ts_ms.
--
-- Live-operation note:
--   CREATE INDEX CONCURRENTLY must not run inside a transaction block. Apply this
--   migration with psql/autocommit or another non-transactional migration path.
--   Do not apply it during an active pressure-test window.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_created_at_desc
    ON events (created_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_event_type_created_at_desc
    ON events (event_type, created_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_source_type_created_at_desc_unsuppressed
    ON events (source_id, event_type, created_at DESC)
    WHERE COALESCE(status, 'new') <> 'suppressed';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_camera_type_ts_desc_unsuppressed
    ON events (camera_id, event_type, event_ts_ms DESC)
    WHERE COALESCE(status, 'new') <> 'suppressed';

COMMENT ON INDEX idx_events_created_at_desc IS
    'Supports recent events list and default event pagination without sorting the events table.';

COMMENT ON INDEX idx_events_event_type_created_at_desc IS
    'Supports event_type-filtered event pagination ordered by created_at DESC.';

COMMENT ON INDEX idx_events_source_type_created_at_desc_unsuppressed IS
    'Supports event-worker source/type recent existence checks for non-suppressed events.';

COMMENT ON INDEX idx_events_camera_type_ts_desc_unsuppressed IS
    'Supports event-worker cooldown lookup for latest non-suppressed camera/type event.';
