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

- Purpose: face embedding extraction (112×112 aligned face crop input).
- **Actual file on the deployment server (verified Phase F2.0, 2026-05-26):**
  `/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx`
  (166.7 MB).
- Status: **present and inspected**.
- Producer: PyTorch 1.13.1
- IR version: 7
- Opset: ai.onnx v14
- Graph: `torch_jit`, 156 nodes, 238 initializers.
- Input tensor:
  - name: `input`
  - dtype: FLOAT
  - shape: `[batch_size, 3, 112, 112]` (NCHW, **dynamic batch**)
- Output tensors:
  - name: `feature`, dtype: FLOAT, shape: `[batch_size, 512]`
    — 512-d embedding, **NOT L2-normalized** (raw feature vector).
    Converter MUST L2-normalize this output before emission.
  - name: `norm`, dtype: FLOAT, shape: `[batch_size, 1]`
    — per-sample scalar (likely feature norm before any built-in
    scaling). For MVP, discard or log for debugging.
- Dynamic axes: batch dimension only (`batch_size`). Spatial dims
  (3, 112, 112) and output dim (512) are static.
- Preprocessing (from official Savant face_reid sample):
  - Input: 112×112 aligned face crop (GPU alignment via
    `savant.input_preproc.align_face.AlignFacePreprocessingObjectImageGPU`).
  - Color format: **BGR**.
  - Normalization: mean/std per official sample preprocessing config
    — exact values pending confirmation from sample's
    `module.yml` preprocessing stanza. Savant's `nvinfer@classifier`
    object-preprocessing handles this inside the pipeline.
- Postprocessing (converter responsibility in F2):
  - Extract `feature` output (index 0).
  - L2-normalize: `feature = feature / np.linalg.norm(feature, axis=-1, keepdims=True)`.
  - Embedding dimension: **512**.
- Source: `adaface_ir50_webface4m` — IR50 backbone pretrained on
  WebFace4M. Official sample uses the same backbone variant.
  TODO — user to confirm exact upstream source / commit and license.
- License: **TODO — pending user confirmation.** Provisional from
  official Savant sample's asset list is the public
  `adaface_ir50_webface4m_90fb74c.zip`; user should verify commercial
  use clearance before building the TensorRT engine.
- TensorRT engine: **not generated in F2.0**. F2.1 builds the engine
  via Savant's auto-engine-build path.
- Git policy: `.gitignore` covers `*.onnx`; the file is never
  committed.
- Pipeline role: **in-pipeline embedding**. Runs inside the Savant
  module as `nvinfer@attribute_model` keyed on YOLOv8-Face's face
  object. Does NOT run in face-worker. Does NOT run in an external
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
