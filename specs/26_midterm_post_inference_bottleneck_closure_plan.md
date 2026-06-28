# 26_midterm_post_inference_bottleneck_closure_plan.md

Date: 2026-06-28

## 1. Purpose

This spec tracks closure of the 2026-06-28 post-inference bottleneck review.
It was first written while the 60-stream pressure run was still in progress;
the current checkout now also contains the pressure result document, so this
file is the remaining implementation and verification plan.

It covers the path after Savant has emitted Redis observations/events:

```text
Savant Redis exporters
  -> face-worker / event-worker
  -> PostgreSQL / pgvector / record request streams
  -> clip-worker / media-worker
  -> evidence bundles and 8090 evidence APIs
```

This is not a Savant model-chain tuning spec. It must be coordinated with:

- `specs/15_savant_performance_observability.md`
- `specs/21_replay_evidence_io_optimization_60_stream_production.md`
- `specs/22_midterm_60_stream_readiness_risk_closure_plan.md`
- `docs/midterm_post_inference_bottleneck_static_review_2026-06-28.md`
- `docs/midterm_downstream_evidence_performance_2026-06-28.md`

## 2. Current Finding And Checkout Status

The current static conclusion is:

```text
The main remaining post-inference risks are not Savant Redis XADD on the
inference hot path. They are downstream single-consumer, synchronous DB/vector
lookup, record-request idempotency, and evidence materialization costs.
```

Already mitigated:

- Savant Redis exporters use bounded async writers, so Redis jitter should not
  directly block `process_frame` under normal operation.
- Event/evidence queue query indexes have been added in migrations 018, 019,
  and 020.
- The downstream 60-stream evidence pressure result has been captured in
  `docs/midterm_downstream_evidence_performance_2026-06-28.md`.
- The current 60-stream 3 FPS downstream evidence run retained 50/50 playable
  bundles and ended with `XPENDING security.record_requests clip-workers-midterm`
  at 0.
- Admission/backpressure now keeps low-value events from overwhelming
  clip/media workers by using `materialization_skipped`; playable-but-missing
  annotation cases are surfaced as degraded instead of silent hard failure.

Still open:

- face-worker writes each face observation and then synchronously runs
  watchlist/gallery matching;
- gallery and face observation vector searches do not yet have ANN vector
  indexes;
- event-worker record request dedupe scans `security.record_requests` with
  `XRANGE - +`;
- media-worker materialization is still organized around one polling process and
  ffprobe/ffmpeg/decode finalization.
- downstream observability still does not fully expose gallery query p95,
  record-request dedupe latency, PostgreSQL hot query-plan deltas, and media
  lifecycle p95/p99 in one report schema.

## 3. Coordination Gate

For the current checkout, the original "wait for pressure test" gate is passed
by `docs/midterm_downstream_evidence_performance_2026-06-28.md`.

Future code changes under this spec must either cite that pressure result or
capture a newer equivalent result before changing shared runtime, compose,
FPS/batch settings, or rule enablement.

Required input for each implementation stage:

- pressure run ID and artifact path;
- exact git SHA or dirty diff summary;
- runtime epoch;
- enabled sources and source count;
- enabled rules and support matrix snapshot;
- FPS, interval, batch, and `MAX_PARALLEL_STREAMS` settings;
- forwarder, Savant, Redis, PostgreSQL, face-worker, event-worker, clip-worker,
  and media-worker metrics;
- final evidence counts: events, tasks, bundles, playable bundles, annotation
  complete/missing/degraded counts;
- cleanup behavior and retained sample IDs.

Exit token for this gate:

```text
PASS_POST_INFERENCE_SPEC0_PRESSURE_RESULT_CAPTURED
```

Current status:

- `PASS_POST_INFERENCE_SPEC0_PRESSURE_RESULT_CAPTURED` is satisfied for the
  2026-06-28 downstream evidence pressure result.
- This is not the final closure token. The current checkout still has open code
  work in Spec 1, Spec 2, Spec 3, and Spec 5.

## 4. Problem Breakdown

| Area | Problem | Why it matters |
| --- | --- | --- |
| face-worker | Single consumer does Postgres insert and synchronous watchlist/gallery pgvector search per face observation | Face observation rate and gallery size can linearly amplify latency after Redis |
| event-worker | `RecordRequestPublisher.has_request()` scans the full `security.record_requests` stream | Every recordable event pays O(N) Redis/JSON cost as stream length grows |
| media-worker | Materialization active count is a guard, not a finalizer worker pool | ffprobe/ffmpeg/decode work can dominate evidence lifecycle even if inference is healthy |
| annotation/evidence windows | Retention and admission are now partially sized, but still need lifecycle and memory-margin proof | Evidence can become playable but annotation-missing, or expire before materialization |
| observability | Pressure reports need more downstream split metrics to rank bottlenecks | Without split metrics, fixes may only move backlog between queues |

## 5. Execution Flow

### Spec 0 - Pressure Result Triage

Problem:

The static review identifies candidate bottlenecks, but does not prove which one
dominates the latest pressure run.

Modification direction:

- Create or update a pressure result analysis document under `docs/`.
- Extract one table that ranks candidate bottlenecks by measured evidence:
  face lag, event-worker CPU, record request stream length, media queue wait,
  ffmpeg CPU, annotation completeness, Redis exporter drops, and forwarder/Savant
  health.
- Record "not enough data" explicitly instead of inferring from missing metrics.

Target effect:

- Each code change after pressure testing has a measured reason.
- Future reviewers can tell whether the fix order came from live pressure data
  or from static risk.

Acceptance:

```text
PASS_POST_INFERENCE_SPEC0_PRESSURE_RESULT_CAPTURED
```

Current checkout status:

- Captured run: `pressure60_3fps_playabledrain_20260628T100531Z`.
- Artifact: `/data/video-analytics/artifacts/pressure60_3fps_playabledrain_20260628T100531Z`.
- Result: 60 sources active, forwarder send failures 0, 50/50 retained
  evidence playable, and 8090 evidence API could query retained bundles.
- Follow-up run `pressure60_16p1_20260628T112109Z` is evidence-chain pressure,
  not proof of 60 streams at 16 FPS inference throughput.
- Missing from the current report schema: face-worker gallery query p95,
  event-worker record-request dedupe latency, and PostgreSQL hot query-plan
  deltas.

### Spec 1 - Record Request Idempotency

Problem:

`event-worker` checks whether a record request already exists by scanning the
whole `security.record_requests` Redis stream. This is O(N) in stream length and
is paid by each recordable event.

Current code verification:

- `services/event-worker/app/worker.py` still calls
  `record_publisher.has_request(source_event_id, "savant_replay")` before
  publishing a record request.
- `services/event-worker/app/record_request.py` still implements
  `has_request()` with `xrange(self._stream, "-", "+")`.

Modification direction:

- Replace full-stream scan dedupe with an explicit idempotency key:
  `(source_event_id, strategy)`.
- Prefer one of:
  - a Redis key/set with TTL aligned to record request retention;
  - a PostgreSQL unique/idempotency table;
  - a unique constraint on the durable request representation if one exists.
- Keep duplicate replay/event behavior deterministic.
- Add unit tests for duplicate events, retry/reclaim, and cleanup/TTL behavior.

Target effect:

- Record request dedupe is O(1) or indexed O(log N), not proportional to stream
  length.
- Event-worker CPU does not grow with `security.record_requests` history.
- Existing evidence routing semantics do not change for `intrusion` and
  `watchlist_hit`.

Acceptance:

```text
PASS_POST_INFERENCE_SPEC1_RECORD_REQUEST_IDEMPOTENCY
```

Required verification:

- targeted tests for `RecordRequestPublisher` and event-worker policy;
- pressure or synthetic replay showing `has_request()` no longer performs
  `XRANGE - +`;
- no duplicate record request for the same `(source_event_id, strategy)`;
- retained pressure evidence remains queryable in 8090.

### Spec 2 - Face Worker Vector Search And Matching Scale

Problem:

`face-worker` handles each face observation by inserting it into PostgreSQL and
then synchronously running watchlist/gallery matching. The gallery and
observation vector tables currently lack ANN vector indexes.

Current code verification:

- `services/face-worker/app/worker.py` still processes each message in a single
  loop, inserts the observation, then calls
  `watchlist_emitter.emit_for_observation()` synchronously for newly inserted
  rows.
- `services/face-worker/app/vector_store.py` still performs exact pgvector
  searches with `ORDER BY embedding <=> %(query_embedding)s`.
- `db/migrations/005_face_observations.sql` and
  `db/migrations/006_gallery_schema.sql` still define btree/person-active
  indexes, not ANN vector indexes.

Modification direction:

- Add measured query plans first:
  - gallery search against `person_gallery_embeddings`;
  - observation search against `face_observations` when used by search flows.
- Add pgvector ANN indexes where the measured plan proves exact scan cost is a
  bottleneck.
- Keep threshold semantics explicit. If ANN recall is introduced, document
  whether exact rerank is required before emitting a watchlist hit.
- Only after index/query-plan closure, consider:
  - batching inserts;
  - decoupling observation persistence from matching;
  - increasing consumer count;
  - moving matching into a separate queue.

Target effect:

- Gallery/person count growth does not linearly degrade watchlist latency.
- `security.face_observations` pending remains bounded under the chosen pressure
  profile.
- Watchlist hit correctness and duplicate suppression remain stable.

Acceptance:

```text
PASS_POST_INFERENCE_SPEC2_FACE_WORKER_SCALE
```

Required verification:

- migration/static tests for vector indexes;
- `EXPLAIN ANALYZE` before/after for representative gallery sizes;
- unit/integration tests for watchlist threshold and target-person filtering;
- pressure evidence showing face-worker lag and gallery query p95 are bounded.

### Spec 3 - Media Worker Finalizer Throughput

Problem:

media-worker materialization is still effectively a single polling/finalization
path. `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE` is a guard, not proof of an
actual finalizer worker pool.

Current code verification:

- `services/media-worker/app/worker.py` still creates one
  `_MaterializationGuard(cfg.materialization_max_active)` inside one process.
- The main loop still calls `_process_sink_output(...)` sequentially from a
  single `while not shutdown_requested` loop.
- Current pressure results show the downstream evidence chain can retain 50
  playable bundles under the selected profile, but they do not prove a real
  finalizer worker pool or DB-backed multi-worker claiming model.

Modification direction:

- Decide the concurrency model explicitly:
  - multiple media-worker containers with DB-backed task claiming; or
  - one process with an internal finalizer worker pool; or
  - a separate finalizer service.
- Split the lifecycle into idempotent stages:
  - task selection/claim;
  - sink output/proof availability;
  - ffprobe/ffmpeg finalization;
  - decode/playability validation;
  - DB state update and cleanup.
- Preserve admission/backpressure before raising concurrency.
- Keep disk quotas and cleanup as hard guardrails.

Target effect:

- Evidence queue wait and playable bundle p95/p99 improve without unbounded
  disk, CPU, or Replay sink growth.
- Increasing materialization concurrency does not create duplicate bundles or
  corrupt terminal states.
- Playable evidence remains stable under pressure.

Acceptance:

```text
PASS_POST_INFERENCE_SPEC3_MEDIA_FINALIZER_THROUGHPUT
```

Required verification:

- tests for task claim idempotency and terminal-state convergence;
- tests for duplicate finalizer attempts;
- measured queue wait, ffmpeg elapsed/CPU, playable p95, and lifecycle p95/p99;
- staged pressure rerun showing at least the same retained playable count as the
  baseline and improved latency.

### Spec 4 - Annotation And Evidence Window Sizing

Problem:

Annotation completeness depends on the relationship between frame annotation
retention, Replay TTL, admission/backpressure, and media materialization
lifecycle. Raising one limit without measuring the others can only move cost to
Redis or disk.

Modification direction:

- Use measured p95/p99 materialization lifecycle plus proof retry budget to
  derive:
  - `FRAME_ANNOTATION_REDIS_MAXLEN`;
  - `FRAME_ANNOTATION_TTL_SECONDS` as message/deadline metadata, not Redis
    key expiry;
  - `EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS`;
  - Replay TTL;
  - evidence admission limits.
- Keep degraded evidence semantics for playable video with missing annotations.
- Record the memory/storage cost of the selected retention window.

Current checkout status:

- Midterm defaults are now `FRAME_ANNOTATION_REDIS_MAXLEN=200000`,
  `FRAME_ANNOTATION_TTL_SECONDS=600`, and
  `EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS=600`.
- Redis stream retention is governed by `XADD ... MAXLEN` plus worker lookback
  windows. `ttl_seconds` is carried in the frame-annotation message and is not
  currently enforced as Redis `EXPIRE` or stream `MINID` trimming.
- The 60-stream 3 FPS pressure run retained 50/50 playable bundles but only
  26/50 annotation-complete bundles; the later 16/1 pressure run retained
  50/50 annotation-complete bundles while not proving 16 FPS inference
  throughput.

Target effect:

- Annotation complete ratio improves for retained pressure samples.
- Missing annotations are explainable and surfaced as degraded, not silent
  success.
- Redis memory and Replay storage stay within the selected operating envelope.

Acceptance:

```text
PASS_POST_INFERENCE_SPEC4_EVIDENCE_WINDOW_SIZED
```

Required verification:

- pressure report includes annotation complete/missing/degraded counts;
- Redis frame annotation stream size and memory are recorded;
- Replay TTL and evidence lifecycle p99 have positive margin;
- 8090 evidence detail shows playable/degraded states accurately.

### Spec 5 - Downstream Observability Contract

Problem:

Post-inference bottlenecks span Redis, PostgreSQL, workers, ffmpeg, and 8090.
If pressure artifacts omit one layer, the next fix order becomes guesswork.

Modification direction:

- Extend pressure reports to include:
  - Redis stream length and pending by consumer group;
  - worker CPU/memory sample where available;
  - PostgreSQL hot query plans/stat deltas;
  - face-worker gallery query p95;
  - event-worker record request dedupe count/latency;
  - media-worker queue wait, ffmpeg elapsed/CPU, playable p95, lifecycle p99;
  - evidence retained IDs and 8090 query proof.
- Add static tests for the report schema if the data is emitted by scripts.

Current checkout status:

- The pressure report already captures retained evidence, playable count,
  cleanup behavior, source/forwarder/Savant health, Redis pending status, and
  8090 evidence query proof.
- Remaining observability gaps are face-worker gallery query p95, event-worker
  dedupe count/latency, PostgreSQL hot query-plan/stat deltas, and explicit
  media queue wait/lifecycle p95/p99.

Target effect:

- Each later optimization can prove whether its target metric improved.
- A passing pressure run includes enough downstream data to reject false
  readiness claims.

Acceptance:

```text
PASS_POST_INFERENCE_SPEC5_DOWNSTREAM_OBSERVABILITY
```

## 6. Final Acceptance

The post-inference closure is complete only when all of the following are true
for the selected pressure profile:

- source adapters do not exit or restart unexpectedly;
- forwarder queue and send failures are bounded;
- Savant effective FPS matches the selected profile definition;
- Redis exporter drops/write errors are zero under normal pressure, or explicitly
  accepted as degraded behavior;
- `security.events`, `security.face_observations`, and
  `security.record_requests` pending do not grow without bound;
- event-worker record request idempotency does not scan the whole stream;
- face-worker gallery/watchlist p95 is bounded under the selected gallery size;
- media-worker queue wait and evidence lifecycle p95/p99 are within target;
- retained evidence bundles meet the playable target;
- annotation complete ratio meets the selected threshold, and missing annotation
  cases surface as degraded;
- 8090 evidence list/detail can query retained samples after cleanup.

Final token:

```text
PASS_POST_INFERENCE_60_STREAM_CLOSURE
```

Current checkout status:

- Do not issue `PASS_POST_INFERENCE_60_STREAM_CLOSURE` yet.
- The downstream evidence-chain pressure result is good enough to start scoped
  code work, but final closure still requires removing the record-request
  full-stream scan, bounding face-worker gallery/watchlist p95 for the selected
  gallery size, choosing and proving the media-finalizer concurrency model, and
  extending pressure observability.

## 7. Non-Goals

This spec does not:

- change model weights, model intervals, detector thresholds, or tracker logic;
- choose the final T4 batch/concurrency operating point;
- change camera/rule product semantics;
- replace the replay/evidence storage architecture;
- authorize code changes without a captured or refreshed pressure result.

Those belong to the existing Savant/T4, operator control, and replay/evidence
specs listed above.
