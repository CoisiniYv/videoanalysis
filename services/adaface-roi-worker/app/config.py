"""Runtime configuration for the ROI AdaFace worker."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    redis_url: str
    roi_stream: str
    roi_consumer_group: str
    roi_consumer_name: str
    observation_stream: str
    roi_stream_maxlen: int
    observation_stream_maxlen: int
    batch_size: int
    batch_timeout_ms: int
    poll_timeout_ms: int
    pending_idle_ms: int
    roi_ttl_ms: int
    engine_path: str
    metrics_port: int


def load_config() -> Config:
    return Config(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        roi_stream=os.getenv("FACE_ROI_STREAM", "security.face_rois"),
        roi_consumer_group=os.getenv(
            "FACE_ROI_CONSUMER_GROUP", "adaface-roi-workers"
        ),
        roi_consumer_name=os.getenv(
            "FACE_ROI_CONSUMER_NAME", f"adaface-roi-{socket.gethostname()}"
        ),
        observation_stream=os.getenv(
            "FACE_OBSERVATION_STREAM", "security.face_observations"
        ),
        roi_stream_maxlen=max(int(os.getenv("FACE_ROI_STREAM_MAXLEN", "20000")), 1),
        observation_stream_maxlen=max(
            int(os.getenv("FACE_OBSERVATION_MAXLEN", "10000")), 1
        ),
        batch_size=max(int(os.getenv("FACE_EMBEDDING_BATCH_SIZE", "16")), 1),
        batch_timeout_ms=max(int(os.getenv("FACE_ROI_BATCH_TIMEOUT_MS", "10")), 1),
        poll_timeout_ms=max(int(os.getenv("FACE_ROI_POLL_TIMEOUT_MS", "100")), 1),
        pending_idle_ms=max(int(os.getenv("FACE_ROI_PENDING_IDLE_MS", "5000")), 1),
        roi_ttl_ms=max(int(os.getenv("FACE_ROI_TTL_MS", "5000")), 1),
        engine_path=os.getenv(
            "ADAFACE_ENGINE_PATH",
            "/models/adaface/adaface_ir50_webface4m.onnx_b16_gpu0_fp16.engine",
        ),
        metrics_port=max(int(os.getenv("METRICS_PORT", "8080")), 1),
    )
