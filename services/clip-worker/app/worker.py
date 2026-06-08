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
PTS_TIME_BASE = 1_000_000_000


@dataclass(frozen=True)
class ClipGateDecision:
    allowed: bool
    reason: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class FrameAnnotationAnchor:
    frame_uuid: str
    frame_pts: int
    stream_id: str
    source_id: str
    camera_id: str


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
    post_seconds: int = 0,
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
            return frame_uuid_ms + int(max(post_seconds, 0) * 1000)
        if event_ts_ms > 0:
            if anchor_strategy == REPLAY_ANCHOR_STRATEGY_EVENT_START:
                logger.warning(
                    "replay_anchor_strategy_deprecated strategy=%s; using "
                    "event_keyframe semantics because Replay offset rewinds "
                    "from anchor",
                    anchor_strategy,
                )
            return event_ts_ms + int(max(post_seconds, 0) * 1000)
    return event_ts_ms


def _should_lookup_replay_anchor(anchor_strategy: str) -> bool:
    return anchor_strategy in {
        REPLAY_ANCHOR_STRATEGY_EVENT_START,
        REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME,
    }


def _replay_anchor_selection(anchor_strategy: str) -> str:
    if _should_lookup_replay_anchor(anchor_strategy):
        return "strict_at_or_after"
    return "nearest"


def _replay_offset_seconds(
    *,
    replay_stop_strategy: str,
    anchor_strategy: str,
    pre_seconds: int,
    post_seconds: int = 0,
    event_frame_uuid: str | None = None,
    keyframe_uuid: str | None = None,
    explicit_offset_seconds: float | None = None,
) -> float | None:
    if explicit_offset_seconds is not None:
        return max(float(explicit_offset_seconds), 0.0)
    event_keyframe_delta = _event_to_keyframe_delta_seconds(
        event_frame_uuid=event_frame_uuid,
        keyframe_uuid=keyframe_uuid,
    )
    _ = replay_stop_strategy
    if (
        anchor_strategy
        in {REPLAY_ANCHOR_STRATEGY_EVENT_START, REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME}
        and event_keyframe_delta is not None
    ):
        return max(float(pre_seconds) + event_keyframe_delta, float(pre_seconds))
    _ = (anchor_strategy, pre_seconds, post_seconds)
    return None


def _replay_duration_seconds(
    *,
    pre_seconds: int,
    post_seconds: int,
    offset_seconds_override: float | None,
    explicit_duration_seconds: float | None = None,
) -> float | None:
    if explicit_duration_seconds is not None:
        return max(float(explicit_duration_seconds), 0.0)
    if offset_seconds_override is None:
        return None
    return float(pre_seconds) + float(post_seconds)


def _event_to_keyframe_delta_seconds(
    *,
    event_frame_uuid: str | None,
    keyframe_uuid: str | None,
) -> float | None:
    event_ms = _uuid7_timestamp_ms(str(event_frame_uuid or ""))
    keyframe_ms = _uuid7_timestamp_ms(str(keyframe_uuid or ""))
    if event_ms is None or keyframe_ms is None:
        return None
    return max((keyframe_ms - event_ms) / 1000.0, 0.0)


def _int_or_none(value: object) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _decode_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _decode_maybe_text(value: object) -> object:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _redis_stream_id_ms(value: object) -> int | None:
    text = _decode_text(value).split("-", 1)[0]
    return _int_or_none(text)


def _fields_to_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {_decode_text(k): _decode_maybe_text(v) for k, v in value.items()}
    if isinstance(value, list):
        result: dict[str, object] = {}
        for index in range(0, len(value) - 1, 2):
            result[_decode_text(value[index])] = _decode_maybe_text(value[index + 1])
        return result
    return {}


def _message_from_frame_annotation_fields(fields: object) -> dict[str, object] | None:
    field_map = _fields_to_dict(fields)
    data = field_map.get("data")
    if data:
        try:
            payload = json.loads(_decode_text(data))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
    else:
        payload = dict(field_map)
    if payload.get("message_type") != "frame_annotation":
        return None
    payload["frame_pts"] = _int_or_none(payload.get("frame_pts"))
    payload["timestamp_ms"] = _int_or_none(payload.get("timestamp_ms"))
    return payload


def _frame_annotation_anchor_target_pts(req: dict, *, post_seconds: int) -> int | None:
    event_frame_pts = _int_or_none(req.get("event_frame_pts") or req.get("frame_pts"))
    if event_frame_pts is None:
        return None
    requested_end_pts = _int_or_none(req.get("requested_end_pts"))
    if requested_end_pts is not None and requested_end_pts >= event_frame_pts:
        return requested_end_pts
    return int(event_frame_pts) + int(max(post_seconds, 0)) * PTS_TIME_BASE


def _find_frame_annotation_anchor(
    redis_client: Redis,
    *,
    stream_name: str,
    source_id: str,
    camera_id: str,
    target_pts: int,
    count: int,
    min_stream_ms: int | None = None,
    max_pts_delta_ns: int | None = None,
) -> FrameAnnotationAnchor | None:
    entries = redis_client.xrevrange(
        stream_name,
        max="+",
        min="-",
        count=max(1, int(count)),
    )
    candidates: list[FrameAnnotationAnchor] = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        stream_id = _decode_text(entry[0])
        stream_ms = _redis_stream_id_ms(stream_id)
        if (
            min_stream_ms is not None
            and stream_ms is not None
            and stream_ms < min_stream_ms
        ):
            continue
        message = _message_from_frame_annotation_fields(entry[1])
        if not isinstance(message, dict):
            continue
        if str(message.get("source_id") or "") != str(source_id):
            continue
        if str(message.get("camera_id") or "") != str(camera_id):
            continue
        frame_uuid = str(message.get("frame_uuid") or "")
        frame_pts = _int_or_none(message.get("frame_pts"))
        if not frame_uuid or frame_pts is None or frame_pts < target_pts:
            continue
        if max_pts_delta_ns is not None and frame_pts - target_pts > max_pts_delta_ns:
            continue
        candidates.append(
            FrameAnnotationAnchor(
                frame_uuid=frame_uuid,
                frame_pts=int(frame_pts),
                stream_id=stream_id,
                source_id=str(source_id),
                camera_id=str(camera_id),
            )
        )
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item.frame_pts, item.frame_uuid))


def _apply_replay_anchor_to_request(
    req: dict,
    *,
    anchor: FrameAnnotationAnchor,
    pre_seconds: int,
    post_seconds: int,
) -> dict:
    updated = dict(req)
    event_frame_pts = _int_or_none(updated.get("event_frame_pts") or updated.get("frame_pts"))
    if event_frame_pts is not None:
        updated.setdefault("event_frame_pts", event_frame_pts)
        updated.setdefault(
            "requested_start_pts",
            max(0, int(event_frame_pts) - int(pre_seconds) * PTS_TIME_BASE),
        )
        updated.setdefault(
            "requested_end_pts",
            int(event_frame_pts) + int(post_seconds) * PTS_TIME_BASE,
        )
        updated["replay_offset_seconds"] = (
            float(pre_seconds)
            + max((int(anchor.frame_pts) - int(event_frame_pts)) / PTS_TIME_BASE, 0.0)
        )
        requested_start_pts = _int_or_none(updated.get("requested_start_pts"))
        requested_end_pts = _int_or_none(updated.get("requested_end_pts"))
        if (
            requested_start_pts is not None
            and requested_end_pts is not None
            and requested_end_pts > requested_start_pts
        ):
            updated["replay_duration_seconds"] = (
                (int(requested_end_pts) - int(requested_start_pts)) / PTS_TIME_BASE
            )
        else:
            updated["replay_duration_seconds"] = float(pre_seconds + post_seconds)
    updated["replay_anchor_pts"] = anchor.frame_pts
    updated["replay_anchor_frame_uuid"] = anchor.frame_uuid
    updated["replay_anchor_frame_annotation_stream_id"] = anchor.stream_id
    updated["replay_anchor_selection_method"] = "frame_cache_pts_at_or_after"
    return updated


def _frame_annotation_anchor_min_stream_ms(
    req: dict,
    *,
    slack_seconds: float,
) -> int | None:
    frame_uuid_ms = _uuid7_timestamp_ms(str(req.get("frame_uuid") or ""))
    event_ts_ms = _int_or_none(req.get("event_ts_ms"))
    candidates = [value for value in (frame_uuid_ms, event_ts_ms) if value is not None]
    if not candidates:
        return None
    return int(max(candidates) - max(float(slack_seconds), 0.0) * 1000)


def _replay_job_labels(
    event_id: str,
    req: dict,
    *,
    replay_offset_seconds: float | None = None,
    replay_duration_seconds: float | None = None,
) -> dict[str, str]:
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
        "replay_anchor_frame_uuid",
        "replay_anchor_frame_annotation_stream_id",
        "replay_anchor_selection_method",
        "replay_anchor_keyframe",
        "replay_stop_strategy",
        "replay_duration_seconds",
    ):
        value = req.get(key)
        if value is not None and value != "":
            labels[key] = str(value).lower() if isinstance(value, bool) else str(value)
    if replay_offset_seconds is not None:
        labels["replay_offset_seconds"] = f"{float(replay_offset_seconds):.6f}"
    if replay_duration_seconds is not None:
        labels["replay_duration_seconds"] = f"{float(replay_duration_seconds):.6f}"
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
                    replay_anchor_req = req
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

                        # Timestamp-anchored keyframe lookup. Replay output
                        # ends near the anchor keyframe, so the anchor must
                        # cover the requested post-event window. Do not fall
                        # back to a pre-event keyframe when event+post has not
                        # reached Replay yet; that produces short, uncentered
                        # evidence clips.
                        target_pts = _frame_annotation_anchor_target_pts(
                            req,
                            post_seconds=post_seconds,
                        )
                        min_anchor_stream_ms = _frame_annotation_anchor_min_stream_ms(
                            req,
                            slack_seconds=(
                                cfg.frame_annotation_anchor_wall_clock_slack_s
                            ),
                        )
                        max_anchor_pts_delta_ns = int(
                            max(cfg.frame_annotation_anchor_pts_tolerance_s, 0.0)
                            * PTS_TIME_BASE
                        )
                        lookup_ts_ms = _replay_anchor_lookup_ts_ms(
                            req,
                            pre_seconds=pre_seconds,
                            post_seconds=post_seconds,
                            anchor_strategy=cfg.replay_anchor_strategy,
                        )
                        selection = _replay_anchor_selection(
                            cfg.replay_anchor_strategy
                        )
                        attempts = max(1, int(cfg.keyframe_lookup_retries) + 1)
                        for attempt in range(attempts):
                            if target_pts is not None:
                                anchor = _find_frame_annotation_anchor(
                                    redis_client,
                                    stream_name=cfg.frame_annotation_stream,
                                    source_id=str(source_id),
                                    camera_id=str(camera_id or source_id),
                                    target_pts=target_pts,
                                    count=cfg.frame_annotation_anchor_lookback_count,
                                    min_stream_ms=min_anchor_stream_ms,
                                    max_pts_delta_ns=max_anchor_pts_delta_ns,
                                )
                                if anchor is not None:
                                    replay_anchor_req = _apply_replay_anchor_to_request(
                                        req,
                                        anchor=anchor,
                                        pre_seconds=pre_seconds,
                                        post_seconds=post_seconds,
                                    )
                                    keyframe_uuid = anchor.frame_uuid
                                    keyframe_source = (
                                        "frame_annotation_pts_at_or_after"
                                    )
                                    logger.info(
                                        "frame_annotation_anchor_selected "
                                        "request_id=%s source_id=%s camera_id=%s "
                                        "target_pts=%s min_stream_ms=%s "
                                        "anchor_pts=%s anchor_uuid=%s "
                                        "stream_id=%s",
                                        req.get("request_id"),
                                        source_id,
                                        camera_id,
                                        target_pts,
                                        min_anchor_stream_ms,
                                        anchor.frame_pts,
                                        anchor.frame_uuid,
                                        anchor.stream_id,
                                    )
                                    break
                            else:
                                keyframe_uuid = replay.find_keyframe(
                                    source_id,
                                    lookup_ts_ms,
                                    window_s=max(
                                        cfg.keyframe_lookup_window_s,
                                        pre_seconds + post_seconds,
                                    ),
                                    selection=selection,
                                )
                                if keyframe_uuid:
                                    break

                            if target_pts is not None and not keyframe_uuid:
                                logger.info(
                                    "frame_annotation_anchor_waiting_for_pts_window "
                                    "request_id=%s source_id=%s camera_id=%s "
                                    "target_pts=%s min_stream_ms=%s "
                                    "max_pts_delta_ns=%s attempt=%s attempts=%s",
                                    req.get("request_id"),
                                    source_id,
                                    camera_id,
                                    target_pts,
                                    min_anchor_stream_ms,
                                    max_anchor_pts_delta_ns,
                                    attempt + 1,
                                    attempts,
                                )
                            if attempt + 1 < attempts:
                                logger.info(
                                    "replay_anchor_lookup_waiting_for_post_window "
                                    "request_id=%s source_id=%s lookup_ts_ms=%s "
                                    "target_pts=%s min_stream_ms=%s "
                                    "attempt=%s attempts=%s sleep_s=%.3f",
                                    req.get("request_id"),
                                    source_id,
                                    lookup_ts_ms,
                                    target_pts,
                                    min_anchor_stream_ms,
                                    attempt + 1,
                                    attempts,
                                    cfg.keyframe_lookup_retry_sleep_s,
                                )
                                time.sleep(max(0.0, cfg.keyframe_lookup_retry_sleep_s))

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
                        post_seconds=post_seconds,
                        event_frame_uuid=str(replay_anchor_req.get("frame_uuid") or ""),
                        keyframe_uuid=keyframe_uuid,
                        explicit_offset_seconds=_to_float(
                            replay_anchor_req.get("replay_offset_seconds")
                        ),
                    )
                    duration_seconds_override = _replay_duration_seconds(
                        pre_seconds=pre_seconds,
                        post_seconds=post_seconds,
                        offset_seconds_override=offset_seconds_override,
                        explicit_duration_seconds=_to_float(
                            replay_anchor_req.get("replay_duration_seconds")
                        ),
                    )
                    job_id = replay.create_job(
                        source_id=source_id,
                        keyframe_uuid=keyframe_uuid,
                        pre_seconds=pre_seconds,
                        post_seconds=post_seconds,
                        sink_endpoint=cfg.replay_job_sink_url,
                        labels=_replay_job_labels(
                            event_id,
                            replay_anchor_req,
                            replay_offset_seconds=offset_seconds_override,
                            replay_duration_seconds=duration_seconds_override,
                        ),
                        stop_condition_mode=stop_condition_mode,
                        fallback_reason=fallback_reason,
                        fps=cfg.replay_fps,
                        offset_seconds_override=offset_seconds_override,
                        duration_seconds_override=duration_seconds_override,
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
