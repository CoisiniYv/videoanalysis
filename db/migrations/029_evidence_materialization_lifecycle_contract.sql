-- Migration 029: canonical materialization lifecycle, retry and fenced lease.
--
-- Transactional and additive.  Migrations 025-028 are published history and
-- must not be rewritten.  This migration makes materialization_deferred
-- claim-terminal, separates retry timing from the original ready time, and
-- introduces explicit phase/owner/fenced-lease/handoff fields.
--
-- Optional upgrade-session settings used only for legacy active rows whose
-- materialization_ready_at is NULL:
--
--   SET video_analytics.materialization_segment_seconds = '4';
--   SET video_analytics.materialization_ready_grace_seconds = '9';
--
-- If the historical values are not supplied, ambiguous active rows are put in
-- an auditable manual quarantine instead of inventing a ready time.

BEGIN;

ALTER TABLE evidence_tasks
    ADD COLUMN IF NOT EXISTS materialization_phase TEXT,
    ADD COLUMN IF NOT EXISTS materialization_phase_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS materialization_next_attempt_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS materialization_retry_reason TEXT,
    ADD COLUMN IF NOT EXISTS materialization_owner TEXT,
    ADD COLUMN IF NOT EXISTS materialization_lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS materialization_lease_token TEXT,
    ADD COLUMN IF NOT EXISTS materialization_lease_generation BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS materialization_lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS materialization_lease_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS materialization_handoff JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN evidence_tasks.materialization_ready_at IS
    'Immutable natural/policy readiness time produced by event-worker; retries must not move it.';
COMMENT ON COLUMN evidence_tasks.materialization_next_attempt_at IS
    'Retry eligibility time; NULL means use the original materialization_ready_at.';
COMMENT ON COLUMN evidence_tasks.materialization_retry_reason IS
    'Normalized retryable reason code; mutually exclusive with terminal defer/failure/expiry reasons.';
COMMENT ON COLUMN evidence_tasks.materialization_owner IS
    'Lifecycle owner: replay, rolling, media_finalizer, or terminal.';
COMMENT ON COLUMN evidence_tasks.materialization_lease_token IS
    'Opaque fencing token checked by every rolling phase/terminal transition.';
COMMENT ON COLUMN evidence_tasks.materialization_lease_generation IS
    'Monotonic fencing generation incremented on each durable claim.';
COMMENT ON COLUMN evidence_tasks.materialization_handoff IS
    'Immutable remux-to-finalizer artifact identity for the current fenced attempt.';

-- Normalize compatibility values before classifying reasons and ownership.
UPDATE evidence_tasks
SET materialization_status = CASE
        WHEN materialization_status IN (
            'pending', 'waiting_proof', 'queued', 'replay_job_created',
            'replaying', 'processing', 'claimed'
        ) THEN 'materialization_pending'
        WHEN materialization_status = 'finalizing' THEN 'materializing'
        WHEN materialization_status IN (
            'ready', 'generated', 'generated_unverified',
            'generated_annotation_failed'
        ) THEN 'materialized'
        WHEN materialization_status IN (
            'failed', 'duration_guard_failed', 'generated_corrupt'
        ) THEN 'materialization_failed'
        WHEN materialization_status IN ('not_implemented')
            THEN 'materialization_skipped'
        ELSE materialization_status
    END,
    status = CASE
        WHEN status = 'claimed' THEN 'materialization_pending'
        ELSE status
    END,
    updated_at = now()
WHERE materialization_status IN (
        'pending', 'waiting_proof', 'queued', 'replay_job_created',
        'replaying', 'processing', 'claimed', 'finalizing', 'ready',
        'generated', 'generated_unverified', 'generated_annotation_failed',
        'failed', 'duration_guard_failed', 'generated_corrupt',
        'not_implemented'
    )
   OR status = 'claimed';

-- Backfill source and epoch only from exact persisted event identity.
UPDATE evidence_tasks et
SET source_id = e.source_id,
    replay_source_id = COALESCE(NULLIF(et.replay_source_id, ''), e.source_id),
    updated_at = now()
FROM events e
WHERE e.id = et.event_id
  AND COALESCE(et.source_id, '') = ''
  AND COALESCE(e.source_id, '') <> '';

WITH epoch_candidates AS (
    SELECT
        et.task_id,
        NULLIF(e.payload->>'runtime_epoch_id', '') AS root_epoch,
        NULLIF(e.payload->'media'->>'runtime_epoch_id', '') AS media_epoch
    FROM evidence_tasks et
    JOIN events e ON e.id = et.event_id
)
UPDATE evidence_tasks et
SET runtime_epoch_id = COALESCE(c.root_epoch, c.media_epoch),
    updated_at = now()
FROM epoch_candidates c
WHERE c.task_id = et.task_id
  AND COALESCE(et.runtime_epoch_id, '') = ''
  AND COALESCE(c.root_epoch, c.media_epoch, '') <> ''
  AND (c.root_epoch IS NULL OR c.media_epoch IS NULL OR c.root_epoch = c.media_epoch);

-- Classify legacy deferred rows.  Unknown reason-filled rows remain terminal
-- deferred for manual audit; reason-empty and known retryable rows are the only
-- rows converted back to pending.
WITH classified AS (
    SELECT
        task_id,
        COALESCE(NULLIF(BTRIM(materialization_defer_reason), ''), '') AS legacy_reason,
        CASE
            WHEN COALESCE(NULLIF(BTRIM(materialization_defer_reason), ''), '') = ''
                THEN 'legacy_empty_deferred'
            WHEN lower(materialization_defer_reason) LIKE 'covered_by_existing_evidence%'
              OR lower(materialization_defer_reason) LIKE 'covered_by_event:%'
                THEN 'covered_by_existing_evidence'
            WHEN lower(materialization_defer_reason) LIKE '%storage_hard_limit%'
              OR lower(materialization_defer_reason) LIKE '%storage_hard_stop%'
                THEN 'storage_hard_stop'
            WHEN lower(materialization_defer_reason) LIKE '%no_overlapping_final_segment%'
              OR lower(materialization_defer_reason) LIKE '%no_overlapping_segments%'
                THEN 'no_overlapping_final_segment'
            WHEN lower(materialization_defer_reason) LIKE '%segment_not_stable%'
              OR lower(materialization_defer_reason) LIKE '%half_written_segment%'
                THEN 'segment_not_stable'
            WHEN lower(materialization_defer_reason) LIKE '%segment_disappeared%'
              OR lower(materialization_defer_reason) LIKE '%segment_deleted%'
                THEN 'segment_disappeared'
            WHEN lower(materialization_defer_reason) LIKE '%coverage%'
              OR lower(materialization_defer_reason) LIKE '%pre_gap_ns=%'
              OR lower(materialization_defer_reason) LIKE '%post_gap_ns=%'
                THEN 'coverage_not_complete'
            WHEN lower(materialization_defer_reason) LIKE '%concurrency_limit%'
              OR lower(materialization_defer_reason) LIKE '%backlog_limit%'
              OR lower(materialization_defer_reason) LIKE '%capacity_unavailable%'
              OR lower(materialization_defer_reason) LIKE '%max_active%'
              OR lower(materialization_defer_reason) LIKE '%max_per_poll%'
                THEN 'capacity_unavailable'
            WHEN lower(materialization_defer_reason) LIKE '%db_pool_timeout%'
                THEN 'db_pool_timeout'
            WHEN lower(materialization_defer_reason) LIKE '%db_unavailable%'
              OR lower(materialization_defer_reason) LIKE '%connection refused%'
                THEN 'db_unavailable'
            WHEN lower(materialization_defer_reason) LIKE '%temporary_io_error%'
              OR lower(materialization_defer_reason) LIKE '%resource temporarily unavailable%'
                THEN 'temporary_io_error'
            ELSE 'unknown'
        END AS normalized_reason
    FROM evidence_tasks
    WHERE materialization_status = 'materialization_deferred'
), retryable AS (
    SELECT *
    FROM classified
    WHERE normalized_reason IN (
        'legacy_empty_deferred', 'coverage_not_complete',
        'no_overlapping_final_segment', 'segment_not_stable',
        'segment_disappeared', 'capacity_unavailable', 'db_unavailable',
        'db_pool_timeout', 'temporary_io_error'
    )
)
UPDATE evidence_tasks et
SET status = CASE
        WHEN et.materialization_deadline_at IS NOT NULL
         AND et.materialization_deadline_at <= now()
            THEN 'materialization_expired'
        ELSE 'materialization_pending'
    END,
    materialization_status = CASE
        WHEN et.materialization_deadline_at IS NOT NULL
         AND et.materialization_deadline_at <= now()
            THEN 'materialization_expired'
        ELSE 'materialization_pending'
    END,
    materialization_phase = CASE
        WHEN et.materialization_deadline_at IS NOT NULL
         AND et.materialization_deadline_at <= now()
            THEN 'terminal'
        WHEN r.normalized_reason IN (
            'coverage_not_complete', 'no_overlapping_final_segment',
            'segment_not_stable', 'segment_disappeared',
            'legacy_empty_deferred'
        ) THEN 'waiting_coverage'
        ELSE 'waiting_ready'
    END,
    materialization_phase_updated_at = now(),
    materialization_next_attempt_at = CASE
        WHEN et.materialization_deadline_at IS NOT NULL
         AND et.materialization_deadline_at <= now()
            THEN NULL
        ELSE GREATEST(now(), COALESCE(et.materialization_ready_at, now()))
    END,
    materialization_retry_reason = CASE
        WHEN et.materialization_deadline_at IS NOT NULL
         AND et.materialization_deadline_at <= now()
            THEN NULL
        ELSE r.normalized_reason
    END,
    materialization_defer_reason = NULL,
    materialization_failure_reason = NULL,
    materialization_expired_reason = CASE
        WHEN et.materialization_deadline_at IS NOT NULL
         AND et.materialization_deadline_at <= now()
            THEN 'business_deadline_expired'
        ELSE NULL
    END,
    materialization_owner = CASE
        WHEN et.materialization_deadline_at IS NOT NULL
         AND et.materialization_deadline_at <= now()
            THEN 'terminal'
        WHEN COALESCE(et.task_type, '') = 'image_only'
          OR r.normalized_reason IN (
              'coverage_not_complete', 'no_overlapping_final_segment',
              'segment_not_stable', 'segment_disappeared'
          ) THEN 'rolling'
        WHEN COALESCE(et.sink_output_path, '') <> '' THEN 'media_finalizer'
        ELSE 'replay'
    END,
    materialization_lease_owner = NULL,
    materialization_lease_token = NULL,
    materialization_lease_expires_at = NULL,
    materialization_lease_heartbeat_at = NULL,
    materialization_handoff = '{}'::jsonb,
    materialization_audit = COALESCE(et.materialization_audit, '{}'::jsonb)
        || jsonb_build_object(
            'migration_029',
            jsonb_strip_nulls(jsonb_build_object(
                'legacy_deferred_reason', NULLIF(r.legacy_reason, ''),
                'normalized_reason', r.normalized_reason,
                'classified_at', now()
            ))
        ),
    updated_at = now()
FROM retryable r
WHERE r.task_id = et.task_id;

-- Remaining deferred rows are claim-terminal.
UPDATE evidence_tasks
SET status = 'materialization_deferred',
    materialization_phase = 'terminal',
    materialization_phase_updated_at = now(),
    materialization_next_attempt_at = NULL,
    materialization_retry_reason = NULL,
    materialization_defer_reason = CASE
        WHEN lower(COALESCE(materialization_defer_reason, '')) LIKE 'covered_by%'
            THEN 'covered_by_existing_evidence'
        WHEN lower(COALESCE(materialization_defer_reason, '')) LIKE '%storage_hard%'
            THEN 'storage_hard_stop'
        ELSE materialization_defer_reason
    END,
    materialization_failure_reason = NULL,
    materialization_expired_reason = NULL,
    materialization_owner = 'terminal',
    materialization_lease_owner = NULL,
    materialization_lease_token = NULL,
    materialization_lease_expires_at = NULL,
    materialization_lease_heartbeat_at = NULL,
    materialization_handoff = '{}'::jsonb,
    updated_at = now()
WHERE materialization_status = 'materialization_deferred';

-- Exact legacy ready-time backfill.  No deployment-wide default is invented.
WITH settings AS (
    SELECT
        NULLIF(
            current_setting(
                'video_analytics.materialization_segment_seconds', true
            ),
            ''
        )::double precision AS segment_seconds,
        NULLIF(
            current_setting(
                'video_analytics.materialization_ready_grace_seconds', true
            ),
            ''
        )::double precision AS grace_seconds
), eligible AS (
    SELECT
        et.task_id,
        CASE
            WHEN et.event_ts_ms BETWEEN 946684800000 AND 4102444800000
                THEN to_timestamp(et.event_ts_ms::double precision / 1000.0)
            ELSE e.created_at
        END AS event_at,
        CASE
            WHEN COALESCE(et.task_type, '') = 'image_only'
                THEN s.segment_seconds
            ELSE GREATEST(0, COALESCE(et.post_seconds, 0))::double precision
        END AS policy_wait_s,
        s.grace_seconds
    FROM evidence_tasks et
    JOIN events e ON e.id = et.event_id
    CROSS JOIN settings s
    WHERE et.materialization_status IN (
        'manifest_ready', 'materialization_pending', 'materializing'
    )
      AND et.materialization_ready_at IS NULL
      AND s.grace_seconds IS NOT NULL
      AND (
          COALESCE(et.task_type, '') <> 'image_only'
          OR s.segment_seconds IS NOT NULL
      )
)
UPDATE evidence_tasks et
SET materialization_ready_at = eligible.event_at
        + (eligible.policy_wait_s + eligible.grace_seconds) * interval '1 second',
    materialization_audit = COALESCE(et.materialization_audit, '{}'::jsonb)
        || jsonb_build_object(
            'migration_029_ready_backfill',
            jsonb_build_object(
                'policy_wait_s', eligible.policy_wait_s,
                'grace_seconds', eligible.grace_seconds,
                'backfilled_at', now()
            )
        ),
    updated_at = now()
FROM eligible
WHERE eligible.task_id = et.task_id;

-- Backfill explicit owners only from unambiguous durable evidence.
UPDATE evidence_tasks
SET materialization_owner = CASE
        WHEN materialization_status IN (
            'materialized', 'materialization_deferred', 'materialization_failed',
            'materialization_expired', 'materialization_skipped'
        ) THEN 'terminal'
        WHEN COALESCE(task_type, '') = 'image_only' THEN 'rolling'
        WHEN COALESCE(materialization_audit->'rolling_cache'->>'status', '') <> ''
          OR COALESCE(materialization_audit->>'materialization_mode', '')
                LIKE 'rolling_cache%'
            THEN 'rolling'
        WHEN COALESCE(replay_job_id, '') <> ''
          OR COALESCE(replay_slot_status, '') <> ''
          OR status IN (
              'waiting_proof', 'queued', 'replay_job_created', 'replaying'
          )
            THEN 'replay'
        ELSE materialization_owner
    END,
    updated_at = now()
WHERE COALESCE(materialization_owner, '') = '';

UPDATE evidence_tasks
SET materialization_phase = CASE
        WHEN materialization_status IN ('manifest_ready', 'materialization_pending')
            THEN COALESCE(materialization_phase, 'waiting_ready')
        WHEN materialization_status = 'materializing'
         AND materialization_owner = 'rolling'
         AND COALESCE(task_type, '') = 'image_only'
            THEN COALESCE(materialization_phase, 'image_running')
        WHEN materialization_status = 'materializing'
         AND materialization_owner = 'rolling'
            THEN COALESCE(materialization_phase, 'remux_running')
        WHEN materialization_status IN (
            'materialized', 'materialization_deferred', 'materialization_failed',
            'materialization_expired', 'materialization_skipped'
        ) THEN CASE
            WHEN materialization_phase = 'manual_quarantine'
                THEN materialization_phase
            ELSE 'terminal'
        END
        ELSE materialization_phase
    END,
    materialization_phase_updated_at = COALESCE(
        materialization_phase_updated_at,
        now()
    ),
    updated_at = now();

-- Any active row whose identity/ready-time/owner is ambiguous is terminalized
-- for manual repair.  A migration must never bind it to the current epoch.
WITH identity AS (
    SELECT
        et.task_id,
        e.source_id AS event_source_id,
        NULLIF(e.payload->>'runtime_epoch_id', '') AS root_epoch,
        NULLIF(e.payload->'media'->>'runtime_epoch_id', '') AS media_epoch
    FROM evidence_tasks et
    JOIN events e ON e.id = et.event_id
    WHERE et.materialization_status IN (
        'manifest_ready', 'materialization_pending', 'materializing'
    )
), ambiguous AS (
    SELECT task_id
    FROM identity i
    JOIN evidence_tasks et USING (task_id)
    WHERE COALESCE(et.source_id, '') = ''
       OR COALESCE(et.runtime_epoch_id, '') = ''
       OR et.materialization_ready_at IS NULL
       OR COALESCE(et.materialization_owner, '') = ''
       OR (
            COALESCE(i.event_source_id, '') <> ''
        AND COALESCE(et.source_id, '') <> i.event_source_id
       )
       OR (i.root_epoch IS NOT NULL AND i.media_epoch IS NOT NULL
           AND i.root_epoch <> i.media_epoch)
       OR (i.root_epoch IS NOT NULL AND et.runtime_epoch_id <> i.root_epoch)
       OR (i.media_epoch IS NOT NULL AND et.runtime_epoch_id <> i.media_epoch)
)
UPDATE evidence_tasks et
SET status = 'materialization_failed',
    materialization_status = 'materialization_failed',
    materialization_phase = 'manual_quarantine',
    materialization_phase_updated_at = now(),
    materialization_next_attempt_at = NULL,
    materialization_retry_reason = NULL,
    materialization_defer_reason = NULL,
    materialization_failure_reason = 'ambiguous_active_identity',
    materialization_expired_reason = NULL,
    materialization_owner = 'terminal',
    materialization_lease_owner = NULL,
    materialization_lease_token = NULL,
    materialization_lease_expires_at = NULL,
    materialization_lease_heartbeat_at = NULL,
    materialization_handoff = '{}'::jsonb,
    error_message = 'ambiguous_active_identity:manual_quarantine',
    materialization_audit = COALESCE(et.materialization_audit, '{}'::jsonb)
        || jsonb_build_object(
            'migration_029_quarantine',
            jsonb_build_object(
                'reason', 'ambiguous_active_identity',
                'observed_at', now()
            )
        ),
    updated_at = now()
FROM ambiguous a
WHERE a.task_id = et.task_id;

-- Normalize field-reset contracts for every canonical family.
UPDATE evidence_tasks
SET materialization_defer_reason = NULL,
    materialization_failure_reason = NULL,
    materialization_expired_reason = NULL
WHERE materialization_status IN ('manifest_ready', 'materialization_pending');

UPDATE evidence_tasks
SET materialization_next_attempt_at = NULL,
    materialization_retry_reason = NULL,
    materialization_defer_reason = NULL
WHERE materialization_status = 'materializing';

UPDATE evidence_tasks
SET materialization_next_attempt_at = NULL,
    materialization_retry_reason = NULL,
    materialization_defer_reason = CASE
        WHEN materialization_status = 'materialization_deferred'
            THEN materialization_defer_reason
        ELSE NULL
    END,
    materialization_failure_reason = CASE
        WHEN materialization_status = 'materialization_failed'
            THEN materialization_failure_reason
        ELSE NULL
    END,
    materialization_expired_reason = CASE
        WHEN materialization_status = 'materialization_expired'
            THEN materialization_expired_reason
        ELSE NULL
    END,
    materialization_lease_owner = NULL,
    materialization_lease_token = NULL,
    materialization_lease_expires_at = NULL,
    materialization_lease_heartbeat_at = NULL,
    materialization_owner = 'terminal',
    materialization_phase = CASE
        WHEN materialization_phase = 'manual_quarantine'
            THEN materialization_phase
        ELSE 'terminal'
    END,
    materialization_phase_updated_at = COALESCE(
        materialization_phase_updated_at,
        now()
    )
WHERE materialization_status IN (
    'materialized', 'materialization_deferred', 'materialization_failed',
    'materialization_expired', 'materialization_skipped'
);

COMMIT;
