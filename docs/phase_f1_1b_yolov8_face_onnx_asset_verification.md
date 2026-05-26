# Phase F1.1b — YOLOv8-Face ONNX Asset Verification

Date: 2026-05-26
HEAD at start: `d645714` (docs(infra): inventory composes and banner legacy ones)
Status: **read-only inspection.** No runtime change, no `module.yml`
edit, no compose edit, no model download, no TensorRT engine build,
no Savant container start, no GPU access.

This is the audit step that the F1.1a architecture lock called out as
a prerequisite ("YOLOv8-Face ONNX input/output tensor shapes
confirmed; manifest updated"). With the shapes confirmed, F1.1c can
proceed to build the `Yolov8FaceConverter` against known dimensions
instead of guessing.

---

## 1. Inputs to this phase

| Item | Expected | Actual on the deployment server |
|---|---|---|
| Manifest path | `/data/video-analytics/models/yolov8_face.onnx` | **does not exist at that exact path** |
| Real file | — | `/data/video-analytics/models/yolov8_face/yolov8n-face.onnx` (12.1 MB) |
| Repo working copy | — | `/home/user/video-analytics/yolomodel/yolov8n-face.onnx` (same md5; covered by `.gitignore`) |

The file IS present; the manifest entry just names it slightly
differently (subdir + `-` vs `_`). Path reconciliation options are
listed in §6.

## 2. Inspection tool

Added: `scripts/models/inspect_onnx_model.py` — read-only ONNX
inspector. Prints model-level metadata, graph inputs/outputs with
tensor shapes (including dynamic dims), node/initializer counts, and
post-shape-inference outputs. Errors cleanly (exit 2) when the `onnx`
Python package is absent — no auto-install. JSON mode for downstream
tooling (`--json`), text mode for human reading.

Reusable for future phases (AdaFace in F2, SCRFD in any future
swap, ArcFace ditto).

Execution environment used:

```bash
/home/user/anaconda3/envs/yolov13/bin/python   # onnx 1.14.0 available
```

The repo's default pytest interpreter
(`/home/user/anaconda3/envs/yolov13/bin/python`) already carries
`onnx`; no extra install was required.

## 3. Inspection result (verbatim, trimmed to the salient fields)

```
model_path        : /data/video-analytics/models/yolov8_face/yolov8n-face.onnx
file_size         : 12.1 MB (12,667,296 B)
ir_version        : 7
producer_name     : pytorch
producer_version  : 2.2.2
opset_imports     :
  - ai.onnx v12
graph_name        : main_graph
initializer_count : 165
node_count        : 268
op_kinds_top      :
  - Conv      73
  - Mul       66
  - Sigmoid   65
  - Concat    19
  - Reshape   13
  - Add        9
  - Split      8
  - Slice      4
  - MaxPool    3
  - Resize     2
inputs            :
  - images       shape=[1, 3, 640, 640]   FLOAT
outputs           :
  - output0      shape=[1, 20, 8400]      FLOAT
outputs (post shape-inference):
  - output0      shape=[1, 20, 8400]      FLOAT
any_dynamic       : False
```

All shapes are fully static. Batch is fixed at 1.

## 4. Output-format interpretation

The `[1, 20, 8400]` shape matches the YOLOv8-Face nano variant
exported with the standard derronqi-style head:

| Channel range | Meaning |
|---|---|
| `output0[0, 0:4, n]` | bbox `[cx, cy, w, h]` (YOLOv8 cxcywh convention; pixel units in the 640×640 model space) |
| `output0[0, 4,   n]` | face class confidence (single class) |
| `output0[0, 5:20, n]` | 5 landmarks × `(x, y, score)` = 15 values |

`N = 8400 = 80² + 40² + 20²` corresponds to feature-map anchors at
strides {8, 16, 32} on a 640×640 input. Anchor-free YOLOv8 head, no
DFL bin tensors visible.

NMS is **not** in the ONNX graph. The op-kind histogram shows
zero `NonMaxSuppression` and zero `EfficientNMS_TRT`. The converter
in F1.1c must implement NMS itself (or rely on Savant's
`BBoxSelector` post-NMS path, which is what the historical
`Yolo26PoseConverter` already does).

Landmark visibility/score is included (15 channels = 5 × 3), so the
F0 `face_quality.py` landmark_score component can consume real per-
point confidence values rather than placeholders.

## 5. Converter risk points for F1.1c

1. **Layout direction is `(B, C, N)`, not `(B, N, C)`.** Several
   YOLOv8-Face forks emit transposed outputs. The converter must
   confirm the orientation in the very first unit test (build a
   synthetic `[1, 20, 8400]` tensor with known cxcywh + landmarks
   and verify the decoder returns the right boxes). Getting this
   wrong silently produces detections at the right confidence but
   the wrong coordinates.
2. **Coordinates are in 640×640 model space.** Letterbox / scale
   mapping back to the source frame (typically 1920×1080) is the
   converter's responsibility. Savant's nvinfer normally handles
   this when the model is wired with the appropriate
   `maintain_aspect_ratio` / `symmetric_padding` options — verify
   what the YOLO26-pose nvinfer block already sets, and copy.
3. **Static batch = 1.** For C1.2's current single-camera smoke this
   is fine. For 60-camera production we'll want dynamic batch — that
   is a re-export of the ONNX (own future phase), not a F1.1c
   problem. Document it as a known limit so the production
   throughput plan doesn't assume batched YOLOv8-Face inference.
4. **Opset 12** is older than what Savant's most recent samples ship
   with (often opset 13+). TensorRT 8.x in the Savant 0.6.0
   container supports opset 12 for all observed op kinds (Conv, Mul,
   Sigmoid, Slice, Concat, Reshape, Split, MaxPool, Resize, Add).
   No mitigation needed; just record the version so a future
   container upgrade can re-verify.
5. **Single-class output**. Don't write the converter assuming
   `nc > 1`. The selector config (`BBoxSelector`) should still be
   keyed on class_id 0 for `label: face` — same pattern as YOLO26-
   pose's `class_id: 0 label: person`.
6. **Landmark NaN / out-of-frame guard.** When a face is partially
   off-frame, landmarks can come back as negative or > image bounds.
   The converter should pass them through unchanged (so quality
   filter can decide), but downstream code must not assume positive
   coordinates.

## 6. Path mismatch — needs an operator decision before F1.1c

The manifest expects `/data/video-analytics/models/yolov8_face.onnx`;
the real file is at
`/data/video-analytics/models/yolov8_face/yolov8n-face.onnx`. Pick
one before F1.1c wires it into `module.yml`:

- **Option A — move/symlink the file (no spec change):**
  ```bash
  cd /data/video-analytics/models
  # either rename:
  mv yolov8_face/yolov8n-face.onnx yolov8_face.onnx
  rmdir yolov8_face
  # or symlink:
  ln -s yolov8_face/yolov8n-face.onnx yolov8_face.onnx
  ```
  Manifest stays the same. Pre-existing scripts that hardcode the
  manifest path keep working.

- **Option B — update the manifest:**
  Change the manifest path entry to
  `/data/video-analytics/models/yolov8_face/yolov8n-face.onnx`, and
  use that path verbatim in the future `module.yml` and any future
  `module_assets_check.sh`. No file move on disk.

Either is acceptable. The current F1.1b manifest update records both
paths so F1.1c can resolve this without a second audit.

## 7. F1.1c (next phase) checklist

When F1.1c starts, it should:

1. Resolve the path mismatch (§6).
2. Confirm the license declared by the user (provisional Apache-2.0
   based on the `yolov8n-face` filename).
3. Add `modules/savant_security/custom/converters/yolov8_face.py`,
   a `BaseComplexModelOutputConverter` subclass that:
   - Reshapes `[1, 20, 8400] → [8400, 20]`.
   - Thresholds on `output[:, 4]`.
   - Decodes bbox cxcywh → xyxy in model coordinates.
   - Decodes landmarks `(x, y, score)` per keypoint.
   - Runs NMS (reuse `custom.converters.scrfd.nms_boxes`).
   - Returns face objects with `face` label, bbox, landmark
     attributes, and confidence.
4. Build unit tests with synthetic `[1, 20, 8400]` tensors against a
   known-good golden detection set, to lock the layout
   interpretation in §4.
5. Wire the converter into the `nvinfer@detector` skeleton block
   already sketched in `specs/02_savant_pipeline.md` §3.
6. Build the TensorRT engine via Savant's auto-engine-build path
   (engine stays out of git per `.gitignore`).
7. Smoke-test F1 acceptance per `specs/02_savant_pipeline.md` §8:
   YOLOv8-Face engine loads, test video produces face bbox + 5
   landmarks, face quality field present, intrusion chain
   unaffected.

F1.1c does NOT need to write any AdaFace code. AdaFace is F2.

## 8. What this phase did NOT do

- No `module.yml` change.
- No converter added.
- No model download.
- No TensorRT engine build.
- No Savant container start.
- No GPU access.
- No compose change.
- No face-worker / pgvector / watchlist / live-search code.
- No `.onnx` / `.engine` / `.pt` / `.pth` / `.ckpt` committed.
- No `savant_phase1d` stub cleanup (still out of scope).

## 9. Related documents

| Document | Content |
|---|---|
| `docs/phase_f1_1a_in_pipeline_face_architecture_lock.md` | Architecture lock that called for this verification |
| `docs/model_assets_manifest.md` | Updated YOLOv8-Face entry with verified shapes (this commit) |
| `specs/02_savant_pipeline.md` | Pipeline element skeleton awaiting F1.1c converter |
| `specs/04_face_intelligence.md` | First-version detector / embedder / vector-store contract |
| `scripts/models/inspect_onnx_model.py` | The new reusable inspection tool (this commit) |

---

*Written 2026-05-26. Phase F1.1b — read-only inspection. No runtime change.*
