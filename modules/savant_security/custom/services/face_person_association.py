"""Face-Person Association — pure Python spatial matching.

Associates YOLOv8-Face face detections with YOLO26-pose person tracks
by spatial containment and upper-body preference.

No Savant imports.  Unit-testable with plain dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class BBox:
    """Axis-aligned bounding box in center-size format."""

    xc: float
    yc: float
    width: float
    height: float

    @property
    def left(self) -> float:
        return self.xc - self.width / 2.0

    @property
    def top(self) -> float:
        return self.yc - self.height / 2.0

    @property
    def right(self) -> float:
        return self.xc + self.width / 2.0

    @property
    def bottom(self) -> float:
        return self.yc + self.height / 2.0

    @property
    def area(self) -> float:
        return max(self.width, 0) * max(self.height, 0)

    def contains_point(self, x: float, y: float) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom


@dataclass
class FaceInput:
    """One face detection from YOLOv8-Face."""

    bbox: BBox
    confidence: float = 0.0
    index: int = 0


@dataclass
class PersonInput:
    """One person detection from YOLO26-pose + nvtracker."""

    bbox: BBox
    track_id: int = 0
    has_track_id: bool = False
    confidence: float = 0.0
    index: int = 0


@dataclass
class FacePersonAssociation:
    """Result of associating one face to one person."""

    face_index: int
    person_index: int
    person_track_id: int
    score: float
    method: str
    person_bbox: BBox


@dataclass
class AssociationConfig:
    """Tunable parameters for face-person association."""

    center_inside_required: bool = True
    upper_body_weight: float = 0.3
    containment_weight: float = 0.3
    size_ratio_weight: float = 0.2
    base_score: float = 0.2
    min_score: float = 0.0
    max_face_to_person_area_ratio: float = 0.8
    require_track_id: bool = True


def _point_in_upper_body(face_cx: float, face_cy: float, person: BBox) -> float:
    """Return 0..1 score for how 'upper body' the face center is.

    1.0 = at person top edge, 0.0 = at person bottom edge.
    """
    if person.height <= 0:
        return 0.0
    relative = (face_cy - person.top) / person.height
    return max(0.0, 1.0 - relative)


def _containment_score(face: BBox, person: BBox) -> float:
    """Return 0..1 score for how much of the face bbox is inside the person bbox.

    1.0 = fully contained, 0.0 = no overlap.
    """
    if face.area <= 0:
        return 0.0
    ix = max(0, min(face.right, person.right) - max(face.left, person.left))
    iy = max(0, min(face.bottom, person.bottom) - max(face.top, person.top))
    intersection = ix * iy
    return intersection / face.area


def _size_ratio_score(face: BBox, person: BBox) -> float:
    """Return 0..1 score for how reasonable the face/person size ratio is.

    Ideal: face area is 2%–30% of person area.
    Too small (< 0.5%) or too large (> 80%) gets low score.
    """
    if person.area <= 0 or face.area <= 0:
        return 0.0
    ratio = face.area / person.area
    if ratio < 0.005 or ratio > 0.8:
        return 0.0
    if ratio < 0.02:
        return (ratio - 0.005) / 0.015
    if ratio > 0.3:
        return max(0.0, (0.8 - ratio) / 0.5)
    return 1.0


def associate_faces_to_persons(
    faces: List[FaceInput],
    persons: List[PersonInput],
    config: Optional[AssociationConfig] = None,
) -> List[FacePersonAssociation]:
    """Associate faces to persons with one-to-one greedy matching.

    Scores every eligible face-person pair, then sorts by score descending
    and greedily assigns — each face assigned at most once, each person
    assigned at most once.  Unassigned faces remain unassociated.

    Algorithm:
        1. Compute score for every (face, person) pair that passes the
           center-inside and track_id checks.
        2. Sort candidates by score descending.
        3. Greedily assign: skip if face or person already taken.
        4. Return the assigned associations.
    """
    if config is None:
        config = AssociationConfig()

    if not faces or not persons:
        return []

    # Step 1: score all eligible pairs
    candidates: list = []  # (score, face, person, method)

    for face in faces:
        face_cx = face.bbox.xc
        face_cy = face.bbox.yc

        for person in persons:
            if config.require_track_id and not person.has_track_id:
                continue

            if config.center_inside_required:
                if not person.bbox.contains_point(face_cx, face_cy):
                    continue

            upper = _point_in_upper_body(face_cx, face_cy, person.bbox)
            contain = _containment_score(face.bbox, person.bbox)
            size = _size_ratio_score(face.bbox, person.bbox)

            score = (
                config.base_score
                + config.upper_body_weight * upper
                + config.containment_weight * contain
                + config.size_ratio_weight * size
            )
            score = min(score, 1.0)

            if score > config.min_score:
                method = (
                    "center_inside_upper_body" if upper > 0.5
                    else "center_inside"
                )
                candidates.append((score, face, person, method))

    # Step 2: sort by score descending
    candidates.sort(key=lambda c: c[0], reverse=True)

    # Step 3: greedy assignment
    assigned_faces: set = set()
    assigned_persons: set = set()
    results: List[FacePersonAssociation] = []

    for score, face, person, method in candidates:
        if face.index in assigned_faces:
            continue
        if person.index in assigned_persons:
            continue

        assigned_faces.add(face.index)
        assigned_persons.add(person.index)

        results.append(
            FacePersonAssociation(
                face_index=face.index,
                person_index=person.index,
                person_track_id=person.track_id,
                score=round(score, 4),
                method=method,
                person_bbox=person.bbox,
            )
        )

    return results
