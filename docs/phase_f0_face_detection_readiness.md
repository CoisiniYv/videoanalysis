# Phase F0 — Face Detection Readiness / Isolated Harness

## 1. F0 Target

F0 establishes the pure-Python engineering skeleton for the face intelligence
pipeline.  It defines data models, utility functions, configuration, and the
event wire format **without** integrating into the live Savant runtime.

F0 is NOT F1 (runtime integration).  F0:
- Does NOT download models.
- Does NOT access the network.
- Does NOT run GPU inference.
- Does NOT modify `module.yml`.
- Does NOT touch the C1.2 camera runtime / compose / evidence chain.

## 2. Face Intelligence Pipeline Overview

```
source adapter
  -> Savant module
  -> YOLO26-pose person detection
  -> nvtracker track_id
  -> head/person ROI selector (estimate_head_roi_from_person)
  -> SCRFD_2.5G face detection (future F1)
  -> face quality filter (evaluate_face_quality)
  -> ArcFace embedding (future F1+)
  -> FaceObservation event
  -> Redis Stream "security.face_observations"
  -> face-worker (future)
  -> PostgreSQL + pgvector / watchlist_hit / live_search_hit
```

## 3. Redis Stream Contract (hard constraint)

The Redis Stream MUST carry only **structured metadata**:

- `camera_id`, `source_id`, `track_id`, `timestamp_ms`
- `person_bbox`, `face_bbox`, `landmarks`
- `quality`, `embedding` (embedding in future F1+)
- `snapshot_path`, `crop_path` (optional filesystem path references)

The Redis Stream MUST NOT carry:
- Full frame bytes (JPEG / PNG / raw)
- Face crop bytes
- Any image data

If the system later needs image data for face-worker processing, a
separate offline-face-worker consuming from a different channel should be
created — it must NOT be the default real-time path.

## 4. Head ROI Selector

`custom/services/face_roi.py` — `estimate_head_roi_from_person()`

Strategy:
1. If nose / eyes / ears keypoints are present and above the confidence
   threshold (default 0.35), estimate the head ROI from the bounding box
   of those keypoints, expanded by `head_box_scale` (default 1.6x).
2. Otherwise, fall back to the top 45% of the person bbox.

A person whose bbox height is below `min_person_height` (default 80 px)
returns `None`.

The ROI is always clamped to the frame dimensions when provided.

## 5. Face Quality Filter

`custom/services/face_quality.py` — `evaluate_face_quality()`

Composite score: `confidence_score * 0.5 + size_score * 0.3 + landmark_score * 0.2`

Hard gates (must pass regardless of composite score):
- `face_confidence >= face_confidence_threshold` (default 0.6)
- `face_width >= min_face_width` (default 24)
- `face_height >= min_face_height` (default 24)

The composite quality score is an additional filter: `quality >= 0.65` to pass.

## 6. SCRFD Converter

`custom/converters/scrfd.py` — skeleton only.

The `ScrfdConverter` class is a placeholder that raises `NotImplementedError`.
Utility functions (`nms_boxes`, `filter_detections_by_confidence`,
`map_roi_coords_to_frame`) are fully implemented and testable.

`decode_scrfd_outputs()` also raises `NotImplementedError` — it requires
confirmed SCRFD_2.5G ONNX output tensor shapes.

## 7. FaceObservation Event Draft

`custom/models/face_events.py` — `FaceObservationEventDraft`

Wire format (schema version `1.0`) — see `to_dict()`.

`source_observation_id` format: `face:{source_id}:{track_id_or_no_track}:{timestamp_ms}`

When `track_id <= 0`, the track segment is replaced with `"no_track"`.

## 8. Model Files Required for F1

The following model files must be prepared by the user (NOT committed to git):

- `/data/video-analytics/models/scrfd_2.5g.onnx`
- `/data/video-analytics/models/arcface.onnx`

See `docs/model_assets_manifest.md` for details.

## 9. F1 Prerequisites

Before F1 can begin:
1. C1.2 (official adapter camera runtime control) must be merged to `master`.
2. `savant_security` module must be startable with the C1.2 runtime.
3. User must place `scrfd_2.5g.onnx` at the expected path.
4. SCRFD ONNX input/output tensor shapes must be confirmed.

## 10. F1 Plan

1. Implement `FaceRoiSelectorPyFunc` — wraps `estimate_head_roi_from_person`
   as a Savant PyFunc, emitting ROI metadata for SCRFD.
2. Wire SCRFD as a secondary model in `savant_security/module.yml` on person ROIs.
3. Complete `ScrfdConverter` with confirmed tensor shapes.
4. Output `FaceDetection` metadata from the converter.
5. Filter through `evaluate_face_quality()` and emit
   `FaceObservationEvent` to `security.face_observations` Redis Stream.

## 11. Relationship to C1.2

F0 does NOT touch:
- `infra/docker-compose.c1-official-adapter.yml`
- `scripts/runtime/camera_source_controller.py`
- `scripts/config/export_runtime_configs.py`
- `modules/savant_security/custom/services/rule_runtime.py`
- `modules/savant_security/module.yml`
- Any evidence / snapshot / clip / media logic

F1 must wait for C1.2 to be merged before modifying `module.yml`.
