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


def load_config() -> Config:
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
        consumer_name=os.getenv("CONSUMER_NAME", "event-worker-1"),
        poll_timeout_ms=int(os.getenv("POLL_TIMEOUT_MS", "5000")),
        batch_size=int(os.getenv("EVENT_BATCH_SIZE", "10")),
    )
