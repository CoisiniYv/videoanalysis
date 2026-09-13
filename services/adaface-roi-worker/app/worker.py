"""Redis consumer-group batch worker for aligned AdaFace crops.

The worker deliberately keeps transport, inference, publication, and cleanup as
separate steps. Redis Streams provide at-least-once delivery; a message is only
acknowledged after its face observation has been published.
"""

from __future__ import annotations

import hashlib
import json
import logging
import signal
import time
from dataclasses import dataclass
from typing import Any, Iterable, Protocol, TypeAlias

import cv2
import numpy as np
from prometheus_client import Counter, Gauge, Histogram
from redis import Redis

from app.config import Config
from app.contracts import build_face_observation, observation_redis_fields


LOGGER = logging.getLogger("adaface_roi_worker")
STOP_REQUESTED = False

StreamFields: TypeAlias = dict[bytes, bytes]
StreamEntry: TypeAlias = tuple[bytes | str, StreamFields]

MESSAGES = Counter("va_adaface_roi_messages_total", "ROI message outcomes", ["outcome"])
STREAM_CLEANUP = Counter(
    "va_adaface_roi_stream_cleanup_total",
    "Acknowledged ROI stream entry cleanup outcomes",
    ["outcome"],
)
BATCHES = Counter("va_adaface_roi_batches_total", "AdaFace batches", ["size"])
BATCH_SIZE = Histogram(
    "va_adaface_roi_batch_size", "AdaFace batch occupancy", buckets=tuple(range(1, 18))
)
INFERENCE_SECONDS = Histogram(
    "va_adaface_roi_inference_seconds",
    "AdaFace TensorRT batch wall time",
    buckets=(0.005, 0.01, 0.02, 0.04, 0.08, 0.12, 0.2, 0.4, 1.0),
)
PENDING = Gauge("va_adaface_roi_pending", "Redis consumer group pending entries")
THUMBNAILS = Counter(
    "va_adaface_roi_thumbnails_total", "Transient ROI thumbnail outcomes", ["outcome"]
)
LAST_SUCCESS_UNIX = Gauge(
    "va_adaface_roi_last_success_unixtime", "Last successful observation publish time"
)


class EmbeddingRunner(Protocol):
    """Minimal inference interface required by the Redis worker."""

    def infer(self, images: list[np.ndarray]) -> np.ndarray: ...


@dataclass(frozen=True)
class RoiMessage:
    message_id: str
    metadata: dict[str, Any]
    image: np.ndarray
    jpeg_bytes: bytes


def _message_id_text(message_id: bytes | str) -> str:
    return message_id.decode() if isinstance(message_id, bytes) else str(message_id)


def ensure_group(redis: Redis, cfg: Config) -> None:
    """Create the ROI consumer group without disturbing an existing group."""
    try:
        redis.xgroup_create(
            cfg.roi_stream, cfg.roi_consumer_group, id="0", mkstream=True
        )
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def decode_message(message_id: bytes | str, fields: StreamFields) -> RoiMessage:
    """Decode and validate one Redis ROI stream entry."""
    raw_meta = fields.get(b"metadata")
    raw_image = fields.get(b"image")
    if not raw_meta or not raw_image:
        raise ValueError("missing_metadata_or_image")

    metadata = json.loads(raw_meta)
    image = cv2.imdecode(np.frombuffer(raw_image, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape != (112, 112, 3):
        raise ValueError(f"invalid_jpeg_shape:{getattr(image, 'shape', None)}")

    return RoiMessage(
        message_id=_message_id_text(message_id),
        metadata=metadata,
        image=image,
        jpeg_bytes=bytes(raw_image),
    )


def thumbnail_redis_key(source_observation_id: str) -> str:
    digest = hashlib.sha256(source_observation_id.encode("utf-8")).hexdigest()
    return f"security:face_roi_thumbnail:{digest}"


def expired(metadata: dict[str, Any], now_ms: int) -> bool:
    try:
        return int(metadata.get("expires_at_ms") or 0) <= now_ms
    except (TypeError, ValueError):
        return True


def read_batch(redis: Redis, cfg: Config) -> list[StreamEntry]:
    """Read up to one inference batch within the configured latency budget."""
    first = redis.xreadgroup(
        cfg.roi_consumer_group,
        cfg.roi_consumer_name,
        {cfg.roi_stream: ">"},
        count=cfg.batch_size,
        block=cfg.poll_timeout_ms,
    )
    if not first:
        return []

    messages: list[StreamEntry] = list(first[0][1])
    deadline = time.monotonic() + cfg.batch_timeout_ms / 1000.0
    while len(messages) < cfg.batch_size:
        remaining_ms = max(int((deadline - time.monotonic()) * 1000), 1)
        result = redis.xreadgroup(
            cfg.roi_consumer_group,
            cfg.roi_consumer_name,
            {cfg.roi_stream: ">"},
            count=cfg.batch_size - len(messages),
            block=remaining_ms,
        )
        if result:
            messages.extend(result[0][1])
        if time.monotonic() >= deadline:
            break
    return messages


def reclaim_stale(redis: Redis, cfg: Config) -> list[StreamEntry]:
    """Reclaim messages left pending by a dead or interrupted consumer."""
    try:
        result = redis.xautoclaim(
            cfg.roi_stream,
            cfg.roi_consumer_group,
            cfg.roi_consumer_name,
            min_idle_time=cfg.pending_idle_ms,
            start_id="0-0",
            count=cfg.batch_size,
        )
    except Exception:
        LOGGER.exception("ROI pending reclaim failed")
        return []
    return list(result[1] if result and len(result) > 1 else [])


def acknowledge_and_delete(redis: Redis, cfg: Config, message_id: str) -> None:
    """Acknowledge first, then remove the short-lived JPEG from Redis."""
    try:
        redis.xack(cfg.roi_stream, cfg.roi_consumer_group, message_id)
    except Exception:
        STREAM_CLEANUP.labels("ack_error").inc()
        LOGGER.exception("ROI stream acknowledge failed id=%s", message_id)
        raise

    try:
        deleted = int(redis.xdel(cfg.roi_stream, message_id) or 0)
    except Exception:
        STREAM_CLEANUP.labels("delete_error").inc()
        LOGGER.exception("ROI stream delete failed id=%s", message_id)
        return
    STREAM_CLEANUP.labels("deleted" if deleted > 0 else "missing").inc()


def _decode_live_messages(
    redis: Redis,
    cfg: Config,
    entries: Iterable[StreamEntry],
    *,
    now_ms: int,
) -> list[RoiMessage]:
    """Decode entries and dispose of invalid or already-expired work."""
    messages: list[RoiMessage] = []
    for message_id, fields in entries:
        msg_id = _message_id_text(message_id)
        try:
            message = decode_message(message_id, fields)
        except Exception:
            LOGGER.exception("invalid ROI message id=%s", msg_id)
            acknowledge_and_delete(redis, cfg, msg_id)
            MESSAGES.labels("invalid").inc()
            continue

        if expired(message.metadata, now_ms):
            acknowledge_and_delete(redis, cfg, message.message_id)
            MESSAGES.labels("expired").inc()
            continue
        messages.append(message)
    return messages


def _infer_embeddings(
    runner: EmbeddingRunner, messages: list[RoiMessage]
) -> np.ndarray:
    """Run one TensorRT batch and record inference-only latency metrics."""
    started = time.perf_counter()
    features = runner.infer([message.image for message in messages])
    INFERENCE_SECONDS.observe(time.perf_counter() - started)
    BATCH_SIZE.observe(len(messages))
    BATCHES.labels(str(len(messages))).inc()
    return features


def _publish_observation(
    redis: Redis,
    cfg: Config,
    message: RoiMessage,
    feature: np.ndarray,
) -> None:
    """Publish one embedding, then acknowledge its source ROI entry.

    Publication intentionally precedes ACK so a crash cannot silently lose the
    observation. The surrounding stream contract therefore remains
    at-least-once and downstream persistence must continue to be idempotent.
    """
    embedding = [float(value) for value in feature.tolist()]
    source_observation_id = str(message.metadata["source_observation_id"])
    thumbnail_key = thumbnail_redis_key(source_observation_id)

    redis.set(thumbnail_key, message.jpeg_bytes, px=cfg.thumbnail_ttl_ms)
    THUMBNAILS.labels("staged").inc()

    metadata = dict(message.metadata)
    metadata["thumbnail_redis_key"] = thumbnail_key
    observation = build_face_observation(metadata, embedding)
    redis.xadd(
        cfg.observation_stream,
        observation_redis_fields(observation),
        maxlen=cfg.observation_stream_maxlen,
        approximate=True,
    )

    acknowledge_and_delete(redis, cfg, message.message_id)
    MESSAGES.labels("published").inc()
    LAST_SUCCESS_UNIX.set(time.time())


def process_entries(
    redis: Redis,
    cfg: Config,
    runner: EmbeddingRunner,
    entries: Iterable[StreamEntry],
) -> None:
    """Validate, batch-infer, publish, and retire a set of ROI messages."""
    messages = _decode_live_messages(
        redis,
        cfg,
        entries,
        now_ms=int(time.time() * 1000),
    )
    if not messages:
        return

    features = _infer_embeddings(runner, messages)
    for message, feature in zip(messages, features, strict=True):
        _publish_observation(redis, cfg, message, feature)


def _pending_count(redis: Redis, cfg: Config) -> int:
    pending = redis.xpending(cfg.roi_stream, cfg.roi_consumer_group)
    return int(pending.get("pending", 0)) if isinstance(pending, dict) else 0


def run(cfg: Config, runner: EmbeddingRunner) -> None:
    redis = Redis.from_url(cfg.redis_url, decode_responses=False)
    redis.ping()
    ensure_group(redis, cfg)
    LOGGER.info(
        "started stream=%s group=%s consumer=%s batch=%d timeout_ms=%d engine=%s",
        cfg.roi_stream,
        cfg.roi_consumer_group,
        cfg.roi_consumer_name,
        cfg.batch_size,
        cfg.batch_timeout_ms,
        cfg.engine_path,
    )

    last_reclaim = 0.0
    while not STOP_REQUESTED:
        try:
            entries: list[StreamEntry] = []
            now = time.monotonic()
            if now - last_reclaim >= 1.0:
                entries = reclaim_stale(redis, cfg)
                last_reclaim = now
            if not entries:
                entries = read_batch(redis, cfg)
            if entries:
                process_entries(redis, cfg, runner, entries)
            PENDING.set(_pending_count(redis, cfg))
        except Exception:
            LOGGER.exception("worker loop failed")
            time.sleep(0.2)


def request_stop(_signum=None, _frame=None) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
