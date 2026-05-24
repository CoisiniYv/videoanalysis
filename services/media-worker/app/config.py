"""Configuration for media-worker."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    database_url: str
    sink_output_dir: str
    poll_interval_s: int


def load_config() -> Config:
    return Config(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://video:video@postgres:5432/video_analytics",
        ),
        sink_output_dir=os.getenv(
            "SINK_OUTPUT_DIR", "/media/replay-sink-output"
        ),
        poll_interval_s=int(os.getenv("MEDIA_POLL_INTERVAL_S", "10")),
    )
