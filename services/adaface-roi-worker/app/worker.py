"""Redis consumer-group batch worker for aligned AdaFace crops."""

from __future__ import annotations

import hashlib
import json
import logging
import signal
import time
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
from prometheus_client import Counter, Gauge, Histogram
from redis import Redis

from app.config import Config
from app.contracts import build_face_observation, observation_redis_fields


LOGGER = logging.getLogger("adaface_roi_worker")
STOP_REQUESTED = False

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


@dataclass(frozen=True)
class RoiMessage:
    message_id: str
    metadata: dict[str, Any]
    image: np.ndarray
    jpeg_bytes: bytes


def ensure_group(redis: Redis, cfg: Config) -> None:
    try:
        redis.xgroup_create(
            cfg.roi_stream, cfg.roi_consumer_group, id="0", mkstream=True
        )
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def decode_message(message_id: bytes | str, fields: dict[bytes, bytes]) -> RoiMessage:
    raw_meta = fields.get(b"metadata")
    raw_image = fields.get(b"image")
    if not raw_meta or not raw_image:
        raise ValueError("missing_metadata_or_image")
    metadata = json.loads(raw_meta)
    image = cv2.imdecode(np.frombuffer(raw_image, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape != (112, 112, 3):
        raise ValueError(f"invalid_jpeg_shape:{getattr(image, 'shape', None)}")
    msg_id = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
    return RoiMessage(msg_id, metadata, image, bytes(raw_image))


def thumbnail_redis_key(source_observation_id: str) -> str:
    digest = hashlib.sha256(source_observation_id.encode("utf-8")).hexdigest()
    return f"security:face_roi_thumbnail:{digest}"


def expired(metadata: dict[str, Any], now_ms: int) -> bool:
    try:
        return int(metadata.get("expires_at_ms") or 0) <= now_ms
    except (TypeError, ValueError):
        return True


def read_batch(redis: Redis, cfg: Config) -> list[tuple[bytes, dict[bytes, bytes]]]:
    first = redis.xreadgroup(
        cfg.roi_consumer_group,
        cfg.roi_consumer_name,
        {cfg.roi_stream: ">"},
        count=cfg.batch_size,
        block=cfg.poll_timeout_ms,
    )
    if not first:
        return []
    messages: list[tuple[bytes, dict[bytes, bytes]]] = list(first[0][1])
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


def reclaim_stale(redis: Redis, cfg: Config) -> list[tuple[bytes, dict[bytes, bytes]]]:
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


def process_entries(redis: Redis, cfg: Config, runner, entries) -> None:
    now_ms = int(time.time() * 1000)
    decoded: list[RoiMessage] = []
    for message_id, fields in entries:
        msg_id = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
        try:
            message = decode_message(message_id, fields)
        except Exception:
            LOGGER.exception("invalid ROI message id=%s", msg_id)
            acknowledge_and_delete(redis, cfg, msg_id)
            MESSAGES.labels("invalid").inc()
            continue
        if expired(message.metadata, now_ms):
            acknowledge_and_delete(redis, cfg, msg_id)
            MESSAGES.labels("expired").inc()
            continue
        decoded.append(message)

    if not decoded:
        return
    started = time.perf_counter()
    features = runner.infer([message.image for message in decoded])
    INFERENCE_SECONDS.observe(time.perf_counter() - started)
    BATCH_SIZE.observe(len(decoded))
    BATCHES.labels(str(len(decoded))).inc()
    for message, feature in zip(decoded, features, strict=True):
        embedding = [float(value) for value in feature.tolist()]
        thumbnail_key = thumbnail_redis_key(
            str(message.metadata["source_observation_id"])
        )
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


def run(cfg: Config, runner) -> None:
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
            entries = []
            now = time.monotonic()
            if now - last_reclaim >= 1.0:
                entries = reclaim_stale(redis, cfg)
                last_reclaim = now
            if not entries:
                entries = read_batch(redis, cfg)
            if entries:
                process_entries(redis, cfg, runner, entries)
            pending = redis.xpending(cfg.roi_stream, cfg.roi_consumer_group)
            PENDING.set(int(pending.get("pending", 0)) if isinstance(pending, dict) else 0)
        except Exception:
            LOGGER.exception("worker loop failed")
            time.sleep(0.2)


def request_stop(_signum=None, _frame=None) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
