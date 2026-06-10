"""OfflineFaceEmbedder — real YOLOv8-Face + AdaFace inference for external images.

Runs face detection and embedding extraction entirely offline using ONNXRuntime.
No Savant, no GPU pipeline, no Redis, no video.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from .onnx_runtime_utils import get_session_providers, load_session

logger = logging.getLogger(__name__)

# YOLOv8-Face defaults
_DEFAULT_YOLOV8_FACE_ONNX = os.getenv(
    "YOLOV8_FACE_ONNX", "/data/video-analytics/models/yolov8_face.onnx"
)
_DEFAULT_ADAFACE_ONNX = os.getenv(
    "ADAFACE_ONNX", "/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx"
)
_DEFAULT_PROVIDER_SPEC = "CUDAExecutionProvider,CPUExecutionProvider"

# YOLOv8-Face input dims
_YOLOV8_INPUT_SIZE = 640
_YOLOV8_CONF_THRESHOLD = 0.5
_YOLOV8_NMS_IOU_THRESHOLD = 0.5
_YOLOV8_MIN_FACE_SIZE = 20

# AdaFace alignment target landmarks (112x112, standard ArcFace/AdaFace)
_ADAFACE_TARGET_LANDMARKS = np.array(
    [
        [30.2946, 51.6963],  # left eye
        [65.5318, 51.5014],  # right eye
        [48.0252, 71.7366],  # nose
        [33.5493, 92.3655],  # left mouth
        [62.7299, 92.2041],  # right mouth
    ],
    dtype=np.float32,
)


@dataclass(frozen=True)
class FaceImageEmbeddingResult:
    """Result of offline face detection + embedding extraction."""

    face_bbox: list[float]  # [x, y, w, h] in original image coords
    landmarks: list[list[float]]  # 5 × [x, y] in original image coords
    face_confidence: float
    quality: float
    embedding: list[float]  # 512-d L2-normalized
    embedding_dim: int
    embedding_norm: float
    embedding_model: str = "adaface"
    detector_model: str = "yolov8_face"
    model_version: str | None = None
    real_embedding_used: bool = True


def _letterbox(
    img: np.ndarray,
    target_size: int,
) -> tuple[np.ndarray, float, int, int]:
    """Resize image with letterbox padding to target_size × target_size.

    Returns (padded_image, scale, pad_x, pad_y).
    """
    h, w = img.shape[:2]
    scale = min(target_size / w, target_size / h)
    new_w = int(w * scale)
    new_h = int(h * scale)
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    padded = np.full(
        (target_size, target_size, 3), 114, dtype=np.uint8
    )
    pad_x = (target_size - new_w) // 2
    pad_y = (target_size - new_h) // 2
    padded[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized
    return padded, scale, pad_x, pad_y


def _preprocess_yolov8(img_bgr: np.ndarray) -> tuple[np.ndarray, float, int, int]:
    """Preprocess image for YOLOv8-Face: letterbox + RGB + /255 + NCHW."""
    padded, scale, pad_x, pad_y = _letterbox(img_bgr, _YOLOV8_INPUT_SIZE)
    rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
    blob = rgb.astype(np.float32) / 255.0
    blob = np.transpose(blob, (2, 0, 1))  # HWC -> CHW
    blob = np.expand_dims(blob, 0)  # add batch dim
    return blob, scale, pad_x, pad_y


def _decode_yolov8_output(
    output: np.ndarray,
    conf_threshold: float,
    scale: float,
    pad_x: int,
    pad_y: int,
    orig_w: int,
    orig_h: int,
) -> list[dict[str, Any]]:
    """Decode YOLOv8-Face output tensor into face detections.

    output shape: [1, 20, 8400]
    Layout per detection:
      [0:4] = cx, cy, w, h
      [4]   = confidence
      [5:20] = 5 landmarks × (x, y, score)

    Returns list of dicts with keys:
      bbox_xywh, confidence, landmarks (5 × [x, y])
    """
    # output shape: [1, 20, N]
    data = output[0]  # [20, N]
    num_dets = data.shape[1]

    # Extract components
    cx = data[0, :]
    cy = data[1, :]
    w = data[2, :]
    h = data[3, :]
    conf = data[4, :]

    # Confidence filter
    mask = conf >= conf_threshold
    cx = cx[mask]
    cy = cy[mask]
    w = w[mask]
    h = h[mask]
    conf = conf[mask]

    if len(conf) == 0:
        return []

    # Landmarks: [5:20] = 5 × (x, y, score)
    lm_data = data[5:20, mask]  # [15, N_filtered]
    lm_x = lm_data[0::3, :]  # indices 0,3,6,9,12
    lm_y = lm_data[1::3, :]  # indices 1,4,7,10,13

    # Convert from letterbox coords to original image coords
    # letterbox coords -> remove padding -> divide by scale
    cx_orig = (cx - pad_x) / scale
    cy_orig = (cy - pad_y) / scale
    w_orig = w / scale
    h_orig = h / scale

    lm_x_orig = (lm_x - pad_x) / scale  # [5, N_filtered]
    lm_y_orig = (lm_y - pad_y) / scale  # [5, N_filtered]

    detections = []
    for i in range(len(conf)):
        landmarks = []
        for j in range(5):
            landmarks.append([float(lm_x_orig[j, i]), float(lm_y_orig[j, i])])
        detections.append(
            {
                "bbox_xywh": [
                    float(cx_orig[i]),
                    float(cy_orig[i]),
                    float(w_orig[i]),
                    float(h_orig[i]),
                ],
                "confidence": float(conf[i]),
                "landmarks": landmarks,
            }
        )
    return detections


def _iou_xywh(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Compute IoU between box a [N,4] and boxes b [M,4] in xywh format.

    Returns IoU matrix [N, M].
    """
    a_x1 = a[:, 0:1] - a[:, 2:3] / 2
    a_y1 = a[:, 1:2] - a[:, 3:4] / 2
    a_x2 = a[:, 0:1] + a[:, 2:3] / 2
    a_y2 = a[:, 1:2] + a[:, 3:4] / 2

    b_x1 = b[:, 0:1] - b[:, 2:3] / 2
    b_y1 = b[:, 1:2] - b[:, 3:4] / 2
    b_x2 = b[:, 0:1] + b[:, 2:3] / 2
    b_y2 = b[:, 1:2] + b[:, 3:4] / 2

    inter_x1 = np.maximum(a_x1, b_x1.T)
    inter_y1 = np.maximum(a_y1, b_y1.T)
    inter_x2 = np.minimum(a_x2, b_x2.T)
    inter_y2 = np.minimum(a_y2, b_y2.T)

    inter_w = np.maximum(inter_x2 - inter_x1, 0)
    inter_h = np.maximum(inter_y2 - inter_y1, 0)
    inter_area = inter_w * inter_h

    a_area = a[:, 2:3] * a[:, 3:4]
    b_area = b[:, 2:3] * b[:, 3:4]
    union_area = a_area + b_area.T - inter_area

    return inter_area / np.maximum(union_area, 1e-6)


def _nms(
    detections: list[dict[str, Any]],
    iou_threshold: float,
) -> list[dict[str, Any]]:
    """Apply Non-Maximum Suppression to detections."""
    if not detections:
        return []

    boxes = np.array([d["bbox_xywh"] for d in detections], dtype=np.float32)
    scores = np.array([d["confidence"] for d in detections], dtype=np.float32)

    order = scores.argsort()[::-1]
    keep = []

    while len(order) > 0:
        i = order[0]
        keep.append(i)
        if len(order) == 1:
            break
        remaining = order[1:]
        ious = _iou_xywh(boxes[i : i + 1], boxes[remaining])[0]
        mask = ious <= iou_threshold
        order = remaining[mask]

    return [detections[i] for i in keep]


def _similarity_transform(
    src_points: np.ndarray,
    dst_points: np.ndarray,
) -> np.ndarray:
    """Estimate similarity transform (2×3 matrix) from src to dst points."""
    num = src_points.shape[0]
    dim = 2

    src_mean = src_points.mean(axis=0)
    dst_mean = dst_points.mean(axis=0)
    src_centered = src_points - src_mean
    dst_centered = dst_points - dst_mean

    src_var = np.sum(src_centered**2) / num
    cov = (dst_centered.T @ src_centered) / num

    U, D, Vt = np.linalg.svd(cov)
    S = np.eye(dim)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[dim - 1, dim - 1] = -1

    rotation = U @ S @ Vt
    scale = np.trace(np.diag(D) @ S) / src_var
    translation = dst_mean - scale * rotation @ src_mean

    M = np.zeros((2, 3), dtype=np.float64)
    M[:, :2] = scale * rotation
    M[:, 2] = translation
    return M


def _align_face(
    img_bgr: np.ndarray,
    landmarks: list[list[float]],
) -> np.ndarray:
    """Align face using 5-point landmarks to 112×112 target."""
    src = np.array(landmarks, dtype=np.float64)
    dst = _ADAFACE_TARGET_LANDMARKS.astype(np.float64)
    M = _similarity_transform(src, dst)
    aligned = cv2.warpAffine(
        img_bgr,
        M,
        (112, 112),
        borderValue=0.0,
    )
    return aligned


def _preprocess_adaface(aligned_bgr: np.ndarray) -> np.ndarray:
    """Preprocess aligned face for AdaFace: BGR, (pixel-127.5)/127.5, NCHW."""
    blob = aligned_bgr.astype(np.float32)
    blob = (blob - 127.5) / 127.5
    blob = np.transpose(blob, (2, 0, 1))  # HWC -> CHW
    blob = np.expand_dims(blob, 0)  # add batch dim
    return blob


def _evaluate_quality(
    confidence: float,
    bbox_xywh: list[float],
    landmarks: list[list[float]],
    quality_threshold: float,
) -> float:
    """Compute face quality score (same formula as face_quality.py)."""
    conf_threshold = 0.6
    min_w = 24.0
    min_h = 24.0

    face_w = bbox_xywh[2]
    face_h = bbox_xywh[3]

    confidence_score = min(confidence / conf_threshold, 1.0) if conf_threshold > 0 else 1.0
    size_w_score = min(face_w / min_w, 1.0) if min_w > 0 else 1.0
    size_h_score = min(face_h / min_h, 1.0) if min_h > 0 else 1.0
    size_score = min(size_w_score, size_h_score)

    if landmarks and len(landmarks) >= 5:
        landmark_score = 1.0
    elif landmarks and len(landmarks) > 0:
        landmark_score = 0.5
    else:
        landmark_score = 0.0

    quality = confidence_score * 0.5 + size_score * 0.3 + landmark_score * 0.2
    return max(0.0, min(quality, 1.0))


class OfflineFaceEmbedder:
    """Real YOLOv8-Face + AdaFace offline inference for external images."""

    def __init__(
        self,
        face_detector_onnx: str | None = None,
        adaface_onnx: str | None = None,
        onnx_provider: str | None = None,
        conf_threshold: float = _YOLOV8_CONF_THRESHOLD,
        nms_iou_threshold: float = _YOLOV8_NMS_IOU_THRESHOLD,
    ) -> None:
        self._face_detector_path = face_detector_onnx or _DEFAULT_YOLOV8_FACE_ONNX
        self._adaface_path = adaface_onnx or _DEFAULT_ADAFACE_ONNX
        self._provider_spec = onnx_provider or _DEFAULT_PROVIDER_SPEC
        self._conf_threshold = conf_threshold
        self._nms_iou_threshold = nms_iou_threshold

        self._det_session = None
        self._emb_session = None

    def _ensure_sessions(self) -> None:
        if self._det_session is None:
            self._det_session = load_session(
                self._face_detector_path, self._provider_spec
            )
        if self._emb_session is None:
            self._emb_session = load_session(
                self._adaface_path, self._provider_spec
            )

    @property
    def detector_providers(self) -> list[str]:
        self._ensure_sessions()
        return get_session_providers(self._det_session)

    @property
    def embedder_providers(self) -> list[str]:
        self._ensure_sessions()
        return get_session_providers(self._emb_session)

    def extract(
        self,
        image_path: str,
        *,
        allow_multiple_faces: bool = False,
        quality_threshold: float = 0.65,
    ) -> FaceImageEmbeddingResult:
        """Extract a single face embedding from an external image.

        Raises ValueError if no face detected, multiple faces (when not
        allowed), or quality too low.
        """
        self._ensure_sessions()

        img = cv2.imread(image_path)
        if img is None:
            raise ValueError(f"Failed to read image: {image_path}")

        orig_h, orig_w = img.shape[:2]

        # Step 1: YOLOv8-Face detection
        blob, scale, pad_x, pad_y = _preprocess_yolov8(img)
        det_input_name = self._det_session.get_inputs()[0].name
        det_output = self._det_session.run(None, {det_input_name: blob})[0]

        detections = _decode_yolov8_output(
            det_output,
            self._conf_threshold,
            scale,
            pad_x,
            pad_y,
            orig_w,
            orig_h,
        )

        # NMS
        detections = _nms(detections, self._nms_iou_threshold)

        # Filter minimum face size
        detections = [
            d
            for d in detections
            if d["bbox_xywh"][2] >= _YOLOV8_MIN_FACE_SIZE
            and d["bbox_xywh"][3] >= _YOLOV8_MIN_FACE_SIZE
        ]

        if not detections:
            raise ValueError("NO_FACE_DETECTED: No face detected in image")

        if len(detections) > 1 and not allow_multiple_faces:
            raise ValueError(
                f"MULTIPLE_FACES_DETECTED: {len(detections)} faces found; "
                "use --allow-multiple-faces to select best"
            )

        # Select best face by quality
        best_det = None
        best_quality = -1.0
        for det in detections:
            q = _evaluate_quality(
                det["confidence"],
                det["bbox_xywh"],
                det["landmarks"],
                quality_threshold,
            )
            if q > best_quality:
                best_quality = q
                best_det = det

        if best_det is None:
            raise ValueError("NO_FACE_DETECTED: No suitable face found")

        # Convert bbox from cxcywh to xywh for output
        cx, cy, w, h = best_det["bbox_xywh"]
        bbox_xywh = [cx - w / 2, cy - h / 2, w, h]

        # Step 2: Face alignment using landmarks
        aligned = _align_face(img, best_det["landmarks"])

        # Step 3: AdaFace embedding
        emb_blob = _preprocess_adaface(aligned)
        emb_input_name = self._emb_session.get_inputs()[0].name
        emb_outputs = self._emb_session.run(None, {emb_input_name: emb_blob})
        feature = emb_outputs[0][0]  # [512]

        # L2 normalize
        norm = float(np.linalg.norm(feature))
        if norm > 0:
            feature = feature / norm

        # Validate
        embedding = feature.tolist()
        embedding_dim = len(embedding)
        embedding_norm = float(np.linalg.norm(embedding))

        if embedding_dim != 512:
            raise ValueError(f"EMBEDDING_DIM_INVALID: got {embedding_dim}, expected 512")
        if not (0.90 <= embedding_norm <= 1.10):
            raise ValueError(
                f"EMBEDDING_NORM_INVALID: norm={embedding_norm:.6f} outside [0.90, 1.10]"
            )
        if any(math.isnan(x) or math.isinf(x) for x in embedding):
            raise ValueError("EMBEDDING_INVALID: NaN or Inf in embedding")

        return FaceImageEmbeddingResult(
            face_bbox=bbox_xywh,
            landmarks=best_det["landmarks"],
            face_confidence=best_det["confidence"],
            quality=best_quality,
            embedding=embedding,
            embedding_dim=embedding_dim,
            embedding_norm=embedding_norm,
            embedding_model="adaface",
            detector_model="yolov8_face",
            model_version=None,
            real_embedding_used=True,
        )
