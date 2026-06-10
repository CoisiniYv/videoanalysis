"""Build frame annotation messages from Savant-like frame objects.

This module intentionally imports no Savant classes. The pyfunc passes Savant
runtime objects in, while pure tests pass small fakes with the same attributes.
The final message is validated against the frame annotation contract before it leaves the
builder.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from custom.models.face_events import build_face_source_observation_id
from custom.models.pose import is_valid_track_id


SCHEMA_VERSION = "1.0"
MESSAGE_TYPE = "frame_annotation"
DEFAULT_PRODUCER = "savant-security"
DEFAULT_TTL_SECONDS = 120
DEFAULT_MAX_OBJECTS_PER_FRAME = 100
COCO17_KEYPOINT_NAMES = (
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
)


@dataclass(frozen=True)
class FrameAnnotationBuildConfig:
    """Configuration for building one frame annotation message."""

    producer: str = DEFAULT_PRODUCER
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    max_objects_per_frame: int = DEFAULT_MAX_OBJECTS_PER_FRAME
    include_keypoints: str = "compact"
    include_landmarks: str = "compact"
    include_embedding: bool = False


def build_frame_annotation_message(
    *,
    source_id: str,
    camera_id: str,
    frame_objects: Iterable[Any],
    frame_pts: int | None = None,
    frame_uuid: str | None = None,
    keyframe_uuid: str | None = None,
    previous_keyframe_uuid: str | None = None,
    keyframe_pts: int | None = None,
    frame_dts: int | None = None,
    duration: int | None = None,
    time_base: str | None = None,
    frame_num: int | None = None,
    timestamp_ms: int | None = None,
    runtime_epoch_id: str | None = None,
    created_at: str | None = None,
    config: FrameAnnotationBuildConfig | None = None,
    validator: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a validated frame annotation message.

    The input objects are only read. No object metadata is mutated or copied
    back to the caller.
    """

    cfg = config or FrameAnnotationBuildConfig()
    objects: list[dict[str, Any]] = []
    face_index = 0
    raw_objects = list(frame_objects)

    for object_index, obj in enumerate(raw_objects):
        if _is_person_object(obj):
            person = _build_person_object(obj, object_index, cfg)
            if person is not None:
                objects.append(person)
            continue

        if _is_face_object(obj):
            face = _build_face_object(
                obj,
                object_index=object_index,
                face_index=face_index,
                source_id=source_id,
                timestamp_ms=timestamp_ms,
                config=cfg,
            )
            face_index += 1
            if face is not None:
                objects.append(face)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "producer": cfg.producer,
        "message_type": MESSAGE_TYPE,
        "source_id": source_id,
        "camera_id": camera_id,
        "frame_pts": frame_pts,
        "frame_uuid": frame_uuid,
        "keyframe_uuid": keyframe_uuid,
        "previous_keyframe_uuid": previous_keyframe_uuid,
        "keyframe_pts": keyframe_pts,
        "frame_dts": frame_dts,
        "duration": duration,
        "time_base": time_base,
        "frame_num": frame_num,
        "timestamp_ms": timestamp_ms,
        "runtime_epoch_id": runtime_epoch_id,
        "objects": objects,
        "ttl_seconds": int(cfg.ttl_seconds),
        "created_at": created_at or _utc_now_iso(),
    }

    validate = validator or _load_contract_validator()
    return validate(payload, max_objects_per_frame=cfg.max_objects_per_frame)


def _build_person_object(
    obj: Any,
    object_index: int,
    config: FrameAnnotationBuildConfig,
) -> dict[str, Any] | None:
    bbox = _read_bbox(obj)
    if bbox is None:
        return None

    track_id = _read_track_id(obj)
    confidence = _read_confidence(obj)
    annotation = {
        "object_id": f"person:track:{track_id}" if track_id else f"person:index:{object_index}",
        "object_type": "person",
        "track_id": track_id,
        "original_object_index": int(object_index),
        "bbox": {
            "format": "xyxy",
            "xyxy": bbox,
            "confidence": confidence,
            "coordinate_space": "pixel",
        },
        "quality": {
            "person_confidence": confidence,
            "quality_status": "ok",
        },
    }

    pose_summary = _read_pose_summary(obj, include_mode=config.include_keypoints)
    if pose_summary is not None:
        annotation["pose"] = pose_summary

    annotation["source_object_fingerprint"] = _source_object_fingerprint(annotation)
    return annotation


def _build_face_object(
    obj: Any,
    *,
    object_index: int,
    face_index: int,
    source_id: str,
    timestamp_ms: int | None,
    config: FrameAnnotationBuildConfig,
) -> dict[str, Any] | None:
    bbox = _read_bbox(obj)
    if bbox is None:
        return None

    track_id = _read_track_id(obj, prefer_associated_person=True)
    track_int = _track_int(track_id)
    confidence = _read_confidence(obj)
    reid_allowed = _read_attr_value(obj, "face_reid_gate", "reid_allowed")
    reid_skip_reason = _read_attr_value(obj, "face_reid_gate", "reid_skip_reason")
    face_quality = _read_float_attr(obj, "face_reid_gate", "reid_quality_score")
    feature = _read_feature(obj)

    source_observation_id = _read_source_observation_id(obj)
    if source_observation_id is None and (reid_allowed is True or feature):
        source_observation_id = _build_face_observation_id(
            source_id=source_id,
            track_id=track_int,
            timestamp_ms=timestamp_ms,
            face_index=face_index,
        )

    annotation = {
        "object_id": (
            f"face:{source_observation_id}"
            if source_observation_id
            else f"face:index:{object_index}"
        ),
        "object_type": "face",
        "track_id": track_id,
        "source_observation_id": source_observation_id,
        "original_object_index": int(object_index),
        "bbox": {
            "format": "xyxy",
            "xyxy": bbox,
            "confidence": confidence,
            "coordinate_space": "pixel",
        },
        "quality": {
            "face_confidence": confidence,
            "face_quality": face_quality,
        },
    }

    if reid_allowed is not None:
        annotation["quality"]["reid_allowed"] = bool(reid_allowed)
    if reid_skip_reason is not None:
        annotation["quality"]["reid_skip_reason"] = str(reid_skip_reason)

    landmarks = _read_landmark_summary(obj, include_mode=config.include_landmarks)
    if landmarks is not None:
        annotation["landmarks"] = landmarks

    if feature:
        annotation["embedding"] = _embedding_summary(feature)

    annotation["source_object_fingerprint"] = _source_object_fingerprint(annotation)
    return annotation


def count_frame_annotation_objects(message: dict[str, Any]) -> dict[str, int]:
    """Return object counters for a validated frame annotation message."""

    person_count = 0
    face_count = 0
    for obj in message.get("objects") or []:
        if not isinstance(obj, dict):
            continue
        if obj.get("object_type") == "person":
            person_count += 1
        elif obj.get("object_type") == "face":
            face_count += 1
    return {"person": person_count, "face": face_count}


def _load_contract_validator() -> Callable[..., dict[str, Any]]:
    try:
        from libs.evidence_metadata.validation import validate_frame_annotation_message

        return validate_frame_annotation_message
    except Exception:
        return _validate_frame_annotation_message_compat


def _is_person_object(obj: Any) -> bool:
    label = str(getattr(obj, "label", "") or "").lower()
    element_name = str(getattr(obj, "element_name", "") or "").lower()
    return label == "person" or element_name == "yolo26_pose"


def _is_face_object(obj: Any) -> bool:
    label = str(getattr(obj, "label", "") or "").lower()
    element_name = str(getattr(obj, "element_name", "") or "").lower()
    return label == "face" or element_name == "yolov8_face"


def _read_track_id(obj: Any, *, prefer_associated_person: bool = False) -> str | None:
    if prefer_associated_person:
        associated = _read_attr_value(
            obj,
            "face_person_associator",
            "person_track_id",
        )
        if is_valid_track_id(associated):
            return str(int(associated))

    for attr_name in ("track_id", "object_id"):
        value = getattr(obj, attr_name, None)
        if is_valid_track_id(value):
            return str(int(value))
    return None


def _track_int(track_id: str | None) -> int:
    try:
        return int(track_id) if track_id else 0
    except (TypeError, ValueError):
        return 0


def _read_confidence(obj: Any) -> float | None:
    try:
        value = getattr(obj, "confidence", None)
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _read_bbox(obj: Any) -> list[float] | None:
    bbox = getattr(obj, "bbox", None)
    if isinstance(bbox, dict):
        return _bbox_from_mapping(bbox)
    if bbox is not None:
        try:
            xc = float(getattr(bbox, "xc"))
            yc = float(getattr(bbox, "yc"))
            width = float(getattr(bbox, "width"))
            height = float(getattr(bbox, "height"))
            return _cxcywh_to_xyxy(xc, yc, width, height)
        except Exception:
            pass

    rect = getattr(obj, "rect_params", None)
    if rect is not None:
        try:
            left = float(getattr(rect, "left"))
            top = float(getattr(rect, "top"))
            width = float(getattr(rect, "width"))
            height = float(getattr(rect, "height"))
            return [left, top, left + width, top + height]
        except Exception:
            pass
    return None


def _bbox_from_mapping(bbox: dict[str, Any]) -> list[float] | None:
    try:
        fmt = str(bbox.get("format") or "").lower()
        if fmt == "xyxy" and isinstance(bbox.get("xyxy"), list):
            return [float(v) for v in bbox["xyxy"][:4]]
        if fmt == "cxcywh" and isinstance(bbox.get("values"), list):
            values = [float(v) for v in bbox["values"][:4]]
            return _cxcywh_to_xyxy(*values)
        if {"x", "y", "width", "height"}.issubset(bbox):
            x = float(bbox["x"])
            y = float(bbox["y"])
            width = float(bbox["width"])
            height = float(bbox["height"])
            return [x, y, x + width, y + height]
    except Exception:
        return None
    return None


def _cxcywh_to_xyxy(xc: float, yc: float, width: float, height: float) -> list[float]:
    return [
        xc - width / 2.0,
        yc - height / 2.0,
        xc + width / 2.0,
        yc + height / 2.0,
    ]


def _read_pose_summary(obj: Any, *, include_mode: str) -> dict[str, Any] | None:
    mode = str(include_mode or "compact").strip().lower()
    if mode == "none":
        return None

    keypoints = _read_attr_sequence(obj, "yolo26_pose", "keypoints")
    if not keypoints:
        return None

    triples = _reshape_triples(keypoints)
    if not triples:
        return None

    confidences = [point[2] for point in triples]
    visible = sum(1 for confidence in confidences if confidence >= 0.25)
    summary: dict[str, Any] = {
        "keypoints_format": "coco17",
        "keypoint_coordinate_space": "pixel",
        "keypoint_count": len(triples),
        "visible_keypoint_count": visible,
        "mean_keypoint_confidence": sum(confidences) / len(confidences),
    }
    if mode == "compact":
        summary["keypoints"] = {
            "format": "coco17_xyc",
            "coordinate_space": "pixel",
            "point_count": len(triples),
        }
    elif mode in {"full", "debug"}:
        summary["keypoints"] = _named_coco17_keypoints(triples)
    return summary


def _named_coco17_keypoints(triples: list[list[float]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for index, point in enumerate(triples):
        name = COCO17_KEYPOINT_NAMES[index] if index < len(COCO17_KEYPOINT_NAMES) else f"keypoint_{index}"
        points.append(
            {
                "index": index,
                "name": name,
                "x": point[0],
                "y": point[1],
                "confidence": point[2],
            }
        )
    return points


def _read_landmark_summary(obj: Any, *, include_mode: str) -> dict[str, Any] | None:
    if include_mode == "none":
        return None

    landmarks = _read_attr_sequence(obj, "yolov8_face", "landmarks")
    if not landmarks:
        return None

    points = [float(v) for v in landmarks]
    summary: dict[str, Any] = {"status": "complete"}
    if include_mode in ("compact", "full"):
        summary["points"] = points[:10]
    return summary


def _read_feature(obj: Any) -> list[float] | None:
    for namespace in ("adaface", "reid"):
        value = _read_attr_sequence(obj, namespace, "feature")
        if value:
            return [float(v) for v in value]

    for attr_name in ("embedding", "embedding_vector", "feature", "features"):
        value = getattr(obj, attr_name, None)
        if isinstance(value, (list, tuple)) and value:
            try:
                return [float(v) for v in value]
            except Exception:
                return None
    return None


def _embedding_summary(feature: list[float]) -> dict[str, Any]:
    norm = math.sqrt(sum(value * value for value in feature)) if feature else 0.0
    return {
        "embedding_model": "adaface",
        "embedding_dim": len(feature),
        "embedding_norm": norm,
        "embedding_status": "generated",
        "embedding_included": False,
    }


def _source_object_fingerprint(annotation: dict[str, Any]) -> str:
    payload = {
        "object_type": annotation.get("object_type"),
        "object_id": annotation.get("object_id"),
        "track_id": annotation.get("track_id"),
        "source_observation_id": annotation.get("source_observation_id"),
        "original_object_index": annotation.get("original_object_index"),
        "bbox": annotation.get("bbox"),
        "pose": annotation.get("pose"),
        "landmarks": annotation.get("landmarks"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _read_source_observation_id(obj: Any) -> str | None:
    value = getattr(obj, "source_observation_id", None)
    if _has_text(value):
        return str(value)
    for namespace in ("face_observation_exporter", "face_reid_gate", "identity"):
        value = _read_attr_value(obj, namespace, "source_observation_id")
        if _has_text(value):
            return str(value)
    return None


def _build_face_observation_id(
    *,
    source_id: str,
    track_id: int,
    timestamp_ms: int | None,
    face_index: int,
) -> str | None:
    if timestamp_ms is None:
        return None
    source_observation_id = build_face_source_observation_id(
        source_id,
        track_id,
        int(timestamp_ms),
    )
    if face_index > 0:
        source_observation_id = f"{source_observation_id}:{face_index}"
    return source_observation_id


def _read_float_attr(obj: Any, namespace: str, name: str) -> float | None:
    value = _read_attr_value(obj, namespace, name)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _read_attr_sequence(obj: Any, namespace: str, name: str) -> list[Any] | None:
    value = _read_attr_value(obj, namespace, name)
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        return None
    try:
        return list(value)
    except Exception:
        return None


def _read_attr_value(obj: Any, namespace: str, name: str) -> Any:
    try:
        attr = obj.get_attr_meta(namespace, name)
        if attr is not None:
            return getattr(attr, "value", None)
    except Exception:
        return None
    return None


def _reshape_triples(values: list[Any]) -> list[list[float]]:
    if len(values) % 3 != 0:
        return []
    triples = []
    for index in range(0, len(values), 3):
        try:
            triples.append([
                float(values[index]),
                float(values[index + 1]),
                float(values[index + 2]),
            ])
        except Exception:
            return []
    return triples


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_frame_annotation_message_compat(
    payload: dict[str, Any],
    *,
    max_objects_per_frame: int = DEFAULT_MAX_OBJECTS_PER_FRAME,
) -> dict[str, Any]:
    """Runtime fallback for containers that only mount ``modules/savant_security``.

    Repo tests and local tooling use the canonical frame annotation validator from
    ``libs.evidence_metadata``. This fallback preserves the same hard safety
    rules so a gated Savant prototype can still fail closed instead of
    exporting unchecked messages when the root library is not mounted.
    """

    data = copy.deepcopy(payload)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid_schema_version")
    if data.get("message_type") != MESSAGE_TYPE:
        raise ValueError("invalid_message_type")
    if not _has_text(data.get("source_id")):
        raise ValueError("missing_source_id")
    if not _has_text(data.get("camera_id")):
        raise ValueError("missing_camera_id")
    if data.get("frame_pts") is None and not _has_text(data.get("frame_uuid")):
        raise ValueError("missing_frame_anchor")
    for field_name in ("keyframe_uuid", "previous_keyframe_uuid", "time_base"):
        value = data.get(field_name)
        if value is not None and not _has_text(value):
            raise ValueError(f"missing_{field_name}")
    for field_name in ("keyframe_pts", "frame_dts", "duration"):
        value = data.get(field_name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"invalid_{field_name}")
    ttl_seconds = data.get("ttl_seconds")
    if not isinstance(ttl_seconds, int) or ttl_seconds < 1 or ttl_seconds > 3600:
        raise ValueError("invalid_ttl_seconds")
    objects = data.get("objects")
    if not isinstance(objects, list):
        raise ValueError("invalid_objects")
    if len(objects) > max_objects_per_frame:
        raise ValueError("too_many_objects")
    _scan_forbidden(data, path="frame_annotation", allow_embedding_summary=True)
    for obj in objects:
        _validate_object_compat(obj)
    return data


def _validate_object_compat(obj: Any) -> None:
    if not isinstance(obj, dict):
        raise ValueError("invalid_object")
    if obj.get("object_type") not in {"person", "face", "behavior_event"}:
        raise ValueError("invalid_object_type")
    if not _has_text(obj.get("object_id")):
        raise ValueError("missing_object_id")
    bbox = obj.get("bbox")
    if not isinstance(bbox, dict) or bbox.get("format") != "xyxy":
        raise ValueError("invalid_bbox")
    if bbox.get("coordinate_space") != "pixel":
        raise ValueError("invalid_bbox_coordinate_space")
    xyxy = bbox.get("xyxy")
    if not isinstance(xyxy, list) or len(xyxy) != 4:
        raise ValueError("invalid_bbox")
    coords = [float(v) for v in xyxy]
    if not all(math.isfinite(v) for v in coords) or coords[2] < coords[0] or coords[3] < coords[1]:
        raise ValueError("invalid_bbox")
    confidence = bbox.get("confidence")
    if confidence is not None and (float(confidence) < 0.0 or float(confidence) > 1.0):
        raise ValueError("invalid_bbox_confidence")
    embedding = obj.get("embedding")
    if embedding is not None:
        if not isinstance(embedding, dict) or embedding.get("embedding_included") is not False:
            raise ValueError("embedding_vector_forbidden")
        allowed = {
            "embedding_model",
            "embedding_dim",
            "embedding_norm",
            "embedding_status",
            "embedding_included",
        }
        if any(key not in allowed for key in embedding):
            raise ValueError("embedding_vector_forbidden")
    pose = obj.get("pose")
    if pose is not None:
        _validate_pose_compat(pose)
    if obj.get("object_type") == "face":
        source_observation_id = obj.get("source_observation_id")
        quality = obj.get("quality") if isinstance(obj.get("quality"), dict) else {}
        if quality.get("reid_allowed") is True and not _has_text(source_observation_id):
            raise ValueError("missing_source_observation_id_for_reid_face")
        if (
            isinstance(embedding, dict)
            and embedding.get("embedding_status") == "generated"
            and not _has_text(source_observation_id)
        ):
            raise ValueError("missing_source_observation_id_for_generated_embedding")


def _validate_pose_compat(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("invalid_pose")
    keypoints = value.get("keypoints")
    if keypoints is None:
        return
    if isinstance(keypoints, list):
        if keypoints and all(isinstance(point, dict) for point in keypoints):
            if len(keypoints) != 17:
                raise ValueError("invalid_pose_keypoints")
            for point in keypoints:
                index = point.get("index")
                if not isinstance(index, int) or index < 0 or index >= 17:
                    raise ValueError("invalid_pose_keypoints")
                for field in ("x", "y", "confidence"):
                    number = float(point.get(field))
                    if not math.isfinite(number):
                        raise ValueError("invalid_pose_keypoints")
            return
        if keypoints and all(isinstance(point, list) for point in keypoints):
            if len(keypoints) != 17:
                raise ValueError("invalid_pose_keypoints")
            for point in keypoints:
                if len(point) < 3:
                    raise ValueError("invalid_pose_keypoints")
                if not all(math.isfinite(float(item)) for item in point[:3]):
                    raise ValueError("invalid_pose_keypoints")
            return
    if isinstance(keypoints, dict):
        point_count = keypoints.get("point_count")
        if point_count is not None and int(point_count) != 17:
            raise ValueError("invalid_pose_keypoints")
        return
    raise ValueError("invalid_pose_keypoints")


def _scan_forbidden(
    value: Any,
    *,
    path: str,
    allow_embedding_summary: bool = False,
) -> None:
    forbidden_image = {
        "crop_bytes",
        "image_bytes",
        "raw_frame",
        "jpeg",
        "png",
        "base64_image",
        "frame_bytes",
    }
    forbidden_vector = {
        "embedding_vector",
        "vector",
        "features",
        "feature",
        "embedding_values",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in forbidden_image:
                raise ValueError(f"image_bytes_forbidden:{path}.{key}")
            if lowered in forbidden_vector:
                raise ValueError(f"embedding_vector_forbidden:{path}.{key}")
            child_allow_embedding = allow_embedding_summary or lowered == "embedding"
            _scan_forbidden(
                child,
                path=f"{path}.{key}",
                allow_embedding_summary=child_allow_embedding,
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_forbidden(
                child,
                path=f"{path}[{index}]",
                allow_embedding_summary=allow_embedding_summary,
            )
