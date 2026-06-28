"""Clip worker — consume record_requests, call Replay API, create jobs."""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from collections.abc import Callable
from collections import defaultdict
from dataclasses import dataclass, replace

import psycopg
from redis import Redis

from app.config import Config, load_config
from app.replay_client import ReplayClient, _uuid7_timestamp_ms
from app.replay_shards import ReplayShard, ReplayShardConfigError
from app.repository import (
    expire_materialization_deadlines,
    get_evidence_diagnostics,
    record_request_target_exists,
    terminal_evidence_state,
    update_clip_status,
)

logger = logging.getLogger(__name__)

shutdown_requested = False
MISSING_KEYFRAME_ERROR = "missing_keyframe_uuid_and_anchored_lookup_unavailable"
_MIN_EPOCH_MS = 946684800000  # 2000-01-01T00:00:00Z
_MAX_FUTURE_SKEW_MS = 24 * 60 * 60 * 1000
REPLAY_ANCHOR_STRATEGY_EVENT_START = "event_start_keyframe"
REPLAY_ANCHOR_STRATEGY_EVENT_KEYFRAME = "event_keyframe"
PTS_TIME_BASE = 1_000_000_000
POST_SAVANT_EVIDENCE_TOPOLOGIES = {"post_savant", "post_savant_replay"}
POST_SAVANT_MISSING_FRAME_TIMELINE_ERROR = "missing_post_savant_frame_pts_window"
MISSING_ANCHOR_KEYFRAME_PTS_ERROR = "missing_anchor_keyframe_pts"
ANCHOR_KEYFRAME_PTS_OUTSIDE_WINDOW_ERROR = "anchor_keyframe_pts_outside_requested_window"
EVENT_FRAME_ANCHOR_NOT_KEYFRAME_ERROR = "event_frame_anchor_not_keyframe"
LOOKUP_RETURNED_PROOF_KEYFRAME_ERROR = "keyframes_find_returned_proof_keyframe"
PRIORITY_EVENT_TYPES = {"watchlist_hit", "live_search_hit"}
FRAME_DOMAIN_SESSION_POLICY_STRICT = "strict_single_session"
FRAME_DOMAIN_SESSION_POLICY_CROSS_POST = "post_window_cross_session_pts_verified"
PRE_WINDOW_POLICY_FULL = "full_requested_window"
PRE_WINDOW_POLICY_TRUNCATED = "truncated_to_current_session"


@dataclass(frozen=True)
class ClipGateDecision:
    allowed: bool
    reason: str = ""
    error_message: str = ""
    terminal_defer: bool = False
    quota_decision: dict | None = None
    degrade_decision: dict | None = None


@dataclass(frozen=True)
class ActiveReplayJob:
    until_monotonic: float
    shard_id: str
    source_id: str
    event_type: str = ""


@dataclass(frozen=True)
class ReplayRoute:
    shard: ReplayShard
    client: ReplayClient

    def diagnostics(self) -> dict[str, str]:
        return {
            "shard_id": self.shard.shard_id,
            "replay_api_url": self.shard.replay_api_url,
            "replay_job_sink_url": self.shard.replay_job_sink_url,
        }


@dataclass(frozen=True)
class FrameAnnotationAnchor:
    frame_uuid: str
    frame_pts: int
    stream_id: str
    source_id: str
    camera_id: str
    stream_session_id: str = ""
    keyframe_uuid: str | None = None
    previous_keyframe_uuid: str | None = None
    keyframe_pts: int | None = None
    anchor_method: str = "frame_annotation"


@dataclass(frozen=True)
class ReplayFrameDomainProofs:
    start_window_frame: FrameAnnotationAnchor
    post_window_frame: FrameAnnotationAnchor
    requested_start_pts: int = 0
    effective_start_pts: int = 0
    pre_window_truncated: bool = False
    pre_window_policy: str = PRE_WINDOW_POLICY_FULL


def _request_identity(req: dict) -> str:
    return f"{req.get('source_event_id', '')}:{req.get('strategy', '')}"


def _event_id_from_request(req: dict) -> str:
    return str(req.get("event_id", ""))


def _record_request_event_type(req: dict) -> str:
    event_type = str(req.get("event_type") or "").strip()
    if event_type:
        return event_type
    source_event_id = str(req.get("source_event_id") or "").strip()
    source_event_type = source_event_id.split(":", 1)[0]
    if source_event_type in PRIORITY_EVENT_TYPES:
        return source_event_type
    return ""


def _keyframe_from_request(req: dict) -> tuple[str | None, str]:
    anchor_keyframe_uuid = req.get("anchor_keyframe_uuid")
    if anchor_keyframe_uuid:
        return str(anchor_keyframe_uuid), "anchor_keyframe_uuid"
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


def _is_post_savant_media_request(req: dict) -> bool:
    replay_source_kind = str(req.get("replay_source_kind") or "").strip()
    evidence_topology = str(req.get("evidence_topology") or "").strip()
    annotation_policy = str(req.get("annotation_source_policy") or "").strip()
    metadata_domain = str(req.get("metadata_domain") or "").strip()
    return (
        replay_source_kind == "post_savant"
        or evidence_topology in POST_SAVANT_EVIDENCE_TOPOLOGIES
        or annotation_policy == "post_savant_sink_metadata_only"
        or (
            str(req.get("strategy") or "").strip() == "savant_replay"
            and metadata_domain == "video_frame"
        )
    )


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
    # Replay ``offset.seconds`` rewinds from the anchor. The sink output starts
    # at the nearest decodable keyframe before that offset target, so the job
    # must run for offset + post to guarantee event+post coverage. The
    # media-worker then crops the final bundle to the requested PTS window.
    return float(offset_seconds_override) + float(post_seconds)


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


def _valid_epoch_ms_or_none(
    value: object,
    *,
    now_ms: int | None = None,
) -> int | None:
    ts_ms = _int_or_none(value)
    if ts_ms is None:
        return None
    compare_now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if ts_ms < _MIN_EPOCH_MS or ts_ms > compare_now_ms + _MAX_FUTURE_SKEW_MS:
        return None
    return ts_ms


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
    payload["keyframe_pts"] = _int_or_none(payload.get("keyframe_pts"))
    payload["timestamp_ms"] = _int_or_none(payload.get("timestamp_ms"))
    return payload


def _runtime_epoch_id_from_request(req: dict) -> str:
    return str(req.get("runtime_epoch_id") or "").strip()


def _stream_session_id_from_request(req: dict) -> str:
    return str(req.get("stream_session_id") or "").strip()


def _frame_annotation_matches_domain(
    message: dict[str, object],
    runtime_epoch_id: str,
    stream_session_id: str = "",
) -> bool:
    if (
        runtime_epoch_id
        and str(message.get("runtime_epoch_id") or "").strip() != runtime_epoch_id
    ):
        return False
    if (
        stream_session_id
        and str(message.get("stream_session_id") or "").strip() != stream_session_id
    ):
        return False
    return True


def _frame_annotation_anchor_target_pts(req: dict, *, post_seconds: int) -> int | None:
    event_frame_pts = _int_or_none(req.get("event_frame_pts") or req.get("frame_pts"))
    if event_frame_pts is None:
        return None
    requested_end_pts = _int_or_none(req.get("requested_end_pts"))
    if requested_end_pts is not None and requested_end_pts >= event_frame_pts:
        return requested_end_pts
    return int(event_frame_pts) + int(max(post_seconds, 0)) * PTS_TIME_BASE


def _frame_annotation_summary(
    message: dict[str, object],
    *,
    stream_id: str,
) -> dict[str, object]:
    return {
        "stream_id": stream_id,
        "source_id": str(message.get("source_id") or ""),
        "camera_id": str(message.get("camera_id") or ""),
        "frame_uuid": str(message.get("frame_uuid") or ""),
        "frame_pts": _int_or_none(message.get("frame_pts")),
        "runtime_epoch_id": str(message.get("runtime_epoch_id") or ""),
        "stream_session_id": str(message.get("stream_session_id") or ""),
    }


def _find_cross_session_post_window_candidate(
    redis_client: Redis,
    cfg: Config,
    *,
    source_id: str,
    camera_id: str,
    requested_end_pts: int | None,
    min_post_stream_ms: int | None,
    min_post_frame_uuid_ms: int | None,
    runtime_epoch_id: str,
    stream_session_id: str,
) -> dict[str, object] | None:
    if requested_end_pts is None:
        return None
    max_post_pts_delta_ns = int(
        max(cfg.frame_annotation_anchor_pts_tolerance_s, 0.0) * PTS_TIME_BASE
    )
    anchor = _find_frame_annotation_anchor(
        redis_client,
        stream_name=cfg.frame_annotation_stream,
        source_id=source_id,
        camera_id=camera_id,
        target_pts=requested_end_pts,
        count=cfg.frame_annotation_anchor_lookback_count,
        direction="at_or_after",
        min_stream_ms=min_post_stream_ms,
        max_pts_delta_ns=max_post_pts_delta_ns,
        min_frame_uuid_ms=min_post_frame_uuid_ms,
        runtime_epoch_id=runtime_epoch_id,
        stream_session_id="",
    )
    if anchor is None:
        return None
    summary = {
        "source_id": anchor.source_id,
        "camera_id": anchor.camera_id,
        "frame_uuid": anchor.frame_uuid,
        "frame_pts": anchor.frame_pts,
        "stream_id": anchor.stream_id,
        "stream_session_id": anchor.stream_session_id,
        "requested_stream_session_id": stream_session_id,
        "pts_delta_ns": int(anchor.frame_pts) - int(requested_end_pts),
    }
    if stream_session_id and anchor.stream_session_id == stream_session_id:
        summary["matches_requested_stream_session"] = True
    else:
        summary["matches_requested_stream_session"] = False
    return summary


def _post_savant_frame_proof_diagnostics(
    redis_client: Redis,
    cfg: Config,
    req: dict,
    *,
    source_id: str,
    camera_id: str,
    target_pts: int | None,
    requested_start_pts: int | None,
    requested_end_pts: int | None,
    runtime_epoch_id: str,
    stream_session_id: str,
) -> dict[str, object]:
    diagnostics: dict[str, object] = {
        "request_id": str(req.get("request_id") or ""),
        "event_id": str(req.get("event_id") or ""),
        "source_event_id": str(req.get("source_event_id") or ""),
        "source_id": str(source_id),
        "camera_id": str(camera_id or source_id),
        "target_pts": target_pts,
        "requested_start_pts": requested_start_pts,
        "requested_end_pts": requested_end_pts,
        "runtime_epoch_id": runtime_epoch_id,
        "stream_session_id": stream_session_id,
        "latest_same_source_frame_annotation": None,
        "same_source_same_session_seen": False,
        "same_source_different_session_candidate": False,
        "cross_session_post_window_candidate": None,
        "truncated_pre_window_candidate": None,
    }
    min_anchor_stream_ms = _frame_annotation_anchor_min_stream_ms(
        req,
        slack_seconds=cfg.frame_annotation_anchor_wall_clock_slack_s,
    )
    min_anchor_frame_uuid_ms = _uuid7_timestamp_ms(str(req.get("frame_uuid") or ""))
    try:
        entries = redis_client.xrevrange(
            cfg.frame_annotation_stream,
            max="+",
            min="-",
            count=max(1, int(cfg.frame_annotation_anchor_lookback_count)),
        )
    except Exception as exc:
        diagnostics["frame_annotation_lookup_error"] = str(exc)
        return diagnostics

    diagnostics["cross_session_post_window_candidate"] = (
        _find_cross_session_post_window_candidate(
            redis_client,
            cfg,
            source_id=str(source_id),
            camera_id=str(camera_id or source_id),
            requested_end_pts=requested_end_pts,
            min_post_stream_ms=min_anchor_stream_ms,
            min_post_frame_uuid_ms=min_anchor_frame_uuid_ms,
            runtime_epoch_id=runtime_epoch_id,
            stream_session_id=stream_session_id,
        )
    )
    event_frame_pts = _int_or_none(req.get("event_frame_pts") or req.get("frame_pts"))
    if requested_start_pts is not None and event_frame_pts is not None:
        min_start_stream_ms = (
            None
            if min_anchor_stream_ms is None
            else int(
                min_anchor_stream_ms
                - max(float(req.get("pre_seconds", cfg.default_pre_seconds) or 0), 0.0)
                * 1000
            )
        )
        min_start_frame_uuid_ms = (
            None
            if min_anchor_frame_uuid_ms is None
            else int(
                min_anchor_frame_uuid_ms
                - max(float(req.get("pre_seconds", cfg.default_pre_seconds) or 0), 0.0)
                * 1000
                - max(cfg.frame_annotation_anchor_wall_clock_slack_s, 0.0) * 1000
            )
        )
        truncated_start = _derive_truncated_start_window_frame(
            redis_client,
            stream_name=cfg.frame_annotation_stream,
            source_id=str(source_id),
            camera_id=str(camera_id or source_id),
            requested_start_pts=int(requested_start_pts),
            event_frame_pts=int(event_frame_pts),
            count=cfg.frame_annotation_anchor_lookback_count,
            min_stream_ms=min_start_stream_ms,
            max_keyframe_pts_delta_ns=int(
                max(float(cfg.keyframe_lookup_window_s), 0.0) * PTS_TIME_BASE
            ),
            min_frame_uuid_ms=min_start_frame_uuid_ms,
            runtime_epoch_id=runtime_epoch_id,
            stream_session_id=stream_session_id,
        )
        if truncated_start is not None:
            diagnostics["truncated_pre_window_candidate"] = {
                "source_id": truncated_start.source_id,
                "camera_id": truncated_start.camera_id,
                "frame_uuid": truncated_start.frame_uuid,
                "frame_pts": truncated_start.frame_pts,
                "stream_id": truncated_start.stream_id,
                "stream_session_id": truncated_start.stream_session_id,
                "keyframe_uuid": truncated_start.keyframe_uuid,
                "keyframe_pts": truncated_start.keyframe_pts,
                "requested_start_pts": requested_start_pts,
                "event_frame_pts": event_frame_pts,
                "effective_start_pts": (
                    truncated_start.keyframe_pts
                    if truncated_start.keyframe_pts is not None
                    else truncated_start.frame_pts
                ),
            }

    expected_camera_id = str(camera_id or source_id)
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        stream_id = _decode_text(entry[0])
        message = _message_from_frame_annotation_fields(entry[1])
        if not isinstance(message, dict):
            continue
        if str(message.get("source_id") or "") != str(source_id):
            continue
        if str(message.get("camera_id") or "") != expected_camera_id:
            continue
        message_runtime_epoch_id = str(message.get("runtime_epoch_id") or "").strip()
        message_stream_session_id = str(message.get("stream_session_id") or "").strip()
        if runtime_epoch_id and message_runtime_epoch_id != runtime_epoch_id:
            continue
        summary = _frame_annotation_summary(message, stream_id=stream_id)
        if diagnostics["latest_same_source_frame_annotation"] is None:
            diagnostics["latest_same_source_frame_annotation"] = summary
        if stream_session_id and message_stream_session_id == stream_session_id:
            diagnostics["same_source_same_session_seen"] = True
        elif stream_session_id and message_stream_session_id:
            diagnostics["same_source_different_session_candidate"] = True
            diagnostics.setdefault("latest_same_source_different_session", summary)
        if (
            diagnostics["latest_same_source_frame_annotation"] is not None
            and (
                diagnostics["same_source_same_session_seen"]
                or diagnostics["same_source_different_session_candidate"]
            )
        ):
            break
    return diagnostics


def _find_frame_annotation_anchor(
    redis_client: Redis,
    *,
    stream_name: str,
    source_id: str,
    camera_id: str,
    target_pts: int,
    count: int,
    direction: str = "at_or_after",
    min_stream_ms: int | None = None,
    max_pts_delta_ns: int | None = None,
    min_frame_uuid_ms: int | None = None,
    require_keyframe: bool = False,
    runtime_epoch_id: str = "",
    stream_session_id: str = "",
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
        if not _frame_annotation_matches_domain(
            message,
            runtime_epoch_id,
            stream_session_id,
        ):
            continue
        if str(message.get("source_id") or "") != str(source_id):
            continue
        if str(message.get("camera_id") or "") != str(camera_id):
            continue
        frame_uuid = str(message.get("frame_uuid") or "")
        frame_pts = _int_or_none(message.get("frame_pts"))
        if not frame_uuid or frame_pts is None:
            continue
        if direction == "at_or_before":
            if frame_pts > target_pts:
                continue
        elif frame_pts < target_pts:
            continue
        keyframe_uuid = str(message.get("keyframe_uuid") or "") or None
        previous_keyframe_uuid = str(message.get("previous_keyframe_uuid") or "") or None
        keyframe_pts = _int_or_none(message.get("keyframe_pts"))
        if require_keyframe:
            if not keyframe_uuid or keyframe_uuid != frame_uuid:
                continue
            if keyframe_pts is None:
                keyframe_pts = frame_pts
        frame_uuid_ms = _uuid7_timestamp_ms(frame_uuid)
        if min_frame_uuid_ms is not None:
            if frame_uuid_ms is None or frame_uuid_ms < min_frame_uuid_ms:
                continue
        if (
            max_pts_delta_ns is not None
            and abs(frame_pts - target_pts) > max_pts_delta_ns
        ):
            continue
        candidates.append(
            FrameAnnotationAnchor(
                frame_uuid=frame_uuid,
                frame_pts=int(frame_pts),
                stream_id=stream_id,
                source_id=str(source_id),
                camera_id=str(camera_id),
                stream_session_id=str(message.get("stream_session_id") or ""),
                keyframe_uuid=keyframe_uuid,
                previous_keyframe_uuid=previous_keyframe_uuid,
                keyframe_pts=keyframe_pts,
            )
        )
    if not candidates:
        return None
    if direction == "at_or_before":
        return max(candidates, key=lambda item: (item.frame_pts, item.frame_uuid))
    return min(candidates, key=lambda item: (item.frame_pts, item.frame_uuid))


def _anchor_keyframe_pts_from_request(
    req: dict,
    *,
    anchor_keyframe_uuid: str,
) -> int | None:
    request_anchor_keyframe_uuid = str(req.get("anchor_keyframe_uuid") or "")
    value = _int_or_none(req.get("anchor_keyframe_pts"))
    if (
        value is not None
        and request_anchor_keyframe_uuid
        and request_anchor_keyframe_uuid == anchor_keyframe_uuid
    ):
        return value

    if anchor_keyframe_uuid and str(req.get("keyframe_uuid") or "") == anchor_keyframe_uuid:
        value = _int_or_none(req.get("keyframe_pts"))
        if value is not None:
            return value

    if (
        anchor_keyframe_uuid
        and str(req.get("previous_keyframe_uuid") or "") == anchor_keyframe_uuid
    ):
        value = _int_or_none(req.get("previous_keyframe_pts"))
        if value is not None:
            return value
    return None


def _find_anchor_keyframe_pts(
    redis_client: Redis,
    *,
    stream_name: str,
    source_id: str,
    camera_id: str,
    anchor_keyframe_uuid: str,
    count: int,
    runtime_epoch_id: str = "",
    stream_session_id: str = "",
) -> int | None:
    """Find PTS for the exact Replay keyframe UUID without selecting a new anchor."""
    match = _find_anchor_keyframe_pts_match(
        redis_client,
        stream_name=stream_name,
        source_id=source_id,
        camera_id=camera_id,
        anchor_keyframe_uuid=anchor_keyframe_uuid,
        count=count,
        runtime_epoch_id=runtime_epoch_id,
        stream_session_id=stream_session_id,
    )
    return None if match is None else match[1]


def _find_anchor_keyframe_pts_match(
    redis_client: Redis,
    *,
    stream_name: str,
    source_id: str,
    camera_id: str,
    anchor_keyframe_uuid: str,
    count: int,
    runtime_epoch_id: str = "",
    stream_session_id: str = "",
) -> tuple[int, int] | None:
    """Return ``(match_rank, pts)`` for the exact Replay keyframe UUID."""
    if not anchor_keyframe_uuid:
        return None
    entries = redis_client.xrevrange(
        stream_name,
        max="+",
        min="-",
        count=max(1, int(count)),
    )
    candidates: list[tuple[int, int]] = []
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        message = _message_from_frame_annotation_fields(entry[1])
        if not isinstance(message, dict):
            continue
        if not _frame_annotation_matches_domain(
            message,
            runtime_epoch_id,
            stream_session_id,
        ):
            continue
        if str(message.get("source_id") or "") != str(source_id):
            continue
        if str(message.get("camera_id") or "") != str(camera_id):
            continue
        frame_uuid = str(message.get("frame_uuid") or "")
        keyframe_uuid = str(message.get("keyframe_uuid") or "")
        previous_keyframe_uuid = str(message.get("previous_keyframe_uuid") or "")
        keyframe_pts = _int_or_none(message.get("keyframe_pts"))
        frame_pts = _int_or_none(message.get("frame_pts"))
        if keyframe_pts is not None and frame_pts is not None and keyframe_pts > frame_pts:
            continue
        if frame_uuid == anchor_keyframe_uuid:
            if keyframe_uuid and keyframe_uuid != anchor_keyframe_uuid:
                continue
            pts = keyframe_pts if keyframe_pts is not None else frame_pts
            if pts is not None:
                candidates.append((0, int(pts)))
            continue
        if keyframe_uuid == anchor_keyframe_uuid:
            if keyframe_pts is not None:
                candidates.append((1, int(keyframe_pts)))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])


def _anchor_keyframe_uuid_is_proven_keyframe(
    redis_client: Redis,
    *,
    stream_name: str,
    source_id: str,
    camera_id: str,
    anchor_keyframe_uuid: str,
    count: int,
    runtime_epoch_id: str = "",
    stream_session_id: str = "",
) -> bool:
    if not anchor_keyframe_uuid:
        return False
    entries = redis_client.xrevrange(
        stream_name,
        max="+",
        min="-",
        count=max(1, int(count)),
    )
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        message = _message_from_frame_annotation_fields(entry[1])
        if not isinstance(message, dict):
            continue
        if not _frame_annotation_matches_domain(
            message,
            runtime_epoch_id,
            stream_session_id,
        ):
            continue
        if str(message.get("source_id") or "") != str(source_id):
            continue
        if str(message.get("camera_id") or "") != str(camera_id):
            continue
        frame_uuid = str(message.get("frame_uuid") or "")
        keyframe_uuid = str(message.get("keyframe_uuid") or "")
        keyframe_pts = _int_or_none(message.get("keyframe_pts"))
        frame_pts = _int_or_none(message.get("frame_pts"))
        if frame_uuid != anchor_keyframe_uuid or keyframe_uuid != anchor_keyframe_uuid:
            continue
        if keyframe_pts is None:
            return frame_pts is not None
        return frame_pts is None or keyframe_pts <= frame_pts
    return False


def _anchor_keyframe_pts_verified_for_lookup(
    *,
    anchor_keyframe_pts: int,
    requested_start_pts: int,
    requested_end_pts: int,
    max_keyframe_pts_delta_ns: int,
) -> bool:
    lower_bound = max(0, int(requested_start_pts) - int(max_keyframe_pts_delta_ns))
    return lower_bound <= int(anchor_keyframe_pts) <= int(requested_end_pts)


def _lookup_keyframe_reuses_frame_domain_proof(
    *,
    anchor_keyframe_uuid: str,
    proofs: ReplayFrameDomainProofs,
) -> str | None:
    if not anchor_keyframe_uuid:
        return None
    proof_keyframes = (
        ("post_window_frame", proofs.post_window_frame.keyframe_uuid),
        ("post_window_frame", proofs.post_window_frame.previous_keyframe_uuid),
        ("start_window_frame", proofs.start_window_frame.keyframe_uuid),
        ("start_window_frame", proofs.start_window_frame.previous_keyframe_uuid),
    )
    for proof_name, proof_keyframe_uuid in proof_keyframes:
        if proof_keyframe_uuid and str(proof_keyframe_uuid) == str(anchor_keyframe_uuid):
            return proof_name
    return None


def _fail_clip_request(
    redis_client: Redis,
    pg_conn: psycopg.Connection,
    *,
    stream: str,
    group: str,
    msg_id: object,
    event_id: str,
    request_id: str,
    error_message: str,
    seen_requests: set[str],
    evidence_state: str | None = None,
    evidence_reason: str = "",
    attempt_count: int | None = None,
    diagnostics: dict | None = None,
    replay_shard: dict | None = None,
) -> None:
    update_clip_status(
        pg_conn,
        event_id,
        "failed",
        error_message=error_message,
        evidence_state=evidence_state,
        evidence_reason=evidence_reason,
        request_id=request_id,
        attempt_count=attempt_count,
        diagnostics=diagnostics,
        replay_shard=replay_shard,
    )
    seen_requests.add(request_id)
    redis_client.xack(stream, group, msg_id)


def _message_id_text(msg_id: object) -> str:
    return _decode_text(msg_id)


def _message_age_seconds(msg_id: object) -> float | None:
    stream_ms = _redis_stream_id_ms(msg_id)
    if stream_ms is None:
        return None
    return max(0.0, time.time() - (stream_ms / 1000.0))


def _pending_delivery_counts(
    redis_client: Redis,
    *,
    stream: str,
    group: str,
    count: int,
) -> dict[str, int]:
    try:
        pending = redis_client.xpending_range(
            stream,
            group,
            min="-",
            max="+",
            count=max(1, int(count)),
        )
    except Exception as exc:
        logger.warning("clip_worker_pending_inspect_failed stream=%s group=%s error=%s", stream, group, exc)
        return {}
    counts: dict[str, int] = {}
    for item in pending or []:
        if not isinstance(item, dict):
            continue
        msg_id = (
            item.get("message_id")
            or item.get("message-id")
            or item.get("id")
        )
        deliveries = (
            item.get("times_delivered")
            or item.get("times-delivered")
            or item.get("delivery_count")
            or 1
        )
        try:
            counts[_message_id_text(msg_id)] = int(deliveries)
        except (TypeError, ValueError):
            counts[_message_id_text(msg_id)] = 1
    return counts


def _claim_pending_entries(
    redis_client: Redis,
    cfg: Config,
    *,
    stream: str,
    group: str,
    consumer: str,
) -> tuple[list[tuple[object, object]], dict[str, int]]:
    if cfg.pending_claim_count <= 0 or cfg.pending_claim_min_idle_ms < 0:
        return [], {}
    delivery_counts = _pending_delivery_counts(
        redis_client,
        stream=stream,
        group=group,
        count=cfg.pending_claim_count,
    )
    try:
        result = redis_client.xautoclaim(
            stream,
            group,
            consumer,
            cfg.pending_claim_min_idle_ms,
            start_id="0-0",
            count=cfg.pending_claim_count,
        )
        entries = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else []
    except AttributeError:
        entries = _claim_pending_entries_fallback(
            redis_client,
            cfg,
            stream=stream,
            group=group,
            consumer=consumer,
            delivery_counts=delivery_counts,
        )
    except Exception as exc:
        logger.warning(
            "clip_worker_pending_claim_failed stream=%s group=%s consumer=%s error=%s",
            stream,
            group,
            consumer,
            exc,
        )
        return [], delivery_counts
    claimed = list(entries or [])
    if claimed:
        logger.info(
            "clip_worker_pending_claimed pending_claimed=%s stream=%s group=%s "
            "consumer=%s min_idle_ms=%s",
            len(claimed),
            stream,
            group,
            consumer,
            cfg.pending_claim_min_idle_ms,
        )
    return claimed, delivery_counts


def _claim_pending_entries_fallback(
    redis_client: Redis,
    cfg: Config,
    *,
    stream: str,
    group: str,
    consumer: str,
    delivery_counts: dict[str, int],
) -> list[tuple[object, object]]:
    if not delivery_counts:
        return []
    message_ids = list(delivery_counts)[: max(1, int(cfg.pending_claim_count))]
    try:
        return list(
            redis_client.xclaim(
                stream,
                group,
                consumer,
                min_idle_time=cfg.pending_claim_min_idle_ms,
                message_ids=message_ids,
            )
            or []
        )
    except Exception as exc:
        logger.warning(
            "clip_worker_pending_claim_fallback_failed stream=%s group=%s "
            "consumer=%s error=%s",
            stream,
            group,
            consumer,
            exc,
        )
        return []


def _retry_count_for_msg(
    msg_id: object,
    pending_delivery_counts: dict[str, int],
) -> int:
    deliveries = pending_delivery_counts.get(_message_id_text(msg_id), 1)
    try:
        return max(0, int(deliveries) - 1)
    except (TypeError, ValueError):
        return 0


def _redis_stream_group_diagnostics(
    redis_client: Redis,
    *,
    stream: str,
    group: str,
) -> dict[str, object]:
    diagnostics: dict[str, object] = {"pending": None, "lag": None}
    try:
        pending = redis_client.xpending(stream, group)
        if isinstance(pending, dict):
            diagnostics["pending"] = pending.get("pending")
        elif isinstance(pending, (list, tuple)) and pending:
            diagnostics["pending"] = pending[0]
    except Exception:
        pass
    try:
        for item in redis_client.xinfo_groups(stream) or []:
            name = item.get("name") if isinstance(item, dict) else None
            if _decode_text(name) != group:
                continue
            diagnostics["lag"] = item.get("lag")
            diagnostics["pending"] = item.get("pending", diagnostics["pending"])
            break
    except Exception:
        pass
    return diagnostics


def _post_savant_anchor_error_retryable(req: dict, error_message: str) -> bool:
    if "missing_stream_session_id" in error_message:
        return False
    if not req.get("source_id"):
        return False
    if _frame_annotation_anchor_target_pts(
        req,
        post_seconds=int(req.get("post_seconds", 0) or 0),
    ) is None:
        return False
    retryable_tokens = (
        POST_SAVANT_MISSING_FRAME_TIMELINE_ERROR,
        MISSING_ANCHOR_KEYFRAME_PTS_ERROR,
        ANCHOR_KEYFRAME_PTS_OUTSIDE_WINDOW_ERROR,
        EVENT_FRAME_ANCHOR_NOT_KEYFRAME_ERROR,
        LOOKUP_RETURNED_PROOF_KEYFRAME_ERROR,
        "keyframes_find_missing_exact_anchor_annotation",
    )
    return any(token in error_message for token in retryable_tokens)


def _persisted_proof_retry_count(pg_conn: psycopg.Connection, event_id: str) -> int:
    diagnostics = get_evidence_diagnostics(pg_conn, event_id)
    value = diagnostics.get("proof_retry_count") if isinstance(diagnostics, dict) else None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _defer_clip_request(
    redis_client: Redis,
    pg_conn: psycopg.Connection,
    *,
    stream: str,
    group: str,
    msg_id: object,
    event_id: str,
    request_id: str,
    reason: str,
    error_message: str,
    retry_count: int,
    max_retries: int,
    seen_requests: set[str],
    replay_shard: dict | None = None,
    evidence_state: str = "materialization_deferred",
    quota_decision: dict | None = None,
    degrade_decision: dict | None = None,
) -> bool:
    retry_age_s = _message_age_seconds(msg_id)
    if retry_count >= max(0, int(max_retries)):
        final_error = (
            f"retry_budget_exhausted reason={reason} retries={retry_count} "
            f"error={error_message}"
        )
        logger.warning(
            "clip_worker_final_failed request_id=%s event_id=%s reason=%s "
            "retry_count=%s retry_age_s=%s final_fail_reason=%s",
            request_id,
            event_id,
            reason,
            retry_count,
            retry_age_s,
            final_error,
        )
        update_clip_status(
            pg_conn,
            event_id,
            "failed",
            error_message=final_error,
            evidence_state="materialization_failed",
            evidence_reason=final_error,
            replay_shard=replay_shard,
            quota_decision=quota_decision,
            degrade_decision=degrade_decision,
        )
        seen_requests.add(request_id)
        redis_client.xack(stream, group, msg_id)
        return True

    logger.info(
        "clip_worker_deferred request_id=%s event_id=%s reason=%s "
        "deferred_retry_count=%s retry_age_s=%s max_retries=%s error=%s",
        request_id,
        event_id,
        reason,
        retry_count + 1,
        retry_age_s,
        max_retries,
        error_message,
    )
    update_clip_status(
        pg_conn,
        event_id,
        "pending",
        error_message=(
            f"deferred_retry reason={reason} "
            f"retry_count={retry_count + 1} error={error_message}"
        ),
        evidence_state=evidence_state,
        evidence_reason=reason,
        quota_decision=quota_decision,
        degrade_decision=degrade_decision,
        replay_shard=replay_shard,
    )
    return False


def _defer_clip_request_terminal(
    redis_client: Redis,
    pg_conn: psycopg.Connection,
    *,
    stream: str,
    group: str,
    msg_id: object,
    event_id: str,
    request_id: str,
    reason: str,
    error_message: str,
    seen_requests: set[str],
    replay_shard: dict | None = None,
    quota_decision: dict | None = None,
    degrade_decision: dict | None = None,
) -> None:
    diagnostics = {
        "defer_reason": reason,
        "terminal_defer": True,
        "quota_decision": quota_decision or {},
        "degrade_decision": degrade_decision or {},
    }
    update_clip_status(
        pg_conn,
        event_id,
        "pending",
        error_message=error_message,
        evidence_state="materialization_deferred",
        evidence_reason=reason,
        request_id=request_id,
        diagnostics=diagnostics,
        replay_shard=replay_shard,
        quota_decision=quota_decision,
        degrade_decision=degrade_decision,
    )
    seen_requests.add(request_id)
    redis_client.xack(stream, group, msg_id)


def _defer_post_savant_frame_proof(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    request_id: str,
    error_message: str,
    proof_retry_count: int,
    max_retries: int,
    msg_id: object,
    diagnostics: dict[str, object],
    replay_shard: dict | None = None,
) -> bool:
    retry_age_s = _message_age_seconds(msg_id)
    if proof_retry_count >= max(0, int(max_retries)):
        return False
    next_retry_count = proof_retry_count + 1
    updated_diagnostics = dict(diagnostics)
    updated_diagnostics["proof_retry_count"] = next_retry_count
    updated_diagnostics["proof_retry_max_attempts"] = max(0, int(max_retries))
    updated_diagnostics["proof_retry_age_seconds"] = retry_age_s
    if replay_shard:
        updated_diagnostics["replay_shard"] = replay_shard
    logger.info(
        "clip_worker_deferred request_id=%s event_id=%s reason=%s "
        "proof_retry_count=%s retry_age_s=%s max_retries=%s error=%s",
        request_id,
        event_id,
        "missing_post_savant_frame_proof",
        next_retry_count,
        retry_age_s,
        max_retries,
        error_message,
    )
    update_clip_status(
        pg_conn,
        event_id,
        "pending",
        error_message=(
            "deferred_retry reason=missing_post_savant_frame_proof "
            f"retry_count={next_retry_count} error={error_message}"
        ),
        evidence_state="waiting_proof",
        evidence_reason=error_message,
        request_id=request_id,
        attempt_count=next_retry_count,
        diagnostics=updated_diagnostics,
        replay_shard=replay_shard,
    )
    return True


def _queue_clip_request(
    pg_conn: psycopg.Connection,
    *,
    event_id: str,
    request_id: str,
    reason: str,
    error_message: str,
    retry_count: int,
    msg_id: object,
    active_job_count: int,
    max_concurrent_jobs: int,
    replay_shard: dict | None = None,
    quota_decision: dict | None = None,
) -> None:
    retry_age_s = _message_age_seconds(msg_id)
    diagnostics = {
        "queue_reason": reason,
        "active_job_count": active_job_count,
        "max_concurrent_jobs": max_concurrent_jobs,
        "redis_delivery_retry_count": retry_count,
        "queued_age_seconds": retry_age_s,
        "quota_decision": quota_decision or {},
    }
    if replay_shard:
        diagnostics["replay_shard"] = replay_shard
    logger.info(
        "clip_worker_queued request_id=%s event_id=%s reason=%s "
        "active_job_count=%s max_concurrent_jobs=%s retry_count=%s "
        "queued_age_s=%s error=%s",
        request_id,
        event_id,
        reason,
        active_job_count,
        max_concurrent_jobs,
        retry_count,
        retry_age_s,
        error_message,
    )
    update_clip_status(
        pg_conn,
        event_id,
        "pending",
        error_message=error_message,
        evidence_state="materialization_deferred",
        evidence_reason=reason,
        request_id=request_id,
        attempt_count=retry_count,
        diagnostics=diagnostics,
        replay_shard=replay_shard,
        quota_decision=quota_decision,
    )


def _derive_start_window_frame_from_keyframe_reference(
    redis_client: Redis,
    *,
    stream_name: str,
    source_id: str,
    camera_id: str,
    requested_start_pts: int,
    count: int,
    min_stream_ms: int | None = None,
    max_pts_delta_ns: int | None = None,
    max_keyframe_pts_delta_ns: int | None = None,
    min_frame_uuid_ms: int | None = None,
    runtime_epoch_id: str = "",
    stream_session_id: str = "",
) -> FrameAnnotationAnchor | None:
    """Find a start-window coverage frame that references a known keyframe.

    The source row must be in the requested start PTS neighborhood and must
    already carry ``keyframe_pts`` from Savant/exporter keyframe metadata. This
    proof is recorded for coverage/crop diagnostics only; it never replaces the
    event/record_request ``anchor_keyframe_uuid``.
    """
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
        if not _frame_annotation_matches_domain(
            message,
            runtime_epoch_id,
            stream_session_id,
        ):
            continue
        if str(message.get("source_id") or "") != str(source_id):
            continue
        if str(message.get("camera_id") or "") != str(camera_id):
            continue
        frame_uuid = str(message.get("frame_uuid") or "")
        frame_pts = _int_or_none(message.get("frame_pts"))
        if not frame_uuid or frame_pts is None or frame_pts > requested_start_pts:
            continue
        keyframe_uuid = (
            str(message.get("keyframe_uuid") or "")
            or str(message.get("previous_keyframe_uuid") or "")
            or None
        )
        keyframe_pts = _int_or_none(message.get("keyframe_pts"))
        if not keyframe_uuid or keyframe_pts is None:
            continue
        if keyframe_pts > requested_start_pts:
            continue
        frame_uuid_ms = _uuid7_timestamp_ms(frame_uuid)
        if min_frame_uuid_ms is not None:
            if frame_uuid_ms is None or frame_uuid_ms < min_frame_uuid_ms:
                continue
        if (
            max_pts_delta_ns is not None
            and requested_start_pts - int(frame_pts) > max_pts_delta_ns
        ):
            continue
        if (
            max_keyframe_pts_delta_ns is not None
            and requested_start_pts - int(keyframe_pts) > max_keyframe_pts_delta_ns
        ):
            continue
        candidates.append(
            FrameAnnotationAnchor(
                frame_uuid=frame_uuid,
                frame_pts=int(frame_pts),
                stream_id=stream_id,
                source_id=str(source_id),
                camera_id=str(camera_id),
                stream_session_id=str(message.get("stream_session_id") or ""),
                keyframe_uuid=keyframe_uuid,
                previous_keyframe_uuid=keyframe_uuid,
                keyframe_pts=keyframe_pts,
                anchor_method="frame_annotation_start_window_keyframe_reference",
            )
        )
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item.frame_pts, item.stream_id))


def _derive_truncated_start_window_frame(
    redis_client: Redis,
    *,
    stream_name: str,
    source_id: str,
    camera_id: str,
    requested_start_pts: int,
    event_frame_pts: int,
    count: int,
    min_stream_ms: int | None = None,
    max_keyframe_pts_delta_ns: int | None = None,
    min_frame_uuid_ms: int | None = None,
    runtime_epoch_id: str = "",
    stream_session_id: str = "",
) -> FrameAnnotationAnchor | None:
    """Find the earliest current-session decodable point before the event."""
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
        if not _frame_annotation_matches_domain(
            message,
            runtime_epoch_id,
            stream_session_id,
        ):
            continue
        if str(message.get("source_id") or "") != str(source_id):
            continue
        if str(message.get("camera_id") or "") != str(camera_id):
            continue
        frame_uuid = str(message.get("frame_uuid") or "")
        frame_pts = _int_or_none(message.get("frame_pts"))
        if not frame_uuid or frame_pts is None:
            continue
        if int(frame_pts) > int(event_frame_pts):
            continue
        frame_uuid_ms = _uuid7_timestamp_ms(frame_uuid)
        if min_frame_uuid_ms is not None:
            if frame_uuid_ms is None or frame_uuid_ms < min_frame_uuid_ms:
                continue
        keyframe_uuid = (
            str(message.get("keyframe_uuid") or "")
            or str(message.get("previous_keyframe_uuid") or "")
            or None
        )
        keyframe_pts = _int_or_none(message.get("keyframe_pts"))
        if not keyframe_uuid:
            continue
        if keyframe_pts is None:
            keyframe_pts = int(frame_pts) if keyframe_uuid == frame_uuid else None
        if keyframe_pts is None:
            continue
        if int(keyframe_pts) > int(event_frame_pts):
            continue
        if (
            max_keyframe_pts_delta_ns is not None
            and int(event_frame_pts) - int(keyframe_pts) > max_keyframe_pts_delta_ns
        ):
            continue
        candidates.append(
            FrameAnnotationAnchor(
                frame_uuid=frame_uuid,
                frame_pts=int(frame_pts),
                stream_id=stream_id,
                source_id=str(source_id),
                camera_id=str(camera_id),
                stream_session_id=str(message.get("stream_session_id") or ""),
                keyframe_uuid=keyframe_uuid,
                previous_keyframe_uuid=keyframe_uuid,
                keyframe_pts=int(keyframe_pts),
                anchor_method="frame_annotation_truncated_start_window",
            )
        )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            item.keyframe_pts if item.keyframe_pts is not None else item.frame_pts,
            item.frame_pts,
            item.stream_id,
        ),
    )


def _find_replay_frame_domain_proofs(
    redis_client: Redis,
    *,
    stream_name: str,
    source_id: str,
    camera_id: str,
    requested_start_pts: int,
    requested_end_pts: int,
    count: int,
    min_start_stream_ms: int | None = None,
    min_post_stream_ms: int | None = None,
    max_start_pts_delta_ns: int | None = None,
    max_post_pts_delta_ns: int | None = None,
    max_keyframe_pts_delta_ns: int | None = None,
    min_start_frame_uuid_ms: int | None = None,
    min_post_frame_uuid_ms: int | None = None,
    runtime_epoch_id: str = "",
    stream_session_id: str = "",
    allow_cross_session_post_window: bool = False,
    allow_truncated_pre_window: bool = False,
    event_frame_pts: int | None = None,
) -> ReplayFrameDomainProofs | None:
    post_window_frame = _find_frame_annotation_anchor(
        redis_client,
        stream_name=stream_name,
        source_id=source_id,
        camera_id=camera_id,
        target_pts=requested_end_pts,
        count=count,
        direction="at_or_after",
        min_stream_ms=min_post_stream_ms,
        max_pts_delta_ns=max_post_pts_delta_ns,
        min_frame_uuid_ms=min_post_frame_uuid_ms,
        runtime_epoch_id=runtime_epoch_id,
        stream_session_id=stream_session_id,
    )
    if post_window_frame is None and allow_cross_session_post_window:
        post_window_frame = _find_frame_annotation_anchor(
            redis_client,
            stream_name=stream_name,
            source_id=source_id,
            camera_id=camera_id,
            target_pts=requested_end_pts,
            count=count,
            direction="at_or_after",
            min_stream_ms=min_post_stream_ms,
            max_pts_delta_ns=max_post_pts_delta_ns,
            min_frame_uuid_ms=min_post_frame_uuid_ms,
            runtime_epoch_id=runtime_epoch_id,
            stream_session_id="",
        )
        if post_window_frame is not None:
            post_window_frame = replace(
                post_window_frame,
                anchor_method="frame_annotation_cross_session_post_window_pts",
            )
    if post_window_frame is None:
        return None
    start_window_frame = _find_frame_annotation_anchor(
        redis_client,
        stream_name=stream_name,
        source_id=source_id,
        camera_id=camera_id,
        target_pts=requested_start_pts,
        count=count,
        direction="at_or_before",
        min_stream_ms=min_start_stream_ms,
        max_pts_delta_ns=max_start_pts_delta_ns,
        min_frame_uuid_ms=min_start_frame_uuid_ms,
        require_keyframe=True,
        runtime_epoch_id=runtime_epoch_id,
        stream_session_id=stream_session_id,
    )
    if start_window_frame is None:
        start_window_frame = _derive_start_window_frame_from_keyframe_reference(
            redis_client,
            stream_name=stream_name,
            source_id=source_id,
            camera_id=camera_id,
            requested_start_pts=requested_start_pts,
            count=count,
            min_stream_ms=min_start_stream_ms,
            max_pts_delta_ns=max_start_pts_delta_ns,
            max_keyframe_pts_delta_ns=max_keyframe_pts_delta_ns,
            min_frame_uuid_ms=min_start_frame_uuid_ms,
            runtime_epoch_id=runtime_epoch_id,
            stream_session_id=stream_session_id,
        )
    if start_window_frame is None:
        if not allow_truncated_pre_window or event_frame_pts is None:
            return None
        start_window_frame = _derive_truncated_start_window_frame(
            redis_client,
            stream_name=stream_name,
            source_id=source_id,
            camera_id=camera_id,
            requested_start_pts=requested_start_pts,
            event_frame_pts=event_frame_pts,
            count=count,
            min_stream_ms=min_start_stream_ms,
            max_keyframe_pts_delta_ns=max_keyframe_pts_delta_ns,
            min_frame_uuid_ms=min_start_frame_uuid_ms,
            runtime_epoch_id=runtime_epoch_id,
            stream_session_id=stream_session_id,
        )
    if start_window_frame is None:
        return None
    effective_start_pts = (
        start_window_frame.keyframe_pts
        if start_window_frame.keyframe_pts is not None
        else start_window_frame.frame_pts
    )
    pre_window_truncated = int(effective_start_pts) > int(requested_start_pts)
    return ReplayFrameDomainProofs(
        start_window_frame=start_window_frame,
        post_window_frame=post_window_frame,
        requested_start_pts=int(requested_start_pts),
        effective_start_pts=int(effective_start_pts),
        pre_window_truncated=pre_window_truncated,
        pre_window_policy=(
            PRE_WINDOW_POLICY_TRUNCATED
            if pre_window_truncated
            else PRE_WINDOW_POLICY_FULL
        ),
    )


def _apply_replay_anchor_to_request(
    req: dict,
    *,
    proofs: ReplayFrameDomainProofs,
    anchor_keyframe_uuid: str,
    anchor_keyframe_pts: int,
    anchor_keyframe_source: str,
    pre_seconds: int,
    post_seconds: int,
    replay_duration_extra_slack_s: float = 0.0,
) -> dict:
    updated = dict(req)
    start_window_frame = proofs.start_window_frame
    post_window_frame = proofs.post_window_frame
    if not anchor_keyframe_uuid or anchor_keyframe_pts is None:
        raise ValueError(MISSING_ANCHOR_KEYFRAME_PTS_ERROR)
    event_frame_uuid = (
        str(updated.get("event_frame_uuid") or updated.get("frame_uuid") or "") or None
    )
    event_frame_pts = _int_or_none(updated.get("event_frame_pts") or updated.get("frame_pts"))
    if event_frame_uuid:
        updated["event_frame_uuid"] = event_frame_uuid
    if event_frame_pts is not None:
        updated.setdefault("event_frame_pts", event_frame_pts)
        original_requested_start_pts = _int_or_none(updated.get("requested_start_pts"))
        updated.setdefault(
            "requested_start_pts",
            max(0, int(event_frame_pts) - int(pre_seconds) * PTS_TIME_BASE),
        )
        updated.setdefault(
            "requested_end_pts",
            int(event_frame_pts) + int(post_seconds) * PTS_TIME_BASE,
        )
        requested_start_pts = _int_or_none(updated.get("requested_start_pts"))
        requested_end_pts = _int_or_none(updated.get("requested_end_pts"))
        effective_start_pts = (
            int(proofs.effective_start_pts)
            if proofs.effective_start_pts
            else requested_start_pts
        )
        if (
            requested_start_pts is not None
            and effective_start_pts is not None
            and int(effective_start_pts) > int(requested_start_pts)
        ):
            updated["original_requested_start_pts"] = int(requested_start_pts)
            updated["effective_start_pts"] = int(effective_start_pts)
            updated["requested_start_pts"] = int(effective_start_pts)
            updated["pre_window_truncated"] = True
            updated["pre_window_policy"] = PRE_WINDOW_POLICY_TRUNCATED
            updated["requested_pre_window_seconds"] = max(
                (int(event_frame_pts) - int(original_requested_start_pts or requested_start_pts))
                / PTS_TIME_BASE,
                0.0,
            )
            updated["effective_pre_window_seconds"] = max(
                (int(event_frame_pts) - int(effective_start_pts)) / PTS_TIME_BASE,
                0.0,
            )
            updated["pre_window_truncated_seconds"] = max(
                (
                    int(effective_start_pts)
                    - int(original_requested_start_pts or requested_start_pts)
                )
                / PTS_TIME_BASE,
                0.0,
            )
            requested_start_pts = int(effective_start_pts)
        else:
            updated["pre_window_truncated"] = False
            updated["pre_window_policy"] = PRE_WINDOW_POLICY_FULL
            if requested_start_pts is not None:
                updated["effective_start_pts"] = int(requested_start_pts)
        if requested_start_pts is not None:
            updated["replay_offset_seconds"] = max(
                (int(anchor_keyframe_pts) - int(requested_start_pts))
                / PTS_TIME_BASE,
                0.0,
            )
        else:
            updated["replay_offset_seconds"] = 0.0
        if (
            requested_start_pts is not None
            and requested_end_pts is not None
            and requested_end_pts > requested_start_pts
        ):
            decodable_start_pts = (
                start_window_frame.keyframe_pts
                if start_window_frame.keyframe_pts is not None
                else start_window_frame.frame_pts
            )
            job_start_pts = min(
                int(anchor_keyframe_pts),
                int(requested_start_pts),
                int(decodable_start_pts),
            )
            replay_duration_base_seconds = max(
                (int(requested_end_pts) - job_start_pts)
                / PTS_TIME_BASE,
                (int(requested_end_pts) - int(requested_start_pts))
                / PTS_TIME_BASE,
            )
            replay_duration_seconds = replay_duration_base_seconds
            anchor_before_start_guard_s = 0.0
            if int(anchor_keyframe_pts) <= int(requested_start_pts):
                # Replay may emit from the decoder keyframe before the requested
                # anchor while the stop condition counts from that first emitted
                # frame. Add a guard so the emitted stream still reaches
                # requested_end_pts before media-worker crops the final bundle.
                anchor_before_start_guard_s = max(
                    float(pre_seconds + post_seconds),
                    1.0,
                ) + 1.0
                replay_duration_seconds += anchor_before_start_guard_s
            extra_slack_s = max(float(replay_duration_extra_slack_s), 0.0)
            updated["replay_duration_base_seconds"] = replay_duration_base_seconds
            updated["replay_duration_anchor_before_start_guard_used"] = (
                anchor_before_start_guard_s > 0
            )
            updated["replay_duration_anchor_before_start_guard_s"] = (
                anchor_before_start_guard_s
            )
            updated["replay_duration_before_slack_s"] = replay_duration_seconds
            if extra_slack_s:
                replay_duration_seconds += extra_slack_s
                updated["replay_duration_extra_slack_s"] = extra_slack_s
            updated["replay_duration_seconds"] = replay_duration_seconds
        else:
            extra_slack_s = max(float(replay_duration_extra_slack_s), 0.0)
            updated["replay_duration_base_seconds"] = float(pre_seconds + post_seconds)
            updated["replay_duration_anchor_before_start_guard_used"] = False
            updated["replay_duration_anchor_before_start_guard_s"] = 0.0
            updated["replay_duration_before_slack_s"] = float(pre_seconds + post_seconds)
            if extra_slack_s:
                updated["replay_duration_extra_slack_s"] = extra_slack_s
            updated["replay_duration_seconds"] = (
                float(pre_seconds + post_seconds) + extra_slack_s
            )
    updated["anchor_keyframe_uuid"] = anchor_keyframe_uuid
    updated["anchor_keyframe_pts"] = int(anchor_keyframe_pts)
    updated["anchor_keyframe_source"] = anchor_keyframe_source
    updated["evidence_anchor_strategy"] = "uuid_first_pts_verified"
    updated["start_window_frame_uuid"] = start_window_frame.frame_uuid
    updated["start_window_frame_pts"] = start_window_frame.frame_pts
    updated["start_window_stream_session_id"] = start_window_frame.stream_session_id
    updated["start_window_frame_annotation_stream_id"] = start_window_frame.stream_id
    updated["post_window_frame_uuid"] = post_window_frame.frame_uuid
    updated["post_window_frame_pts"] = post_window_frame.frame_pts
    updated["post_window_stream_session_id"] = post_window_frame.stream_session_id
    updated["post_window_frame_annotation_stream_id"] = post_window_frame.stream_id
    updated["post_window_proof_used"] = True
    updated["start_window_coverage_used"] = True
    requested_stream_session_id = str(updated.get("stream_session_id") or "")
    post_window_cross_session = bool(
        requested_stream_session_id
        and post_window_frame.stream_session_id
        and post_window_frame.stream_session_id != requested_stream_session_id
    )
    updated["post_window_cross_session_proof_used"] = post_window_cross_session
    updated["pre_window_truncated"] = bool(updated.get("pre_window_truncated", False))
    updated["pre_window_policy"] = str(
        updated.get("pre_window_policy") or PRE_WINDOW_POLICY_FULL
    )
    updated["frame_domain_session_policy"] = (
        FRAME_DOMAIN_SESSION_POLICY_CROSS_POST
        if post_window_cross_session
        else FRAME_DOMAIN_SESSION_POLICY_STRICT
    )
    if bool(updated.get("pre_window_truncated")) and post_window_cross_session:
        updated["frame_domain_proof_method"] = (
            "frame_cache_truncated_start_window_and_cross_session_post_window_pts"
        )
    elif bool(updated.get("pre_window_truncated")):
        updated["frame_domain_proof_method"] = (
            "frame_cache_truncated_start_window_keyframe_reference_and_post_window_pts"
        )
    elif post_window_cross_session:
        updated["frame_domain_proof_method"] = (
            "frame_cache_start_window_keyframe_and_cross_session_post_window_pts"
        )
    else:
        updated["frame_domain_proof_method"] = (
            "frame_cache_start_window_keyframe_and_post_window_pts"
            if start_window_frame.anchor_method == "frame_annotation"
            else "frame_cache_start_window_keyframe_reference_and_post_window_pts"
        )
    return updated


def _frame_annotation_anchor_min_stream_ms(
    req: dict,
    *,
    slack_seconds: float,
) -> int | None:
    frame_uuid_ms = _uuid7_timestamp_ms(str(req.get("frame_uuid") or ""))
    slack_ms = max(float(slack_seconds), 0.0) * 1000
    if frame_uuid_ms is not None:
        return int(frame_uuid_ms - slack_ms)
    event_ts_ms = _valid_epoch_ms_or_none(req.get("event_ts_ms"))
    if event_ts_ms is None:
        return None
    return int(event_ts_ms - slack_ms)


def _requested_pts_window(
    req: dict,
    *,
    pre_seconds: int,
    post_seconds: int,
) -> tuple[int | None, int | None]:
    event_frame_pts = _int_or_none(req.get("event_frame_pts") or req.get("frame_pts"))
    requested_start_pts = _int_or_none(req.get("requested_start_pts"))
    requested_end_pts = _int_or_none(req.get("requested_end_pts"))
    if event_frame_pts is not None:
        if requested_start_pts is None:
            requested_start_pts = max(
                0,
                int(event_frame_pts) - int(pre_seconds) * PTS_TIME_BASE,
            )
        if requested_end_pts is None:
            requested_end_pts = int(event_frame_pts) + int(post_seconds) * PTS_TIME_BASE
    return requested_start_pts, requested_end_pts


def _prepare_post_savant_replay_request(
    redis_client: Redis,
    replay: ReplayClient,
    cfg: Config,
    req: dict,
    *,
    source_id: str,
    camera_id: str,
    keyframe_uuid: str | None,
    keyframe_source: str,
    pre_seconds: int,
    post_seconds: int,
    on_wait: Callable[[int, int, str, dict[str, object]], None] | None = None,
    wait_budget_s_override: float | None = None,
    poll_interval_s_override: float | None = None,
    attempts_override: int | None = None,
) -> tuple[dict | None, str | None, str, str | None]:
    if not source_id:
        return None, keyframe_uuid, keyframe_source, "missing source_id in record_request"

    target_pts = _frame_annotation_anchor_target_pts(req, post_seconds=post_seconds)
    if target_pts is None:
        return (
            None,
            keyframe_uuid,
            keyframe_source,
            f"{POST_SAVANT_MISSING_FRAME_TIMELINE_ERROR} source_id={source_id}",
        )
    requested_start_pts, requested_end_pts = _requested_pts_window(
        req,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
    )
    if requested_start_pts is None or requested_end_pts is None:
        return (
            None,
            keyframe_uuid,
            keyframe_source,
            f"{POST_SAVANT_MISSING_FRAME_TIMELINE_ERROR} source_id={source_id}",
        )

    min_anchor_stream_ms = _frame_annotation_anchor_min_stream_ms(
        req,
        slack_seconds=cfg.frame_annotation_anchor_wall_clock_slack_s,
    )
    min_start_keyframe_stream_ms = (
        None
        if min_anchor_stream_ms is None
        else int(min_anchor_stream_ms - max(float(pre_seconds), 0.0) * 1000)
    )
    min_anchor_frame_uuid_ms = _uuid7_timestamp_ms(str(req.get("frame_uuid") or ""))
    min_start_keyframe_uuid_ms = (
        None
        if min_anchor_frame_uuid_ms is None
        else int(
            min_anchor_frame_uuid_ms
            - max(float(pre_seconds), 0.0) * 1000
            - max(cfg.frame_annotation_anchor_wall_clock_slack_s, 0.0) * 1000
        )
    )
    max_anchor_pts_delta_ns = int(
        max(cfg.frame_annotation_anchor_pts_tolerance_s, 0.0) * PTS_TIME_BASE
    )
    max_keyframe_pts_delta_ns = int(
        max(float(cfg.keyframe_lookup_window_s), 0.0) * PTS_TIME_BASE
    )
    event_frame_uuid = str(req.get("event_frame_uuid") or req.get("frame_uuid") or "")
    runtime_epoch_id = _runtime_epoch_id_from_request(req)
    stream_session_id = _stream_session_id_from_request(req)
    if not stream_session_id:
        return (
            None,
            keyframe_uuid,
            keyframe_source,
            f"missing_stream_session_id source_id={source_id}",
        )
    lookup_ts_ms = _replay_anchor_lookup_ts_ms(
        req,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
        anchor_strategy=cfg.replay_anchor_strategy,
    )
    selection = _replay_anchor_selection(cfg.replay_anchor_strategy)
    if wait_budget_s_override is None:
        wait_budget_value = getattr(cfg, "post_savant_frame_proof_wait_budget_s", 0.0)
    else:
        wait_budget_value = wait_budget_s_override
    if poll_interval_s_override is None:
        poll_interval_value = getattr(
            cfg,
            "post_savant_frame_proof_poll_interval_s",
            getattr(cfg, "post_savant_frame_proof_retry_sleep_s", 0.0),
        )
    else:
        poll_interval_value = poll_interval_s_override
    wait_budget_s = max(0.0, float(wait_budget_value or 0.0))
    poll_interval_s = max(0.0, float(poll_interval_value or 0.0))
    legacy_attempts = max(1, int(cfg.post_savant_frame_proof_attempts))
    if attempts_override is not None:
        attempts = max(1, int(attempts_override))
    elif wait_budget_s > 0.0 and poll_interval_s > 0.0:
        attempts = max(1, int(wait_budget_s / poll_interval_s) + 1)
    else:
        attempts = legacy_attempts
    wait_started_at = time.monotonic()
    wait_deadline_at = wait_started_at + wait_budget_s if wait_budget_s > 0.0 else None
    last_error = f"{POST_SAVANT_MISSING_FRAME_TIMELINE_ERROR} source_id={source_id}"
    for attempt in range(attempts):
        proofs = _find_replay_frame_domain_proofs(
            redis_client,
            stream_name=cfg.frame_annotation_stream,
            source_id=str(source_id),
            camera_id=str(camera_id or source_id),
            requested_start_pts=requested_start_pts,
            requested_end_pts=requested_end_pts,
            count=cfg.frame_annotation_anchor_lookback_count,
            min_start_stream_ms=min_start_keyframe_stream_ms,
            min_post_stream_ms=min_anchor_stream_ms,
            max_start_pts_delta_ns=(
                int(max(pre_seconds, 0) * PTS_TIME_BASE) + max_anchor_pts_delta_ns
            ),
            max_post_pts_delta_ns=max_anchor_pts_delta_ns,
            max_keyframe_pts_delta_ns=max_keyframe_pts_delta_ns,
            min_start_frame_uuid_ms=min_start_keyframe_uuid_ms,
            min_post_frame_uuid_ms=min_anchor_frame_uuid_ms,
            runtime_epoch_id=runtime_epoch_id,
            stream_session_id=stream_session_id,
            allow_cross_session_post_window=bool(
                getattr(
                    cfg,
                    "post_savant_allow_cross_session_post_window_proof",
                    False,
                )
            ),
            allow_truncated_pre_window=bool(
                getattr(
                    cfg,
                    "post_savant_allow_truncated_pre_window_proof",
                    False,
                )
            ),
            event_frame_pts=_int_or_none(
                req.get("event_frame_pts") or req.get("frame_pts")
            ),
        )
        candidate_uuid = keyframe_uuid
        candidate_source = keyframe_source
        if proofs is not None and candidate_uuid is None:
            candidate_uuid = replay.find_keyframe(
                source_id,
                lookup_ts_ms,
                window_s=max(cfg.keyframe_lookup_window_s, pre_seconds + post_seconds),
                selection=selection,
            )
            if candidate_uuid:
                candidate_source = "keyframes_find_pts_verified"
        if proofs is not None and candidate_uuid:
            candidate_uuid_text = str(candidate_uuid)
            if candidate_source == "keyframes_find_pts_verified":
                proof_name = _lookup_keyframe_reuses_frame_domain_proof(
                    anchor_keyframe_uuid=candidate_uuid_text,
                    proofs=proofs,
                )
                if proof_name is not None:
                    last_error = (
                        f"{LOOKUP_RETURNED_PROOF_KEYFRAME_ERROR} "
                        f"anchor_keyframe_uuid={candidate_uuid_text} "
                        f"proof={proof_name} source_id={source_id}"
                    )
                    logger.info(
                        "keyframes_find_proof_keyframe_rejected request_id=%s "
                        "source_id=%s anchor_keyframe_uuid=%s proof=%s",
                        req.get("request_id"),
                        source_id,
                        candidate_uuid_text,
                        proof_name,
                    )
                    continue
            if (
                event_frame_uuid
                and candidate_uuid_text == event_frame_uuid
                and not _anchor_keyframe_uuid_is_proven_keyframe(
                    redis_client,
                    stream_name=cfg.frame_annotation_stream,
                    source_id=str(source_id),
                    camera_id=str(camera_id or source_id),
                    anchor_keyframe_uuid=candidate_uuid_text,
                    count=cfg.frame_annotation_anchor_lookback_count,
                    runtime_epoch_id=runtime_epoch_id,
                    stream_session_id=stream_session_id,
                )
            ):
                last_error = (
                    f"{EVENT_FRAME_ANCHOR_NOT_KEYFRAME_ERROR} "
                    f"event_frame_uuid={event_frame_uuid} source_id={source_id}"
                )
                logger.info(
                    "event_frame_anchor_rejected request_id=%s source_id=%s "
                    "event_frame_uuid=%s candidate_source=%s",
                    req.get("request_id"),
                    source_id,
                    event_frame_uuid,
                    candidate_source,
                )
                continue
            anchor_keyframe_pts = _anchor_keyframe_pts_from_request(
                req,
                anchor_keyframe_uuid=candidate_uuid_text,
            )
            anchor_keyframe_pts_match: tuple[int, int] | None = None
            if anchor_keyframe_pts is None:
                anchor_keyframe_pts_match = _find_anchor_keyframe_pts_match(
                    redis_client,
                    stream_name=cfg.frame_annotation_stream,
                    source_id=str(source_id),
                    camera_id=str(camera_id or source_id),
                    anchor_keyframe_uuid=candidate_uuid_text,
                    count=cfg.frame_annotation_anchor_lookback_count,
                    runtime_epoch_id=runtime_epoch_id,
                    stream_session_id=stream_session_id,
                )
                if anchor_keyframe_pts_match is not None:
                    anchor_keyframe_pts = anchor_keyframe_pts_match[1]
            if anchor_keyframe_pts is None:
                last_error = (
                    f"{MISSING_ANCHOR_KEYFRAME_PTS_ERROR} "
                    f"anchor_keyframe_uuid={candidate_uuid_text} source_id={source_id}"
                )
            elif (
                candidate_source == "keyframes_find_pts_verified"
                and anchor_keyframe_pts_match is not None
                and anchor_keyframe_pts_match[0] != 0
            ):
                last_error = (
                    "keyframes_find_missing_exact_anchor_annotation "
                    f"anchor_keyframe_uuid={candidate_uuid_text} source_id={source_id}"
                )
            elif not _anchor_keyframe_pts_verified_for_lookup(
                anchor_keyframe_pts=anchor_keyframe_pts,
                requested_start_pts=requested_start_pts,
                requested_end_pts=requested_end_pts,
                max_keyframe_pts_delta_ns=max_keyframe_pts_delta_ns,
            ):
                last_error = (
                    f"{ANCHOR_KEYFRAME_PTS_OUTSIDE_WINDOW_ERROR} "
                    f"anchor_keyframe_uuid={candidate_uuid_text} "
                    f"anchor_keyframe_pts={anchor_keyframe_pts} "
                    f"requested_start_pts={requested_start_pts} "
                    f"requested_end_pts={requested_end_pts}"
                )
            else:
                updated_req = _apply_replay_anchor_to_request(
                    req,
                    proofs=proofs,
                    anchor_keyframe_uuid=candidate_uuid_text,
                    anchor_keyframe_pts=anchor_keyframe_pts,
                    anchor_keyframe_source=candidate_source,
                    pre_seconds=pre_seconds,
                    post_seconds=post_seconds,
                    replay_duration_extra_slack_s=cfg.replay_duration_extra_slack_s,
                )
                logger.info(
                    "frame_domain_proof_selected request_id=%s source_id=%s "
                    "camera_id=%s event_frame_uuid=%s event_frame_pts=%s "
                    "stream_session_id=%s "
                    "anchor_keyframe_uuid=%s anchor_keyframe_pts=%s "
                    "anchor_keyframe_source=%s start_window_frame_pts=%s "
                    "start_window_frame_uuid=%s start_window_session_id=%s "
                    "post_window_frame_pts=%s post_window_frame_uuid=%s "
                    "post_window_session_id=%s post_window_stream_id=%s "
                    "frame_domain_session_policy=%s "
                    "post_window_proof_only=true start_window_coverage_only=true "
                    "replay_offset_seconds=%s replay_duration_base_seconds=%s "
                    "replay_duration_anchor_before_start_guard_used=%s "
                    "replay_duration_anchor_before_start_guard_s=%s "
                    "replay_duration_before_slack_s=%s replay_duration_seconds=%s "
                    "replay_duration_extra_slack_s=%s",
                    req.get("request_id"),
                    source_id,
                    camera_id,
                    event_frame_uuid,
                    req.get("event_frame_pts") or req.get("frame_pts"),
                    stream_session_id,
                    candidate_uuid_text,
                    anchor_keyframe_pts,
                    candidate_source,
                    proofs.start_window_frame.frame_pts,
                    proofs.start_window_frame.frame_uuid,
                    proofs.start_window_frame.stream_session_id,
                    proofs.post_window_frame.frame_pts,
                    proofs.post_window_frame.frame_uuid,
                    proofs.post_window_frame.stream_session_id,
                    proofs.post_window_frame.stream_id,
                    updated_req.get("frame_domain_session_policy"),
                    updated_req.get("replay_offset_seconds"),
                    updated_req.get("replay_duration_base_seconds"),
                    updated_req.get("replay_duration_anchor_before_start_guard_used"),
                    updated_req.get("replay_duration_anchor_before_start_guard_s"),
                    updated_req.get("replay_duration_before_slack_s"),
                    updated_req.get("replay_duration_seconds"),
                    updated_req.get("replay_duration_extra_slack_s"),
                )
                return updated_req, candidate_uuid_text, candidate_source, None

        logger.info(
            "frame_domain_proof_waiting request_id=%s source_id=%s camera_id=%s "
            "target_pts=%s min_stream_ms=%s min_frame_uuid_ms=%s "
            "max_pts_delta_ns=%s stream_session_id=%s keyframe_source=%s "
            "attempt=%s attempts=%s wait_budget_s=%.3f poll_interval_s=%.3f "
            "last_error=%s",
            req.get("request_id"),
            source_id,
            camera_id,
            target_pts,
            min_anchor_stream_ms,
            min_anchor_frame_uuid_ms,
            max_anchor_pts_delta_ns,
            stream_session_id,
            keyframe_source,
            attempt + 1,
            attempts,
            wait_budget_s,
            poll_interval_s,
            last_error,
        )
        if on_wait is not None:
            now_after_lookup = time.monotonic()
            is_first_wait = attempt == 0
            is_last_wait = attempt + 1 >= attempts
            deadline_reached = (
                wait_deadline_at is not None and now_after_lookup >= wait_deadline_at
            )
            diagnostics = (
                _post_savant_frame_proof_diagnostics(
                    redis_client,
                    cfg,
                    req,
                    source_id=str(source_id),
                    camera_id=str(camera_id or source_id),
                    target_pts=target_pts,
                    requested_start_pts=requested_start_pts,
                    requested_end_pts=requested_end_pts,
                    runtime_epoch_id=runtime_epoch_id,
                    stream_session_id=stream_session_id,
                )
                if is_first_wait or is_last_wait or deadline_reached
                else {}
            )
            diagnostics["proof_attempt"] = attempt + 1
            diagnostics["proof_attempts"] = attempts
            diagnostics["proof_wait_budget_s"] = wait_budget_s
            diagnostics["proof_poll_interval_s"] = poll_interval_s
            diagnostics["proof_elapsed_s"] = now_after_lookup - wait_started_at
            diagnostics["proof_deadline_reached"] = deadline_reached
            on_wait(attempt + 1, attempts, last_error, diagnostics)
        now_after_wait_update = time.monotonic()
        if wait_deadline_at is not None and now_after_wait_update >= wait_deadline_at:
            break
        if attempt + 1 < attempts:
            sleep_s = poll_interval_s
            if wait_deadline_at is not None:
                sleep_s = min(sleep_s, max(0.0, wait_deadline_at - time.monotonic()))
            if sleep_s > 0.0:
                time.sleep(sleep_s)

    return None, keyframe_uuid, keyframe_source, last_error


def _replay_job_labels(
    event_id: str,
    req: dict,
    *,
    replay_offset_seconds: float | None = None,
    replay_duration_seconds: float | None = None,
) -> dict[str, str]:
    """Build Replay labels, preserving explicit post-Savant policy fields."""
    labels = {"event_id": event_id}
    for key in (
        "request_id",
        "source_event_id",
        "replay_source_kind",
        "evidence_topology",
        "annotation_source_policy",
        "frame_pts",
        "frame_num",
        "metadata_domain",
        "requested_start_pts",
        "original_requested_start_pts",
        "effective_start_pts",
        "requested_end_pts",
        "requested_pre_window_seconds",
        "effective_pre_window_seconds",
        "pre_window_truncated_seconds",
        "event_frame_uuid",
        "event_frame_pts",
        "anchor_keyframe_uuid",
        "anchor_keyframe_pts",
        "anchor_keyframe_source",
        "evidence_anchor_strategy",
        "start_window_frame_uuid",
        "start_window_frame_pts",
        "start_window_stream_session_id",
        "start_window_coverage_used",
        "start_window_frame_annotation_stream_id",
        "post_window_frame_uuid",
        "post_window_frame_pts",
        "post_window_stream_session_id",
        "post_window_proof_used",
        "post_window_frame_annotation_stream_id",
        "post_window_cross_session_proof_used",
        "pre_window_truncated",
        "pre_window_policy",
        "frame_domain_session_policy",
        "frame_domain_proof_method",
        "replay_stop_strategy",
        "replay_duration_base_seconds",
        "replay_duration_anchor_before_start_guard_used",
        "replay_duration_anchor_before_start_guard_s",
        "replay_duration_before_slack_s",
        "replay_duration_extra_slack_s",
        "replay_duration_seconds",
        "runtime_epoch_id",
        "stream_session_id",
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
    return _valid_epoch_ms_or_none(req.get("event_ts_ms"), now_ms=now_ms) or now_ms


def _max_jobs_limit_enabled(cfg: Config) -> bool:
    """Only one-shot invocations may use CLIP_WORKER_MAX_JOBS_PER_RUN as a cap."""
    return cfg.run_once and cfg.max_jobs_per_run > 0


def _max_jobs_limit_reached(cfg: Config, jobs_created: int) -> bool:
    return _max_jobs_limit_enabled(cfg) and jobs_created >= cfg.max_jobs_per_run


def _clip_gate_decision(
    cfg: Config,
    *,
    jobs_created: int,
    active_jobs: list[ActiveReplayJob],
    shard_id: str,
    source_id: str,
    camera_id: str,
    cooldown_gate_ts_ms: int,
    last_job_by_camera: dict[str, int],
    event_type_counts: dict[str, int],
    event_type: str = "",
) -> ClipGateDecision:
    high_priority_types = set(cfg.evidence_high_priority_event_types) or PRIORITY_EVENT_TYPES
    is_priority_event = event_type in high_priority_types
    active_job_count = len(active_jobs)
    active_for_shard = sum(1 for job in active_jobs if job.shard_id == shard_id)
    active_for_source = sum(1 for job in active_jobs if job.source_id == source_id)
    pressure_level = str(cfg.evidence_materialization_pressure_level or "normal").lower()
    if pressure_level == "hard":
        return ClipGateDecision(
            allowed=False,
            reason="materialization_pressure_hard_limit",
            error_message="EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL=hard",
            terminal_defer=True,
            degrade_decision={
                "level": pressure_level,
                "action": "keep_manifest_stop_media_materialization",
            },
        )
    if pressure_level == "critical" and not is_priority_event:
        return ClipGateDecision(
            allowed=False,
            reason="materialization_pressure_critical_low_priority_deferred",
            error_message="critical pressure allows only high-priority materialization",
            terminal_defer=True,
            degrade_decision={
                "level": pressure_level,
                "action": "defer_low_priority",
            },
        )
    if pressure_level == "warning" and not is_priority_event:
        return ClipGateDecision(
            allowed=False,
            reason="materialization_pressure_warning_low_priority_deferred",
            error_message="warning pressure defers low-priority materialization",
            terminal_defer=True,
            degrade_decision={
                "level": pressure_level,
                "action": "defer_low_priority",
            },
        )
    if _max_jobs_limit_reached(cfg, jobs_created):
        return ClipGateDecision(
            allowed=False,
            reason="max_jobs_reached",
            error_message="CLIP_WORKER_MAX_JOBS_PER_RUN reached",
            terminal_defer=True,
            quota_decision={
                "scope": "run",
                "limit": cfg.max_jobs_per_run,
                "observed": jobs_created,
            },
        )
    event_type_limit = cfg.evidence_materialization_event_type_quotas.get(event_type, 0)
    if event_type_limit > 0 and event_type_counts.get(event_type, 0) >= event_type_limit:
        return ClipGateDecision(
            allowed=False,
            reason="event_type_quota_reached",
            error_message=f"EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS reached for {event_type}",
            terminal_defer=True,
            quota_decision={
                "scope": "event_type",
                "event_type": event_type,
                "limit": event_type_limit,
                "observed": event_type_counts.get(event_type, 0),
            },
        )
    max_global = cfg.evidence_materialization_max_concurrency
    if (
        max_global > 0
        and active_job_count >= max_global
        and not is_priority_event
    ):
        return ClipGateDecision(
            allowed=False,
            reason="max_concurrent_reached",
            error_message="EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY reached",
            quota_decision={
                "scope": "global_concurrency",
                "limit": max_global,
                "observed": active_job_count,
            },
        )
    max_per_shard = cfg.evidence_materialization_max_concurrency_per_shard
    if max_per_shard > 0 and active_for_shard >= max_per_shard and not is_priority_event:
        return ClipGateDecision(
            allowed=False,
            reason="max_concurrent_per_shard_reached",
            error_message="EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD reached",
            quota_decision={
                "scope": "shard_concurrency",
                "shard_id": shard_id,
                "limit": max_per_shard,
                "observed": active_for_shard,
            },
        )
    max_per_source = cfg.evidence_materialization_max_concurrency_per_source
    if max_per_source > 0 and active_for_source >= max_per_source and not is_priority_event:
        return ClipGateDecision(
            allowed=False,
            reason="max_concurrent_per_source_reached",
            error_message="EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE reached",
            quota_decision={
                "scope": "source_concurrency",
                "source_id": source_id,
                "limit": max_per_source,
                "observed": active_for_source,
            },
        )
    if (
        cfg.per_camera_cooldown_seconds > 0
        and camera_id
        and cooldown_gate_ts_ms - last_job_by_camera.get(camera_id, 0)
        < cfg.per_camera_cooldown_seconds * 1000
        and not is_priority_event
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


def _resolve_replay_route(
    cfg: Config,
    *,
    source_id: str,
    replay_clients: dict[str, ReplayClient],
) -> ReplayRoute:
    shard = cfg.replay_shards.shard_for_source(source_id)
    client = replay_clients.get(shard.replay_api_url)
    if client is None:
        client = ReplayClient(shard.replay_api_url)
        replay_clients[shard.replay_api_url] = client
    return ReplayRoute(shard=shard, client=client)


def run_worker(
    cfg: Config, redis_client: Redis, pg_conn: psycopg.Connection
) -> None:
    stream = cfg.record_request_stream
    group = cfg.consumer_group
    consumer = cfg.consumer_name
    _ensure_group(redis_client, stream, group)
    replay_clients: dict[str, ReplayClient] = {}
    jobs_created = 0
    active_jobs: list[ActiveReplayJob] = []
    event_type_counts: dict[str, int] = defaultdict(int)
    last_job_by_camera: dict[str, int] = defaultdict(int)
    seen_requests: set[str] = set()
    last_pending_claim_at = 0.0
    last_expire_check_at = 0.0

    logger.info(
        "clip-worker started stream=%s group=%s replay=%s "
        "max_jobs_per_run=%s run_once=%s max_jobs_per_run_effective=%s "
        "max_concurrent_jobs=%s per_camera_cooldown_seconds=%s "
        "pending_claim_min_idle_ms=%s pending_claim_count=%s "
        "pending_claim_interval_s=%s deferred_retry_max_attempts=%s "
        "stop_condition_mode=%s replay_fps=%s replay_duration_extra_slack_s=%s "
        "allow_unbounded_keyframe_fallback=%s replay_shards_enabled=%s",
        stream, group, cfg.replay_api_url,
        cfg.max_jobs_per_run,
        cfg.run_once,
        "enabled" if _max_jobs_limit_enabled(cfg) else "disabled",
        cfg.max_concurrent_jobs,
        cfg.per_camera_cooldown_seconds,
        cfg.pending_claim_min_idle_ms,
        cfg.pending_claim_count,
        cfg.pending_claim_interval_s,
        cfg.deferred_retry_max_attempts,
        cfg.replay_stop_condition_mode,
        cfg.replay_fps,
        cfg.replay_duration_extra_slack_s,
        cfg.allow_unbounded_keyframe_fallback,
        cfg.replay_shards.enabled,
    )

    total_processed = 0
    last_report = time.monotonic()

    while not shutdown_requested:
        try:
            now_for_expire = time.monotonic()
            if now_for_expire - last_expire_check_at >= 30.0 or cfg.run_once:
                expired_count = expire_materialization_deadlines(pg_conn)
                if expired_count:
                    logger.info(
                        "clip_worker_materialization_expired count=%s",
                        expired_count,
                    )
                last_expire_check_at = now_for_expire

            pending_delivery_counts: dict[str, int] = {}
            pending_entries: list[tuple[object, object]] = []
            now_for_claim = time.monotonic()
            if (
                cfg.pending_claim_count > 0
                and now_for_claim - last_pending_claim_at
                >= max(0.0, cfg.pending_claim_interval_s)
            ):
                pending_entries, pending_delivery_counts = _claim_pending_entries(
                    redis_client,
                    cfg,
                    stream=stream,
                    group=group,
                    consumer=consumer,
                )
                last_pending_claim_at = now_for_claim

            if pending_entries:
                result = [(stream, pending_entries)]
            else:
                # Read new record requests. Pending messages are handled by the
                # claim path above so one blocked request does not hide later ones.
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
                    retry_count = _retry_count_for_msg(msg_id, pending_delivery_counts)
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
                    source_id = str(req.get("source_id") or "")
                    source_event_id = req.get("source_event_id", "")
                    event_ts_ms = int(req.get("event_ts_ms", 0))
                    event_type = _record_request_event_type(req)
                    cooldown_gate_ts_ms = _cooldown_gate_ts_ms(req)
                    camera_id = req.get("camera_id", "")
                    pre_seconds = int(req.get("pre_seconds", cfg.default_pre_seconds))
                    post_seconds = int(req.get("post_seconds", cfg.default_post_seconds))
                    keyframe_uuid, keyframe_source = _keyframe_from_request(req)
                    replay_anchor_req = req
                    post_savant_media_request = _is_post_savant_media_request(req)
                    now_monotonic = time.monotonic()
                    active_jobs = [
                        job for job in active_jobs if job.until_monotonic > now_monotonic
                    ]

                    terminal_state = terminal_evidence_state(pg_conn, event_id)
                    if terminal_state:
                        logger.info(
                            "clip_worker_acked_terminal_request request_id=%s "
                            "event_id=%s state=%s msg_id=%s",
                            request_id,
                            event_id,
                            terminal_state,
                            msg_id,
                        )
                        seen_requests.add(request_id)
                        redis_client.xack(stream, group, msg_id)
                        total_processed += 1
                        continue

                    target_exists = record_request_target_exists(
                        pg_conn,
                        event_id=event_id,
                        source_event_id=str(source_event_id or ""),
                    )
                    if target_exists is False:
                        logger.warning(
                            "clip_worker_acked_stale_request request_id=%s "
                            "event_id=%s source_event_id=%s msg_id=%s "
                            "reason=missing_db_event_and_evidence_task",
                            request_id,
                            event_id,
                            source_event_id,
                            msg_id,
                        )
                        seen_requests.add(request_id)
                        redis_client.xack(stream, group, msg_id)
                        total_processed += 1
                        continue

                    try:
                        replay_route = _resolve_replay_route(
                            cfg,
                            source_id=source_id,
                            replay_clients=replay_clients,
                        )
                    except ReplayShardConfigError as exc:
                        diagnostics = {
                            "source_id": source_id,
                            "replay_shard_error": str(exc),
                            "replay_shards": cfg.replay_shards.to_dict(),
                        }
                        update_clip_status(
                            pg_conn,
                            event_id,
                            "failed",
                            error_message=f"replay shard routing failed: {exc}",
                            evidence_state="failed",
                            evidence_reason="replay_shard_routing_failed",
                            request_id=request_id,
                            diagnostics=diagnostics,
                        )
                        seen_requests.add(request_id)
                        redis_client.xack(stream, group, msg_id)
                        total_processed += 1
                        continue
                    replay = replay_route.client
                    replay_shard = replay_route.diagnostics()

                    schedule_gate = _clip_gate_decision(
                        cfg,
                        jobs_created=jobs_created,
                        active_jobs=active_jobs,
                        shard_id=replay_route.shard.shard_id,
                        source_id=str(source_id),
                        camera_id=str(camera_id),
                        cooldown_gate_ts_ms=cooldown_gate_ts_ms,
                        last_job_by_camera=last_job_by_camera,
                        event_type_counts=event_type_counts,
                        event_type=event_type,
                    )
                    if not schedule_gate.allowed:
                        if schedule_gate.terminal_defer:
                            _defer_clip_request_terminal(
                                redis_client,
                                pg_conn,
                                stream=stream,
                                group=group,
                                msg_id=msg_id,
                                event_id=event_id,
                                request_id=request_id,
                                reason=schedule_gate.reason,
                                error_message=schedule_gate.error_message,
                                seen_requests=seen_requests,
                                replay_shard=replay_shard,
                                quota_decision=schedule_gate.quota_decision,
                                degrade_decision=schedule_gate.degrade_decision,
                            )
                            total_processed += 1
                            continue
                        if schedule_gate.reason.startswith("max_concurrent"):
                            _queue_clip_request(
                                pg_conn,
                                event_id=event_id,
                                request_id=request_id,
                                reason=schedule_gate.reason,
                                error_message=schedule_gate.error_message,
                                retry_count=retry_count,
                                msg_id=msg_id,
                                active_job_count=len(active_jobs),
                                max_concurrent_jobs=cfg.evidence_materialization_max_concurrency,
                                replay_shard=replay_shard,
                                quota_decision=schedule_gate.quota_decision,
                            )
                            total_processed += 1
                            continue
                        if schedule_gate.reason == "cooldown":
                            _defer_clip_request(
                                redis_client,
                                pg_conn,
                                stream=stream,
                                group=group,
                                msg_id=msg_id,
                                event_id=event_id,
                                request_id=request_id,
                                reason=schedule_gate.reason,
                                error_message=schedule_gate.error_message,
                                retry_count=retry_count,
                                max_retries=cfg.deferred_retry_max_attempts,
                                seen_requests=seen_requests,
                                replay_shard=replay_shard,
                                quota_decision=schedule_gate.quota_decision,
                                degrade_decision=schedule_gate.degrade_decision,
                            )
                            total_processed += 1
                            continue
                        logger.info(
                            "clip_worker_skipped %s event_id=%s source_event_id=%s "
                            "event_type=%s camera_id=%s jobs_created=%s run_once=%s",
                            schedule_gate.reason,
                            event_id,
                            source_event_id,
                            event_type,
                            camera_id,
                            jobs_created,
                            cfg.run_once,
                        )
                        update_clip_status(
                            pg_conn,
                            event_id,
                            "skipped_by_poc_limit",
                            error_message=schedule_gate.error_message,
                            replay_shard=replay_shard,
                        )
                        seen_requests.add(request_id)
                        redis_client.xack(stream, group, msg_id)
                        total_processed += 1
                        continue

                    if post_savant_media_request:
                        frame_proof_wait_started = time.monotonic()
                        proof_wait_diagnostics: dict[str, object] = {}
                        proof_wait_attempt_count = 0
                        proof_retry_count = _persisted_proof_retry_count(
                            pg_conn,
                            event_id,
                        )
                        wait_budget_override = None
                        poll_interval_override = None
                        attempts_override = None
                        if retry_count > 0:
                            wait_budget_override = 0.0
                            poll_interval_override = 0.0
                            attempts_override = 1

                        def _mark_waiting_proof(
                            attempt: int,
                            _attempts: int,
                            error: str,
                            diagnostics: dict[str, object],
                        ) -> None:
                            nonlocal proof_wait_diagnostics, proof_wait_attempt_count
                            proof_wait_diagnostics = dict(diagnostics)
                            proof_wait_diagnostics["proof_retry_count"] = proof_retry_count
                            proof_wait_diagnostics["redis_delivery_retry_count"] = retry_count
                            proof_wait_attempt_count = attempt
                            update_clip_status(
                                pg_conn,
                                event_id,
                                "pending",
                                error_message=error,
                                evidence_state="waiting_proof",
                                evidence_reason=error,
                                request_id=request_id,
                                attempt_count=attempt,
                                diagnostics=proof_wait_diagnostics,
                                replay_shard=replay_shard,
                            )

                        (
                            replay_anchor_req,
                            keyframe_uuid,
                            keyframe_source,
                            anchor_error,
                        ) = _prepare_post_savant_replay_request(
                            redis_client,
                            replay,
                            cfg,
                            req,
                            source_id=str(source_id),
                            camera_id=str(camera_id or source_id),
                            keyframe_uuid=keyframe_uuid,
                            keyframe_source=keyframe_source,
                            pre_seconds=pre_seconds,
                            post_seconds=post_seconds,
                            on_wait=_mark_waiting_proof,
                            wait_budget_s_override=wait_budget_override,
                            poll_interval_s_override=poll_interval_override,
                            attempts_override=attempts_override,
                        )
                        frame_proof_wait_seconds = time.monotonic() - frame_proof_wait_started
                        logger.info(
                            "clip_worker_frame_proof_wait request_id=%s event_id=%s "
                            "frame_proof_wait_seconds=%.3f anchor_ready=%s",
                            req.get("request_id"),
                            event_id,
                            frame_proof_wait_seconds,
                            anchor_error is None and replay_anchor_req is not None,
                        )
                        if anchor_error is not None or replay_anchor_req is None:
                            target_pts = _frame_annotation_anchor_target_pts(
                                req,
                                post_seconds=post_seconds,
                            )
                            requested_start_pts, requested_end_pts = _requested_pts_window(
                                req,
                                pre_seconds=pre_seconds,
                                post_seconds=post_seconds,
                            )
                            if not proof_wait_diagnostics:
                                proof_wait_diagnostics = (
                                    _post_savant_frame_proof_diagnostics(
                                        redis_client,
                                        cfg,
                                        req,
                                        source_id=str(source_id),
                                        camera_id=str(camera_id or source_id),
                                        target_pts=target_pts,
                                        requested_start_pts=requested_start_pts,
                                        requested_end_pts=requested_end_pts,
                                        runtime_epoch_id=_runtime_epoch_id_from_request(req),
                                        stream_session_id=_stream_session_id_from_request(req),
                                    )
                                )
                            proof_wait_diagnostics["proof_wait_seconds"] = (
                                frame_proof_wait_seconds
                            )
                            proof_retryable = _post_savant_anchor_error_retryable(
                                req,
                                str(anchor_error),
                            )
                            proof_wait_diagnostics["proof_retryable_legacy"] = proof_retryable
                            proof_wait_diagnostics["proof_retry_count"] = (
                                proof_retry_count
                            )
                            proof_wait_diagnostics["redis_delivery_retry_count"] = (
                                retry_count
                            )
                            if proof_retryable and _defer_post_savant_frame_proof(
                                pg_conn,
                                event_id=event_id,
                                request_id=request_id,
                                error_message=str(anchor_error),
                                proof_retry_count=proof_retry_count,
                                max_retries=cfg.deferred_retry_max_attempts,
                                msg_id=msg_id,
                                diagnostics=proof_wait_diagnostics,
                                replay_shard=replay_shard,
                            ):
                                total_processed += 1
                                continue
                            final_error = str(anchor_error)
                            if proof_retryable:
                                final_error = (
                                    "retry_budget_exhausted "
                                    "reason=missing_post_savant_frame_proof "
                                    f"retries={proof_retry_count} "
                                    f"error={anchor_error}"
                                )
                            logger.warning(
                                "clip_worker_final_failed post_savant_anchor_failed "
                                "request_id=%s source_event_id=%s source_id=%s "
                                "camera_id=%s attempt_count=%s proof_retry_count=%s "
                                "error=%s",
                                req.get("request_id"),
                                source_event_id,
                                source_id,
                                camera_id,
                                proof_wait_attempt_count,
                                proof_retry_count,
                                final_error,
                            )
                            _fail_clip_request(
                                redis_client,
                                pg_conn,
                                stream=stream,
                                group=group,
                                msg_id=msg_id,
                                event_id=event_id,
                                request_id=request_id,
                                error_message=final_error,
                                seen_requests=seen_requests,
                                evidence_state="failed",
                                evidence_reason=final_error,
                                attempt_count=proof_retry_count or proof_wait_attempt_count or None,
                                diagnostics=proof_wait_diagnostics,
                                replay_shard=replay_shard,
                            )
                            total_processed += 1
                            continue
                    elif keyframe_uuid:
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
                                replay_shard=replay_shard,
                            )
                            seen_requests.add(request_id)
                            redis_client.xack(stream, group, msg_id)
                            total_processed += 1
                            continue

                        if not source_id:
                            update_clip_status(
                                pg_conn, event_id, "failed",
                                error_message="missing source_id in record_request",
                                replay_shard=replay_shard,
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
                                replay_shard=replay_shard,
                            )
                            redis_client.xack(stream, group, msg_id)
                            total_processed += 1
                            continue

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
                            if attempt + 1 < attempts:
                                logger.info(
                                    "replay_anchor_lookup_waiting "
                                    "request_id=%s source_id=%s lookup_ts_ms=%s "
                                    "attempt=%s attempts=%s sleep_s=%.3f",
                                    req.get("request_id"),
                                    source_id,
                                    lookup_ts_ms,
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
                            replay_shard=replay_shard,
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
                        sink_endpoint=replay_route.shard.replay_job_sink_url,
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
                            replay_shard=replay_shard,
                        )
                        jobs_created += 1
                        active_jobs.append(
                            ActiveReplayJob(
                                until_monotonic=(
                                    time.monotonic()
                                    + float(pre_seconds + post_seconds + 5)
                                ),
                                shard_id=replay_route.shard.shard_id,
                                source_id=str(source_id),
                                event_type=event_type,
                            )
                        )
                        if event_type:
                            event_type_counts[event_type] += 1
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
                            replay_shard=replay_shard,
                        )

                    seen_requests.add(request_id)
                    redis_client.xack(stream, group, msg_id)
                    total_processed += 1

            now = time.monotonic()
            if now - last_report >= 60:
                diag = _redis_stream_group_diagnostics(
                    redis_client,
                    stream=stream,
                    group=group,
                )
                logger.info(
                    "clip-worker summary: total_processed=%d redis_pending=%s "
                    "redis_lag=%s",
                    total_processed,
                    diag.get("pending"),
                    diag.get("lag"),
                )
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
