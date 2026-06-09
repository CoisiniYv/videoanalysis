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
C2_POST_SAVANT_TOPOLOGIES = {"post_savant", "post_savant_replay"}
POST_SAVANT_MISSING_FRAME_TIMELINE_ERROR = "missing_post_savant_frame_pts_window"
MISSING_ANCHOR_KEYFRAME_PTS_ERROR = "missing_anchor_keyframe_pts"
ANCHOR_KEYFRAME_PTS_OUTSIDE_WINDOW_ERROR = "anchor_keyframe_pts_outside_requested_window"
EVENT_FRAME_ANCHOR_NOT_KEYFRAME_ERROR = "event_frame_anchor_not_keyframe"
LOOKUP_RETURNED_PROOF_KEYFRAME_ERROR = "keyframes_find_returned_proof_keyframe"
PRIORITY_EVENT_TYPES = {"watchlist_hit", "live_search_hit"}


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
    keyframe_uuid: str | None = None
    previous_keyframe_uuid: str | None = None
    keyframe_pts: int | None = None
    anchor_method: str = "frame_annotation"


@dataclass(frozen=True)
class ReplayFrameDomainProofs:
    start_window_frame: FrameAnnotationAnchor
    post_window_frame: FrameAnnotationAnchor


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
        or evidence_topology in C2_POST_SAVANT_TOPOLOGIES
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
    direction: str = "at_or_after",
    min_stream_ms: int | None = None,
    max_pts_delta_ns: int | None = None,
    min_frame_uuid_ms: int | None = None,
    require_keyframe: bool = False,
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
) -> int | None:
    """Find PTS for the exact Replay keyframe UUID without selecting a new anchor."""
    match = _find_anchor_keyframe_pts_match(
        redis_client,
        stream_name=stream_name,
        source_id=source_id,
        camera_id=camera_id,
        anchor_keyframe_uuid=anchor_keyframe_uuid,
        count=count,
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
) -> None:
    update_clip_status(pg_conn, event_id, "failed", error_message=error_message)
    seen_requests.add(request_id)
    redis_client.xack(stream, group, msg_id)


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
                keyframe_uuid=keyframe_uuid,
                previous_keyframe_uuid=keyframe_uuid,
                keyframe_pts=keyframe_pts,
                anchor_method="frame_annotation_start_window_keyframe_reference",
            )
        )
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item.frame_pts, item.stream_id))


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
        )
    if start_window_frame is None:
        return None
    return ReplayFrameDomainProofs(
        start_window_frame=start_window_frame,
        post_window_frame=post_window_frame,
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
            replay_duration_seconds = max(
                (int(requested_end_pts) - job_start_pts)
                / PTS_TIME_BASE,
                float(pre_seconds + post_seconds),
            )
            if int(anchor_keyframe_pts) <= int(requested_start_pts):
                # Replay may emit from the decoder keyframe before the requested
                # anchor while the stop condition counts from that first emitted
                # frame. Add a guard so the emitted stream still reaches
                # requested_end_pts before media-worker crops the final bundle.
                replay_duration_seconds += max(float(pre_seconds + post_seconds), 1.0) + 1.0
            extra_slack_s = max(float(replay_duration_extra_slack_s), 0.0)
            if extra_slack_s:
                replay_duration_seconds += extra_slack_s
                updated["replay_duration_extra_slack_s"] = extra_slack_s
            updated["replay_duration_seconds"] = replay_duration_seconds
        else:
            extra_slack_s = max(float(replay_duration_extra_slack_s), 0.0)
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
    updated["start_window_frame_annotation_stream_id"] = start_window_frame.stream_id
    updated["post_window_frame_uuid"] = post_window_frame.frame_uuid
    updated["post_window_frame_pts"] = post_window_frame.frame_pts
    updated["post_window_frame_annotation_stream_id"] = post_window_frame.stream_id
    updated["post_window_proof_used"] = True
    updated["start_window_coverage_used"] = True
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
    lookup_ts_ms = _replay_anchor_lookup_ts_ms(
        req,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
        anchor_strategy=cfg.replay_anchor_strategy,
    )
    selection = _replay_anchor_selection(cfg.replay_anchor_strategy)
    attempts = max(1, int(cfg.keyframe_lookup_retries) + 1)
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
                    "anchor_keyframe_uuid=%s anchor_keyframe_pts=%s "
                    "anchor_keyframe_source=%s start_window_frame_pts=%s "
                    "start_window_frame_uuid=%s "
                    "post_window_frame_pts=%s post_window_frame_uuid=%s "
                    "post_window_stream_id=%s "
                    "post_window_proof_only=true start_window_coverage_only=true "
                    "replay_offset_seconds=%s replay_duration_seconds=%s "
                    "replay_duration_extra_slack_s=%s",
                    req.get("request_id"),
                    source_id,
                    camera_id,
                    event_frame_uuid,
                    req.get("event_frame_pts") or req.get("frame_pts"),
                    candidate_uuid_text,
                    anchor_keyframe_pts,
                    candidate_source,
                    proofs.start_window_frame.frame_pts,
                    proofs.start_window_frame.frame_uuid,
                    proofs.post_window_frame.frame_pts,
                    proofs.post_window_frame.frame_uuid,
                    proofs.post_window_frame.stream_id,
                    updated_req.get("replay_offset_seconds"),
                    updated_req.get("replay_duration_seconds"),
                    updated_req.get("replay_duration_extra_slack_s"),
                )
                return updated_req, candidate_uuid_text, candidate_source, None

        logger.info(
            "frame_domain_proof_waiting request_id=%s source_id=%s camera_id=%s "
            "target_pts=%s min_stream_ms=%s min_frame_uuid_ms=%s "
            "max_pts_delta_ns=%s keyframe_source=%s attempt=%s attempts=%s "
            "last_error=%s",
            req.get("request_id"),
            source_id,
            camera_id,
            target_pts,
            min_anchor_stream_ms,
            min_anchor_frame_uuid_ms,
            max_anchor_pts_delta_ns,
            keyframe_source,
            attempt + 1,
            attempts,
            last_error,
        )
        if attempt + 1 < attempts:
            time.sleep(max(0.0, cfg.keyframe_lookup_retry_sleep_s))

    return None, keyframe_uuid, keyframe_source, last_error


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
        "event_frame_uuid",
        "event_frame_pts",
        "anchor_keyframe_uuid",
        "anchor_keyframe_pts",
        "anchor_keyframe_source",
        "evidence_anchor_strategy",
        "start_window_frame_uuid",
        "start_window_frame_pts",
        "start_window_coverage_used",
        "start_window_frame_annotation_stream_id",
        "post_window_frame_uuid",
        "post_window_frame_pts",
        "post_window_proof_used",
        "post_window_frame_annotation_stream_id",
        "frame_domain_proof_method",
        "replay_stop_strategy",
        "replay_duration_extra_slack_s",
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
    active_job_count: int,
    camera_id: str,
    cooldown_gate_ts_ms: int,
    last_job_by_camera: dict[str, int],
    event_type: str = "",
) -> ClipGateDecision:
    is_priority_event = event_type in PRIORITY_EVENT_TYPES
    if _max_jobs_limit_reached(cfg, jobs_created):
        return ClipGateDecision(
            allowed=False,
            reason="max_jobs_reached",
            error_message="CLIP_WORKER_MAX_JOBS_PER_RUN reached",
        )
    if (
        cfg.max_concurrent_jobs > 0
        and active_job_count >= cfg.max_concurrent_jobs
        and not is_priority_event
    ):
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
        "stop_condition_mode=%s replay_fps=%s replay_duration_extra_slack_s=%s "
        "allow_unbounded_keyframe_fallback=%s",
        stream, group, cfg.replay_api_url,
        cfg.max_jobs_per_run,
        cfg.run_once,
        "enabled" if _max_jobs_limit_enabled(cfg) else "disabled",
        cfg.max_concurrent_jobs,
        cfg.per_camera_cooldown_seconds,
        cfg.replay_stop_condition_mode,
        cfg.replay_fps,
        cfg.replay_duration_extra_slack_s,
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
                    event_type = _record_request_event_type(req)
                    cooldown_gate_ts_ms = _cooldown_gate_ts_ms(req)
                    camera_id = req.get("camera_id", "")
                    pre_seconds = int(req.get("pre_seconds", cfg.default_pre_seconds))
                    post_seconds = int(req.get("post_seconds", cfg.default_post_seconds))
                    keyframe_uuid, keyframe_source = _keyframe_from_request(req)
                    replay_anchor_req = req
                    post_savant_media_request = _is_post_savant_media_request(req)
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
                        event_type=event_type,
                    )
                    if not gate.allowed:
                        logger.info(
                            "clip_worker_skipped %s event_id=%s source_event_id=%s "
                            "event_type=%s camera_id=%s jobs_created=%s run_once=%s",
                            gate.reason,
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
                            error_message=gate.error_message,
                        )
                        seen_requests.add(request_id)
                        redis_client.xack(stream, group, msg_id)
                        total_processed += 1
                        continue

                    if post_savant_media_request:
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
                        )
                        if anchor_error is not None or replay_anchor_req is None:
                            logger.warning(
                                "clip_worker_blocked post_savant_anchor_failed "
                                "request_id=%s source_event_id=%s source_id=%s "
                                "camera_id=%s error=%s",
                                req.get("request_id"),
                                source_event_id,
                                source_id,
                                camera_id,
                                anchor_error,
                            )
                            _fail_clip_request(
                                redis_client,
                                pg_conn,
                                stream=stream,
                                group=group,
                                msg_id=msg_id,
                                event_id=event_id,
                                request_id=request_id,
                                error_message=str(anchor_error),
                                seen_requests=seen_requests,
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
