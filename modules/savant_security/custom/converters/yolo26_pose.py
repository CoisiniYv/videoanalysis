"""Savant complex-model converter for YOLO26-pose ONNX.

Output format ``[B, N, 57]`` (post_nms_57)::

    0:4   = bbox (x1, y1, x2, y2)
    4     = confidence
    5     = class_id
    6:57  = 17 keypoints × 3 (x, y, confidence)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

from savant.base.converter import BaseComplexModelOutputConverter

from custom.yolo26_pose_decode import DecoderConfig, decode_pose_output

logger = logging.getLogger(__name__)

_DETAILED_LOG_LIMIT = 5
_SUMMARY_INTERVAL = 300
_RESTORE_STRETCH = "stretch"
_RESTORE_LETTERBOX = "letterbox"
_BUILDER_VERSION = "c1m5_letterbox_restore_v1"
_RUNTIME_DEBUG_MARKER = "C1M5R_RUNTIME_FRESHNESS_MARKER"


class Yolo26PoseConverter(BaseComplexModelOutputConverter):
    """Convert YOLO26-pose ``[B, N, 57]`` output to bbox tensor + keypoint attrs."""

    def __init__(
        self,
        decoder_layout: str = "post_nms_57",
        confidence_threshold: float = 0.35,
        keypoint_threshold: float = 0.25,
        force_nms: bool = False,
        nms_threshold: float = 0.6,
        coordinate_restore_mode: str = _RESTORE_LETTERBOX,
        debug_dump_enabled: bool = False,
        debug_dump_dir: str = "/data/video-analytics/artifacts/c1m5/pose_converter_debug",
        debug_dump_max_records: int = 200,
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
        self._coordinate_restore_mode = normalize_coordinate_restore_mode(coordinate_restore_mode)
        self._debug_dump_enabled = bool(debug_dump_enabled)
        self._debug_dump_dir = Path(str(debug_dump_dir))
        self._debug_dump_max_records = max(0, int(debug_dump_max_records))
        self._debug_dump_records = 0
        self._call_count = 0
        self._last_returned_detections = 0
        self._last_bbox_tensor_shape: Any = None
        self._last_attrs_len = 0
        self._last_first_keypoints_len = 0
        print(
            f"stage=phase2c_converter_init "
            f"builder_version={_BUILDER_VERSION} "
            f"runtime_debug_marker={_RUNTIME_DEBUG_MARKER} "
            f"decoder_layout={decoder_layout} "
            f"confidence_threshold={confidence_threshold} "
            f"keypoint_threshold={keypoint_threshold} "
            f"force_nms={force_nms}",
            f"nms_threshold={nms_threshold}",
            f"coordinate_restore_mode={self._coordinate_restore_mode}",
            f"letterbox_restore_enabled={self._coordinate_restore_mode == _RESTORE_LETTERBOX}",
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

        transform = compute_pose_coordinate_transform(
            roi=roi,
            model_w=float(model_w),
            model_h=float(model_h),
            mode=self._coordinate_restore_mode,
        )
        raw_output_rows = candidate_debug_rows(tensor, self._decoder_config)

        if is_detailed:
            print(
                f"stage=phase2c_converter_roi "
                f"roi={list(roi)} "
                f"restore_mode={transform['mode']} "
                f"scale_x={transform['stretch_scale_x']:.4f} "
                f"scale_y={transform['stretch_scale_y']:.4f} "
                f"letterbox_scale={transform['letterbox_scale']:.4f} "
                f"pad_x={transform['letterbox_pad_x']:.2f} "
                f"pad_y={transform['letterbox_pad_y']:.2f}",
                flush=True,
            )

        bbox_rows = []
        attrs_rows: List[List[Tuple[str, Any, None]]] = []

        for i, det in enumerate(detections):
            restored_bbox = restore_model_xyxy_to_frame(det.bbox, transform, clamp=True)
            x1_frame, y1_frame, x2_frame, y2_frame = restored_bbox
            w_frame = x2_frame - x1_frame
            h_frame = y2_frame - y1_frame
            xc_frame = (x1_frame + x2_frame) / 2.0
            yc_frame = (y1_frame + y2_frame) / 2.0

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
            kpts[:, 0:2] = restore_model_points_to_frame(kpts[:, 0:2], transform)
            kpts_flat = kpts.astype(float).reshape(-1).tolist()
            attrs_rows.append([("keypoints", kpts_flat, 1.0)])

            if self._debug_dump_enabled:
                raw_row = match_raw_output_row(det, raw_output_rows)
                self._write_debug_dump(
                    detection_index=i,
                    raw_output_row=raw_row,
                    decoded_bbox_before_restore=tuple(float(v) for v in det.bbox),
                    decoded_keypoints_before_restore=det.keypoints,
                    restored_bbox_xyxy=restored_bbox,
                    restored_keypoints=kpts,
                    model_w=float(model_w),
                    model_h=float(model_h),
                    roi=roi,
                    transform=transform,
                    confidence=float(det.confidence),
                    label=int(det.label),
                )

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

    def _write_debug_dump(
        self,
        *,
        detection_index: int,
        raw_output_row: dict[str, Any] | None,
        decoded_bbox_before_restore: tuple[float, float, float, float],
        decoded_keypoints_before_restore: np.ndarray,
        restored_bbox_xyxy: tuple[float, float, float, float],
        restored_keypoints: np.ndarray,
        model_w: float,
        model_h: float,
        roi: Tuple[float, float, float, float],
        transform: dict[str, float | str],
        confidence: float,
        label: int,
    ) -> None:
        if self._debug_dump_records >= self._debug_dump_max_records:
            return
        try:
            self._debug_dump_dir.mkdir(parents=True, exist_ok=True)
            path = self._debug_dump_dir / "yolo26_pose_converter_debug.jsonl"
            payload = {
                "builder": "Yolo26PoseConverter",
                "builder_version": _BUILDER_VERSION,
                "runtime_debug_marker": _RUNTIME_DEBUG_MARKER,
                "call_count": self._call_count,
                "detection_index": detection_index,
                "raw_output_row": raw_output_row,
                "decoded_bbox_before_restore": list(decoded_bbox_before_restore),
                "decoded_keypoints_before_restore": decoded_keypoints_before_restore.astype(float).reshape(-1).tolist(),
                "restored_bbox_xyxy": list(restored_bbox_xyxy),
                "restored_keypoints": restored_keypoints.astype(float).reshape(-1).tolist(),
                "model_input_size": {"width": model_w, "height": model_h},
                "original_frame_size": {"width": transform["roi_w"], "height": transform["roi_h"]},
                "roi": list(roi),
                "current_scale_x": transform["stretch_scale_x"],
                "current_scale_y": transform["stretch_scale_y"],
                "expected_letterbox_scale": transform["letterbox_scale"],
                "expected_pad_x": transform["letterbox_pad_x"],
                "expected_pad_y": transform["letterbox_pad_y"],
                "coordinate_restore_mode": transform["mode"],
                "letterbox_restore_enabled": transform["mode"] == _RESTORE_LETTERBOX,
                "bbox_format_assumption": "xyxy_in_model_input",
                "keypoint_coordinate_assumption": "xyc_in_model_input",
                "confidence": confidence,
                "label": label,
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
            self._debug_dump_records += 1
        except Exception as exc:
            if self._debug_dump_records == 0:
                print(f"stage=phase2c_converter_debug_dump_failed error={exc}", flush=True)
            self._debug_dump_records = self._debug_dump_max_records


def normalize_coordinate_restore_mode(value: Any) -> str:
    mode = str(value or _RESTORE_LETTERBOX).strip().lower()
    if mode in {"legacy", "scale", "scale_xy", _RESTORE_STRETCH}:
        return _RESTORE_STRETCH
    if mode in {"auto", "pad", "unpad", "letterboxed", _RESTORE_LETTERBOX}:
        return _RESTORE_LETTERBOX
    return _RESTORE_LETTERBOX


def compute_pose_coordinate_transform(
    *,
    roi: Tuple[float, float, float, float],
    model_w: float,
    model_h: float,
    mode: str = _RESTORE_LETTERBOX,
) -> dict[str, float | str]:
    roi_top, roi_left, roi_w, roi_h = [float(v) for v in roi]
    stretch_scale_x = roi_w / model_w if model_w > 0 else 1.0
    stretch_scale_y = roi_h / model_h if model_h > 0 else 1.0
    if roi_w > 0 and roi_h > 0 and model_w > 0 and model_h > 0:
        letterbox_scale = min(model_w / roi_w, model_h / roi_h)
        resized_w = roi_w * letterbox_scale
        resized_h = roi_h * letterbox_scale
        pad_x = max(0.0, (model_w - resized_w) / 2.0)
        pad_y = max(0.0, (model_h - resized_h) / 2.0)
    else:
        letterbox_scale = 1.0
        pad_x = 0.0
        pad_y = 0.0
    return {
        "mode": normalize_coordinate_restore_mode(mode),
        "roi_top": roi_top,
        "roi_left": roi_left,
        "roi_w": roi_w,
        "roi_h": roi_h,
        "model_w": float(model_w),
        "model_h": float(model_h),
        "stretch_scale_x": stretch_scale_x,
        "stretch_scale_y": stretch_scale_y,
        "letterbox_scale": letterbox_scale,
        "letterbox_pad_x": pad_x,
        "letterbox_pad_y": pad_y,
    }


def restore_model_xyxy_to_frame(
    bbox: Tuple[float, float, float, float],
    transform: dict[str, float | str],
    *,
    clamp: bool = False,
) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = [float(v) for v in bbox]
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    points = np.array([[x1, y1], [x2, y2]], dtype=np.float32)
    restored = restore_model_points_to_frame(points, transform)
    rx1, ry1 = restored[0]
    rx2, ry2 = restored[1]
    if clamp:
        left = float(transform["roi_left"])
        top = float(transform["roi_top"])
        right = left + float(transform["roi_w"])
        bottom = top + float(transform["roi_h"])
        rx1 = min(max(float(rx1), left), right)
        rx2 = min(max(float(rx2), left), right)
        ry1 = min(max(float(ry1), top), bottom)
        ry2 = min(max(float(ry2), top), bottom)
    return (float(rx1), float(ry1), float(rx2), float(ry2))


def restore_model_points_to_frame(points: np.ndarray, transform: dict[str, float | str]) -> np.ndarray:
    restored = np.asarray(points, dtype=np.float32).copy()
    if str(transform["mode"]) == _RESTORE_LETTERBOX:
        scale = float(transform["letterbox_scale"]) or 1.0
        restored[:, 0] = (restored[:, 0] - float(transform["letterbox_pad_x"])) / scale + float(transform["roi_left"])
        restored[:, 1] = (restored[:, 1] - float(transform["letterbox_pad_y"])) / scale + float(transform["roi_top"])
    else:
        restored[:, 0] = restored[:, 0] * float(transform["stretch_scale_x"]) + float(transform["roi_left"])
        restored[:, 1] = restored[:, 1] * float(transform["stretch_scale_y"]) + float(transform["roi_top"])
    return restored


def candidate_debug_rows(tensor: np.ndarray, config: DecoderConfig) -> list[dict[str, Any]]:
    raw = np.asarray(tensor)
    if raw.size == 0:
        return []
    try:
        if raw.ndim == 3:
            raw = raw[0]
        if config.layout == "post_nms_57":
            if raw.ndim != 2:
                return []
            if raw.shape[0] == 57 and raw.shape[1] != 57:
                raw = raw.T
            rows = []
            for index, row in enumerate(raw):
                if len(row) < 57:
                    continue
                confidence = float(row[4])
                class_id = int(row[5])
                if confidence < config.confidence_threshold or class_id != 0:
                    continue
                rows.append(
                    {
                        "raw_index": index,
                        "bbox_xyxy": [float(v) for v in row[0:4]],
                        "confidence": confidence,
                        "class_id": class_id,
                        "keypoints": [float(v) for v in row[6:57]],
                    }
                )
            return rows
    except Exception:
        return []
    return []


def match_raw_output_row(det: Any, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    target = np.array(det.bbox, dtype=np.float32)
    best: tuple[float, dict[str, Any]] | None = None
    for row in rows:
        bbox = np.array(row.get("bbox_xyxy") or [], dtype=np.float32)
        if bbox.shape != (4,):
            continue
        score = float(np.mean(np.abs(target - bbox))) + abs(float(det.confidence) - float(row.get("confidence") or 0.0))
        if best is None or score < best[0]:
            best = (score, row)
    return best[1] if best else None
