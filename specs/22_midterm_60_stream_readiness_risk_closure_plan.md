# 22_midterm_60_stream_readiness_risk_closure_plan.md

Date: 2026-06-23

## 1. Purpose

This spec records the 2026-06-23 readiness review for the current midterm
runtime and turns the open 60-stream findings into an execution plan. It bridges:

- `specs/16_dual_path_30x2_t4_production_optimization.md`
- `specs/20_dual_4090_as_t4_two_source_validation.md`
- `specs/21_replay_evidence_io_optimization_60_stream_production.md`
- `docs/midterm_migration_runbook_2026-06-23.md`

This is not a code-change spec by itself. It is the ordering and acceptance
contract for the next implementation phases.

## 2. Current Verdict

The sampled analysis path is working. The 10-second audit reported that:

| Source | Forwarder input | Forwarder output to Savant | Meaning |
| --- | ---: | ---: | --- |
| `primary_rtsp` | about 23.82 fps | about 7.91 fps | forwarder is dropping sampled analysis frames |
| lab dynamic source | about 29.85 fps | about 8.01 fps | forwarder is dropping sampled analysis frames |

Savant sees about 8 fps per source, so the remaining 60-stream capacity question
is no longer "does the forwarder sample?" It is whether the post-sampling
analysis path can sustain about:

```text
60 streams * 8 fps = 480 analyzed frames/s total
30 streams * 8 fps = 240 analyzed frames/s per shard
```

and whether the full-rate evidence materialization path can keep up without
losing annotation/proof windows.

The system is not 60-stream ready. The current Phase 2 readiness gate fails on
this host:

```text
FAIL enabled_rtsp_sources actual=2 required>=30
FAIL gpu_t4_count actual=0 required>=1 names=NVIDIA GeForce RTX 4090,NVIDIA GeForce RTX 4090
FAIL runtime_health_ok issues=['container_restart_count_high']
FAIL runtime_source_count actual=2 required>=30
PHASE2_SINGLE_T4_READY=false
```

The current two-GPU 4090 host can validate topology, routing, evidence semantics,
and selected soak behavior. It cannot produce `PASS_PHASE2_SINGLE_T4_30` or
`PASS_PHASE3_DUAL_T4_60`.

Active profile boundary:

- Step A must cover every profile that can be used as a production readiness
  path: the current single-shard `savant-security` service and the sharded
  `savant-a` / `savant-b` services.
- A profile-specific optimization is not sufficient unless the final 60-stream
  active services use that same module path, Redis exporter configuration, and
  output-frame policy.
- Readiness reports must record which Savant services were active, which module
  file each service loaded, and whether any legacy single-shard service was
  still running during the run.

Migration boundary:

- No Step A-D optimization should start until the current baseline can be moved
  to another host and started from a documented package.
- The package must include a release commit or git bundle, deploy env/config,
  model roots, database dump when camera/rule/gallery state is required, and a
  manifest with checksums.
- Runtime Redis streams are not migration state. Historical evidence and Replay
  RocksDB are optional and must be declared explicitly if moved.
- The required runbook and acceptance token are defined in
  `docs/midterm_migration_runbook_2026-06-23.md`.

## 3. Reviewed Risks

### R1 - Savant batch and concurrency are still untuned

Current facts:

- single-shard `savant-security` now reads `BATCH_SIZE`, `POSE_BATCH_SIZE`,
  `FACE_DETECTOR_BATCH_SIZE`, `FACE_EMBEDDING_BATCH_SIZE`,
  `MAX_PARALLEL_STREAMS`, and `BATCHED_PUSH_TIMEOUT` from env-backed compose
  defaults;
- `infra/env/midterm.env` now keeps the explicit 60-stream 8 FPS acceptance
  operating point:
  `BATCH_SIZE=4`, `POSE_BATCH_SIZE=4`, `FACE_DETECTOR_BATCH_SIZE=4`,
  `FACE_EMBEDDING_BATCH_SIZE=16`, `MAX_PARALLEL_STREAMS=64`, and
  `BATCHED_PUSH_TIMEOUT=40000`;
- `module.yml` still has safe env fallback defaults, but the midterm compose/env
  contract prevents the active 60-stream profile from silently returning to
  detector batch 1;
- the dual 4090 profile changes output codec to `copy`, but still leaves model
  batch and parallel-stream sizing as T4-unproven defaults;
- no T4 TensorRT batch/latency operating point is recorded.

Risk:

At 30 streams per shard, silently falling back to detector batch 1 or
`MAX_PARALLEL_STREAMS=4` is now treated as a deployment regression, not a
valid 60-stream profile. Remaining pressure failures after batch 4 / parallel
64 should be diagnosed as runtime throughput, sharding, exporter, or model
engine issues rather than hidden baseline misconfiguration.

Required closure:

- run the real T4 benchmark in spec 16 Phase 2;
- generate or validate TensorRT engines for candidate pose, face, and AdaFace
  batch sizes;
- derive `ANALYSIS_FPS`, `BATCH_SIZE`, `POSE_BATCH_SIZE`,
  `FACE_DETECTOR_BATCH_SIZE`, `FACE_EMBEDDING_BATCH_SIZE`,
  `MAX_PARALLEL_STREAMS`, `BATCHED_PUSH_TIMEOUT`, and model intervals from the
  measured T4 numbers;
- record the operating point in spec 16 Appendix A.

Acceptance:

- `PASS_PHASE2_SINGLE_T4_READY` on the target T4 host;
- `PASS_PHASE2_SINGLE_T4_30` after a 30-minute 30-stream shard pressure run;
- no 4090-only result may be used as the T4 acceptance token.

### R2 - Redis exporters have hot-path isolation, but fault validation remains

Current facts:

- `event_exporter`, `face_observation_exporter`,
  `person_observation_exporter`, and `frame_annotation_exporter` now enqueue
  Redis Stream writes through `AsyncRedisStreamWriter`;
- the writer uses bounded drop-on-full queues and performs `XADD` from a
  daemon thread, outside the Savant pyfunc hot path;
- defaults are `SAVANT_REDIS_EXPORTER_SOCKET_TIMEOUT_MS=50`,
  `SAVANT_REDIS_EXPORTER_CONNECT_TIMEOUT_MS=50`, and
  `SAVANT_REDIS_EXPORTER_QUEUE_MAXSIZE=1024`;
- `FRAME_ANNOTATION_WRITE_TIMEOUT_MS=50` remains the frame-annotation
  socket timeout default.

Risk:

At 480 analyzed frames/s, Redis pause or network stall should no longer block
Savant frame processing directly. The remaining risk is operational: queue
depth, enqueue drops, write failures, and degraded evidence behavior still need
production-style fault injection and metrics.

Required closure:

- keep every Redis exporter on explicit `socket_timeout` and
  `socket_connect_timeout` defaults;
- keep Redis writes behind the bounded async writer, not directly in pyfunc
  processing;
- use drop-on-full semantics for Redis fault containment, with counters/logs,
  rather than blocking Savant;
- distinguish normal-pressure behavior from fault behavior:
  - normal 30/60-stream pressure runs must show zero frame-annotation enqueue
    drops, or a documented non-zero threshold that is accepted by the evidence
    product owner;
  - Redis fault injection may drop derived observations and frame annotations,
    but affected evidence must be marked degraded / annotation-missing instead
    of being silently treated as a complete bbox/proof bundle;
- expose runtime metrics for queue depth, enqueue drops, write failures, write
  latency, enqueue latency, and last Redis error by exporter and source.

Acceptance:

- Redis fault injection causes exporter drop/error metrics to rise, while Savant
  effective fps and last-frame-age stay within the selected operating point;
- source-adapter restart count stays flat during the fault window;
- no exporter can block `process_frame` longer than the configured hot-path
  budget;
- define the budget before implementation. Initial target:

```text
EXPORTER_ENQUEUE_HOT_PATH_BUDGET_MS=1
exporter_enqueue_latency_p99_ms <= 1
exporter_enqueue_blocking_wait_max_ms <= 1
normal_pressure_frame_annotation_enqueue_drops_total == 0
```

The writer thread may spend longer in Redis `XADD`, but that cost must be
reported as writer latency and must not run on the Savant frame-processing call
stack.

### R3 - Debug PyFuncs and unused output encoding remain production cost

Current facts:

- `SAME_FRAME_DEBUG_ENABLED=1` is still set in the single-shard midterm compose;
- `face_embedding_debug`, `same_frame_detection_debug`, and `face_debug` remain
  in `module.yml`;
- `same_frame_detection_debug` is gated by env, but writes JSONL for every
  analyzed frame when enabled;
- `face_embedding_debug` and `face_debug` log every 30 frames and are not gated
  by a production env switch in the module;
- single-shard `savant-security` still uses `OUTPUT_FRAME` h264/nvenc even though
  midterm evidence comes from Replay/video-file-sink jobs, not Savant `5558`;
- `savant-a` and `savant-b` already use `OUTPUT_FRAME: {"codec":"copy"}`, so the
  single-shard service is now the drift point.

Risk:

The debug PyFuncs add Python object traversal, logging, and sometimes file IO on
the analyzed-frame path. The h264/nvenc output spends encoder/pipeline work on
an output stream that has no production consumer in the midterm evidence design.

Required closure:

- disable `SAME_FRAME_DEBUG_ENABLED` by default in production compose;
- gate or remove `face_embedding_debug` and `face_debug` from the production
  module path, while keeping a separate debug module/profile available;
- confirm no `5558` consumer in the target topology, then make single-shard
  `savant-security` match the sharded services with `OUTPUT_FRAME=copy` or a
  metadata-only/disabled-output mode supported by the deployed Savant image.

Acceptance:

- production `docker compose config` shows `SAME_FRAME_DEBUG_ENABLED=false` or no
  debug profile selected;
- a module-path audit, static test, or rendered Savant module check proves the
  active production module path does not include `face_debug`,
  `face_embedding_debug`, or an enabled same-frame JSONL writer;
- runtime logs do not contain recurring `[face_debug]`, `[face_embedding]`, or
  same-frame JSONL writes unless a debug profile is explicitly enabled;
- Savant output-frame encoding is not h264/nvenc in production profiles unless a
  real consumer is documented.

### R4 - Evidence materialization is already queue-bound

Current facts:

- reported 50-event audit: average materialization queue wait about 38.3s,
  p95 about 42.6s, lifecycle p95 about 145.4s, active ffmpeg child CPU about
  13.07s per clip;
- latest local `phase2plus` report found the same shape with worse queue depth:
  queue current depth 115, queue wait avg 44.427s, queue wait p95 52.793s,
  ffmpeg child CPU avg 13.326s, p95 16.102s. Evidence artifact:
  `/data/video-analytics/artifacts/phase2plus/evidence_materialization_phase2plus_report.json`;
- `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=2`;
- `MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S=180`,
  `MEDIA_WORKER_MATERIALIZATION_MAX_BACKLOG=200`,
  `EVIDENCE_FINAL_ROOT_MAX_BYTES=0`,
  `EVIDENCE_INCOMING_ROOT_MAX_BYTES=0`, and `REPLAY_SINK_OUTPUT_MAX_BYTES=0`.

Risk:

This is already visible with two sources. Scaling analysis to 60 streams can make
inference look healthy while evidence appears late, expires its annotation
window, or fails without bbox/sidecar proof.

Required closure:

- finish spec 21 as a separate workstream; do not mix it into T4 batch tuning;
- choose and record a materialization operating point:
  - active workers;
  - per-shard and per-source limits;
  - backlog cap;
  - hard timeout;
  - high-priority latency target;
  - storage quotas;
  - CPU/ffmpeg mode (`baseline_crop`, `bounded_crop`, `stream_copy_keyframe`,
    `nvenc_crop`, or another measured mode);
- keep quota and manifest-first behavior, but do not treat deferral as useful
  unless Replay and annotation TTLs cover the requested window.

Acceptance:

- two-source run reports p50/p95 queue wait, lifecycle latency, ffmpeg elapsed,
  child CPU, and current backlog;
- high-priority evidence p95 ready latency is below the configured target;
- 10 -> 30 -> 60 pressure runs prove materialization queues are bounded and do
  not expire required annotation/proof windows;
- `PASS_REPLAY_EVIDENCE_IO_OPTIMIZED_60_STREAM` is emitted only on real
  60-stream input.

### R5 - Frame annotation retention is too small for observed latency

Current facts:

- `FRAME_ANNOTATION_REDIS_MAXLEN=20000`;
- `FRAME_ANNOTATION_TTL_SECONDS=120`;
- at 60 streams and 8 analyzed fps, frame annotations arrive at about 480
  entries/s total;
- `20000 / 480 = about 42s` of Redis Stream length if all sources share one
  stream;
- observed queue waits are already around 38-53s, and reported lifecycle p95 is
  above the 120s TTL boundary.

Risk:

The system can successfully detect events but lose the post-Savant frame
annotation window before evidence finalization or sidecar generation. That
creates "clip exists but bbox/proof missing" failures.

Required closure:

- derive annotation retention from measured p99 materialization lifecycle plus
  replay post-window/proof budget and headroom;
- either raise maxlen/TTL for the shared stream or shard frame annotation streams
  by Replay/Savant shard;
- keep `FRAME_ANNOTATION_TTL_SECONDS` and
  `EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS` aligned;
- measure Redis memory impact before committing the production value.

Sizing rule:

```text
required_maxlen_per_stream >= stream_count * analysis_fps * retention_seconds * headroom

example, per 30-source shard:
30 * 8 * 180 * 1.5 = 64,800 entries
```

Acceptance:

- retention window is greater than measured p99 evidence lifecycle plus proof
  budget;
- 30-stream shard pressure run shows no missing annotation caused by Redis
  stream trimming or TTL expiry;
- Redis memory stays under the selected production budget.

### R6 - Forwarder fairness is not proven at 30 sources per shard

Current facts:

- the forwarder has one read loop, one writer thread, and one global
  `BoundedDropQueue`;
- queue depth is currently zero in two-source sampling windows;
- per-source counters exist for seen, forwarded, dropped, and send failures;
- there is no 30-source same-shard fairness pressure result yet.

Risk:

A global queue can look healthy in aggregate while one or more sources are
starved or dropped disproportionately under 30-source burst pressure. Current
two-source queue depth 0 does not prove fairness.

Required closure:

- add or derive per-source forwarded fps, drop ratio, send failure ratio, and
  last-forwarded age;
- run a 30-source same-shard synthetic or real pressure test with source jitter,
  keyframe bursts, and a Savant write stall;
- if unfairness appears, replace the global FIFO behavior with per-source queues
  and a round-robin or weighted scheduler.

Acceptance:

- over the Phase 2 pressure window, every enabled source stays within the
  configured fps band, for example target fps +/- 10% after warmup;
- no source has unbounded last-forwarded age while other sources continue;
- drop ratios are explained by input cadence or configured source priority, not
  queue starvation.

## 4. Execution Order

### Step 0 - Migration package and clean-host baseline

Scope:

- freeze the code/config state into a release candidate commit or git bundle;
- classify all dirty worktree files as included, patch-artifact, or excluded;
- package required models, downloads, env/config, and database state;
- restore the package on a clean target host or clean target directory;
- prove the restored host can run the midterm stack and produce runtime evidence.

Why first:

Performance tuning on a non-reproducible local tree is not portable. The target
T4/dual-T4 work must start from a baseline that can be rebuilt, restored, and
diagnosed on another machine without relying on hidden local files.

Exit gate:

```text
PASS_60R_STEP_0_MIGRATION_BASELINE
```

Required checks:

- migration manifest exists with git SHA, `git status --short`, package
  checksums, compose profile, and data inclusion policy;
- `docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml config`
  passes on the target host;
- required Docker images build or load on the target host;
- Savant, Replay, Redis, API, event-worker, clip-worker, media-worker, and
  evidence-viewer start from the migrated package;
- at least one real RTSP source reaches Savant and advances frame annotation
  counters;
- a real event produces an evidence bundle, or the report explicitly records why
  event generation was not triggered and proves the lower-level runtime path is
  healthy.
- 8090 operator sanity passes on the restored host:
  - evidence list camera display names match `cameras.name` for managed cameras;
  - face registration new-person mode does not submit hidden `person_id`, and
    append mode only submits it after the operator explicitly selects append;
  - storage maintenance time-range delete preview uses event/alarm time and
    skips bundles without trusted event time instead of using filesystem mtime.

### Step A - Hot-path cost reduction without model accuracy changes

Scope:

- gate or remove production debug PyFuncs;
- disable or change unused Savant output encoding after confirming no consumer;
- add Redis exporter timeouts, bounded async queues, and metrics.

Current status:

- production debug PyFuncs and unused NVENC output have been removed from the
  midterm production path;
- Redis exporter timeouts and bounded async queues are in place;
- Redis exporter runtime metrics and fault-injection evidence remain to close
  the full production gate.

Why first:

These changes reduce avoidable jitter and waste without changing model weights,
confidence thresholds, evidence semantics, or the analysis fps target.

Exit gate:

```text
PASS_60R_STEP_A_HOT_PATH_DEBURDENED
```

Required checks:

- targeted unit/static tests for debug gating and exporter config;
- active module/profile audit for `savant-security`, `savant-a`, and `savant-b`;
- `docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml config`;
- Redis fault injection smoke;
- normal-pressure exporter drop check:
  `normal_pressure_frame_annotation_enqueue_drops_total == 0`;
- `git diff --check`.

### Step B - Forwarder fairness and 30-source shard pressure

Scope:

- extend forwarder metrics for per-source fairness;
- run 30-source same-shard pressure before or alongside the T4 run;
- change queue scheduling only if the pressure run proves starvation.

Exit gate:

```text
PASS_60R_STEP_B_FORWARDER_FAIRNESS_30
```

Required checks:

- per-source forwarded fps and drop ratio stay within the selected band;
- queue and send-failure behavior is bounded under Savant write stall;
- source-adapter restart count stays flat.

### Step C - Savant T4 batch and operating point

Scope:

- run the real single-T4 Phase 2 benchmark;
- build/validate dynamic TensorRT engines;
- set production batch, intervals, and parallel-stream values;
- record spec 16 Appendix A.

Runtime performance control update - 2026-06-27:

The 8090 runtime control page now has a performance configuration surface backed
by:

```text
GET  /api/v1/runtime/performance-config
PUT  /api/v1/runtime/performance-config
POST /api/v1/runtime/performance-config/apply
```

It can save and apply Forwarder sampling FPS, Savant ingress FPS, pose/face/
AdaFace infer intervals, and `BATCHED_PUSH_TIMEOUT`. Apply is intentionally a
controlled runtime operation: it recreates only `analysis-forwarder` and/or
`savant-security`, waits for Savant readiness when needed, and uses the existing
evidence restart guard.

This closes the operator-control gap for FPS/interval tuning, but does not close
the production readiness gate. The remaining required evidence is a real T4
pressure run at the selected 2/3/4 FPS operating points, plus annotation
retention configuration and Redis exporter fault metrics.

Exit gate:

```text
PASS_PHASE2_SINGLE_T4_30
```

This remains the authoritative T4 shard readiness token.

### Step D - Evidence materialization and retention closure

Scope:

- finish spec 21 materialization quotas, timeout, backlog, and storage limits;
- choose the media materialization mode from measured CPU/IO data;
- size Redis annotation maxlen/TTL and Replay TTL together;
- run staged 10 -> 30 -> 60 pressure.

Exit gate:

```text
PASS_REPLAY_EVIDENCE_IO_OPTIMIZED_60_STREAM
```

This must pass before declaring end-to-end 60-stream readiness even if Savant
analysis is already stable.

## 5. Coordination Boundary

The current worktree contains in-progress evidence materialization changes. Do
not mix the next Savant/forwarder hot-path changes into those files until that
workstream boundary is explicitly settled.

Before implementing Step 0:

- decide whether the current evidence materialization changes are included in
  the release candidate commit, carried as a separate patch artifact, or left
  behind;
- do not package an unlabelled dirty worktree as the migration baseline;
- record whether historical evidence media and Replay RocksDB are part of the
  migration package or intentionally omitted for a clean performance host.

Before implementing Step A or Step B:

- decide whether the current evidence materialization changes are the baseline
  for spec 21 Phase 1A or should be separated;
- keep Savant model-chain changes out of evidence materialization commits;
- keep T4 TensorRT engine generation and batch tuning out of Redis/exporter
  hot-path commits.

## 6. Definition Of Ready For 60 Streams

60-stream ready means all of the following are true:

- `PASS_60R_STEP_0_MIGRATION_BASELINE` proves the baseline can run after
  packaging and restore on a target host;
- `PASS_PHASE2_SINGLE_T4_30` is recorded on real T4 hardware for one 30-source
  shard;
- two shards run with the recorded operating point and pass
  `PASS_PHASE3_DUAL_T4_60`;
- the active service/module matrix in the readiness report matches the intended
  production topology, with no unreviewed legacy single-shard path active;
- forwarder fairness is proven for 30 sources per shard;
- Redis exporter faults do not block Savant hot paths, and normal-pressure runs
  do not drop required frame annotations;
- frame annotation retention covers measured evidence lifecycle p99 plus proof
  budget;
- evidence materialization passes
  `PASS_REPLAY_EVIDENCE_IO_OPTIMIZED_60_STREAM`;
- no readiness claim depends only on the current two-source dual-4090 host.
