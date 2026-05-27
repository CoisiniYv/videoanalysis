"""Configuration for face-worker, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    redis_url: str
    database_url: str
    face_observation_stream: str
    consumer_group: str
    consumer_name: str
    poll_timeout_ms: int
    batch_size: int
    consumer_start_id: str


def load_config() -> Config:
    return Config(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://video:video@postgres:5432/video_analytics",
        ),
        face_observation_stream=os.getenv(
            "FACE_OBSERVATION_STREAM", "security.face_observations"
        ),
        consumer_group=os.getenv("CONSUMER_GROUP", "face-workers"),
        consumer_name=os.getenv("CONSUMER_NAME", "face-worker-1"),
        poll_timeout_ms=int(os.getenv("POLL_TIMEOUT_MS", "5000")),
        batch_size=int(os.getenv("BATCH_SIZE", "10")),
        consumer_start_id=os.getenv(
            "FACE_OBSERVATION_CONSUMER_START_ID", "0"
        ),
    )
