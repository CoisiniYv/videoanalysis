# Phase F1.3 — Face-Person Association

Date: 2026-05-26
Status: code complete, unit tests pass, static smoke pass, GPU runtime smoke **PASS**

## 1. What Changed

Added face-person association to link YOLOv8-Face face detections with
YOLO26-pose person tracks.  Each face object now carries a `person_track_id`,
`association_score`, and `association_method` as Savant attribute metadata.

### Files changed

| File | Change |
|------|--------|
| `modules/savant_security/custom/services/face_person_association.py` | New — pure Python association service |
| `modules/savant_security/custom/pyfuncs/face_person_associator.py` | New — Savant PyFunc adapter |
| `modules/savant_security/module.yml` | Inserted `face_person_associator` between yolov8_face and face_debug |
| `modules/savant_security/custom/pyfuncs/face_debug.py` | Updated — reads and displays association metadata |
| `harness/tests/test_face_person_association.py` | New — 22 unit tests |
| `scripts/smoke/check_f1_3_face_person_association_runtime.sh` | New — 18 static checks |
| `docs/phase_f1_3_face_person_association.md` | This doc |

## 2. Architecture

### Pipeline order (module.yml)

```text
yolo26_pose (nvinfer) -> nvtracker -> behavior_rules -> yolov8_face (nvinfer) -> face_person_associator (pyfunc) -> face_debug (pyfunc)
```

### Pure Python service

`face_person_association.py` contains all association logic with no Savant
or DeepStream imports.  Testable in isolation.

**Dataclasses:**
- `BBox(xc, yc, width, height)`
- `FaceInput(bbox, confidence, index)`
- `PersonInput(bbox, track_id, has_track_id, confidence, index)`
- `FacePersonAssociation(face_index, person_index, person_track_id, score, method, person_bbox)`
- `AssociationConfig(upper_body_weight, containment_weight, size_ratio_weight, base_score)`

**Core function:**
```python
associate_faces_to_persons(faces, persons, config) -> List[FacePersonAssociation]
```

**Algorithm:**
1. Face center must fall inside person bbox (hard requirement).
2. Score = base_score(0.2) + upper_body_bonus(0.3) + containment_bonus(0.3) + size_ratio_bonus(0.2).
3. Score all eligible (face, person) pairs, sort by score descending.
4. One-to-one greedy matching: each face assigned at most once, each person assigned at most once. Unassigned faces remain unassociated.
5. Track ID inheritance: assigned face inherits the matched person's track_id.

### Savant PyFunc adapter

`FacePersonAssociatorPyFunc` reads person and face objects from
`frame_meta.objects`, calls the pure Python service, and attaches
association metadata via `add_attr_meta`.

**Metadata written per face:**
- `face_person_associator.person_track_id` (int)
- `face_person_associator.association_score` (float)
- `face_person_associator.association_method` (str)

## 3. Unit Tests

22 tests in `harness/tests/test_face_person_association.py`:

| Class | Tests | Covers |
|-------|-------|--------|
| TestCenterInside | 3 | Face center inside/outside/edge of person bbox |
| TestFaceOutside | 2 | Face entirely outside, partially overlapping |
| TestMultiplePersons | 2 | Two persons, best match wins; one face two persons |
| TestUpperBodyPreference | 2 | Face near head vs feet; equal position different sizes |
| TestTrackIdInheritance | 2 | Track ID inherited when single match; none when ambiguous |
| TestMissingLandmarks | 1 | Face with missing landmarks still associates |
| TestMissingTrackId | 1 | Person without track_id (has_track_id=False) |
| TestEmptyInputs | 3 | No faces, no persons, both empty |
| TestEdgeCases | 2 | Tiny face, very large face rejected by min size |
| TestScoreRange | 1 | All scores in [0, 1] |
| TestLargeFaceReject | 1 | Face > person bbox rejected (center outside) |

```
22 passed in 0.02s
```

## 4. Static Smoke

18 checks in `scripts/smoke/check_f1_3_face_person_association_runtime.sh`:

| Category | Checks |
|----------|--------|
| module.yml structure | yolov8_face present, full-frame, batch=1 |
| Pipeline order | face_person_associator after yolov8_face, face_debug after face_person_associator |
| No forbidden elements | No AdaFace, no Redis face_observations, no HNSWLIB, no Qdrant |
| Source files | All 3 Python files exist |
| Compose | Valid, has savant-security |

```
18 passed, 0 failed
```

## 5. GPU Runtime Smoke

- **Date/Time**: 2026-05-26 14:03+ UTC+8
- **Compose**: `infra/docker-compose.c1-official-adapter.yml`
- **Input**: `testVideo/allface.mp4` via source adapter `c1_2_test`
- **Container**: `c1-official-savant` — stable, no crashes

### Evidence

| Check | Result |
|-------|--------|
| `[face_assoc]` logs appear | YES |
| persons > 0 in at least one frame | YES (persons=25) |
| faces > 0 in at least one frame | YES (faces=4) |
| associated > 0 in at least one frame | YES (associated=4) |
| person_track_id non-empty | YES (385, 391) |
| landmarks still readable | YES (landmarks=10pts) |
| association score range | 0.84–0.96 |
| association method | center_inside_upper_body |
| Container crash | NONE |

### Key Log Lines

**face_assoc summary:**
```
[face_assoc] frame=7531 source=c1_2_test persons=1 faces=1 associated=1
[face_assoc] frame=7981 source=c1_2_test persons=21 faces=4 associated=4
[face_assoc] frame=8041 source=c1_2_test persons=10 faces=2 associated=2
```

**face_assoc detail:**
```
face[0] person_track_id=391 score=0.96 face_bbox=(212,668,83,95) person_bbox=(267,837,197,463) landmarks=10pts method=center_inside_upper_body
face[1] person_track_id=385 score=0.95 face_bbox=(861,278,158,216) person_bbox=(842,602,527,950) landmarks=10pts method=center_inside_upper_body
```

**face_debug with association metadata:**
```
face[0] conf=0.708 bbox="RBBox { xc: 212.1, yc: 668.2, ... }" landmarks=10pts value=[192.49, 656.15, ...]... person_tid=391 assoc_score=0.96 method=center_inside_upper_body
face[1] conf=0.783 bbox="RBBox { xc: 860.8, yc: 278.3, ... }" landmarks=10pts value=[865.64, 251.52, ...]... person_tid=385 assoc_score=0.95 method=center_inside_upper_body
```

## 6. Known Limitations

| Limitation | Impact |
|------------|--------|
| Spatial-only association (no appearance model) | Faces may mismatch in crowded scenes with overlapping bboxes |
| Greedy one-to-one matching | Two faces in same person bbox: only higher-scoring one associates |
| No face quality filter | Low-quality faces still associate (quality gate is F1.4) |
| No per-track throttle | Every frame's face is evaluated (throttle is F1.4) |
| Static batch = 1 for face detector | Throughput limited by single-batch inference |

## 7. What This Phase Does NOT Do

- Does NOT write to Redis `security.face_observations`
- Does NOT invoke AdaFace
- Does NOT implement face-worker / pgvector / watchlist
- Does NOT affect intrusion events or behavior rules
- Does NOT add face quality filtering or per-track throttle

## 8. Next Phase

**F1.4** — Face quality filter + per-track throttle:
- Filter faces by blur/brightness/size quality score.
- Throttle face processing per person track (e.g., one embedding per N seconds).
- Reduce downstream AdaFace compute load.

**F2** — AdaFace in-pipeline embedding:
- Wire `nvinfer@attribute_model` on face objects.
- Face alignment preprocessing (GPU-side).
- L2-normalize AdaFace `feature` output.
- Emit `FaceObservationEvent` to Redis `security.face_observations`.

**F2.1 update (2026-05-26):** F2.1 now consumes associated face
objects from this phase. AdaFace `nvinfer@attribute_model` runs after
`FacePersonAssociatorPyFunc`, and `person_track_id` is preserved
through the embedding stage. `FaceEmbeddingDebugPyFunc` confirms
embedded faces carry the associated `person_track_id`.
