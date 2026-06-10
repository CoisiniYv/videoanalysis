# Phase F1.1c - Savant Face ReID Official Reference

Date: 2026-05-26
Status: reference doc

This document summarizes the official Savant `samples/face_reid`
usage of YOLOV8-Face + AdaFace, and maps it to this project’s
PostgreSQL + pgvector architecture.

Sources:

- [Official sample README](https://github.com/insight-platform/Savant/tree/develop/samples/face_reid)
- [Official sample module.yml](/tmp/Savant-upstream/samples/face_reid/src/module.yml)
- [Official sample index_builder.yml](/tmp/Savant-upstream/samples/face_reid/src/index_builder.yml)

---

## 1. What the official sample does

The sample builds a face ReID pipeline with:

- YOLOV8-Face detector.
- nvtracker track assignment.
- AdaFace embedding inside the Savant module.
- HNSWLIB index storage for gallery search.

It is split into two flows:

- Index Builder.
- Demo / runtime module.

## 2. Official module wiring

The sample’s runtime module uses:

- `nvinfer@complex_model` for YOLOV8-Face.
- `savant.converter.yolo_v8face.YoloV8faceConverter`.
- `nvtracker`.
- `nvinfer@attribute_model` for AdaFace.
- `savant.input_preproc.align_face.AlignFacePreprocessingObjectImageGPU`.
- `savant.converter.TensorToVectorConverter`.
- `samples.face_reid.recognition.Recognition` for HNSWLIB lookup.

Key model settings:

- detector input: `3x640x640`.
- detector output: `output0`.
- reid input: `3x112x112`.
- reid model file: `adaface_ir50_webface4m.onnx`.
- reid batch size: `16`.
- color format: `bgr`.
- preprocessing: face alignment on GPU.

## 3. Official model asset

The sample uses the remote asset:

- `adaface_ir50_webface4m_90fb74c.zip`

Inside the zip is:

- `adaface_ir50_webface4m.onnx`

So:

- download -> unzip -> ONNX file.
- no manual ONNX conversion is required.
- Savant will build the TensorRT engine on first run.

Official sample download path pattern:

```text
s3://savant-data/models/adaface_ir50_webface4m_90fb74c/adaface_ir50_webface4m_90fb74c.zip
```

## 4. What we reuse

We reuse the official Savant-side face pipeline shape:

- full-frame face detector.
- landmarks.
- face alignment.
- AdaFace in-pipeline embedding.

## 5. What we do not reuse

We do not reuse the sample’s gallery index backend:

- no HNSWLIB.
- no `Recognition` pyfunc.
- no `index_builder_client.py`.
- no demo overlay workflow as production logic.

This project writes face observations to Redis, then hands matching and
storage to `face-worker` + PostgreSQL + pgvector.

## 6. Local asset placement

Verified local asset:

- `/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx`

This is an ONNX file, not a TensorRT engine.

Recommended local path convention for this repo:

```text
/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx
```

## 7. Project integration rule

For this repo, the official sample should be translated into:

```text
YOLOv8-Face
  -> face-person association
  -> face quality filter
  -> AdaFace embedding
  -> Redis security.face_observations
  -> face-worker
  -> PostgreSQL + pgvector
```

The downstream matcher is our own business stack, not the sample’s
HNSWLIB index.

## 8. Official run flow

The upstream sample is normally exercised like this:

```bash
./scripts/run_module.py --build-engines samples/face_reid/src/module.yml
docker compose -f samples/face_reid/docker-compose.x86.yml --profile index up
docker compose -f samples/face_reid/docker-compose.x86.yml --profile demo up
```

Notes:

- index mode builds the gallery index from images in `assets/gallery`.
- demo mode runs the live ReID pipeline with the prebuilt index.
- the sample stores gallery matches in HNSWLIB, which we do not reuse.
- our project keeps only the Savant-side detector / embedder pattern.
