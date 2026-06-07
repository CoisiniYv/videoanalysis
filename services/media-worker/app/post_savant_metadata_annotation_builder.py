"""Build production annotations from post-Savant video-file-sink metadata."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "2.0-c2-post-savant"
ANNOTATION_SOURCE = "post_savant_sink_metadata"
SIDECAR_ANNOTATIONS_FILE = "annotations.frame_cache.identity.jsonl"
SIDECAR_SUMMARY_FILE = "summary.frame_cache.identity.json"
PRODUCTION_TIMELINE_DOMAIN = "final_canonical_clip"

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

FORBIDDEN_VECTOR_ATTRIBUTE_NAMES = {
    "feature",
    "features",
    "embedding",
    "embedding_vector",
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


@dataclass(frozen=True)
class BuildResult:
    annotations_path: Path
    summary_path: Path
    summary: dict[str, Any]
    rows: list[dict[str, Any]]


def build_post_savant_annotation_sidecar(
    metadata_path: Path,
    output_jsonl_path: Path,
    summary_path: Path,
) -> BuildResult:
    """Read native sink metadata and write a production annotation sidecar."""

    frames = load_native_metadata(metadata_path)
    rows, summary = build_annotations_from_metadata(frames)
    output_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_jsonl_path, rows)
    _write_json(summary_path, summary)
    if summary_path.name != SIDECAR_SUMMARY_FILE:
        _write_json(summary_path.parent / SIDECAR_SUMMARY_FILE, summary)
    return BuildResult(
        annotations_path=output_jsonl_path,
        summary_path=summary_path,
        summary=summary,
        rows=rows,
    )


def load_native_metadata(path: Path) -> list[dict[str, Any]]:
    """Load video-file-sink metadata from JSONL, JSON array, or JSON object form."""

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        frames = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid metadata JSONL at line {line_number}: {exc}") from exc
            if isinstance(value, dict):
                frames.append(value)
        return frames
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("frames", "metadata", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def build_annotations_from_metadata(frames: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    frame_list = [frame for frame in frames if _is_native_frame(frame)]
    first_pts = _first_number(frame.get("pts") for frame in frame_list)
    rows: list[dict[str, Any]] = []
    counts = {
        "person": 0,
        "face": 0,
        "known_face": 0,
    }
    frames_with_objects = 0
    keypoints_count = 0
    face_landmarks_count = 0
    track_id_count = 0
    frame_uuid_count = 0
    source_observation_id_count = 0
    skipped_missing_bbox = 0
    output_contains_forbidden_vectors = 0
    output_contains_forbidden_images = 0

    for frame_index, frame in enumerate(frame_list):
        objects: list[dict[str, Any]] = []
        for object_index, native_object in enumerate(frame.get("objects") or []):
            if not isinstance(native_object, dict):
                continue
            annotation = _native_object_to_annotation(native_object, object_index=object_index)
            if annotation is None:
                if _is_supported_object(native_object):
                    skipped_missing_bbox += 1
                continue
            objects.append(annotation)
            object_type = annotation.get("object_type")
            if object_type in counts:
                counts[object_type] += 1
            if annotation.get("pose"):
                keypoints_count += len(_dict(annotation.get("pose")).get("keypoints") or [])
            if annotation.get("landmarks"):
                face_landmarks_count += len(_dict(annotation.get("landmarks")).get("points") or [])
            if annotation.get("track_id") not in (None, ""):
                track_id_count += 1
            identity = _dict(annotation.get("identity"))
            if identity.get("source_observation_id") not in (None, ""):
                source_observation_id_count += 1
        if objects:
            frames_with_objects += 1
        frame_uuid = _text_or_none(frame.get("uuid") or frame.get("frame_uuid"))
        if frame_uuid:
            frame_uuid_count += 1
        frame_pts = _number_or_none(frame.get("pts") or frame.get("frame_pts"))
        row = {
            "schema_version": SCHEMA_VERSION,
            "annotation_source": ANNOTATION_SOURCE,
            "production_ready": bool(objects),
            "displayable": bool(objects),
            "frame_index": frame_index,
            "clip_frame_index": frame_index,
            "frame_pts": frame_pts,
            "frame_uuid": frame_uuid,
            "timestamp_ms": _number_or_none(frame.get("timestamp_ms")),
            "time_offset_ms": _time_offset_ms(frame_pts, first_pts),
            "source_id": _text_or_none(frame.get("source_id")),
            "width": _number_or_none(frame.get("width")),
            "height": _number_or_none(frame.get("height")),
            "objects": objects,
        }
        output_contains_forbidden_vectors += _count_forbidden_vectors(row)
        output_contains_forbidden_images += _count_forbidden_fields(row, FORBIDDEN_IMAGE_FIELDS)
        rows.append(row)

    production_ready = bool(rows) and frames_with_objects > 0
    annotation_status = "complete" if production_ready else "no_post_savant_objects"
    frame_identity_method = (
        "post_savant_metadata_frame_uuid"
        if frame_uuid_count
        else "post_savant_metadata_frame_pts"
        if any(row.get("frame_pts") is not None for row in rows)
        else "post_savant_metadata_order"
    )
    limitations = _limitations(
        frame_count=len(rows),
        frame_uuid_count=frame_uuid_count,
        source_observation_id_count=source_observation_id_count,
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "sidecar_type": "production",
        "timeline_domain": PRODUCTION_TIMELINE_DOMAIN,
        "annotation_source": ANNOTATION_SOURCE,
        "annotation_status": annotation_status,
        "production_ready": production_ready,
        "canonical_clip": production_ready,
        "legacy_fallback_allowed": False,
        "legacy_used_for_visual_binding": False,
        "visual_binding_status": "verified" if production_ready else "unverified",
        "visual_binding_reason": (
            "verified_same_stream_metadata" if production_ready else "no_post_savant_objects"
        ),
        "visual_evidence_status": (
            "verified_same_stream_metadata" if production_ready else "unverified"
        ),
        "evidence_visual_status": "verified" if production_ready else "unverified",
        "frame_identity_method": frame_identity_method,
        "frame_identity_confidence": "high" if frame_uuid_count else "medium" if rows else "none",
        "trigger_face_row_exists": False,
        "trigger_face_row_passed_freshness_guard": False,
        "trigger_face_row_match_type": None,
        "source_observation_id": None,
        "frame_count": len(rows),
        "frames_with_objects_count": frames_with_objects,
        "object_count": sum(counts.values()),
        "object_counts": counts,
        "person_objects_count": counts["person"],
        "face_objects_count": counts["face"],
        "known_face_objects_count": counts["known_face"],
        "keypoints_count": keypoints_count,
        "face_landmarks_count": face_landmarks_count,
        "track_id_count": track_id_count,
        "frame_uuid_count": frame_uuid_count,
        "source_observation_id_count": source_observation_id_count,
        "rows_displayable": frames_with_objects,
        "rows_total_input": len(rows),
        "rows_missing_bbox_skipped": skipped_missing_bbox,
        "embedding_vectors_in_output": output_contains_forbidden_vectors,
        "image_bytes_in_output": output_contains_forbidden_images,
        "limitations": limitations,
    }
    return rows, summary


def _native_object_to_annotation(native_object: dict[str, Any], *, object_index: int) -> dict[str, Any] | None:
    object_type = _object_type(native_object)
    if object_type is None:
        return None
    bbox_source, bbox = _bbox_from_native_object(native_object, object_type=object_type)
    if bbox is None:
        return None
    confidence = _number_or_none(native_object.get("confidence"))
    track_id = _track_id(native_object)
    annotation: dict[str, Any] = {
        "object_type": object_type,
        "namespace": _text_or_none(native_object.get("namespace")),
        "label": _label_payload(object_type),
        "native_label": _text_or_none(native_object.get("label")),
        "object_id": _text_or_none(native_object.get("id")),
        "track_id": track_id,
        "original_object_index": object_index,
        "bbox": {
            "format": "xyxy",
            "coordinate_space": "pixel",
            "xyxy": bbox,
            "confidence": confidence,
            "source": bbox_source,
        },
        "detection": {
            "confidence": confidence,
            "namespace": _text_or_none(native_object.get("namespace")),
            "label": _text_or_none(native_object.get("label")),
        },
        "quality": _quality_summary(native_object, object_type=object_type),
    }
    if object_type == "person":
        annotation["annotation_role"] = "person_context"
        pose = _pose_from_attributes(native_object.get("attributes"))
        if pose is not None:
            annotation["pose"] = pose
    if object_type == "face":
        landmarks = _landmarks_from_attributes(native_object.get("attributes"))
        if landmarks is not None:
            annotation["landmarks"] = landmarks
        annotation["identity"] = {
            "source_observation_id": _source_observation_id(native_object),
            "visual_evidence_status": "observation_only",
            "status": "unknown",
            "match_status": "not_searched",
        }
    return annotation


def _object_type(native_object: dict[str, Any]) -> str | None:
    namespace = str(native_object.get("namespace") or "")
    label = str(native_object.get("label") or "")
    if label == "person" or namespace == "yolo26_pose":
        return "person"
    if label == "face" or namespace == "yolov8_face":
        return "face"
    return None


def _is_supported_object(native_object: dict[str, Any]) -> bool:
    return _object_type(native_object) in {"person", "face"}


def _bbox_from_native_object(
    native_object: dict[str, Any],
    *,
    object_type: str,
) -> tuple[str | None, list[float] | None]:
    candidates = (
        ("track_box", native_object.get("track_box")),
        ("detection_box", native_object.get("detection_box")),
    )
    if object_type == "face":
        candidates = (
            ("detection_box", native_object.get("detection_box")),
            ("track_box", native_object.get("track_box")),
        )
    for source, box in candidates:
        xyxy = _xyxy_from_center_box(box)
        if xyxy is not None:
            return source, xyxy
    return None, None


def _xyxy_from_center_box(value: Any) -> list[float] | None:
    if not isinstance(value, dict):
        return None
    xc = _number_or_none(value.get("xc"))
    yc = _number_or_none(value.get("yc"))
    width = _number_or_none(value.get("width"))
    height = _number_or_none(value.get("height"))
    if xc is None or yc is None or width is None or height is None:
        return None
    if width <= 0 or height <= 0:
        return None
    return [
        float(xc - width / 2),
        float(yc - height / 2),
        float(xc + width / 2),
        float(yc + height / 2),
    ]


def _track_id(native_object: dict[str, Any]) -> str | None:
    direct = _text_or_none(native_object.get("track_id"))
    if direct is not None:
        return direct
    person_track_id = _attribute_scalar(native_object.get("attributes"), "face_person_associator", "person_track_id")
    return _text_or_none(person_track_id)


def _pose_from_attributes(attributes: Any) -> dict[str, Any] | None:
    vector = _attribute_float_vector(attributes, "yolo26_pose", "keypoints")
    if vector is None or len(vector) < len(COCO17_KEYPOINT_NAMES) * 3:
        return None
    keypoints = []
    for index, name in enumerate(COCO17_KEYPOINT_NAMES):
        offset = index * 3
        x = _number_or_none(vector[offset])
        y = _number_or_none(vector[offset + 1])
        confidence = _number_or_none(vector[offset + 2])
        if x is None or y is None or confidence is None:
            continue
        keypoints.append(
            {
                "index": index,
                "name": name,
                "x": x,
                "y": y,
                "confidence": confidence,
            }
        )
    if not keypoints:
        return None
    return {
        "format": "coco17",
        "keypoints_format": "coco17",
        "coordinate_space": "pixel",
        "keypoint_coordinate_space": "pixel",
        "source": "native_metadata",
        "keypoint_count": len(keypoints),
        "visible_keypoint_count": sum(1 for point in keypoints if (point.get("confidence") or 0) > 0),
        "mean_keypoint_confidence": _mean(point["confidence"] for point in keypoints),
        "keypoints": keypoints,
    }


def _landmarks_from_attributes(attributes: Any) -> dict[str, Any] | None:
    vector = _attribute_float_vector(attributes, "yolov8_face", "landmarks")
    if vector is None:
        vector = _attribute_float_vector(attributes, None, "landmarks")
    if vector is None or len(vector) < 10:
        return None
    points = []
    for index in range(5):
        offset = index * 2
        x = _number_or_none(vector[offset])
        y = _number_or_none(vector[offset + 1])
        if x is None or y is None:
            continue
        points.append([x, y])
    if not points:
        return None
    return {
        "format": "5_point",
        "coordinate_space": "pixel",
        "source": "native_metadata",
        "points": points,
    }


def _attribute_float_vector(
    attributes: Any,
    namespace: str | None,
    name: str,
) -> list[float] | None:
    for attribute in _iter_attributes(attributes):
        if namespace is not None and attribute.get("namespace") != namespace:
            continue
        if attribute.get("name") != name:
            continue
        for value in attribute.get("values") or []:
            if not isinstance(value, dict):
                continue
            payload = value.get("value")
            if not isinstance(payload, dict):
                continue
            vector = payload.get("FloatVector")
            if isinstance(vector, list):
                parsed = [_number_or_none(item) for item in vector]
                return [float(item) for item in parsed if item is not None]
    return None


def _attribute_scalar(attributes: Any, namespace: str | None, name: str) -> Any:
    for attribute in _iter_attributes(attributes):
        if namespace is not None and attribute.get("namespace") != namespace:
            continue
        if attribute.get("name") != name:
            continue
        for value in attribute.get("values") or []:
            if not isinstance(value, dict):
                continue
            payload = value.get("value")
            if not isinstance(payload, dict):
                continue
            for key in ("Integer", "Long", "String", "Float", "Double", "Boolean"):
                if key in payload:
                    return payload[key]
    return None


def _iter_attributes(attributes: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(attributes, list):
        return []
    return (attribute for attribute in attributes if isinstance(attribute, dict))


def _is_native_frame(frame: Any) -> bool:
    if not isinstance(frame, dict):
        return False
    if frame.get("type") == "VideoFrame":
        return True
    return any(key in frame for key in ("pts", "frame_pts", "uuid", "frame_uuid", "objects"))


def _source_observation_id(native_object: dict[str, Any]) -> str | None:
    direct = _text_or_none(native_object.get("source_observation_id"))
    if direct is not None:
        return direct
    return _text_or_none(
        _attribute_scalar(native_object.get("attributes"), None, "source_observation_id")
    )


def _quality_summary(native_object: dict[str, Any], *, object_type: str) -> dict[str, Any]:
    confidence = _number_or_none(native_object.get("confidence"))
    quality: dict[str, Any] = {
        "quality_status": "ok" if confidence is not None else "unknown",
        "source": "confidence",
    }
    if object_type == "person":
        quality["person_confidence"] = confidence
        pose = _pose_from_attributes(native_object.get("attributes"))
        if pose is not None:
            quality["keypoint_count"] = pose.get("keypoint_count")
            quality["mean_keypoint_confidence"] = pose.get("mean_keypoint_confidence")
    elif object_type == "face":
        quality["face_confidence"] = confidence
    return quality


def _label_payload(object_type: str) -> dict[str, Any]:
    if object_type == "person":
        return {"kind": "person"}
    return {"kind": "unknown_face"}


def _limitations(
    *,
    frame_count: int,
    frame_uuid_count: int,
    source_observation_id_count: int,
) -> list[str]:
    limitations = []
    if frame_count and frame_uuid_count < frame_count:
        limitations.append("frame_uuid_missing_in_native_metadata")
    if source_observation_id_count <= 0:
        limitations.append("source_observation_id_missing_in_native_metadata")
        limitations.append("watchlist_trigger_identity_binding_not_verified_in_c2_1")
    return limitations


def _time_offset_ms(frame_pts: float | int | None, first_pts: float | int | None) -> int | None:
    if frame_pts is None or first_pts is None:
        return None
    return int(round((float(frame_pts) - float(first_pts)) / 1_000_000.0))


def _first_number(values: Iterable[Any]) -> float | int | None:
    for value in values:
        number = _number_or_none(value)
        if number is not None:
            return number
    return None


def _number_or_none(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        if number.is_integer():
            return int(number)
        return number
    return None


def _text_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _mean(values: Iterable[float]) -> float | None:
    collected = [float(value) for value in values]
    if not collected:
        return None
    return sum(collected) / len(collected)


def _count_forbidden_vectors(value: Any) -> int:
    count = 0
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_VECTOR_ATTRIBUTE_NAMES:
                count += 1
            count += _count_forbidden_vectors(nested)
    elif isinstance(value, list):
        for item in value:
            count += _count_forbidden_vectors(item)
    return count


def _count_forbidden_fields(value: Any, names: set[str]) -> int:
    count = 0
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in names:
                count += 1
            count += _count_forbidden_fields(nested, names)
    elif isinstance(value, list):
        for item in value:
            count += _count_forbidden_fields(item, names)
    return count


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a C2 production annotation sidecar from post-Savant sink metadata."
    )
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--output-jsonl", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    args = parser.parse_args(argv)

    result = build_post_savant_annotation_sidecar(
        metadata_path=args.metadata,
        output_jsonl_path=args.output_jsonl,
        summary_path=args.summary,
    )
    print(json.dumps(result.summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
