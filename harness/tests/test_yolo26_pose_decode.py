"""YOLO26-pose decoder contracts."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import numpy as np


MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
MODULES_ROOT = str(Path(__file__).resolve().parents[2] / "modules")


def _load_decoder():
    sys.path[:] = [
        p for p in sys.path
        if not (p.startswith(MODULES_ROOT) and p != MODULE_DIR)
    ]
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    for name in [m for m in list(sys.modules) if m == "custom" or m.startswith("custom.")]:
        sys.modules.pop(name, None)
    return importlib.import_module("custom.yolo26_pose_decode")


def _decoded_row(
    *,
    bbox: tuple[float, float, float, float],
    confidence: float,
    keypoint_confidence: float = 0.2,
) -> np.ndarray:
    row = np.zeros((56,), dtype=np.float32)
    row[0:4] = np.asarray(bbox, dtype=np.float32)
    row[4] = np.float32(confidence)
    keypoints = np.zeros((17, 3), dtype=np.float32)
    keypoints[:, 0] = np.linspace(10.0, 170.0, 17, dtype=np.float32)
    keypoints[:, 1] = np.linspace(20.0, 180.0, 17, dtype=np.float32)
    keypoints[:, 2] = np.float32(keypoint_confidence)
    row[5:56] = keypoints.reshape(-1)
    return row


def test_decoded_56_keeps_batch_slots_independent_for_nms() -> None:
    decoder = _load_decoder()
    tensor = np.zeros((4, 2, 56), dtype=np.float32)
    same_box = (20.0, 30.0, 120.0, 230.0)
    tensor[0, 0] = _decoded_row(bbox=same_box, confidence=0.80)
    tensor[1, 0] = _decoded_row(bbox=same_box, confidence=0.70)
    tensor[2, 0] = _decoded_row(bbox=(200.0, 30.0, 260.0, 180.0), confidence=0.10)
    tensor[3, 0] = _decoded_row(bbox=(300.0, 40.0, 360.0, 200.0), confidence=0.90)
    tensor[3, 1] = _decoded_row(bbox=(302.0, 42.0, 362.0, 202.0), confidence=0.85)

    detections, layout = decoder.decode_pose_output(
        tensor,
        decoder.DecoderConfig(
            layout=decoder.DECODER_LAYOUT_DECODED_56,
            confidence_threshold=0.25,
            nms_threshold=0.5,
        ),
    )

    assert layout == "decoded_56"
    assert [round(det.confidence, 2) for det in detections] == [0.80, 0.70, 0.90]


def test_decoded_56_does_not_sigmoid_decoded_confidences_again() -> None:
    decoder = _load_decoder()
    tensor = _decoded_row(
        bbox=(20.0, 30.0, 120.0, 230.0),
        confidence=0.80,
        keypoint_confidence=0.20,
    )[None, None, :]

    detections, _layout = decoder.decode_pose_output(
        tensor,
        decoder.DecoderConfig(
            layout=decoder.DECODER_LAYOUT_DECODED_56,
            confidence_threshold=0.25,
        ),
    )

    assert len(detections) == 1
    assert np.isclose(detections[0].confidence, np.float32(0.80))
    assert np.isclose(detections[0].keypoints[0, 2], np.float32(0.20))


def test_decoded_56_accepts_channel_first_tensor_shape() -> None:
    decoder = _load_decoder()
    tensor = np.zeros((2, 56, 1), dtype=np.float32)
    tensor[0, :, 0] = _decoded_row(bbox=(20.0, 30.0, 120.0, 230.0), confidence=0.80)
    tensor[1, :, 0] = _decoded_row(bbox=(200.0, 30.0, 260.0, 180.0), confidence=0.70)

    detections, _layout = decoder.decode_pose_output(
        tensor,
        decoder.DecoderConfig(layout=decoder.DECODER_LAYOUT_DECODED_56),
    )

    assert len(detections) == 2
