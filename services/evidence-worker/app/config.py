"""Configuration for evidence-worker, loaded from environment variables.

Phase E1.1a — unified evidence directory policy.

Canonical env vars:
    MEDIA_ROOT          — /data/video-analytics/media (single mount point)
    EVIDENCE_ROOT       — /data/video-analytics/media/evidence
    EVIDENCE_EVENTS_DIR — /data/video-analytics/media/evidence/events
    VIDEO_INPUT_DIR     — /data/video-analytics/media/phase3h-savant-output

Deprecated (still accepted, normalized to EVIDENCE_EVENTS_DIR):
    SNAPSHOT_OUTPUT_DIR, ANNOTATED_OUTPUT_DIR, CLIP_OUTPUT_DIR
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    database_url: str = "postgresql://video:video@postgres:5432/video_analytics"
    video_input_dir: str = "/media/phase3h-savant-output"
    evidence_events_dir: str = "/media/evidence/events"
    pre_seconds: float = 3.0
    post_seconds: float = 3.0
    fps: float = 30.0
    evidence_max_events: int = 5


def _normalize_events_dir(raw: str) -> str:
    """Normalize legacy SNAPSHOT/ANNOTATED/CLIP dirs to events dir."""
    if raw.endswith("/snapshots") or raw.endswith("/annotated") or raw.endswith("/clips"):
        return os.path.dirname(os.path.dirname(raw))  # strip two levels
    return raw


def load_config() -> Config:
    snapshot_dir = os.getenv("SNAPSHOT_OUTPUT_DIR", "")
    annotated_dir = os.getenv("ANNOTATED_OUTPUT_DIR", "")
    clip_dir = os.getenv("CLIP_OUTPUT_DIR", "")

    evidence_events_dir = os.getenv(
        "EVIDENCE_EVENTS_DIR",
        os.getenv("EVIDENCE_ROOT", "/media/evidence") + "/events",
    )

    # Normalize legacy vars if set (they override the canonical var)
    for legacy in [snapshot_dir, annotated_dir, clip_dir]:
        if legacy:
            evidence_events_dir = _normalize_events_dir(legacy)
            break

    return Config(
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql://video:video@postgres:5432/video_analytics",
        ),
        video_input_dir=os.getenv(
            "VIDEO_INPUT_DIR", "/media/phase3h-savant-output"
        ),
        evidence_events_dir=evidence_events_dir,
        pre_seconds=float(os.getenv("PRE_SECONDS", "3")),
        post_seconds=float(os.getenv("POST_SECONDS", "3")),
        fps=float(os.getenv("FPS", "30")),
        evidence_max_events=int(os.getenv("EVIDENCE_MAX_EVENTS", "5")),
    )
