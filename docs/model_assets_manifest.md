# Model Assets Manifest

This file records required model assets for the video-analytics
project. Models are NOT committed to git. Users must prepare them
manually.

> Updated by Phase F1.1a (2026-05-26). First-version detector is now
> YOLOv8-Face full-frame primary, first-version embedder is AdaFace
> in-pipeline. SCRFD_2.5G and ArcFace are preserved as future
> candidates. See
> `docs/phase_f1_1a_in_pipeline_face_architecture_lock.md`.

## Face Intelligence Pipeline (first version, F1 / F2)

### YOLOv8-Face (detector — F1)

- Purpose: full-frame face detection with 5-point landmarks.
- Expected path: `/data/video-analytics/models/yolov8_face.onnx`
- Status: user has prepared a converted ONNX with landmarks output.
  Input / output tensor shapes still require introspection in F1.1b
  before the converter is wired into `module.yml`.
- Typical input shape (TO CONFIRM via `onnx.shape_inference` / Netron):
  `1x3x640x640` (RGB or BGR — verify against export).
- Typical output shape (TO CONFIRM): `(1, N, 21)` where each row is
  `[x, y, w, h, conf, kp1_x, kp1_y, kp2_x, kp2_y, kp3_x, kp3_y,
  kp4_x, kp4_y, kp5_x, kp5_y]` or an equivalent layout. Some
  exports emit `(N, 6 + 10)` with separate landmark tensor — record
  the actual layout once introspected.
- Source: TODO — user to confirm upstream repository (commonly
  `derronqi/yolov8-face` or its Apache-2.0 fork).
- License: TODO — must be Apache-2.0 or another commercially-friendly
  license; SCRFD-style "research only" weights are NOT acceptable for
  this slot.
- Pipeline role: **PRIMARY** (full-frame), NOT secondary-on-person-ROI
  per F1.1a lock.
- Phase required: **F1**.

### AdaFace (embedder — F2)

- Purpose: face embedding extraction (112×112 aligned crop input).
- Expected path: `/data/video-analytics/models/adaface.onnx`
- Status: pending — model must be sourced and placed by the operator
  before F2 runtime smoke.
- Input shape: TODO (typical: `1x3x112x112`; verify BGR vs RGB and
  pixel normalization).
- Output shape: TODO (typical: `1x512` embedding; verify dimension and
  whether L2 normalization is built-in).
- Source: TODO — user to confirm (e.g., `mk-minchul/AdaFace`,
  `adaface_ir101_webface4m` variant).
- License: TODO — confirm commercial use clearance before F2 build.
- Pipeline role: **in-pipeline embedding**. Runs inside the Savant
  module as `nvinfer@classifier` keyed on YOLOv8-Face's face object.
  Does NOT run in face-worker. Does NOT run in an external
  Triton/Python service.
- Phase required: **F2**.

### Future detector candidate — SCRFD_2.5G

- Purpose: alternate face detector with better small-face recall in
  dense crowds.
- Expected path: `/data/video-analytics/models/scrfd_2.5g.onnx`
- Status: **future detector candidate only.** Not the first-version
  target since F1.1a. The skeleton converter at
  `modules/savant_security/custom/converters/scrfd.py` remains for
  this swap; its internal NMS / confidence-filter / ROI→frame mapping
  utilities are detector-agnostic and reusable today.
- Input shape: TODO (typical: 640x640x3).
- Output shape: TODO — multi-FPN-scale anchor outputs need decoding.
- Source: TODO — user to confirm; InsightFace pretrained weights have
  non-commercial license terms that need procurement clearance.
- License: TODO — likely "research only" unless InsightFace consents.
- Phase required: future (post-F2 swap, requires a phase doc that
  supersedes F1.1a).

### Future embedding alternative — ArcFace

- Purpose: alternate face embedding backbone.
- Expected path: `/data/video-analytics/models/arcface.onnx`
- Status: **future embedding alternative only.** AdaFace is the
  first-version embedder since F1.1a.
- Input shape: TODO (typical: 1x3x112x112).
- Output shape: TODO (typical: 1x512 embedding).
- Source / License: TODO.
- Phase required: future swap (post-F2, requires a phase doc that
  supersedes F1.1a).

## Existing models (already prepared)

### YOLO26-pose

- Path: managed in module config (`/models/yolo26_pose/yolo26_pose.onnx`).
- Status: integrated in Phase 1/2; running in production for person
  detection + 17 COCO keypoints.

## Policy

- Do NOT commit `.onnx`, `.engine`, `.pt`, `.pth`, `.ckpt` files to
  git. `.gitignore` covers all five extensions (F1.1a chore commit).
- TensorRT engine files are **machine and GPU specific** — they are
  built on the target host from the ONNX, never copied between
  machines or committed.
- ONNX weights live on the deployment server's
  `/data/video-analytics/models/` directory. Operator places them
  manually per this manifest.
- Each model swap (YOLOv8-Face → SCRFD, AdaFace → ArcFace, or any
  other) must be accompanied by a new phase doc that explicitly
  supersedes F1.1a and updates this manifest in the same commit.
