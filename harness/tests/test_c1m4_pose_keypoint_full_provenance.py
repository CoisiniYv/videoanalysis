"""C1M.4 pose keypoint full-provenance export tests."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from libs.evidence_metadata.validation import validate_frame_annotation_message


ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = str(ROOT / "modules" / "savant_security")
MEDIA_WORKER_DIR = str(ROOT / "services" / "media-worker")


class _Attr:
    def __init__(self, value: Any) -> None:
        self.value = value


class _BBox:
    xc = 120.0
    yc = 220.0
    width = 80.0
    height = 180.0


class _Object:
    label = "person"
    element_name = "yolo26_pose"
    bbox = _BBox()
    confidence = 0.9
    track_id = 4
    object_id = 4

    def __init__(self, *, keypoints: list[float] | None = None) -> None:
        self.attrs = {("yolo26_pose", "keypoints"): keypoints or _flat_keypoints()}

    def get_attr_meta(self, namespace: str, name: str) -> _Attr | None:
        key = (namespace, name)
        if key not in self.attrs:
            return None
        return _Attr(self.attrs[key])


def test_frame_annotation_builder_full_exports_named_coco17_keypoints() -> None:
    _activate_module()
    from custom.services.frame_annotation_builder import (
        FrameAnnotationBuildConfig,
        build_frame_annotation_message,
    )

    message = build_frame_annotation_message(
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        frame_pts=123,
        frame_uuid="frame-1",
        frame_objects=[_Object()],
        config=FrameAnnotationBuildConfig(include_keypoints="full"),
    )

    pose = message["objects"][0]["pose"]
    points = pose["keypoints"]
    assert pose["keypoints_format"] == "coco17"
    assert pose["keypoint_coordinate_space"] == "pixel"
    assert pose["keypoint_count"] == 17
    assert points[0] == {"index": 0, "name": "nose", "x": 10.0, "y": 20.0, "confidence": 0.9}
    assert points[-1]["name"] == "right_ankle"
    validate_frame_annotation_message(message)


def test_frame_annotation_builder_debug_matches_full_mode() -> None:
    _activate_module()
    from custom.services.frame_annotation_builder import (
        FrameAnnotationBuildConfig,
        build_frame_annotation_message,
    )

    message = build_frame_annotation_message(
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        frame_pts=123,
        frame_uuid="frame-1",
        frame_objects=[_Object()],
        config=FrameAnnotationBuildConfig(include_keypoints="debug"),
    )

    assert len(message["objects"][0]["pose"]["keypoints"]) == 17
    validate_frame_annotation_message(message)


def test_compact_mode_keeps_summary_not_raw_coordinates() -> None:
    _activate_module()
    from custom.services.frame_annotation_builder import (
        FrameAnnotationBuildConfig,
        build_frame_annotation_message,
    )

    message = build_frame_annotation_message(
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
        frame_pts=123,
        frame_uuid="frame-1",
        frame_objects=[_Object()],
        config=FrameAnnotationBuildConfig(include_keypoints="compact"),
    )

    keypoints = message["objects"][0]["pose"]["keypoints"]
    assert keypoints == {"format": "coco17_xyc", "coordinate_space": "pixel", "point_count": 17}
    validate_frame_annotation_message(message)


def test_validator_rejects_invalid_full_keypoint_count() -> None:
    payload = _frame_annotation_payload()
    payload["objects"][0]["pose"]["keypoints"] = payload["objects"][0]["pose"]["keypoints"][:16]

    with pytest.raises(ValueError, match="invalid_pose_keypoints"):
        validate_frame_annotation_message(payload)


def test_sidecar_shadow_builder_propagates_pose_without_mutating_input() -> None:
    _activate_media_worker()
    from app.frame_annotation_shadow_builder import build_shadow_annotations_from_frame_cache

    message = _frame_annotation_payload()
    original = copy.deepcopy(message)
    rows, summary = build_shadow_annotations_from_frame_cache(
        [{"clip_frame_index": 0, "t_ms": 0, "frame_uuid": "frame-1", "frame_pts": 123}],
        [message],
        source_id="c1e_rtsp_replay",
        camera_id="cam_c1e_rtsp_replay",
    )

    assert summary["annotations_written"] == 1
    assert rows[0]["pose"]["keypoints"][0]["name"] == "nose"
    assert rows[0]["sidecar_row_fingerprint"]
    assert message == original


def _frame_annotation_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "producer": "savant-security",
        "message_type": "frame_annotation",
        "source_id": "c1e_rtsp_replay",
        "camera_id": "cam_c1e_rtsp_replay",
        "frame_pts": 123,
        "frame_uuid": "frame-1",
        "frame_num": 0,
        "timestamp_ms": 1000,
        "ttl_seconds": 120,
        "created_at": "2026-06-06T00:00:00Z",
        "objects": [
            {
                "object_id": "person:track:4",
                "object_type": "person",
                "track_id": "4",
                "original_object_index": 0,
                "bbox": {
                    "format": "xyxy",
                    "coordinate_space": "pixel",
                    "xyxy": [80.0, 130.0, 160.0, 310.0],
                    "confidence": 0.9,
                },
                "quality": {"person_confidence": 0.9, "quality_status": "ok"},
                "pose": _pose_full(),
                "source_object_fingerprint": "abc",
            }
        ],
    }


def _pose_full() -> dict[str, Any]:
    return {
        "keypoints_format": "coco17",
        "keypoint_coordinate_space": "pixel",
        "keypoint_count": 17,
        "visible_keypoint_count": 17,
        "mean_keypoint_confidence": 0.9,
        "keypoints": [
            {
                "index": index,
                "name": name,
                "x": float(10 + index),
                "y": float(20 + index),
                "confidence": 0.9,
            }
            for index, name in enumerate((
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
            ))
        ],
    }


def _flat_keypoints() -> list[float]:
    values = []
    for index in range(17):
        values.extend([float(10 + index), float(20 + index), 0.9])
    return values


def _activate_module() -> None:
    for name in list(sys.modules):
        if name == "custom" or name.startswith("custom."):
            del sys.modules[name]
    if MODULE_DIR in sys.path:
        sys.path.remove(MODULE_DIR)
    sys.path.insert(0, MODULE_DIR)


def _activate_media_worker() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if MEDIA_WORKER_DIR in sys.path:
        sys.path.remove(MEDIA_WORKER_DIR)
    sys.path.insert(0, MEDIA_WORKER_DIR)
