"""Clip worker — consume record_requests, call Replay API, create jobs."""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from collections import defaultdict

import psycopg
from redis import Redis

from app.config import Config, load_config
from app.replay_client import ReplayClient
from app.repository import update_clip_status

logger = logging.getLogger(__name__)

shutdown_requested = False
MISSING_KEYFRAME_ERROR = "missing_keyframe_uuid_and_anchored_lookup_unavailable"


def _request_identity(req: dict) -> str:
    return f"{req.get('source_event_id', '')}:{req.get('strategy', '')}"


def _event_id_from_request(req: dict) -> str:
    return str(req.get("event_id", ""))


def _keyframe_from_request(req: dict) -> tuple[str | None, str]:
    previous_keyframe_uuid = req.get("previous_keyframe_uuid")
    if previous_keyframe_uuid:
        return str(previous_keyframe_uuid), "previous_keyframe_uuid"
    keyframe_uuid = req.get("keyframe_uuid")
    if keyframe_uuid:
        return str(keyframe_uuid), "keyframe_uuid"
    return None, MISSING_KEYFRAME_ERROR


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


def run_worker(
    cfg: Config, redis_client: Redis, pg_conn: psycopg.Connection
) -> None:
    stream = cfg.record_request_stream
    group = cfg.consumer_group
    consumer = cfg.consumer_name
    _ensure_group(redis_client, stream, group)
    replay = ReplayClient(cfg.replay_api_url)
    jobs_created = 0
    last_job_by_camera: dict[str, int] = defaultdict(int)
    seen_requests: set[str] = set()

    logger.info(
        "clip-worker started stream=%s group=%s replay=%s "
        "max_jobs_per_run=%s max_concurrent_jobs=%s per_camera_cooldown_seconds=%s "
        "stop_condition_mode=%s allow_unbounded_keyframe_fallback=%s",
        stream, group, cfg.replay_api_url,
        cfg.max_jobs_per_run,
        cfg.max_concurrent_jobs,
        cfg.per_camera_cooldown_seconds,
        cfg.replay_stop_condition_mode,
        cfg.allow_unbounded_keyframe_fallback,
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

                    request_id = _request_identity(req)
                    if request_id in seen_requests:
                        logger.info(
                            "clip_worker_skipped duplicate request_id=%s event_id=%s",
                            request_id,
                            req.get("event_id", ""),
                        )
                        redis_client.xack(stream, group, msg_id)
                        total_processed += 1
                        continue

                    event_id = _event_id_from_request(req)
                    source_id = req.get("source_id", "")
                    source_event_id = req.get("source_event_id", "")
                    event_ts_ms = int(req.get("event_ts_ms", 0))
                    camera_id = req.get("camera_id", "")
                    keyframe_uuid, keyframe_source = _keyframe_from_request(req)

                    if (
                        cfg.max_jobs_per_run > 0
                        and jobs_created >= cfg.max_jobs_per_run
                    ):
                        logger.info(
                            "clip_worker_skipped max_jobs_reached event_id=%s "
                            "source_event_id=%s",
                            event_id,
                            source_event_id,
                        )
                        update_clip_status(
                            pg_conn,
                            event_id,
                            "skipped_by_poc_limit",
                            error_message="CLIP_WORKER_MAX_JOBS_PER_RUN reached",
                        )
                        seen_requests.add(request_id)
                        redis_client.xack(stream, group, msg_id)
                        total_processed += 1
                        continue

                    if (
                        cfg.max_concurrent_jobs > 0
                        and jobs_created >= cfg.max_concurrent_jobs
                    ):
                        logger.info(
                            "clip_worker_skipped max_concurrent_reached event_id=%s "
                            "source_event_id=%s",
                            event_id,
                            source_event_id,
                        )
                        update_clip_status(
                            pg_conn,
                            event_id,
                            "skipped_by_poc_limit",
                            error_message="CLIP_WORKER_MAX_CONCURRENT_JOBS reached",
                        )
                        seen_requests.add(request_id)
                        redis_client.xack(stream, group, msg_id)
                        total_processed += 1
                        continue

                    if (
                        cfg.per_camera_cooldown_seconds > 0
                        and camera_id
                        and event_ts_ms - last_job_by_camera[camera_id]
                        < cfg.per_camera_cooldown_seconds * 1000
                    ):
                        logger.info(
                            "clip_worker_skipped cooldown event_id=%s source_event_id=%s "
                            "camera_id=%s",
                            event_id,
                            source_event_id,
                            camera_id,
                        )
                        update_clip_status(
                            pg_conn,
                            event_id,
                            "skipped_by_poc_limit",
                            error_message="CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS reached",
                        )
                        seen_requests.add(request_id)
                        redis_client.xack(stream, group, msg_id)
                        total_processed += 1
                        continue

                    # Find keyframe if not provided
                    if keyframe_uuid:
                        logger.info(
                            "keyframe_provided_directly request_id=%s "
                            "keyframe_uuid=%s keyframe_source=%s event_id=%s",
                            req.get("request_id"), keyframe_uuid, keyframe_source,
                            event_id,
                        )
                    else:
                        if not cfg.allow_unbounded_keyframe_fallback:
                            logger.warning(
                                "clip_worker_blocked %s request_id=%s "
                                "source_event_id=%s source_id=%s event_ts_ms=%s",
                                MISSING_KEYFRAME_ERROR,
                                req.get("request_id"),
                                source_event_id,
                                source_id,
                                event_ts_ms,
                            )
                            update_clip_status(
                                pg_conn,
                                event_id,
                                "failed",
                                error_message=MISSING_KEYFRAME_ERROR,
                            )
                            seen_requests.add(request_id)
                            redis_client.xack(stream, group, msg_id)
                            total_processed += 1
                            continue

                        if not source_id:
                            update_clip_status(
                                pg_conn, event_id, "failed",
                                error_message="missing source_id in record_request",
                            )
                            redis_client.xack(stream, group, msg_id)
                            total_processed += 1
                            continue

                        if not event_ts_ms:
                            logger.warning(
                                "missing event_ts_ms for request_id=%s source_event_id=%s",
                                req.get("request_id"),
                                source_event_id,
                            )
                            update_clip_status(
                                pg_conn, event_id, "failed",
                                error_message=(
                                    f"missing event_ts_ms in record_request "
                                    f"source_id={source_id}"
                                ),
                            )
                            redis_client.xack(stream, group, msg_id)
                            total_processed += 1
                            continue

                        # Timestamp-anchored keyframe lookup
                        keyframe_uuid = replay.find_keyframe(
                            source_id, event_ts_ms,
                            window_s=cfg.keyframe_lookup_window_s,
                        )

                    if not keyframe_uuid:
                        logger.warning(
                            "no keyframe for request_id=%s source_event_id=%s "
                            "source_id=%s event_ts_ms=%s",
                            req.get("request_id"),
                            source_event_id,
                            source_id,
                            event_ts_ms,
                        )
                        update_clip_status(
                            pg_conn, event_id, "failed",
                            error_message=(
                                f"no keyframe found for source_id={source_id}"
                                + (f" event_ts_ms={event_ts_ms}" if event_ts_ms else "")
                            ),
                        )
                        redis_client.xack(stream, group, msg_id)
                        total_processed += 1
                        continue

                    # Create replay job
                    stop_condition_mode = cfg.replay_stop_condition_mode
                    fallback_reason = None
                    if stop_condition_mode != "ts_delta_sec":
                        fallback_reason = (
                            "configured_frame_count_fallback"
                        )
                    job_id = replay.create_job(
                        source_id=source_id,
                        keyframe_uuid=keyframe_uuid,
                        pre_seconds=int(req.get("pre_seconds", cfg.default_pre_seconds)),
                        post_seconds=int(req.get("post_seconds", cfg.default_post_seconds)),
                        sink_endpoint=cfg.replay_job_sink_url,
                        labels={"event_id": event_id},
                        stop_condition_mode=stop_condition_mode,
                        fallback_reason=fallback_reason,
                    )

                    if job_id:
                        logger.info(
                            "replay_job_created job_id=%s request_id=%s event_id=%s",
                            job_id,
                            req.get("request_id"),
                            event_id,
                        )
                        update_clip_status(
                            pg_conn, event_id, "replay_job_created",
                            replay_job_id=job_id,
                            replay_job_request=replay.last_job_request,
                        )
                        jobs_created += 1
                        seen_requests.add(request_id)
                        if camera_id:
                            last_job_by_camera[camera_id] = event_ts_ms
                    else:
                        logger.error(
                            "replay_job_creation_failed request_id=%s event_id=%s",
                            req.get("request_id"),
                            event_id,
                        )
                        update_clip_status(
                            pg_conn, event_id, "failed",
                            error_message="Replay job creation returned None",
                        )

                    seen_requests.add(request_id)
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
