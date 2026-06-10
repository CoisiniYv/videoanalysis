# Phase F0 — Face Detection Readiness / Isolated Harness

> **Updated by Phase F1.1a (2026-05-26).** The F0 harness — ROI
> estimator, face quality filter, `FaceDetection` / `FaceObservationDraft`
> data models, event wire format — remains reusable. What changed is
> the detector + embedder that follow in F1 / F2:
>
> - **First-version detector is now YOLOv8-Face full-frame primary**,
>   not SCRFD secondary-on-ROI.
> - **First-version embedder is now AdaFace inside the Savant module**,
>   not ArcFace, not face-worker, not a Triton sidecar.
> - SCRFD_2.5G converter skeleton remains as future detector swap
>   scaffolding; the NMS / confidence-filter / ROI→frame mapping
>   utility functions inside it are detector-agnostic and reused.
> - ArcFace remains a future embedding alternative.
> - The Redis face_observations stream will carry the AdaFace
>   embedding after F2 lands; F0 reserved that field name correctly.
>
> See `docs/phase_f1_1a_in_pipeline_face_architecture_lock.md` for the
> authoritative architecture. This document is preserved as the F0
> readiness record.

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
  -> YOLOv8-Face full-frame primary face detection (F1)
  -> face-person association
       (FaceRoiSelectorPyFunc — role redefined per F1.1a;
        head-ROI / IoU geometric matching, NO image crop, NO inference)
  -> face quality filter (evaluate_face_quality)
  -> AdaFace 5-pt landmark alignment + 112x112 preprocess (F2)
  -> AdaFace embedding (in-pipeline, F2)
  -> FaceObservation (with embedding)
  -> Redis Stream "security.face_observations"
  -> face-worker (CPU only — F3)
  -> PostgreSQL + pgvector / watchlist_hit / live_search_hit
```

The old chain `head/person ROI -> SCRFD_2.5G -> ArcFace ->
face-worker embedding` is superseded by F1.1a. SCRFD_2.5G and ArcFace
become future swap candidates only.

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

## 4. Head ROI Selector → Face-Person Associator (F1.1a redefinition)

`custom/services/face_roi.py` — `estimate_head_roi_from_person()`

In F0 this function was designed to feed a SCRFD secondary detector
with a cropped head ROI. **F1.1a redefines its consumer**:
`FaceRoiSelectorPyFunc` no longer crops pixels and no longer triggers
inference. Instead it uses the head-ROI estimate to perform
**face↔person association** against the YOLOv8-Face primary
detector's output (IoU + greedy match). The function itself is
unchanged; only the surrounding pyfunc's purpose changed.

Strategy (unchanged from F0):
1. If nose / eyes / ears keypoints are present and above the confidence
   threshold (default 0.35), estimate the head ROI from the bounding box
   of those keypoints, expanded by `head_box_scale` (default 1.6x).
2. Otherwise, fall back to the top 45% of the person bbox.

A person whose bbox height is below `min_person_height` (default 80 px)
returns `None`.

The ROI is always clamped to the frame dimensions when provided.

The estimate is now used as a **target rectangle for IoU matching
against YOLOv8-Face's face bounding boxes**, not as a crop region for
secondary inference.

## 5. Face Quality Filter

`custom/services/face_quality.py` — `evaluate_face_quality()`

Composite score: `confidence_score * 0.5 + size_score * 0.3 + landmark_score * 0.2`

Hard gates (must pass regardless of composite score):
- `face_confidence >= face_confidence_threshold` (default 0.6)
- `face_width >= min_face_width` (default 24)
- `face_height >= min_face_height` (default 24)

The composite quality score is an additional filter: `quality >= 0.65` to pass.

## 6. SCRFD Converter — preserved as future swap scaffolding

`custom/converters/scrfd.py` — skeleton only.

The `ScrfdConverter` class is a placeholder that raises
`NotImplementedError`. Utility functions (`nms_boxes`,
`filter_detections_by_confidence`, `map_roi_coords_to_frame`) are
fully implemented and **detector-agnostic** — they are reused by the
F1.1b YOLOv8-Face converter and any future face detector.

`decode_scrfd_outputs()` also raises `NotImplementedError` — it
requires confirmed SCRFD_2.5G ONNX output tensor shapes.

**F1.1a leaves this file in place** as the future-swap scaffold for
SCRFD. Renaming or removing it would only churn the codebase — the
utility functions are useful today and the skeleton class costs
nothing to keep.

## 7. FaceObservation Event Draft

`custom/models/face_events.py` — `FaceObservationEventDraft`

Wire format (schema version `1.0`) — see `to_dict()`.

`source_observation_id` format: `face:{source_id}:{track_id_or_no_track}:{timestamp_ms}`

When `track_id <= 0`, the track segment is replaced with `"no_track"`.

## 8. Model Files Required for F1 / F2

The following model files must be prepared by the user (NOT committed
to git):

- `/data/video-analytics/models/yolov8_face.onnx`  (F1 — already prepared by user, shapes pending introspection)
- `/data/video-analytics/models/adaface.onnx`      (F2 — pending)

SCRFD / ArcFace ONNX paths are reserved as future-swap candidates.

See `docs/model_assets_manifest.md` for details.

## 9. F1 Prerequisites

Before F1 can begin:

1. C1.2 (official adapter camera runtime control) merged to `master` — DONE.
2. `savant_security` module is startable with the C1.2 runtime — DONE.
3. User places `yolov8_face.onnx` at the expected path.
4. YOLOv8-Face ONNX input/output tensor shapes confirmed (Netron /
   `onnx.shape_inference`); manifest updated.
5. NVDEC capacity TODOs from
   `specs/08_performance_policy.md` §10.2 answered for the target 60
   cameras (encoding format, source frame rate, bitrate).

## 10. F1 / F2 / F3 Plan (locked by F1.1a)

**F1.1b — YOLOv8-Face runtime integration:**

1. Implement `custom/converters/yolov8_face.py` using the F0 utility
   functions in `scrfd.py` (NMS, confidence filter, ROI→frame mapping).
2. Wire YOLOv8-Face as a **full-frame primary** `nvinfer@detector` in
   `modules/savant_security/module.yml` (not secondary on person ROI).
3. Redefine `FaceRoiSelectorPyFunc` to perform face↔person association
   instead of cropping.
4. Plumb face_quality through, but DO NOT yet emit observations to
   Redis (no embedding yet).

**F1.2 — association policy + throttle:**

1. Tune IoU thresholds for face↔person matching.
2. Verify per-track ≤ 1Hz throttle under load.
3. Optional rename `FaceRoiSelectorPyFunc` → `FacePersonAssociatorPyFunc`.

**F2 — AdaFace in-pipeline embedding:**

1. Confirm AdaFace ONNX shapes; update manifest.
2. Implement `custom/converters/adaface.py` (L2 normalization policy
   matches model export).
3. Wire AdaFace as `nvinfer@classifier` keyed on YOLOv8-Face's face
   object.
4. Implement `redis_publisher_pyfunc` writing `FaceObservation` with
   embedding to `security.face_observations`.

**F3 — face-worker (CPU):**

1. Consume `security.face_observations`.
2. Insert into PG `face_observations` (idempotent on
   `source_observation_id`).
3. pgvector cosine search against `person_gallery_embeddings` →
   `watchlist_hit`.
4. pgvector cosine search against active `live_search_jobs` →
   `live_search_hit`.
5. Emit hits to `security.events`.

## 11. Relationship to C1.2

F0 does NOT touch:
- `infra/docker-compose.c1-official-adapter.yml`
- `scripts/runtime/camera_source_controller.py`
- `scripts/config/export_runtime_configs.py`
- `modules/savant_security/custom/services/rule_runtime.py`
- `modules/savant_security/module.yml`
- Any evidence / snapshot / clip / media logic

F1 must wait for C1.2 to be merged before modifying `module.yml`.
