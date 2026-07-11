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
    watchlist_match_enabled: bool
    watchlist_event_stream: str
    watchlist_threshold: float
    watchlist_top_k: int
    watchlist_target_external_person_ids: tuple[str, ...]
    watchlist_target_names: tuple[str, ...]
    watchlist_target_refresh_seconds: int
    face_vector_backend: str
    face_vector_small_target_threshold: int
    qdrant_url: str
    qdrant_api_key: str
    qdrant_collection: str
    qdrant_base_collection: str
    qdrant_prefer_grpc: bool
    qdrant_timeout_seconds: float
    qdrant_search_ef: int
    qdrant_candidate_multiplier: int
    qdrant_min_candidates: int
    qdrant_exact_rerank_enabled: bool
    qdrant_fallback_to_pgvector: bool
    qdrant_write_wait: bool
    qdrant_indexing_threshold_kb: int
    qdrant_full_scan_threshold_kb: int
    qdrant_default_segment_number: int
    qdrant_hnsw_m: int
    qdrant_hnsw_ef_construct: int
    qdrant_batch_query_enabled: bool = False
    qdrant_sync_poll_interval_seconds: float = 2.0
    qdrant_sync_processing_timeout_seconds: int = 300
    watchlist_event_cooldown_s: float = 60.0
    trajectory_thumbnail_root: str = "/data/video-analytics/media/face_trajectories"
    trajectory_thumbnail_max_bytes: int = 256 * 1024


def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


def _csv_env(name: str) -> tuple[str, ...]:
    value = os.getenv(name, "")
    return tuple(part.strip() for part in value.split(",") if part.strip())


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
        consumer_group=os.getenv(
            "FACE_WORKER_CONSUMER_GROUP",
            os.getenv("CONSUMER_GROUP", "face-workers"),
        ),
        consumer_name=os.getenv(
            "FACE_WORKER_CONSUMER_NAME",
            os.getenv("CONSUMER_NAME", "face-worker-1"),
        ),
        poll_timeout_ms=int(os.getenv("POLL_TIMEOUT_MS", "5000")),
        batch_size=int(os.getenv("BATCH_SIZE", "10")),
        consumer_start_id=os.getenv(
            "FACE_WORKER_CONSUMER_START_ID",
            os.getenv("FACE_OBSERVATION_CONSUMER_START_ID", "0"),
        ),
        watchlist_match_enabled=_bool_env("WATCHLIST_MATCH_ENABLED"),
        watchlist_event_stream=os.getenv("WATCHLIST_EVENT_STREAM", "security.events"),
        watchlist_threshold=float(os.getenv("WATCHLIST_THRESHOLD", "0.50")),
        watchlist_top_k=int(os.getenv("WATCHLIST_TOP_K", "5")),
        watchlist_target_external_person_ids=_csv_env(
            "WATCHLIST_TARGET_EXTERNAL_PERSON_IDS"
        ),
        watchlist_target_names=_csv_env("WATCHLIST_TARGET_NAMES"),
        watchlist_target_refresh_seconds=int(
            os.getenv("WATCHLIST_TARGET_REFRESH_SECONDS", "30")
        ),
        watchlist_event_cooldown_s=max(
            float(os.getenv("WATCHLIST_EVENT_COOLDOWN_S", "60")), 0.0
        ),
        trajectory_thumbnail_root=os.getenv(
            "FACE_TRAJECTORY_THUMBNAIL_ROOT",
            "/data/video-analytics/media/face_trajectories",
        ),
        trajectory_thumbnail_max_bytes=max(
            int(os.getenv("FACE_TRAJECTORY_THUMBNAIL_MAX_BYTES", str(256 * 1024))),
            1024,
        ),
        face_vector_backend=os.getenv("FACE_VECTOR_BACKEND", "pgvector").strip().lower(),
        face_vector_small_target_threshold=int(
            os.getenv("FACE_VECTOR_SMALL_TARGET_THRESHOLD", "5")
        ),
        qdrant_url=os.getenv("QDRANT_URL", "http://qdrant:6333"),
        qdrant_api_key=os.getenv("QDRANT_API_KEY", ""),
        qdrant_collection=os.getenv("QDRANT_COLLECTION", "face_gallery_current"),
        qdrant_base_collection=os.getenv(
            "QDRANT_BASE_COLLECTION",
            "face_gallery_adaface_512_v1",
        ),
        qdrant_prefer_grpc=_bool_env("QDRANT_PREFER_GRPC", "true"),
        qdrant_timeout_seconds=float(os.getenv("QDRANT_TIMEOUT_SECONDS", "2.0")),
        qdrant_search_ef=int(os.getenv("QDRANT_SEARCH_EF", "128")),
        qdrant_candidate_multiplier=int(os.getenv("QDRANT_CANDIDATE_MULTIPLIER", "3")),
        qdrant_min_candidates=int(os.getenv("QDRANT_MIN_CANDIDATES", "20")),
        qdrant_exact_rerank_enabled=_bool_env("QDRANT_EXACT_RERANK_ENABLED", "true"),
        qdrant_fallback_to_pgvector=_bool_env("QDRANT_FALLBACK_TO_PGVECTOR", "true"),
        qdrant_write_wait=_bool_env("QDRANT_WRITE_WAIT", "true"),
        qdrant_indexing_threshold_kb=int(
            os.getenv("QDRANT_INDEXING_THRESHOLD_KB", "1000")
        ),
        qdrant_full_scan_threshold_kb=int(
            os.getenv("QDRANT_FULL_SCAN_THRESHOLD_KB", "1000")
        ),
        qdrant_default_segment_number=int(
            os.getenv("QDRANT_DEFAULT_SEGMENT_NUMBER", "2")
        ),
        qdrant_hnsw_m=int(os.getenv("QDRANT_HNSW_M", "16")),
        qdrant_hnsw_ef_construct=int(os.getenv("QDRANT_HNSW_EF_CONSTRUCT", "100")),
        qdrant_batch_query_enabled=_bool_env("QDRANT_BATCH_QUERY_ENABLED", "false"),
        qdrant_sync_poll_interval_seconds=float(
            os.getenv("QDRANT_SYNC_POLL_INTERVAL_SECONDS", "2.0")
        ),
        qdrant_sync_processing_timeout_seconds=int(
            os.getenv("QDRANT_SYNC_PROCESSING_TIMEOUT_SECONDS", "300")
        ),
    )
