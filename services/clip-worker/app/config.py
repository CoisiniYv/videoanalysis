"""Configuration for clip-worker."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass

from app.replay_shards import ReplayShardMap, load_replay_shard_map


@dataclass(frozen=True)
class Config:
    redis_url: str
    record_request_stream: str
    replay_api_url: str
    replay_job_sink_url: str
    replay_shards: ReplayShardMap
    database_url: str
    consumer_group: str
    consumer_name: str
    consumer_count: int
    poll_timeout_ms: int
    default_pre_seconds: int
    default_post_seconds: int
    keyframe_lookup_window_s: int
    max_jobs_per_run: int
    run_once: bool
    max_concurrent_jobs: int
    pending_claim_min_idle_ms: int
    pending_claim_count: int
    pending_claim_interval_s: float
    deferred_retry_max_attempts: int
    per_camera_cooldown_seconds: int
    replay_stop_condition_mode: str
    replay_fps: int
    replay_duration_extra_slack_s: float
    replay_anchor_strategy: str
    allow_unbounded_keyframe_fallback: bool
    keyframe_lookup_retries: int
    keyframe_lookup_retry_sleep_s: float
    post_savant_frame_proof_attempts: int
    post_savant_frame_proof_retry_sleep_s: float
    post_savant_frame_proof_wait_budget_s: float
    post_savant_frame_proof_poll_interval_s: float
    post_savant_frame_proof_fast_path_batch_size: int
    post_savant_frame_proof_fast_path_lag: int
    post_savant_frame_proof_fast_path_pending: int
    frame_annotation_lookup_concurrency: int
    frame_annotation_range_cache_ttl_s: float
    frame_annotation_range_cache_bucket_ms: int
    frame_annotation_range_cache_max_entries: int
    post_savant_allow_cross_session_post_window_proof: bool
    post_savant_allow_truncated_pre_window_proof: bool
    frame_annotation_stream: str
    frame_annotation_anchor_lookback_count: int
    frame_annotation_anchor_page_count: int
    frame_annotation_anchor_wall_clock_slack_s: float
    frame_annotation_anchor_pts_tolerance_s: float
    evidence_materialization_policy: str
    evidence_high_priority_event_types: tuple[str, ...]
    evidence_defer_low_priority: bool
    evidence_replay_ttl_seconds: int
    evidence_frame_annotation_ttl_seconds: int
    evidence_unknown_source_fail_closed: bool
    evidence_materialization_max_concurrency: int
    evidence_materialization_max_concurrency_per_shard: int
    evidence_materialization_max_concurrency_per_source: int
    evidence_replay_active_slot_extra_seconds: float
    media_poll_interval_s: float
    midterm_sink_stability_checks: int
    evidence_replay_sink_stability_budget_s: float
    evidence_replay_finalizer_budget_s: float
    evidence_replay_slot_grace_s: float
    evidence_materialization_event_type_quotas: dict[str, int]
    evidence_materialization_pressure_level: str
    replay_force_constant_cadence: bool = True
    replay_ts_sync: bool = False
    planner_shadow_enabled: bool = True
    coordinator_v2_enabled: bool = False


def _csv_env(name: str, default: str = "") -> tuple[str, ...]:
    value = os.getenv(name, default)
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _event_type_quotas_env(name: str) -> dict[str, int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        result: dict[str, int] = {}
        for key, value in parsed.items():
            try:
                result[str(key)] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        return result
    result = {}
    for item in raw.split(","):
        if not item.strip() or ":" not in item:
            continue
        key, value = item.split(":", 1)
        try:
            result[key.strip()] = max(0, int(value.strip()))
        except ValueError:
            continue
    return result


def load_config() -> Config:
    replay_api_url = os.getenv("REPLAY_API_URL", "http://replay-service:8080")
    replay_job_sink_url = os.getenv(
        "REPLAY_JOB_SINK_URL",
        "dealer+connect:tcp://video-file-sink:6666",
    )
    return Config(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        record_request_stream=os.getenv(
            "RECORD_REQUEST_STREAM", "security.record_requests"
        ),
        replay_api_url=replay_api_url,
        replay_job_sink_url=replay_job_sink_url,
        replay_shards=load_replay_shard_map(
            default_replay_api_url=replay_api_url,
            default_in_stream_endpoint=os.getenv(
                "REPLAY_IN_STREAM_ENDPOINT",
                "dealer+connect:tcp://replay-service:5555",
            ),
            default_replay_job_sink_url=replay_job_sink_url,
        ),
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://video:video@postgres:5432/video_analytics",
        ),
        consumer_group=os.getenv("CONSUMER_GROUP", "clip-workers"),
        consumer_name=os.getenv("CONSUMER_NAME", "clip-worker-1"),
        consumer_count=max(1, int(os.getenv("CLIP_WORKER_CONSUMER_COUNT", "8"))),
        poll_timeout_ms=int(os.getenv("POLL_TIMEOUT_MS", "5000")),
        default_pre_seconds=int(os.getenv("DEFAULT_PRE_SECONDS", "5")),
        default_post_seconds=int(os.getenv("DEFAULT_POST_SECONDS", "5")),
        keyframe_lookup_window_s=int(os.getenv("KEYFRAME_LOOKUP_WINDOW_S", "10")),
        max_jobs_per_run=int(os.getenv("CLIP_WORKER_MAX_JOBS_PER_RUN", "0")),
        run_once=os.getenv("CLIP_WORKER_RUN_ONCE", "false").strip().lower()
        in ("1", "true", "yes"),
        max_concurrent_jobs=int(
            os.getenv("CLIP_WORKER_MAX_CONCURRENT_JOBS", "0")
        ),
        pending_claim_min_idle_ms=int(
            os.getenv("CLIP_WORKER_PENDING_CLAIM_MIN_IDLE_MS", "5000")
        ),
        pending_claim_count=int(os.getenv("CLIP_WORKER_PENDING_CLAIM_COUNT", "10")),
        pending_claim_interval_s=float(
            os.getenv("CLIP_WORKER_PENDING_CLAIM_INTERVAL_S", "5")
        ),
        deferred_retry_max_attempts=int(
            os.getenv("CLIP_WORKER_DEFERRED_RETRY_MAX_ATTEMPTS", "12")
        ),
        per_camera_cooldown_seconds=int(
            os.getenv("CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS", "0")
        ),
        replay_stop_condition_mode=os.getenv(
            "REPLAY_STOP_CONDITION_MODE", "frame_count"
        ),
        replay_fps=int(os.getenv("REPLAY_FPS", "30")),
        replay_duration_extra_slack_s=float(
            os.getenv("REPLAY_DURATION_EXTRA_SLACK_S", "0")
        ),
        replay_anchor_strategy=os.getenv(
            "REPLAY_ANCHOR_STRATEGY", "request_keyframe"
        ),
        allow_unbounded_keyframe_fallback=os.getenv(
            "ALLOW_UNBOUNDED_KEYFRAME_FALLBACK", "false"
        ).strip().lower() in ("1", "true", "yes"),
        keyframe_lookup_retries=int(os.getenv("KEYFRAME_LOOKUP_RETRIES", "0")),
        keyframe_lookup_retry_sleep_s=float(
            os.getenv("KEYFRAME_LOOKUP_RETRY_SLEEP_S", "1.0")
        ),
        post_savant_frame_proof_attempts=int(
            os.getenv("POST_SAVANT_FRAME_PROOF_ATTEMPTS", "1")
        ),
        post_savant_frame_proof_retry_sleep_s=float(
            os.getenv(
                "POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S",
                os.getenv("KEYFRAME_LOOKUP_RETRY_SLEEP_S", "1.0"),
            )
        ),
        post_savant_frame_proof_wait_budget_s=float(
            os.getenv("POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S", "12")
        ),
        post_savant_frame_proof_poll_interval_s=float(
            os.getenv(
                "POST_SAVANT_FRAME_PROOF_POLL_INTERVAL_S",
                os.getenv(
                    "POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S",
                    os.getenv("KEYFRAME_LOOKUP_RETRY_SLEEP_S", "0.5"),
                ),
            )
        ),
        post_savant_frame_proof_fast_path_batch_size=max(
            0,
            int(os.getenv("POST_SAVANT_FRAME_PROOF_FAST_PATH_BATCH_SIZE", "0")),
        ),
        post_savant_frame_proof_fast_path_lag=max(
            0,
            int(os.getenv("POST_SAVANT_FRAME_PROOF_FAST_PATH_LAG", "0")),
        ),
        post_savant_frame_proof_fast_path_pending=max(
            0,
            int(os.getenv("POST_SAVANT_FRAME_PROOF_FAST_PATH_PENDING", "0")),
        ),
        frame_annotation_lookup_concurrency=max(
            1,
            int(os.getenv("CLIP_WORKER_FRAME_ANNOTATION_LOOKUP_CONCURRENCY", "8")),
        ),
        frame_annotation_range_cache_ttl_s=max(
            0.0,
            float(os.getenv("CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_TTL_S", "0.75")),
        ),
        frame_annotation_range_cache_bucket_ms=max(
            0,
            int(os.getenv("CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_BUCKET_MS", "1000")),
        ),
        frame_annotation_range_cache_max_entries=max(
            1,
            int(os.getenv("CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_MAX_ENTRIES", "64")),
        ),
        post_savant_allow_cross_session_post_window_proof=os.getenv(
            "POST_SAVANT_ALLOW_CROSS_SESSION_POST_WINDOW_PROOF", "true"
        )
        .strip()
        .lower()
        in ("1", "true", "yes"),
        post_savant_allow_truncated_pre_window_proof=os.getenv(
            "POST_SAVANT_ALLOW_TRUNCATED_PRE_WINDOW_PROOF", "true"
        )
        .strip()
        .lower()
        in ("1", "true", "yes"),
        frame_annotation_stream=os.getenv(
            "FRAME_ANNOTATION_STREAM", "security.frame_annotations"
        ),
        frame_annotation_anchor_lookback_count=int(
            os.getenv("FRAME_ANNOTATION_ANCHOR_LOOKBACK_COUNT", "20000")
        ),
        frame_annotation_anchor_page_count=int(
            os.getenv("FRAME_ANNOTATION_ANCHOR_PAGE_COUNT", "2000")
        ),
        frame_annotation_anchor_wall_clock_slack_s=float(
            os.getenv("FRAME_ANNOTATION_ANCHOR_WALL_CLOCK_SLACK_S", "1.0")
        ),
        frame_annotation_anchor_pts_tolerance_s=float(
            os.getenv("FRAME_ANNOTATION_ANCHOR_PTS_TOLERANCE_S", "1.0")
        ),
        evidence_materialization_policy=os.getenv(
            "EVIDENCE_MATERIALIZATION_POLICY", "priority"
        ),
        evidence_high_priority_event_types=_csv_env(
            "EVIDENCE_HIGH_PRIORITY_EVENT_TYPES",
            "watchlist_hit,live_search_hit",
        ),
        evidence_defer_low_priority=_bool_env(
            "EVIDENCE_MATERIALIZATION_DEFER_LOW_PRIORITY", "false"
        ),
        evidence_replay_ttl_seconds=int(
            os.getenv("EVIDENCE_REPLAY_TTL_SECONDS", "300")
        ),
        evidence_frame_annotation_ttl_seconds=int(
            os.getenv("EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS", "120")
        ),
        evidence_unknown_source_fail_closed=_bool_env(
            "EVIDENCE_UNKNOWN_SOURCE_FAIL_CLOSED", "true"
        ),
        evidence_materialization_max_concurrency=int(
            os.getenv(
                "EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY",
                os.getenv("CLIP_WORKER_MAX_CONCURRENT_JOBS", "4"),
            )
        ),
        evidence_materialization_max_concurrency_per_shard=int(
            os.getenv("EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD", "2")
        ),
        evidence_materialization_max_concurrency_per_source=int(
            os.getenv("EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE", "1")
        ),
        evidence_replay_active_slot_extra_seconds=max(
            0.0,
            float(os.getenv("EVIDENCE_REPLAY_ACTIVE_SLOT_EXTRA_SECONDS", "5")),
        ),
        media_poll_interval_s=max(
            0.0,
            float(os.getenv("MEDIA_POLL_INTERVAL_S", "5")),
        ),
        midterm_sink_stability_checks=max(
            0,
            int(os.getenv("MIDTERM_SINK_STABILITY_CHECKS", "2")),
        ),
        evidence_replay_sink_stability_budget_s=max(
            0.0,
            float(os.getenv("EVIDENCE_REPLAY_SLOT_SINK_STABILITY_BUDGET_S", "60")),
        ),
        evidence_replay_finalizer_budget_s=max(
            0.0,
            float(os.getenv("EVIDENCE_REPLAY_SLOT_FINALIZER_BUDGET_S", "0")),
        ),
        evidence_replay_slot_grace_s=max(
            0.0,
            float(
                os.getenv(
                    "EVIDENCE_REPLAY_SLOT_GRACE_S",
                    os.getenv("EVIDENCE_REPLAY_ACTIVE_SLOT_EXTRA_SECONDS", "5"),
                )
            ),
        ),
        evidence_materialization_event_type_quotas=_event_type_quotas_env(
            "EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS"
        ),
        evidence_materialization_pressure_level=os.getenv(
            "EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL", "normal"
        )
        .strip()
        .lower(),
        replay_force_constant_cadence=_bool_env(
            "REPLAY_FORCE_CONSTANT_CADENCE", "true"
        ),
        replay_ts_sync=_bool_env("REPLAY_TS_SYNC", "false"),
        planner_shadow_enabled=_bool_env(
            "CLIP_WORKER_PLANNER_SHADOW_ENABLED", "true"
        ),
        coordinator_v2_enabled=_bool_env(
            "CLIP_WORKER_COORDINATOR_V2_ENABLED", "false"
        ),
    )
