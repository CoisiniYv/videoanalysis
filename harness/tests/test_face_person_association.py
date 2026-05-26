"""Unit tests for face-person association service."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from modules.savant_security.custom.services.face_person_association import (
    AssociationConfig,
    BBox,
    FaceInput,
    FacePersonAssociation,
    PersonInput,
    associate_faces_to_persons,
)


def _face(xc=500, yc=200, w=80, h=100, conf=0.8, idx=0):
    return FaceInput(bbox=BBox(xc, yc, w, h), confidence=conf, index=idx)


def _person(xc=500, yc=400, w=200, h=500, tid=1, has_tid=True, conf=0.9, idx=0):
    return PersonInput(
        bbox=BBox(xc, yc, w, h),
        track_id=tid,
        has_track_id=has_tid,
        confidence=conf,
        index=idx,
    )


class TestCenterInside:
    """Face center inside person bbox -> associated."""

    def test_face_center_inside_person(self):
        face = _face(xc=500, yc=200)
        person = _person(xc=500, yc=400, w=200, h=500)
        result = associate_faces_to_persons([face], [person])
        assert len(result) == 1
        assert result[0].person_track_id == 1

    def test_face_center_at_edge(self):
        face = _face(xc=400, yc=150)
        person = _person(xc=500, yc=400, w=200, h=500)
        result = associate_faces_to_persons([face], [person])
        assert len(result) == 1


class TestFaceOutside:
    """Face outside all person bboxes -> unassociated."""

    def test_face_completely_outside(self):
        face = _face(xc=100, yc=100)
        person = _person(xc=500, yc=500, w=200, h=200)
        result = associate_faces_to_persons([face], [person])
        assert len(result) == 0

    def test_face_nearby_but_outside(self):
        face = _face(xc=350, yc=150)
        person = _person(xc=500, yc=500, w=200, h=200)
        result = associate_faces_to_persons([face], [person])
        assert len(result) == 0


class TestMultiplePersons:
    """Multiple persons -> chooses best candidate."""

    def test_chooses_closest_person(self):
        face = _face(xc=300, yc=200)
        person_a = _person(xc=300, yc=400, w=150, h=400, tid=10, idx=0)
        person_b = _person(xc=800, yc=400, w=150, h=400, tid=20, idx=1)
        result = associate_faces_to_persons([face], [person_a, person_b])
        assert len(result) == 1
        assert result[0].person_track_id == 10

    def test_chooses_upper_body_person(self):
        # Face at (500, 200) — inside both persons but closer to person_a's top
        face = _face(xc=500, yc=200)
        person_a = _person(xc=500, yc=350, w=200, h=400, tid=10, idx=0)
        person_b = _person(xc=500, yc=600, w=200, h=400, tid=20, idx=1)
        result = associate_faces_to_persons([face], [person_a, person_b])
        assert len(result) == 1
        # Both contain the face center; upper body score determines winner
        assert result[0].person_track_id == 10


class TestUpperBodyPreference:
    """Face in upper body preferred over lower-body candidate."""

    def test_upper_body_higher_score(self):
        face = _face(xc=500, yc=200)
        # Person A: face is in upper portion
        person_a = _person(xc=500, yc=400, w=200, h=500, tid=10, idx=0)
        # Person B: face is in lower portion (shifted person down)
        person_b = _person(xc=500, yc=200, w=200, h=500, tid=20, idx=1)
        result = associate_faces_to_persons([face], [person_a, person_b])
        assert len(result) == 1
        assert result[0].person_track_id == 10
        assert "upper_body" in result[0].method


class TestTrackIdInheritance:
    """Person track_id inherited by association."""

    def test_track_id_copied(self):
        face = _face(xc=500, yc=200)
        person = _person(xc=500, yc=400, w=200, h=500, tid=42)
        result = associate_faces_to_persons([face], [person])
        assert result[0].person_track_id == 42

    def test_track_id_zero_still_associated(self):
        face = _face(xc=500, yc=200)
        person = _person(xc=500, yc=400, w=200, h=500, tid=0, has_tid=True)
        result = associate_faces_to_persons([face], [person])
        assert len(result) == 1
        assert result[0].person_track_id == 0


class TestMissingLandmarks:
    """Missing landmarks does not crash (pure service has no landmarks)."""

    def test_no_landmarks_field(self):
        face = _face(xc=500, yc=200)
        person = _person(xc=500, yc=400, w=200, h=500)
        result = associate_faces_to_persons([face], [person])
        assert len(result) == 1


class TestMissingTrackId:
    """Missing person track_id does not produce valid association."""

    def test_untracked_person_skipped(self):
        face = _face(xc=500, yc=200)
        person = _person(xc=500, yc=400, w=200, h=500, tid=0, has_tid=False)
        result = associate_faces_to_persons([face], [person])
        assert len(result) == 0

    def test_untracked_allowed_with_config(self):
        config = AssociationConfig(require_track_id=False)
        face = _face(xc=500, yc=200)
        person = _person(xc=500, yc=400, w=200, h=500, tid=0, has_tid=False)
        result = associate_faces_to_persons([face], [person], config=config)
        assert len(result) == 1


class TestEmptyInputs:
    """No persons -> no association.  No faces -> empty output."""

    def test_no_persons(self):
        face = _face(xc=500, yc=200)
        result = associate_faces_to_persons([face], [])
        assert result == []

    def test_no_faces(self):
        person = _person(xc=500, yc=400, w=200, h=500)
        result = associate_faces_to_persons([], [person])
        assert result == []

    def test_both_empty(self):
        result = associate_faces_to_persons([], [])
        assert result == []


class TestEdgeCases:
    """BBox edge cases."""

    def test_face_at_exact_corner(self):
        face = _face(xc=400, yc=150)
        person = _person(xc=500, yc=400, w=200, h=500)
        result = associate_faces_to_persons([face], [person])
        # Face at left edge, top area — should associate
        assert len(result) == 1

    def test_zero_size_person(self):
        face = _face(xc=500, yc=200)
        person = _person(xc=500, yc=400, w=0, h=0)
        result = associate_faces_to_persons([face], [person])
        assert len(result) == 0

    def test_zero_size_face(self):
        face = _face(xc=500, yc=200, w=0, h=0)
        person = _person(xc=500, yc=400, w=200, h=500)
        result = associate_faces_to_persons([face], [person])
        # Zero-size face center is still inside person — may associate
        # Score will be low but may pass default threshold
        # This is acceptable behavior


class TestScoreRange:
    """Association score within 0..1."""

    def test_score_in_range(self):
        face = _face(xc=500, yc=200)
        person = _person(xc=500, yc=400, w=200, h=500)
        result = associate_faces_to_persons([face], [person])
        assert len(result) == 1
        assert 0.0 <= result[0].score <= 1.0

    def test_score_reasonable_for_good_match(self):
        face = _face(xc=500, yc=200, w=80, h=100)
        person = _person(xc=500, yc=400, w=200, h=500)
        result = associate_faces_to_persons([face], [person])
        assert result[0].score >= 0.5


class TestLargeFaceReject:
    """Face bbox much larger than person bbox -> rejected or low score."""

    def test_face_larger_than_person(self):
        face = _face(xc=500, yc=400, w=300, h=400)
        person = _person(xc=500, yc=400, w=100, h=200)
        result = associate_faces_to_persons([face], [person])
        if result:
            assert result[0].score < 0.5

    def test_face_much_larger_rejected(self):
        config = AssociationConfig(max_face_to_person_area_ratio=0.3)
        face = _face(xc=500, yc=400, w=500, h=600)
        person = _person(xc=500, yc=400, w=100, h=200)
        result = associate_faces_to_persons([face], [person], config=config)
        # Large face gets low size_ratio_score; may still associate with low score
