# C1F.1 Same-Frame Dual Primary Detection — Final Report

Date: 2026-06-01

Status: **PASS**

## 1. Objective

Verify that the C1 official replay dev stack can run YOLO26-pose and
YOLOv8-Face as dual full-frame primary detectors on the same RTSP frame,
producing a same-frame debug summary with person detections, keypoints,
face detections, landmarks, and face-person association — all from a single
frame anchor.

## 2. Phase Results

| Phase | Status | Commit |
|-------|--------|--------|
| C1F.1a — Dual primary config audit | PASS_CONFIG_READY | `8952ddb` |
| C1F.1c — Same-frame debug summary export | PASS_DEBUG_EXPORT_READY | `1908583` |
| C1F.1c.1 — Artifact path runtime sanity | PASS_PATH_READY | `9a3b9dc` |
| C1F.1d.6 — Harden same_frame_detection_debug | PASS | `91a0986` |

## 3. C1F.1d Isolation Summary

The C1F.1d series systematically isolated a pipeline startup failure:

| Experiment | Module | Result |
|-----------|--------|--------|
| C1F.1d.2 | Staged startup (replay before savant) | BLOCKED — same 0.3s stop |
| C1F.1d.4 | ZMQ no-op (source + noop_counter) | PASS — 541+ frames |
| C1F.1d.4 | Pose only (source + yolo26_pose + tracker + noop) | PASS — 661+ frames |
| C1F.1d.4 | Same-frame trimmed (pose + face + assoc + debug) | FAIL — 0.3s stop |
| C1F.1d.5 | Detector only (pose + tracker + face + noop) | PASS — 511+ frames |
| C1F.1d.5 | Debug no assoc (pose + tracker + face + debug) | FAIL — 0.3s stop |
| C1F.1d.5 | Assoc no debug (pose + tracker + face + assoc + noop) | PASS — 661+ frames |

**Excluded:** Replay, ZMQ source, YOLO26-pose, nvtracker, YOLOv8-Face detector,
face-person associator, AdaFace, artifact path, startup order.

**Root cause:** `same_frame_detection_debug.py` `on_start()` performed file I/O
(mkdir + open), which caused the Savant 0.6.0 GStreamer pipeline to stop within
~0.3s. Fix: moved file operations to lazy init in `_ensure_file()`, called from
first `process_frame()`.

## 4. Final Smoke Results

```
Result=PASS
run_duration=24s
frames_inspected=96
frames_with_person=91
frames_with_keypoints=91
frames_with_face=38
frames_with_both=38
association_observed=yes
sample_keypoints_count=17
sample_landmarks_count=5
```

## 5. Trimmed Module

`modules/savant_security/module.c1f1_same_frame.yml` is a **minimal subset**
used only for the same-frame dual primary detection smoke. It contains:

- `zeromq_source_bin`
- `nvinfer@complex_model` yolo26_pose (full-frame primary)
- `nvtracker`
- `nvinfer@complex_model` yolov8_face (full-frame primary)
- `pyfunc` face_person_associator
- `pyfunc` same_frame_detection_debug

It does **NOT** contain: behavior_rules, adaface, face_reid_gate,
face_observation_exporter, face_embedding_debug, face_debug.

This module is NOT a production module. It does NOT prove AdaFace embedding,
face observation export, or Redis face_observations.

## 6. What C1F.1 Proves

- Same RTSP input chain (`rtsp://10.37.57.112:8554/live/1080movie`)
- Replay inline (source-adapter → replay-service → savant-security)
- YOLO26-pose full-frame primary detection
- YOLOv8-Face full-frame primary detection
- Same frame anchor: person + 17 keypoints + face + 5 landmarks simultaneously
- Face-person association observable (502 matched pairs in 690 frames)
- Same-frame debug JSONL summary written to disk

## 7. What C1F.1 Does NOT Prove

- AdaFace embedding
- face_reid_gate quality gate
- face_observation_exporter Redis export
- Redis `security.face_observations` stream
- face-worker INSERT into PostgreSQL
- PostgreSQL `face_observations` table
- pgvector gallery match
- watchlist_hit event
- live_search_hit event
- API / frontend
- production annotated_clip

## 8. Boundaries

| Boundary | Status |
|----------|--------|
| No second RTSP | ✅ config-verified |
| No source extraction fallback | ✅ config-verified |
| No production annotated_clip | ✅ config-verified |
| No JPEG/PNG/RAW/crop/base64 in Redis | ✅ config-verified |
| Debug visual renderer only | ✅ config-verified |

## 9. Related Files

| File | Purpose |
|------|---------|
| `scripts/smoke/check_c1f1_same_frame_pose_face_detection.sh` | C1F.1d smoke script |
| `modules/savant_security/module.c1f1_same_frame.yml` | Trimmed module for dual primary smoke |
| `modules/savant_security/custom/pyfuncs/same_frame_detection_debug.py` | Hardened debug pyfunc |
| `harness/tests/test_c1f1a_dual_primary_config_contract.py` | C1F.1a contract tests (33) |
| `harness/tests/test_c1f1c_same_frame_debug_summary_contract.py` | C1F.1c contract tests (55) |
| `docs/c1f1a_dual_primary_current_state_audit.md` | C1F.1a audit doc |
| `docs/c1f1c_same_frame_debug_summary_export.md` | C1F.1c spec doc |
