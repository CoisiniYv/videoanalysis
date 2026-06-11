"""Configuration for clip-worker."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    redis_url: str
    record_request_stream: str
    replay_api_url: str
    replay_job_sink_url: str
    database_url: str
    consumer_group: str
    consumer_name: str
    poll_timeout_ms: int
    default_pre_seconds: int
    default_post_seconds: int
    keyframe_lookup_window_s: int
    max_jobs_per_run: int
    run_once: bool
    max_concurrent_jobs: int
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
    frame_annotation_stream: str
    frame_annotation_anchor_lookback_count: int
    frame_annotation_anchor_wall_clock_slack_s: float
    frame_annotation_anchor_pts_tolerance_s: float


def load_config() -> Config:
    return Config(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        record_request_stream=os.getenv(
            "RECORD_REQUEST_STREAM", "security.record_requests"
        ),
        replay_api_url=os.getenv("REPLAY_API_URL", "http://replay-service:8080"),
        replay_job_sink_url=os.getenv(
            "REPLAY_JOB_SINK_URL",
            "dealer+connect:tcp://video-file-sink:6666",
        ),
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://video:video@postgres:5432/video_analytics",
        ),
        consumer_group=os.getenv("CONSUMER_GROUP", "clip-workers"),
        consumer_name=os.getenv("CONSUMER_NAME", "clip-worker-1"),
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
            os.getenv("POST_SAVANT_FRAME_PROOF_ATTEMPTS", "30")
        ),
        post_savant_frame_proof_retry_sleep_s=float(
            os.getenv(
                "POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S",
                os.getenv("KEYFRAME_LOOKUP_RETRY_SLEEP_S", "1.0"),
            )
        ),
        frame_annotation_stream=os.getenv(
            "FRAME_ANNOTATION_STREAM", "security.frame_annotations"
        ),
        frame_annotation_anchor_lookback_count=int(
            os.getenv("FRAME_ANNOTATION_ANCHOR_LOOKBACK_COUNT", "20000")
        ),
        frame_annotation_anchor_wall_clock_slack_s=float(
            os.getenv("FRAME_ANNOTATION_ANCHOR_WALL_CLOCK_SLACK_S", "1.0")
        ),
        frame_annotation_anchor_pts_tolerance_s=float(
            os.getenv("FRAME_ANNOTATION_ANCHOR_PTS_TOLERANCE_S", "1.0")
        ),
    )
