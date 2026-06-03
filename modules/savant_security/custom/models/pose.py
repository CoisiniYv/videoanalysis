"""PersonPoseObservation dataclass and support functions."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, List, Tuple

COCO17_KEYPOINT_NAMES: List[str] = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
]

_UINT64_MAX: int = 18446744073709551615


@dataclass
class BBox:
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    @property
    def center(self) -> Tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    @property
    def foot_point(self) -> Tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height)

    @property
    def xyxy(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)


@dataclass
class Keypoint:
    x: float = 0.0
    y: float = 0.0
    confidence: float = 0.0
    name: str = ""


@dataclass
class PersonPoseObservation:
    source_id: str = ""
    camera_id: str = ""
    frame_id: int = 0
    timestamp_ms: int = 0
    bbox: BBox = field(default_factory=BBox)
    confidence: float = 0.0
    track_id: int = 0
    keypoints: List[Keypoint] = field(default_factory=list)
    keypoint_confidence: float = 0.0


@dataclass
class AdapterResult:
    observations: List[PersonPoseObservation] = field(default_factory=list)
    skipped_untracked_person_count: int = 0
    skipped_no_keypoints_count: int = 0
    skipped_invalid_keypoints_count: int = 0


@dataclass(frozen=True)
class PersonQualityGateConfig:
    """Minimum quality requirements before a person enters behavior rules."""

    min_confidence: float = 0.25
    min_width: float = 20.0
    min_height: float = 40.0
    min_visible_keypoints: int = 0
    keypoint_threshold: float = 0.25
    max_bbox_area_ratio: float = 0.9
    min_aspect_ratio: float = 0.1
    max_aspect_ratio: float = 4.0


@dataclass(frozen=True)
class PersonQualityDecision:
    accepted: bool
    reason: str = "accepted"
    visible_keypoint_count: int = 0
    bbox_was_clamped: bool = False
    bbox: BBox = field(default_factory=BBox)


@dataclass
class PersonQualityGateStats:
    raw_person_detection_count: int = 0
    accepted_person_detection_count: int = 0
    filtered_low_confidence_count: int = 0
    filtered_small_bbox_count: int = 0
    filtered_invalid_bbox_count: int = 0
    filtered_aspect_ratio_count: int = 0
    filtered_keypoints_count: int = 0
    clamped_bbox_count: int = 0

    def record(self, decision: PersonQualityDecision) -> None:
        self.raw_person_detection_count += 1
        if decision.bbox_was_clamped:
            self.clamped_bbox_count += 1
        if decision.accepted:
            self.accepted_person_detection_count += 1
            return
        if decision.reason == "low_confidence":
            self.filtered_low_confidence_count += 1
        elif decision.reason == "small_bbox":
            self.filtered_small_bbox_count += 1
        elif decision.reason == "invalid_bbox":
            self.filtered_invalid_bbox_count += 1
        elif decision.reason == "aspect_ratio":
            self.filtered_aspect_ratio_count += 1
        elif decision.reason == "keypoints":
            self.filtered_keypoints_count += 1
        else:
            self.filtered_invalid_bbox_count += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "raw_person_detection_count": self.raw_person_detection_count,
            "accepted_person_detection_count": self.accepted_person_detection_count,
            "filtered_low_confidence_count": self.filtered_low_confidence_count,
            "filtered_small_bbox_count": self.filtered_small_bbox_count,
            "filtered_invalid_bbox_count": self.filtered_invalid_bbox_count,
            "filtered_aspect_ratio_count": self.filtered_aspect_ratio_count,
            "filtered_keypoints_count": self.filtered_keypoints_count,
            "clamped_bbox_count": self.clamped_bbox_count,
        }


@dataclass
class PersonQualityGateResult:
    accepted_observations: List[PersonPoseObservation] = field(default_factory=list)
    stats: PersonQualityGateStats = field(default_factory=PersonQualityGateStats)
    decisions: List[PersonQualityDecision] = field(default_factory=list)


def parse_keypoints(value: Any) -> List[Keypoint]:
    import numpy as np
    arr = np.asarray(value, dtype=float).reshape(-1)
    if arr.shape[0] != 51:
        raise ValueError(f"Expected 51 values, got {arr.shape[0]}")
    kpts_2d = arr.reshape(17, 3)
    result: List[Keypoint] = []
    for i in range(17):
        result.append(Keypoint(
            x=float(kpts_2d[i, 0]),
            y=float(kpts_2d[i, 1]),
            confidence=float(kpts_2d[i, 2]),
            name=COCO17_KEYPOINT_NAMES[i],
        ))
    return result


def visible_keypoint_count(
    keypoints: List[Keypoint],
    threshold: float = 0.25,
) -> int:
    return sum(1 for kp in keypoints if kp.confidence >= threshold)


def evaluate_person_quality(
    obs: PersonPoseObservation,
    config: PersonQualityGateConfig | None = None,
    *,
    frame_width: float | None = None,
    frame_height: float | None = None,
) -> PersonQualityDecision:
    """Return the quality decision for one person observation.

    Bboxes that only exceed the frame by a small amount are clamped before
    minimum-size and area checks. Fully out-of-frame or degenerate boxes are
    rejected.
    """

    cfg = config or PersonQualityGateConfig()
    bbox = obs.bbox
    x1, y1, x2, y2 = bbox.xyxy
    bbox_was_clamped = False

    if obs.confidence < cfg.min_confidence:
        return PersonQualityDecision(
            accepted=False,
            reason="low_confidence",
            visible_keypoint_count=visible_keypoint_count(
                obs.keypoints, cfg.keypoint_threshold
            ),
            bbox=bbox,
        )

    if x2 <= x1 or y2 <= y1:
        return PersonQualityDecision(
            accepted=False,
            reason="invalid_bbox",
            visible_keypoint_count=visible_keypoint_count(
                obs.keypoints, cfg.keypoint_threshold
            ),
            bbox=bbox,
        )

    if frame_width and frame_height:
        clamped_x1 = min(max(x1, 0.0), float(frame_width))
        clamped_y1 = min(max(y1, 0.0), float(frame_height))
        clamped_x2 = min(max(x2, 0.0), float(frame_width))
        clamped_y2 = min(max(y2, 0.0), float(frame_height))
        bbox_was_clamped = (
            clamped_x1 != x1
            or clamped_y1 != y1
            or clamped_x2 != x2
            or clamped_y2 != y2
        )
        x1, y1, x2, y2 = clamped_x1, clamped_y1, clamped_x2, clamped_y2
        if x2 <= x1 or y2 <= y1:
            return PersonQualityDecision(
                accepted=False,
                reason="invalid_bbox",
                visible_keypoint_count=visible_keypoint_count(
                    obs.keypoints, cfg.keypoint_threshold
                ),
                bbox=BBox(x=x1, y=y1, width=max(x2 - x1, 0.0), height=max(y2 - y1, 0.0)),
                bbox_was_clamped=bbox_was_clamped,
            )
        bbox = BBox(x=x1, y=y1, width=x2 - x1, height=y2 - y1)

    if bbox.width < cfg.min_width or bbox.height < cfg.min_height:
        return PersonQualityDecision(
            accepted=False,
            reason="small_bbox",
            visible_keypoint_count=visible_keypoint_count(
                obs.keypoints, cfg.keypoint_threshold
            ),
            bbox=bbox,
            bbox_was_clamped=bbox_was_clamped,
        )

    aspect_ratio = bbox.width / bbox.height if bbox.height > 0 else 0.0
    if aspect_ratio < cfg.min_aspect_ratio or aspect_ratio > cfg.max_aspect_ratio:
        return PersonQualityDecision(
            accepted=False,
            reason="aspect_ratio",
            visible_keypoint_count=visible_keypoint_count(
                obs.keypoints, cfg.keypoint_threshold
            ),
            bbox=bbox,
            bbox_was_clamped=bbox_was_clamped,
        )

    if frame_width and frame_height and cfg.max_bbox_area_ratio > 0:
        frame_area = float(frame_width) * float(frame_height)
        if frame_area > 0 and (bbox.width * bbox.height / frame_area) > cfg.max_bbox_area_ratio:
            return PersonQualityDecision(
                accepted=False,
                reason="invalid_bbox",
                visible_keypoint_count=visible_keypoint_count(
                    obs.keypoints, cfg.keypoint_threshold
                ),
                bbox=bbox,
                bbox_was_clamped=bbox_was_clamped,
            )

    visible_count = visible_keypoint_count(obs.keypoints, cfg.keypoint_threshold)
    if visible_count < cfg.min_visible_keypoints:
        return PersonQualityDecision(
            accepted=False,
            reason="keypoints",
            visible_keypoint_count=visible_count,
            bbox=bbox,
            bbox_was_clamped=bbox_was_clamped,
        )

    return PersonQualityDecision(
        accepted=True,
        reason="accepted",
        visible_keypoint_count=visible_count,
        bbox=bbox,
        bbox_was_clamped=bbox_was_clamped,
    )


def filter_person_pose_observations(
    observations: List[PersonPoseObservation],
    config: PersonQualityGateConfig | None = None,
    *,
    frame_width: float | None = None,
    frame_height: float | None = None,
) -> PersonQualityGateResult:
    result = PersonQualityGateResult()
    for obs in observations:
        decision = evaluate_person_quality(
            obs,
            config,
            frame_width=frame_width,
            frame_height=frame_height,
        )
        result.stats.record(decision)
        result.decisions.append(decision)
        if decision.accepted:
            if decision.bbox != obs.bbox:
                result.accepted_observations.append(replace(obs, bbox=decision.bbox))
            else:
                result.accepted_observations.append(obs)
    return result


def is_valid_track_id(track_id: Any) -> bool:
    if track_id is None:
        return False
    try:
        tid = int(track_id)
    except (ValueError, TypeError):
        return False
    if tid <= 0:
        return False
    if tid == _UINT64_MAX:
        return False
    return True
