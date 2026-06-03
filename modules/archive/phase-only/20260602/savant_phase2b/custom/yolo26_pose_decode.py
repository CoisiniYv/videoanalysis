"""Pure YOLO26-pose ONNX output decoder.

No Savant dependency — only numpy.  Can be unit-tested standalone.

Supports two ONNX output layouts:

**raw_56** (default)::
    shape  = [batch, 56, num_predictions]  or  [batch, num_predictions, 56]
    layout = [cx, cy, w, h, box_conf, kpt0_x, kpt0_y, kpt0_conf, …] × 17
    Needs NMS.

**post_nms_57**::
    shape  = [batch, num_detections, 57]
    layout = [x1, y1, x2, y2, confidence, class_id, kpt0_x, kpt0_y, kpt0_conf, …] × 17
    Already post-NMS.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Layout identifiers
# ---------------------------------------------------------------------------
DECODER_LAYOUT_RAW_56 = "raw_56"
DECODER_LAYOUT_POST_NMS_57 = "post_nms_57"


@dataclass
class PoseDetection:
    """A single detected person with pose keypoints."""

    bbox: Tuple[float, float, float, float]  # (cx, cy, w, h) *normalized* for raw_56;
    # (x1, y1, x2, y2) *pixel* for post_nms_57
    confidence: float
    keypoints: np.ndarray  # shape (17, 3)  — (x, y, confidence) each
    label: int = 0


@dataclass
class DecoderConfig:
    """Configuration for the YOLO-pose decoder.

    Set ``layout`` to match the ONNX export type.
    """

    layout: str = DECODER_LAYOUT_RAW_56

    # --- raw_56 fields ------------------------------------------------------
    num_channels: int = 56
    bbox_start: int = 0
    bbox_len: int = 4
    conf_start: int = 4
    kpt_start: int = 5
    num_keypoints: int = 17
    kpt_step: int = 3

    # --- thresholds ---------------------------------------------------------
    confidence_threshold: float = 0.25
    nms_threshold: float = 0.7
    nms_top_k: int = 300

    # --- post_nms_57 only: re-run NMS even though model already did it ------
    force_nms: bool = False

    input_width: int = 640
    input_height: int = 640

    @property
    def kpt_total(self) -> int:
        return self.num_keypoints * self.kpt_step


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def decode_pose_output(
    raw: np.ndarray,
    config: DecoderConfig | None = None,
) -> Tuple[List[PoseDetection], str]:
    """Decode raw ONNX output tensor into a list of PoseDetection.

    Args:
        raw: ONNX output tensor.
        config: Decoder parameters.

    Returns:
        Tuple of (detections list, layout string).
        Detections are for the first (only) batch item, sorted by confidence
        descending.
    """
    if config is None:
        config = DecoderConfig()

    raw = _ensure_3d(raw, config.layout)

    if config.layout == DECODER_LAYOUT_POST_NMS_57:
        dets = _decode_post_nms_57(raw, config)
    else:
        dets = _decode_raw_56(raw, config)

    return dets, config.layout


# ---------------------------------------------------------------------------
# Shape normalisation  —  handle 2-D / transposed inputs
# ---------------------------------------------------------------------------


def _ensure_3d(raw: np.ndarray, layout: str) -> np.ndarray:
    """Ensure the tensor is 3-D ``(B, ..., ...)`` regardless of input shape.

    Handles:
      - ``[B, N, C]`` or ``[B, C, N]`` — keep as-is
      - ``[N, C]``                    — add batch dim → ``[1, N, C]``
      - ``[C, N]`` with C small (57)  — transpose then add batch dim

    For ``post_nms_57`` layout, the expected 3-D form is ``(B, N, 57)``.
    For ``raw_56``, the expected 3-D form is ``(B, C, N)`` or ``(B, N, C)``.
    """
    if raw.ndim == 3:
        return raw

    if raw.ndim != 2:
        raise ValueError(
            f"Expected 2-D or 3-D tensor, got shape {raw.shape}"
        )

    # 2-D tensor: determine whether it's (N, C) or (C, N)
    H, W = raw.shape
    if layout == DECODER_LAYOUT_POST_NMS_57:
        if W == 57:
            return raw[None, :, :]
        if H == 57:
            return raw.T[None, :, :]
    else:
        if H == 56:
            return raw[None, :, :]
        if W == 56:
            return raw.T[None, :, :]

    if min(H, W) <= 57:
        if H < W:
            return raw.T[None, :, :]
        return raw[None, :, :]

    raise ValueError(
        f"Cannot determine layout for 2-D tensor shape {raw.shape} "
        f"with layout '{layout}'"
    )


# ---------------------------------------------------------------------------
# raw_56  —  [B, C, N] or [B, N, C]  layout
# ---------------------------------------------------------------------------


def _decode_raw_56(raw: np.ndarray, cfg: DecoderConfig) -> List[PoseDetection]:
    """Decode raw_56 layout (standard YOLO-pose before NMS)."""
    if raw.shape[1] < raw.shape[2] and raw.shape[2] == cfg.num_channels:
        raw = raw.transpose(0, 2, 1)

    results: List[PoseDetection] = []
    for b in range(raw.shape[0]):
        dets = _decode_raw_56_one(raw[b], cfg)
        results.extend(dets)

    return results


def _decode_raw_56_one(tensor: np.ndarray, cfg: DecoderConfig) -> List[PoseDetection]:
    C, N = tensor.shape
    if C != cfg.num_channels:
        raise ValueError(
            f"raw_56: expected {cfg.num_channels} channels, got {C}. "
            "Adjust DecoderConfig.num_channels."
        )

    box_conf = _sigmoid(tensor[cfg.conf_start, :])

    mask = box_conf >= cfg.confidence_threshold
    if not mask.any():
        return []

    box_conf = box_conf[mask]
    tensor = tensor[:, mask]
    M = tensor.shape[1]

    cx = tensor[cfg.bbox_start + 0, :]
    cy = tensor[cfg.bbox_start + 1, :]
    w = tensor[cfg.bbox_start + 2, :]
    h = tensor[cfg.bbox_start + 3, :]

    kpt_raw = tensor[cfg.kpt_start : cfg.kpt_start + cfg.kpt_total, :]

    candidates: List[PoseDetection] = []
    for i in range(M):
        kpts = kpt_raw[:, i].reshape(cfg.num_keypoints, cfg.kpt_step).copy()
        kpts[:, 2] = _sigmoid(kpts[:, 2])
        candidates.append(
            PoseDetection(
                bbox=(float(cx[i]), float(cy[i]), float(w[i]), float(h[i])),
                confidence=float(box_conf[i]),
                keypoints=kpts,
            )
        )

    keep = _nms(candidates, cfg.nms_threshold, cfg.nms_top_k)
    return [candidates[i] for i in keep]


# ---------------------------------------------------------------------------
# post_nms_57  —  [B, N, 57]  layout
# ---------------------------------------------------------------------------


def _decode_post_nms_57(raw: np.ndarray, cfg: DecoderConfig) -> List[PoseDetection]:
    results: List[PoseDetection] = []
    for b in range(raw.shape[0]):
        dets = _decode_post_nms_57_one(raw[b], cfg)
        results.extend(dets)

    return results


def _decode_post_nms_57_one(tensor: np.ndarray, cfg: DecoderConfig) -> List[PoseDetection]:
    N, C = tensor.shape
    if C != 57:
        raise ValueError(f"post_nms_57: expected 57 channels, got {C}.")

    confs = tensor[:, 4]
    mask = confs >= cfg.confidence_threshold
    if not mask.any():
        return []

    tensor = tensor[mask]
    confs = confs[mask]

    class_ids = tensor[:, 5]
    person_mask = class_ids == 0
    if not person_mask.any():
        return []

    tensor = tensor[person_mask]
    confs = confs[person_mask]
    M = tensor.shape[0]

    x1 = tensor[:, 0]
    y1 = tensor[:, 1]
    x2 = tensor[:, 2]
    y2 = tensor[:, 3]

    kpts_flat = tensor[:, 6:57]
    kpts_all = kpts_flat.reshape(M, cfg.num_keypoints, cfg.kpt_step)

    candidates: List[PoseDetection] = []
    for i in range(M):
        kpts = kpts_all[i].copy()
        kpts[:, 2] = _sigmoid(kpts[:, 2])

        candidates.append(
            PoseDetection(
                bbox=(float(x1[i]), float(y1[i]), float(x2[i]), float(y2[i])),
                confidence=float(confs[i]),
                keypoints=kpts,
                label=0,
            )
        )

    if cfg.force_nms:
        keep = _nms(candidates, cfg.nms_threshold, cfg.nms_top_k)
        return [candidates[i] for i in keep]

    return candidates


# ---------------------------------------------------------------------------
# NMS
# ---------------------------------------------------------------------------


def _nms(
    dets: List[PoseDetection],
    iou_threshold: float,
    top_k: int,
) -> List[int]:
    if not dets:
        return []

    boxes = np.array([d.bbox for d in dets], dtype=np.float32)
    scores = np.array([d.confidence for d in dets], dtype=np.float32)

    if boxes[:, 2].max() <= 2.0 and boxes[:, 3].max() <= 2.0:
        x1 = boxes[:, 0] - boxes[:, 2] / 2
        y1 = boxes[:, 1] - boxes[:, 3] / 2
        x2 = boxes[:, 0] + boxes[:, 2] / 2
        y2 = boxes[:, 1] + boxes[:, 3] / 2
    else:
        x1 = boxes[:, 0]
        y1 = boxes[:, 1]
        x2 = boxes[:, 2]
        y2 = boxes[:, 3]

    order = scores.argsort()[::-1][:top_k]
    keep = []

    while order.size > 0:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break

        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        area_i = (x2[i] - x1[i]) * (y2[i] - y1[i])
        area_j = (x2[order[1:]] - x1[order[1:]]) * (y2[order[1:]] - y1[order[1:]])
        union = area_i + area_j - inter + 1e-7

        iou = inter / union
        mask = iou <= iou_threshold
        order = order[1:][mask]

    return keep
