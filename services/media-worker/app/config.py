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
    materialization_finalizer_max_per_source_per_poll: int
    materialization_finalizer_source_serial: bool
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
    rolling_cache_enabled: bool
    rolling_cache_materialization_enabled: bool
    rolling_cache_root: str
    rolling_cache_materialized_root: str
    rolling_cache_retention_seconds: int
    rolling_cache_segment_seconds: int
    rolling_cache_sources: tuple[str, ...]
    rolling_cache_fallback_to_replay: bool
    rolling_cache_materialization_max_per_poll: int
    rolling_cache_materialization_workers: int
    rolling_cache_materialization_poll_interval_s: float
    rolling_cache_materialization_ready_segment_grace_seconds: float
    rolling_cache_materialization_processing_deadline_seconds: float
    cleanup_recovery_poll_interval_s: float = 30.0
    materialization_image_workers: int = 1
    materialization_image_queue_capacity: int = 0
    materialization_remux_queue_capacity: int = 0
    materialization_finalizer_queue_capacity: int = 0
    materialization_source_limit: int = 1
    materialization_reserved_non_image: int = 1
    media_worker_db_pool_enabled: bool = False
    media_worker_db_pool_timeout_s: float = 5.0
    media_worker_scheduler_v2_enabled: bool = False
    media_worker_segment_index_enabled: bool = False
    media_worker_segment_index_refresh_interval_s: float = 0.5
    media_worker_segment_index_reconcile_interval_s: float = 30.0
    media_worker_segment_index_stability_age_s: float = 0.25
    media_worker_segment_index_row_cache_entries: int = 256
    media_worker_segment_index_row_cache_max_bytes: int = 256 * 1024 * 1024
    media_worker_segment_index_max_catalogs: int = 256
    rolling_cache_read_pin_ttl_s: float = 600.0
    media_worker_shutdown_grace_s: float = 45.0
    media_worker_shutdown_kill_timeout_s: float = 5.0
    materialization_lease_heartbeat_interval_s: float = 10.0
    materialization_max_attempt_age_s: float = 300.0
    # Number of spawn-based worker processes used for CPU-heavy evidence
    # bundle/sidecar construction. Zero preserves the legacy in-process path.
    materialization_finalizer_process_workers: int = 0


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
        materialization_finalizer_process_workers=max(
            0,
            int(os.getenv("MEDIA_WORKER_FINALIZER_PROCESS_WORKERS", "0")),
        ),
        materialization_finalizer_max_per_source_per_poll=max(
            0,
            int(os.getenv("MEDIA_WORKER_FINALIZER_MAX_PER_SOURCE_PER_POLL", "1")),
        ),
        materialization_finalizer_source_serial=os.getenv(
            "MEDIA_WORKER_FINALIZER_SOURCE_SERIAL", "true"
        )
        .strip()
        .lower()
        in ("true", "1", "yes"),
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
        cleanup_recovery_poll_interval_s=max(
            1.0,
            float(
                os.getenv(
                    "MEDIA_WORKER_CLEANUP_RECOVERY_POLL_INTERVAL_S",
                    "30",
                )
            ),
        ),
        rolling_cache_enabled=os.getenv("ROLLING_CACHE_ENABLED", "false")
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
        rolling_cache_materialization_enabled=os.getenv(
            "ROLLING_CACHE_MATERIALIZATION_ENABLED", "false"
        )
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
        rolling_cache_root=os.getenv(
            "ROLLING_CACHE_ROOT",
            "/media/rolling-cache",
        ),
        rolling_cache_materialized_root=os.getenv(
            "ROLLING_CACHE_MATERIALIZED_ROOT",
            "/media/rolling-cache-materialized",
        ),
        rolling_cache_retention_seconds=max(
            0,
            int(os.getenv("ROLLING_CACHE_RETENTION_SECONDS", "300")),
        ),
        rolling_cache_segment_seconds=max(
            1,
            int(os.getenv("ROLLING_CACHE_SEGMENT_SECONDS", "4")),
        ),
        rolling_cache_sources=tuple(
            source.strip()
            for source in os.getenv("ROLLING_CACHE_SOURCES", "").split(",")
            if source.strip()
        ),
        rolling_cache_fallback_to_replay=os.getenv(
            "ROLLING_CACHE_FALLBACK_TO_REPLAY", "true"
        )
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
        rolling_cache_materialization_max_per_poll=max(
            1,
            int(os.getenv("ROLLING_CACHE_MATERIALIZATION_MAX_PER_POLL", "16")),
        ),
        rolling_cache_materialization_workers=max(
            1,
            int(os.getenv("ROLLING_CACHE_MATERIALIZATION_WORKERS", "1")),
        ),
        rolling_cache_materialization_poll_interval_s=max(
            0.1,
            float(os.getenv("ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S", "1.0")),
        ),
        rolling_cache_materialization_ready_segment_grace_seconds=max(
            0.0,
            float(
                os.getenv(
                    "ROLLING_CACHE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS",
                    "1.0",
                )
            ),
        ),
        rolling_cache_materialization_processing_deadline_seconds=max(
            1.0,
            float(
                os.getenv(
                    "ROLLING_CACHE_MATERIALIZATION_PROCESSING_DEADLINE_SECONDS",
                    "120.0",
                )
            ),
        ),
        materialization_image_workers=max(
            1,
            int(os.getenv("MEDIA_WORKER_IMAGE_WORKERS", "1")),
        ),
        materialization_image_queue_capacity=max(
            0,
            int(os.getenv("MEDIA_WORKER_IMAGE_QUEUE_CAPACITY", "0")),
        ),
        materialization_remux_queue_capacity=max(
            0,
            int(os.getenv("MEDIA_WORKER_REMUX_QUEUE_CAPACITY", "0")),
        ),
        materialization_finalizer_queue_capacity=max(
            0,
            int(os.getenv("MEDIA_WORKER_FINALIZER_QUEUE_CAPACITY", "0")),
        ),
        materialization_source_limit=max(
            1,
            int(os.getenv("MEDIA_WORKER_SOURCE_MAX_ACTIVE", "1")),
        ),
        materialization_reserved_non_image=max(
            0,
            int(os.getenv("MEDIA_WORKER_RESERVED_NON_IMAGE", "1")),
        ),
        media_worker_db_pool_enabled=os.getenv(
            "MEDIA_WORKER_DB_POOL_ENABLED", "false"
        )
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
        media_worker_db_pool_timeout_s=max(
            0.05,
            float(os.getenv("MEDIA_WORKER_DB_POOL_TIMEOUT_S", "5")),
        ),
        media_worker_scheduler_v2_enabled=os.getenv(
            "MEDIA_WORKER_SCHEDULER_V2_ENABLED", "false"
        )
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
        media_worker_segment_index_enabled=os.getenv(
            "MEDIA_WORKER_SEGMENT_INDEX_ENABLED", "false"
        )
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
        media_worker_segment_index_refresh_interval_s=max(
            0.0,
            float(
                os.getenv(
                    "MEDIA_WORKER_SEGMENT_INDEX_REFRESH_INTERVAL_S",
                    "0.5",
                )
            ),
        ),
        media_worker_segment_index_reconcile_interval_s=max(
            1.0,
            float(
                os.getenv(
                    "MEDIA_WORKER_SEGMENT_INDEX_RECONCILE_INTERVAL_S",
                    "30.0",
                )
            ),
        ),
        media_worker_segment_index_stability_age_s=max(
            0.0,
            float(
                os.getenv(
                    "MEDIA_WORKER_SEGMENT_INDEX_STABILITY_AGE_S",
                    "0.25",
                )
            ),
        ),
        media_worker_segment_index_row_cache_entries=max(
            1,
            int(
                os.getenv(
                    "MEDIA_WORKER_SEGMENT_INDEX_ROW_CACHE_ENTRIES",
                    "256",
                )
            ),
        ),
        media_worker_segment_index_row_cache_max_bytes=max(
            1,
            int(
                os.getenv(
                    "MEDIA_WORKER_SEGMENT_INDEX_ROW_CACHE_MAX_BYTES",
                    str(256 * 1024 * 1024),
                )
            ),
        ),
        media_worker_segment_index_max_catalogs=max(
            1,
            int(
                os.getenv(
                    "MEDIA_WORKER_SEGMENT_INDEX_MAX_CATALOGS",
                    "256",
                )
            ),
        ),
        rolling_cache_read_pin_ttl_s=max(
            1.0,
            float(os.getenv("ROLLING_CACHE_READ_PIN_TTL_SECONDS", "600.0")),
        ),
        media_worker_shutdown_grace_s=max(
            0.0,
            float(os.getenv("MEDIA_WORKER_SHUTDOWN_GRACE_S", "45")),
        ),
        media_worker_shutdown_kill_timeout_s=max(
            0.0,
            float(os.getenv("MEDIA_WORKER_SHUTDOWN_KILL_TIMEOUT_S", "5")),
        ),
        materialization_lease_heartbeat_interval_s=max(
            0.1,
            float(os.getenv("MEDIA_WORKER_LEASE_HEARTBEAT_INTERVAL_S", "10")),
        ),
        materialization_max_attempt_age_s=max(
            1.0,
            float(os.getenv("MEDIA_WORKER_MAX_ATTEMPT_AGE_S", "300")),
        ),
    )
