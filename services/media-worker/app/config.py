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
    midterm_sink_stability_checks: int
    poll_interval_s: int
    default_pre_seconds: float
    evidence_max_duration_slack_sec: float
    media_worker_state_path: str
    sink_scan_max_metadata_files: int
    media_probe_timeout_s: float
    media_decode_timeout_s: float
    materialization_max_active: int
    materialization_timeout_s: float
    materialization_max_backlog: int
    materialization_max_per_poll: int
    materialization_finalizer_workers: int
    materialization_throttle_sleep_s: float
    materialization_throttle_deadline_guard_s: float
    materialization_cpu_thread_limit: int
    evidence_final_root_max_bytes: int
    evidence_incoming_root_max_bytes: int
    replay_sink_output_max_bytes: int
    evidence_storage_warning_ratio: float
    evidence_storage_critical_ratio: float
    evidence_storage_hard_ratio: float
    cleanup_replay_sink_output_enabled: bool
    cleanup_replay_sink_output_statuses: tuple[str, ...]


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
        midterm_sink_stability_checks=int(
            os.getenv("MIDTERM_SINK_STABILITY_CHECKS", "2")
        ),
        poll_interval_s=int(os.getenv("MEDIA_POLL_INTERVAL_S", "10")),
        default_pre_seconds=float(os.getenv("DEFAULT_PRE_SECONDS", "5")),
        evidence_max_duration_slack_sec=float(
            os.getenv("EVIDENCE_MAX_DURATION_SLACK_SEC", "10")
        ),
        media_worker_state_path=os.getenv(
            "MEDIA_WORKER_STATE_PATH",
            "/media/replay-sink-output/midterm/.media-worker.processed.json",
        ),
        sink_scan_max_metadata_files=int(
            os.getenv("MEDIA_SINK_SCAN_MAX_METADATA_FILES", "20000")
        ),
        media_probe_timeout_s=float(os.getenv("MEDIA_PROBE_TIMEOUT_S", "30")),
        media_decode_timeout_s=float(os.getenv("MEDIA_DECODE_TIMEOUT_S", "120")),
        materialization_max_active=max(
            0,
            int(os.getenv("MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE", "1")),
        ),
        materialization_timeout_s=max(
            0.0,
            float(os.getenv("MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S", "0")),
        ),
        materialization_max_backlog=max(
            0,
            int(os.getenv("MEDIA_WORKER_MATERIALIZATION_MAX_BACKLOG", "0")),
        ),
        materialization_max_per_poll=max(
            0,
            int(os.getenv("MEDIA_WORKER_MATERIALIZATION_MAX_PER_POLL", "0")),
        ),
        materialization_finalizer_workers=max(
            1,
            int(os.getenv("MEDIA_WORKER_FINALIZER_WORKERS", "1")),
        ),
        materialization_throttle_sleep_s=max(
            0.0,
            float(os.getenv("MEDIA_WORKER_MATERIALIZATION_THROTTLE_SLEEP_S", "0")),
        ),
        materialization_throttle_deadline_guard_s=max(
            0.0,
            float(
                os.getenv(
                    "MEDIA_WORKER_MATERIALIZATION_THROTTLE_DEADLINE_GUARD_S",
                    "0",
                )
            ),
        ),
        materialization_cpu_thread_limit=max(
            0,
            int(os.getenv("MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT", "0")),
        ),
        evidence_final_root_max_bytes=max(
            0,
            int(os.getenv("EVIDENCE_FINAL_ROOT_MAX_BYTES", "0")),
        ),
        evidence_incoming_root_max_bytes=max(
            0,
            int(os.getenv("EVIDENCE_INCOMING_ROOT_MAX_BYTES", "0")),
        ),
        replay_sink_output_max_bytes=max(
            0,
            int(os.getenv("REPLAY_SINK_OUTPUT_MAX_BYTES", "0")),
        ),
        evidence_storage_warning_ratio=max(
            0.0,
            float(os.getenv("EVIDENCE_STORAGE_WARNING_RATIO", "0.80")),
        ),
        evidence_storage_critical_ratio=max(
            0.0,
            float(os.getenv("EVIDENCE_STORAGE_CRITICAL_RATIO", "0.90")),
        ),
        evidence_storage_hard_ratio=max(
            0.0,
            float(os.getenv("EVIDENCE_STORAGE_HARD_RATIO", "1.00")),
        ),
        cleanup_replay_sink_output_enabled=os.getenv(
            "MEDIA_WORKER_CLEANUP_REPLAY_SINK_OUTPUT_ENABLED", "false"
        ).lower()
        in ("1", "true", "yes"),
        cleanup_replay_sink_output_statuses=tuple(
            status.strip()
            for status in os.getenv(
                "MEDIA_WORKER_CLEANUP_REPLAY_SINK_OUTPUT_STATUSES", "ready"
            ).split(",")
            if status.strip()
        ),
    )
