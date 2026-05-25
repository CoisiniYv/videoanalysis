"""Configuration for evidence-worker, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    database_url: str = "postgresql://video:video@postgres:5432/video_analytics"
    video_dir: str = "/media/phase3h-savant-output"
    snapshot_output_dir: str = "/media/evidence/snapshots"
    annotated_output_dir: str = "/media/evidence/snapshots/annotated"
    clip_output_dir: str = "/media/evidence/clips"
    pre_seconds: float = 3.0
    post_seconds: float = 3.0
    fps: float = 30.0
    evidence_max_events: int = 5


def load_config() -> Config:
    return Config(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://video:video@postgres:5432/video_analytics",
        ),
        video_dir=os.getenv("VIDEO_DIR", "/media/phase3h-savant-output"),
        snapshot_output_dir=os.getenv(
            "SNAPSHOT_OUTPUT_DIR", "/media/evidence/snapshots"
        ),
        annotated_output_dir=os.getenv(
            "ANNOTATED_OUTPUT_DIR", "/media/evidence/snapshots/annotated"
        ),
        clip_output_dir=os.getenv("CLIP_OUTPUT_DIR", "/media/evidence/clips"),
        pre_seconds=float(os.getenv("PRE_SECONDS", "3")),
        post_seconds=float(os.getenv("POST_SECONDS", "3")),
        fps=float(os.getenv("FPS", "30")),
        evidence_max_events=int(os.getenv("EVIDENCE_MAX_EVENTS", "5")),
    )
