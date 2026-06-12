# Midterm Recent 24h Findings Goal

Date: 2026-06-12

Use this file as the next goal prompt for repairing the current midterm runtime.
It consolidates the markdown findings created or updated in the last 24 hours,
plus the same-window performance observability spec.

## Source Documents

Recent `docs/**/*.md` inputs:

- `docs/runtime_stability_fix/midterm_worker_savant_batching_findings.md`
- `docs/midterm_camera_runtime_source_identity_findings_2026-06-12.md`
- `docs/midterm_media_worker_snapshot_performance_findings_2026-06-12.md`
- `docs/runtime_stability_fix/midterm_multi_source_runtime_stability_plan.md`
- `docs/repair_goal/midterm_replay_runtime_split_goal_order.md`
- `docs/midterm_deployment.md`
- `docs/current_mainline_status.md`
- `docs/midterm_progress_snapshot_2026-06-11.md`
- `docs/project_current_progress_summary.md`
- `docs/replay_evidence_fix/midterm_replay_evidence_upstream_fix_plan.md`

Related same-window spec:

- `specs/15_savant_performance_observability.md`

## Goal

Repair the current `midterm` runtime so enabled cameras converge into the
running source-adapter topology, Savant performance is observable through a
valid metrics/smoke path, and evidence-path bottlenecks in `clip-worker` and
`media-worker` are no longer hidden behind skipped jobs, full-tree scans, or
manual log inspection.

The finished goal must leave a runnable, validated midterm deployment with:

- desired-vs-actual source state visible for every enabled camera;
- a real dynamic source-adapter path for non-primary enabled cameras;
- Savant `/metrics` or stable project aliases discoverable and smoke-tested;
- bounded Redis consumer lag/pending recovery for evidence requests;
- media-worker scan/probe work measured and reduced;
- a 10-15 minute two-source validation artifact bundle.

## Current Mainline

Use only the active midterm entrypoint:

- compose: `infra/docker-compose.midterm.yml`
- env: `infra/env/midterm.env`
- replay config: `modules/savant_replay/config.midterm.json`
- Savant module: `modules/savant_security/module.yml`
- camera config: `modules/savant_security/config/cameras.midterm.yml`
- generated sources: `infra/generated/sources.generated.yml`
- customer entry: `http://0.0.0.0:8090/`

Do not copy or revive archived phase-only compose/config files.

## Hard Boundaries

Do not treat these as bugs:

- `MAX_FPS=8/1` is intentional PTS-domain frame admission before inference.
- Intrusion `snapshot_required=false` is current product policy, not snapshot
  backend failure.
- Evidence fail-closed states such as `duration_guard_failed` can be correct
  safety behavior.
- Redis/PostgreSQL are shared logical stores keyed by `source_id` and
  `camera_id`; do not create one table per camera.

Do not do these in this goal:

- Do not enable detector multi-batch or change `BATCH_SIZE`,
  `POSE_BATCH_SIZE`, or `FACE_DETECTOR_BATCH_SIZE`.
- Do not replace `PtsFpsGate` with only `nvinfer.interval`.
- Do not broaden into unrelated algorithm implementations unless needed for
  the validation source/rule path.
- Do not remove Replay/evidence fail-closed guards.
- Do not revert uncommitted deployment state such as generated runtime epoch
  changes unless explicitly requested.

## Consolidated Findings

### F1: Enabled camera does not mean running source

The `lab` camera is in DB/config and is enabled:

```text
source_id=source_00000000-0000-4000-8000-781078565686
name=lab
enabled=true
rtsp_url=rtsp://10.37.57.157:8554/camera
```

But the running topology only had:

```text
video-analytics-midterm-source-adapter
SOURCE_ID=primary_rtsp
```

No `video-analytics-source-*` dynamic adapter was running for `lab`. Recent
events therefore still came from `primary_rtsp`. Any performance or two-source
validation is invalid until desired enabled sources and actual running source
adapters match.

### F2: UI/source naming confuses operators

The UI currently surfaces long machine `source_id` values where the operator
expects `camera.name`, for example `lab`. Keep `source_id` in technical details,
but evidence list/detail and runtime status should prefer camera display name
when it is available.

### F3: Savant observability is not yet production-grade

The spec requires official Savant metrics or stable project aliases for:

- per-source frame throughput and last-frame age;
- inference activity and object output;
- queues and event latency;
- runtime state and restart counts;
- GPU/CPU/memory/IO.

Current gap from the spec:

- no explicit `parameters.telemetry.metrics` in `module.yml`;
- compose does not expose/publish Savant webserver metrics;
- no Prometheus/Grafana path in midterm compose;
- no metrics smoke with `PASS_SAVANT_PERF_OBSERVABILITY_READY`;
- current checks rely on Docker state, logs, Redis length, and manual
  `nvidia-smi`.

### F4: `clip-worker` can lose or delay evidence work

`clip-worker` is a single-threaded Redis Stream consumer using `XREADGROUP`
with `">"`. Current risks:

- no startup claim/replay of pending entries after crash-before-ack;
- `CLIP_WORKER_MAX_CONCURRENT_JOBS=1` is a skip gate, not a queue;
- post-Savant frame proof can wait around 30 seconds in the consumer loop;
- keyframe lookup has a separate retry window;
- Replay API has a 30 second HTTP timeout.

Recent runtime state showed no Redis backlog at inspection time, but still
showed evidence path losses or delays:

```text
not_implemented      37
failed               14
ready                 9
replay_job_created    1
skipped_by_poc_limit  1

failure reasons:
missing_post_savant_frame_pts_window
CLIP_WORKER_MAX_CONCURRENT_JOBS reached
```

### F5: `media-worker` is single-threaded and scan/probe heavy

`media-worker` polls active sink output and synchronously finalizes evidence.
Current risks:

- repeated `rglob("metadata.json")` full-tree scans;
- in-memory-only `processed_dirs`, causing restart rescan;
- synchronous probing/copying/cropping/metadata/sidecar/DB updates;
- missing `ffprobe` in the inspected container, causing `imageio_ffmpeg`
  fallback;
- per-event Redis frame-cache scan defaults:
  `FRAME_CACHE_SIDECAR_LOOKBACK_COUNT=20000`,
  `FRAME_CACHE_SIDECAR_MAX_SCAN=20000`;
- JSONB media status filters lack expression/partial indexes.

Runtime evidence showed active progress but large accumulated work:

```text
sink metadata files under active root: 1566
evidence directories under /media/evidence: 2864
```

### F6: Replay/evidence implementation is mostly landed, but runtime acceptance remains

Recent replay/evidence commits are marked implemented, including Replay
foundation, evidence duration tuning, frame identity alignment, stream-session
isolation, and crop safety. Remaining acceptance must still prove with real
samples:

- ready clips are near the requested 10 second window;
- bad sink windows fail closed;
- previous 31.28s/61.35s failure shapes no longer publish unsafe raw clips.

### F7: Multi-source runtime hardening is partly complete, but live long-run remains

Runtime capacity/readiness hardening and one active source-adapter restart
injection are marked complete. Remaining acceptance:

- full 10-15 minute two-source long-run;
- restart counts stable;
- both source adapters remain running;
- both source ids produce frame annotations;
- no Savant pad/streammux/nvinfer/TensorRT fatal errors.

### F8: Savant source-reset patch and supervisor are current mainline assumptions

Current docs say the midterm mainline applies md5-pinned Savant v0.6.0
source-reset hardening through `modules/savant_security/savant_patches/`, and
the API service owns STOPPED/stall recovery. Treat these as preconditions to
verify, not work to re-invent inside this goal.

### F9: Algorithm capability is uneven

`behavior.intrusion` is the current end-to-end production baseline.

Partially implemented:

- `behavior.crowd_gathering`
- `behavior.fall`
- `behavior.chasing`

Config-visible but not complete runtime/evidence capability:

- `behavior.loitering`
- `behavior.running`
- `behavior.wall_climb_suspicious`
- `face.observation`
- `face.watchlist`
- `face.live_search`

Do not let UI-visible switches imply production evidence capability. Add or
preserve a support matrix if this goal touches operator/runtime status.

## Execution Plan

### Phase 0: Preflight and state accounting

1. Record dirty worktree before edits:

```bash
git status --short
```

2. Run current static checks:

```bash
pytest -q harness/tests/test_midterm_deployment_contract.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
```

3. If containers are running, capture current desired/actual source state:

```bash
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/cameras/runtime/supervisor
python scripts/runtime/camera_source_controller.py status \
  --sources infra/generated/sources.generated.yml || true
docker ps -a --filter name=video-analytics-source --format '{{.Names}}\t{{.Status}}'
```

### Phase 1: Source convergence and identity

Implement a source-only convergence path for camera add/enable/disable/RTSP URL
edits when Replay/Savant/module topology does not need a full restart.

Required behavior:

- supervisor reports desired enabled sources from DB/config/generated sources;
- supervisor reports expected source-adapter container names;
- supervisor reports running, missing, stale, and stopped adapters;
- source-only apply can start/recreate/stop only affected dynamic adapters;
- full runtime apply remains available for topology/module changes;
- dynamic adapter names keep the `video-analytics-source-*` convention;
- `primary_rtsp` remains the compose-managed special case.

Tests to add or update:

- enabled non-primary camera has expected dynamic adapter name;
- disabled camera stops/removes expected dynamic adapter;
- supervisor flags configured-but-not-running source;
- runtime apply/source-only apply does not start sources before Savant is ready.

### Phase 2: Evidence UI camera naming

Expose and display camera names without breaking technical identity.

Required behavior:

- evidence list/detail responses include `camera_name` when resolvable;
- UI prefers `camera_name`, with `source_id` and `camera_id` still visible in
  technical details;
- if backend cannot resolve name, UI falls back to `source_id` safely.

Tests to add or update:

- evidence API response includes camera display name;
- operator/evidence UI render path prefers camera name over long `source_id`;
- source identity remains unchanged in payloads and evidence metadata.

### Phase 3: Savant performance observability

Implement the minimum spec-15 observability path.

Required compose/module work:

- add explicit Savant metrics environment:

```yaml
WEBSERVER_PORT: "8080"
METRICS_FRAME_PERIOD: "1000"
METRICS_TIME_PERIOD: "5"
METRICS_HISTORY: "100"
METRICS_EXTRA_LABELS: '{"service":"savant-security","profile":"midterm"}'
```

- optionally publish local dev port `18080:8080`, or provide a container-network
  curl path if host publishing is not chosen;
- discover actual metric names from the running `0.6.0-7.1` image;
- add a lightweight metrics PyFunc only if official metrics do not provide
  stable project-level answers.

Metrics semantics must distinguish:

- source input FPS;
- Savant-admitted FPS after `PtsFpsGate`;
- model opportunity FPS from `nvinfer.interval`;
- object-output FPS;
- Redis/export FPS.

Add smoke:

```text
scripts/smoke/current/check_savant_perf_observability.sh
```

The smoke must print:

```text
PASS_SAVANT_PERF_OBSERVABILITY_READY
```

only when `/metrics` is scrapeable, a frame metric or alias advances, restart
count is stable, frame flow exists for enabled sources, and GPU metrics are
collected or explicitly reported unavailable.

### Phase 4: Clip-worker queue safety

Repair evidence request handling without weakening fail-closed behavior.

Required behavior:

- recover pending Redis Stream entries on startup or periodic sweep using
  `XPENDING`/`XAUTOCLAIM` or equivalent;
- replace `skipped_by_poc_limit` for normal concurrency pressure with queued or
  delayed retry state;
- move post-Savant frame proof waiting out of the single hot consumer path, or
  store retryable state so later requests can still be consumed;
- keep final failure bounded and explicit after retry budget is exhausted;
- preserve priority handling for `watchlist_hit` / `live_search_hit`.

Add counters/log fields for:

- pending claimed;
- deferred retry count;
- retry age;
- frame proof wait seconds;
- Replay job created;
- final fail reason;
- Redis lag and pending count.

Tests to add or update:

- crash-before-ack pending entry is reclaimed;
- concurrency pressure defers instead of permanently skipping;
- post-Savant missing proof retries without blocking unrelated messages;
- exhausted retry budget marks fail-closed with clear reason.

### Phase 5: Media-worker scan/probe reduction

Make the media-worker work measurable and reduce repeat scanning.

Required behavior:

- constrain sink scanning to active runtime epoch;
- persist processed/invalid sink output directories across restart;
- avoid full-tree `rglob` on every 5 second poll when a manifest, cursor, or
  incremental discovery path is available;
- install `ffprobe` in the media-worker image or make fallback cost and timeout
  explicit;
- reuse parsed metadata, duration probes, and decoded-frame counts within one
  finalization pass;
- make frame-cache sidecar reads time/range bounded by stream id or timestamp,
  with 20k scan only as a fallback.

Add metrics/log fields for:

- sink scan duration;
- metadata files visited;
- per-event finalization duration;
- ffmpeg/ffprobe invocation count and duration;
- metadata rows loaded;
- Redis frame-cache scanned/retained;
- snapshot queue size and extraction duration.

Tests to add or update:

- processed dirs survive restart;
- active epoch scan ignores old epochs;
- ffprobe present or fallback reports bounded diagnostic;
- frame-cache range read filters by runtime epoch, source, camera, and stream
  session.

### Phase 6: Database indexes for worker queries

Add a narrow migration for media/evidence worker query shapes.

Candidate indexes must be validated with `EXPLAIN`, but likely include:

- expression/partial index on `payload -> 'media' ->> 'clip_status'`;
- expression/partial index on `payload -> 'media' ->> 'snapshot_status'`;
- expression/partial index on `payload -> 'media' ->> 'snapshot_required'`;
- optional recent-row partial filter on `updated_at` or `created_at` if used by
  worker scans.

Do not rewrite the schema into per-camera tables.

### Phase 7: Runtime and evidence acceptance

Run static validation:

```bash
pytest -q harness/tests/test_midterm_deployment_contract.py
pytest -q harness/tests/test_camera_runtime_apply_service.py
pytest -q harness/tests/test_midterm_replay_evidence_duration_guard.py
pytest -q harness/tests/test_midterm_replay_epoch_isolation.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
git diff --check
```

Run deployment checks:

```bash
bash scripts/runtime/doctor_midterm.sh
bash scripts/smoke/current/check_midterm_deployment.sh
bash scripts/smoke/current/check_savant_perf_observability.sh
```

Then run a 10-15 minute two-source validation only after desired and actual
source adapters match. Capture:

- run id and timestamp;
- git commit and dirty worktree summary;
- compose and env files;
- active source ids and camera names;
- model intervals and FPS gate settings;
- Savant metrics scrape or alias sample;
- GPU/CPU/container stats;
- Redis stream lag and pending;
- container restart counts before/after;
- event latency summary;
- evidence ready/fail-closed sample status.

Write artifacts under:

```text
/data/video-analytics/artifacts/perf/<run_id>/
  run_config.json
  savant_metrics_head.txt
  docker_stats.jsonl
  nvidia_smi.jsonl
  redis_streams.json
  source_convergence.json
  evidence_samples.json
  summary.json
```

The final summary must classify the bottleneck as one of:

- source/input-bound;
- pipeline stuck but container healthy;
- GIL-bound Python;
- CPU-bound;
- GPU-bound;
- external backpressure from Redis/Postgres/workers/evidence IO;
- no bottleneck observed in the run window.

## Acceptance Criteria

This goal is complete only when all conditions below are true:

- `lab` or another enabled non-primary source has a matching running dynamic
  source-adapter, or the system explicitly reports it missing with a repair
  action.
- Evidence UI/API can show camera display name while preserving `source_id`.
- Savant metrics or project aliases are available and verified by
  `PASS_SAVANT_PERF_OBSERVABILITY_READY`.
- Redis frame flow is verified per enabled source, not inferred from total
  stream length alone.
- `clip-worker` can recover pending entries and no longer permanently skips
  normal jobs only because the single concurrency slot is busy.
- `media-worker` no longer rescans all historical active-epoch output after
  restart and records scan/probe timings.
- media/evidence worker queries have targeted indexes or documented `EXPLAIN`
  evidence showing indexes are not needed yet.
- Runtime evidence samples show ready clips near the requested 10 second window
  or fail closed without publishing unsafe raw clips.
- A 10-15 minute two-source run is recorded with no unbounded queue growth, no
  Savant restart-count increase, and no fatal pad/streammux/nvinfer/TensorRT
  errors.

## Reporting Requirements

Final response for the implementation run must include:

- changed files grouped by phase;
- tests and smoke commands run;
- runtime artifact directory;
- source convergence summary;
- evidence ready/fail-closed sample summary;
- performance bottleneck classification;
- any remaining blocked item with concrete reason.

