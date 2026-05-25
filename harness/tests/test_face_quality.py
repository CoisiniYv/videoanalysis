"""Tests for face quality filter — pure Python, no GPU, no image loading."""

import sys
from pathlib import Path

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import pytest

from custom.models.faces import FaceDetection, FaceQualityResult
from custom.services.face_quality import evaluate_face_quality


_DEFAULT_FACE_BBOX = [115.0, 215.0, 50.0, 50.0]


def _detection(confidence=0.85, face_bbox=_DEFAULT_FACE_BBOX, landmarks=None):
    return FaceDetection(
        source_id="src1",
        camera_id="cam1",
        track_id=1,
        timestamp_ms=1000,
        person_bbox=[100.0, 200.0, 80.0, 180.0],
        face_bbox=face_bbox,
        landmarks=landmarks,
        confidence=confidence,
        model_name="scrfd_2.5g",
    )


class TestHighQualityPass:
    def test_confident_large_landmarks_passes(self):
        result = evaluate_face_quality(
            _detection(
                confidence=0.9,
                face_bbox=[100, 200, 50, 50],
                landmarks=[[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]],
            )
        )
        assert result.passed
        assert result.quality >= 0.65
        assert result.landmark_score == 1.0

    def test_fewer_landmarks_still_scores(self):
        result = evaluate_face_quality(
            _detection(
                confidence=0.9,
                face_bbox=[100, 200, 50, 50],
                landmarks=[[1, 2], [3, 4]],
            )
        )
        assert result.landmark_score == 0.5


class TestLowConfidenceFails:
    def test_low_confidence_fails(self):
        result = evaluate_face_quality(_detection(confidence=0.3))
        assert not result.passed
        assert "low_confidence" in result.reasons

    def test_confidence_threshold_config(self):
        result = evaluate_face_quality(
            _detection(confidence=0.3),
            config={"face_confidence_threshold": 0.2, "face_quality_threshold": 0.3},
        )
        assert result.passed


class TestSmallFaceFails:
    def test_narrow_face_fails(self):
        result = evaluate_face_quality(
            _detection(
                confidence=0.9,
                face_bbox=[100, 200, 10, 50],
                landmarks=[[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]],
            )
        )
        assert not result.passed
        assert "face_too_narrow" in result.reasons

    def test_short_face_fails(self):
        result = evaluate_face_quality(
            _detection(
                confidence=0.9,
                face_bbox=[100, 200, 50, 10],
                landmarks=[[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]],
            )
        )
        assert not result.passed
        assert "face_too_short" in result.reasons


class TestQualityRange:
    def test_quality_in_0_to_1(self):
        result = evaluate_face_quality(_detection(confidence=0.85))
        assert 0.0 <= result.quality <= 1.0

    def test_minimum_quality_is_zero(self):
        result = evaluate_face_quality(
            _detection(confidence=0.0, face_bbox=[10, 10, 5, 5], landmarks=None)
        )
        assert result.quality >= 0.0

    def test_maximum_quality_capped(self):
        result = evaluate_face_quality(
            _detection(
                confidence=1.0,
                face_bbox=[100, 200, 500, 500],
                landmarks=[[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]],
            )
        )
        assert result.quality <= 1.0


class TestMissingFaceBbox:
    def test_missing_face_bbox_fails(self):
        result = evaluate_face_quality(_detection(face_bbox=None, confidence=0.9))
        assert not result.passed
        assert "missing_face_bbox" in result.reasons


class TestConfigOverride:
    def test_config_overrides_threshold(self):
        result = evaluate_face_quality(
            _detection(confidence=0.7, face_bbox=[100, 200, 50, 50], landmarks=[]),
            config={"face_confidence_threshold": 0.8, "face_quality_threshold": 0.9},
        )
        assert not result.passed

    def test_config_relaxes_threshold(self):
        result = evaluate_face_quality(
            _detection(
                confidence=0.5,
                face_bbox=[100, 200, 50, 50],
                landmarks=[[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]],
            ),
            config={"face_confidence_threshold": 0.4, "face_quality_threshold": 0.5},
        )
        assert result.passed


class TestNoImageBytes:
    def test_result_has_no_image_data(self):
        result = evaluate_face_quality(_detection())
        assert isinstance(result, FaceQualityResult)
        assert not hasattr(result, "image")
        assert not hasattr(result, "frame_bytes")
        assert not hasattr(result, "crop_bytes")
