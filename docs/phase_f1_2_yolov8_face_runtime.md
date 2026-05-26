# Phase F1.2 — YOLOv8-Face Detector Runtime Integration

Date: 2026-05-26
Status: code complete, static smoke pass, GPU runtime smoke pending

## 1. What Changed

Added YOLOv8-Face as a full-frame face detector to the
`savant_security` module, alongside the existing YOLO26-pose person
detector.  The two detectors share the same frame input but produce
independent object tracks.

### Files changed

| File | Change |
|------|--------|
| `modules/savant_security/module.yml` | Added YOLOv8-Face nvinfer block + FaceDebugPyFunc |
| `infra/docker-compose.c1-official-adapter.yml` | Added `FACE_DETECTOR_BATCH_SIZE=1` env var |
| `modules/savant_security/custom/pyfuncs/face_debug.py` | New — minimal face debug logging pyfunc |
| `scripts/smoke/check_f1_2_yolov8_face_runtime.sh` | New — config + model-path static smoke |
| `harness/tests/test_f1_2_yolov8_face_config.py` | New — static config tests |
| `docs/phase_f1_2_yolov8_face_runtime.md` | This doc |
| `docs/model_assets_manifest.md` | Updated runtime path + batch policy |

### Module.yml block summary

```yaml
- element: nvinfer@complex_model
  name: yolov8_face
  properties:
    interval: 0
  model:
    format: onnx
    model_file: /models/yolov8_face.onnx
    batch_size: 1  (default, env FACE_DETECTOR_BATCH_SIZE)
    precision: fp16
    input:
      shape: [3, 640, 640]
    output:
      layer_names: [output0]
      converter:
        module: savant.converter.yolo_v8face
        class_name: YoloV8faceConverter
      objects:
        - class_id: 0
          label: face
          selector:
            module: savant.selector.detector
            class_name: MinMaxSizeBBoxSelector
            kwargs:
              min_width: 40
              min_height: 40
      attributes:
        - name: landmarks
```

Key design decisions:
- Full-frame detector (no `input.object`) — NOT secondary on person ROI.
- Official `savant.converter.yolo_v8face.YoloV8faceConverter` — no
  custom converter needed.
- `landmarks` attribute — 5-point facial landmarks.
- `MinMaxSizeBBoxSelector` at 40×40 px minimum face size.

## 2. Why Official Converter

The Savant 0.6.0-7.1 runtime image ships
`savant.converter.yolo_v8face.YoloV8faceConverter`, which handles:
- YOLOv8-format output decoding (`output0` with `[1, 20, 8400]` layout)
- NMS
- Landmark extraction (5 × (x, y, score) from channels 5:20)
- Bbox coordinate mapping to frame space

This project adds no custom YOLOv8-Face decoder — the official
converter is the sole decoder path.  If a future detector swap (e.g.,
SCRFD) is needed, a custom converter will be added at that time.

## 3. Detector Batch Limitation

The YOLOv8-Face ONNX has **static batch = 1**.  Key implications:

- `FACE_DETECTOR_BATCH_SIZE=1` is a hard constraint in F1.2.
- TensorRT cannot override this to batch 8 or 16.
- Per-frame throughput is gated by single-batch inference latency.
- If 60-camera throughput requires larger detector batch:
  - Re-export YOLOv8-Face ONNX with dynamic batch.
  - OR add batch-dimension surgery as a dedicated phase.
  - This is a known architectural limitation, NOT a bug.

## 4. Runtime Model Path

| What | Path |
|------|------|
| Actual ONNX | `/data/video-analytics/models/yolov8_face/yolov8n-face.onnx` |
| Runtime stable path | `/data/video-analytics/models/yolov8_face.onnx` (symlink) |
| Inside container | `/models/yolov8_face.onnx` (mounted from host) |

## 5. Smoke Result

Static smoke: **PASS** (config syntax, model path, batch policy verified).
GPU runtime smoke: **NOT RUN** (requires GPU + test video + `C1_TEST_SOURCE_URI`).

To run GPU smoke:
```bash
C1_TEST_SOURCE_URI=<rtsp-or-file> bash scripts/smoke/check_f1_2_yolov8_face_runtime.sh
# Then watch logs:
docker logs c1-official-savant | grep '\[face_debug\]'
```

Expected output: `[face_debug] frame=N source=... pts=... faces=M` with
bbox + landmarks values every ~30 frames.

## 6. Known Risks

| Risk | Mitigation |
|------|------------|
| Static batch = 1 limits throughput | Re-export ONNX with dynamic batch before multi-camera perf test |
| Full-frame face detector adds GPU load | Monitor GPU utilization; reduce face detection interval if needed |
| Face-person association not yet implemented | F2+ phase; for now face and person objects are independent |
| AdaFace not yet wired | F2 phase; module.yml placeholder prepared |
| Official converter may differ from sample | First GPU run will confirm; fallback to custom converter if needed |
| FaceDebugPyFunc is verbose at log_every_n_frames=30 | Tune interval per env var if needed |

## 7. Next Phase

**F2** — AdaFace in-pipeline embedding:
- Wire `nvinfer@attribute_model` on face objects.
- Face alignment preprocessing (GPU-side).
- L2-normalize AdaFace `feature` output.
- Emit `FaceObservationEvent` to Redis `security.face_observations`.

Prerequisites for F2:
1. F1.2 GPU runtime smoke passes (face objects + landmarks confirmed).
2. AdaFace ONNX verified (F2.0 already complete).
3. Fac person association design finalized.
