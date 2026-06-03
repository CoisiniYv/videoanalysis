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

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
C1I_MVP_ANNOTATION_SCHEMA_VERSION = "c1i.mvp.annotation.v1"
DEFAULT_PRE_SECONDS = 5.0
DEFAULT_POST_SECONDS = 10.0
ANNOTATION_STATUS_COMPLETE = "complete"
ANNOTATION_STATUS_EMPTY = "empty"
ANNOTATION_STATUS_UNAVAILABLE = "unavailable"
PERSON_CONTEXT_BBOX_SOURCE = "replay.metadata.person_bbox"
PERSON_CONTEXT_OBSERVATION_BBOX_SOURCE = "person_bbox_observations.person_bbox"
PERSON_CONTEXT_BBOX_COLOR = "#00C853"
INTRUSION_BBOX_COLOR = "#FF6D00"


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


def _normalise_bbox(face_bbox: Any, confidence: Any) -> dict[str, Any] | None:
    if isinstance(face_bbox, dict):
        values = face_bbox.get("values") or face_bbox.get("bbox")
        fmt = face_bbox.get("format") or face_bbox.get("bbox_format")
        conf = face_bbox.get("confidence", confidence)
        # Handle {x, y, width, height} dict format (common in behavior events).
        if values is None and all(
            k in face_bbox for k in ("x", "y", "width", "height")
        ):
            values = [
                float(face_bbox["x"]),
                float(face_bbox["y"]),
                float(face_bbox["width"]),
                float(face_bbox["height"]),
            ]
            fmt = fmt or "xywh"
    else:
        values = face_bbox
        fmt = "cxcywh"
        conf = confidence

    if not isinstance(values, list) or len(values) < 4:
        return None

    numbers = [_to_float(item) for item in values[:4]]
    if any(item is None for item in numbers):
        return None

    return {
        "format": fmt or "xyxy_or_cxcywh",
        "values": [float(item) for item in numbers if item is not None],
        "confidence": _to_float(conf, 0.0),
    }


def _payload_person_bbox(payload: dict[str, Any]) -> tuple[Any, str] | tuple[None, None]:
    if payload.get("person_bbox") is not None:
        return payload.get("person_bbox"), "payload.person_bbox"
    if payload.get("bbox") is not None:
        return payload.get("bbox"), "payload.bbox"
    return None, None


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
            fmt = fmt or "xyxy"
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
        "schema_version": C1I_MVP_ANNOTATION_SCHEMA_VERSION,
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
        "schema_version": C1I_MVP_ANNOTATION_SCHEMA_VERSION,
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
) -> list[dict[str, Any]]:
    try:
        with pg_conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, source_observation_id, camera_id, source_id, track_id,
                       timestamp_ms, frame_num, face_bbox, landmarks,
                       face_confidence, quality, detector_model, embedding_model,
                       embedding_dim, embedding_norm, payload
                FROM face_observations
                WHERE source_id = %(source_id)s
                  AND timestamp_ms BETWEEN %(start_ts_ms)s AND %(end_ts_ms)s
                ORDER BY timestamp_ms ASC, source_observation_id ASC
                """,
                {
                    "source_id": source_id,
                    "start_ts_ms": start_ts_ms,
                    "end_ts_ms": end_ts_ms,
                },
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception(
            "continuous annotation observation lookup failed source_id=%s",
            source_id,
        )
        return []


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


def _identity_for_observation(
    *,
    observation: dict[str, Any],
    event_identity: dict[str, Any],
    gallery_candidates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_observation_id = str(observation.get("source_observation_id") or "")
    candidate = None
    if source_observation_id and source_observation_id == event_identity.get(
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


def _fallback_event_observation(event_context: dict[str, Any]) -> dict[str, Any] | None:
    payload = _as_dict(event_context.get("payload"))
    observation = _as_dict(payload.get("observation"))
    media = _payload_media(payload)
    event_type = str(event_context.get("event_type", ""))

    face_bbox = observation.get("face_bbox")
    bbox_source = "observation.face_bbox" if face_bbox is not None else None

    if face_bbox is None:
        face_bbox = _as_dict(payload.get("overlay")).get("face_bbox")
        if face_bbox is not None:
            bbox_source = "payload.overlay.face_bbox"

    # For watchlist_hit / live_search_hit, also check payload.face_bbox
    # before falling back to person_bbox.
    if face_bbox is None and event_type in ("watchlist_hit", "live_search_hit"):
        face_bbox = payload.get("face_bbox")
        if face_bbox is not None:
            bbox_source = "payload.face_bbox"

    # Behavior event or last-resort fallback: read person bbox from
    # payload.person_bbox first, then payload.bbox.
    person_bbox_source = None
    if face_bbox is None:
        face_bbox, bbox_source = _payload_person_bbox(payload)
        if face_bbox is not None:
            person_bbox_source = face_bbox

    if face_bbox is None:
        return None

    identity = _event_identity(event_context)
    event_ts_ms = event_context.get("event_ts_ms", 0)
    timestamp_ms = observation.get("timestamp_ms")
    if timestamp_ms is None and person_bbox_source is not None:
        timestamp_ms = event_ts_ms

    source_observation_id = identity.get("source_observation_id") or ""
    if not source_observation_id and person_bbox_source is not None:
        source_observation_id = (
            f"behavior:{event_context.get('event_type', 'event')}"
            f":{event_context.get('event_id', '')}"
        )

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
        "payload": {"media": media},
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

    # For behavior events, the bbox is a person bbox (from fallback).
    # For watchlist_hit / live_search_hit, always use "face" object_type
    # even if the bbox came from person_bbox fallback.
    if is_behavior:
        object_type = "person"
        object_id_prefix = "person"
    else:
        object_type = "face"
        object_id_prefix = "face"
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
        "track_id": str(observation.get("track_id") or ""),
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
    return result


def _line_key(observation: dict[str, Any]) -> tuple[int, str]:
    media = _payload_media(observation.get("payload"))
    return (
        _to_int(observation.get("timestamp_ms")),
        str(media.get("frame_uuid") or ""),
    )


def _line_base(
    *,
    observation: dict[str, Any],
    start_ts_ms: int,
) -> dict[str, Any]:
    payload = _as_dict(observation.get("payload"))
    media = _payload_media(payload)
    timestamp_ms = _to_int(observation.get("timestamp_ms"))
    return {
        "schema_version": SCHEMA_VERSION,
        "source_id": observation.get("source_id") or media.get("source_id") or "",
        "camera_id": observation.get("camera_id") or "",
        "timestamp_ms": timestamp_ms,
        "time_offset_ms": timestamp_ms - start_ts_ms,
        "frame_num": observation.get("frame_num") or media.get("frame_num"),
        "frame_uuid": media.get("frame_uuid"),
        "keyframe_uuid": media.get("keyframe_uuid"),
        "previous_keyframe_uuid": media.get("previous_keyframe_uuid"),
        "frame_pts": media.get("frame_pts"),
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
    matched_objects = 0
    low_similarity_objects = 0
    unknown_objects = 0
    pose_unavailable_objects = 0
    action_none_objects = 0
    colors_used: set[str] = set()

    for line in lines:
        if line.get("record_type") == "object_annotation":
            if line.get("object_type") == "face":
                face_objects += 1
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
                identity_status = _as_dict(obj.get("identity")).get("status")
                if identity_status == "matched":
                    matched_objects += 1
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
        "person_bbox_gate": {
            "min_person_confidence": _person_bbox_gate_config()["min_person_confidence"],
            "min_person_width": _person_bbox_gate_config()["min_person_width"],
            "min_person_height": _person_bbox_gate_config()["min_person_height"],
            "max_bbox_area_ratio": _person_bbox_gate_config()["max_bbox_area_ratio"],
        },
        "matched_objects": matched_objects,
        "low_similarity_objects": low_similarity_objects,
        "unknown_objects": unknown_objects,
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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    replay_lines, replay_stats = extract_person_bbox_timeline(
        replay_metadata_path=replay_metadata_path,
        event_context=event_context,
        start_ts_ms=start_ts_ms,
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
    start_ts_ms, end_ts_ms, pre_seconds, post_seconds = _clip_window(event_context)
    source_id = str(event_context.get("source_id") or "")
    event_type = str(event_context.get("event_type", ""))
    replay_path = (
        replay_metadata_path
        or _payload_media(event_context.get("payload")).get("sink_metadata_path")
    )

    if event_type == "intrusion":
        person_context_lines, person_context_stats = _person_context_from_replay_or_db(
            pg_conn,
            event_context=event_context,
            start_ts_ms=start_ts_ms,
            end_ts_ms=end_ts_ms,
            replay_metadata_path=replay_path,
        )
        record = _intrusion_mvp_annotation_record(
            event_context,
            start_ts_ms=start_ts_ms,
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
                "pre_seconds": pre_seconds,
                "post_seconds": post_seconds,
                "source_id": source_id,
                "event_id": event_context.get("event_id", ""),
                "event_type": event_type,
                "camera_id": event_context.get("camera_id", ""),
                "annotation_status": annotation_status,
                "overlay_available": bool(lines),
                "frontend_overlay_required": bool(lines),
                "annotation_mode": "c1i_mvp_intrusion_with_person_context",
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

    observations = _load_observations(
        pg_conn,
        source_id=source_id,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
    )
    observations_loaded = len(observations)
    fallback_used = False
    if not observations:
        fallback = _fallback_event_observation(event_context)
        if fallback is not None:
            observations = [fallback]
            fallback_used = True

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

    grouped: "OrderedDict[tuple[int, str], dict[str, Any]]" = OrderedDict()
    for observation in sorted(observations, key=_line_key):
        identity = _identity_for_observation(
            observation=observation,
            event_identity=event_identity,
            gallery_candidates=gallery_candidates,
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
            )
        grouped[key]["objects"].append(obj)

    person_context_lines, person_context_stats = _person_context_from_replay_or_db(
        pg_conn,
        event_context=event_context,
        replay_metadata_path=replay_path,
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
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
            "pre_seconds": pre_seconds,
            "post_seconds": post_seconds,
            "source_id": source_id,
            "event_id": event_context.get("event_id", ""),
            "event_type": event_context.get("event_type", ""),
            "camera_id": event_context.get("camera_id", ""),
            "annotation_status": annotation_status,
            "overlay_available": bool(lines),
            "frontend_overlay_required": bool(lines),
            "annotation_mode": "continuous_jsonl",
            "annotation_records_loaded": observations_loaded,
            "annotation_records_used": len(observations) + len(person_context_lines),
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
    start_ts_ms, end_ts_ms, pre_seconds, post_seconds = _clip_window(event_context)
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
        "pose_unavailable_objects": 0,
        "action_none_objects": 0,
        "colors_used": [],
        "embedding_leaked": False,
        "image_bytes_leaked": False,
        "clip_start_ts_ms": start_ts_ms,
        "clip_end_ts_ms": end_ts_ms,
        "pre_seconds": pre_seconds,
        "post_seconds": post_seconds,
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

    records = lines if lines else [_status_record(summary)]
    _atomic_write_jsonl(annotations_file, records)
    _atomic_write_json(summary_file, summary)

    return summary
