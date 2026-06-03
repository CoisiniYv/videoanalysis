"""PersonPoseObservation dataclass and support functions."""

from __future__ import annotations

from dataclasses import dataclass, field
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
