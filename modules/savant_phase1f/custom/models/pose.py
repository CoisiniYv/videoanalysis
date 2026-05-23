"""PersonPoseObservation dataclass and support functions.

Provides the internal representation of a detected person with pose,
independent of Savant/DeepStream metadata structures.  This is the
canonical observation type that behavior rules consume downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

# ---------------------------------------------------------------------------
# COCO 17 keypoint names (index order matching YOLO26-pose output)
# ---------------------------------------------------------------------------
COCO17_KEYPOINT_NAMES: List[str] = [
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
]

# Sentinel value from DeepStream when no tracker is active
_UINT64_MAX: int = 18446744073709551615


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BBox:
    """Bounding box in frame coordinates (top-left origin).

    The primary representation uses top-left (x, y) so that behavior rules
    can directly compare against zone polygons without conversion.

    Properties:
        center: ``(xc, yc)`` tuple derived from ``(x + w/2, y + h/2)``.
        foot_point: ``(xf, yf)`` at the bottom-centre of the box, suitable
            for ground-plane checks.
    """

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
    """A single keypoint with COCO17 name."""

    x: float = 0.0
    y: float = 0.0
    confidence: float = 0.0
    name: str = ""


@dataclass
class PersonPoseObservation:
    """Canonical observation for a detected person with pose.

    This is the primary data type that behavior rules operate on.
    Fields are intentionally flat (no nested Savant metadata) so that
    the object can be serialized, tested, and inspected independently.
    """

    source_id: str = ""
    camera_id: str = ""
    frame_id: int = 0
    timestamp_ms: int = 0
    bbox: BBox = field(default_factory=BBox)
    confidence: float = 0.0
    track_id: int = 0
    keypoints: List[Keypoint] = field(default_factory=list)
    keypoint_confidence: float = 0.0


# ---------------------------------------------------------------------------
# Keypoint parsing
# ---------------------------------------------------------------------------


def parse_keypoints(value: Any) -> List[Keypoint]:
    """Parse a raw keypoints value into a list of 17 Keypoint objects.

    Accepts:
      - flat Python list of 51 floats  (x, y, conf repeated × 17)
      - flat tuple of 51 floats
      - numpy ndarray of shape (51,) or (17, 3)

    Returns a list of 17 Keypoint objects with COCO17 names assigned.
    """
    import numpy as np

    arr = np.asarray(value, dtype=float).reshape(-1)

    if arr.shape[0] != 51:
        raise ValueError(
            f"Expected 51 values (17 keypoints × 3), got {arr.shape[0]}"
        )

    kpts_2d = arr.reshape(17, 3)
    result: List[Keypoint] = []
    for i in range(17):
        result.append(
            Keypoint(
                x=float(kpts_2d[i, 0]),
                y=float(kpts_2d[i, 1]),
                confidence=float(kpts_2d[i, 2]),
                name=COCO17_KEYPOINT_NAMES[i],
            )
        )
    return result


# ---------------------------------------------------------------------------
# Track ID validation
# ---------------------------------------------------------------------------


def is_valid_track_id(track_id: Any) -> bool:
    """Check whether *track_id* is a real tracker-assigned ID.

    Returns ``True`` only for positive integers that are not the DeepStream
    UINT64_MAX sentinel (``18446744073709551615``).
    """
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


# ---------------------------------------------------------------------------
# Adapter result
# ---------------------------------------------------------------------------


@dataclass
class AdapterResult:
    """Result of a single ``build_person_pose_observations`` call.

    Attributes:
        observations: All person observations found (including untracked).
        skipped_untracked_person_count: Persons whose track_id was invalid.
        skipped_no_keypoints_count: Persons with empty keypoints list.
        skipped_invalid_keypoints_count: Persons whose keypoints had an
            unexpected shape/format.
    """

    observations: List[PersonPoseObservation] = field(default_factory=list)
    skipped_untracked_person_count: int = 0
    skipped_no_keypoints_count: int = 0
    skipped_invalid_keypoints_count: int = 0
