"""Pure validation for frame annotation and identity patch payloads."""

from __future__ import annotations

import copy
import math
from typing import Any

from .contracts import (
    ALLOWED_IDENTITY_EVENT_TYPES,
    ALLOWED_OBJECT_TYPES,
    FRAME_ANNOTATION_MESSAGE_TYPE,
    IDENTITY_PATCH_MESSAGE_TYPE,
    SCHEMA_VERSION,
)

FORBIDDEN_IMAGE_FIELDS = {
    "crop_bytes",
    "image_bytes",
    "raw_frame",
    "jpeg",
    "png",
    "base64_image",
    "frame_bytes",
}

FORBIDDEN_VECTOR_FIELDS = {
    "embedding_vector",
    "vector",
    "features",
    "feature",
    "embedding_values",
}

ALLOWED_EMBEDDING_SUMMARY_FIELDS = {
    "embedding_model",
    "embedding_dim",
    "embedding_norm",
    "embedding_status",
    "embedding_included",
}

COCO17_KEYPOINT_NAMES = {
    "nose",
    "left_eye",
    "right_eye",
    "left_ear",
    "right_ear",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
}


def validate_frame_annotation_message(
    payload: dict[str, Any],
    *,
    max_objects_per_frame: int = 100,
) -> dict[str, Any]:
    """Validate and normalize a frame annotation message.

    The returned dict is a deep copy. The input object is never mutated.
    """

    if not isinstance(payload, dict):
        raise ValueError("invalid_payload")
    if max_objects_per_frame < 0:
        raise ValueError("invalid_max_objects_per_frame")

    data = copy.deepcopy(payload)

    _validate_schema(data, FRAME_ANNOTATION_MESSAGE_TYPE)
    source_id = _require_non_empty_string(data.get("source_id"), "source_id")
    camera_id = _require_non_empty_string(data.get("camera_id"), "camera_id")
    data["source_id"] = source_id
    data["camera_id"] = camera_id

    frame_pts = data.get("frame_pts")
    frame_uuid = data.get("frame_uuid")
    if frame_pts is None and not _has_text(frame_uuid):
        raise ValueError("missing_frame_anchor")
    if frame_pts is not None:
        data["frame_pts"] = _require_int(frame_pts, "frame_pts")
    if frame_uuid is not None:
        data["frame_uuid"] = _require_non_empty_string(frame_uuid, "frame_uuid")
    for field_name in ("keyframe_uuid", "previous_keyframe_uuid"):
        if data.get(field_name) is not None:
            data[field_name] = _require_non_empty_string(
                data[field_name],
                field_name,
            )
    for field_name in ("keyframe_pts", "frame_dts", "duration"):
        if data.get(field_name) is not None:
            data[field_name] = _require_int(data[field_name], field_name)
    if data.get("time_base") is not None:
        data["time_base"] = _require_non_empty_string(data["time_base"], "time_base")

    if data.get("frame_num") is not None:
        data["frame_num"] = _require_int(data["frame_num"], "frame_num")
    if data.get("timestamp_ms") is not None:
        data["timestamp_ms"] = _require_int(data["timestamp_ms"], "timestamp_ms")

    ttl_seconds = _require_int(data.get("ttl_seconds"), "ttl_seconds")
    if ttl_seconds < 1 or ttl_seconds > 3600:
        raise ValueError("invalid_ttl_seconds")
    data["ttl_seconds"] = ttl_seconds

    objects = data.get("objects")
    if not isinstance(objects, list):
        raise ValueError("invalid_objects")
    if len(objects) > max_objects_per_frame:
        raise ValueError("too_many_objects")

    _scan_top_level_forbidden(data)
    data["objects"] = [
        _validate_frame_object(obj, index)
        for index, obj in enumerate(objects)
    ]
    return data


def validate_identity_patch_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize an identity patch message."""

    if not isinstance(payload, dict):
        raise ValueError("invalid_payload")

    data = copy.deepcopy(payload)
    _scan_forbidden_fields(data, path="identity_patch", forbid_embedding_key=True)

    _validate_schema(data, IDENTITY_PATCH_MESSAGE_TYPE)
    data["source_id"] = _require_non_empty_string(data.get("source_id"), "source_id")
    data["camera_id"] = _require_non_empty_string(data.get("camera_id"), "camera_id")
    data["source_observation_id"] = _require_non_empty_string(
        data.get("source_observation_id"),
        "source_observation_id",
    )

    event_type = data.get("event_type")
    if event_type not in ALLOWED_IDENTITY_EVENT_TYPES:
        raise ValueError("invalid_event_type")

    if data.get("similarity") is not None:
        data["similarity"] = _normalize_unit_float(data["similarity"], "similarity")
    if data.get("threshold") is not None:
        data["threshold"] = _normalize_unit_float(data["threshold"], "threshold")
    if data.get("frame_pts") is not None:
        data["frame_pts"] = _require_int(data["frame_pts"], "frame_pts")
    if data.get("timestamp_ms") is not None:
        data["timestamp_ms"] = _require_int(data["timestamp_ms"], "timestamp_ms")
    if data.get("frame_uuid") is not None:
        data["frame_uuid"] = _require_non_empty_string(data["frame_uuid"], "frame_uuid")
    if data.get("bbox") is not None:
        data["bbox"] = _validate_bbox_list(data["bbox"], field_name="bbox")

    return data


def _validate_frame_object(obj: object, index: int) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError(f"invalid_object:{index}")

    normalized = copy.deepcopy(obj)
    object_type = normalized.get("object_type")
    if object_type not in ALLOWED_OBJECT_TYPES:
        raise ValueError("invalid_object_type")

    normalized["object_id"] = _require_non_empty_string(
        normalized.get("object_id"),
        "object_id",
    )
    if normalized.get("track_id") is not None:
        normalized["track_id"] = _require_non_empty_string(
            normalized["track_id"],
            "track_id",
        )
    if normalized.get("source_observation_id") is not None:
        normalized["source_observation_id"] = _require_non_empty_string(
            normalized["source_observation_id"],
            "source_observation_id",
        )

    normalized["bbox"] = _validate_bbox(normalized.get("bbox"))

    quality = normalized.get("quality", {})
    if not isinstance(quality, dict):
        raise ValueError("invalid_quality")
    normalized["quality"] = quality

    embedding_summary = normalized.get("embedding")
    if embedding_summary is not None:
        normalized["embedding"] = _validate_embedding_summary(embedding_summary)

    pose = normalized.get("pose")
    if pose is not None:
        normalized["pose"] = _validate_pose_summary(pose)

    for key, value in normalized.items():
        if key == "embedding":
            continue
        _scan_forbidden_field(key, value, path=f"objects[{index}].{key}")

    if object_type == "face":
        source_observation_id = normalized.get("source_observation_id")
        if quality.get("reid_allowed") is True and not source_observation_id:
            raise ValueError("missing_source_observation_id_for_reid_face")
        if _embedding_status(normalized) == "generated" and not source_observation_id:
            raise ValueError("missing_source_observation_id_for_generated_embedding")

    label = normalized.get("label")
    if label is not None and not isinstance(label, dict):
        raise ValueError("invalid_label")
    extra = normalized.get("extra")
    if extra is not None and not isinstance(extra, dict):
        raise ValueError("invalid_extra")

    return normalized


def _validate_pose_summary(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid_pose")
    normalized = copy.deepcopy(value)
    keypoints = normalized.get("keypoints")
    if keypoints is None:
        return normalized
    if isinstance(keypoints, dict):
        point_count = keypoints.get("point_count")
        if point_count is not None:
            count = _require_int(point_count, "pose_keypoint_count")
            if count != 17:
                raise ValueError("invalid_pose_keypoints")
            keypoints["point_count"] = count
        coordinate_space = keypoints.get("coordinate_space")
        if coordinate_space is not None and coordinate_space != "pixel":
            raise ValueError("invalid_pose_keypoint_coordinate_space")
        normalized["keypoints"] = keypoints
        return normalized
    if not isinstance(keypoints, list):
        raise ValueError("invalid_pose_keypoints")
    if keypoints and all(isinstance(point, dict) for point in keypoints):
        if len(keypoints) != 17:
            raise ValueError("invalid_pose_keypoints")
        points = []
        for expected_index, point in enumerate(keypoints):
            points.append(_validate_named_keypoint(point, expected_index))
        normalized["keypoints"] = points
        normalized["keypoint_count"] = len(points)
        normalized["keypoints_format"] = str(normalized.get("keypoints_format") or "coco17")
        normalized["keypoint_coordinate_space"] = str(
            normalized.get("keypoint_coordinate_space") or "pixel"
        )
        if normalized["keypoints_format"] != "coco17":
            raise ValueError("invalid_pose_keypoints_format")
        if normalized["keypoint_coordinate_space"] != "pixel":
            raise ValueError("invalid_pose_keypoint_coordinate_space")
        return normalized
    if keypoints and all(isinstance(point, list) for point in keypoints):
        if len(keypoints) != 17:
            raise ValueError("invalid_pose_keypoints")
        normalized["keypoints"] = [
            [
                _normalize_number(point[0], "pose_keypoint_x"),
                _normalize_number(point[1], "pose_keypoint_y"),
                _normalize_unit_float(point[2], "pose_keypoint_confidence"),
            ]
            for point in keypoints
            if len(point) >= 3
        ]
        if len(normalized["keypoints"]) != 17:
            raise ValueError("invalid_pose_keypoints")
        return normalized
    raise ValueError("invalid_pose_keypoints")


def _validate_named_keypoint(point: object, expected_index: int) -> dict[str, Any]:
    if not isinstance(point, dict):
        raise ValueError("invalid_pose_keypoints")
    index = _require_int(point.get("index"), "pose_keypoint_index")
    if index != expected_index:
        raise ValueError("invalid_pose_keypoints")
    name = _require_non_empty_string(point.get("name"), "pose_keypoint_name")
    if name not in COCO17_KEYPOINT_NAMES:
        raise ValueError("invalid_pose_keypoint_name")
    return {
        "index": index,
        "name": name,
        "x": _normalize_number(point.get("x"), "pose_keypoint_x"),
        "y": _normalize_number(point.get("y"), "pose_keypoint_y"),
        "confidence": _normalize_unit_float(
            point.get("confidence"),
            "pose_keypoint_confidence",
        ),
    }


def _validate_schema(data: dict[str, Any], message_type: str) -> None:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid_schema_version")
    if data.get("message_type") != message_type:
        raise ValueError("invalid_message_type")


def _validate_bbox(bbox: object) -> dict[str, Any]:
    if not isinstance(bbox, dict):
        raise ValueError("invalid_bbox")
    normalized = copy.deepcopy(bbox)
    if normalized.get("format") != "xyxy":
        raise ValueError("invalid_bbox_format")
    if normalized.get("coordinate_space") != "pixel":
        raise ValueError("invalid_bbox_coordinate_space")
    normalized["xyxy"] = _validate_bbox_list(normalized.get("xyxy"), field_name="xyxy")
    if normalized.get("confidence") is not None:
        normalized["confidence"] = _normalize_unit_float(
            normalized["confidence"],
            "bbox_confidence",
        )
    return normalized


def _validate_bbox_list(value: object, *, field_name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("invalid_bbox")
    coords = [_normalize_number(v, field_name) for v in value]
    x1, y1, x2, y2 = coords
    if x2 < x1 or y2 < y1:
        raise ValueError("invalid_bbox")
    return coords


def _validate_embedding_summary(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("embedding_vector_forbidden")
    for key in value:
        if key not in ALLOWED_EMBEDDING_SUMMARY_FIELDS:
            lowered = str(key).lower()
            if lowered in FORBIDDEN_VECTOR_FIELDS or lowered == "embedding":
                raise ValueError("embedding_vector_forbidden")
            raise ValueError("invalid_embedding_summary")
    _scan_forbidden_fields(value, path="embedding_summary", forbid_embedding_key=False)
    if value.get("embedding_included") is not False:
        raise ValueError("embedding_vector_forbidden")
    normalized = copy.deepcopy(value)
    if normalized.get("embedding_dim") is not None:
        dim = _require_int(normalized["embedding_dim"], "embedding_dim")
        if dim <= 0:
            raise ValueError("invalid_embedding_dim")
        normalized["embedding_dim"] = dim
    if normalized.get("embedding_norm") is not None:
        normalized["embedding_norm"] = _normalize_number(
            normalized["embedding_norm"],
            "embedding_norm",
        )
    return normalized


def _scan_top_level_forbidden(data: dict[str, Any]) -> None:
    for key, value in data.items():
        if key == "objects":
            continue
        _scan_forbidden_field(key, value, path=key)


def _scan_forbidden_fields(
    value: object,
    *,
    path: str,
    forbid_embedding_key: bool,
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered == "embedding" and forbid_embedding_key:
                raise ValueError("embedding_vector_forbidden")
            _scan_forbidden_field(key, child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_forbidden_fields(
                child,
                path=f"{path}[{index}]",
                forbid_embedding_key=forbid_embedding_key,
            )


def _scan_forbidden_field(key: object, value: object, *, path: str) -> None:
    lowered = str(key).lower()
    if lowered in FORBIDDEN_IMAGE_FIELDS:
        raise ValueError(f"image_bytes_forbidden:{path}")
    if lowered in FORBIDDEN_VECTOR_FIELDS:
        raise ValueError(f"embedding_vector_forbidden:{path}")
    if lowered == "embedding":
        raise ValueError(f"embedding_vector_forbidden:{path}")
    _scan_forbidden_fields(value, path=path, forbid_embedding_key=True)


def _embedding_status(obj: dict[str, Any]) -> str | None:
    embedding = obj.get("embedding")
    if isinstance(embedding, dict):
        status = embedding.get("embedding_status")
        if status is not None:
            return str(status)
    status = obj.get("embedding_status")
    if status is not None:
        return str(status)
    return None


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing_{field_name}")
    return value


def _require_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"invalid_{field_name}")
    return value


def _normalize_unit_float(value: object, field_name: str) -> float:
    number = _normalize_number(value, field_name)
    if number < 0.0 or number > 1.0:
        raise ValueError(f"invalid_{field_name}")
    return number


def _normalize_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid_{field_name}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"invalid_{field_name}")
    return number


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
