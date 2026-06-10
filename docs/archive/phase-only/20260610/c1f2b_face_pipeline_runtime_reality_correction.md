# C1F.2b Face Pipeline Runtime Reality Correction

Date: 2026-06-01

Status: **PASS_RUNTIME_SCOPE_CORRECTED**

## 1. Purpose

This document corrects the scope interpretation of C1F.2a and establishes
the boundary between "config present" and "runtime validated" for the face
observation pipeline.

## 2. Current Runtime Validated Capabilities

These capabilities have been verified through real RTSP smoke tests
(C1F.1 series):

| Capability | Status | Evidence |
|-----------|--------|----------|
| Inline Replay (source-adapter → replay → savant) | RUNTIME VERIFIED | C1F.1d.6 smoke |
| YOLO26-pose full-frame primary | RUNTIME VERIFIED | 91 frames with person |
| nvtracker person tracking | RUNTIME VERIFIED | track_id observable |
| 17 keypoints per person | RUNTIME VERIFIED | keypoints_count=17 |
| YOLOv8-Face full-frame primary | RUNTIME VERIFIED | 38 frames with face |
| 5 landmarks per face | RUNTIME VERIFIED | landmarks_count=5 |
| Same-frame person + face | RUNTIME VERIFIED | 38 frames with both |
| Face-person association | RUNTIME VERIFIED | 502 matched pairs |
| Same-frame debug JSONL | RUNTIME VERIFIED | 690 lines written |

## 3. NOT Runtime Validated

These capabilities exist in code/config but have NOT been verified through
real RTSP runtime:

| Capability | Status | Reason |
|-----------|--------|--------|
| AdaFace preprocessing (AlignFace) | CONFIG ONLY | Not in C1F.1 trimmed module |
| AdaFace embedding runtime | CONFIG ONLY | Not in C1F.1 trimmed module |
| face_reid_gate runtime | CONFIG ONLY | Not in C1F.1 trimmed module |
| face_observation_exporter runtime | CONFIG ONLY | Not in C1F.1 trimmed module |
| Redis security.face_observations | CONFIG ONLY | Export disabled in compose |
| face-worker INSERT | NOT IMPLEMENTED | face-worker only does INSERT, no GPU embedding |
| PostgreSQL face_observations | NOT VERIFIED | No runtime data |
| pgvector gallery match | NOT VERIFIED | Empty gallery |
| watchlist_hit | NOT IMPLEMENTED | — |
| live_search_hit | NOT IMPLEMENTED | — |
| API/frontend face features | NOT IMPLEMENTED | — |

## 4. C1F.2a Reinterpretation

C1F.2a concluded `PASS_FACE_OBSERVATION_CONFIG_READY`. This means:

- All pipeline elements (AdaFace, face_reid_gate, face_observation_exporter)
  are present in module.yml
- Model assets (AdaFace ONNX + engine) exist on disk
- The landmark contract between YOLOv8-Face and AdaFace AlignFace is consistent
- face_reid_gate checks are correctly specified (7 rules + throttle)
- face_observation_exporter writes to Redis stream (code exists)
- Image bytes are forbidden in the exporter
- Export is disabled by default in compose

C1F.2a does **NOT** mean:

- AdaFace has been run on real face crops
- AdaFace produces valid 512-d embeddings
- face_reid_gate has passed on real embeddings
- face_observation_exporter has written to Redis
- Redis security.face_observations contains data
- face-worker has processed observations
- PostgreSQL face_observations table has rows

**C1F.2a is a config audit. It does NOT mean runtime face observation is validated.**

## 5. Corrected Phase Plan

| Phase | Scope | Status |
|-------|-------|--------|
| C1F.1 | Same-frame dual primary detection | DONE |
| C1F.2a | Face observation pipeline config audit | DONE |
| C1F.2b | Runtime scope correction | DONE (this document) |
| C1F.2c | AdaFace preprocessing + model runtime enablement | NOT STARTED |
| C1F.2d | face_reid_gate implementation / validation | NOT STARTED |
| C1F.2e | face_observation_exporter implementation / Redis contract | NOT STARTED |
| C1F.2f | Real RTSP face_observation Redis smoke | NOT STARTED |

## 6. What C1F.2c Must Prove

Before claiming AdaFace runtime ready, C1F.2c must:

1. Run AdaFace on real face crops from the RTSP stream
2. Verify the output is a 512-d vector
3. Verify the embedding norm is in [0.9, 1.1] (L2-normalized)
4. Verify no NaN/Inf in the embedding
5. Verify AlignFace preprocessing uses the 5 landmarks correctly

## 7. What C1F.2e Must Prove

Before claiming face observation export ready, C1F.2e must:

1. Enable `FACE_OBSERVATION_EXPORT_ENABLED=1` in compose
2. Verify Redis stream `security.face_observations` receives entries
3. Verify each entry contains: source_observation_id, camera_id, track_id,
   timestamp_ms, embedding (512-d), quality score
4. Verify no image bytes in any entry
5. Verify the per-track throttle works (not flooding Redis)

## 8. Boundaries Preserved

| Boundary | Status |
|----------|--------|
| No second RTSP | ✅ |
| No source extraction | ✅ |
| No production annotated_clip | ✅ |
| No image bytes in Redis | ✅ |
| No watchlist_hit / live_search_hit | ✅ |
| YOLOv8-Face is full-frame primary (not person ROI secondary) | ✅ |
