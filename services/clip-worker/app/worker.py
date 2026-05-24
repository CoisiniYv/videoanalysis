"""Clip worker — consume record_requests, call Replay API, create jobs."""

from __future__ import annotations

import json
import logging
import signal
import sys
import time

from redis import Redis

from app.config import Config, load_config
from app.replay_client import ReplayClient

logger = logging.getLogger(__name__)

shutdown_requested = False


def request_shutdown(signum: int, _frame: object) -> None:
    global shutdown_requested
    logger.info("shutdown requested by signal=%s", signum)
    shutdown_requested = True


def _parse_request(fields: dict[bytes, bytes]) -> dict | None:
    data_raw = fields.get(b"data")
    if not data_raw:
        return None
    try:
        return json.loads(data_raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _ensure_group(client: Redis, stream: str, group: str) -> None:
    try:
        client.xgroup_create(stream, group, id="$", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def connect_redis(cfg: Config) -> Redis:
    client = Redis.from_url(cfg.redis_url, decode_responses=False)
    client.ping()
    logger.info("connected to redis url=%s", cfg.redis_url)
    return client


def run_worker(cfg: Config, redis_client: Redis) -> None:
    stream = cfg.record_request_stream
    group = cfg.consumer_group
    consumer = cfg.consumer_name
    _ensure_group(redis_client, stream, group)
    replay = ReplayClient(cfg.replay_api_url)

    logger.info(
        "clip-worker started stream=%s group=%s replay=%s",
        stream, group, cfg.replay_api_url,
    )

    total_processed = 0
    last_report = time.monotonic()

    while not shutdown_requested:
        try:
            # Read new record requests
            result = redis_client.xreadgroup(
                group, consumer, {stream: ">"},
                count=10, block=cfg.poll_timeout_ms,
            )
            if not result:
                continue

            for _stream_name, entries in result:
                for msg_id, fields in entries:
                    req = _parse_request(fields)
                    if req is None:
                        redis_client.xack(stream, group, msg_id)
                        continue

                    source_id = req.get("source_id", "")
                    event_ts_ms = int(req.get("event_ts_ms", 0))
                    keyframe_uuid = req.get("keyframe_uuid")

                    # Find keyframe if not provided
                    if not keyframe_uuid and source_id and event_ts_ms:
                        keyframe_uuid = replay.find_keyframe(source_id, event_ts_ms)

                    if not keyframe_uuid:
                        logger.warning(
                            "no keyframe for request_id=%s source_event_id=%s",
                            req.get("request_id"),
                            req.get("source_event_id"),
                        )
                        redis_client.xack(stream, group, msg_id)
                        continue

                    # Create replay job
                    job_id = replay.create_job(
                        source_id=source_id,
                        keyframe_uuid=keyframe_uuid,
                        pre_seconds=int(req.get("pre_seconds", cfg.default_pre_seconds)),
                        post_seconds=int(req.get("post_seconds", cfg.default_post_seconds)),
                        sink_endpoint="tcp://video-file-sink:6666",
                        labels={"event_id": req.get("event_id", "")},
                    )

                    if job_id:
                        logger.info(
                            "replay_job_created job_id=%s request_id=%s event_id=%s",
                            job_id,
                            req.get("request_id"),
                            req.get("event_id"),
                        )

                    redis_client.xack(stream, group, msg_id)
                    total_processed += 1

            now = time.monotonic()
            if now - last_report >= 60:
                logger.info("clip-worker summary: total_processed=%d", total_processed)
                last_report = now

        except Exception:
            logger.exception("worker loop error, sleeping 1s")
            time.sleep(1)

    logger.info("clip-worker stopped: total_processed=%d", total_processed)
