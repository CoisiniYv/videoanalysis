# Phase F2.1 — AdaFace Runtime

## Purpose

Add AdaFace face embedding as a Savant `nvinfer@attribute_model` in the
GPU pipeline. Verify that 512-d feature vectors are produced on
YOLOv8-Face detections and that the feature metadata is readable with
associated `person_track_id`.

This phase does NOT implement Redis face observations, pgvector search,
watchlist, or live_search.

## Official Sample Mapping

| Official sample | Local adaptation |
|---|---|
| `nvinfer@attribute_model` for AdaFace | Same element type |
| `savant.input_preproc.align_face.AlignFacePreprocessingObjectImageGPU` | Same class |
| `savant.converter.TensorToVectorConverter` | Same class |
| `samples.face_reid.recognition.Recognition` | **NOT used** — no HNSWLIB |
| `hnswlib` index | **NOT used** — pgvector in later phase |
| Input shape `[3, 112, 112]` | Same |
| Offsets `[127.5, 127.5, 127.5]` | Same |
| Scale factor `0.007843137254902` | Same |
| Color format `bgr` | Same |
| Batch size 16 | Same (`FACE_EMBEDDING_BATCH_SIZE`) |

## What We Reuse

- `AlignFacePreprocessingObjectImageGPU` for GPU-side 5-point landmark
  alignment to 112x112.
- `TensorToVectorConverter` for post-inference vector extraction.
- Official sample's input preprocessing config (offsets, scale, color).
- Batch size 16.

## What We Do NOT Reuse

- `samples.face_reid.recognition.Recognition` pyfunc (HNSWLIB lookup).
- HNSWLIB index builder workflow.
- Any external recognition/index service.

## module.yml Block

```yaml
- element: nvinfer@attribute_model
  name: adaface
  model:
    format: onnx
    model_file: /models/adaface/adaface_ir50_webface4m.onnx
    batch_size: ${oc.decode:${oc.env:FACE_EMBEDDING_BATCH_SIZE, 16}}
    precision: fp16
    input:
      object: yolov8_face.face
      shape: [3, 112, 112]
      offsets: [127.5, 127.5, 127.5]
      scale_factor: 0.007843137254902
      color_format: bgr
      preprocess_object_image:
        module: savant.input_preproc.align_face
        class_name: AlignFacePreprocessingObjectImageGPU
    output:
      layer_names: [feature]
      converter:
        module: savant.converter
        class_name: TensorToVectorConverter
      attributes:
        - name: feature
```

## Pipeline Order

```text
YOLO26-pose -> nvtracker -> BehaviorRulesPyFunc
  -> YOLOv8-Face (full-frame detector)
    -> FacePersonAssociatorPyFunc
      -> AdaFace (attribute_model)
        -> FaceEmbeddingDebugPyFunc
          -> FaceDebugPyFunc
```

## Preprocessing Details

- `AlignFacePreprocessingObjectImageGPU` reads 5-point landmarks from
  YOLOv8-Face detections and performs GPU-side affine alignment to
  produce 112x112 face crops.
- Input normalization: mean offsets `[127.5, 127.5, 127.5]`,
  scale_factor `0.007843137254902` (= 1/127.5).
- Color format: BGR (matching AdaFace training convention).

## Model Path

- Host: `/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx`
- Container: `/models/adaface/adaface_ir50_webface4m.onnx`
- Engine (auto-built): `/models/adaface/adaface_ir50_webface4m.onnx_b16_gpu0_fp16.engine`

## Batch Policy

- `FACE_EMBEDDING_BATCH_SIZE=16` (dynamic batch ONNX, max 16).
- `FACE_DETECTOR_BATCH_SIZE=1` (unchanged, static batch constraint).

## Static Test Results

32/32 tests pass in `harness/tests/test_f2_1_adaface_config.py`.

All F1.2 and F1.3 tests continue to pass (70/70 total).

## GPU Runtime Smoke Result

**PASS** — 2026-05-26

- AdaFace engine build: SUCCESS (b16_gpu0_fp16, ~48s first run).
- `[face_embedding]` logs: appear with `feature_dim=512`.
- `raw_l2_norm=1.0`: TensorToVectorConverter applies L2 normalization
  internally (contradicts earlier ONNX inspection).
- `person_track_id`: present on all embedded faces.
- `landmarks`: 10 values (5 points x 2 coords), readable after
  AdaFace stage.
- Pipeline stable, no crashes.

## Feature Metadata Evidence

```text
[face_embedding] frame=1501 source=c1_2_test faces=3 associated=3 embedded=3
  face[0] person_track_id=107 feature_dim=512 raw_l2_norm=1.0 first3=[-0.0338,0.0485,0.0204] landmarks=10
  face[1] person_track_id=120 feature_dim=512 raw_l2_norm=1.0 first3=[-0.0270,-0.0709,-0.0266] landmarks=10
  face[2] person_track_id=34 feature_dim=512 raw_l2_norm=1.0 first3=[0.0171,0.0305,-0.0764] landmarks=10
```

## Key Finding: L2 Normalization

Earlier ONNX inspection (F2.0) showed that the raw `feature` output
from the AdaFace ONNX model was NOT L2-normalized. However, runtime
observation shows `raw_l2_norm=1.0` consistently, indicating that
`TensorToVectorConverter` applies L2 normalization internally.

This means:
- No custom converter or post-processing L2 normalization is needed.
- Features can be used directly for cosine similarity (which equals
  dot product for unit vectors) in pgvector.

## Known Risks

| Risk | Mitigation |
|---|---|
| AlignFace fails if landmarks missing | Face must pass YOLOv8-Face with landmarks; quality gate in F1.4 |
| Batch 16 may cause OOM on T4 with many concurrent faces | Monitor GPU memory; reduce batch if needed |
| TensorToVectorConverter normalization behavior undocumented | Verified at runtime; add regression test |
| No face quality filter yet | F1.4 adds blur/brightness/size gate |
| No per-track throttle yet | F1.4 adds one-embedding-per-N-seconds |

## Files Changed

- `modules/savant_security/module.yml` — added AdaFace block + face_embedding_debug
- `modules/savant_security/custom/pyfuncs/face_embedding_debug.py` — new
- `infra/docker-compose.c1-official-adapter.yml` — added FACE_EMBEDDING_BATCH_SIZE
- `harness/tests/test_f2_1_adaface_config.py` — new (32 tests)
- `scripts/smoke/check_f2_1_adaface_runtime.sh` — new
- `docs/model_assets_manifest.md` — updated AdaFace section
- `docs/phase_f1_3_face_person_association.md` — noted F2.1 consumption

## Next Phase Recommendation

**F1.4** — Face quality filter + per-track throttle:
- Filter faces by blur/brightness/size quality score.
- Throttle face processing per person track (one embedding per N seconds).
- Reduce downstream AdaFace compute load.

**F3** — Face observation pipeline:
- Emit `FaceObservationEvent` to Redis `security.face_observations`.
- face-worker consumes from Redis, stores in PostgreSQL + pgvector.
- pgvector cosine similarity search.
- watchlist_hit / live_search_hit events.
