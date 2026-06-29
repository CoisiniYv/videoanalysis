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
- `docs/midterm_frontend_inference_performance_2026-06-28.md`

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
- The 8090 runtime control surface now exposes performance and topology config
  endpoints, including single runtime control, dual runtime stop, and
  `topology-config` save/apply paths.
- `clip-worker` already has replay shard routing support through
  `REPLAY_SHARDS_CONFIG_PATH` / `REPLAY_SHARDS_JSON`; this spec must not treat
  replay shard routing as absent code.
- 8090 camera algorithm/ROI saves now use
  `/api/v1/cameras/runtime/config/sync`; this is a config snapshot sync and is
  not supposed to restart Savant, Replay, source adapters, clip-worker, or
  media-worker.
- `clip-worker` now acks stale record requests whose DB event/task has already
  disappeared, so old Redis pending messages should not cause long-running
  proof/replay polling.
- Active evidence states such as `materializing`, `replay_job_created`, and
  `finalizing` are included in stale materialization convergence checks.
- `REPLAY_FORCE_CONSTANT_CADENCE=true` is now the midterm default, so Replay
  payload fallback should no longer be the normal evidence path.

Still open:

- face-worker still writes each face observation and then synchronously runs
  watchlist/gallery matching, but 2026-06-29 code now emits
  `gallery_query_duration_ms` so pressure reports can bound p95/p99 instead of
  reporting `not_enough_data`;
- gallery and face observation vector searches do not yet have ANN vector
  indexes; current live gallery size was only 3 active embeddings / 5 total
  embeddings, so ANN should not be added until representative gallery-size
  EXPLAIN/pressure data proves exact scan cost is real;
- media-worker materialization is still organized around one polling process and
  ffprobe/ffmpeg/decode finalization, but 2026-06-29 code now exposes
  `queue_wait_ms`, `lifecycle_elapsed_ms`, and post-Savant finalizer elapsed
  logs for pressure report p95/p99 ranking;
- dual-branch topology now has one same-GPU 8 FPS retained-evidence pass, but
  it still needs longer 8 FPS soak and real RTSP mixed-input repeat runs before
  being treated as a production guarantee.
- topology apply replay shard output and clip-worker replay shard input have
  been wired through `REPLAY_SHARDS_JSON` for the pressure harness; regression
  coverage now asserts topology branch source maps, generated source
  `replay_shard_id`, and replay shard `source_ids` stay consistent.
- downstream observability now emits a fixed Redis/PostgreSQL/worker/media/8090
  schema; face-worker gallery query latency and media-worker lifecycle splits
  are now populated from logs when present. Event-worker dedupe latency remains
  an explicit `not_enough_data` field until a code-level timer is added.
- 8090 config-sync versus runtime-apply behavior is now covered by static
  regression harnesses so camera rule/ROI edits do not reintroduce unnecessary
  runtime restarts.

Important boundary:

- The 60-stream 3 FPS downstream evidence run proves the current downstream
  evidence chain for that pressure profile.
- The 60-stream 16/1 run proves high-input evidence-chain resilience, not
  16 FPS inference throughput.
- The same-GPU dual-branch 30+30 topology has front-end inference entry proof
  and one retained-evidence closure proof at 4 FPS and 8 FPS. It is a strong
  single-run result, not yet a production soak guarantee.
- 2026-06-29 retained-evidence follow-up:
  `docs/midterm_dual1gpu_evidence_chain_4fps_8fps_report_2026-06-29.md`
  proves the same-GPU dual-branch topology at 4 FPS with replay shard routing
  and 50/50 playable, annotation-complete evidence bundles. A first 8 FPS
  retained run exposed a false-positive risk where ingress passed but
  pose/person/face observation output was 0. The follow-up 8 FPS batch=4 run
  passed with 50 retained playable bundles, 8,189 pose/person observations, and
  2,605 exported face observations. Future pressure reports must keep the
  semantic observation gate so ingress-only success is not mistaken for
  algorithm/evidence success.

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
- selected runtime topology: single, dual same GPU, or dual GPU;
- replay shard plan path and source-to-shard assignment when topology is dual;
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
  work in Spec 1, Spec 2, Spec 3, Spec 5, and dual-topology evidence-chain
  verification in Spec 6.
- 2026-06-29 follow-up fixes moved camera rule/ROI saves to config-sync,
  enabled Replay constant-cadence by default, and tightened stale evidence/task
  convergence. Those items are no longer primary implementation targets for
  this spec, but they must stay in the regression set.

Runtime topology note:

- `services/api/app/routers/runtime.py` exposes `/api/v1/runtime/topology-config`
  GET/PUT/apply endpoints.
- `services/api/app/services/runtime_topology.py` can build single, automatic
  dual, same-GPU dual, or dual-GPU plans; dual apply writes camera/module/source
  config plus a replay shard plan, recreates branch Savant/forwarder containers,
  starts branch Replay/video-sink containers, and starts source adapters.
- The apply path is protected by the evidence restart guard, but it is still an
  operator-visible disruptive action. Pressure artifacts must record whether it
  was applied, skipped, or blocked.

## 4. Problem Breakdown

| Area | Problem | Why it matters |
| --- | --- | --- |
| face-worker | Single consumer does Postgres insert and synchronous watchlist/gallery pgvector search per face observation | Face observation rate and gallery size can linearly amplify latency after Redis |
| event-worker | Record request dedupe is now Redis `SET NX EX`, but runtime pressure artifacts still need to show duplicate suppression and low CPU under event storms | A regression to full-stream scan would make every recordable event pay O(N) Redis/JSON cost |
| media-worker | Materialization active count is a guard, not a finalizer worker pool | ffprobe/ffmpeg/decode work can dominate evidence lifecycle even if inference is healthy |
| topology/replay shards | 8090 can apply dual-branch plans and clip-worker can route by replay shard, but the dual evidence-chain profile is not yet proven end-to-end | Front-end 8 FPS success can be misread as proof that evidence materialization is also production-ready |
| annotation/evidence windows | Retention and admission are now partially sized, but still need lifecycle and memory-margin proof | Evidence can become playable but annotation-missing, or expire before materialization |
| observability | Pressure reports need more downstream split metrics to rank bottlenecks | Without split metrics, fixes may only move backlog between queues |
| 8090 config sync | Camera rule/ROI saves must not call disruptive runtime apply | A simple ROI edit should not restart unrelated inference/evidence services or interrupt evidence generation |

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
- Front-end runs in `docs/midterm_frontend_inference_performance_2026-06-28.md`
  show that single 4090 dual branch 30+30 can pass 4 FPS and 8 FPS entry
  pressure with `keep-evidence=0`; these runs do not replace downstream
  evidence-chain closure.
- The pressure report schema now includes `downstream_observability_summary.json`
  with Redis stream/group status, PostgreSQL run/table/lifecycle summaries,
  event-worker dedupe counters, face-worker watchlist slots, media-worker
  finalization metrics, and retained-evidence 8090 proof.
- Remaining gaps are deliberately explicit `not_enough_data` fields where the
  worker code still lacks timers: face-worker gallery query p95/p99 and
  event-worker dedupe latency.

### Spec 1 - Record Request Idempotency

Problem:

`event-worker` previously checked whether a record request already existed by
scanning the whole `security.record_requests` Redis stream. That was O(N) in
stream length and was paid by each recordable event.

Current code verification:

- `services/event-worker/app/worker.py` still calls
  `record_publisher.has_request(source_event_id, "savant_replay")` before
  publishing a record request.
- `services/event-worker/app/record_request.py` now implements `has_request()`
  with a Redis idempotency key lookup and `publish()` with atomic `SET NX EX`.
- Duplicate record requests now terminally skip the new task as
  `recording_policy_skipped:duplicate_record_request` instead of leaving a
  retry-created evidence task pending.

Modification direction:

- Implemented Redis key/set idempotency keyed by `(stream, source_event_id,
  strategy)`, stored as a SHA-256 Redis key with configurable
  `RECORD_REQUEST_DEDUPE_TTL_SECONDS`.
- `publish()` reserves the key before `XADD` and releases the key if `XADD`
  fails, so a transient publish failure can be retried.
- Unit tests cover first publish, duplicate publish, publish-failure retry,
  `has_request()` without `XRANGE`, and event-worker duplicate policy.

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
- synthetic replay showing `has_request()` no longer performs `XRANGE - +`;
- no duplicate record request for the same `(source_event_id, strategy)`;
- retained pressure evidence remains queryable in 8090.

Harness plan:

- Added `harness/tests/test_record_request_idempotency.py` for duplicate,
  retry/release, TTL, and no-`XRANGE` contracts.
- Extended `harness/tests/test_event_worker_recording_policy.py` for
  duplicate retry task terminal skip behavior.
- No PostgreSQL migration is required because Redis stream remains the durable
  queue and the new idempotency key is a bounded TTL index.

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

Harness plan:

- Add a query-plan harness that can seed representative gallery rows and capture
  `EXPLAIN ANALYZE` for gallery lookup and face-observation lookup.
- Add a correctness test that compares ANN candidate results plus optional exact
  rerank against the current exact pgvector ordering for a small deterministic
  dataset.
- Add watchlist threshold tests that prove target-person filtering and duplicate
  suppression do not change when the index/search path changes.
- Extend the pressure report schema with face-worker consumed count, pending
  count, gallery query p95/p99, and emitted watchlist hit count.

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

Harness plan:

- Add media-worker tests for stale active task convergence from
  `materializing`, `replay_job_created`, and `finalizing` to a terminal state.
- Add duplicate finalizer tests where two claims see the same sink output and
  only one terminal bundle/update wins.
- Add degraded evidence tests for playable raw clip with missing frame metadata,
  so annotation loss remains visible without failing the video evidence.
- Add a report collector for `queue_wait_s`, `proof_wait_s`,
  `replay_job_elapsed_s`, `ffprobe_elapsed_s`, `finalizer_elapsed_s`, and
  total evidence lifecycle p95/p99.

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

- The pressure report now writes `downstream_observability_summary.json` and
  embeds the same object in `report.json`.
- The summary has fixed sections for Redis, PostgreSQL, event-worker,
  face-worker, media-worker, and 8090 retained-evidence proof.
- Redis uses `XLEN` and `XINFO GROUPS`; PostgreSQL uses run summary,
  `pg_stat_user_tables`, and evidence task lifecycle aggregates; worker CPU and
  downstream logs are sampled from docker stats/logs.
- Remaining observability gaps are now explicit `not_enough_data` values rather
  than omitted fields: face-worker gallery query p95/p99 and event-worker
  dedupe latency need code-level timers in a later stage.

Target effect:

- Each later optimization can prove whether its target metric improved.
- A passing pressure run includes enough downstream data to reject false
  readiness claims.

Acceptance:

```text
PASS_POST_INFERENCE_SPEC5_DOWNSTREAM_OBSERVABILITY
```

Harness plan:

- `scripts/runtime/run_midterm_pressure60.py` emits one JSON summary with Redis,
  PostgreSQL, worker, media, and 8090 proof sections.
- `harness/tests/test_midterm_pressure60_script.py` validates that required
  sections cannot be omitted and that explicit `not_enough_data` is accepted.
- Media finalization/ffprobe/ffmpeg distributions are parsed from logs when
  present; absent metrics stay explicit rather than being treated as pass.

### Spec 6 - Dual Topology Evidence-Chain Closure

Problem:

The current checkout has two related but separate capabilities:

- the 8090 control plane can save/apply dual topology plans;
- clip-worker can route record requests through replay shard config.

The pressure evidence so far is split: downstream evidence-chain closure was
captured on the selected 60-stream 3 FPS profile, while same-GPU dual branch
4 FPS/8 FPS runs were front-end inference entry tests with `keep-evidence=0`.
That split is useful for diagnosis, but it is not sufficient to claim the dual
topology is post-inference closed.

Current code verification:

- `services/api/app/services/runtime_topology.py` writes a replay shard document
  for dual plans and starts branch Replay/video-sink containers.
- `services/clip-worker/app/replay_shards.py` loads shard config from
  `REPLAY_SHARDS_JSON` or `REPLAY_SHARDS_CONFIG_PATH`.
- `services/clip-worker/app/worker.py` resolves the replay route per
  `source_id` before starting Replay jobs and records shard diagnostics on
  evidence tasks.
- `infra/docker-compose.midterm.yml` exposes `REPLAY_SHARDS_CONFIG_PATH` to the
  API and clip-worker with an empty default, while runtime topology writes to
  `RUNTIME_TOPOLOGY_REPLAY_SHARDS_PATH` or its default path. A pressure run must
  prove the active clip-worker used the intended file and source-to-shard
  mapping.

Modification direction:

- Make topology pressure artifacts capture:
  - saved topology config;
  - effective topology mode;
  - runtime epoch;
  - topology replay shard output path;
  - clip-worker replay shard input path;
  - replay shard file content hash;
  - branch container states;
  - per-branch forwarder/Savant metrics;
  - clip-worker replay shard diagnostics for retained evidence.
- For dual topology runs, require retained evidence from both branches.
- Keep front-end inference pressure and downstream evidence pressure separate
  in reports, but add one end-to-end run when claiming dual topology production
  readiness.

Target effect:

- A successful dual topology claim proves both front-end inference routing and
  post-inference Replay/clip/media/evidence behavior.
- 8090 topology apply cannot silently leave clip-worker using the default Replay
  route while source adapters write to branch Replay instances.
- Same-GPU and dual-GPU modes have separate evidence artifacts and acceptance
  tokens.

Acceptance:

```text
PASS_POST_INFERENCE_SPEC6_DUAL_TOPOLOGY_EVIDENCE_CHAIN
```

Required verification:

- topology config GET/PUT/apply targeted tests or smoke proof;
- replay shard parser/routing tests for duplicate, missing, and default shard
  cases;
- one pressure run for the selected dual topology with evidence retention
  enabled;
- retained samples include both branches and are queryable through 8090 list and
  detail APIs;
- no replay shard routing failures, duplicate terminal bundles, or branch-only
  evidence gaps.

Harness plan:

- Add replay shard parser tests for duplicate source assignments, missing
  source assignments, invalid shard URLs, and default shard fallback.
- Add a topology dry-run/apply smoke that records the written replay shard plan
  path, file hash, runtime epoch, and branch container names.
- Add a clip-worker routing test that proves retained evidence tasks include
  shard diagnostics matching the selected source-to-shard plan.
- Add a dual-topology pressure profile with evidence retention enabled and
  retained samples from both branches.

### Spec 7 - 8090 Config Sync Regression

Problem:

Camera algorithm, ROI, threshold, cooldown, and rule edits are lightweight
configuration changes. They must export the DB-backed camera/rule state to
runtime config snapshots without invoking disruptive runtime apply.

Modification direction:

- Keep 8090 camera rule/ROI save paths on
  `/api/v1/cameras/runtime/config/sync`.
- Keep runtime performance and topology changes on their explicit apply
  endpoints.
- Record the list of restarted/touched containers in every operator-facing
  response where an action can affect runtime.

Target effect:

- A camera rule edit does not restart unrelated services.
- Evidence generation is not interrupted by ROI/threshold/cooldown edits.
- Operators can distinguish "configuration synced" from "runtime restarted".

Acceptance:

```text
PASS_POST_INFERENCE_SPEC7_8090_CONFIG_SYNC_REGRESSION
```

Required verification:

- targeted API test for `/api/v1/cameras/runtime/config/sync` returning
  `containers_restarted=[]` and `source_containers_touched=[]`;
- frontend or contract test proving rule/ROI save calls config-sync, not
  `/api/v1/cameras/runtime/apply`;
- live smoke after one rule edit showing Savant/Replay/source adapter,
  clip-worker, and media-worker container IDs are unchanged;
- one evidence-generation smoke showing no new stale `materializing` task is
  introduced by a rule edit.

## 6. Next Implementation Plan

The next repair pass should be staged so each change has a narrow harness and a
clear stop condition.

### Stage 1 - Record request idempotency

Change:

- Done: `RecordRequestPublisher.has_request()` no longer scans the Redis
  stream. `publish()` uses Redis `SET NX EX` keyed by `(stream,
  source_event_id, strategy)` and releases the key if `XADD` fails.

Harness:

- Done: unit tests cover first publish, duplicate publish, retry after failed
  publish, TTL wiring, and a fake Redis guard that fails on `XRANGE`.
- Done: event-worker duplicate policy terminally marks duplicate retry tasks as
  `materialization_skipped`.

Acceptance:

- `PASS_POST_INFERENCE_SPEC1_RECORD_REQUEST_IDEMPOTENCY`;
- no duplicate record request for one `(source_event_id, strategy)`;
- Redis stream length no longer affects per-event dedupe latency.

### Stage 2 - Downstream observability schema

Change:

- Done: pressure collection now emits `downstream_observability_summary.json`
  before cleanup and embeds the same object in `report.json`.
- Done: the schema contains Redis stream/group status, PostgreSQL run/table and
  lifecycle summaries, event-worker dedupe counters, face-worker watchlist slots,
  media-worker finalization/ffprobe/ffmpeg distributions, worker CPU, and 8090
  retained-evidence proof.
- Remaining timer gaps are explicit `not_enough_data` fields.

Harness:

- Done: JSON schema/static tests reject missing sections and accept explicit
  `not_enough_data`.
- Done: synthetic log test proves downstream worker metrics are extracted.

Acceptance:

- `PASS_POST_INFERENCE_SPEC5_DOWNSTREAM_OBSERVABILITY`;
- every later optimization report can prove the target metric changed.

### Stage 3 - Face-worker vector search scale

Change:

- Measure exact pgvector query plans first.
- Add ANN indexes only after the representative plan shows exact scan cost is a
  real bottleneck.
- Preserve exact threshold semantics by exact rerank if ANN recall is used.

Harness:

- Seeded gallery/observation plan harness.
- Exact-versus-ANN correctness comparison.
- Watchlist threshold and target-person filtering tests.
- Pressure-log parser test for `gallery_query_duration_ms` p95/p99.

2026-06-29 status:

- Implemented: `WatchlistMatchEmitter` logs
  `watchlist_gallery_query_completed` / `watchlist_gallery_query_failed` with
  `gallery_query_duration_ms`, target count, top_k, threshold, and result count.
- Implemented: pressure report parses `face_worker.gallery_query_latency_ms`
  from logs instead of leaving it permanently `not_enough_data`.
- Current live DB check showed 3 active `person_gallery_embeddings` / 5 total
  embeddings and no ANN vector indexes. With this gallery size, exact scan is
  not the current bottleneck; defer ANN until representative gallery-size
  EXPLAIN/pressure evidence exists.

Acceptance:

- `PASS_POST_INFERENCE_SPEC2_FACE_WORKER_SCALE`;
- bounded face-worker pending and gallery query p95 under the selected gallery
  size.

### Stage 4 - Media finalizer throughput

Change:

- Do not raise concurrency blindly.
- First split media lifecycle metrics and harden idempotent terminal updates.
- Then choose internal worker pool, multi-container DB claim, or separate
  finalizer service based on measured queue wait and CPU/IO behavior.

Harness:

- Stale active task convergence tests.
- Duplicate finalizer claim tests.
- Playable-but-missing-annotation degraded evidence tests.
- Pressure rerun comparing lifecycle p95/p99 against the 60-stream 3 FPS
  baseline.

2026-06-29 status:

- Implemented: post-Savant materialization metrics now include
  `lifecycle_elapsed_ms` alongside `queue_wait_ms` and finalization elapsed.
- Implemented: `media_event_finalized` logs expose `queue_wait_ms`,
  `lifecycle_elapsed_ms`, and `post_savant_finalization_elapsed_ms`.
- Implemented: pressure report parses media queue wait, lifecycle elapsed,
  post-Savant finalization, ffprobe, and ffmpeg distributions. The next
  pressure run can rank whether the remaining bottleneck is queue admission,
  finalizer CPU/ffmpeg, or end-to-end lifecycle.

Acceptance:

- `PASS_POST_INFERENCE_SPEC3_MEDIA_FINALIZER_THROUGHPUT`;
- retained playable evidence does not regress and lifecycle p95/p99 improves or
  is explicitly bounded.

### Stage 5 - Dual topology evidence-chain closure

Change:

- Wire and prove topology replay shard output path equals clip-worker replay
  shard input path.
- Keep same-GPU dual branch retained-evidence pressure in the acceptance suite
  and repeat it as longer 8 FPS soak / real RTSP mixed-input tests before
  calling the topology production-ready.

Harness:

- Replay shard parser/routing tests.
- Topology apply dry-run smoke.
- Clip-worker shard diagnostics test.
- Dual pressure profile retaining evidence from both branches.

Acceptance:

- `PASS_POST_INFERENCE_SPEC6_DUAL_TOPOLOGY_EVIDENCE_CHAIN`;
- retained evidence samples cover both branches and are queryable through 8090
  list/detail APIs.

## 7. Final Acceptance

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
- if the selected runtime topology is dual, retained evidence proves both
  branches and clip-worker replay shard routing used the intended shard map;
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
  code work. Stage 1-2 removed the record-request full-stream scan and added the
  downstream observability contract. The 2026-06-29 P1 follow-up added
  face-worker gallery latency logs, media-worker queue/lifecycle split metrics,
  8090 config-sync no-restart regression coverage, and topology replay shard
  source-map regression coverage. Final closure still requires a fresh pressure
  report with these metrics populated, bounding face-worker gallery/watchlist
  p95 for the selected gallery size, and choosing/proving the media-finalizer
  concurrency model if queue/lifecycle p95 remains high.
- If the target deployment uses same-GPU or dual-GPU topology, final closure
  also requires repeated Spec 6 evidence-chain proof on that topology. The
  current same-GPU dual-branch 8 FPS batch=4 run is a valid single-run
  downstream evidence-chain closure token, but not a soak-test or production
  reliability guarantee.

## 8. Non-Goals

This spec does not:

- change model weights, model intervals, detector thresholds, or tracker logic;
- choose the final T4 batch/concurrency operating point;
- change camera/rule product semantics;
- replace the replay/evidence storage architecture;
- authorize code changes without a captured or refreshed pressure result.

Those belong to the existing Savant/T4, operator control, and replay/evidence
specs listed above.
