# Phase F2.2 — Face ReID Gate and Throttle

## Purpose

Add a production-safe quality gate and per-track throttle before any
face embedding is allowed downstream (Redis / pgvector / face-worker).

Only embeddings that pass quality checks and are not throttled will be
eligible for storage in later phases.

## Gate Rules (MVP)

Evaluated in order; first failure wins:

| # | Rule | Skip reason | Default threshold |
|---|---|---|---|
| 1 | person_track_id > 0 required | `no_track_id` | — |
| 2 | Face confidence >= threshold | `low_confidence` | 0.6 |
| 3 | Face bbox min width/height >= threshold | `face_too_small` | 40px |
| 4 | Landmarks = 5 points (10 or 15 floats) | `no_landmarks` / `bad_landmarks` | — |
| 5 | Feature dim = 512 | `wrong_feature_dim_N` | 512 |
| 6 | Embedding L2 norm in [1.0 - tol, 1.0 + tol] | `bad_norm_X.XXX` | ±0.10 |
| 7 | No NaN feature values | `nan_feature` | — |

Quality score is a composite in [0, 1]:

```text
quality = confidence * 0.6 + size * 0.4
```

## Throttle Policy

Per camera_id + person_track_id, minimum interval between allowed
observations:

```text
FACE_REID_MIN_INTERVAL_MS = 1000 (default)
```

Key format: `{camera_id}:{source_id}:{person_track_id}`

Camera_id is resolved via `CameraConfigBundle.get_by_source_id(source_id)`,
falling back to source_id when no mapping exists.

Throttle is evaluated only after all quality gates pass.

## Configuration

| Env var | Default | Description |
|---|---|---|
| `FACE_REID_MIN_CONFIDENCE` | 0.6 | Minimum face detection confidence |
| `FACE_REID_MIN_FACE_SIZE` | 40.0 | Minimum face bbox width/height (px) |
| `FACE_REID_MIN_INTERVAL_MS` | 1000 | Per-track throttle interval (ms) |
| `FACE_REID_NORM_TOLERANCE` | 0.10 | L2 norm tolerance (± from 1.0) |

## Pipeline Order

```text
YOLO26-pose -> nvtracker -> BehaviorRulesPyFunc
  -> YOLOv8-Face -> FacePersonAssociatorPyFunc
    -> AdaFace -> FaceReidGatePyFunc
      -> FaceEmbeddingDebugPyFunc -> FaceDebugPyFunc
```

## Metadata Attached to Face Objects

| Namespace | Name | Type | Description |
|---|---|---|---|
| `face_reid_gate` | `reid_allowed` | bool | Gate verdict |
| `face_reid_gate` | `reid_quality_score` | float | Quality score [0, 1] |
| `face_reid_gate` | `reid_skip_reason` | str | Skip reason or "ok" |
| `face_reid_gate` | `reid_throttle_key` | str | Throttle key |

## Unit Test Results

`harness/tests/test_face_reid_gate.py` — all tests pass:

- Valid face + track + landmarks + feature -> allowed
- Missing person_track_id -> rejected
- Low confidence -> rejected
- Too small face -> rejected
- Missing / malformed landmarks -> rejected
- Wrong feature dim -> rejected
- Embedding norm out of tolerance -> rejected
- NaN feature -> rejected
- Throttle first observation allowed
- Throttle repeated observation blocked
- Throttle after interval allowed
- Separate track_id / camera not blocked
- Quality score range 0..1

## Static Smoke Result

All checks pass.

## GPU Runtime Smoke Result

**PASS** — 2026-05-26

- `[face_reid_gate]` logs appear
- Allowed faces carry person_track_id, feature_dim=512, norm~1.0
- Throttle skips observed on repeated frames
- Pipeline stable

## Log Evidence

```text
[face_reid_gate] frame=1 source=c1_2_test faces=3 allowed=3 skipped=0
  face[0] person_track_id=107 allowed=true score=0.83 feature_dim=512 norm=1.000 landmarks=10
  face[1] person_track_id=120 allowed=true score=0.83 feature_dim=512 norm=1.000 landmarks=10
  face[2] person_track_id=34 allowed=true score=0.83 feature_dim=512 norm=1.000 landmarks=10
```

## Known Limitations

| Limitation | Impact |
|---|---|
| No blur/brightness quality | Low-quality sharp faces still pass |
| No face angle check | Profile faces may pass |
| Throttle in-memory only | Resets on container restart |
| No per-camera config | All cameras share thresholds |

## Files Changed

- `modules/savant_security/module.yml` — added FaceReidGatePyFunc
- `modules/savant_security/custom/services/face_reid_gate.py` — new
- `modules/savant_security/custom/pyfuncs/face_reid_gate.py` — new
- `modules/savant_security/custom/pyfuncs/face_embedding_debug.py` — extended
- `harness/tests/test_face_reid_gate.py` — new
- `scripts/smoke/check_f2_2_face_reid_gate_runtime.sh` — new
- `docs/phase_f2_2_face_reid_gate.md` — new
- `docs/phase_f2_1_adaface_runtime.md` — updated
- `docs/model_assets_manifest.md` — updated
- `specs/08_performance_policy.md` — updated
- `specs/04_face_intelligence.md` — updated

## Next Phase Recommendation

**F3** — Face observation pipeline:
- Emit allowed embeddings to Redis `security.face_observations`.
- face-worker consumes from Redis, stores in PostgreSQL + pgvector.
- pgvector cosine similarity search.
- watchlist_hit / live_search_hit events.

## F2.3 Downstream Consumer

**F2.3** (completed 2026-05-27) consumes `reid_allowed=true` faces from this
gate and exports them to Redis Stream `security.face_observations`. The
`FaceObservationExporterPyFunc` sits immediately after `FaceReidGatePyFunc`
in the pipeline. See `docs/phase_f2_3_face_observation_redis.md`.

**F2.3b** (2026-05-27) — exporter now treats gate verdict as **required**:
faces without `reid_allowed` metadata are skipped (`missing_gate_verdict`),
and the exporter has its own defensive `ExportThrottleMap` using millisecond
timestamps. This guards against PTS-unit mismatches in the gate throttle.

## F2.4 Contract Hardening (2026-05-27)

- **Throttle key** changed from 2-part `{source_id}:{track_id}` to 3-part
  `{camera_id}:{source_id}:{person_track_id}`.
- **Camera ID** resolved via `CameraConfigBundle.get_by_source_id()` instead
  of defaulting to source_id.
- **Timestamp** now uses `normalize_pts_to_ms()` instead of raw
  `frame_meta.pts`.  The gate and exporter share the same helper so
  throttle decisions are consistent.
- **ReIDGateInput** gained `source_id` field distinct from `camera_id`.
