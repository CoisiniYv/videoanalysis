"""Configuration for event-worker, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    redis_url: str
    event_stream: str
    alert_stream: str
    record_request_stream: str
    recording_enabled: bool
    database_url: str
    consumer_group: str
    consumer_name: str
    poll_timeout_ms: int
    batch_size: int
    default_replay_source_id: str
    recording_event_types: tuple[str, ...]
    recording_source_id: str
    recording_max_requests_per_run: int
    recording_cooldown_seconds: int
    recording_cooldown_scope: str
    recording_cooldown_grace_ms: int
    recording_pre_seconds: int
    recording_post_seconds: int
    record_request_dedupe_ttl_seconds: int
    rolling_cache_suppress_record_requests: bool
    evidence_task_creation_enabled: bool
    evidence_task_event_not_before_ts_ms: int
    evidence_task_event_not_after_ts_ms: int
    evidence_task_gate_redis_key: str
    person_observation_stream: str
    person_observation_consumer_group: str
    person_observation_consumer_name: str
    person_observation_consumer_start_id: str
    person_observation_batch_size: int
    person_observation_enabled: bool


def _csv_env(name: str) -> tuple[str, ...]:
    value = os.getenv(name, "")
    return tuple(part.strip() for part in value.split(",") if part.strip())


def load_config() -> Config:
    consumer_name = os.getenv("CONSUMER_NAME", "event-worker-1")
    return Config(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        event_stream=os.getenv("EVENT_STREAM", "security.events"),
        alert_stream=os.getenv("ALERT_STREAM", "security.alerts"),
        record_request_stream=os.getenv(
            "RECORD_REQUEST_STREAM", "security.record_requests"
        ),
        recording_enabled=os.getenv("RECORDING_ENABLED", "false").strip().lower()
        in ("true", "1", "yes"),
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://video:video@postgres:5432/video_analytics",
        ),
        consumer_group=os.getenv("CONSUMER_GROUP", "event-workers"),
        consumer_name=consumer_name,
        poll_timeout_ms=int(os.getenv("POLL_TIMEOUT_MS", "5000")),
        batch_size=max(1, int(os.getenv("EVENT_BATCH_SIZE", "100"))),
        default_replay_source_id=os.getenv("DEFAULT_REPLAY_SOURCE_ID", ""),
        recording_event_types=_csv_env("RECORDING_EVENT_TYPES"),
        recording_source_id=os.getenv("RECORDING_SOURCE_ID", ""),
        recording_max_requests_per_run=int(
            os.getenv("RECORDING_MAX_REQUESTS_PER_RUN", "0")
        ),
        recording_cooldown_seconds=int(
            os.getenv("RECORDING_COOLDOWN_SECONDS", "0")
        ),
        recording_cooldown_scope=os.getenv(
            "RECORDING_COOLDOWN_SCOPE", "event_type"
        ).strip().lower(),
        recording_cooldown_grace_ms=max(
            0, int(os.getenv("RECORDING_COOLDOWN_GRACE_MS", "1000"))
        ),
        recording_pre_seconds=int(
            os.getenv("RECORDING_PRE_SECONDS", os.getenv("DEFAULT_PRE_SECONDS", "5"))
        ),
        recording_post_seconds=int(
            os.getenv("RECORDING_POST_SECONDS", os.getenv("DEFAULT_POST_SECONDS", "5"))
        ),
        record_request_dedupe_ttl_seconds=max(
            1, int(os.getenv("RECORD_REQUEST_DEDUPE_TTL_SECONDS", "86400"))
        ),
        rolling_cache_suppress_record_requests=os.getenv(
            "ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS", "false"
        )
        .strip()
        .lower()
        in ("true", "1", "yes", "on"),
        evidence_task_creation_enabled=os.getenv(
            "EVIDENCE_TASK_CREATION_ENABLED", "true"
        ).strip().lower()
        in ("true", "1", "yes", "on"),
        evidence_task_event_not_before_ts_ms=max(
            0, int(os.getenv("EVIDENCE_TASK_EVENT_NOT_BEFORE_TS_MS", "0"))
        ),
        evidence_task_event_not_after_ts_ms=max(
            0, int(os.getenv("EVIDENCE_TASK_EVENT_NOT_AFTER_TS_MS", "0"))
        ),
        evidence_task_gate_redis_key=os.getenv(
            "EVIDENCE_TASK_GATE_REDIS_KEY", ""
        ).strip(),
        person_observation_stream=os.getenv(
            "PERSON_OBSERVATION_STREAM", "security.person_observations"
        ),
        person_observation_consumer_group=os.getenv(
            "PERSON_OBSERVATION_CONSUMER_GROUP",
            "person-observation-workers",
        ),
        person_observation_consumer_name=os.getenv(
            "PERSON_OBSERVATION_CONSUMER_NAME",
            f"{consumer_name}-person-observations",
        ),
        person_observation_consumer_start_id=os.getenv(
            "PERSON_OBSERVATION_CONSUMER_START_ID", "$"
        ),
        person_observation_batch_size=int(
            os.getenv("PERSON_OBSERVATION_BATCH_SIZE", "100")
        ),
        person_observation_enabled=os.getenv(
            "PERSON_OBSERVATION_CONSUMER_ENABLED", "true"
        ).strip().lower()
        in ("true", "1", "yes"),
    )
