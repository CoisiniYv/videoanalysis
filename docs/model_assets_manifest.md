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
- Expected manifest path: `/data/video-analytics/models/yolov8_face.onnx`
- **Actual file on the deployment server (verified Phase F1.1b, 2026-05-26):**
  `/data/video-analytics/models/yolov8_face/yolov8n-face.onnx`
  (12.1 MB; md5 also matches the repo-local copy at
  `yolomodel/yolov8n-face.onnx` which is gitignored.)
- **Path mismatch resolution required** — pick one before F1.1c:
  - Option A: rename / symlink the deployment file to the manifest
    path (`mv yolov8_face/yolov8n-face.onnx yolov8_face.onnx` or `ln
    -s yolov8_face/yolov8n-face.onnx yolov8_face.onnx`).
  - Option B: update this manifest + the future `module.yml` /
    `module_assets_check.sh` to read from
    `yolov8_face/yolov8n-face.onnx`.
- Status: **present and inspected**.
- Producer: PyTorch 2.2.2
- IR version: 7
- Opset: ai.onnx v12
- Graph: `main_graph`, 268 nodes, 165 initializers.
- Input tensor:
  - name: `images`
  - dtype: FLOAT
  - shape: `[1, 3, 640, 640]` (NCHW, **static batch = 1**)
- Output tensor:
  - name: `output0`
  - dtype: FLOAT
  - shape: `[1, 20, 8400]` (NCN, **fully static**)
- Output layout (interpretation, to be confirmed by F1.1c converter
  unit tests with synthetic input):
  - `output0[0, 0:4, n]` — bbox `[cx, cy, w, h]` (YOLOv8 cxcywh convention)
  - `output0[0, 4, n]` — face confidence (single class)
  - `output0[0, 5:20, n]` — 5 landmarks × (x, y, score) = 15 values
  - N = 8400 = 80² + 40² + 20² (strides {8, 16, 32} at 640×640)
- Landmark support: **confirmed** (15 channels = 5 × 3 layout).
- NMS in graph: **no** (no `NonMaxSuppression` node in op-kind
  histogram — Conv 73, Mul 66, Sigmoid 65, Concat 19, Reshape 13,
  Add 9, Split 8, Slice 4, MaxPool 3, Resize 2). Converter MUST
  apply NMS itself.
- Dynamic axes: **none**. Batch is static at 1. F1.1c TensorRT engine
  will be built with `min/opt/max = 1`; if 60-camera throughput needs
  batching, the ONNX must be re-exported with dynamic batch first
  (own phase, not F1.1b's scope).
- Source: filename `yolov8n-face` is consistent with the
  `derronqi/yolov8-face` Apache-2.0 nano variant. **TODO — user to
  confirm the exact upstream commit and re-confirm license.**
- License: **TODO — pending user confirmation.** Provisional reading
  is Apache-2.0 (compatible with commercial use).
- Pipeline role: **PRIMARY** (full-frame), NOT secondary-on-person-ROI
  per F1.1a lock.
- TensorRT engine: **not generated in F1.1b**. F1.1c builds the
  engine via Savant's auto-engine-build path.
- Git policy: `.gitignore` covers `*.onnx`; the file is never
  committed (verified post-Phase F1.1a chore commit `d583e67`).
- Phase required: **F1**.

### F0 placeholder paths (NOT the real face detector — historical)

`/home/user/video-analytics/yolomodel/yolov8n-face.onnx` exists as a
working copy and is byte-identical to the deployment-server file. It
is covered by `.gitignore` (`yolomodel/` rule) and is not committed.
Operators may keep it for local experimentation; the deployment-server
path is the authoritative source.

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
