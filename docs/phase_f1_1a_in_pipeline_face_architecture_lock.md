# Phase F1.1a — In-Pipeline YOLOv8-Face + AdaFace Architecture Lock

Date: 2026-05-26
Status: **architecture lock** — docs-only phase. Records the final
face-intelligence pipeline shape after the F1.0 review and supersedes
several alternatives discussed in earlier sessions.

This is the authoritative document for "where in the pipeline does
each face-related stage run". Subsequent face phases (F1.1b runtime
integration, F1.2 association refinement, F2 AdaFace embedding, F3
face-worker) MUST conform to this shape; any deviation requires a new
phase doc that explicitly supersedes this one.

---

## 1. Final data flow

```
RTSP / file source
  -> Source Adapter (gstreamer, controller-managed per C1.2)
    -> Savant module
        YOLO26-pose                       (primary, full-frame)
        nvtracker                         (track_id assignment)
        BehaviorRulesPyFunc               (intrusion / future rules)
        YOLOv8-Face                       (PRIMARY, FULL-FRAME — NOT secondary-on-ROI)
        FaceRoiSelectorPyFunc             (re-purposed: face↔person association)
        face_quality + per-track throttle (≤1 face/track/second)
        AdaFace preprocessing (5-pt landmark alignment, 112×112)
        AdaFace                           (in-pipeline embedding)
        redis_publisher_pyfunc            (emits AFTER embedding)
        |
        v
Redis  security.face_observations         (metadata + 512-d embedding only,
                                           NEVER frames or crops)
  -> face-worker (CPU, business logic only)
       insert face_observations
       pgvector gallery search
       pgvector live-search match
       emit watchlist_hit / live_search_hit
  -> Redis  security.events
  -> event-worker -> PostgreSQL events
  -> FastAPI / alarm screen
```

## 2. The Redis boundary rule

**Redis is the boundary between GPU inference and CPU business logic.**

Concretely:

- **Before Redis**: everything that needs the GPU, the model engines,
  or the raw frame tensor. YOLO26-pose, nvtracker, behavior rules,
  YOLOv8-Face, face-person association, face quality, AdaFace
  preprocessing, AdaFace inference.
- **After Redis**: everything that needs the database, the business
  state, or the gallery. Insert observations, run pgvector search,
  decide watchlist_hit / live_search_hit, derive events.

The Redis stream `security.face_observations` carries:

- camera_id, source_id, track_id, timestamp_ms,
- person_bbox, face_bbox, landmarks,
- quality, model_name, model_version,
- **embedding** (512-d float32, ≈ 2 KB),
- snapshot_path, crop_path (optional path references only — NOT bytes).

The stream MUST NOT carry:

- Full frame bytes (JPEG / PNG / RAW).
- Face crop bytes.
- Any image data.

## 3. Why AdaFace stays in the Savant pipeline

Three reasons, in order of weight:

1. **No JPEG roundtrip.** If AdaFace ran outside Savant, the pipeline
   would have to encode each candidate face crop to JPEG/PNG, push
   bytes through Redis, decode on the consumer, then run embedding.
   At 60 cameras × 5 fps × N faces/frame the JPEG encode alone is a
   meaningful CPU cost, and the decoded crop is bit-for-bit identical
   to the GPU-resident tensor we already had a moment earlier.
2. **GPU residency.** YOLOv8-Face output already lives in GPU memory.
   Feeding it into AdaFace as another in-pipeline TensorRT engine
   keeps it there. Crossing the GPU↔CPU boundary twice (out for
   encoding, in for re-inference) is exactly the bottleneck a
   batched-inference framework like Savant is designed to avoid.
3. **Operational coherence.** TensorRT engine cache, NVENC budget,
   GPU memory budget, model versioning — all already exist in the
   Savant module. Adding a parallel CPU/Python AdaFace deployment
   would duplicate every one of those concerns.

## 4. Why the Redis boundary is *after* embedding

If Redis received crops instead of embeddings, the system would need
**real-time GPU inference on the consumer side** to extract
embeddings before doing any business logic. That makes the face-worker
a second GPU service — which means:

- Two TensorRT engine inventories to keep in sync.
- Two GPU memory budgets to plan.
- Two retry / backpressure stories to design.
- Two model-version drift problems to monitor.

Putting embedding before Redis collapses this to one GPU service
(Savant module) and one CPU service (face-worker). face-worker becomes
plain Python + psycopg + pgvector, with no model files and no GPU
dependency. It can scale horizontally on cheap CPU nodes and survive
restarts without warming up engine caches.

## 5. Why face-worker is CPU-only

face-worker's only jobs are:

1. Consume `security.face_observations`.
2. Insert one row into `face_observations` (idempotent on
   `source_observation_id`).
3. Run pgvector cosine search against `person_gallery_embeddings`.
4. Run pgvector cosine search against active `live_search_jobs`.
5. Emit `watchlist_hit` / `live_search_hit` to `security.events` if
   above threshold.

Every one of these is I/O-bound on PostgreSQL or Redis. None requires
a GPU, a model file, or even NumPy beyond byte-level vector packing.
Keeping face-worker CPU-only means:

- It runs alongside event-worker on the same backend nodes.
- It scales horizontally with consumer groups, not with GPUs.
- A face-worker outage does NOT degrade detection or embedding — only
  delays watchlist/live-search hits. Detection events keep flowing.

## 6. Why no HNSWLIB or Qdrant in the MVP

PostgreSQL + pgvector is the only vector backend through F3:

- **HNSWLIB** is a single-process in-memory index. We would need our
  own persistence, replication, and snapshot story. pgvector already
  ships with PostgreSQL's WAL / replication / point-in-time recovery
  story, free.
- **Qdrant** is an external service. Adopting it means a second
  database in deployment, a second backup story, and a second cluster
  to operate. The marginal recall/latency win at our gallery size
  (target ≤ 10⁴ persons, ≤ 10⁶ observations) does not pay for the
  operational cost.

pgvector at HNSW index settings (`m=16, ef_construction=64`) gives
sub-100ms p95 search latency for these sizes. We pay no operational
tax for it.

Future swap path is preserved via a `FaceVectorStore` interface (specs
`04_face_intelligence.md` §12). If real production traffic later
demands Qdrant or a managed service, the contract is small enough to
re-implement without touching the Savant pipeline.

## 7. Why YOLOv8-Face is a full-frame primary detector

Earlier discussion proposed YOLOv8-Face as a *secondary* nvinfer that
runs only on person ROI crops produced by YOLO26-pose. **That route
is now rejected as the F1 target.** It would mean:

- A separate nvinfer pass per person object (variable batch size,
  worse GPU utilisation than a fixed full-frame primary).
- ROI crop bookkeeping in `FaceRoiSelectorPyFunc` (image-tensor slice
  ops, coordinate space mapping back to the full frame).
- Lost ability to detect faces that the person detector missed
  (occluded torso, side angle, partial frame entry).

Full-frame YOLOv8-Face removes all three issues:

- One nvinfer pass per frame, full batch, deterministic shape.
- No ROI math, no tensor slicing in PyFunc.
- Faces are detected on whatever pixels they occupy, independent of
  person detection success.

At our target FPS (5 fps per camera, 60 cameras) this fits well within
the T4 budget alongside YOLO26-pose. NVDEC capacity is the binding
constraint, not detector compute — see §10.

## 8. FaceRoiSelectorPyFunc → face-person associator

The F0 module `modules/savant_security/custom/services/face_roi.py`
selects a head ROI from a person bounding box and YOLO26-pose
keypoints. Under the old SCRFD route this ROI fed a secondary
detector. Under F1.1a it has a different job:

```
Inputs per frame:
    persons:   [(track_id, person_bbox, keypoints), ...]   from YOLO26-pose + nvtracker
    faces:     [(face_bbox, landmarks, confidence), ...]   from YOLOv8-Face

Outputs per frame:
    associated_faces: [(track_id_or_None, face_bbox, landmarks, ...), ...]
```

Association policy (to be finalised in F1.1b runtime work):

1. For each face, compute IoU with the head-ROI estimate (existing F0
   `estimate_head_roi_from_person`) of each tracked person.
2. Greedy match: each face takes the highest-IoU person above
   `min_iou`; ties broken by closer face centre to head-ROI centre.
3. A face that matches no person gets `track_id = None` and propagates
   downstream as an "unattributed face" (still embedded by AdaFace and
   stored, but without person-track correlation).

The PyFunc does NOT crop pixels and does NOT trigger inference. Pure
geometric reasoning over already-produced object metadata.

If naming becomes a source of confusion in F1.2 or later, we may
rename `FaceRoiSelectorPyFunc` → `FacePersonAssociatorPyFunc` in a
separate refactor phase. F1.1a does not rename anything — only
records the redefinition.

## 9. F1 / F2 / F3 phase boundaries

| Phase | Scope | Smoke acceptance |
|---|---|---|
| **F1.1** (runtime) | YOLOv8-Face full-frame primary, association, face quality, FaceObservationDraft *without* embedding | face_bbox + landmarks + quality + (optional) track_id appear in the Redis stream; intrusion chain unaffected |
| **F1.2** (refinement) | Association policy tuning, optional rename, per-track throttle verification | per-track AdaFace attempts ≤ 1/s under load |
| **F2** | AdaFace preprocessing, AdaFace TRT engine, embedding written to Redis | embedding (512-d) present in `security.face_observations` |
| **F3** | face-worker (CPU): consume observations → DB insert → pgvector → watchlist / live-search emission | watchlist_hit lands on `security.events`, alarm screen displays |

Each phase has independent acceptance and can be rolled back without
touching the others.

## 10. NVDEC capacity TODO (binding before F1.2 runtime smoke)

Before any GPU smoke beyond a single test source can be declared a
success, the operator MUST answer:

- **Encoding format**: are the 60 production cameras H.264 or H.265?
- **Source frame rate**: what does each camera actually emit
  (configurable 3–5 fps, locked 10 fps, locked 25/30 fps)?
- **Bitrate**: 1–4 Mbps typical?

Known hardware constraints (single T4):

| Encoding | 1080p30 cameras | Note |
|---|---|---|
| H.264 | ≈ 22 | NVDEC HW limit |
| H.265 | ≈ 44 | NVDEC HW limit |

Implications:

- 30 cameras/GPU at 1080p30 H.264 **exceeds** T4 NVDEC capacity.
- 30 cameras/GPU at 1080p30 H.265 is feasible.
- If cameras are H.264-only, options are: lower source fps at the
  camera (best), or switch encoding to H.265 (best long term), or
  add NVDEC headroom by adding a third GPU.

Decision dependencies for F1.2:

- If H.265 confirmed → no NVDEC mitigation needed.
- If H.264 only → source-side fps reduction is the optimal mitigation.
- **`nvstreammux` frame-drop is NOT a substitute.** It throttles
  downstream inference; the NVDEC decode path still pays full cost
  for every encoded frame received. Source-side fps reduction is the
  only way to reduce NVDEC pressure.

These items belong on the F1.2 / F1.3 entry checklist, not in F1.1a
code. F1.1a only records that they are required.

## 11. Superseded plans (do not re-introduce)

The following alternatives were discussed in prior sessions and are
**explicitly deprecated** as the first-version target. They are
preserved here for historical context only.

| Superseded plan | Why deprecated |
|---|---|
| Two-Savant-module split via ZMQ (Module 1: detection, Module 2: AdaFace) | Premature distribution; one Savant module fits the F1 / F2 footprint on a single T4. Re-evaluate only if profiling shows module-level GPU saturation. |
| AdaFace as a non-Savant Python/Triton service consuming Redis crops | Requires JPEG roundtrip and a second GPU service. Eliminated by §3 and §4. |
| Face crop bytes transported via Redis Streams | Violates the Redis boundary rule (§2). Image transport belongs on the filesystem (path references) or a media bus, not on the event stream. |
| HNSWLIB in-Savant index | Embedding belongs in PostgreSQL; in-process indexes lose the WAL/replication story. See §6. |
| Qdrant external vector DB | Extra service for no recall/latency win at MVP scale. See §6. |
| Per-person ROI crop feeding YOLOv8-Face as secondary nvinfer | Worse batch utilisation, ROI crop bookkeeping, misses person-detection failures. See §7. |
| Real-time AdaFace inference inside face-worker (CPU/Python service) | face-worker stays CPU-only; AdaFace runs on GPU before the Redis boundary. See §3 and §5. |

If a future phase wants to reverse any of these, it must:

1. Open a new phase doc that **explicitly supersedes F1.1a** by
   number.
2. State which specific limit of the current pipeline forces the
   reversal (profiling data, capacity ceiling, etc.).
3. Carry the spec edits in the same phase, not "fold them in later".

Silent reintroduction via spec changes alone is not acceptable.

## 12. What this phase did NOT do

- No `modules/savant_security/module.yml` change.
- No YOLOv8-Face converter added.
- No AdaFace converter added.
- No model download.
- No TensorRT engine build.
- No Savant container start.
- No GPU access.
- No compose runtime change.
- No face-worker implementation.
- No pgvector search code.
- No watchlist_hit / live_search_hit code.
- No `.onnx` / `.engine` / `.pt` / `.pth` / `.ckpt` committed.
- No `modules/savant_phase1d` stub cleanup (still out of scope).

Implementation lands in F1.1b and later.

## 13. Related documents (updated in this same commit)

| Document | Update |
|---|---|
| `docs/phase_f1_0_face_detector_strategy_review.md` | "User final decision" section appended; superseded text marked |
| `specs/01_architecture.md` | Face flow rewritten end to end; Redis boundary stated |
| `specs/02_savant_pipeline.md` | Pipeline elements list updated; AdaFace preprocessing notes; FaceRoiSelectorPyFunc redefinition; smoke acceptance bands; env-var rename plan |
| `specs/04_face_intelligence.md` | First-version detector/embedder/vector-store locked; SCRFD/ArcFace future |
| `specs/05_database_schema.md` | `embedding_model='adaface'` note; `source_observation_id` idempotency note |
| `specs/08_performance_policy.md` | Full-frame YOLOv8-Face throttle; per-track AdaFace ≥1s; anti-patterns; NVDEC TODO block |
| `docs/model_assets_manifest.md` | YOLOv8-Face (F1) and AdaFace (F2) entries; SCRFD/ArcFace as future candidates; engine policy |
| `docs/phase_f0_face_detection_readiness.md` | F0 reusable; detector swap noted; FaceRoiSelectorPyFunc redefinition |

---

*Written 2026-05-26. Phase F1.1a — architecture lock, docs only.*
