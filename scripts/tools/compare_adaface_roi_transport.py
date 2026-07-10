#!/usr/bin/env python3
"""Compare inline-aligned and JPEG-transport AdaFace embeddings offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/face-worker"))

from app.offline_face_embedder import (  # noqa: E402
    _decode_yolov8_output,
    _nms,
    _preprocess_adaface,
    _preprocess_yolov8,
)


SAVANT_ADAFACE_TARGET = np.array(
    [
        [38.29459953, 51.69630051],
        [73.53179932, 51.50139999],
        [56.02519989, 71.73660278],
        [41.54930115, 92.3655014],
        [70.72990036, 92.20410156],
    ],
    dtype=np.float32,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--face-model", type=Path, required=True)
    parser.add_argument("--adaface-model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-every", type=int, default=24)
    parser.add_argument("--max-samples", type=int, default=32)
    parser.add_argument("--jpeg-quality", type=int, default=95)
    parser.add_argument("--threshold", type=float, default=0.60)
    parser.add_argument("--face-confidence", type=float, default=0.45)
    parser.add_argument("--min-face-size", type=float, default=40.0)
    parser.add_argument(
        "--gallery-file",
        type=Path,
        help="Optional id|person_id|person_name|embedding JSON-lines export.",
    )
    return parser.parse_args()


def session(path: Path) -> ort.InferenceSession:
    providers = [
        provider
        for provider in ("CUDAExecutionProvider", "CPUExecutionProvider")
        if provider in ort.get_available_providers()
    ]
    return ort.InferenceSession(str(path), providers=providers)


def detect_best_face(
    detector,
    frame: np.ndarray,
    *,
    confidence: float,
    min_face_size: float,
):
    blob, scale, pad_x, pad_y = _preprocess_yolov8(frame)
    output = detector.run(None, {detector.get_inputs()[0].name: blob})[0]
    detections = _decode_yolov8_output(
        output,
        confidence,
        scale,
        pad_x,
        pad_y,
        frame.shape[1],
        frame.shape[0],
    )
    detections = [
        item
        for item in _nms(detections, 0.50)
        if item["bbox_xywh"][2] >= min_face_size
        and item["bbox_xywh"][3] >= min_face_size
    ]
    return max(detections, key=lambda item: item["confidence"], default=None)


def align(frame: np.ndarray, landmarks) -> np.ndarray:
    matrix, _ = cv2.estimateAffinePartial2D(
        np.asarray(landmarks, dtype=np.float32), SAVANT_ADAFACE_TARGET
    )
    if matrix is None:
        raise ValueError("affine_estimation_failed")
    return cv2.warpAffine(frame, matrix, (112, 112), borderValue=0)


def embedding(embedder, image: np.ndarray) -> np.ndarray:
    blob = _preprocess_adaface(image)
    feature = embedder.run(None, {embedder.get_inputs()[0].name: blob})[0][0]
    feature = feature.astype(np.float32)
    norm = float(np.linalg.norm(feature))
    if norm <= 0:
        raise ValueError("zero_embedding_norm")
    return feature / norm


def distribution(values: list[float]) -> dict:
    array = np.asarray(values, dtype=np.float64)
    return {
        "count": int(array.size),
        "min": float(array.min()),
        "p50": float(np.quantile(array, 0.50)),
        "p95": float(np.quantile(array, 0.95)),
        "max": float(array.max()),
    }


def load_gallery(path: Path) -> tuple[list[dict], np.ndarray]:
    rows = []
    embeddings = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split("|", 3)
        if len(fields) != 4:
            raise ValueError(f"invalid_gallery_row:{line_number}")
        vector = np.asarray(json.loads(fields[3]), dtype=np.float32)
        if vector.shape != (512,):
            raise ValueError(f"invalid_gallery_embedding:{line_number}:{vector.shape}")
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            raise ValueError(f"zero_gallery_embedding:{line_number}")
        rows.append(
            {
                "gallery_embedding_id": int(fields[0]),
                "person_id": int(fields[1]),
                "person_name": fields[2],
            }
        )
        embeddings.append(vector / norm)
    if not embeddings:
        raise ValueError("empty_gallery")
    return rows, np.stack(embeddings)


def main() -> int:
    args = parse_args()
    detector = session(args.face_model)
    embedder = session(args.adaface_model)
    capture = cv2.VideoCapture(str(args.video))
    raw_embeddings: list[np.ndarray] = []
    jpeg_embeddings: list[np.ndarray] = []
    frame_indices: list[int] = []
    frame_index = -1
    while len(raw_embeddings) < args.max_samples:
        ok, frame = capture.read()
        if not ok:
            break
        frame_index += 1
        if frame_index % max(args.sample_every, 1):
            continue
        detection = detect_best_face(
            detector,
            frame,
            confidence=args.face_confidence,
            min_face_size=args.min_face_size,
        )
        if detection is None:
            continue
        aligned = align(frame, detection["landmarks"])
        encoded_ok, encoded = cv2.imencode(
            ".jpg",
            aligned,
            [cv2.IMWRITE_JPEG_QUALITY, int(args.jpeg_quality)],
        )
        if not encoded_ok:
            raise RuntimeError("jpeg_encode_failed")
        transported = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if transported is None or transported.shape != (112, 112, 3):
            raise RuntimeError("jpeg_roundtrip_failed")
        raw_embeddings.append(embedding(embedder, aligned))
        jpeg_embeddings.append(embedding(embedder, transported))
        frame_indices.append(frame_index)
    capture.release()
    if len(raw_embeddings) < 2:
        raise RuntimeError("not_enough_face_samples")

    raw = np.stack(raw_embeddings)
    jpeg = np.stack(jpeg_embeddings)
    paired_cosine = np.sum(raw * jpeg, axis=1)
    raw_similarity = raw @ raw.T
    jpeg_query_similarity = jpeg @ raw.T
    pair_mask = ~np.eye(len(raw), dtype=bool)
    raw_decisions = raw_similarity[pair_mask] >= args.threshold
    jpeg_decisions = jpeg_query_similarity[pair_mask] >= args.threshold
    raw_top1 = np.argmax(raw_similarity, axis=1)
    jpeg_top1 = np.argmax(jpeg_query_similarity, axis=1)
    gallery_report = None
    if args.gallery_file:
        gallery_rows, gallery = load_gallery(args.gallery_file)
        raw_gallery_similarity = raw @ gallery.T
        jpeg_gallery_similarity = jpeg @ gallery.T
        raw_gallery_decisions = raw_gallery_similarity >= args.threshold
        jpeg_gallery_decisions = jpeg_gallery_similarity >= args.threshold
        raw_gallery_top1 = np.argmax(raw_gallery_similarity, axis=1)
        jpeg_gallery_top1 = np.argmax(jpeg_gallery_similarity, axis=1)
        gallery_report = {
            "entries": gallery_rows,
            "score_abs_delta": distribution(
                np.abs(raw_gallery_similarity - jpeg_gallery_similarity)
                .reshape(-1)
                .tolist()
            ),
            "threshold_decision_agreement": float(
                np.mean(raw_gallery_decisions == jpeg_gallery_decisions)
            ),
            "threshold_decision_mismatches": int(
                np.count_nonzero(raw_gallery_decisions != jpeg_gallery_decisions)
            ),
            "top1_agreement": float(np.mean(raw_gallery_top1 == jpeg_gallery_top1)),
            "raw_match_count": int(np.count_nonzero(raw_gallery_decisions)),
            "jpeg_match_count": int(np.count_nonzero(jpeg_gallery_decisions)),
        }
    report = {
        "status": "passed",
        "video": str(args.video.resolve()),
        "face_model": str(args.face_model.resolve()),
        "adaface_model": str(args.adaface_model.resolve()),
        "providers": {
            "face": detector.get_providers(),
            "adaface": embedder.get_providers(),
        },
        "sample_count": len(raw_embeddings),
        "frame_indices": frame_indices,
        "jpeg_quality": args.jpeg_quality,
        "detector_gate": {
            "confidence": args.face_confidence,
            "min_face_size": args.min_face_size,
            "note": (
                "The detector gate selects offline comparison samples only; "
                "it does not change the production ROI exporter gate."
            ),
        },
        "match_threshold": args.threshold,
        "paired_raw_vs_jpeg_cosine": distribution(paired_cosine.tolist()),
        "pairwise_similarity_abs_delta": distribution(
            np.abs(raw_similarity[pair_mask] - jpeg_query_similarity[pair_mask]).tolist()
        ),
        "threshold_decision_agreement": float(
            np.mean(raw_decisions == jpeg_decisions)
        ),
        "threshold_decision_mismatches": int(
            np.count_nonzero(raw_decisions != jpeg_decisions)
        ),
        "top1_agreement": float(np.mean(raw_top1 == jpeg_top1)),
        "watchlist_gallery_comparison": gallery_report,
        "acceptance": {
            "paired_cosine_p50_gte_0_985": bool(
                np.quantile(paired_cosine, 0.50) >= 0.985
            ),
            "watchlist_threshold_decisions_identical": bool(
                np.array_equal(raw_gallery_decisions, jpeg_gallery_decisions)
                if gallery_report is not None
                else np.array_equal(raw_decisions, jpeg_decisions)
            ),
            "accepted_watchlist_identities_identical": bool(
                np.array_equal(raw_gallery_decisions, jpeg_gallery_decisions)
                if gallery_report is not None
                else np.array_equal(raw_decisions, jpeg_decisions)
            ),
            "top1_identical": bool(np.array_equal(raw_top1, jpeg_top1)),
        },
    }
    if not all(report["acceptance"].values()):
        report["status"] = "failed"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
