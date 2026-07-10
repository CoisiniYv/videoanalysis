-- Migration 022: completion-aware Replay admission slot lifecycle.
--
-- Additive only. Clip-worker writes an active slot after Replay job creation;
-- media-worker releases it when sink output becomes stable/ready, with timeout
-- fallback for crashed or stalled jobs.

ALTER TABLE evidence_tasks
    ADD COLUMN IF NOT EXISTS replay_job_id TEXT,
    ADD COLUMN IF NOT EXISTS replay_resulting_stream_id TEXT,
    ADD COLUMN IF NOT EXISTS replay_sink_instance TEXT,
    ADD COLUMN IF NOT EXISTS replay_duration_seconds_effective DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS replay_duration_effective_reason TEXT,
    ADD COLUMN IF NOT EXISTS replay_slot_status TEXT,
    ADD COLUMN IF NOT EXISTS replay_slot_acquired_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS replay_slot_deadline_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS replay_slot_released_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS replay_slot_release_reason TEXT,
    ADD COLUMN IF NOT EXISTS replay_slot_timeout_budget_s DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS replay_slot_active_age_s DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS sink_video_to_stable_ms INTEGER,
    ADD COLUMN IF NOT EXISTS finalization_duration_ms INTEGER;

CREATE INDEX IF NOT EXISTS evidence_tasks_replay_slot_active_idx
    ON evidence_tasks(replay_slot_deadline_at, replay_shard_id, replay_source_id)
    WHERE replay_slot_status = 'active';

CREATE INDEX IF NOT EXISTS evidence_tasks_replay_slot_event_idx
    ON evidence_tasks(event_id, replay_slot_status);

COMMENT ON COLUMN evidence_tasks.replay_slot_status IS
    'Completion-aware Replay admission slot state: active, released, or timeout.';

COMMENT ON COLUMN evidence_tasks.replay_slot_deadline_at IS
    'Fallback deadline for crashed or stalled Replay/video-file-sink/media-worker slot release.';
