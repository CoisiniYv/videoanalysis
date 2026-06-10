# Phase F1.0 — Face Detector Strategy Review

Date: 2026-05-26
Status: **research only** — no runtime change, no model download, no
TensorRT engine build, no `module.yml` change, no Savant start, no
GPU access. Output is this document plus a recommendation; any code
work follows in a subsequent F1.x sub-phase.

Pause-context: F1.0 originally proposed a direct SCRFD-only runtime
integration. Before committing to that, the user asked to step back
and compare the SCRFD route against Savant's own face sample family
(YOLOv8-Face / YOLOv5-Face). This document is the audit + decision.

---

## 0. User final decision (2026-05-26) — supersedes parts of this doc

After this review the user chose a sharper variant of Option A than
the recommendation in §6. The final architecture is recorded
authoritatively in
[`docs/phase_f1_1a_in_pipeline_face_architecture_lock.md`](phase_f1_1a_in_pipeline_face_architecture_lock.md).
Key locked decisions:

| Decision | Value |
|---|---|
| F1 detector | **YOLOv8-Face, full-frame primary nvinfer** (NOT secondary on per-person ROI) |
| F2 embedder | **AdaFace inside the Savant module** (NOT in a downstream face-worker, NOT in a Triton sidecar) |
| Redis boundary | **After AdaFace embedding** — `security.face_observations` carries metadata + 512-d embedding only, never frames or crops |
| face-worker scope | **CPU only**: consume observations → PG insert → pgvector search → emit watchlist_hit / live_search_hit. No GPU, no model files. |
| Vector store | **PostgreSQL + pgvector**, locked for the MVP. **HNSWLIB rejected. Qdrant rejected.** |
| `FaceRoiSelectorPyFunc` role | **Re-purposed to face↔person association** (IoU on head ROI), no cropping, no inference trigger. Rename to `FacePersonAssociatorPyFunc` deferred to F1.2+. |
| SCRFD_2.5G | **Future detector candidate**, not first-version target |
| ArcFace | **Future embedding alternative**, not first-version target |
| Module chaining via ZMQ | **Future optimization** if a single Savant module proves too heavy. NOT the first implementation target. |

The following text in this document predates that decision and is
**superseded** to the extent it implied otherwise — most notably:

- §9 "If we *do* switch to YOLOv8-Face — F1.1 / F1.2 / F1.3 split"
  proposed embedding in face-worker. **That is wrong now.** F1.1a
  moves embedding into Savant; face-worker is CPU only.
- §6 recommended YOLOv8-Face "with SCRFD held as a deliberate F1.4
  swap option". The recommendation stands; the SCRFD swap remains a
  future option, but the F1.4 numbering is informal.
- §8 lists SCRFD-phase breakdown (F1.1a/b, F1.2, F1.3). That route
  is not the chosen path; it stays only as a contingency description
  in case SCRFD is later revisited.

### F1 / F2 / F3 phase boundaries (locked by F1.1a)

| Phase | Scope |
|---|---|
| F1.1 | YOLOv8-Face full-frame primary + face↔person association + face quality; no embedding yet |
| F1.2 | Association refinement, per-track AdaFace throttle verification, optional PyFunc rename |
| F2   | AdaFace preprocessing + inference inside Savant; embedding written to `security.face_observations` |
| F3   | face-worker (CPU): consume observations → PostgreSQL insert → pgvector → watchlist_hit / live_search_hit |

For all binding details — pipeline diagram, Redis-boundary rule,
NVDEC TODO list, and the explicit Superseded-Plans section — read
`docs/phase_f1_1a_in_pipeline_face_architecture_lock.md`. **That is
the authoritative doc going forward.** This file is preserved as the
*reasoning trail* that led to the decision.

---

## 1. Current face-related assets in the repo

Audited via grep over `services/api`, `modules/savant_security`,
`scripts`, `harness/tests`, and `docs`. All matches found:

| Path | Phase | Detector-coupled? | State |
|---|---|---|---|
| `modules/savant_security/custom/converters/scrfd.py` | F0 | SCRFD-named, but utility functions inside (NMS, confidence filter, ROI→frame mapping, generic decode) are **detector-agnostic** | `ScrfdConverter.__init__` raises `NotImplementedError`; `decode_scrfd_outputs` raises `NotImplementedError`; utility functions are usable as-is |
| `modules/savant_security/custom/services/face_roi.py` | F0 | **No** — operates on YOLO26-pose keypoints + person bbox | Implemented + tested |
| `modules/savant_security/custom/services/face_quality.py` | F0 | **No** — operates on `bbox, confidence, landmarks` | Implemented + tested |
| `modules/savant_security/custom/services/face_config.py` | F0 | **No** | Implemented |
| `modules/savant_security/custom/models/faces.py` | F0 | **No** — `FaceDetection` already carries `model_name` and `model_version` | Implemented |
| `modules/savant_security/custom/models/face_events.py` | F0 | **No** — wire format independent of detector | Implemented |
| `harness/tests/test_scrfd_converter_utils.py` | F0 | The NMS / filter / mapping tests are generic; only the file's name pins SCRFD | All passing |
| `harness/tests/test_face_quality.py` / `test_face_roi_selector.py` / `test_face_observation_event.py` | F0 | **No** | All passing |
| `docs/phase_f0_face_detection_readiness.md` | F0 | Mentions SCRFD as future detector | Documentation only |
| `docs/model_assets_manifest.md` | F0 | Has SCRFD + ArcFace entries with TODO shapes / TODO source / TODO license | Documentation only — no model file in tree |
| `specs/04_face_intelligence.md` | spec | SCRFD_2.5G named throughout | Documentation only |

**No `yolo_v8face` / `yolov8-face` / `yolov5-face` converter exists in
the repo today.** SCRFD is named in the converter file and in the
specs, but the actual detector-decoding code is `NotImplementedError`.

### What this means

Everything *above* the detector (ROI selector, quality filter, event
draft, Redis wire format) and *below* the detector (FaceObservation,
gallery, watchlist, live-search) is detector-agnostic. The boundary is
narrow: only `custom/converters/scrfd.py` and the future PyFunc that
wires a face detector into `module.yml` actually depend on the choice
of model. Swapping detector means rewriting **one converter file** plus
**one model assets manifest entry**.

This is the right shape for a strategy decision: the cost of choosing
the wrong route is small and well-localised.

---

## 2. Does Savant support SCRFD out of the box?

**No.** Savant 0.6.x (the version pinned in
`infra/docker-compose.c1-official-adapter.yml` and `phase3h-zmq.yml`)
ships:

- A `nvinfer` element wrapper around DeepStream's primary/secondary
  inference operators.
- A base `BaseComplexModelOutputConverter` API for custom output
  decoders.
- A library of **converter helpers** (NMS, score sort, anchor decode
  helpers) under `savant.utils` and `savant.selector`.
- **Sample face modules** under the upstream `Savant/samples/`
  directory: `face_reid`, `peoplenet_detector`, and friends. The face
  samples that ship with reference converters are **YOLOv8-Face /
  YOLOv5-Face** family models. Their converters live in the sample
  module, not in `savant.converters` core, but they are
  Savant-published reference code we can copy from.

What Savant does NOT ship for SCRFD:

- No SCRFD-named converter, sample module, or pre-built engine.
- No documented input/output tensor schema for SCRFD_2.5G in Savant's
  docs.
- No pre-trained SCRFD ONNX file in any Savant artefact registry.

Using SCRFD with Savant therefore means: source the ONNX externally
(InsightFace), confirm the actual output tensor layout against the
specific ONNX export (which varies between InsightFace official, the
community insightface-Paddle export, and ad-hoc ONNX re-exports),
write a custom `BaseComplexModelOutputConverter` that does multi-scale
anchor-box decoding, then build the TRT engine.

## 3. What does Savant's official face sample use?

YOLOv8-Face (and earlier YOLOv5-Face) is what Savant's first-party
face samples build against. The same single-stage detector family
also returns a 5-point landmark head per box, which is the input
ArcFace wants. The Savant sample includes:

- A nvinfer config block that maps directly onto the ONNX export.
- An output converter that decodes per-box `(x1, y1, x2, y2, conf,
  landmark_0_x, landmark_0_y, ..., landmark_4_x, landmark_4_y)`
  rows — no multi-FPN-scale anchor reconstruction required.
- A worked example of running it as a secondary model on person ROIs
  (which matches our `face_roi.py` design).

This is "open the sample, swap the model path and the camera input"
level of effort, not "design a new converter" level.

## 4. YOLOv8-Face — engineering advantages

1. **Reference converter exists.** Savant publishes a sample
   converter; we copy + adapt, rather than design from scratch.
2. **ONNX export shapes are well-documented** by the upstream
   `derronqi/yolov8-face` project. Output is a single `(N, 21)` tensor
   per box: `[x, y, w, h, conf, kp1_x, kp1_y, ..., kp5_x, kp5_y]`.
   No FPN-scale stride decoding mystery.
3. **TensorRT build path is identical to YOLO26-pose.** Same nvinfer
   options, same precision/fp16 toggle, same engine cache directory
   conventions. Operations team only has one inference profile to
   manage instead of two.
4. **Landmarks come in the same forward pass.** No separate landmark
   model, no second nvinfer hop.
5. **License is straightforward.** Most YOLOv8-Face forks ship under
   Apache-2.0 / GPL-3.0; the most common commercial-friendly variant
   is derronqi's Apache-2.0 release.
6. **Failure mode is well-understood.** YOLO family detectors are the
   most-deployed face detectors in Savant production lore, and
   InsightPlatform docs have the engine-build steps for this exact
   family.

What YOLOv8-Face is **not** best at: very small faces in crowded
scenes. SCRFD has a measurable edge in that regime (see Section 5).

## 5. SCRFD_2.5G — algorithm advantages and engineering risks

### 5.1 Algorithm advantages

1. **State-of-the-art accuracy at 640×640 input** among lightweight
   face detectors on WIDER FACE (per the original 2021 InsightFace
   paper). Better small-face recall than YOLO-family face detectors
   at the same compute budget.
2. **2.5 GFLOPs is genuinely tiny.** Throughput headroom on a T4 is
   higher than YOLOv8-Face-n by a clear margin, leaving more budget
   for the rest of the pipeline.
3. **Landmark head designed for ArcFace alignment.** SCRFD's 5-point
   landmarks are explicitly trained for downstream face-recognition
   alignment — the original SCRFD paper is from the InsightFace
   group that also ships ArcFace.

### 5.2 Engineering risks

1. **No upstream Savant converter.** We write and maintain
   `BaseComplexModelOutputConverter` ourselves, including decoding
   multi-FPN-scale outputs at strides {8, 16, 32}.
2. **Output tensor shape ambiguity.** SCRFD ONNX is exported by
   several projects in subtly different ways. `docs/model_assets_manifest.md`
   already flags "Input shape: TODO" and "Output shape: TODO". Until
   we have the exact ONNX in hand and probe it with `onnx.shape_inference`,
   we cannot finish the converter. F0's `decode_scrfd_outputs` raises
   `NotImplementedError` for exactly this reason.
3. **License gating.** The InsightFace research code is Apache-2.0,
   but the **pretrained SCRFD weights** are released under a
   "research/non-commercial" license unless InsightFace gives written
   consent. For a security product this is a procurement risk that
   must be resolved before any production deployment. (YOLOv8-Face
   forks do not have this constraint.)
4. **TensorRT build complexity.** Anchor-based multi-scale outputs
   are TRT-supported but the engine-build path needs careful
   verification (per-output dtype, dynamic shapes, NMS plugin
   choice). YOLOv8-Face's single-output engine build path is shorter.
5. **Lower familiarity in our pipeline.** YOLO-family is already
   running for pose; SCRFD would be the first non-YOLO model in the
   stack, with its own debugging idioms.

## 6. Recommendation: route choice for this project

**Recommended path: YOLOv8-Face first (F1.1), with SCRFD held as a
deliberate F1.4 swap option.**

Reasoning:

- F0 produced detector-agnostic data models, ROI selector, quality
  filter, and event draft. The risk on the *non-detector* parts of
  the pipeline is therefore independent of the detector choice.
- The biggest unknown right now is "does the full face chain even
  run end-to-end under our C1.2 official-adapter runtime, with the
  ROI selector picking the right region, with the quality filter
  passing real faces, with the event landing in
  `security.face_observations`, and with the snapshot/clip evidence
  pipeline still producing artifacts for face events". This is a
  *runtime integration* question. It does not depend on which
  detector we pick — only that the detector reliably produces
  `(bbox, conf, landmarks)`.
- YOLOv8-Face minimises the cost of answering that runtime question.
  Reference converter exists, TRT path is the same as YOLO26-pose,
  license is unambiguous. Risk of unrelated noise (wrong tensor
  shape, wrong stride decoding, license re-procurement) is
  effectively zero.
- Once the chain is verified end-to-end with YOLOv8-Face we have a
  baseline. If we then need better small-face recall in dense
  scenes, swap to SCRFD as a one-converter, one-engine change. F0's
  `FaceDetection.model_name` / `model_version` fields are designed
  precisely so this swap leaves no trace in downstream consumers
  (gallery, watchlist, live-search, evidence-worker).

This is **explicitly not** an abandonment of SCRFD. It is sequencing:
prove the chain with the lowest-risk detector, then upgrade if and
when small-face accuracy is the actual production gap.

## 7. If we go with YOLOv8-Face — specs to update

The following docs reference SCRFD as the assumed first-stage face
detector and would need a corresponding update at the *start* of F1.1
(not in F1.0):

| File | Change |
|---|---|
| `specs/04_face_intelligence.md` | Replace the SCRFD_2.5G block with YOLOv8-Face. Keep ArcFace, gallery, watchlist, and live-search sections unchanged. Add a short "future SCRFD upgrade" subsection. |
| `docs/model_assets_manifest.md` | Add YOLOv8-Face entry (path, input/output shape, source, license). SCRFD entry can stay as "future upgrade candidate". |
| `docs/phase_f0_face_detection_readiness.md` | Note that F1 will integrate YOLOv8-Face first; SCRFD converter skeleton remains as future-upgrade scaffolding. |
| `modules/savant_security/custom/converters/scrfd.py` | Stays. Filename misleads but the generic utility functions inside (NMS / confidence filter / ROI→frame mapping) are detector-agnostic and reused by the YOLOv8-Face converter. Rename can wait until SCRFD lands or is dropped. |

No spec / doc above carries SCRFD as a hard architectural constraint
— every mention is "SCRFD is the chosen detector" rather than "the
architecture requires SCRFD specifically". The data model is generic.

## 8. If we *do* continue with SCRFD — F1.1 / F1.2 / F1.3 split

If a later review reverses the recommendation in Section 6, the
correct SCRFD breakdown is:

- **F1.1a — model file confirmation (no runtime).** User places
  `scrfd_2.5g.onnx` at the manifest path. Run `onnx.shape_inference`
  + `Netron` capture; update `docs/model_assets_manifest.md` with the
  exact input shape, output names, output shapes per FPN scale, and
  the source/license confirmation. Output: a one-page artefact in
  `docs/`. No code yet.
- **F1.1b — pure-Python decoder.** Implement
  `decode_scrfd_outputs(*output_layers)` against the confirmed shapes.
  Cover multi-stride anchor decoding with unit tests using stubbed
  output tensors that match the real ONNX shape and a small set of
  golden ground-truth detections derived offline. Still no Savant
  runtime.
- **F1.2 — `ScrfdConverter` plus `module.yml` wiring.** Subclass
  `BaseComplexModelOutputConverter`. Wire as a secondary model on
  person ROIs in a new `savant_security/module.yml` block. Build
  TensorRT engine, smoke test end-to-end.
- **F1.3 — `FaceRoiSelectorPyFunc`, quality filter, event emission.**
  Plug the ROI selector and quality filter from F0 into the live
  pipeline. Emit `FaceObservationEventDraft` to Redis Stream
  `security.face_observations`. Verify against the C1.2 evidence
  chain (no regression to intrusion / snapshot / clip).

Per-phase tests, harness coverage, and smoke scripts mirror the C1.x
cadence.

## 9. If we *do* switch to YOLOv8-Face — F1.1 / F1.2 / F1.3 split

The recommended sequencing if Section 6 is approved:

- **F1.1 — YOLOv8-Face converter + module.yml wiring.** Adapt
  Savant's reference YOLOv8-Face converter into
  `modules/savant_security/custom/converters/yolo_v8face.py`. Add the
  nvinfer block to `modules/savant_security/module.yml` as a
  secondary model on person ROIs. Reuse `face_roi.py` /
  `face_quality.py` from F0 unchanged. Reuse the generic NMS / filter
  utilities from `custom/converters/scrfd.py`.
- **F1.2 — `FaceRoiSelectorPyFunc` + face observation emission.**
  Wrap the ROI selector and quality filter as a Savant PyFunc, emit
  `FaceObservationEventDraft` to `security.face_observations`. Smoke
  test end-to-end against `infra/docker-compose.c1-official-adapter.yml`.
- **F1.3 — face-worker (Redis → PostgreSQL + pgvector).** Persist
  face observations. No ArcFace yet — that becomes F2.
- **F1.4 (optional) — SCRFD swap.** Reuse the F1.1 wiring; add a
  parallel `scrfd.py` converter and an alternate nvinfer config.
  Switch at the model-path level when the operator wants better
  small-face recall.

This route reaches "face events flowing into Redis + DB" in F1.3
versus F1.2 on the SCRFD path — one extra step for SCRFD comes from
the model-file-confirmation phase that has no equivalent for the
YOLOv8-Face flow.

## 10. Why F1.0 is a separate phase

This document does not propose any code change because:

- The choice between routes is high-leverage and irreversible only
  in terms of *which model file gets blessed for procurement*. The
  code is reversible: F0 made the data layer detector-agnostic on
  purpose.
- Spending an hour on this audit prevents committing to a
  multi-week SCRFD converter project on the basis of "specs say
  SCRFD" alone. The specs were written before the C1.x runtime was
  proven; the data models that came out of F0 are flexible enough
  to honor either choice.
- A single docs-only commit gives the team a written rationale to
  point at when the next session opens. Future Claude/operator
  sessions should treat this document as the authoritative answer
  to "why are we using detector X".

## 11. Decision needed from the user

Pick one:

| Option | Next phase | Spec update |
|---|---|---|
| **A** — YOLOv8-Face first, SCRFD held as future swap (recommended in §6) | F1.1 YOLOv8-Face converter + module.yml | Section 7 list |
| **B** — Continue with SCRFD_2.5G as originally planned | F1.1a SCRFD ONNX shape confirmation | None — keep current specs |
| **C** — Defer face detection entirely; do something else next | n/a | None |

This document does not pick for the user. It records what changes
once they pick.

## 12. Out of scope (this phase)

- No `module.yml` changes.
- No `infra/docker-compose.*.yml` changes.
- No Savant container start.
- No GPU usage.
- No model downloads.
- No `.onnx` / `.engine` / `.pt` / `.pth` files committed to git.
- No changes to `specs/04_face_intelligence.md` or
  `docs/model_assets_manifest.md` until Section 11 is answered.
- No deletion or renaming of `custom/converters/scrfd.py` regardless
  of route — the utility functions inside it remain useful.

## 13. Related documents

| File | Content |
|---|---|
| `docs/phase_f0_face_detection_readiness.md` | F0 readiness harness (data models, ROI, quality) |
| `docs/model_assets_manifest.md` | Model file expectations (SCRFD + ArcFace TODOs) |
| `specs/04_face_intelligence.md` | Original face spec (SCRFD-named) |
| `docs/phase_c1_2_official_adapter_runtime.md` | The runtime any face detector will integrate into |
| `docs/phase_c1_3_operator_camera_config_cli.md` | Operator CLI that face config will eventually extend |

---

*Written 2026-05-26. Phase F1.0 — strategy review, no code change.*
