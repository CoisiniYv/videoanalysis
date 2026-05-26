# Phase F1.2 — YOLOv8-Face Detector Runtime Integration

Date: 2026-05-26
Status: code complete, static smoke pass, GPU runtime smoke **PASS**

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

Static smoke: **PASS** (14/14 — config syntax, model path, batch policy verified).
GPU runtime smoke: **PASS** (face bbox + confidence + landmarks confirmed).

### GPU Runtime Smoke Details

- **Date/Time**: 2026-05-26 14:00–14:25 UTC+8
- **Compose**: `infra/docker-compose.c1-official-adapter.yml`
- **Input**: `testVideo/allface.mp4` via source adapter `c1_2_test`
- **Container**: `c1-official-savant` — running, exit code 0

### Evidence

| Check | Result |
|-------|--------|
| savant-security started | YES |
| YOLOv8-Face engine build | SUCCESS (~3 min, cached after first run) |
| Official converter import | OK (`savant.converter.yolo_v8face.YoloV8faceConverter`) |
| `[face_debug]` logs | YES — 105 lines |
| Face count | 0, 1, 2, 3, 5 faces per frame |
| Face bbox | `RBBox { xc, yc, width, height }` |
| Face confidence | 0.69–0.85 |
| Face landmarks | `landmarks=10pts` (5 × 2 coords) |
| Container crash | NONE |
| TensorRT errors | NONE |
| Batch mismatch | NONE |
| C1.2 behavior rules | Still running (frame 4380+) |

### Key Log Lines

```
[face_debug] frame=31 source=c1_2_test pts=1280000000 faces=1
  face[0] conf=0.825 bbox="RBBox { xc: 917.89, ...}" landmarks=10pts value=[846.75, 257.92, 942.14, 255.05, 881.06, 299.89]...
[face_debug] frame=841 source=c1_2_test pts=33680000000 faces=5
  face[0] conf=0.723 ... landmarks=10pts value=[...]
  face[4] conf=0.815 ... landmarks=10pts value=[...]
```

### Landmarks Access Fix

Original `FaceDebugPyFunc` used `getattr(obj, "landmarks", None)` which always
returned `None`.  The correct Savant API is:

```python
attr = obj.get_attr_meta("yolov8_face", "landmarks")
value = attr.value  # list of 10 floats: [x1,y1,x2,y2,x3,y3,x4,y4,x5,y5]
```

Reference: `custom/adapters/person_pose_adapter.py:96` uses the same pattern
for YOLO26-pose keypoints (`obj.get_attr_meta("yolo26_pose", "keypoints")`).

### Issues Fixed During Smoke

1. **Model symlink**: Absolute host symlink didn't resolve in container.
   Fixed with relative symlink: `ln -sf yolov8_face/yolov8n-face.onnx`
2. **Engine path mismatch**: TensorRT saved engine to resolved symlink path.
   Fixed with container-internal symlink.
3. **Landmarks API**: `getattr` → `get_attr_meta` (see above).

## 6. Known Risks

| Risk | Mitigation |
|------|------------|
| Static batch = 1 limits throughput | Re-export ONNX with dynamic batch before multi-camera perf test |
| Full-frame face detector adds GPU load | Monitor GPU utilization; reduce face detection interval if needed |
| Face-person association not yet implemented | F2+ phase; for now face and person objects are independent |
| AdaFace not yet wired | F2 phase |
| Engine path symlink is inside container | Deployment concern: engine symlink must survive container recreation |

## 7. Output Video

The `video-file-sink` service writes output to:

```
/data/video-analytics/media/c1-official-savant-output/c1_2_test%/test%/video.mov
```

**This file is NOT used as F1.2 acceptance evidence.** Checked timestamp:

```
2026-05-26 01:03:35 — from an earlier C1.2 run, NOT from this F1.2 smoke (14:00+)
```

The file is a stale artifact from a previous pipeline run. The current F1.2 smoke
did not produce a new annotated video because the source adapter looped the test
video before the `video-file-sink` could process the face-detector frames.

F1.2 acceptance evidence is **docker logs only**:
- `[face_debug]` lines with face count, bbox, confidence, landmarks
- Engine build success logs
- Container stability (no crash)

Annotated video output validation is a **separate future task** — not part of F1.2.

## 8. Next Phase

**F2** — AdaFace in-pipeline embedding:
- Wire `nvinfer@attribute_model` on face objects.
- Face alignment preprocessing (GPU-side).
- L2-normalize AdaFace `feature` output.
- Emit `FaceObservationEvent` to Redis `security.face_observations`.

Prerequisites for F2:
1. F1.2 GPU runtime smoke passes — **DONE** (face objects + landmarks confirmed).
2. AdaFace ONNX verified (F2.0 already complete).
3. Face-person association design finalized.

**F1.3 — Face-Person Association** can start:
- Face objects are available in pipeline with bbox + confidence + landmarks.
- Person objects are available via YOLO26-pose + nvtracker (with track_id).
- Association logic can match face bbox to person bbox spatially.
- No AdaFace dependency for basic spatial association.
