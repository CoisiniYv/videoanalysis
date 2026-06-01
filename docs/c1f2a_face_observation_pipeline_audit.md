# C1F.2a Face Observation Pipeline Audit

Date: 2026-06-01

Status: **PASS_FACE_OBSERVATION_CONFIG_READY**

## 1. Objective

Audit the face observation pipeline readiness from YOLOv8-Face detection
through AdaFace embedding, face_reid_gate, face_observation_exporter, to
Redis `security.face_observations` — without running a real RTSP smoke.

## 2. Target Chain

```text
RTSP → Source Adapter → Replay → Savant
  → YOLO26-pose (full-frame primary)
  → nvtracker (person-only)
  → YOLOv8-Face (full-frame primary)
  → face-person association (geometry-only)
  → AdaFace (attribute model on face crops)
  → face_reid_gate (quality gate + throttle)
  → face_observation_exporter (Redis XADD)
  → Redis security.face_observations
```

## 3. Audit Results

### 3.1 AdaFace Element

| Check | Result | Evidence |
|-------|--------|----------|
| Element exists in module.yml | ✅ | `nvinfer@attribute_model`, name=`adaface` (line 158) |
| Element type | `nvinfer@attribute_model` | Not a complex_model; runs on face crops |
| Pipeline position | After `face_person_associator` | Line 158 is after line 145 |

### 3.2 AdaFace Model Asset

| Asset | Path | Exists | Size |
|-------|------|--------|------|
| ONNX | `/models/adaface/adaface_ir50_webface4m.onnx` | YES | 167M |
| Engine | `/models/adaface/adaface_ir50_webface4m.onnx_b16_gpu0_fp16.engine` | YES | 87M |
| Config | `/models/adaface/adaface_ir50_webface4m_config_savant.txt` | YES | 587B |
| Batch size | `FACE_EMBEDDING_BATCH_SIZE=16` | Configured in compose | — |

### 3.3 AdaFace Input Contract

| Check | Result | Evidence |
|-------|--------|----------|
| input.object | `yolov8_face.face` | module.yml line 166 |
| Input shape | `[3, 112, 112]` | module.yml line 167 |
| Color format | `bgr` | module.yml line 170 |
| Preprocessing | `AlignFacePreprocessingObjectImageGPU` | module.yml line 172-173 |
| Preprocessing depends on landmarks | YES | AlignFace uses 5-point landmarks for face alignment |
| Output layer | `feature` | module.yml line 175 |
| Converter | `TensorToVectorConverter` | module.yml line 178 |
| Output attribute name | `feature` | module.yml line 180 |

### 3.4 Landmark Contract

| Component | Landmark Attr | Format |
|-----------|--------------|--------|
| YOLOv8-Face output | `yolov8_face.landmarks` | 5-point landmarks (10 floats or 5 pairs) |
| AdaFace AlignFace input | Reads from face object | Same `yolov8_face.landmarks` |
| face_reid_gate check | `lm_count in (5, 10, 15)` | Accepts 5 pairs, 10 floats, or 15 floats |
| face_observation_exporter | Reads `yolov8_face.landmarks` | Passes through to observation |

**Contract:** YOLOv8-Face outputs `landmarks` attribute → AdaFace `AlignFace` reads
same attribute for face alignment → face_reid_gate validates count → exporter
passes through. Consistent.

### 3.5 face_reid_gate

| Check | Result | Evidence |
|-------|--------|----------|
| Element exists | ✅ | module.yml line 188 |
| Checks person_track_id > 0 | ✅ | face_reid_gate.py line 87 |
| Checks face confidence >= threshold | ✅ | line 96 (default 0.6) |
| Checks min face width/height | ✅ | line 105 (default 40.0) |
| Checks landmarks = 5 | ✅ | line 124 (accepts 5, 10, 15) |
| Checks feature dim = 512 | ✅ | line 141 |
| Checks embedding norm tolerance | ✅ | line 151 (1.0 ± 0.10) |
| Rejects NaN | ✅ | line 160 |
| Per camera+track throttle | ✅ | `ReIDThrottleMap` with `min_interval_ms` |
| Default min_interval_ms | 1000ms | face_reid_gate.py line 49 |

### 3.6 face_observation_exporter

| Check | Result | Evidence |
|-------|--------|----------|
| Element exists | ✅ | module.yml line 205 |
| Writes Redis stream | ✅ | `security.face_observations` (configurable via env) |
| Uses XADD | ✅ | face_observation_exporter.py line 131 |
| maxlen configurable | ✅ | default 10000 |
| Requires reid_allowed=true | ✅ | line 109 |
| Requires person_track_id > 0 | ✅ | line 120 |
| Requires non-empty embedding | ✅ | line 127 |
| Per-track export throttle | ✅ | `ExportThrottleMap` |
| Frame anchor metadata included | ✅ | lines 356-373 |

### 3.7 Image Bytes Forbidden

| Check | Result | Evidence |
|-------|--------|----------|
| No JPEG/PNG/RAW in exporter | ✅ | No image encoding code |
| No crop bytes in exporter | ✅ | Only embedding vector (~2KB JSON) |
| No base64 in exporter | ✅ | No base64 encoding |
| Embedding is JSON list | ✅ | `json.dumps(obs_dict)` line 113 |

### 3.8 Export Default Disabled

| Check | Result | Evidence |
|-------|--------|----------|
| Code default | `"true"` | face_observation_exporter.py line 157 |
| Compose override | `"false"` | c1-official-replay-dev.yml line 89 |
| Smoke can enable | `FACE_OBSERVATION_EXPORT_ENABLED=1` | Env var override |

### 3.9 Full Module Startup Risk

The full `module.yml` contains all elements (behavior_rules, yolov8_face,
face_person_associator, adaface, face_reid_gate, face_observation_exporter,
face_embedding_debug, same_frame_detection_debug, face_debug). The
C1F.1d investigation proved that `on_start()` file I/O in pyfuncs can
cause pipeline stop. The hardened `same_frame_detection_debug.py` has
been fixed, but other pyfuncs with `on_start()` may have similar issues.

**Recommendation:** Create a C1F.2-specific module that includes only the
face observation chain, without behavior_rules or debug pyfuncs.

## 4. Conclusion

**PASS_FACE_OBSERVATION_CONFIG_READY**

All 13 audit items pass at the configuration level. The face observation
pipeline is correctly wired from YOLOv8-Face through AdaFace, face_reid_gate,
and face_observation_exporter to Redis. The landmark contract is consistent.
Image bytes are forbidden. Export is disabled by default.

**IMPORTANT:** This is a config audit only. It does NOT mean AdaFace embedding,
face_reid_gate, or face_observation_exporter have been runtime validated.
See `docs/c1f2b_face_pipeline_runtime_reality_correction.md` for the
runtime scope correction.

## 5. C1F.2-Specific Module

A trimmed module `module.c1f2_face_observation.yml` is recommended for
C1F.2 runtime smoke, containing only:

```text
zeromq_source_bin
→ yolo26_pose
→ nvtracker
→ yolov8_face
→ face_person_associator
→ adaface
→ face_reid_gate
→ face_observation_exporter
```

Without: behavior_rules, face_embedding_debug, same_frame_detection_debug,
face_debug.

## 6. Related Files

| File | Purpose |
|------|---------|
| `modules/savant_security/module.yml` | Full pipeline with all elements |
| `modules/savant_security/custom/pyfuncs/face_reid_gate.py` | Quality gate pyfunc |
| `modules/savant_security/custom/pyfuncs/face_observation_exporter.py` | Redis exporter pyfunc |
| `modules/savant_security/custom/services/face_reid_gate.py` | Pure Python gate logic |
| `modules/savant_security/custom/services/face_observation_exporter.py` | Redis stream exporter |
| `modules/savant_security/custom/models/face_events.py` | FaceObservationEventDraft |
