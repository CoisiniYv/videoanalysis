"""SCRFD_2.5G converter skeleton + testable post-processing utilities.

**F0 scope**: Skeleton only.  The actual converter cannot be completed
until the SCRFD_2.5G ONNX model output tensor shapes are known.

F1 will:
  1. Load ``scrfd_2.5g.onnx`` and inspect output tensor shapes.
  2. Complete ``ScrfdConverter`` by subclassing ``BaseComplexModelOutputConverter``.
  3. Wire it into ``module.yml`` as a secondary model on person ROIs.
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np


class ScrfdConverter:
    """SCRFD_2.5G output converter — **skeleton placeholder**.

    TODO(F1):
        - Subclass ``savant.base.converter.BaseComplexModelOutputConverter``.
        - Implement ``__call__(*output_layers, model, roi)``.
        - Map SCRFD ONNX outputs to ``FaceDetection`` objects.
        - Determine actual tensor shapes from the ONNX model file.

    Expected SCRFD_2.5G outputs (typical, NOT confirmed):
        - ``scores``: [1, N] or [1, N, 1] — per-anchor confidence.
        - ``bboxes``: [1, N, 4] — xyxy or cxcywh in model coordinates.
        - ``landmarks``: [1, N, 10] — 5-point facial landmarks (x,y pairs).

    These shapes MUST be verified against the actual ONNX model before
    completing the converter.  Do NOT hard-code unverified shapes into
    production code.
    """

    def __init__(self):
        raise NotImplementedError(
            "ScrfdConverter is a skeleton placeholder. "
            "Complete the implementation in F1 after SCRFD ONNX shapes are confirmed."
        )


def filter_detections_by_confidence(
    bboxes: np.ndarray,
    scores: np.ndarray,
    landmarks: np.ndarray | None,
    threshold: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """Filter detections whose confidence is below ``threshold``.

    Args:
        bboxes: [N, 4] array of bounding boxes.
        scores: [N] or [N, 1] array of confidence scores.
        landmarks: [N, K] array of landmarks, or ``None``.
        threshold: Minimum confidence.

    Returns:
        Tuple of ``(filtered_bboxes, filtered_scores, filtered_landmarks)``.
        Arrays may be empty.
    """
    scores_flat = np.asarray(scores).reshape(-1)
    mask = scores_flat >= threshold
    filtered_bboxes = bboxes[mask]
    filtered_scores = scores_flat[mask]
    if landmarks is not None:
        filtered_landmarks = landmarks[mask]
    else:
        filtered_landmarks = None
    return filtered_bboxes, filtered_scores, filtered_landmarks


def nms_boxes(
    bboxes: np.ndarray,
    scores: np.ndarray,
    iou_threshold: float = 0.45,
) -> np.ndarray:
    """Simple NMS — suppress overlapping boxes by IoU.

    Args:
        bboxes: [N, 4] in xyxy format.
        scores: [N] confidence scores.
        iou_threshold: IoU threshold for suppression.

    Returns:
        Indices of kept boxes (sorted by score descending).
    """
    if bboxes.shape[0] == 0:
        return np.array([], dtype=np.intp)

    x1 = bboxes[:, 0]
    y1 = bboxes[:, 1]
    x2 = bboxes[:, 2]
    y2 = bboxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = np.argsort(scores)[::-1]

    keep: List[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)

        if order.size == 1:
            break

        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-8)

        remaining = np.where(iou <= iou_threshold)[0]
        order = order[remaining + 1]

    return np.array(keep, dtype=np.intp)


def map_roi_coords_to_frame(
    roi_x: float,
    roi_y: float,
    roi_w: float,
    roi_h: float,
    det_bboxes: np.ndarray,
    det_landmarks: np.ndarray | None = None,
    model_w: int = 640,
    model_h: int = 640,
) -> Tuple[np.ndarray, np.ndarray | None]:
    """Map detection coordinates from model (ROI-crop) space to full-frame space.

    Args:
        roi_x, roi_y, roi_w, roi_h: ROI rectangle in full-frame pixels.
        det_bboxes: [N, 4] bboxes in model coordinates (xyxy or cxcywh).
        det_landmarks: [N, K] landmarks in model coordinates, or ``None``.
        model_w, model_h: Model input dimensions.

    Returns:
        Tuple of ``(frame_bboxes, frame_landmarks)``.
    """
    scale_x = roi_w / model_w if model_w > 0 else 1.0
    scale_y = roi_h / model_h if model_h > 0 else 1.0

    frame_bboxes = det_bboxes.copy().astype(float)
    frame_bboxes[:, 0] = frame_bboxes[:, 0] * scale_x + roi_x
    frame_bboxes[:, 1] = frame_bboxes[:, 1] * scale_y + roi_y
    if frame_bboxes.shape[1] >= 4:
        frame_bboxes[:, 2] = frame_bboxes[:, 2] * scale_x + roi_x
        frame_bboxes[:, 3] = frame_bboxes[:, 3] * scale_y + roi_y

    frame_landmarks = None
    if det_landmarks is not None:
        frame_landmarks = det_landmarks.copy().astype(float)
        frame_landmarks[:, 0::2] = frame_landmarks[:, 0::2] * scale_x + roi_x
        frame_landmarks[:, 1::2] = frame_landmarks[:, 1::2] * scale_y + roi_y

    return frame_bboxes, frame_landmarks


def decode_scrfd_outputs(
    *output_layers: np.ndarray,
    model_w: int | None = None,
    model_h: int | None = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    """Decode raw SCRFD ONNX outputs into bboxes, scores, landmarks.

    **Not implemented in F0.**  The actual tensor shapes depend on the
    SCRFD_2.5G ONNX model export and cannot be guessed.

    F1 must:
      1. Inspect ``scrfd_2.5g.onnx`` input/output shapes.
      2. Implement stride/anchor decoding if the model outputs
         feature-map-level predictions.
      3. Or simply reshape + threshold if the model already outputs
         decoded (N, K) tensors.

    Raises:
        NotImplementedError: Always in F0.
    """
    raise NotImplementedError(
        "decode_scrfd_outputs requires confirmed SCRFD ONNX output tensor shapes. "
        "This will be implemented in F1 after the model file is available."
    )
