"""Tests for SCRFD converter utility functions — pure Python, no model loading."""

import sys
from pathlib import Path

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np
import pytest

from custom.converters.scrfd import (
    ScrfdConverter,
    filter_detections_by_confidence,
    map_roi_coords_to_frame,
    nms_boxes,
    decode_scrfd_outputs,
)


class TestNMS:
    def test_nms_removes_overlapping_box(self):
        bboxes = np.array(
            [[0, 0, 100, 100], [10, 10, 90, 90]], dtype=float
        )
        scores = np.array([0.9, 0.8], dtype=float)
        keep = nms_boxes(bboxes, scores, iou_threshold=0.5)
        assert len(keep) == 1
        assert keep[0] == 0

    def test_nms_keeps_non_overlapping(self):
        bboxes = np.array(
            [[0, 0, 50, 50], [100, 100, 150, 150]], dtype=float
        )
        scores = np.array([0.9, 0.8], dtype=float)
        keep = nms_boxes(bboxes, scores, iou_threshold=0.5)
        assert len(keep) == 2

    def test_nms_empty_input(self):
        keep = nms_boxes(np.empty((0, 4)), np.empty(0))
        assert len(keep) == 0

    def test_nms_single_box(self):
        bboxes = np.array([[0, 0, 100, 100]], dtype=float)
        scores = np.array([0.9], dtype=float)
        keep = nms_boxes(bboxes, scores)
        assert len(keep) == 1


class TestConfidenceFilter:
    def test_filter_removes_low_confidence(self):
        bboxes = np.array([[0, 0, 100, 100], [10, 10, 50, 50]], dtype=float)
        scores = np.array([0.9, 0.3], dtype=float)
        fb, fs, fl = filter_detections_by_confidence(
            bboxes, scores, None, threshold=0.5
        )
        assert len(fb) == 1
        assert fs[0] == 0.9

    def test_filter_all_pass(self):
        bboxes = np.array([[0, 0, 100, 100]], dtype=float)
        scores = np.array([0.9], dtype=float)
        fb, fs, fl = filter_detections_by_confidence(
            bboxes, scores, None, threshold=0.5
        )
        assert len(fb) == 1

    def test_filter_all_removed(self):
        bboxes = np.array([[0, 0, 100, 100]], dtype=float)
        scores = np.array([0.2], dtype=float)
        fb, fs, fl = filter_detections_by_confidence(
            bboxes, scores, None, threshold=0.5
        )
        assert len(fb) == 0

    def test_filter_with_landmarks(self):
        bboxes = np.array([[0, 0, 100, 100], [10, 10, 50, 50]], dtype=float)
        scores = np.array([0.9, 0.3], dtype=float)
        landmarks = np.array(
            [[1, 2, 3, 4, 5, 6, 7, 8, 9, 10], [11, 12, 13, 14, 15, 16, 17, 18, 19, 20]],
            dtype=float,
        )
        fb, fs, fl = filter_detections_by_confidence(
            bboxes, scores, landmarks, threshold=0.5
        )
        assert len(fb) == 1
        assert fl is not None
        assert len(fl) == 1

    def test_empty_input_returns_empty(self):
        fb, fs, fl = filter_detections_by_confidence(
            np.empty((0, 4)), np.empty(0), None, threshold=0.5
        )
        assert len(fb) == 0


class TestMapROICoords:
    def test_map_bbox_from_roi_to_frame(self):
        det_bboxes = np.array([[320, 240, 400, 300]], dtype=float)
        frame_bboxes, frame_landmarks = map_roi_coords_to_frame(
            roi_x=100,
            roi_y=200,
            roi_w=200,
            roi_h=150,
            det_bboxes=det_bboxes,
            model_w=640,
            model_h=480,
        )
        assert frame_bboxes.shape == (1, 4)
        assert frame_bboxes[0, 0] == pytest.approx(320 * (200 / 640) + 100)
        assert frame_bboxes[0, 1] == pytest.approx(240 * (150 / 480) + 200)

    def test_map_landmarks_from_roi_to_frame(self):
        det_bboxes = np.array([[100, 100, 200, 200]], dtype=float)
        det_landmarks = np.array([[120, 130, 140, 150, 160, 170, 180, 190, 200, 210]], dtype=float)
        frame_bboxes, frame_landmarks = map_roi_coords_to_frame(
            roi_x=50,
            roi_y=80,
            roi_w=320,
            roi_h=320,
            det_bboxes=det_bboxes,
            det_landmarks=det_landmarks,
            model_w=640,
            model_h=640,
        )
        assert frame_landmarks is not None
        assert frame_landmarks.shape == (1, 10)
        assert frame_landmarks[0, 0] == pytest.approx(120 * 0.5 + 50)

    def test_map_roi_no_landmarks(self):
        det_bboxes = np.array([[100, 100, 200, 200]], dtype=float)
        frame_bboxes, frame_landmarks = map_roi_coords_to_frame(
            roi_x=0, roi_y=0, roi_w=640, roi_h=640,
            det_bboxes=det_bboxes,
            det_landmarks=None,
            model_w=640, model_h=640,
        )
        assert frame_landmarks is None


class TestDecodeNotImplemented:
    def test_decode_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            decode_scrfd_outputs(np.ones((1, 100, 1)))


class TestConverterSkeleton:
    def test_converter_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            ScrfdConverter()

    def test_utilities_importable_pure_python(self):
        """All utility functions must be importable without Savant runtime."""
        from custom.converters.scrfd import (
            filter_detections_by_confidence,
            map_roi_coords_to_frame,
            nms_boxes,
        )
        assert callable(nms_boxes)
        assert callable(filter_detections_by_confidence)
        assert callable(map_roi_coords_to_frame)
