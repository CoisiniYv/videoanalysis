"""Savant complex-model converter for YOLO26-pose ONNX.

Output format ``[B, N, 57]`` (post_nms_57)::

    0:4   = bbox (x1, y1, x2, y2)
    4     = confidence
    5     = class_id
    6:57  = 17 keypoints × 3 (x, y, confidence)
"""

from __future__ import annotations

import logging
from typing import Any, List, Tuple

import numpy as np

from savant.base.converter import BaseComplexModelOutputConverter

from custom.yolo26_pose_decode import DecoderConfig, decode_pose_output

logger = logging.getLogger(__name__)

_DETAILED_LOG_LIMIT = 5
_SUMMARY_INTERVAL = 300


class Yolo26PoseConverter(BaseComplexModelOutputConverter):
    """Convert YOLO26-pose ``[B, N, 57]`` output to bbox tensor + keypoint attrs."""

    def __init__(
        self,
        decoder_layout: str = "post_nms_57",
        confidence_threshold: float = 0.35,
        keypoint_threshold: float = 0.25,
        force_nms: bool = False,
        nms_threshold: float = 0.6,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._decoder_config = DecoderConfig(
            layout=decoder_layout,
            confidence_threshold=confidence_threshold,
            force_nms=force_nms,
            nms_threshold=nms_threshold,
        )
        self._keypoint_threshold = keypoint_threshold
        self._call_count = 0
        self._last_returned_detections = 0
        self._last_bbox_tensor_shape: Any = None
        self._last_attrs_len = 0
        self._last_first_keypoints_len = 0
        print(
            f"stage=phase2c_converter_init "
            f"decoder_layout={decoder_layout} "
            f"confidence_threshold={confidence_threshold} "
            f"keypoint_threshold={keypoint_threshold} "
            f"force_nms={force_nms}",
            f"nms_threshold={nms_threshold}",
            flush=True,
        )

    def __call__(
        self,
        *output_layers: np.ndarray,
        model: Any,
        roi: Tuple[float, float, float, float],
    ) -> Tuple[np.ndarray, List[List[Tuple[str, Any, None]]]]:
        try:
            return self._call_impl(*output_layers, model=model, roi=roi)
        except Exception as exc:
            print(
                f"stage=phase2c_converter_error error={exc}",
                flush=True,
            )
            raise

    def _call_impl(
        self,
        *output_layers: np.ndarray,
        model: Any,
        roi: Tuple[float, float, float, float],
    ) -> Tuple[np.ndarray, List[List[Tuple[str, Any, None]]]]:
        self._call_count += 1
        is_detailed = self._call_count <= _DETAILED_LOG_LIMIT
        is_summary = not is_detailed and (self._call_count % _SUMMARY_INTERVAL == 0)

        if is_detailed:
            print(
                f"stage=phase2c_converter_entered "
                f"call_count={self._call_count} "
                f"output_layers_count={len(output_layers)}",
                flush=True,
            )

        tensor = np.asarray(output_layers[0]) if output_layers else np.array([])

        if is_detailed:
            print(
                f"stage=phase2c_converter_tensor "
                f"raw_shape={list(tensor.shape)} "
                f"raw_dtype={tensor.dtype} "
                f"raw_min={tensor.min() if tensor.size > 0 else 'N/A'} "
                f"raw_max={tensor.max() if tensor.size > 0 else 'N/A'} "
                f"roi={list(roi) if roi else 'N/A'}",
                flush=True,
            )

        if tensor.size == 0:
            if is_detailed:
                print("stage=phase2c_converter_return bbox_tensor_empty=true reason=empty_tensor", flush=True)
            return np.empty((0, 6), dtype=np.float32), []

        detections, _layout = decode_pose_output(tensor, self._decoder_config)
        self._last_returned_detections = len(detections)

        if is_detailed:
            print(
                f"stage=phase2c_converter_decode returned_detections={len(detections)}",
                flush=True,
            )

        if not detections:
            if is_detailed:
                print("stage=phase2c_converter_return bbox_tensor_empty=true reason=no_detections_after_decode", flush=True)
            return np.empty((0, 6), dtype=np.float32), []

        input_shape = list(model.input.shape)
        if len(input_shape) == 4:
            _, _, model_h, model_w = input_shape
        else:
            model_h, model_w = input_shape[-2:]

        if is_detailed:
            print(
                f"stage=phase2c_converter_model "
                f"input_shape={input_shape} model_h={model_h} model_w={model_w}",
                flush=True,
            )

        roi_top, roi_left, roi_w, roi_h = roi
        scale_x = roi_w / model_w if model_w > 0 else 1.0
        scale_y = roi_h / model_h if model_h > 0 else 1.0

        if is_detailed:
            print(
                f"stage=phase2c_converter_roi "
                f"roi={list(roi)} scale_x={scale_x:.4f} scale_y={scale_y:.4f}",
                flush=True,
            )

        bbox_rows = []
        attrs_rows: List[List[Tuple[str, Any, None]]] = []

        for i, det in enumerate(detections):
            x1, y1, x2, y2 = det.bbox

            w_m = x2 - x1
            h_m = y2 - y1
            if w_m < 0:
                x1, x2 = x2, x1
                w_m = -w_m
            if h_m < 0:
                y1, y2 = y2, y1
                h_m = -h_m

            xc_m = (x1 + x2) / 2.0
            yc_m = (y1 + y2) / 2.0

            xc_frame = xc_m * scale_x + roi_left
            yc_frame = yc_m * scale_y + roi_top
            w_frame = w_m * scale_x
            h_frame = h_m * scale_y

            w_frame = max(w_frame, 1.0)
            h_frame = max(h_frame, 1.0)

            bbox_rows.append(
                [
                    float(det.label),
                    float(det.confidence),
                    float(xc_frame),
                    float(yc_frame),
                    float(w_frame),
                    float(h_frame),
                ]
            )

            kpts = det.keypoints.copy()
            kpts[:, 0] = kpts[:, 0] * scale_x + roi_left
            kpts[:, 1] = kpts[:, 1] * scale_y + roi_top
            kpts_flat = kpts.astype(float).reshape(-1).tolist()
            attrs_rows.append([("keypoints", kpts_flat, 1.0)])

        bbox_tensor = np.array(bbox_rows, dtype=np.float32).reshape(-1, 6)

        if is_detailed and bbox_tensor.shape[0] > 0:
            det0 = detections[0]
            bbox0 = bbox_tensor[0]
            kpts0_list = attrs_rows[0][0][1] if attrs_rows else []
            print(
                f"phase=phase2c_converter_bbox_debug "
                f"raw_first_detection_xyxy=({det0.bbox[0]:.2f},{det0.bbox[1]:.2f},{det0.bbox[2]:.2f},{det0.bbox[3]:.2f}) "
                f"converted_first_bbox_cxcywh=({bbox0[2]:.2f},{bbox0[3]:.2f},{bbox0[4]:.2f},{bbox0[5]:.2f})",
                flush=True,
            )

        self._last_bbox_tensor_shape = list(bbox_tensor.shape)
        self._last_attrs_len = len(attrs_rows)

        if bbox_tensor.shape[0] > 0:
            if is_detailed:
                print(
                    f"stage=phase2c_converter_return "
                    f"bbox_tensor_shape={list(bbox_tensor.shape)} "
                    f"attrs_len={len(attrs_rows)}",
                    flush=True,
                )

        return bbox_tensor, attrs_rows
