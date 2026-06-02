"""Configuration for media-worker."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    database_url: str
    sink_output_dir: str
    snapshot_output_dir: str
    annotated_output_dir: str
    evidence_output_dir: str
    p1_raw_clip_finalizer_enabled: bool
    p1_sink_stability_checks: int
    poll_interval_s: int
    default_pre_seconds: float
    evidence_max_duration_slack_sec: float


def load_config() -> Config:
    return Config(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://video:video@postgres:5432/video_analytics",
        ),
        sink_output_dir=os.getenv(
            "SINK_OUTPUT_DIR", "/media/replay-sink-output"
        ),
        snapshot_output_dir=os.getenv(
            "SNAPSHOT_OUTPUT_DIR", "/media/snapshots"
        ),
        annotated_output_dir=os.getenv(
            "ANNOTATED_OUTPUT_DIR", "/media/snapshots/annotated"
        ),
        evidence_output_dir=os.getenv("EVIDENCE_OUTPUT_DIR", "/media/evidence"),
        p1_raw_clip_finalizer_enabled=os.getenv(
            "P1_RAW_CLIP_FINALIZER_ENABLED", "false"
        ).lower() in ("1", "true", "yes"),
        p1_sink_stability_checks=int(os.getenv("P1_SINK_STABILITY_CHECKS", "2")),
        poll_interval_s=int(os.getenv("MEDIA_POLL_INTERVAL_S", "10")),
        default_pre_seconds=float(os.getenv("DEFAULT_PRE_SECONDS", "5")),
        evidence_max_duration_slack_sec=float(
            os.getenv("EVIDENCE_MAX_DURATION_SLACK_SEC", "10")
        ),
    )
