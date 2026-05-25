"""Tests for head ROI selector — pure Python, no GPU, no model loading."""

import sys
from pathlib import Path

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import pytest

from custom.models.pose import BBox, Keypoint
from custom.models.faces import FaceROI
from custom.services.face_roi import estimate_head_roi_from_person


def _person_bbox(x=100.0, y=200.0, w=80.0, h=200.0):
    return BBox(x=x, y=y, width=w, height=h)


def _face_keypoints(conf=0.9):
    return [
        Keypoint(x=140.0, y=210.0, confidence=conf, name="nose"),
        Keypoint(x=130.0, y=205.0, confidence=conf, name="left_eye"),
        Keypoint(x=150.0, y=205.0, confidence=conf, name="right_eye"),
        Keypoint(x=120.0, y=210.0, confidence=conf, name="left_ear"),
        Keypoint(x=160.0, y=210.0, confidence=conf, name="right_ear"),
    ]


class TestBboxFallback:
    def test_fallback_produces_roi(self):
        roi = estimate_head_roi_from_person(_person_bbox())
        assert roi is not None
        assert roi.method == "bbox_fallback"
        assert roi.width > 0
        assert roi.height > 0

    def test_fallback_roi_has_positive_dims(self):
        roi = estimate_head_roi_from_person(_person_bbox())
        assert roi is not None
        assert roi.width > 0
        assert roi.height > 0

    def test_fallback_passthrough_fields(self):
        bbox = _person_bbox()
        roi = estimate_head_roi_from_person(
            bbox, source_id="src1", camera_id="cam1", track_id=42, timestamp_ms=1000
        )
        assert roi is not None
        assert roi.source_id == "src1"
        assert roi.camera_id == "cam1"
        assert roi.track_id == 42
        assert roi.timestamp_ms == 1000


class TestPersonTooSmall:
    def test_person_too_small_returns_none(self):
        bbox = BBox(x=0, y=0, width=10, height=50)
        roi = estimate_head_roi_from_person(bbox)
        assert roi is None

    def test_config_override_min_height(self):
        bbox = BBox(x=0, y=0, width=10, height=60)
        roi = estimate_head_roi_from_person(bbox, config={"min_person_height": 80})
        assert roi is None

    def test_config_lowers_min_height(self):
        bbox = BBox(x=0, y=0, width=10, height=60)
        roi = estimate_head_roi_from_person(bbox, config={"min_person_height": 50})
        assert roi is not None


class TestKeypointROI:
    def test_keypoints_used_when_confident(self):
        kps = _face_keypoints(conf=0.9)
        roi = estimate_head_roi_from_person(_person_bbox(), keypoints=kps)
        assert roi is not None
        assert roi.method == "keypoints"
        assert roi.confidence > 0.0

    def test_low_confidence_keypoints_fallback(self):
        kps = _face_keypoints(conf=0.1)
        roi = estimate_head_roi_from_person(_person_bbox(), keypoints=kps)
        assert roi is not None
        assert roi.method == "bbox_fallback"

    def test_empty_keypoints_fallback(self):
        roi = estimate_head_roi_from_person(_person_bbox(), keypoints=[])
        assert roi is not None
        assert roi.method == "bbox_fallback"


class TestROIClamp:
    def test_roi_clamped_to_frame(self):
        bbox = BBox(x=0, y=0, width=200, height=400)
        roi = estimate_head_roi_from_person(bbox, frame_width=320, frame_height=240)
        assert roi is not None
        assert roi.x >= 0
        assert roi.y >= 0
        assert roi.x + roi.width <= 320
        assert roi.y + roi.height <= 240

    def test_negative_coords_clamped(self):
        bbox = BBox(x=-50, y=-50, width=200, height=400)
        roi = estimate_head_roi_from_person(bbox, frame_width=320, frame_height=240)
        assert roi is not None
        assert roi.x >= 0
        assert roi.y >= 0

    def test_roi_does_not_exceed_frame(self):
        bbox = BBox(x=300, y=200, width=200, height=400)
        roi = estimate_head_roi_from_person(bbox, frame_width=320, frame_height=240)
        assert roi is not None
        assert roi.x + roi.width <= 320 + 1e-6
        assert roi.y + roi.height <= 240 + 1e-6


class TestNoImageBytes:
    def test_roi_has_no_image_data(self):
        roi = estimate_head_roi_from_person(_person_bbox())
        assert roi is not None
        assert not hasattr(roi, "image")
        assert not hasattr(roi, "frame_bytes")
        assert not hasattr(roi, "crop_bytes")
