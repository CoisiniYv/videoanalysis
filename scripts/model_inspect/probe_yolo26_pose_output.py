#!/usr/bin/env python3
"""Offline ONNXRuntime probe for YOLO26-pose model output.

Loads a frame from video, runs ONNX inference, and dumps detailed info
about the output tensor to diagnose why objects=0 in the Savant pipeline.

Usage:
    python scripts/model_inspect/probe_yolo26_pose_output.py \
        --model /data/video-analytics/models/yolo26_pose/yolo26_pose.onnx \
        --video testVideo/test.mp4 \
        --frame-index 30 \
        --imgsz 640 \
        [--save-debug /tmp/yolo26_pose_debug.jpg]
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


def letterbox(
    img: np.ndarray,
    new_shape=(640, 640),
    color=(114, 114, 114),
) -> tuple[np.ndarray, float, float, float, float]:
    """Resize and pad image to target size, keeping aspect ratio."""
    shape = img.shape[:2]  # H, W
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])

    new_unpad = (int(round(shape[1] * r)), int(round(shape[0] * r)))  # W, H
    dw = new_shape[1] - new_unpad[0]
    dh = new_shape[0] - new_unpad[1]

    dw /= 2
    dh /= 2

    if (shape[1], shape[0]) != new_unpad:
        img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)

    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))

    img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    return img, r, left, top


def parse_args():
    parser = argparse.ArgumentParser(description="Probe YOLO26-pose ONNX output")
    parser.add_argument("--model", required=True, help="Path to ONNX model")
    parser.add_argument("--video", required=True, help="Path to test video")
    parser.add_argument("--frame-index", type=int, default=30, help="Frame index to extract")
    parser.add_argument("--imgsz", type=int, default=640, help="Model input size")
    parser.add_argument("--save-debug", default=None, help="Save debug visualization to path")
    return parser.parse_args()


def main():
    args = parse_args()

    model_path = Path(args.model)
    if not model_path.is_file():
        print(f"[FAIL] Model not found: {args.model}")
        sys.exit(1)

    video_path = Path(args.video)
    if not video_path.is_file():
        print(f"[FAIL] Video not found: {args.video}")
        sys.exit(1)

    # ---- Load model ----
    try:
        import onnxruntime as ort
    except ImportError:
        print("[FAIL] onnxruntime not installed. Run: pip install onnxruntime")
        sys.exit(1)

    session = ort.InferenceSession(str(model_path))
    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name

    print(f"Model: {model_path}")
    print(f"Input:  {input_name}  shape={session.get_inputs()[0].shape}")
    print(f"Output: {output_name}  shape={session.get_outputs()[0].shape}")
    print()

    # ---- Read frame ----
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"[FAIL] Cannot open video: {args.video}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"Video: {video_path}  {total_frames} frames  {fps:.2f} fps")
    print(f"Target frame index: {args.frame_index}")

    frame_idx = args.frame_index
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ret, frame_bgr = cap.read()
    cap.release()

    if not ret:
        print(f"[FAIL] Could not read frame {frame_idx}")
        sys.exit(1)

    orig_h, orig_w = frame_bgr.shape[:2]
    print(f"Frame {frame_idx}: {orig_w}x{orig_h}  dtype={frame_bgr.dtype}")
    print()

    # ---- Preprocess (letterbox) ----
    imgsz = args.imgsz
    img_letterbox, scale, pad_left, pad_top = letterbox(frame_bgr, new_shape=(imgsz, imgsz))
    print(f"Letterbox: {img_letterbox.shape[1]}x{img_letterbox.shape[0]}  "
          f"scale={scale:.4f}  pad=({pad_left:.0f},{pad_top:.0f})")

    # BGR → RGB, HWC → CHW, float32, /255
    img_rgb = cv2.cvtColor(img_letterbox, cv2.COLOR_BGR2RGB)
    img_norm = img_rgb.astype(np.float32) / 255.0
    img_chw = np.ascontiguousarray(img_norm.transpose(2, 0, 1))
    input_tensor = img_chw[None, :, :, :]  # (1, 3, H, W)

    print(f"Input tensor: shape={input_tensor.shape}  "
          f"min={input_tensor.min():.4f}  max={input_tensor.max():.4f}")
    print()

    # ---- Run inference ----
    outputs = session.run([output_name], {input_name: input_tensor})
    raw = outputs[0]  # (1, 300, 57)
    print(f"Output raw: shape={raw.shape}  dtype={raw.dtype}")
    print(f"  min={raw.min():.6f}  max={raw.max():.6f}")
    print()

    # ---- Analyse output ----
    out = raw[0]  # (300, 57)

    scores = out[:, 4]  # column 4 = score
    class_ids = out[:, 5]  # column 5 = class_id

    print("=== Score analysis (column 4) ===")
    print(f"  min={scores.min():.6f}  max={scores.max():.6f}")
    top10_idx = np.argsort(scores)[-10:][::-1]
    print(f"  top10 values: {[f'{scores[i]:.6f}' for i in top10_idx]}")
    print()

    for thresh in [0.01, 0.05, 0.15, 0.25, 0.35]:
        cnt = int((scores > thresh).sum())
        print(f"  count score>{thresh:.2f} = {cnt}")

    print()
    print("=== Class ID analysis (column 5) ===")
    unique_classes, class_counts = np.unique(class_ids, return_counts=True)
    print(f"  unique class_ids: {list(zip(unique_classes.astype(int), class_counts))}")
    # Top classes by score
    top_class_idx = np.argsort(scores)[-20:][::-1]
    print(f"  top-20 class_ids: {[int(class_ids[i]) for i in top_class_idx]}")
    print()

    # ---- Filtered detections ----
    person_mask = (scores > 0.25) & (class_ids == 0)
    person_count = int(person_mask.sum())
    print(f"=== Person detections (score>0.25, class_id==0) ===")
    print(f"  count = {person_count}")

    if person_count == 0:
        # Broader search
        any_person = class_ids == 0
        any_person_count = int(any_person.sum())
        print(f"  person class any score: count = {any_person_count}")
        if any_person_count > 0:
            person_scores = scores[any_person]
            print(f"  person scores: min={person_scores.min():.6f}  max={person_scores.max():.6f}")
        print()

    # ---- Top-5 rows detail ----
    valid_idx = np.where(scores > 0.05)[0]
    valid_idx = valid_idx[np.argsort(scores[valid_idx])[-5:][::-1]]
    print()
    print("=== Top-5 rows (score > 0.05) ===")
    print()
    for i, idx in enumerate(valid_idx):
        row = out[idx]
        bbox = row[0:4]  # xyxy in letterbox coords
        score = row[4]
        cls_id = int(row[5])
        kpts = row[6:57].reshape(17, 3)  # (17, 3)

        valid_05 = int((kpts[:, 2] > 0.05).sum())
        valid_25 = int((kpts[:, 2] > 0.25).sum())

        print(f"  [{i}] row={idx}  class={cls_id}  score={score:.6f}")
        print(f"       bbox_xyxy=({bbox[0]:.1f},{bbox[1]:.1f},{bbox[2]:.1f},{bbox[3]:.1f})")
        print(f"       keypoints shape={kpts.shape}")
        print(f"       kpt_conf  min={kpts[:,2].min():.4f}  max={kpts[:,2].max():.4f}  "
              f"mean={kpts[:,2].mean():.4f}")
        print(f"       valid kpts @0.05={valid_05}  @0.25={valid_25}")
        print()

    # ---- Save debug visualization ----
    if args.save_debug and person_count > 0:
        _save_debug(args.save_debug, frame_bgr, out, valid_idx, imgsz,
                     scale, pad_left, pad_top, orig_w, orig_h)
        print(f"  Debug image saved to: {args.save_debug}")

    print("=== Done ===")


def _save_debug(path, frame_bgr, out, valid_idx, imgsz,
                scale, pad_left, pad_top, orig_w, orig_h):
    """Draw top detections on the original frame."""
    debug = frame_bgr.copy()
    for idx in valid_idx:
        row = out[idx]
        score = row[4]
        cls_id = int(row[5])
        if cls_id != 0 or score < 0.05:
            continue

        # bbox in letterbox coords → original frame coords
        x1_lb, y1_lb, x2_lb, y2_lb = row[0:4]
        x1 = (x1_lb - pad_left) / scale
        y1 = (y1_lb - pad_top) / scale
        x2 = (x2_lb - pad_left) / scale
        y2 = (y2_lb - pad_top) / scale

        # Clip to frame
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(orig_w, int(x2)), min(orig_h, int(y2))

        cv2.rectangle(debug, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
        cv2.putText(debug, f"{score:.3f}", (int(x1), int(y1) - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        # Keypoints
        kpts = row[6:57].reshape(17, 3)
        for kp in kpts:
            if kp[2] > 0.25:
                kx = int((kp[0] - pad_left) / scale)
                ky = int((kp[1] - pad_top) / scale)
                cv2.circle(debug, (kx, ky), 3, (0, 0, 255), -1)

    cv2.imwrite(path, debug)


if __name__ == "__main__":
    main()
