# Phase F2.0 — AdaFace ONNX Asset Verification

Date: 2026-05-26
Status: complete

## 1. Current HEAD

`c2a38f0` — `docs(face): add savant face reid official reference`

## 2. Model Path

```
/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx
```

Existence: **confirmed** — 166.7 MB (174,746,887 bytes).

## 3. ONNX Inspection Result

| Attribute | Value |
|-----------|-------|
| IR version | 7 |
| Producer | PyTorch 1.13.1 |
| Opset | ai.onnx v14 |
| Graph name | `torch_jit` |
| Node count | 156 |
| Initializer count | 238 |

### Top ops

| Op | Count |
|----|-------|
| Conv | 52 |
| BatchNormalization | 26 |
| PRelu | 25 |
| Add | 24 |
| MaxPool | 21 |
| Shape / Gather / Unsqueeze / Concat / Reshape | 1 each |

### Input

- Name: `input`
- Shape: `[batch_size, 3, 112, 112]` (NCHW, **dynamic batch**)
- Dtype: FLOAT

### Outputs

- **`feature`** — shape `[batch_size, 512]`, FLOAT.
  This is the 512-d embedding vector. **NOT L2-normalized** — the
  converter must apply L2 normalization before emission.
- **`norm`** — shape `[batch_size, 1]`, FLOAT.
  Per-sample scalar (likely the pre-normalization feature norm). MVP
  discards or logs for debugging.

### Dynamic axes

Batch dimension only (`batch_size`). Spatial dims (3, 112, 112) and
embedding dim (512) are static.

## 4. Preprocessing Assumptions

From official Savant `samples/face_reid` reference:

- Input: 112×112 aligned face crop.
- Alignment: GPU-side via `AlignFacePreprocessingObjectImageGPU`.
- Color format: **BGR**.
- Normalization: mean/std per official sample preprocessing config.
  Exact values pending confirmation; Savant's `nvinfer@classifier`
  object-preprocessing handles this inside the pipeline.

## 5. Embedding Dimension

**512** — confirmed from ONNX output tensor shape.

## 6. Converter / Runtime Implications

- Savant role: `nvinfer@attribute_model` keyed on face object.
- Post-model: extract `feature` (output index 0), L2-normalize.
- `norm` output (index 1) can be discarded in MVP.
- Dynamic batch supports Savant batching; no ONNX re-export needed.

## 7. Risks Before F2.1

| Risk | Mitigation |
|------|------------|
| L2 normalization not built-in | Converter must apply `feature / ||feature||` |
| Preprocessing normalization values unconfirmed | Confirm from official sample preprocessing stanza before engine build |
| BGR vs RGB mismatch | Use same preprocessing as official sample (BGR) |
| License unconfirmed | Verify commercial use clearance before production engine build |
| TensorRT engine not built | F2.1 builds engine via Savant auto-engine-build path |

## 8. Next Phase (F2.1) Recommendation

**Proceed.** The ONNX asset is verified: 512-d, dynamic batch, standard
IR50/WebFace4M backbone. No ONNX re-export needed.

F2.1 prerequisites:
1. YOLOv8-Face detector wired in module.yml (F1.1c).
2. AdaFace ONNX at the verified path.
3. Face alignment preprocessing confirmed from official sample.
4. TensorRT engine built on first run via Savant.

## 9. Commit Policy

- Model file: NOT committed (`.gitignore` covers `*.onnx`).
- Manifest updated: `docs/model_assets_manifest.md`.
- Phase doc: this file.
