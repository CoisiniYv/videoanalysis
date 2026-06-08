"""Clip worker — consume record_requests, call Replay API, create jobs."""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass

import psycopg
from redis import Redis

from app.config import Config, load_config
from app.replay_client import ReplayClient, _uuid7_timestamp_ms
from app.repository import update_clip_status

logger = logging.getLogger(__name__)

shutdown_requested = False
MISSING_KEYFRAME_ERROR = "missing_keyframe_uuid_and_anchored_lookup_unavailable"
_MIN_EPOCH_MS = 946684800000  # 2000-01-01T00:00:00Z
_MAX_FUTURE_SKEW_MS = 24 * 60 * 60 * 1000
REPLAY_ANCHOR_STRATEGY_EVENT_START = "event_start_keyframe"
REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME = "event_keyframe"


@dataclass(frozen=True)
class ClipGateDecision:
    allowed: bool
    reason: str = ""
    error_message: str = ""


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


def _replay_anchor_lookup_ts_ms(
    req: dict,
    *,
    pre_seconds: int,
    anchor_strategy: str,
) -> int:
    event_ts_ms = int(req.get("event_ts_ms", 0) or 0)
    if anchor_strategy in {
        REPLAY_ANCHOR_STRATEGY_EVENT_START,
        REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME,
    }:
        frame_uuid_ms = _uuid7_timestamp_ms(str(req.get("frame_uuid") or ""))
        if frame_uuid_ms is not None:
            if anchor_strategy == REPLAY_ANCHOR_STRATEGY_EVENT_START:
                logger.warning(
                    "replay_anchor_strategy_deprecated strategy=%s; using "
                    "event_keyframe semantics because Replay offset rewinds "
                    "from anchor",
                    anchor_strategy,
                )
            return frame_uuid_ms
        if event_ts_ms > 0:
            if anchor_strategy == REPLAY_ANCHOR_STRATEGY_EVENT_START:
                logger.warning(
                    "replay_anchor_strategy_deprecated strategy=%s; using "
                    "event_keyframe semantics because Replay offset rewinds "
                    "from anchor",
                    anchor_strategy,
                )
            return event_ts_ms
    return event_ts_ms


def _should_lookup_replay_anchor(anchor_strategy: str) -> bool:
    return anchor_strategy in {
        REPLAY_ANCHOR_STRATEGY_EVENT_START,
        REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME,
    }


def _replay_offset_seconds(
    *,
    replay_stop_strategy: str,
    anchor_strategy: str,
    pre_seconds: int,
) -> float | None:
    _ = (replay_stop_strategy, anchor_strategy, pre_seconds)
    return None


def _replay_job_labels(event_id: str, req: dict) -> dict[str, str]:
    """Build Replay labels, preserving explicit C2 post-Savant policy fields."""
    labels = {"event_id": event_id}
    for key in (
        "request_id",
        "source_event_id",
        "replay_source_kind",
        "evidence_topology",
        "annotation_source_policy",
        "allow_db_annotation_fallback",
        "allow_legacy_annotation_fallback",
        "frame_pts",
        "frame_num",
        "metadata_domain",
        "requested_start_pts",
        "requested_end_pts",
        "event_frame_pts",
        "replay_anchor_pts",
        "replay_anchor_keyframe",
        "replay_stop_strategy",
    ):
        value = req.get(key)
        if value is not None and value != "":
            labels[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return labels


def _cooldown_gate_ts_ms(req: dict) -> int:
    """Return a comparable timestamp for clip-worker cooldown decisions."""
    now_ms = int(time.time() * 1000)
    try:
        ts_ms = int(req.get("event_ts_ms", 0) or 0)
    except (TypeError, ValueError):
        ts_ms = 0
    if ts_ms < _MIN_EPOCH_MS or ts_ms > now_ms + _MAX_FUTURE_SKEW_MS:
        return now_ms
    return ts_ms


def _max_jobs_limit_enabled(cfg: Config) -> bool:
    """Only one-shot invocations may use CLIP_WORKER_MAX_JOBS_PER_RUN as a cap."""
    return cfg.run_once and cfg.max_jobs_per_run > 0


def _max_jobs_limit_reached(cfg: Config, jobs_created: int) -> bool:
    return _max_jobs_limit_enabled(cfg) and jobs_created >= cfg.max_jobs_per_run


def _clip_gate_decision(
    cfg: Config,
    *,
    jobs_created: int,
    active_job_count: int,
    camera_id: str,
    cooldown_gate_ts_ms: int,
    last_job_by_camera: dict[str, int],
) -> ClipGateDecision:
    if _max_jobs_limit_reached(cfg, jobs_created):
        return ClipGateDecision(
            allowed=False,
            reason="max_jobs_reached",
            error_message="CLIP_WORKER_MAX_JOBS_PER_RUN reached",
        )
    if cfg.max_concurrent_jobs > 0 and active_job_count >= cfg.max_concurrent_jobs:
        return ClipGateDecision(
            allowed=False,
            reason="max_concurrent_reached",
            error_message="CLIP_WORKER_MAX_CONCURRENT_JOBS reached",
        )
    if (
        cfg.per_camera_cooldown_seconds > 0
        and camera_id
        and cooldown_gate_ts_ms - last_job_by_camera.get(camera_id, 0)
        < cfg.per_camera_cooldown_seconds * 1000
    ):
        return ClipGateDecision(
            allowed=False,
            reason="cooldown",
            error_message="CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS reached",
        )
    return ClipGateDecision(allowed=True)


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
    active_jobs_until: list[float] = []
    last_job_by_camera: dict[str, int] = defaultdict(int)
    seen_requests: set[str] = set()

    logger.info(
        "clip-worker started stream=%s group=%s replay=%s "
        "max_jobs_per_run=%s run_once=%s max_jobs_per_run_effective=%s "
        "max_concurrent_jobs=%s per_camera_cooldown_seconds=%s "
        "stop_condition_mode=%s replay_fps=%s allow_unbounded_keyframe_fallback=%s",
        stream, group, cfg.replay_api_url,
        cfg.max_jobs_per_run,
        cfg.run_once,
        "enabled" if _max_jobs_limit_enabled(cfg) else "disabled",
        cfg.max_concurrent_jobs,
        cfg.per_camera_cooldown_seconds,
        cfg.replay_stop_condition_mode,
        cfg.replay_fps,
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
                if cfg.run_once:
                    logger.info("clip-worker run_once completed with no messages")
                    break
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
                    cooldown_gate_ts_ms = _cooldown_gate_ts_ms(req)
                    camera_id = req.get("camera_id", "")
                    pre_seconds = int(req.get("pre_seconds", cfg.default_pre_seconds))
                    post_seconds = int(req.get("post_seconds", cfg.default_post_seconds))
                    keyframe_uuid, keyframe_source = _keyframe_from_request(req)
                    if _should_lookup_replay_anchor(cfg.replay_anchor_strategy):
                        keyframe_uuid = None
                        keyframe_source = "event_start_keyframe_lookup"
                    now_monotonic = time.monotonic()
                    active_jobs_until = [
                        until for until in active_jobs_until if until > now_monotonic
                    ]

                    gate = _clip_gate_decision(
                        cfg,
                        jobs_created=jobs_created,
                        active_job_count=len(active_jobs_until),
                        camera_id=str(camera_id),
                        cooldown_gate_ts_ms=cooldown_gate_ts_ms,
                        last_job_by_camera=last_job_by_camera,
                    )
                    if not gate.allowed:
                        logger.info(
                            "clip_worker_skipped %s event_id=%s source_event_id=%s "
                            "camera_id=%s jobs_created=%s run_once=%s",
                            gate.reason,
                            event_id,
                            source_event_id,
                            camera_id,
                            jobs_created,
                            cfg.run_once,
                        )
                        update_clip_status(
                            pg_conn,
                            event_id,
                            "skipped_by_poc_limit",
                            error_message=gate.error_message,
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
                        lookup_required = _should_lookup_replay_anchor(
                            cfg.replay_anchor_strategy
                        )
                        if (
                            not lookup_required
                            and not cfg.allow_unbounded_keyframe_fallback
                        ):
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

                        # Timestamp-anchored keyframe lookup. Replay offset
                        # rewinds from the anchor keyframe, so the anchor must
                        # be near the event, not near the desired window start.
                        lookup_ts_ms = _replay_anchor_lookup_ts_ms(
                            req,
                            pre_seconds=pre_seconds,
                            anchor_strategy=cfg.replay_anchor_strategy,
                        )
                        keyframe_uuid = replay.find_keyframe(
                            source_id,
                            lookup_ts_ms,
                            window_s=max(
                                cfg.keyframe_lookup_window_s,
                                pre_seconds * 3,
                            ),
                            selection=(
                                "at_or_after"
                                if _should_lookup_replay_anchor(
                                    cfg.replay_anchor_strategy
                                )
                                else "nearest"
                            ),
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
                    replay_stop_strategy = str(req.get("replay_stop_strategy") or "")
                    offset_seconds_override = _replay_offset_seconds(
                        replay_stop_strategy=replay_stop_strategy,
                        anchor_strategy=cfg.replay_anchor_strategy,
                        pre_seconds=pre_seconds,
                    )
                    job_id = replay.create_job(
                        source_id=source_id,
                        keyframe_uuid=keyframe_uuid,
                        pre_seconds=pre_seconds,
                        post_seconds=post_seconds,
                        sink_endpoint=cfg.replay_job_sink_url,
                        labels=_replay_job_labels(event_id, req),
                        stop_condition_mode=stop_condition_mode,
                        fallback_reason=fallback_reason,
                        fps=cfg.replay_fps,
                        offset_seconds_override=offset_seconds_override,
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
                        active_jobs_until.append(
                            time.monotonic() + float(pre_seconds + post_seconds + 5)
                        )
                        seen_requests.add(request_id)
                        if camera_id:
                            last_job_by_camera[camera_id] = cooldown_gate_ts_ms
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
            if cfg.run_once:
                logger.info(
                    "clip-worker run_once completed: total_processed=%d jobs_created=%d",
                    total_processed,
                    jobs_created,
                )
                break

        except Exception:
            logger.exception("worker loop error, sleeping 1s")
            time.sleep(1)

    logger.info("clip-worker stopped: total_processed=%d", total_processed)
