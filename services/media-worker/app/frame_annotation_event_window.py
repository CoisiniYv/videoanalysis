"""Event-scoped frame annotation window helpers for C1J.5."""

from __future__ import annotations

import copy
import json
import math
from statistics import median
from typing import Any


SUPPORTED_EVENT_TYPES = {"watchlist_hit", "live_search_hit"}


def extract_watchlist_event_anchor(
    event: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Extract the event anchor used to scope a frame annotation window."""

    if not isinstance(event, dict):
        return None, {"status": "error", "reason": "invalid_event", "missing_fields": []}

    normalized = _normalize_event(event)
    payload = _dict_or_empty(normalized.get("payload"))
    match = _dict_or_empty(payload.get("match"))
    observation = _dict_or_empty(payload.get("observation"))
    media = _dict_or_empty(payload.get("media"))
    matched_person = _dict_or_empty(payload.get("matched_person"))
    person = _dict_or_empty(payload.get("person"))
    overlay = _dict_or_empty(payload.get("overlay"))
    live_search = _dict_or_empty(payload.get("live_search"))

    event_type = _first_text(normalized, payload, keys=("event_type", "type"))
    if event_type not in SUPPORTED_EVENT_TYPES:
        return None, {
            "status": "error",
            "reason": "unsupported_event_type",
            "event_type": event_type,
            "missing_fields": [],
        }

    anchor = {
        "event_id": _first_text(normalized, payload, keys=("id", "event_id")),
        "source_event_id": _first_text(normalized, payload, keys=("source_event_id",)),
        "event_type": event_type,
        "source_id": _first_text(normalized, observation, payload, keys=("source_id",)),
        "camera_id": _first_text(normalized, observation, payload, keys=("camera_id",)),
        "source_observation_id": _first_text(
            normalized,
            payload,
            match,
            observation,
            live_search,
            keys=("source_observation_id", "observation_id"),
        ),
        "frame_uuid": _first_text(media, normalized, observation, keys=("frame_uuid",)),
        "frame_pts": _first_int(
            media,
            normalized,
            observation,
            keys=("frame_pts", "source_pts", "pts"),
        ),
        "timestamp_ms": _first_int(
            observation,
            media,
            normalized,
            payload,
            keys=("timestamp_ms", "event_ts_ms", "start_ts_ms"),
        ),
        "event_ts_ms": _first_int(
            normalized,
            media,
            payload,
            keys=("event_ts_ms", "timestamp_ms", "start_ts_ms"),
        ),
        "face_bbox": _first_value(
            observation,
            overlay,
            payload,
            normalized,
            keys=("face_bbox", "bbox"),
        ),
        "person_id": _first_value(
            normalized,
            matched_person,
            person,
            payload,
            keys=("person_id", "id"),
        ),
        "external_person_id": _first_text(
            matched_person,
            person,
            payload,
            keys=("external_person_id", "external_id"),
        ),
        "display_name": _first_text(
            matched_person,
            person,
            payload,
            keys=("display_name", "person_name", "name"),
        ),
        "similarity": _first_float(
            match,
            payload,
            normalized,
            keys=("similarity", "match_score", "score", "confidence"),
        ),
        "threshold": _first_float(match, payload, keys=("threshold", "match_threshold")),
    }

    missing_fields = [
        field
        for field in (
            "source_id",
            "camera_id",
            "source_observation_id",
            "frame_uuid_or_frame_pts",
        )
        if (
            (field == "frame_uuid_or_frame_pts" and not anchor.get("frame_uuid") and anchor.get("frame_pts") is None)
            or (field != "frame_uuid_or_frame_pts" and not anchor.get(field))
        )
    ]
    summary = {
        "status": "ok",
        "reason": None,
        "event_type": event_type,
        "missing_fields": missing_fields,
        "has_source_observation_id": bool(anchor.get("source_observation_id")),
        "has_source_camera": bool(anchor.get("source_id") and anchor.get("camera_id")),
        "has_frame_uuid": bool(anchor.get("frame_uuid")),
        "has_frame_pts": anchor.get("frame_pts") is not None,
        "has_face_bbox": anchor.get("face_bbox") is not None,
        "has_person_identity": bool(
            anchor.get("person_id") is not None
            or anchor.get("external_person_id")
            or anchor.get("display_name")
        ),
        "has_similarity": anchor.get("similarity") is not None,
        "has_threshold": anchor.get("threshold") is not None,
    }
    return anchor, summary


def select_frame_annotation_event_window(
    messages: list[dict[str, Any]],
    *,
    source_id: str,
    camera_id: str,
    anchor_frame_pts: int | None,
    anchor_frame_uuid: str | None,
    anchor_source_observation_id: str | None,
    pre_seconds: float = 5.0,
    post_seconds: float = 5.0,
    max_frames: int = 300,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select a bounded event-centered window from frame annotation messages."""

    _validate_window_args(pre_seconds=pre_seconds, post_seconds=post_seconds, max_frames=max_frames)
    source_id = _require_text(source_id, "source_id")
    camera_id = _require_text(camera_id, "camera_id")
    copied = copy.deepcopy(messages)
    candidates = [
        message
        for message in copied
        if message.get("source_id") == source_id and message.get("camera_id") == camera_id
    ]
    candidates.sort(key=_message_sort_key)

    median_pts_delta = _median_delta(
        [
            int(message["frame_pts"])
            for message in candidates
            if isinstance(message.get("frame_pts"), int)
        ]
    )
    median_timestamp_delta_ms = _median_delta(
        [
            int(message["timestamp_ms"])
            for message in candidates
            if isinstance(message.get("timestamp_ms"), int)
        ]
    )

    anchor_index, anchor_found_by = _find_anchor_index(
        candidates,
        anchor_source_observation_id=anchor_source_observation_id,
        anchor_frame_uuid=anchor_frame_uuid,
        anchor_frame_pts=anchor_frame_pts,
        median_pts_delta=median_pts_delta,
    )
    anchor_found = anchor_index is not None
    missing_reason = None if anchor_found else "missing_anchor_frame"

    window: list[dict[str, Any]] = []
    window_bounded = True
    window_frame_count_method = "missing_anchor"
    nearest_pts_tolerance = (
        float(median_pts_delta) * 0.5
        if median_pts_delta is not None
        else None
    )

    if anchor_index is not None:
        start, end, window_frame_count_method = _window_bounds(
            candidates,
            anchor_index=anchor_index,
            pre_seconds=pre_seconds,
            post_seconds=post_seconds,
            max_frames=max_frames,
            median_timestamp_delta_ms=median_timestamp_delta_ms,
        )
        window = copy.deepcopy(candidates[start:end])
        if len(window) > max_frames:
            window_bounded = False
            window = window[:max_frames]

    summary = {
        "source_id": source_id,
        "camera_id": camera_id,
        "anchor_source_observation_id": anchor_source_observation_id,
        "anchor_frame_uuid": anchor_frame_uuid,
        "anchor_frame_pts": anchor_frame_pts,
        "anchor_found_by": anchor_found_by,
        "anchor_found": anchor_found,
        "median_pts_delta": median_pts_delta,
        "median_timestamp_delta_ms": median_timestamp_delta_ms,
        "nearest_pts_tolerance": nearest_pts_tolerance,
        "pre_seconds": float(pre_seconds),
        "post_seconds": float(post_seconds),
        "window_messages": len(window),
        "candidate_messages_after_source_camera_filter": len(candidates),
        "max_frames": int(max_frames),
        "window_bounded": window_bounded and len(window) <= max_frames,
        "window_frame_count_method": window_frame_count_method,
        "missing_reason": missing_reason,
        "returned_whole_lookback": len(window) == len(candidates) and len(candidates) > max_frames,
    }
    return window, summary


def _find_anchor_index(
    candidates: list[dict[str, Any]],
    *,
    anchor_source_observation_id: str | None,
    anchor_frame_uuid: str | None,
    anchor_frame_pts: int | None,
    median_pts_delta: float | None,
) -> tuple[int | None, str]:
    if anchor_source_observation_id:
        for index, message in enumerate(candidates):
            if _message_has_source_observation_id(message, anchor_source_observation_id):
                return index, "source_observation_id"

    if anchor_frame_uuid:
        for index, message in enumerate(candidates):
            if message.get("frame_uuid") == anchor_frame_uuid:
                return index, "frame_uuid"

    if anchor_frame_pts is not None:
        for index, message in enumerate(candidates):
            if message.get("frame_pts") == anchor_frame_pts:
                return index, "frame_pts_exact"

        if median_pts_delta is not None:
            tolerance = float(median_pts_delta) * 0.5
            best: tuple[float, int] | None = None
            for index, message in enumerate(candidates):
                frame_pts = message.get("frame_pts")
                if not isinstance(frame_pts, int):
                    continue
                distance = abs(int(frame_pts) - int(anchor_frame_pts))
                if distance > tolerance:
                    continue
                candidate = (float(distance), index)
                if best is None or candidate < best:
                    best = candidate
            if best is not None:
                return best[1], "frame_pts_nearest"

    return None, "missing"


def _message_has_source_observation_id(
    message: dict[str, Any],
    source_observation_id: str,
) -> bool:
    objects = message.get("objects")
    if not isinstance(objects, list):
        return False
    for obj in objects:
        if isinstance(obj, dict) and obj.get("source_observation_id") == source_observation_id:
            return True
    return False


def _window_bounds(
    candidates: list[dict[str, Any]],
    *,
    anchor_index: int,
    pre_seconds: float,
    post_seconds: float,
    max_frames: int,
    median_timestamp_delta_ms: float | None,
) -> tuple[int, int, str]:
    total_budget = max(int(max_frames), 1)
    if median_timestamp_delta_ms is not None and median_timestamp_delta_ms > 0:
        pre_count = int(math.ceil(float(pre_seconds) * 1000.0 / median_timestamp_delta_ms))
        post_count = int(math.ceil(float(post_seconds) * 1000.0 / median_timestamp_delta_ms))
        method = "timestamp_ms_delta"
    else:
        total_seconds = max(float(pre_seconds) + float(post_seconds), 0.001)
        pre_count = int(round((float(pre_seconds) / total_seconds) * max(total_budget - 1, 0)))
        post_count = max(total_budget - pre_count - 1, 0)
        method = "max_frames_fallback_pts_unit_unknown"

    if pre_count + post_count + 1 > total_budget:
        available = max(total_budget - 1, 0)
        total = max(pre_count + post_count, 1)
        pre_count = int(round(available * (pre_count / total)))
        post_count = available - pre_count
        method = f"{method}_bounded"

    start = max(anchor_index - pre_count, 0)
    end = min(anchor_index + post_count + 1, len(candidates))
    return start, end, method


def _median_delta(values: list[int]) -> float | None:
    distinct = sorted(set(values))
    deltas = [
        distinct[index] - distinct[index - 1]
        for index in range(1, len(distinct))
        if distinct[index] > distinct[index - 1]
    ]
    return float(median(deltas)) if deltas else None


def _message_sort_key(message: dict[str, Any]) -> tuple[int, int, int, str]:
    frame_pts = message.get("frame_pts")
    timestamp_ms = message.get("timestamp_ms")
    pts_missing = 0 if isinstance(frame_pts, int) else 1
    pts_value = int(frame_pts) if isinstance(frame_pts, int) else 0
    ts_value = int(timestamp_ms) if isinstance(timestamp_ms, int) else 0
    stream_order = int(message.get("_stream_order")) if isinstance(message.get("_stream_order"), int) else 0
    return pts_missing, pts_value, ts_value, f"{stream_order:012d}:{message.get('_stream_id') or ''}"


def _validate_window_args(*, pre_seconds: float, post_seconds: float, max_frames: int) -> None:
    if float(pre_seconds) < 0 or float(post_seconds) < 0:
        raise ValueError("invalid_window_seconds")
    if not isinstance(max_frames, int) or isinstance(max_frames, bool) or max_frames < 1:
        raise ValueError("invalid_max_frames")


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing_{field_name}")
    return value


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    if isinstance(event.get("data"), str):
        try:
            parsed = json.loads(event["data"])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            merged = copy.deepcopy(parsed)
            for key, value in event.items():
                if key not in merged and key != "data":
                    merged[key] = copy.deepcopy(value)
            return merged
    return copy.deepcopy(event)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_value(*scopes: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for scope in scopes:
        for key in keys:
            value = scope.get(key)
            if value is not None and value != "":
                return value
    return None


def _first_text(*scopes: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    value = _first_value(*scopes, keys=keys)
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _first_int(*scopes: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    value = _first_value(*scopes, keys=keys)
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_float(*scopes: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    value = _first_value(*scopes, keys=keys)
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
