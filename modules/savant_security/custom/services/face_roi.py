"""Head ROI selector — estimates a head region from a person bbox + keypoints.

Pure Python.  No GPU, no image loading, no SCRFD invocation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from custom.models.pose import BBox, Keypoint


_FACE_KEYPOINT_NAMES = {"nose", "left_eye", "right_eye", "left_ear", "right_ear"}

_DEFAULT_MIN_PERSON_HEIGHT = 80
_DEFAULT_KEYPOINT_CONFIDENCE_THRESHOLD = 0.35
_DEFAULT_HEAD_BOX_SCALE = 1.6
_DEFAULT_FALLBACK_TOP_RATIO = 0.45
_DEFAULT_MIN_ROI_SIZE = 16


def estimate_head_roi_from_person(
    person_bbox: BBox,
    keypoints: Optional[List[Keypoint]] = None,
    source_id: str = "",
    camera_id: str = "",
    track_id: int = 0,
    timestamp_ms: int = 0,
    frame_width: Optional[float] = None,
    frame_height: Optional[float] = None,
    config: Optional[Dict[str, Any]] = None,
) -> "FaceROI | None":  # noqa: F821
    """Estimate a head ROI from a person detection.

    Strategy (in priority order):

    1. If nose / eyes / ears keypoints are present and above the
       confidence threshold, estimate the head ROI from the bounding
       box of those keypoints, expanded by ``head_box_scale``.
    2. Otherwise, fall back to the top portion of the person bbox
       (ratio controlled by ``fallback_top_ratio``).

    Returns ``None`` when the person is too small (``person_height <
    min_person_height``).

    Args:
        person_bbox: Person bounding box in frame coordinates.
        keypoints: Optional COCO-17 keypoints list.
        source_id: Source identifier (transparent passthrough).
        camera_id: Camera identifier (transparent passthrough).
        track_id: Track identifier (transparent passthrough).
        timestamp_ms: Frame timestamp in milliseconds.
        frame_width: Frame width for clamping (optional).
        frame_height: Frame height for clamping (optional).
        config: Optional overrides dict with keys:
            ``min_person_height``, ``keypoint_confidence_threshold``,
            ``head_box_scale``, ``fallback_top_ratio``, ``min_roi_size``.

    Returns:
        ``FaceROI`` or ``None``.
    """
    from custom.models.faces import FaceROI

    cfg = config or {}

    min_person_height = cfg.get("min_person_height", _DEFAULT_MIN_PERSON_HEIGHT)
    kp_conf_threshold = cfg.get(
        "keypoint_confidence_threshold", _DEFAULT_KEYPOINT_CONFIDENCE_THRESHOLD
    )
    head_box_scale = cfg.get("head_box_scale", _DEFAULT_HEAD_BOX_SCALE)
    fallback_top_ratio = cfg.get("fallback_top_ratio", _DEFAULT_FALLBACK_TOP_RATIO)
    min_roi_size = cfg.get("min_roi_size", _DEFAULT_MIN_ROI_SIZE)

    if person_bbox.height < min_person_height:
        return None

    face_kps = _filter_face_keypoints(keypoints, kp_conf_threshold)

    if face_kps:
        roi_x, roi_y, roi_w, roi_h = _roi_from_keypoints(face_kps, head_box_scale)
        method = "keypoints"
        confidence = sum(kp.confidence for kp in face_kps) / len(face_kps)
    else:
        roi_x, roi_y, roi_w, roi_h = _roi_from_bbox_fallback(
            person_bbox, fallback_top_ratio, head_box_scale
        )
        method = "bbox_fallback"
        confidence = 0.0

    roi_w = max(roi_w, min_roi_size)
    roi_h = max(roi_h, min_roi_size)

    roi_x, roi_y, roi_w, roi_h = _clamp_to_frame(
        roi_x, roi_y, roi_w, roi_h, frame_width, frame_height
    )

    if roi_w <= 0 or roi_h <= 0:
        return None

    return FaceROI(
        source_id=source_id,
        camera_id=camera_id,
        track_id=track_id,
        timestamp_ms=timestamp_ms,
        x=roi_x,
        y=roi_y,
        width=roi_w,
        height=roi_h,
        confidence=confidence,
        method=method,
    )


def _filter_face_keypoints(
    keypoints: Optional[List[Keypoint]],
    threshold: float,
) -> List[Keypoint]:
    if not keypoints:
        return []
    return [
        kp
        for kp in keypoints
        if kp.name in _FACE_KEYPOINT_NAMES and kp.confidence >= threshold
    ]


def _roi_from_keypoints(
    face_kps: List[Keypoint],
    scale: float,
):
    xs = [kp.x for kp in face_kps]
    ys = [kp.y for kp in face_kps]
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    raw_w = max(xs) - min(xs)
    raw_h = max(ys) - min(ys)
    w = max(raw_w, 1.0) * scale
    h = max(raw_h, 1.0) * scale
    x = cx - w / 2.0
    y = cy - h / 2.0
    return x, y, w, h


def _roi_from_bbox_fallback(
    bbox: BBox,
    top_ratio: float,
    scale: float,
):
    head_h = bbox.height * top_ratio * scale
    head_w = bbox.width * scale
    head_cx = bbox.x + bbox.width / 2.0
    head_cy = bbox.y + head_h / 2.0
    x = head_cx - head_w / 2.0
    y = max(bbox.y - head_h * 0.1, 0.0)
    return x, y, head_w, head_h


def _clamp_to_frame(
    x: float,
    y: float,
    w: float,
    h: float,
    frame_width: Optional[float],
    frame_height: Optional[float],
):
    if frame_width is not None and frame_width > 0:
        x = max(0.0, min(x, frame_width - 1))
        w = min(w, frame_width - x)
    if frame_height is not None and frame_height > 0:
        y = max(0.0, min(y, frame_height - 1))
        h = min(h, frame_height - y)
    return x, y, max(w, 0.0), max(h, 0.0)
