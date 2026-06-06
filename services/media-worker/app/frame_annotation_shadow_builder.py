"""Build shadow evidence annotations from C1J frame annotation cache data."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from app.frame_annotation_timeline import match_clip_frames_to_annotations


SCHEMA_VERSION = "1.0"
ANNOTATION_SOURCE = "frame_annotation_cache"
DEFAULT_MIN_HIT_RATIO = 0.2

FORBIDDEN_VECTOR_FIELDS = {
    "embedding_vector",
    "vector",
    "features",
    "feature",
    "embedding_values",
}
FORBIDDEN_IMAGE_FIELDS = {
    "crop_bytes",
    "image_bytes",
    "raw_frame",
    "jpeg",
    "png",
    "base64_image",
    "frame_bytes",
}


def build_shadow_annotations_from_frame_cache(
    clip_frames: list[dict[str, Any]],
    annotation_messages: list[dict[str, Any]],
    *,
    source_id: str,
    camera_id: str,
    match_mode: str = "frame_uuid_then_pts",
    nearest_pts_ratio: float = 0.5,
    min_hit_ratio: float = DEFAULT_MIN_HIT_RATIO,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Convert matched frame annotation cache entries to shadow JSONL rows."""

    matches, match_summary = match_clip_frames_to_annotations(
        copy.deepcopy(clip_frames),
        copy.deepcopy(annotation_messages),
        match_mode=match_mode,
        nearest_pts_ratio=nearest_pts_ratio,
    )

    annotations: list[dict[str, Any]] = []
    person_count = 0
    face_count = 0
    behavior_count = 0

    for match in matches:
        message = match.get("annotation_message")
        if not isinstance(message, dict):
            continue
        for obj in message.get("objects") or []:
            if not isinstance(obj, dict):
                continue
            row = _object_to_shadow_annotation(
                obj,
                match=match,
                source_id=source_id,
                camera_id=camera_id,
            )
            if row is None:
                continue
            annotations.append(row)
            if row["object_type"] == "person":
                person_count += 1
            elif row["object_type"] == "face":
                face_count += 1
            elif row["object_type"] == "behavior_event":
                behavior_count += 1

    frames_matched = (
        match_summary["frames_matched_uuid"]
        + match_summary["frames_matched_pts_exact"]
        + match_summary["frames_matched_pts_nearest"]
    )
    hit_ratio = float(match_summary["match_hit_ratio"])
    if frames_matched <= 0:
        annotation_status = "missing_frame_metadata"
    elif hit_ratio >= float(min_hit_ratio):
        annotation_status = "complete"
    else:
        annotation_status = "partial"

    embedding_vectors = _count_forbidden_fields(annotations, FORBIDDEN_VECTOR_FIELDS)
    image_bytes = _count_forbidden_fields(annotations, FORBIDDEN_IMAGE_FIELDS)
    summary = {
        "annotation_source": ANNOTATION_SOURCE,
        "annotation_status": annotation_status,
        "clip_frames_requested": len(clip_frames),
        "frames_matched": frames_matched,
        "frames_missed": match_summary["frames_missed"],
        "match_hit_ratio": hit_ratio,
        "annotations_written": len(annotations),
        "person_annotations": person_count,
        "face_annotations": face_count,
        "behavior_event_annotations": behavior_count,
        "embedding_vectors_in_output": embedding_vectors,
        "image_bytes_in_output": image_bytes,
        "source_id": source_id,
        "camera_id": camera_id,
        "match_summary": match_summary,
        "min_hit_ratio": float(min_hit_ratio),
    }
    return annotations, summary


def ensure_person_context_annotations(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return rows with person detections explicitly marked for viewer person mode."""

    output: list[dict[str, Any]] = []
    for row in copy.deepcopy(annotations):
        if not isinstance(row, dict):
            continue
        if row.get("object_type") == "person":
            row["annotation_role"] = "person_context"
            style = row.get("style") if isinstance(row.get("style"), dict) else {}
            row["style"] = {
                **copy.deepcopy(style),
                "reason": "person_detection",
            }
        output.append(_sanitize_value(row))
    return output


def _object_to_shadow_annotation(
    obj: dict[str, Any],
    *,
    match: dict[str, Any],
    source_id: str,
    camera_id: str,
) -> dict[str, Any] | None:
    object_type = obj.get("object_type")
    if object_type not in {"person", "face", "behavior_event"}:
        return None

    bbox = _sanitize_bbox(obj.get("bbox"))
    if bbox is None:
        return None

    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": ANNOTATION_SOURCE,
        "source_id": source_id,
        "camera_id": camera_id,
        "clip_frame_index": _to_int_or_none(match.get("clip_frame_index")),
        "t_ms": _to_int_or_none(match.get("t_ms")),
        "frame_pts": _to_int_or_none(match.get("frame_pts")),
        "frame_uuid": match.get("frame_uuid"),
        "object_type": object_type,
        "track_id": _text_or_none(obj.get("track_id")),
        "bbox": bbox,
        "label": {"kind": _label_kind(object_type)},
        "quality": _sanitize_value(obj.get("quality") or {}),
        "match_type": match.get("match_type"),
    }
    message = match.get("annotation_message")
    if isinstance(message, dict):
        stream_id = _text_or_none(message.get("_stream_id"))
        if stream_id is not None:
            row["frame_annotation_redis_id"] = stream_id
            row["source_message_id"] = stream_id
        stream_order = _to_int_or_none(message.get("_stream_order"))
        if stream_order is not None:
            row["frame_annotation_stream_order"] = stream_order
        created_at = _text_or_none(message.get("created_at"))
        if created_at is not None:
            row["frame_annotation_created_at"] = created_at
        frame_num = _to_int_or_none(message.get("frame_num"))
        if frame_num is not None:
            row["source_frame_num"] = frame_num
            row["frame_num"] = frame_num
    original_object_index = _to_int_or_none(obj.get("original_object_index"))
    if original_object_index is not None:
        row["original_object_index"] = original_object_index
    source_fingerprint = _text_or_none(obj.get("source_object_fingerprint"))
    if source_fingerprint is not None:
        row["source_object_fingerprint"] = source_fingerprint

    source_observation_id = _text_or_none(obj.get("source_observation_id"))
    if source_observation_id is not None:
        row["source_observation_id"] = source_observation_id

    annotation_role = _text_or_none(obj.get("annotation_role"))
    if annotation_role is not None:
        row["annotation_role"] = annotation_role
    elif object_type == "person":
        row["annotation_role"] = "person_context"

    if object_type == "person":
        style = obj.get("style") if isinstance(obj.get("style"), dict) else {}
        row["style"] = {
            **copy.deepcopy(style),
            "reason": "person_detection",
        }
        pose = obj.get("pose")
        if isinstance(pose, dict):
            row["pose"] = _sanitize_value(copy.deepcopy(pose))

    row["sidecar_row_fingerprint"] = _sidecar_row_fingerprint(row)
    return ensure_person_context_annotations([row])[0]


def _label_kind(object_type: str) -> str:
    if object_type == "person":
        return "person"
    if object_type == "face":
        return "unknown_face"
    return "behavior_event"


def _sanitize_bbox(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    xyxy = value.get("xyxy")
    if not isinstance(xyxy, list) or len(xyxy) != 4:
        return None
    return {
        "format": "xyxy",
        "coordinate_space": value.get("coordinate_space", "pixel"),
        "xyxy": [float(item) for item in xyxy],
        "confidence": _to_float_or_none(value.get("confidence")),
    }


def _sidecar_row_fingerprint(row: dict[str, Any]) -> str:
    payload = {
        "source_id": row.get("source_id"),
        "camera_id": row.get("camera_id"),
        "frame_pts": row.get("frame_pts"),
        "frame_uuid": row.get("frame_uuid"),
        "object_type": row.get("object_type"),
        "source_observation_id": row.get("source_observation_id"),
        "track_id": row.get("track_id"),
        "bbox": row.get("bbox"),
        "pose": row.get("pose"),
        "original_object_index": row.get("original_object_index"),
        "frame_annotation_redis_id": row.get("frame_annotation_redis_id"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_VECTOR_FIELDS or lowered in FORBIDDEN_IMAGE_FIELDS:
                continue
            if lowered == "embedding":
                continue
            result[str(key)] = _sanitize_value(child)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value]
    return value


def _count_forbidden_fields(value: Any, forbidden: set[str]) -> int:
    if isinstance(value, dict):
        count = 0
        for key, child in value.items():
            count += 1 if str(key).lower() in forbidden else 0
            count += _count_forbidden_fields(child, forbidden)
        return count
    if isinstance(value, list):
        return sum(_count_forbidden_fields(item, forbidden) for item in value)
    return 0


def _to_int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None
