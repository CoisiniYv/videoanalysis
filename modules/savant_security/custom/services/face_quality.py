"""Face quality filter — evaluates whether a face detection is good enough
for ArcFace embedding and downstream storage.

Pure Python.  No GPU, no image loading, no OpenCV.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from custom.models.faces import FaceDetection, FaceQualityResult

_DEFAULT_FACE_CONFIDENCE_THRESHOLD = 0.6
_DEFAULT_MIN_FACE_WIDTH = 24.0
_DEFAULT_MIN_FACE_HEIGHT = 24.0
_DEFAULT_QUALITY_THRESHOLD = 0.65

_WEIGHT_CONFIDENCE = 0.5
_WEIGHT_SIZE = 0.3
_WEIGHT_LANDMARK = 0.2


def evaluate_face_quality(
    face_detection: FaceDetection,
    config: Optional[Dict[str, Any]] = None,
) -> FaceQualityResult:
    """Evaluate face detection quality.

    Composite score:

        quality = confidence_score * 0.5 + size_score * 0.3 + landmark_score * 0.2

    Each sub-score is in [0.0, 1.0].  ``passed`` is ``True`` when
    ``quality >= face_quality_threshold``.

    Args:
        face_detection: A ``FaceDetection`` produced by SCRFD.
        config: Optional overrides dict with keys:
            ``face_confidence_threshold``, ``min_face_width``,
            ``min_face_height``, ``face_quality_threshold``.

    Returns:
        ``FaceQualityResult`` with scores and pass/fail verdict.
    """
    cfg = config or {}

    conf_threshold = cfg.get(
        "face_confidence_threshold", _DEFAULT_FACE_CONFIDENCE_THRESHOLD
    )
    min_w = cfg.get("min_face_width", _DEFAULT_MIN_FACE_WIDTH)
    min_h = cfg.get("min_face_height", _DEFAULT_MIN_FACE_HEIGHT)
    quality_threshold = cfg.get(
        "face_quality_threshold", _DEFAULT_QUALITY_THRESHOLD
    )

    reasons: List[str] = []

    confidence = face_detection.confidence

    face_bbox = face_detection.face_bbox
    if face_bbox is None or len(face_bbox) < 4:
        return FaceQualityResult(
            passed=False,
            quality=0.0,
            confidence_score=0.0,
            size_score=0.0,
            landmark_score=0.0,
            reasons=["missing_face_bbox"],
        )

    face_w = float(face_bbox[2]) if len(face_bbox) >= 4 else 0.0
    face_h = float(face_bbox[3]) if len(face_bbox) >= 4 else 0.0

    landmarks = face_detection.landmarks

    confidence_score = min(confidence / conf_threshold, 1.0) if conf_threshold > 0 else 1.0

    size_w_score = min(face_w / min_w, 1.0) if min_w > 0 else 1.0
    size_h_score = min(face_h / min_h, 1.0) if min_h > 0 else 1.0
    size_score = min(size_w_score, size_h_score)

    if landmarks and len(landmarks) >= 5:
        landmark_score = 1.0
    elif landmarks and len(landmarks) > 0:
        landmark_score = 0.5
    else:
        landmark_score = 0.0
        reasons.append("no_landmarks")

    quality = (
        confidence_score * _WEIGHT_CONFIDENCE
        + size_score * _WEIGHT_SIZE
        + landmark_score * _WEIGHT_LANDMARK
    )
    quality = max(0.0, min(quality, 1.0))

    if confidence < conf_threshold:
        reasons.append("low_confidence")
    if face_w < min_w:
        reasons.append("face_too_narrow")
    if face_h < min_h:
        reasons.append("face_too_short")

    # Hard gates: minimum confidence and minimum face size must be met
    # regardless of composite score.  The composite quality score is an
    # additional filter on top (landmark completeness, size margin, etc.).
    hard_fail = (
        confidence < conf_threshold
        or face_w < min_w
        or face_h < min_h
    )

    passed = (not hard_fail) and (quality >= quality_threshold)

    return FaceQualityResult(
        passed=passed,
        quality=quality,
        confidence_score=confidence_score,
        size_score=size_score,
        landmark_score=landmark_score,
        reasons=reasons,
    )
