# C1F.2c AdaFace Runtime Enablement

Date: 2026-06-01

Status: Pending smoke validation

## 1. Objective

Verify AdaFace produces valid 512-d embeddings with correct norm on real
face crops from the RTSP stream. This is the first runtime validation of
the face embedding pipeline beyond C1F.1's two-detector same-frame proof.

## 2. Relationship to C1F.2b

C1F.2b established that AdaFace is config-only (not runtime validated).
C1F.2c closes that gap for AdaFace specifically. It does NOT validate:

- face_reid_gate (C1F.2d)
- face_observation_exporter (C1F.2e)
- Redis security.face_observations (C1F.2f)
- face-worker / PostgreSQL / gallery / watchlist / live_search

## 3. Why Only AdaFace

The C1F.1 trimmed module (`module.c1f1_same_frame.yml`) proved dual
detector same-frame works but stopped at face-person association. Adding
AdaFace to the pipeline introduces:

- AlignFace preprocessing (landmark-dependent face alignment)
- AdaFace ONNX inference (embedding model)
- TensorToVectorConverter (output conversion)

Each of these can fail independently. Validating them in isolation (without
gate/exporter/Redis) isolates the failure domain.

## 4. Trimmed Module

`modules/savant_security/module.c1f2c_adaface_runtime.yml` contains:

```text
zeromq_source_bin
→ yolo26_pose (full-frame primary)
→ nvtracker
→ yolov8_face (full-frame primary)
→ face_person_associator (geometry-only)
→ adaface (nvinfer@attribute_model)
→ adaface_runtime_debug (pyfunc)
```

NOT included: behavior_rules, face_reid_gate, face_observation_exporter,
same_frame_detection_debug, face_embedding_debug, face_debug.

## 5. Debug PyFunc Output

`adaface_runtime_debug.py` writes per-face JSONL to:

```text
/data/video-analytics/artifacts/c1f2c/adaface_runtime_summary.jsonl
```

Each record:

```json
{
  "frame_num": 123,
  "source_id": "c1e_rtsp_replay",
  "has_landmarks": true,
  "has_embedding": true,
  "embedding_dim": 512,
  "embedding_norm": 1.000123,
  "valid_norm": true
}
```

## 6. Smoke Acceptance Criteria

| Metric | Minimum |
|--------|---------|
| faces_with_embedding | > 0 |
| faces_with_embedding_dim_512 | > 0 |
| faces_with_valid_norm | > 0 |

`valid_norm` = `0.8 <= norm <= 1.2`.

If source corruption occurs but valid embeddings are still produced:
`PASS_WITH_SOURCE_CORRUPTION`.

## 7. Not Validated

- face_reid_gate quality checks
- face_reid_gate per-track throttle
- face_observation_exporter Redis XADD
- Redis security.face_observations stream contents
- face-worker INSERT into PostgreSQL
- pgvector gallery match
- watchlist_hit / live_search_hit
- API / frontend
- production annotated_clip

## 8. Known Limitations

- Staged startup (source-adapter before savant-security) is a smoke
  stabilization tactic for Savant 0.6.0, not a production recommendation.
- Fixed RTSP source may produce H.264 reference errors; this does not
  invalidate AdaFace embedding results if valid embeddings are observed.
- Embedding norm check uses 0.8–1.2 tolerance; tighter bounds may be
  needed for production quality gates.
