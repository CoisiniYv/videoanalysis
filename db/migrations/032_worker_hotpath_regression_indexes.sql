-- Migration 032: remove worker hot-path sequential scans introduced by the
-- materialization scheduler and algorithm-scoped cooldown lookups.
--
-- This migration is deliberately non-transactional because PostgreSQL does
-- not allow CREATE INDEX CONCURRENTLY inside a transaction block. Apply it
-- with psql/autocommit and never during an active pressure-test window.

CREATE INDEX CONCURRENTLY IF NOT EXISTS evidence_tasks_cleanup_pending_idx
    ON evidence_tasks (updated_at ASC, event_id)
    WHERE cleanup_audit->'sink_output'->>'status' = 'cleanup_pending'
      AND COALESCE(cleanup_audit->'sink_output'->>'path', '') <> '';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_camera_algorithm_ts_desc_unsuppressed
    ON events (
        camera_id,
        (COALESCE(NULLIF(algorithm_type, ''), event_type)),
        event_ts_ms DESC
    )
    WHERE COALESCE(status, 'new') <> 'suppressed';

COMMENT ON INDEX evidence_tasks_cleanup_pending_idx IS
    'Supports media-worker durable sink-cleanup recovery without scanning terminal evidence tasks.';

COMMENT ON INDEX idx_events_camera_algorithm_ts_desc_unsuppressed IS
    'Supports event-worker algorithm-scoped cooldown lookup ordered by latest event timestamp.';
