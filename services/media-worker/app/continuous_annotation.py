"""Continuous evidence overlay JSONL generation.

The output is intentionally metadata-only: no embeddings and no image/crop
bytes. Frontend clients can combine ``raw_clip`` with this JSONL timeline to
render overlays on demand.
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.annotation_style import build_style
from app.identity_scope import apply_watchlist_identity_strict, unknown_watchlist_identity

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
INTRUSION_ANNOTATION_SCHEMA_VERSION = "intrusion.annotation.v1"
DEFAULT_PRE_SECONDS = 5.0
DEFAULT_POST_SECONDS = 5.0
ANNOTATION_STATUS_COMPLETE = "complete"
ANNOTATION_STATUS_EMPTY = "empty"
ANNOTATION_STATUS_UNAVAILABLE = "unavailable"
PERSON_CONTEXT_BBOX_SOURCE = "replay.metadata.person_bbox"
PERSON_CONTEXT_OBSERVATION_BBOX_SOURCE = "person_bbox_observations.person_bbox"
PERSON_CONTEXT_BBOX_COLOR = "#00C853"
INTRUSION_BBOX_COLOR = "#FF6D00"
ALLOWED_FACE_BBOX_SOURCES = frozenset(
    {
        "observation.face_bbox",
        "payload.face_bbox",
        "payload.overlay.face_bbox",
    }
)
FORBIDDEN_FACE_BBOX_SOURCE_FRAGMENTS = (
    "person_bbox",
    "person_bbox_observations",
    "payload.bbox",
)
DEFAULT_FRAME_WIDTH = 1920.0
DEFAULT_FRAME_HEIGHT = 1080.0
DEFAULT_FACE_OBSERVATION_CREATED_AT_MARGIN_SECONDS = 15.0


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    parsed = _to_float(value, None)
    return parsed if parsed is not None else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _format_datetime(value: Any) -> str | None:
    dt = _parse_datetime(value)
    return dt.isoformat() if dt is not None else None


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _payload_media(payload: Any) -> dict[str, Any]:
    payload_dict = _as_dict(payload)
    media = payload_dict.get("media")
    return media if isinstance(media, dict) else {}


def _clip_window(event_context: dict[str, Any]) -> tuple[int, int, float, float]:
    payload = _as_dict(event_context.get("payload"))
    media = _payload_media(payload)
    policy = _as_dict(event_context.get("evidence_policy"))
    pre_seconds = _to_float(
        policy.get("pre_seconds", media.get("pre_seconds")),
        DEFAULT_PRE_SECONDS,
    )
    post_seconds = _to_float(
        policy.get("post_seconds", media.get("post_seconds")),
        DEFAULT_POST_SECONDS,
    )
    pre_seconds = pre_seconds if pre_seconds is not None else DEFAULT_PRE_SECONDS
    post_seconds = (
        post_seconds if post_seconds is not None else DEFAULT_POST_SECONDS
    )
    event_ts_ms = _to_int(event_context.get("event_ts_ms"))
    start_ts_ms = event_ts_ms - int(pre_seconds * 1000)
    end_ts_ms = event_ts_ms + int(post_seconds * 1000)
    return start_ts_ms, end_ts_ms, pre_seconds, post_seconds


def _duration_target_seconds(pre_seconds: float, post_seconds: float) -> float:
    return float(pre_seconds) + float(post_seconds)


def _effective_window_fields(
    pre_seconds: float,
    post_seconds: float,
) -> dict[str, float]:
    return {
        "effective_pre_seconds": float(pre_seconds),
        "effective_post_seconds": float(post_seconds),
        "duration_target_seconds": _duration_target_seconds(
            pre_seconds,
            post_seconds,
        ),
    }


def _pts_like_to_ms(value: Any) -> int | None:
    parsed = _to_float(value, None)
    if parsed is None:
        return None
    # Savant frame pts/dts/duration values are nanoseconds. Explicit
    # timestamp_ms fields are handled elsewhere and are not passed here.
    if abs(parsed) > 10_000_000:
        return int(parsed / 1_000_000)
    return int(parsed)


def _duration_like_to_ms(value: Any) -> int | None:
    parsed = _to_float(value, None)
    if parsed is None or parsed <= 0:
        return None
    if parsed > 10_000:
        return int(parsed / 1_000_000)
    return int(parsed)


def _metadata_timestamp_ms(record: dict[str, Any]) -> tuple[int, str] | tuple[None, None]:
    scopes: list[dict[str, Any]] = [record]
    for key in ("metadata", "tags", "payload"):
        value = record.get(key)
        if isinstance(value, dict):
            scopes.append(value)

    for scope in scopes:
        for key in (
            "source_timestamp_ms",
            "timestamp_ms",
            "original_timestamp_ms",
            "event_ts_ms",
        ):
            value = scope.get(key)
            if value is not None:
                return _to_int(value), f"sink_metadata_{key}"

    for scope in scopes:
        for key in ("source_pts", "frame_pts", "pts"):
            value = scope.get(key)
            pts_ms = _pts_like_to_ms(value)
            if pts_ms is not None:
                return pts_ms, f"sink_metadata_first_{key}"
    return None, None


def _metadata_duration_ms(records: list[dict[str, Any]]) -> int | None:
    timestamps: list[int] = []
    max_frame_duration_ms = 0
    for record in records:
        timestamp_ms, _source = _metadata_timestamp_ms(record)
        if timestamp_ms is not None:
            timestamps.append(timestamp_ms)
        duration_ms = _duration_like_to_ms(record.get("duration"))
        if duration_ms is not None:
            max_frame_duration_ms = max(max_frame_duration_ms, duration_ms)
    if not timestamps:
        return None
    duration_ms = max(timestamps) - min(timestamps) + max_frame_duration_ms
    return duration_ms if duration_ms > 0 else None


def _payload_clip_bounds(media: dict[str, Any]) -> tuple[int | None, int | None, str]:
    for start_key, end_key, source in (
        ("actual_clip_start_ts_ms", "actual_clip_end_ts_ms", "replay_job_metadata"),
        ("clip_start_ts_ms", "clip_end_ts_ms", "record_request"),
        ("start_ts_ms", "end_ts_ms", "record_request"),
    ):
        start = media.get(start_key)
        if start is None:
            continue
        start_ts_ms = _to_int(start)
        end_ts_ms = _to_int(media.get(end_key), 0) if media.get(end_key) is not None else None
        return start_ts_ms, end_ts_ms, source
    return None, None, ""


def _time_anchor_is_plausible(
    *,
    event_ts_ms: int,
    start_ts_ms: int,
    duration_ms: int,
    tolerance_ms: int = 250,
) -> bool:
    if event_ts_ms <= 0 or duration_ms <= 0:
        return True
    event_offset_ms = event_ts_ms - start_ts_ms
    return -tolerance_ms <= event_offset_ms <= duration_ms + tolerance_ms


def _resolve_time_anchor(
    event_context: dict[str, Any],
    *,
    replay_metadata_path: str | Path | None = None,
) -> dict[str, Any]:
    ideal_start_ts_ms, ideal_end_ts_ms, pre_seconds, post_seconds = _clip_window(
        event_context
    )
    target_duration_ms = int(_duration_target_seconds(pre_seconds, post_seconds) * 1000)
    event_ts_ms = _to_int(event_context.get("event_ts_ms"))
    records = _jsonl_records(replay_metadata_path)
    rejected_source = ""
    rejected_start_ts_ms: int | None = None

    if records:
        first_ts_ms, source = _metadata_timestamp_ms(records[0])
        duration_ms = _metadata_duration_ms(records) or target_duration_ms
        if first_ts_ms is not None and source:
            if _time_anchor_is_plausible(
                event_ts_ms=event_ts_ms,
                start_ts_ms=first_ts_ms,
                duration_ms=duration_ms,
            ):
                return {
                    "actual_clip_start_ts_ms": first_ts_ms,
                    "actual_clip_end_ts_ms": first_ts_ms + duration_ms,
                    "actual_clip_duration_ms": duration_ms,
                    "clip_start_source": source,
                    "time_alignment_status": "exact",
                    "event_offset_in_clip_ms": event_ts_ms - first_ts_ms,
                }
            rejected_source = source
            rejected_start_ts_ms = first_ts_ms

    payload = _as_dict(event_context.get("payload"))
    media = _payload_media(payload)
    payload_start, payload_end, payload_source = _payload_clip_bounds(media)
    if payload_start is not None:
        duration_ms = (
            max(0, payload_end - payload_start)
            if payload_end is not None
            else target_duration_ms
        )
        return {
            "actual_clip_start_ts_ms": payload_start,
            "actual_clip_end_ts_ms": payload_start + duration_ms,
            "actual_clip_duration_ms": duration_ms,
            "clip_start_source": payload_source or "record_request",
            "time_alignment_status": "exact",
            "event_offset_in_clip_ms": event_ts_ms - payload_start,
        }

    anchor = {
        "actual_clip_start_ts_ms": ideal_start_ts_ms,
        "actual_clip_end_ts_ms": ideal_end_ts_ms,
        "actual_clip_duration_ms": max(0, ideal_end_ts_ms - ideal_start_ts_ms),
        "clip_start_source": "estimated_event_minus_pre",
        "time_alignment_status": "estimated",
        "event_offset_in_clip_ms": event_ts_ms - ideal_start_ts_ms,
    }
    if rejected_source:
        anchor["rejected_clip_start_source"] = rejected_source
        anchor["rejected_clip_start_ts_ms"] = rejected_start_ts_ms
        anchor["rejected_reason"] = "sink_metadata_not_in_event_timestamp_timeline"
    return anchor


def _created_at_clip_window(
    event_context: dict[str, Any],
) -> tuple[datetime, datetime] | tuple[None, None]:
    created_at = _parse_datetime(event_context.get("created_at"))
    if created_at is None:
        return None, None
    _start_ts_ms, _end_ts_ms, pre_seconds, post_seconds = _clip_window(event_context)
    return (
        created_at - timedelta(seconds=pre_seconds),
        created_at + timedelta(seconds=post_seconds),
    )


def _face_observation_created_at_window(
    event_context: dict[str, Any],
) -> tuple[datetime | None, datetime | None, float]:
    created_at = _parse_datetime(event_context.get("created_at"))
    margin_seconds = max(
        0.0,
        _env_float(
            "FACE_OBSERVATION_CREATED_AT_MARGIN_SECONDS",
            DEFAULT_FACE_OBSERVATION_CREATED_AT_MARGIN_SECONDS,
        ),
    )
    if created_at is None:
        return None, None, margin_seconds
    _start_ts_ms, _end_ts_ms, pre_seconds, post_seconds = _clip_window(event_context)
    return (
        created_at - timedelta(seconds=pre_seconds + margin_seconds),
        created_at + timedelta(seconds=post_seconds + margin_seconds),
        margin_seconds,
    )


def _normalise_bbox(face_bbox: Any, confidence: Any) -> dict[str, Any] | None:
    default_format = "cxcywh" if isinstance(face_bbox, list) else None
    values, fmt = _bbox_values_and_format(face_bbox, source_format=default_format)
    if values is None or fmt is None:
        return None
    if fmt not in {"cxcywh", "xywh", "xyxy"}:
        return None

    conf = confidence
    if isinstance(face_bbox, dict):
        conf = face_bbox.get("confidence", confidence)
    xyxy = _bbox_to_xyxy({"format": fmt, "values": values})
    if xyxy is None:
        return None
    return {
        "format": fmt,
        "values": values,
        "xyxy": xyxy,
        "confidence": _to_float(conf, 0.0),
    }


def _payload_person_bbox(payload: dict[str, Any]) -> tuple[Any, str] | tuple[None, None]:
    if payload.get("person_bbox") is not None:
        return payload.get("person_bbox"), "payload.person_bbox"
    if payload.get("bbox") is not None:
        return payload.get("bbox"), "payload.bbox"
    return None, None


def _is_allowed_face_bbox_source(source: Any) -> bool:
    text = str(source or "")
    if not text:
        return False
    if text not in ALLOWED_FACE_BBOX_SOURCES:
        return False
    lowered = text.lower()
    return not any(fragment in lowered for fragment in FORBIDDEN_FACE_BBOX_SOURCE_FRAGMENTS)


def _face_bbox_is_large(
    bbox: dict[str, Any] | None,
    *,
    frame_width: float = DEFAULT_FRAME_WIDTH,
    frame_height: float = DEFAULT_FRAME_HEIGHT,
) -> bool:
    if not isinstance(bbox, dict):
        return False
    xyxy = _bbox_to_xyxy(bbox, source_format=str(bbox.get("format") or ""))
    if xyxy is None:
        return False
    x1, y1, x2, y2 = xyxy
    width = max(0.0, x2 - x1)
    height = max(0.0, y2 - y1)
    frame_area = max(1.0, frame_width * frame_height)
    area_ratio = width * height / frame_area
    return (
        width >= frame_width * 0.45
        or height >= frame_height * 0.65
        or area_ratio >= 0.20
    )


def _bbox_values_and_format(
    bbox: Any,
    *,
    source_format: str | None = None,
) -> tuple[list[float], str] | tuple[None, None]:
    values: Any = None
    fmt = source_format

    if isinstance(bbox, dict):
        fmt = bbox.get("format") or bbox.get("bbox_format") or fmt
        if bbox.get("xyxy") is not None:
            values = bbox.get("xyxy")
            fmt = "xyxy"
        elif bbox.get("values") is not None:
            values = bbox.get("values")
        elif bbox.get("bbox") is not None:
            values = bbox.get("bbox")
        elif all(key in bbox for key in ("x", "y", "width", "height")):
            values = [
                bbox.get("x"),
                bbox.get("y"),
                bbox.get("width"),
                bbox.get("height"),
            ]
            fmt = fmt or "xywh"
        elif all(key in bbox for key in ("xc", "yc", "width", "height")):
            values = [
                bbox.get("xc"),
                bbox.get("yc"),
                bbox.get("width"),
                bbox.get("height"),
            ]
            fmt = fmt or "cxcywh"
        elif all(key in bbox for key in ("x1", "y1", "x2", "y2")):
            values = [
                bbox.get("x1"),
                bbox.get("y1"),
                bbox.get("x2"),
                bbox.get("y2"),
            ]
            fmt = fmt or "xyxy"
    else:
        values = bbox

    if not isinstance(values, list) or len(values) < 4:
        return None, None

    numbers = [_to_float(item) for item in values[:4]]
    if any(item is None for item in numbers):
        return None, None
    return [float(item) for item in numbers if item is not None], (fmt or "xyxy")


def _bbox_to_xyxy(
    bbox: Any,
    *,
    source_format: str | None = None,
) -> list[float] | None:
    values, fmt = _bbox_values_and_format(bbox, source_format=source_format)
    if values is None or fmt is None:
        return None
    if fmt == "xywh":
        x, y, width, height = values
        return [x, y, x + width, y + height]
    if fmt == "cxcywh":
        cx, cy, width, height = values
        return [cx - width / 2.0, cy - height / 2.0, cx + width / 2.0, cy + height / 2.0]
    if fmt == "xyxy":
        return values
    return values


def _intrusion_mvp_annotation_record(
    event_context: dict[str, Any],
    *,
    start_ts_ms: int,
    time_alignment_status: str = "estimated",
) -> dict[str, Any] | None:
    payload = _as_dict(event_context.get("payload"))
    raw_bbox, bbox_source = _payload_person_bbox(payload)
    if raw_bbox is None or bbox_source is None:
        return None

    source_format = (
        payload.get("person_bbox_format")
        if bbox_source == "payload.person_bbox"
        else payload.get("bbox_format")
    )
    xyxy = _bbox_to_xyxy(raw_bbox, source_format=source_format)
    if xyxy is None:
        return None

    media = _payload_media(payload)
    timestamp_ms = _to_int(event_context.get("event_ts_ms"))
    return {
        "schema_version": INTRUSION_ANNOTATION_SCHEMA_VERSION,
        "record_type": "object_annotation",
        "annotation_role": "behavior_event",
        "annotation_status": ANNOTATION_STATUS_COMPLETE,
        "overlay_available": True,
        "frontend_overlay_required": True,
        "event_id": event_context.get("event_id", ""),
        "event_type": "intrusion",
        "source_id": event_context.get("source_id", ""),
        "camera_id": event_context.get("camera_id", ""),
        "timestamp_ms": timestamp_ms,
        "time_offset_ms": timestamp_ms - start_ts_ms,
        "time_alignment_status": time_alignment_status,
        "frame_num": media.get("frame_num"),
        "frame_uuid": media.get("frame_uuid") or event_context.get("frame_uuid"),
        "keyframe_uuid": media.get("keyframe_uuid") or event_context.get("keyframe_uuid"),
        "track_id": str(event_context.get("track_id") or ""),
        "object_type": "person",
        "label": {
            "kind": "behavior_event",
            "text": "Intrusion",
        },
        "action": {
            "event_type": "intrusion",
            "status": "event_triggered",
        },
        "bbox": {
            "format": "xyxy",
            "xyxy": xyxy,
            "source": bbox_source,
            "coordinate_space": "pixel",
        },
        "pose": {
            "status": "unavailable",
            "reason": "not_present_in_event_payload",
        },
        "style": {
            "priority": "warning",
            "reason": "behavior.intrusion",
            "bbox_color": INTRUSION_BBOX_COLOR,
            "color": INTRUSION_BBOX_COLOR,
        },
    }


def _person_bbox_gate_config() -> dict[str, float]:
    return {
        "min_person_confidence": _env_float("POSE_CONFIDENCE_THRESHOLD", 0.25),
        "min_person_width": _env_float("POSE_MIN_WIDTH", 20.0),
        "min_person_height": _env_float("POSE_MIN_HEIGHT", 40.0),
        "max_bbox_area_ratio": _env_float("INTRUSION_MAX_BBOX_AREA_RATIO", 0.9),
        "min_bbox_aspect_ratio": _env_float("INTRUSION_MIN_BBOX_ASPECT_RATIO", 0.1),
        "max_bbox_aspect_ratio": _env_float("INTRUSION_MAX_BBOX_ASPECT_RATIO", 4.0),
    }


def _person_gate_decision(
    *,
    xyxy: list[float] | None,
    confidence: float | None,
    frame_width: float | None,
    frame_height: float | None,
    gate: dict[str, float] | None = None,
) -> tuple[bool, str, list[float] | None]:
    gate = gate or _person_bbox_gate_config()
    if confidence is None or confidence < gate["min_person_confidence"]:
        return False, "low_confidence", xyxy
    if xyxy is None:
        return False, "invalid_bbox", None

    x1, y1, x2, y2 = xyxy
    if x2 <= x1 or y2 <= y1:
        return False, "invalid_bbox", xyxy

    if frame_width and frame_height:
        x1 = min(max(0.0, x1), float(frame_width))
        y1 = min(max(0.0, y1), float(frame_height))
        x2 = min(max(0.0, x2), float(frame_width))
        y2 = min(max(0.0, y2), float(frame_height))
        if x2 <= x1 or y2 <= y1:
            return False, "invalid_bbox", [x1, y1, x2, y2]

    width = x2 - x1
    height = y2 - y1
    if width < gate["min_person_width"] or height < gate["min_person_height"]:
        return False, "small_bbox", [x1, y1, x2, y2]

    aspect_ratio = width / height if height > 0 else 0.0
    if (
        aspect_ratio < gate["min_bbox_aspect_ratio"]
        or aspect_ratio > gate["max_bbox_aspect_ratio"]
    ):
        return False, "aspect_ratio", [x1, y1, x2, y2]

    if frame_width and frame_height:
        frame_area = float(frame_width) * float(frame_height)
        if frame_area > 0 and (width * height / frame_area) > gate["max_bbox_area_ratio"]:
            return False, "invalid_bbox", [x1, y1, x2, y2]

    return True, "accepted", [x1, y1, x2, y2]


def _jsonl_records(path: str | Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    records: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                records.append(data)
    return records


def _is_person_metadata_object(obj: dict[str, Any]) -> bool:
    text = " ".join(
        str(obj.get(key) or "")
        for key in ("label", "class_name", "object_type", "element_name")
    ).lower()
    return "person" in text or obj.get("class_id") == 0


def _person_object_confidence(obj: dict[str, Any]) -> float | None:
    for key in ("confidence", "score", "detector_confidence", "person_confidence"):
        value = _to_float(obj.get(key), None)
        if value is not None:
            return value
    bbox = obj.get("bbox")
    if isinstance(bbox, dict):
        value = _to_float(bbox.get("confidence"), None)
        if value is not None:
            return value
    return None


def _person_object_track_id(obj: dict[str, Any]) -> str:
    for key in ("track_id", "object_id", "id"):
        value = obj.get(key)
        if value not in (None, "", "None"):
            return str(value)
    attributes = obj.get("attributes")
    if isinstance(attributes, dict):
        for key in ("track_id", "object_id"):
            value = attributes.get(key)
            if value not in (None, "", "None"):
                return str(value)
    return ""


def _person_object_bbox(obj: dict[str, Any]) -> tuple[list[float] | None, str | None]:
    raw_bbox = obj.get("bbox") or obj.get("box") or obj.get("person_bbox")
    source_format = obj.get("bbox_format") or obj.get("format")
    if raw_bbox is None and any(key in obj for key in ("xyxy", "xywh", "cxcywh")):
        raw_bbox = obj
    xyxy = _bbox_to_xyxy(raw_bbox, source_format=source_format)
    _, fmt = _bbox_values_and_format(raw_bbox, source_format=source_format)
    return xyxy, fmt


def build_person_context_annotation(
    *,
    event_context: dict[str, Any],
    frame_record: dict[str, Any],
    obj: dict[str, Any],
    xyxy: list[float],
    confidence: float | None,
    start_ts_ms: int,
    first_frame_pts: int | None = None,
    bbox_source: str = PERSON_CONTEXT_BBOX_SOURCE,
    time_alignment_status: str = "estimated",
) -> dict[str, Any] | None:
    frame_pts = frame_record.get("pts") or frame_record.get("frame_pts")
    time_offset_ms = _to_int(frame_record.get("time_offset_ms"), 0) if (
        frame_record.get("time_offset_ms") is not None
    ) else None
    timestamp_ms = _to_int(frame_record.get("timestamp_ms"), 0)
    if timestamp_ms > 0 and time_offset_ms is None:
        time_offset_ms = timestamp_ms - start_ts_ms
    if frame_pts is not None and first_frame_pts is not None:
        try:
            time_offset_ms = int((int(frame_pts) - int(first_frame_pts)) / 1_000_000)
        except (TypeError, ValueError):
            time_offset_ms = None
    if timestamp_ms <= 0 and time_offset_ms is not None:
        timestamp_ms = start_ts_ms + time_offset_ms
    if frame_pts is None and time_offset_ms is None:
        return None

    record = {
        "schema_version": INTRUSION_ANNOTATION_SCHEMA_VERSION,
        "record_type": "object_annotation",
        "annotation_role": "person_context",
        "annotation_status": ANNOTATION_STATUS_COMPLETE,
        "overlay_available": True,
        "frontend_overlay_required": True,
        "event_id": event_context.get("event_id", ""),
        "event_type": event_context.get("event_type", ""),
        "source_id": event_context.get("source_id", ""),
        "camera_id": event_context.get("camera_id", ""),
        "timestamp_ms": timestamp_ms,
        "time_offset_ms": time_offset_ms,
        "time_alignment_status": time_alignment_status,
        "frame_pts": frame_pts,
        "frame_num": frame_record.get("frame_num"),
        "track_id": _person_object_track_id(obj),
        "object_type": "person",
        "label": {
            "kind": "object_detection",
            "text": "Person",
        },
        "action": {
            "status": "none",
            "event_type": None,
        },
        "bbox": {
            "format": "xyxy",
            "xyxy": xyxy,
            "source": bbox_source,
            "coordinate_space": "pixel",
        },
        "detection": {
            "confidence": confidence,
        },
        "gate": {
            "status": "accepted",
        },
        "style": {
            "priority": "context",
            "reason": "person_detection",
            "bbox_color": PERSON_CONTEXT_BBOX_COLOR,
            "color": PERSON_CONTEXT_BBOX_COLOR,
        },
    }
    return {key: value for key, value in record.items() if value is not None}


def extract_person_bbox_timeline(
    *,
    replay_metadata_path: str | Path | None,
    event_context: dict[str, Any],
    start_ts_ms: int,
    time_alignment_status: str = "estimated",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = _jsonl_records(replay_metadata_path)
    gate = _person_bbox_gate_config()
    stats: dict[str, Any] = {
        "replay_metadata_path": str(replay_metadata_path or ""),
        "replay_metadata_available": bool(records),
        "frame_metadata_count": len(records),
        "frame_metadata_with_objects_count": 0,
        "person_metadata_count": 0,
        "person_bbox_metadata_count": 0,
        "person_confidence_metadata_count": 0,
        "person_track_id_metadata_count": 0,
        "person_frame_pts_metadata_count": 0,
        "person_context_filtered_low_confidence_count": 0,
        "person_context_filtered_small_bbox_count": 0,
        "person_context_filtered_invalid_bbox_count": 0,
        "person_context_filtered_aspect_ratio_count": 0,
        "person_bbox_format_distribution": {},
        "person_bbox_source_distribution": {},
        "person_bbox_gate": {
            "min_person_confidence": gate["min_person_confidence"],
            "min_person_width": gate["min_person_width"],
            "min_person_height": gate["min_person_height"],
            "max_bbox_area_ratio": gate["max_bbox_area_ratio"],
        },
    }
    annotations: list[dict[str, Any]] = []
    first_frame_pts = None
    for record in records:
        pts = record.get("pts") or record.get("frame_pts")
        if pts is not None and first_frame_pts is None:
            try:
                first_frame_pts = int(pts)
            except (TypeError, ValueError):
                pass

    format_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for frame in records:
        objects = _as_dict(frame.get("metadata")).get("objects")
        if not isinstance(objects, list):
            objects = []
        if objects:
            stats["frame_metadata_with_objects_count"] += 1
        frame_width = _to_float(frame.get("width"), None)
        frame_height = _to_float(frame.get("height"), None)
        frame_has_person_pts = frame.get("pts") is not None or frame.get("frame_pts") is not None

        for obj in objects:
            if not isinstance(obj, dict) or not _is_person_metadata_object(obj):
                continue
            stats["person_metadata_count"] += 1
            if frame_has_person_pts:
                stats["person_frame_pts_metadata_count"] += 1
            confidence = _person_object_confidence(obj)
            if confidence is not None:
                stats["person_confidence_metadata_count"] += 1
            if _person_object_track_id(obj):
                stats["person_track_id_metadata_count"] += 1
            xyxy, fmt = _person_object_bbox(obj)
            if fmt:
                format_counts[fmt] = format_counts.get(fmt, 0) + 1
            if xyxy is not None:
                stats["person_bbox_metadata_count"] += 1
            accepted, reason, clamped_xyxy = _person_gate_decision(
                xyxy=xyxy,
                confidence=confidence,
                frame_width=frame_width,
                frame_height=frame_height,
                gate=gate,
            )
            if not accepted:
                key = f"person_context_filtered_{reason}_count"
                if key not in stats:
                    key = "person_context_filtered_invalid_bbox_count"
                stats[key] += 1
                continue
            annotation = build_person_context_annotation(
                event_context=event_context,
                frame_record=frame,
                obj=obj,
                xyxy=clamped_xyxy or xyxy or [],
                confidence=confidence,
                start_ts_ms=start_ts_ms,
                first_frame_pts=first_frame_pts,
                time_alignment_status=time_alignment_status,
            )
            if annotation is None:
                stats["person_context_filtered_invalid_bbox_count"] += 1
                continue
            annotations.append(annotation)
            source = annotation["bbox"]["source"]
            source_counts[source] = source_counts.get(source, 0) + 1

    stats["person_bbox_format_distribution"] = format_counts
    stats["person_bbox_source_distribution"] = source_counts
    return annotations, stats


def _load_person_bbox_observations(
    pg_conn: psycopg.Connection,
    *,
    source_id: str,
    camera_id: str,
    start_ts_ms: int,
    end_ts_ms: int,
) -> list[dict[str, Any]]:
    if not source_id or not camera_id:
        return []
    try:
        with pg_conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, source_observation_id, source_id, camera_id, track_id,
                       timestamp_ms, frame_pts, frame_num, person_bbox,
                       person_confidence, gate_status, payload, created_at,
                       NULL::bigint AS time_offset_ms,
                       'timestamp_ms'::text AS time_basis
                FROM person_bbox_observations
                WHERE source_id = %(source_id)s
                  AND camera_id = %(camera_id)s
                  AND timestamp_ms BETWEEN %(start_ts_ms)s AND %(end_ts_ms)s
                  AND gate_status = 'accepted'
                ORDER BY timestamp_ms ASC, source_observation_id ASC
                """,
                {
                    "source_id": source_id,
                    "camera_id": camera_id,
                    "start_ts_ms": start_ts_ms,
                    "end_ts_ms": end_ts_ms,
                },
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception(
            "person bbox observation lookup failed source_id=%s camera_id=%s",
            source_id,
            camera_id,
        )
        return []


def _load_person_bbox_observations_by_created_at(
    pg_conn: psycopg.Connection,
    *,
    source_id: str,
    camera_id: str,
    start_created_at: datetime,
    end_created_at: datetime,
) -> list[dict[str, Any]]:
    if not source_id or not camera_id:
        return []
    try:
        with pg_conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, source_observation_id, source_id, camera_id, track_id,
                       timestamp_ms, frame_pts, frame_num, person_bbox,
                       person_confidence, gate_status, payload, created_at,
                       FLOOR(
                           EXTRACT(EPOCH FROM (
                               created_at - %(start_created_at)s::timestamptz
                           )) * 1000
                       )::bigint AS time_offset_ms,
                       'created_at'::text AS time_basis
                FROM person_bbox_observations
                WHERE source_id = %(source_id)s
                  AND camera_id = %(camera_id)s
                  AND created_at BETWEEN %(start_created_at)s::timestamptz
                                      AND %(end_created_at)s::timestamptz
                  AND gate_status = 'accepted'
                ORDER BY created_at ASC, source_observation_id ASC
                """,
                {
                    "source_id": source_id,
                    "camera_id": camera_id,
                    "start_created_at": start_created_at.isoformat(),
                    "end_created_at": end_created_at.isoformat(),
                },
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception(
            "person bbox observation created_at lookup failed source_id=%s camera_id=%s",
            source_id,
            camera_id,
        )
        return []


def extract_person_bbox_timeline_from_observations(
    *,
    observations: list[dict[str, Any]],
    event_context: dict[str, Any],
    start_ts_ms: int,
    time_alignment_status: str = "estimated",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    gate = _person_bbox_gate_config()
    stats: dict[str, Any] = {
        "person_observation_count": len(observations),
        "person_observation_bbox_count": 0,
        "person_observation_confidence_count": 0,
        "person_observation_track_id_count": 0,
        "person_observation_time_anchor_count": 0,
        "person_context_filtered_low_confidence_count": 0,
        "person_context_filtered_small_bbox_count": 0,
        "person_context_filtered_invalid_bbox_count": 0,
        "person_context_filtered_aspect_ratio_count": 0,
        "person_bbox_format_distribution": {},
        "person_bbox_source_distribution": {},
        "person_bbox_gate": {
            "min_person_confidence": gate["min_person_confidence"],
            "min_person_width": gate["min_person_width"],
            "min_person_height": gate["min_person_height"],
            "max_bbox_area_ratio": gate["max_bbox_area_ratio"],
        },
    }
    annotations: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    format_counts: dict[str, int] = {}

    for obs in observations:
        raw_bbox = obs.get("person_bbox")
        xyxy = _bbox_to_xyxy(raw_bbox, source_format="xyxy")
        _, fmt = _bbox_values_and_format(raw_bbox, source_format="xyxy")
        if fmt:
            format_counts[fmt] = format_counts.get(fmt, 0) + 1
        if xyxy is not None:
            stats["person_observation_bbox_count"] += 1
        confidence = _to_float(obs.get("person_confidence"), None)
        if confidence is not None:
            stats["person_observation_confidence_count"] += 1
        if obs.get("track_id"):
            stats["person_observation_track_id_count"] += 1
        if obs.get("frame_pts") is not None or obs.get("timestamp_ms") is not None:
            stats["person_observation_time_anchor_count"] += 1

        accepted, reason, clamped_xyxy = _person_gate_decision(
            xyxy=xyxy,
            confidence=confidence,
            frame_width=None,
            frame_height=None,
            gate=gate,
        )
        if not accepted:
            key = f"person_context_filtered_{reason}_count"
            if key not in stats:
                key = "person_context_filtered_invalid_bbox_count"
            stats[key] += 1
            continue

        frame_record = {
            "timestamp_ms": (
                None if obs.get("time_basis") == "created_at" else obs.get("timestamp_ms")
            ),
            "frame_pts": obs.get("frame_pts"),
            "frame_num": obs.get("frame_num"),
            "time_offset_ms": obs.get("time_offset_ms"),
        }
        obj = {
            "label": "person",
            "track_id": obs.get("track_id"),
            "confidence": confidence,
            "bbox": {
                "format": "xyxy",
                "xyxy": clamped_xyxy or xyxy or [],
            },
        }
        annotation = build_person_context_annotation(
            event_context=event_context,
            frame_record=frame_record,
            obj=obj,
            xyxy=clamped_xyxy or xyxy or [],
            confidence=confidence,
            start_ts_ms=start_ts_ms,
            first_frame_pts=None,
            bbox_source=PERSON_CONTEXT_OBSERVATION_BBOX_SOURCE,
            time_alignment_status=time_alignment_status,
        )
        if annotation is None:
            stats["person_context_filtered_invalid_bbox_count"] += 1
            continue
        annotation["source_observation_id"] = obs.get("source_observation_id", "")
        annotations.append(annotation)
        source = annotation["bbox"]["source"]
        source_counts[source] = source_counts.get(source, 0) + 1

    stats["person_bbox_format_distribution"] = format_counts
    stats["person_bbox_source_distribution"] = source_counts
    return annotations, stats


def extract_person_bbox_timeline_from_db(
    pg_conn: psycopg.Connection,
    *,
    event_context: dict[str, Any],
    start_ts_ms: int,
    end_ts_ms: int,
    time_alignment_status: str = "estimated",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    observations = _load_person_bbox_observations(
        pg_conn,
        source_id=str(event_context.get("source_id") or ""),
        camera_id=str(event_context.get("camera_id") or ""),
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
    )
    lookup_mode = "timestamp_ms"
    if not observations:
        start_created_at, end_created_at = _created_at_clip_window(event_context)
        if start_created_at is not None and end_created_at is not None:
            observations = _load_person_bbox_observations_by_created_at(
                pg_conn,
                source_id=str(event_context.get("source_id") or ""),
                camera_id=str(event_context.get("camera_id") or ""),
                start_created_at=start_created_at,
                end_created_at=end_created_at,
            )
            if observations:
                lookup_mode = "created_at_fallback"
    lines, stats = extract_person_bbox_timeline_from_observations(
        observations=observations,
        event_context=event_context,
        start_ts_ms=start_ts_ms,
        time_alignment_status=time_alignment_status,
    )
    stats["person_observation_lookup_mode"] = lookup_mode
    return lines, stats


def _normalise_landmarks(landmarks: Any) -> dict[str, Any]:
    points: list[Any] = []
    if isinstance(landmarks, list):
        if landmarks and all(isinstance(item, (int, float)) for item in landmarks):
            coords = [float(item) for item in landmarks]
            points = [
                [coords[i], coords[i + 1]]
                for i in range(0, min(len(coords) - 1, 10), 2)
            ]
        elif landmarks and all(isinstance(item, list) for item in landmarks):
            points = landmarks[:5]
    return {
        "format": "5_point",
        "points": _jsonable(points),
    }


def _load_observations(
    pg_conn: psycopg.Connection,
    *,
    source_id: str,
    start_ts_ms: int,
    end_ts_ms: int,
    camera_id: str = "",
    event_context: dict[str, Any] | None = None,
    return_audit: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any]]:
    event_context = event_context or {}
    event_created_at = _parse_datetime(event_context.get("created_at"))
    created_at_from, created_at_to, created_at_margin_seconds = (
        _face_observation_created_at_window(event_context)
    )
    audit: dict[str, Any] = {
        "mode": "timestamp_and_created_at",
        "source_id": source_id,
        "camera_id": camera_id,
        "timestamp_from_ms": start_ts_ms,
        "timestamp_to_ms": end_ts_ms,
        "created_at_from": _format_datetime(created_at_from),
        "created_at_to": _format_datetime(created_at_to),
        "created_at_margin_seconds": created_at_margin_seconds,
        "db_result_count": 0,
        "db_result_created_at_min": None,
        "db_result_created_at_max": None,
        "cross_day_result_count": 0,
        "timestamp_only_fallback_used": False,
        "camera_id_fallback_used": False,
        "allow_timestamp_only_query": _env_bool(
            "ALLOW_FACE_TIMESTAMP_ONLY_QUERY", False
        ),
    }

    def finish(rows: list[dict[str, Any]], mode: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        audit["mode"] = mode
        audit["db_result_count"] = len(rows)
        created_values = [
            dt
            for dt in (_parse_datetime(row.get("created_at")) for row in rows)
            if dt is not None
        ]
        if created_values:
            audit["db_result_created_at_min"] = min(created_values).isoformat()
            audit["db_result_created_at_max"] = max(created_values).isoformat()
        if event_created_at is not None:
            event_date = event_created_at.date()
            audit["cross_day_result_count"] = sum(
                1 for dt in created_values if dt.date() != event_date
            )
        return rows, audit

    if not source_id:
        rows, audit = finish([], "missing_source_id")
        return (rows, audit) if return_audit else rows

    if created_at_from is None or created_at_to is None:
        if not audit["allow_timestamp_only_query"]:
            rows, audit = finish([], "missing_created_at_anchor")
            return (rows, audit) if return_audit else rows
    try:
        with pg_conn.cursor(row_factory=dict_row) as cur:
            select_sql = """
                SELECT id, source_observation_id, camera_id, source_id, track_id,
                       timestamp_ms, frame_num, face_bbox, landmarks,
                       face_confidence, quality, detector_model, embedding_model,
                       embedding_dim, embedding_norm, payload, created_at
                FROM face_observations
            """
            order_sql = " ORDER BY timestamp_ms ASC, source_observation_id ASC"
            params: dict[str, Any] = {
                "source_id": source_id,
                "camera_id": camera_id,
                "start_ts_ms": start_ts_ms,
                "end_ts_ms": end_ts_ms,
                "created_at_from": (
                    created_at_from.isoformat()
                    if created_at_from is not None
                    else None
                ),
                "created_at_to": (
                    created_at_to.isoformat() if created_at_to is not None else None
                ),
            }

            def execute(where_sql: str, extra_params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
                query_params = dict(params)
                if extra_params:
                    query_params.update(extra_params)
                cur.execute(select_sql + where_sql + order_sql, query_params)
                return [dict(row) for row in cur.fetchall()]

            camera_sql = " AND camera_id = %(camera_id)s" if camera_id else ""
            created_rows: list[dict[str, Any]] = []
            if created_at_from is not None and created_at_to is not None:
                primary_rows = execute(
                    f"""
                    WHERE source_id = %(source_id)s
                      {camera_sql}
                      AND timestamp_ms BETWEEN %(start_ts_ms)s AND %(end_ts_ms)s
                      AND created_at BETWEEN %(created_at_from)s::timestamptz
                                          AND %(created_at_to)s::timestamptz
                    """
                )
                if primary_rows:
                    rows, audit = finish(primary_rows, "timestamp_and_created_at")
                    return (rows, audit) if return_audit else rows

                if camera_id:
                    camera_null_rows = execute(
                        """
                        WHERE source_id = %(source_id)s
                          AND (camera_id IS NULL OR camera_id = '')
                          AND timestamp_ms BETWEEN %(start_ts_ms)s AND %(end_ts_ms)s
                          AND created_at BETWEEN %(created_at_from)s::timestamptz
                                              AND %(created_at_to)s::timestamptz
                        """
                    )
                    if camera_null_rows:
                        audit["camera_id_fallback_used"] = True
                        rows, audit = finish(
                            camera_null_rows,
                            "timestamp_and_created_at",
                        )
                        return (rows, audit) if return_audit else rows

                created_rows = execute(
                    f"""
                    WHERE source_id = %(source_id)s
                      {camera_sql}
                      AND created_at BETWEEN %(created_at_from)s::timestamptz
                                          AND %(created_at_to)s::timestamptz
                    """
                )
                if created_rows:
                    rows, audit = finish(created_rows, "created_at_fallback")
                    return (rows, audit) if return_audit else rows

                if camera_id:
                    created_camera_null_rows = execute(
                        """
                        WHERE source_id = %(source_id)s
                          AND (camera_id IS NULL OR camera_id = '')
                          AND created_at BETWEEN %(created_at_from)s::timestamptz
                                              AND %(created_at_to)s::timestamptz
                        """
                    )
                    if created_camera_null_rows:
                        audit["camera_id_fallback_used"] = True
                        rows, audit = finish(
                            created_camera_null_rows,
                            "created_at_fallback",
                        )
                        return (rows, audit) if return_audit else rows

            if audit["allow_timestamp_only_query"]:
                audit["timestamp_only_fallback_used"] = True
                timestamp_rows = execute(
                    f"""
                    WHERE source_id = %(source_id)s
                      {camera_sql}
                      AND timestamp_ms BETWEEN %(start_ts_ms)s AND %(end_ts_ms)s
                    """
                )
                rows, audit = finish(timestamp_rows, "timestamp_only_debug_fallback")
                return (rows, audit) if return_audit else rows

            rows, audit = finish([], "timestamp_and_created_at")
            return (rows, audit) if return_audit else rows
    except Exception:
        logger.exception(
            "continuous annotation observation lookup failed source_id=%s camera_id=%s",
            source_id,
            camera_id,
        )
        rows, audit = finish([], "query_error")
        return (rows, audit) if return_audit else rows


def _load_gallery_candidates(
    pg_conn: psycopg.Connection,
    source_observation_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not source_observation_ids:
        return {}
    try:
        with pg_conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (mr.query_source_observation_id)
                       mr.query_source_observation_id,
                       mr.query_person_id AS person_id,
                       mr.query_gallery_embedding_id AS gallery_embedding_id,
                       mr.similarity,
                       mr.rank,
                       mr.similarity_threshold,
                       p.external_person_id,
                       p.name AS display_name
                FROM match_results mr
                LEFT JOIN persons p ON p.id = mr.query_person_id
                WHERE mr.search_mode = 'gallery_match'
                  AND mr.query_source_observation_id = ANY(
                      %(source_observation_ids)s::text[]
                  )
                ORDER BY mr.query_source_observation_id, mr.rank ASC,
                         mr.created_at DESC
                """,
                {"source_observation_ids": source_observation_ids},
            )
            return {
                str(row["query_source_observation_id"]): dict(row)
                for row in cur.fetchall()
                if row.get("query_source_observation_id")
            }
    except Exception:
        logger.exception("continuous annotation match_result lookup failed")
        return {}


def _event_identity(event_context: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(event_context.get("payload"))
    match = _as_dict(payload.get("match"))
    person = _as_dict(payload.get("matched_person"))
    source_observation_id = str(match.get("source_observation_id") or "")
    similarity = _to_float(match.get("similarity"))
    threshold = _to_float(match.get("threshold"), 0.5)
    return {
        "source_observation_id": source_observation_id,
        "person_id": person.get("person_id"),
        "external_person_id": person.get("external_person_id"),
        "display_name": person.get("name") or person.get("display_name") or "",
        "similarity": similarity,
        "rank": 1,
        "threshold": threshold,
        "gallery_embedding_id": match.get("gallery_embedding_id"),
    }


def _event_expects_known_face(event_context: dict[str, Any]) -> bool:
    return str(event_context.get("event_type", "")) in (
        "watchlist_hit",
        "live_search_hit",
    )


def _identity_for_observation(
    *,
    observation: dict[str, Any],
    event_identity: dict[str, Any],
    gallery_candidates: dict[str, dict[str, Any]],
    event_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_observation_id = str(observation.get("source_observation_id") or "")
    candidate = None
    if event_context is not None and _event_expects_known_face(event_context):
        trigger = str(event_identity.get("source_observation_id") or "")
        if not trigger:
            return unknown_watchlist_identity(
                threshold=event_identity.get("threshold", 0.5),
                skip_reason="missing_trigger_source_observation_id",
            )
        if not source_observation_id:
            return unknown_watchlist_identity(
                threshold=event_identity.get("threshold", 0.5),
                skip_reason="missing_source_observation_id",
            )
        if source_observation_id != trigger:
            return unknown_watchlist_identity(
                threshold=event_identity.get("threshold", 0.5),
                skip_reason="source_observation_id_mismatch",
            )
        candidate = event_identity
    elif source_observation_id and source_observation_id == event_identity.get(
        "source_observation_id"
    ):
        candidate = event_identity
    elif source_observation_id:
        candidate = gallery_candidates.get(source_observation_id)

    if not candidate:
        return {
            "status": "unknown",
            "person_id": None,
            "external_person_id": None,
            "display_name": "",
            "similarity": None,
            "rank": None,
            "threshold": event_identity.get("threshold", 0.5),
            "match_status": "not_searched",
        }

    similarity = _to_float(candidate.get("similarity"))
    threshold = _to_float(candidate.get("threshold"), None)
    if threshold is None:
        threshold = _to_float(candidate.get("similarity_threshold"), 0.5)
    above = similarity is not None and threshold is not None and similarity >= threshold

    return {
        "status": "matched" if above else "low_similarity_candidate",
        "person_id": candidate.get("person_id"),
        "external_person_id": candidate.get("external_person_id"),
        "display_name": candidate.get("display_name") or "",
        "similarity": similarity,
        "rank": candidate.get("rank"),
        "threshold": threshold,
        "match_status": "above_threshold" if above else "below_threshold",
    }


def _identity_is_known(identity: dict[str, Any]) -> bool:
    return (
        identity.get("status") == "matched"
        or identity.get("match_status") == "above_threshold"
        or bool(identity.get("external_person_id"))
        or identity.get("person_id") not in (None, "")
    )


def _identity_track_scope(
    line: dict[str, Any],
    obj: dict[str, Any],
    event_context: dict[str, Any],
) -> tuple[str, str, str] | None:
    track_id = str(obj.get("track_id") or "")
    if not track_id:
        return None
    source_id = str(line.get("source_id") or event_context.get("source_id") or "")
    camera_id = str(line.get("camera_id") or event_context.get("camera_id") or "")
    return source_id, camera_id, track_id


def _propagate_identities_by_track(
    grouped: "OrderedDict[tuple[int, str], dict[str, Any]]",
    *,
    event_context: dict[str, Any],
) -> int:
    """Propagate a known face identity to unknown faces on the same track."""
    if _event_expects_known_face(event_context):
        return 0
    if os.getenv(
        "ANNOTATION_TRACK_IDENTITY_PROPAGATION", "false"
    ).lower() in {"0", "false", "no"}:
        return 0

    best_by_track: dict[tuple[str, str, str], dict[str, Any]] = {}
    for line in grouped.values():
        for obj in line.get("objects", []):
            if obj.get("object_type") != "face":
                continue
            identity = _as_dict(obj.get("identity"))
            if not _identity_is_known(identity):
                continue
            scope = _identity_track_scope(line, obj, event_context)
            if scope is None:
                continue
            similarity = _to_float(identity.get("similarity"), -1.0)
            best = best_by_track.get(scope)
            best_similarity = (
                _to_float(_as_dict(best).get("similarity"), -1.0)
                if best is not None
                else -2.0
            )
            if best is None or (
                similarity is not None and similarity > (best_similarity or -2.0)
            ):
                best_by_track[scope] = identity

    if not best_by_track:
        return 0

    event_type = str(event_context.get("event_type", ""))
    severity = str(event_context.get("severity", ""))
    propagated = 0
    for line in grouped.values():
        for obj in line.get("objects", []):
            if obj.get("object_type") != "face":
                continue
            if _identity_is_known(_as_dict(obj.get("identity"))):
                continue
            scope = _identity_track_scope(line, obj, event_context)
            known = best_by_track.get(scope) if scope is not None else None
            if not known:
                continue
            identity = {
                "status": "matched",
                "person_id": known.get("person_id"),
                "external_person_id": known.get("external_person_id"),
                "display_name": known.get("display_name") or "",
                "similarity": known.get("similarity"),
                "rank": known.get("rank"),
                "threshold": known.get("threshold"),
                "match_status": "propagated_by_track",
            }
            obj["identity"] = identity
            obj["style"] = build_style(
                event_type=event_type,
                severity=severity,
                identity_status="matched",
                display_name=str(identity["display_name"] or ""),
                similarity=_to_float(identity.get("similarity")),
            )
            propagated += 1

    return propagated


def _fallback_event_observation(event_context: dict[str, Any]) -> dict[str, Any] | None:
    payload = _as_dict(event_context.get("payload"))
    observation = _as_dict(payload.get("observation"))
    media = _payload_media(payload)

    face_bbox = observation.get("face_bbox")
    bbox_source = "observation.face_bbox" if face_bbox is not None else None

    if face_bbox is None:
        face_bbox = _as_dict(payload.get("overlay")).get("face_bbox")
        if face_bbox is not None:
            bbox_source = "payload.overlay.face_bbox"

    if face_bbox is None and _event_expects_known_face(event_context):
        face_bbox = payload.get("face_bbox")
        if face_bbox is not None:
            bbox_source = "payload.face_bbox"

    if face_bbox is None:
        return None

    identity = _event_identity(event_context)
    event_ts_ms = event_context.get("event_ts_ms", 0)
    timestamp_ms = observation.get("timestamp_ms")
    if timestamp_ms is None:
        timestamp_ms = event_ts_ms

    source_observation_id = identity.get("source_observation_id") or ""

    return {
        "id": None,
        "source_observation_id": source_observation_id,
        "camera_id": observation.get("camera_id") or event_context.get("camera_id", ""),
        "source_id": observation.get("source_id") or event_context.get("source_id", ""),
        "track_id": observation.get("track_id") or event_context.get("track_id", ""),
        "timestamp_ms": timestamp_ms or event_ts_ms,
        "frame_num": observation.get("frame_num") or media.get("frame_num"),
        "face_bbox": face_bbox,
        "landmarks": observation.get("landmarks"),
        "face_confidence": observation.get("face_confidence")
        or event_context.get("confidence", 0.0),
        "quality": observation.get("quality", 0.0),
        "detector_model": "yolov8_face",
        "embedding_model": "adaface",
        "embedding_dim": None,
        "embedding_norm": None,
        "payload": {
            "media": media,
            "person_track_id": observation.get("person_track_id")
            or observation.get("track_id")
            or event_context.get("track_id", ""),
            "face_track_id": observation.get("face_track_id"),
            "track_id_semantics": observation.get(
                "track_id_semantics",
                "person_track_id",
            ),
        },
        "_bbox_source": bbox_source,
    }


_BEHAVIOR_EVENT_TYPES = frozenset({
    "intrusion",
    "loitering",
    "crowd_gathering",
    "running",
    "fall",
    "perimeter_breach",
})


def _annotation_object(
    *,
    observation: dict[str, Any],
    identity: dict[str, Any],
    event_context: dict[str, Any],
) -> dict[str, Any] | None:
    bbox = _normalise_bbox(
        observation.get("face_bbox"),
        observation.get("face_confidence"),
    )
    if bbox is None:
        return None

    event_type = str(event_context.get("event_type", ""))
    is_behavior = event_type in _BEHAVIOR_EVENT_TYPES
    bbox_source = observation.get("_bbox_source", "")
    if not is_behavior and not _is_allowed_face_bbox_source(bbox_source):
        return None
    if not is_behavior:
        bbox["source"] = bbox_source
        bbox["coordinate_space"] = "pixel"

    if is_behavior:
        object_type = "person"
        object_id_prefix = "person"
        person_track_id = None
        face_track_id = None
        track_id_semantics = None
    else:
        object_type = "face"
        object_id_prefix = "face"
        payload = _as_dict(observation.get("payload"))
        person_track_id = (
            payload.get("person_track_id")
            or observation.get("person_track_id")
            or observation.get("track_id")
        )
        face_track_id = payload.get("face_track_id") or observation.get("face_track_id")
        track_id_semantics = (
            payload.get("track_id_semantics")
            or observation.get("track_id_semantics")
            or "person_track_id"
        )
    source_obs_id = observation.get("source_observation_id", "")

    style = build_style(
        event_type=event_type,
        severity=str(event_context.get("severity", "")),
        identity_status=str(identity.get("status") or "unknown"),
        display_name=str(identity.get("display_name") or ""),
        similarity=_to_float(identity.get("similarity")),
    )

    result = {
        "object_type": object_type,
        "object_id": f"{object_id_prefix}:{source_obs_id}",
        "source_observation_id": source_obs_id,
        "track_id": str(observation.get("track_id") or ""),
        "person_track_id": str(person_track_id or "") if person_track_id is not None else None,
        "face_track_id": str(face_track_id) if face_track_id not in (None, "") else None,
        "track_id_semantics": track_id_semantics,
        "bbox": bbox,
        "landmarks": _normalise_landmarks(observation.get("landmarks")),
        "identity": {
            "status": identity.get("status", "unknown"),
            "person_id": identity.get("person_id"),
            "external_person_id": identity.get("external_person_id"),
            "display_name": identity.get("display_name") or "",
            "similarity": identity.get("similarity"),
            "rank": identity.get("rank"),
            "threshold": identity.get("threshold"),
            "match_status": identity.get("match_status"),
        },
        "pose": {
            "status": "unavailable",
            "reason": "pose_observation_not_yet_persisted",
        },
        "action": {
            "status": "event_triggered" if is_behavior else "none",
            "event_type": event_type if is_behavior else None,
            "severity": event_context.get("severity") if is_behavior else None,
        },
        "style": style,
    }
    # Include bbox_source for debugging geometry issues.
    if bbox_source:
        result["bbox_source"] = bbox_source
    if not is_behavior and _event_expects_known_face(event_context):
        result = apply_watchlist_identity_strict(
            result,
            trigger_source_observation_id=_event_identity(event_context).get("source_observation_id"),
            identity=identity,
        )
    return result


def _object_bbox_source(obj: dict[str, Any]) -> str:
    bbox = _as_dict(obj.get("bbox"))
    source = bbox.get("source") or obj.get("bbox_source")
    return str(source or "")


def _face_bbox_format_stats(
    observations: list[dict[str, Any]],
    lines: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    stats = {
        "structured_count": 0,
        "legacy_list_count": 0,
        "unknown_format_count": 0,
        "annotation_unknown_format_count": 0,
    }
    for observation in observations:
        raw_bbox = observation.get("face_bbox")
        if isinstance(raw_bbox, dict):
            fmt = raw_bbox.get("format") or raw_bbox.get("bbox_format")
            values = (
                raw_bbox.get("values")
                or raw_bbox.get("bbox")
                or raw_bbox.get("xyxy")
            )
            if fmt in {"cxcywh", "xywh", "xyxy"} and isinstance(values, list):
                stats["structured_count"] += 1
            else:
                stats["unknown_format_count"] += 1
        elif isinstance(raw_bbox, list):
            stats["legacy_list_count"] += 1
        elif raw_bbox is not None:
            stats["unknown_format_count"] += 1

    for line in lines or []:
        for obj in line.get("objects", []):
            if not isinstance(obj, dict) or obj.get("object_type") != "face":
                continue
            bbox = _as_dict(obj.get("bbox"))
            if bbox.get("format") not in {"cxcywh", "xywh", "xyxy"}:
                stats["annotation_unknown_format_count"] += 1
            if not isinstance(bbox.get("xyxy"), list):
                stats["annotation_unknown_format_count"] += 1
    return stats


def _face_track_semantics_stats(lines: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "track_id_semantics": "person_track_id",
        "face_track_id_present_count": 0,
        "person_track_id_present_count": 0,
        "unknown_track_id_semantics_count": 0,
    }
    for line in lines:
        for obj in line.get("objects", []):
            if not isinstance(obj, dict) or obj.get("object_type") != "face":
                continue
            if obj.get("face_track_id") not in (None, ""):
                stats["face_track_id_present_count"] += 1
            if obj.get("person_track_id") not in (None, ""):
                stats["person_track_id_present_count"] += 1
            if obj.get("track_id_semantics") != "person_track_id":
                stats["unknown_track_id_semantics_count"] += 1
    return stats


def _object_is_known_face(obj: dict[str, Any]) -> bool:
    if obj.get("object_type") != "face":
        return False
    return _identity_is_known(_as_dict(obj.get("identity")))


def _bbox_iou(left: list[float] | None, right: list[float] | None) -> float:
    if left is None or right is None or len(left) < 4 or len(right) < 4:
        return 0.0
    lx1, ly1, lx2, ly2 = left[:4]
    rx1, ry1, rx2, ry2 = right[:4]
    ix1 = max(lx1, rx1)
    iy1 = max(ly1, ry1)
    ix2 = min(lx2, rx2)
    iy2 = min(ly2, ry2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if intersection <= 0.0:
        return 0.0
    left_area = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
    right_area = max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1)
    union = left_area + right_area - intersection
    return intersection / union if union > 0.0 else 0.0


def _face_candidate_second(line: dict[str, Any]) -> int:
    time_offset_ms = _to_float(line.get("time_offset_ms"), None)
    if time_offset_ms is None:
        time_offset_ms = _to_float(line.get("timestamp_ms"), 0.0) or 0.0
    return int(time_offset_ms // 1000)


def _face_candidate_score(
    line: dict[str, Any],
    obj: dict[str, Any],
) -> tuple[int, float, float, float]:
    identity = _as_dict(obj.get("identity"))
    similarity = _to_float(identity.get("similarity"), -1.0)
    confidence = _to_float(_as_dict(obj.get("bbox")).get("confidence"), -1.0)
    second = _face_candidate_second(line)
    time_offset_ms = _to_float(line.get("time_offset_ms"), 0.0) or 0.0
    center_distance = abs(time_offset_ms - (second * 1000 + 500))
    return (
        1 if _object_is_known_face(obj) else 0,
        similarity if similarity is not None else -1.0,
        confidence if confidence is not None else -1.0,
        -center_distance,
    )


def _face_identity_dedup_key(obj: dict[str, Any]) -> str:
    identity = _as_dict(obj.get("identity"))
    for key in ("external_person_id", "person_id", "display_name"):
        value = identity.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    return "unknown"


def _face_xyxy(obj: dict[str, Any]) -> list[float] | None:
    bbox = _as_dict(obj.get("bbox"))
    return _bbox_to_xyxy(bbox, source_format=str(bbox.get("format") or ""))


def _dedupe_face_annotations_by_clip(
    grouped: "OrderedDict[tuple[int, str], dict[str, Any]]",
    *,
    event_context: dict[str, Any],
) -> dict[str, int]:
    candidates: list[tuple[tuple[int, str], dict[str, Any], dict[str, Any]]] = []
    for line_key, line in grouped.items():
        for obj in line.get("objects", []):
            if isinstance(obj, dict) and obj.get("object_type") == "face":
                candidates.append((line_key, line, obj))

    removed: set[int] = set()
    stats = {
        "input_count": len(candidates),
        "output_count": len(candidates),
        "same_track_second_removed": 0,
        "iou_duplicate_removed": 0,
    }

    best_by_track_second: dict[tuple[str, str, str, int], tuple[tuple[int, str], dict[str, Any], dict[str, Any]]] = {}
    for candidate in candidates:
        _line_key_value, line, obj = candidate
        track_id = str(obj.get("track_id") or "")
        if not track_id:
            continue
        key = (
            str(line.get("source_id") or event_context.get("source_id") or ""),
            str(line.get("camera_id") or event_context.get("camera_id") or ""),
            track_id,
            _face_candidate_second(line),
        )
        best = best_by_track_second.get(key)
        if best is None:
            best_by_track_second[key] = candidate
            continue
        if _face_candidate_score(line, obj) > _face_candidate_score(best[1], best[2]):
            removed.add(id(best[2]))
            best_by_track_second[key] = candidate
        else:
            removed.add(id(obj))
        stats["same_track_second_removed"] += 1

    untracked = [
        candidate
        for candidate in candidates
        if id(candidate[2]) not in removed and not str(candidate[2].get("track_id") or "")
    ]
    for index, left in enumerate(untracked):
        if id(left[2]) in removed:
            continue
        for right in untracked[index + 1 :]:
            if id(right[2]) in removed:
                continue
            if _face_candidate_second(left[1]) != _face_candidate_second(right[1]):
                continue
            if str(left[1].get("source_id") or "") != str(right[1].get("source_id") or ""):
                continue
            if str(left[1].get("camera_id") or "") != str(right[1].get("camera_id") or ""):
                continue
            left_identity = _face_identity_dedup_key(left[2])
            right_identity = _face_identity_dedup_key(right[2])
            if left_identity != right_identity and "unknown" not in {
                left_identity,
                right_identity,
            }:
                continue
            if _bbox_iou(_face_xyxy(left[2]), _face_xyxy(right[2])) <= 0.7:
                continue
            if _face_candidate_score(right[1], right[2]) > _face_candidate_score(
                left[1],
                left[2],
            ):
                removed.add(id(left[2]))
                stats["iou_duplicate_removed"] += 1
                break
            removed.add(id(right[2]))
            stats["iou_duplicate_removed"] += 1

    if removed:
        for line_key in list(grouped.keys()):
            line = grouped[line_key]
            objects = [
                obj for obj in line.get("objects", []) if id(obj) not in removed
            ]
            if objects:
                line["objects"] = objects
            else:
                del grouped[line_key]

    stats["output_count"] = sum(
        1
        for line in grouped.values()
        for obj in line.get("objects", [])
        if isinstance(obj, dict) and obj.get("object_type") == "face"
    )
    return stats


def _line_role(line: dict[str, Any]) -> str:
    if line.get("record_type") == "object_annotation":
        return str(line.get("annotation_role") or line.get("object_type") or "")
    objects = line.get("objects")
    if isinstance(objects, list):
        for obj in objects:
            if isinstance(obj, dict) and _object_is_known_face(obj):
                return "known_face"
        for obj in objects:
            if isinstance(obj, dict) and obj.get("object_type") == "face":
                return "unknown_face"
    return str(line.get("record_type") or "")


def _line_track_id(line: dict[str, Any]) -> str:
    if line.get("track_id") not in (None, ""):
        return str(line.get("track_id"))
    objects = line.get("objects")
    if isinstance(objects, list):
        for obj in objects:
            if isinstance(obj, dict) and obj.get("track_id") not in (None, ""):
                return str(obj.get("track_id"))
    return ""


def _line_time_sort_value(line: dict[str, Any]) -> float:
    time_offset_ms = _to_float(line.get("time_offset_ms"), None)
    if time_offset_ms is not None:
        return time_offset_ms
    frame_pts = _to_float(line.get("frame_pts"), None)
    if frame_pts is not None:
        return frame_pts / 1_000_000.0
    return float("inf")


def _annotation_sort_key(line: dict[str, Any]) -> tuple[float, int, str]:
    role_priority = {
        "behavior_event": 0,
        "known_face": 1,
        "unknown_face": 2,
        "person_context": 3,
        "annotation_status": 9,
    }
    return (
        _line_time_sort_value(line),
        role_priority.get(_line_role(line), 5),
        _line_track_id(line),
    )


def _drawable_annotation(line: dict[str, Any]) -> bool:
    return line.get("record_type") == "object_annotation" or bool(line.get("objects"))


def _has_time_anchor(line: dict[str, Any]) -> bool:
    return line.get("time_offset_ms") is not None or line.get("frame_pts") is not None


def _annotation_time_contract_stats(
    lines: list[dict[str, Any]],
    *,
    clip_start_ts_ms: int,
    clip_end_ts_ms: int,
) -> dict[str, Any]:
    duration_ms = max(0, clip_end_ts_ms - clip_start_ts_ms)
    missing_time_anchor_count = 0
    negative_time_offset_count = 0
    over_duration_time_offset_count = 0
    estimated_time_alignment_count = 0
    previous_key: tuple[float, int, str] | None = None
    sorted_ok = True

    for line in lines:
        key = _annotation_sort_key(line)
        if previous_key is not None and key < previous_key:
            sorted_ok = False
        previous_key = key
        if _drawable_annotation(line) and not _has_time_anchor(line):
            missing_time_anchor_count += 1
        if line.get("time_alignment_status") == "estimated":
            estimated_time_alignment_count += 1
        time_offset_ms = _to_float(line.get("time_offset_ms"), None)
        if time_offset_ms is None:
            continue
        if time_offset_ms < 0:
            negative_time_offset_count += 1
        if duration_ms > 0 and time_offset_ms > duration_ms:
            over_duration_time_offset_count += 1

    return {
        "annotation_time_sorted": sorted_ok,
        "missing_time_anchor_count": missing_time_anchor_count,
        "negative_time_offset_count": negative_time_offset_count,
        "over_duration_time_offset_count": over_duration_time_offset_count,
        "time_offset_over_duration_count": over_duration_time_offset_count,
        "estimated_time_alignment_count": estimated_time_alignment_count,
    }


def _refresh_drawable_summary_counts(
    summary: dict[str, Any],
    records: list[dict[str, Any]],
) -> None:
    refreshed = _summary(records)
    for key, value in refreshed.items():
        if key in {
            "schema_version",
            "identities_propagated_by_track",
            "annotation_time_sorted",
            "missing_time_anchor_count",
            "negative_time_offset_count",
            "over_duration_time_offset_count",
            "time_offset_over_duration_count",
            "estimated_time_alignment_count",
        }:
            continue
        summary[key] = value

    face_bbox_format = dict(summary.get("face_bbox_format") or {})
    emitted_bbox_stats = _face_bbox_format_stats([], records)
    face_bbox_format["annotation_unknown_format_count"] = emitted_bbox_stats[
        "annotation_unknown_format_count"
    ]
    summary["face_bbox_format"] = face_bbox_format
    summary["face_track_semantics"] = _face_track_semantics_stats(records)


def _face_object_count(line: dict[str, Any]) -> int:
    if line.get("record_type") == "object_annotation":
        return 1 if line.get("object_type") == "face" else 0
    objects = line.get("objects")
    if not isinstance(objects, list):
        return 0
    return sum(
        1
        for obj in objects
        if isinstance(obj, dict) and obj.get("object_type") == "face"
    )


def _face_frame_alignment_template(
    *,
    sink_first_pts: int | None = None,
    sink_frame_offset_index: dict[int, int] | None = None,
) -> dict[str, Any]:
    return {
        "sink_first_pts_present": sink_first_pts is not None,
        "sink_frame_index_count": len(sink_frame_offset_index or {}),
        "face_frame_num_present_count": 0,
        "face_frame_num_matched_count": 0,
        "face_frame_pts_present_count": 0,
        "face_frame_pts_fallback_count": 0,
        "face_timestamp_estimated_count": 0,
        "frame_anchored_face_count": 0,
    }


def _face_frame_alignment_from_records(
    records: list[dict[str, Any]],
    *,
    sink_first_pts: int | None = None,
    sink_frame_offset_index: dict[int, int] | None = None,
) -> dict[str, Any]:
    stats = _face_frame_alignment_template(
        sink_first_pts=sink_first_pts,
        sink_frame_offset_index=sink_frame_offset_index,
    )
    for line in records:
        face_count = _face_object_count(line)
        if face_count <= 0:
            continue
        if line.get("frame_num") is not None:
            stats["face_frame_num_present_count"] += face_count
        if line.get("frame_pts") is not None:
            stats["face_frame_pts_present_count"] += face_count
        basis = str(line.get("time_basis") or "")
        if basis == "frame_num":
            stats["face_frame_num_matched_count"] += face_count
            stats["frame_anchored_face_count"] += face_count
        elif basis == "frame_pts":
            stats["face_frame_pts_fallback_count"] += face_count
            stats["frame_anchored_face_count"] += face_count
        elif basis == "timestamp_estimated" or not basis:
            stats["face_timestamp_estimated_count"] += face_count
    return stats


def _merge_face_frame_alignment_summary(
    summary: dict[str, Any],
    stats: dict[str, Any],
) -> None:
    existing = _as_dict(summary.get("face_frame_alignment"))
    merged = dict(existing)
    merged.update(stats)
    if existing.get("sink_first_pts_present") and not stats.get(
        "sink_first_pts_present"
    ):
        merged["sink_first_pts_present"] = True
    if int(existing.get("sink_frame_index_count") or 0) > int(
        stats.get("sink_frame_index_count") or 0
    ):
        merged["sink_frame_index_count"] = int(
            existing.get("sink_frame_index_count") or 0
        )
    summary["face_frame_alignment"] = merged
    summary["frame_anchored_face_count"] = int(
        merged.get("frame_anchored_face_count") or 0
    )
    summary["face_timestamp_estimated_count"] = int(
        merged.get("face_timestamp_estimated_count") or 0
    )
    if summary["face_timestamp_estimated_count"] > 0:
        summary["face_timestamp_estimated_reason"] = (
            "missing_frame_num_match_and_frame_pts_alignment"
        )
    else:
        summary.pop("face_timestamp_estimated_reason", None)


def _disable_estimated_face_time_offsets(
    records: list[dict[str, Any]],
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Prevent estimated clip anchors from driving precise face overlays."""
    time_anchor = _as_dict(summary.get("time_anchor"))
    estimated_anchor = time_anchor.get("time_alignment_status") == "estimated"

    disabled_count = 0
    frame_pts_fallback_count = 0
    dropped_no_frame_pts_count = 0
    frame_anchored_count = 0
    output: list[dict[str, Any]] = []

    for line in records:
        face_count = _face_object_count(line)
        if face_count <= 0:
            output.append(line)
            continue

        line_basis = str(line.get("time_basis") or "")
        line_status = str(line.get("time_alignment_status") or "")
        if line_basis in {"frame_num", "frame_pts"} or line_status.startswith("exact"):
            line["face_overlay_time_alignment_status"] = "aligned_frame_based"
            line["face_overlay_time_basis"] = line_basis or "frame"
            objects = line.get("objects")
            if isinstance(objects, list):
                for obj in objects:
                    if isinstance(obj, dict) and obj.get("object_type") == "face":
                        obj["face_overlay_time_alignment_status"] = (
                            "aligned_frame_based"
                        )
                        obj["face_overlay_time_basis"] = line_basis or "frame"
            elif line.get("record_type") == "object_annotation":
                line["face_overlay_time_alignment_status"] = "aligned_frame_based"
                line["face_overlay_time_basis"] = line_basis or "frame"
            frame_anchored_count += face_count
            output.append(line)
            continue

        if not estimated_anchor:
            output.append(line)
            continue

        if line.get("time_offset_ms") is not None:
            line["estimated_time_offset_ms"] = line.get("time_offset_ms")
            line["time_offset_ms"] = None
            disabled_count += face_count
        line["face_overlay_time_alignment_status"] = "estimated_unreliable"
        line["face_overlay_time_basis"] = "frame_pts_fallback_only"

        objects = line.get("objects")
        if isinstance(objects, list):
            for obj in objects:
                if isinstance(obj, dict) and obj.get("object_type") == "face":
                    obj["face_overlay_time_alignment_status"] = "estimated_unreliable"
                    obj["face_overlay_time_basis"] = "frame_pts_fallback_only"
        elif line.get("record_type") == "object_annotation":
            line["face_overlay_time_alignment_status"] = "estimated_unreliable"
            line["face_overlay_time_basis"] = "frame_pts_fallback_only"

        if line.get("frame_pts") is None:
            dropped_no_frame_pts_count += face_count
            continue
        frame_pts_fallback_count += face_count
        output.append(line)

    summary["frame_anchored_face_count"] = frame_anchored_count
    summary["estimated_face_time_offset_disabled_count"] = disabled_count
    summary["estimated_face_frame_pts_fallback_count"] = frame_pts_fallback_count
    summary["estimated_face_overlay_dropped_no_frame_pts_count"] = (
        dropped_no_frame_pts_count
    )
    if disabled_count or dropped_no_frame_pts_count:
        summary["face_overlay_time_alignment_status"] = (
            "partial_frame_based" if frame_anchored_count else "estimated_unreliable"
        )
        summary["estimated_face_overlay_policy"] = (
            "disable_estimated_time_offset_use_frame_pts_only"
        )
    elif frame_anchored_count:
        summary["face_overlay_time_alignment_status"] = "aligned_frame_based"
    else:
        summary.setdefault(
            "face_overlay_time_alignment_status",
            "estimated" if estimated_anchor else "aligned",
        )
    return output


def _prepare_annotation_records(
    lines: list[dict[str, Any]],
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    missing_anchor = [
        line for line in lines if _drawable_annotation(line) and not _has_time_anchor(line)
    ]
    records = [
        line
        for line in lines
        if not (_drawable_annotation(line) and not _has_time_anchor(line))
    ]
    records = _disable_estimated_face_time_offsets(records, summary)
    duration_ms = _to_int(
        _as_dict(summary.get("time_anchor")).get("actual_clip_duration_ms"),
        max(0, _to_int(summary.get("clip_end_ts_ms")) - _to_int(summary.get("clip_start_ts_ms"))),
    )
    time_stats = _annotation_time_contract_stats(
        records,
        clip_start_ts_ms=_to_int(summary.get("clip_start_ts_ms")),
        clip_end_ts_ms=_to_int(summary.get("clip_start_ts_ms")) + duration_ms,
    )
    time_stats["missing_time_anchor_count"] += len(missing_anchor)
    records = [
        line
        for line in records
        if (
            (_to_float(line.get("time_offset_ms"), None) is None)
            or (
                (_to_float(line.get("time_offset_ms"), 0.0) or 0.0) >= 0
                and (
                    duration_ms <= 0
                    or (_to_float(line.get("time_offset_ms"), 0.0) or 0.0) <= duration_ms
                )
            )
        )
    ]
    _merge_face_frame_alignment_summary(
        summary,
        _face_frame_alignment_from_records(records),
    )
    records.sort(key=_annotation_sort_key)
    summary.update(time_stats)
    summary["annotation_time"] = {
        "missing_time_anchor_count": summary.get("missing_time_anchor_count", 0),
        "negative_time_offset_count": summary.get("negative_time_offset_count", 0),
        "over_duration_time_offset_count": summary.get(
            "over_duration_time_offset_count",
            0,
        ),
        "estimated_time_alignment_count": summary.get(
            "estimated_time_alignment_count",
            0,
        ),
    }
    _refresh_drawable_summary_counts(summary, records)
    summary["annotation_lines"] = len(records)
    return records


def _line_key(observation: dict[str, Any]) -> tuple[int, str]:
    media = _payload_media(observation.get("payload"))
    return (
        _to_int(observation.get("timestamp_ms")),
        str(media.get("frame_uuid") or ""),
    )


def _build_sink_frame_index(
    replay_metadata_path: str | Path | None,
) -> tuple[int | None, dict[int, int]]:
    """Build a frame_num to clip-relative millisecond index from sink metadata."""
    records = _jsonl_records(replay_metadata_path)
    if not records:
        return None, {}

    first_pts: int | None = None
    for record in records:
        pts = record.get("pts")
        if pts is None:
            pts = record.get("frame_pts")
        if pts is None:
            continue
        try:
            first_pts = int(pts)
            break
        except (TypeError, ValueError):
            continue
    if first_pts is None:
        return None, {}

    frame_index: dict[int, int] = {}
    for record in records:
        frame_num = record.get("frame_num")
        pts = record.get("pts")
        if pts is None:
            pts = record.get("frame_pts")
        if frame_num is None or pts is None:
            continue
        try:
            frame_index[int(frame_num)] = int((int(pts) - first_pts) / 1_000_000)
        except (TypeError, ValueError):
            continue
    return first_pts, frame_index


def _line_base(
    *,
    observation: dict[str, Any],
    start_ts_ms: int,
    time_alignment_status: str = "estimated",
    sink_first_pts: int | None = None,
    sink_frame_offset_index: dict[int, int] | None = None,
) -> dict[str, Any]:
    payload = _as_dict(observation.get("payload"))
    media = _payload_media(payload)
    timestamp_ms = _to_int(observation.get("timestamp_ms"))
    frame_num = observation.get("frame_num")
    if frame_num is None:
        frame_num = media.get("frame_num")
    frame_pts = media.get("frame_pts")
    frame_index = sink_frame_offset_index or {}
    try:
        frame_num_int = int(frame_num) if frame_num is not None else None
    except (TypeError, ValueError):
        frame_num_int = None

    time_offset_ms: int | None = None
    time_basis = "timestamp_estimated"
    resolved_status = time_alignment_status
    if frame_num_int is not None and frame_num_int in frame_index:
        time_offset_ms = frame_index[frame_num_int]
        time_basis = "frame_num"
        resolved_status = "exact_frame_num"
    elif frame_pts is not None and sink_first_pts is not None:
        try:
            time_offset_ms = int((int(frame_pts) - int(sink_first_pts)) / 1_000_000)
            time_basis = "frame_pts"
            resolved_status = "exact_frame_pts"
        except (TypeError, ValueError):
            time_offset_ms = None

    if time_offset_ms is None:
        time_offset_ms = timestamp_ms - start_ts_ms
        time_basis = "timestamp_estimated"
        resolved_status = time_alignment_status

    return {
        "schema_version": SCHEMA_VERSION,
        "source_id": observation.get("source_id") or media.get("source_id") or "",
        "camera_id": observation.get("camera_id") or "",
        "timestamp_ms": timestamp_ms,
        "created_at": _format_datetime(observation.get("created_at")),
        "time_offset_ms": time_offset_ms,
        "time_alignment_status": resolved_status,
        "time_basis": time_basis,
        "frame_num": frame_num,
        "frame_uuid": media.get("frame_uuid"),
        "keyframe_uuid": media.get("keyframe_uuid"),
        "previous_keyframe_uuid": media.get("previous_keyframe_uuid"),
        "frame_pts": frame_pts,
        "ntp_timestamp": media.get("ntp_timestamp"),
        "objects": [],
    }


def _contains_key_fragment(value: Any, fragments: tuple[str, ...]) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in fragments):
                return True
            if _contains_key_fragment(child, fragments):
                return True
    elif isinstance(value, list):
        return any(_contains_key_fragment(item, fragments) for item in value)
    elif isinstance(value, str):
        lowered = value.lower()
        if "data:image/" in lowered:
            return True
        if len(value) > 2048 and any(mark in lowered[:128] for mark in ("jpeg", "png")):
            return True
    return False


def _summary(lines: list[dict[str, Any]]) -> dict[str, Any]:
    face_objects = 0
    person_objects = 0
    person_context_count = 0
    person_context_frames: set[Any] = set()
    person_context_tracks: set[str] = set()
    person_bbox_source_distribution: dict[str, int] = {}
    known_face_bbox_source_distribution: dict[str, int] = {}
    known_face_invalid_bbox_source_count = 0
    known_face_large_bbox_count = 0
    matched_objects = 0
    low_similarity_objects = 0
    unknown_objects = 0
    propagated_face_count = 0
    pose_unavailable_objects = 0
    action_none_objects = 0
    colors_used: set[str] = set()

    for line in lines:
        if line.get("record_type") == "object_annotation":
            if line.get("object_type") == "face":
                face_objects += 1
                identity = _as_dict(line.get("identity"))
                identity_status = identity.get("status")
                if _object_is_known_face(line):
                    matched_objects += 1
                    source = _object_bbox_source(line)
                    if source:
                        known_face_bbox_source_distribution[source] = (
                            known_face_bbox_source_distribution.get(source, 0) + 1
                        )
                    if not _is_allowed_face_bbox_source(source):
                        known_face_invalid_bbox_source_count += 1
                    if _face_bbox_is_large(_as_dict(line.get("bbox"))):
                        known_face_large_bbox_count += 1
                    if (
                        identity.get("match_status") == "propagated_by_track"
                    ):
                        propagated_face_count += 1
                elif identity_status == "low_similarity_candidate":
                    low_similarity_objects += 1
                else:
                    unknown_objects += 1
            if line.get("object_type") == "person":
                person_objects += 1
                if line.get("annotation_role") == "person_context":
                    person_context_count += 1
                    frame_anchor = line.get("frame_pts")
                    if frame_anchor is None:
                        frame_anchor = line.get("time_offset_ms")
                    if frame_anchor is not None:
                        person_context_frames.add(frame_anchor)
                    track_id = str(line.get("track_id") or "")
                    if track_id:
                        person_context_tracks.add(track_id)
                    bbox_source = _as_dict(line.get("bbox")).get("source")
                    if bbox_source:
                        source = str(bbox_source)
                        person_bbox_source_distribution[source] = (
                            person_bbox_source_distribution.get(source, 0) + 1
                        )
            if _as_dict(line.get("pose")).get("status") == "unavailable":
                pose_unavailable_objects += 1
            if _as_dict(line.get("action")).get("status") == "none":
                action_none_objects += 1
            color = _as_dict(line.get("style")).get("bbox_color")
            if color:
                colors_used.add(str(color))
            continue

        for obj in line.get("objects", []):
            if obj.get("object_type") == "face":
                face_objects += 1
                identity = _as_dict(obj.get("identity"))
                identity_status = identity.get("status")
                if _identity_is_known(identity):
                    matched_objects += 1
                    source = _object_bbox_source(obj)
                    if source:
                        known_face_bbox_source_distribution[source] = (
                            known_face_bbox_source_distribution.get(source, 0) + 1
                        )
                    if not _is_allowed_face_bbox_source(source):
                        known_face_invalid_bbox_source_count += 1
                    if _face_bbox_is_large(_as_dict(obj.get("bbox"))):
                        known_face_large_bbox_count += 1
                    if identity.get("match_status") == "propagated_by_track":
                        propagated_face_count += 1
                elif identity_status == "low_similarity_candidate":
                    low_similarity_objects += 1
                else:
                    unknown_objects += 1
            elif obj.get("object_type") == "person":
                person_objects += 1
                if obj.get("annotation_role") == "person_context":
                    person_context_count += 1
            else:
                continue
            if _as_dict(obj.get("pose")).get("status") == "unavailable":
                pose_unavailable_objects += 1
            if _as_dict(obj.get("action")).get("status") == "none":
                action_none_objects += 1
            color = _as_dict(obj.get("style")).get("bbox_color")
            if color:
                colors_used.add(str(color))

    return {
        "schema_version": SCHEMA_VERSION,
        "annotation_lines": len(lines),
        "face_objects": face_objects,
        "person_objects": person_objects,
        "person_context_count": person_context_count,
        "person_context_frame_count": len(person_context_frames),
        "person_context_track_count": len(person_context_tracks),
        "person_bbox_source_distribution": person_bbox_source_distribution,
        "known_face_bbox_source_distribution": known_face_bbox_source_distribution,
        "skipped_known_face_missing_face_bbox": 0,
        "known_face_invalid_bbox_source_count": known_face_invalid_bbox_source_count,
        "known_face_large_bbox_count": known_face_large_bbox_count,
        "annotation_time_sorted": True,
        "missing_time_anchor_count": 0,
        "negative_time_offset_count": 0,
        "over_duration_time_offset_count": 0,
        "time_offset_over_duration_count": 0,
        "estimated_time_alignment_count": 0,
        "person_bbox_gate": {
            "min_person_confidence": _person_bbox_gate_config()["min_person_confidence"],
            "min_person_width": _person_bbox_gate_config()["min_person_width"],
            "min_person_height": _person_bbox_gate_config()["min_person_height"],
            "max_bbox_area_ratio": _person_bbox_gate_config()["max_bbox_area_ratio"],
        },
        "matched_objects": matched_objects,
        "known_face_count": matched_objects,
        "low_similarity_objects": low_similarity_objects,
        "unknown_objects": unknown_objects,
        "unknown_face_count": unknown_objects,
        "propagated_face_count": propagated_face_count,
        "identities_propagated_by_track": 0,
        "pose_unavailable_objects": pose_unavailable_objects,
        "action_none_objects": action_none_objects,
        "colors_used": sorted(colors_used),
        "embedding_leaked": _contains_key_fragment(lines, ("embedding",)),
        "image_bytes_leaked": _contains_key_fragment(
            lines,
            (
                "image",
                "crop",
                "crop_bytes",
                "base64",
                "jpeg",
                "png",
                "raw_bytes",
                "frame_bytes",
            ),
        ),
    }


def _empty_reason(*, observations_loaded: int, fallback_used: bool) -> str:
    if observations_loaded == 0 and not fallback_used:
        return "no_annotation_records_for_source_window"
    return "annotation_records_missing_supported_overlay_objects"


def _status_record(summary: dict[str, Any]) -> dict[str, Any]:
    """Build a non-overlay JSONL status row for empty/unavailable bundles."""
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "annotation_status",
        "annotation_status": summary.get("annotation_status"),
        "annotation_empty_reason": summary.get("annotation_empty_reason"),
        "annotation_unavailable_reason": summary.get("annotation_unavailable_reason"),
        "overlay_available": False,
        "frontend_overlay_required": False,
        "event_id": summary.get("event_id", ""),
        "event_type": summary.get("event_type", ""),
        "source_id": summary.get("source_id", ""),
        "camera_id": summary.get("camera_id", ""),
        "clip_start_ts_ms": summary.get("clip_start_ts_ms"),
        "clip_end_ts_ms": summary.get("clip_end_ts_ms"),
        "time_anchor": summary.get("time_anchor"),
        "objects": [],
    }


def _bbox_signature(record: dict[str, Any]) -> tuple[Any, str, tuple[int, int, int, int]] | None:
    xyxy = _as_dict(record.get("bbox")).get("xyxy")
    if not isinstance(xyxy, list) or len(xyxy) < 4:
        return None
    try:
        bbox = tuple(int(round(float(value))) for value in xyxy[:4])
    except (TypeError, ValueError):
        return None
    frame_anchor = record.get("frame_pts")
    if frame_anchor is None:
        frame_anchor = record.get("time_offset_ms")
    return frame_anchor, str(record.get("track_id") or ""), bbox


def _dedupe_person_context_against_behavior_event(
    lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    behavior_signatures = {
        signature
        for line in lines
        if line.get("record_type") == "object_annotation"
        and line.get("annotation_role") == "behavior_event"
        and (signature := _bbox_signature(line)) is not None
    }
    if not behavior_signatures:
        return lines

    out: list[dict[str, Any]] = []
    for line in lines:
        if line.get("annotation_role") != "person_context":
            out.append(line)
            continue
        signature = _bbox_signature(line)
        if signature is not None and signature in behavior_signatures:
            continue
        out.append(line)
    return out


def _person_context_from_replay_or_db(
    pg_conn: psycopg.Connection,
    *,
    event_context: dict[str, Any],
    replay_metadata_path: str | None,
    start_ts_ms: int,
    end_ts_ms: int,
    time_alignment_status: str = "estimated",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    replay_lines, replay_stats = extract_person_bbox_timeline(
        replay_metadata_path=replay_metadata_path,
        event_context=event_context,
        start_ts_ms=start_ts_ms,
        time_alignment_status=time_alignment_status,
    )
    if replay_lines:
        replay_stats["person_context_source_kind"] = "replay_metadata"
        replay_stats["person_context_observation_count"] = 0
        return replay_lines, replay_stats

    db_lines, db_stats = extract_person_bbox_timeline_from_db(
        pg_conn,
        event_context=event_context,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
        time_alignment_status=time_alignment_status,
    )
    merged_stats = dict(replay_stats)
    for key, value in db_stats.items():
        if key.startswith("person_context_filtered_") and key.endswith("_count"):
            merged_stats[key] = int(merged_stats.get(key, 0) or 0) + int(value or 0)
        elif key in (
            "person_bbox_format_distribution",
            "person_bbox_source_distribution",
        ):
            combined = dict(merged_stats.get(key, {}) or {})
            for item_key, item_value in (value or {}).items():
                combined[item_key] = int(combined.get(item_key, 0) or 0) + int(
                    item_value or 0
                )
            merged_stats[key] = combined
        else:
            merged_stats[key] = value
    merged_stats["person_context_source_kind"] = "person_bbox_observations"
    merged_stats["replay_person_context_count"] = len(replay_lines)
    merged_stats["db_person_context_count"] = len(db_lines)
    return db_lines, merged_stats


def _atomic_write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(_jsonable(record), ensure_ascii=False))
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(_jsonable(data), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def build_continuous_annotations(
    pg_conn: psycopg.Connection,
    event_context: dict[str, Any],
    *,
    replay_metadata_path: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build ordered JSONL-ready annotation lines and summary metadata."""
    ideal_start_ts_ms, ideal_end_ts_ms, pre_seconds, post_seconds = _clip_window(
        event_context
    )
    source_id = str(event_context.get("source_id") or "")
    event_type = str(event_context.get("event_type", ""))
    replay_path = (
        replay_metadata_path
        or _payload_media(event_context.get("payload")).get("sink_metadata_path")
    )
    time_anchor = _resolve_time_anchor(
        event_context,
        replay_metadata_path=replay_path,
    )
    start_ts_ms = _to_int(
        time_anchor.get("actual_clip_start_ts_ms"),
        ideal_start_ts_ms,
    )
    end_ts_ms = _to_int(
        time_anchor.get("actual_clip_end_ts_ms"),
        ideal_end_ts_ms,
    )
    time_alignment_status = str(
        time_anchor.get("time_alignment_status") or "estimated"
    )
    effective_fields = _effective_window_fields(pre_seconds, post_seconds)

    if event_type == "intrusion":
        person_context_lines, person_context_stats = _person_context_from_replay_or_db(
            pg_conn,
            event_context=event_context,
            start_ts_ms=start_ts_ms,
            end_ts_ms=end_ts_ms,
            replay_metadata_path=replay_path,
            time_alignment_status=time_alignment_status,
        )
        record = _intrusion_mvp_annotation_record(
            event_context,
            start_ts_ms=start_ts_ms,
            time_alignment_status=time_alignment_status,
        )
        lines = list(person_context_lines)
        if record is not None:
            lines.append(record)
        lines = _dedupe_person_context_against_behavior_event(lines)
        summary = _summary(lines)
        annotation_status = (
            ANNOTATION_STATUS_COMPLETE if lines else ANNOTATION_STATUS_EMPTY
        )
        summary.update(
            {
                "clip_start_ts_ms": start_ts_ms,
                "clip_end_ts_ms": end_ts_ms,
                "ideal_clip_start_ts_ms": ideal_start_ts_ms,
                "ideal_clip_end_ts_ms": ideal_end_ts_ms,
                "pre_seconds": pre_seconds,
                "post_seconds": post_seconds,
                **effective_fields,
                "time_anchor": time_anchor,
                "event_offset_in_clip_ms": time_anchor.get("event_offset_in_clip_ms"),
                "source_id": source_id,
                "event_id": event_context.get("event_id", ""),
                "event_type": event_type,
                "camera_id": event_context.get("camera_id", ""),
                "annotation_status": annotation_status,
                "overlay_available": bool(lines),
                "frontend_overlay_required": bool(lines),
                "annotation_mode": "intrusion_with_person_context",
                "annotation_records_loaded": person_context_stats.get(
                    "person_metadata_count",
                    person_context_stats.get("person_observation_count", 0),
                ),
                "annotation_records_used": len(lines),
                "person_context_source_kind": person_context_stats.get(
                    "person_context_source_kind", ""
                ),
                "person_context_observation_lookup_mode": person_context_stats.get(
                    "person_observation_lookup_mode", ""
                ),
                "person_context_source": person_context_stats.get(
                    "replay_metadata_path", ""
                ),
                "person_context_source_available": person_context_stats.get(
                    "replay_metadata_available", False
                ),
                "person_context_source_frame_count": person_context_stats.get(
                    "frame_metadata_count", 0
                ),
                "person_context_source_frame_with_objects_count": (
                    person_context_stats.get("frame_metadata_with_objects_count", 0)
                ),
                "person_context_metadata_person_count": person_context_stats.get(
                    "person_metadata_count", 0
                ),
                "person_context_metadata_person_bbox_count": (
                    person_context_stats.get("person_bbox_metadata_count", 0)
                ),
                "person_context_metadata_person_confidence_count": (
                    person_context_stats.get("person_confidence_metadata_count", 0)
                ),
                "person_context_metadata_person_track_id_count": (
                    person_context_stats.get("person_track_id_metadata_count", 0)
                ),
                "person_context_metadata_person_frame_pts_count": (
                    person_context_stats.get("person_frame_pts_metadata_count", 0)
                ),
                "person_context_observation_count": person_context_stats.get(
                    "person_observation_count", 0
                ),
                "person_context_observation_bbox_count": (
                    person_context_stats.get("person_observation_bbox_count", 0)
                ),
                "person_context_observation_confidence_count": (
                    person_context_stats.get(
                        "person_observation_confidence_count", 0
                    )
                ),
                "person_context_observation_track_id_count": (
                    person_context_stats.get("person_observation_track_id_count", 0)
                ),
                "person_context_observation_time_anchor_count": (
                    person_context_stats.get("person_observation_time_anchor_count", 0)
                ),
                "person_context_filtered_low_confidence_count": (
                    person_context_stats.get(
                        "person_context_filtered_low_confidence_count", 0
                    )
                ),
                "person_context_filtered_small_bbox_count": (
                    person_context_stats.get("person_context_filtered_small_bbox_count", 0)
                ),
                "person_context_filtered_invalid_bbox_count": (
                    person_context_stats.get(
                        "person_context_filtered_invalid_bbox_count", 0
                    )
                ),
                "person_context_filtered_aspect_ratio_count": (
                    person_context_stats.get(
                        "person_context_filtered_aspect_ratio_count", 0
                    )
                ),
                "person_bbox_format_distribution": person_context_stats.get(
                    "person_bbox_format_distribution", {}
                ),
                "identities_propagated_by_track": 0,
                "identity_propagation": {
                    "propagated_count": 0,
                    "cross_track": False,
                    "cross_source": False,
                    "cross_camera": False,
                    "null_track": False,
                    "track_id_semantics": "person_track_id",
                },
                "face_track_semantics": _face_track_semantics_stats(lines),
                "face_bbox_format": _face_bbox_format_stats([], lines),
            }
        )
        if not lines:
            summary["annotation_empty_reason"] = "missing_event_bbox"
        if summary.get("person_context_count", 0) == 0:
            if person_context_stats.get("person_observation_count", 0) > 0:
                summary["person_context_unavailable_reason"] = (
                    "no_person_bbox_observation_after_gate"
                )
            elif person_context_stats.get("frame_metadata_count", 0) == 0:
                summary["person_context_unavailable_reason"] = (
                    "no_replay_person_metadata_or_person_bbox_observations"
                )
            elif person_context_stats.get("person_metadata_count", 0) == 0:
                summary["person_context_unavailable_reason"] = (
                    "no_replay_person_metadata_or_person_bbox_observations"
                )
            else:
                summary["person_context_unavailable_reason"] = (
                    "no_person_bbox_after_gate"
                )
        return lines, summary

    observations, face_observation_query = _load_observations(
        pg_conn,
        source_id=source_id,
        camera_id=str(event_context.get("camera_id") or ""),
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
        event_context=event_context,
        return_audit=True,
    )
    observations_loaded = len(observations)
    fallback_used = False
    skipped_known_face_missing_face_bbox = 0
    if not observations:
        fallback = _fallback_event_observation(event_context)
        if fallback is not None:
            observations = [fallback]
            fallback_used = True
        elif _event_expects_known_face(event_context):
            skipped_known_face_missing_face_bbox = 1

    # Tag DB-loaded observations with bbox_source when missing.
    # The fallback path sets _bbox_source itself; DB rows don't have it.
    for obs in observations:
        if "_bbox_source" not in obs and obs.get("face_bbox") is not None:
            obs["_bbox_source"] = "observation.face_bbox"

    source_observation_ids = [
        str(row.get("source_observation_id"))
        for row in observations
        if row.get("source_observation_id")
    ]
    gallery_candidates = _load_gallery_candidates(pg_conn, source_observation_ids)
    event_identity = _event_identity(event_context)
    sink_first_pts, sink_frame_offset_index = _build_sink_frame_index(replay_path)

    grouped: "OrderedDict[tuple[int, str], dict[str, Any]]" = OrderedDict()
    for observation in sorted(observations, key=_line_key):
        identity = _identity_for_observation(
            observation=observation,
            event_identity=event_identity,
            gallery_candidates=gallery_candidates,
            event_context=event_context,
        )
        obj = _annotation_object(
            observation=observation,
            identity=identity,
            event_context=event_context,
        )
        if obj is None:
            continue
        key = _line_key(observation)
        if key not in grouped:
            grouped[key] = _line_base(
                observation=observation,
                start_ts_ms=start_ts_ms,
                time_alignment_status=time_alignment_status,
                sink_first_pts=sink_first_pts,
                sink_frame_offset_index=sink_frame_offset_index,
            )
        grouped[key]["objects"].append(obj)

    face_dedup = _dedupe_face_annotations_by_clip(
        grouped,
        event_context=event_context,
    )
    identities_propagated = _propagate_identities_by_track(
        grouped,
        event_context=event_context,
    )
    identity_propagation = {
        "propagated_count": identities_propagated,
        "cross_track": False,
        "cross_source": False,
        "cross_camera": False,
        "null_track": False,
        "track_id_semantics": "person_track_id",
    }

    person_context_lines, person_context_stats = _person_context_from_replay_or_db(
        pg_conn,
        event_context=event_context,
        replay_metadata_path=replay_path,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
        time_alignment_status=time_alignment_status,
    )

    lines = list(grouped.values()) + person_context_lines
    summary = _summary(lines)
    annotation_status = (
        ANNOTATION_STATUS_COMPLETE if lines else ANNOTATION_STATUS_EMPTY
    )
    summary.update(
        {
            "clip_start_ts_ms": start_ts_ms,
            "clip_end_ts_ms": end_ts_ms,
            "ideal_clip_start_ts_ms": ideal_start_ts_ms,
            "ideal_clip_end_ts_ms": ideal_end_ts_ms,
            "pre_seconds": pre_seconds,
            "post_seconds": post_seconds,
            **effective_fields,
            "time_anchor": time_anchor,
            "event_offset_in_clip_ms": time_anchor.get("event_offset_in_clip_ms"),
            "source_id": source_id,
            "event_id": event_context.get("event_id", ""),
            "event_type": event_context.get("event_type", ""),
            "camera_id": event_context.get("camera_id", ""),
            "annotation_status": annotation_status,
            "overlay_available": bool(lines),
            "frontend_overlay_required": bool(lines),
            "annotation_mode": "continuous_jsonl",
            "annotation_records_loaded": observations_loaded,
            "annotation_records_used": (
                int(face_dedup.get("output_count", 0)) + len(person_context_lines)
            ),
            "skipped_known_face_missing_face_bbox": (
                skipped_known_face_missing_face_bbox
            ),
            "face_observation_query": face_observation_query,
            "face_dedup": face_dedup,
            "identity_propagation": identity_propagation,
            "face_track_semantics": _face_track_semantics_stats(lines),
            "face_bbox_format": _face_bbox_format_stats(observations, lines),
            "face_frame_alignment": _face_frame_alignment_from_records(
                list(grouped.values()),
                sink_first_pts=sink_first_pts,
                sink_frame_offset_index=sink_frame_offset_index,
            ),
            "person_context_source_kind": person_context_stats.get(
                "person_context_source_kind", ""
            ),
            "person_context_observation_lookup_mode": person_context_stats.get(
                "person_observation_lookup_mode", ""
            ),
            "person_context_source": person_context_stats.get(
                "replay_metadata_path", ""
            ),
            "person_context_observation_count": person_context_stats.get(
                "person_observation_count", 0
            ),
            "person_context_observation_bbox_count": person_context_stats.get(
                "person_observation_bbox_count", 0
            ),
            "person_context_observation_confidence_count": person_context_stats.get(
                "person_observation_confidence_count", 0
            ),
            "person_context_observation_track_id_count": person_context_stats.get(
                "person_observation_track_id_count", 0
            ),
            "person_context_observation_time_anchor_count": person_context_stats.get(
                "person_observation_time_anchor_count", 0
            ),
            "person_context_filtered_low_confidence_count": person_context_stats.get(
                "person_context_filtered_low_confidence_count", 0
            ),
            "person_context_filtered_small_bbox_count": person_context_stats.get(
                "person_context_filtered_small_bbox_count", 0
            ),
            "person_context_filtered_invalid_bbox_count": person_context_stats.get(
                "person_context_filtered_invalid_bbox_count", 0
            ),
            "person_context_filtered_aspect_ratio_count": person_context_stats.get(
                "person_context_filtered_aspect_ratio_count", 0
            ),
            "person_bbox_format_distribution": person_context_stats.get(
                "person_bbox_format_distribution", {}
            ),
            "identities_propagated_by_track": identities_propagated,
        }
    )
    if not lines:
        summary["annotation_empty_reason"] = _empty_reason(
            observations_loaded=observations_loaded,
            fallback_used=fallback_used,
        )
    return lines, summary


def _unavailable_summary(
    event_context: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    ideal_start_ts_ms, ideal_end_ts_ms, pre_seconds, post_seconds = _clip_window(
        event_context
    )
    time_anchor = _resolve_time_anchor(event_context)
    start_ts_ms = _to_int(
        time_anchor.get("actual_clip_start_ts_ms"),
        ideal_start_ts_ms,
    )
    end_ts_ms = _to_int(
        time_anchor.get("actual_clip_end_ts_ms"),
        ideal_end_ts_ms,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "annotation_lines": 0,
        "face_objects": 0,
        "person_objects": 0,
        "person_context_count": 0,
        "person_context_frame_count": 0,
        "person_context_track_count": 0,
        "person_bbox_source_distribution": {},
        "person_bbox_gate": {
            "min_person_confidence": _person_bbox_gate_config()["min_person_confidence"],
            "min_person_width": _person_bbox_gate_config()["min_person_width"],
            "min_person_height": _person_bbox_gate_config()["min_person_height"],
            "max_bbox_area_ratio": _person_bbox_gate_config()["max_bbox_area_ratio"],
        },
        "matched_objects": 0,
        "low_similarity_objects": 0,
        "unknown_objects": 0,
        "identities_propagated_by_track": 0,
        "pose_unavailable_objects": 0,
        "action_none_objects": 0,
        "colors_used": [],
        "embedding_leaked": False,
        "image_bytes_leaked": False,
        "clip_start_ts_ms": start_ts_ms,
        "clip_end_ts_ms": end_ts_ms,
        "ideal_clip_start_ts_ms": ideal_start_ts_ms,
        "ideal_clip_end_ts_ms": ideal_end_ts_ms,
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
        **_effective_window_fields(pre_seconds, post_seconds),
        "time_anchor": time_anchor,
        "event_offset_in_clip_ms": time_anchor.get("event_offset_in_clip_ms"),
        "source_id": event_context.get("source_id", ""),
        "event_id": event_context.get("event_id", ""),
        "event_type": event_context.get("event_type", ""),
        "camera_id": event_context.get("camera_id", ""),
        "annotation_status": ANNOTATION_STATUS_UNAVAILABLE,
        "annotation_unavailable_reason": reason,
        "annotation_generation_failed": True,
        "overlay_available": False,
        "frontend_overlay_required": False,
        "annotation_mode": "continuous_jsonl",
        "annotation_records_loaded": 0,
        "annotation_records_used": 0,
        "face_track_semantics": _face_track_semantics_stats([]),
        "face_bbox_format": _face_bbox_format_stats([], []),
        "identity_propagation": {
            "propagated_count": 0,
            "cross_track": False,
            "cross_source": False,
            "cross_camera": False,
            "null_track": False,
            "track_id_semantics": "person_track_id",
        },
    }


def write_continuous_annotation_bundle(
    pg_conn: psycopg.Connection,
    event_context: dict[str, Any],
    *,
    annotations_path: str,
    summary_path: str,
    replay_metadata_path: str | None = None,
) -> dict[str, Any]:
    """Write ``annotations.jsonl`` and ``summary.json`` for one evidence bundle."""
    annotations_file = Path(annotations_path)
    summary_file = Path(summary_path)
    annotations_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        lines, summary = build_continuous_annotations(
            pg_conn,
            event_context,
            replay_metadata_path=replay_metadata_path,
        )
    except Exception as exc:
        logger.exception(
            "continuous annotation generation failed event_id=%s",
            event_context.get("event_id", ""),
        )
        summary = _unavailable_summary(
            event_context,
            f"annotation_generation_failed:{exc.__class__.__name__}",
        )
        lines = []

    records = _prepare_annotation_records(lines, summary) if lines else []
    if not records:
        records = [_status_record(summary)]
    _atomic_write_jsonl(annotations_file, records)
    _atomic_write_json(summary_file, summary)

    return summary
