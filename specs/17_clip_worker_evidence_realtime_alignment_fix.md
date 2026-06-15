# 17_clip_worker_evidence_realtime_alignment_fix.md

## 1. Goal

Fix the midterm evidence experience so a detected event becomes visible quickly,
evidence generation state is observable in 8090, and annotation overlays are
frame-aligned instead of showing stale or wrong bounding boxes during the first
seconds of a clip.

This spec is the next Goal target after the current running stack was observed
to be generally healthy but evidence generation was slow or failed:

- Savant and analysis-forwarder were running, with both sources around 8.1fps.
- Redis `security.record_requests` had no consumer lag and only one pending
  item, so the bottleneck was not stream backlog.
- In the recent runtime window, clip-worker showed many deferrals and failures:
  `missing_post_savant_frame_pts_window`, `max_concurrent_reached`, and
  `retry_budget_exhausted`.
- A live-camera intrusion event was detected but the evidence failed after
  repeated `missing_post_savant_frame_pts_window` retries.
- Evidence that does succeed can take tens of seconds to become visible.
- Operator-visible overlays can show wrong/stale bbox in the first seconds of
  evidence playback.

The target behavior:

- detection event visibility is immediate in 8090;
- clip generation status is explicit (`waiting_proof`, `replaying`,
  `finalizing`, `ready`, `failed`);
- proof waiting does not consume clip generation concurrency;
- successful clips are generated from final cropped metadata/video timelines;
- frontend overlays render only frame-bound annotations, not broad time-offset
  fallbacks that can paint stale bbox onto the wrong video frame.

## 2. Confirmed Current Facts

### 2.1 Clip-worker state model is too coarse

Current code writes only broad statuses through
`services/clip-worker/app/repository.py::update_clip_status()`:

- `pending`
- `failed`
- replay job id after job creation

`_defer_clip_request()` rewrites event media status to `pending` and puts the
reason into `payload.media.error_message`. 8090 cannot clearly distinguish:

- event detected but waiting for post-Savant frame proof;
- waiting because Replay/video-file-sink is busy;
- actively replaying;
- finalizing in media-worker;
- failed permanently.

### 2.2 Proof wait is implemented as Redis consumer deferral

Current post-Savant proof lookup is in
`services/clip-worker/app/worker.py::_prepare_post_savant_replay_request()`.
Runtime env currently has:

```text
POST_SAVANT_FRAME_PROOF_ATTEMPTS=1
POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S=1.0
CLIP_WORKER_DEFERRED_RETRY_MAX_ATTEMPTS=12
CLIP_WORKER_MAX_CONCURRENT_JOBS=1
```

One failed lookup returns `missing_post_savant_frame_pts_window`; the message
then stays pending, is reclaimed, and burns delivery-count retry budget. This
is why failures appear after roughly 70-90 seconds rather than after a clean
state-machine timeout.

### 2.3 Concurrency gate is applied before proof readiness

`_clip_gate_decision()` checks `CLIP_WORKER_MAX_CONCURRENT_JOBS` before the
post-Savant proof is prepared. With `max_concurrent_jobs=1`, a task waiting on
proof or one long Replay job can make another detected event hit
`max_concurrent_reached` and eventually fail.

This is acceptable as a one-shot development gate, but not as production
evidence scheduling.

### 2.4 Video/metadata crop is mostly correct in recent bundles

Recent evidence summaries show:

```text
time_domain_crop_applied=true
video_crop.crop_video_to_time_window=true
video_crop.method=ffmpeg_segment_normalized_transcode
decoded_video_frame_count=240
timeline_reconciliation_status=metadata_time_aligned_sparse_sidecar
```

So the newest "wrong bbox in the first seconds" symptom is less likely to be
raw clip overrun and more likely to be sparse annotation rendering / fallback
timing / frontend hold behavior.

### 2.5 Annotation sidecar is sparse

Analysis FPS is around 8fps while evidence video is around 24fps. Recent
bundles can have 240 decoded video frames but only tens of sidecar rows.
Sparse sidecar is expected, but viewer rendering must not show a bbox before
or after the exact frame interval where it is valid.

### 2.6 Frontend currently supports broad hold windows

`services/api/app/static/operator/evidence.js::findActiveAnnotations()` uses a
per-object hold window around sparse annotations. `annotationTimeSeconds()` can
fall back to time offsets when frame identity is missing. This can make an
event-frame bbox appear during pre-event frames.

## 3. Non-Goals

Do not solve unrelated architecture items in this goal:

- no 30-stream/T4 pressure run;
- no Replay/Savant dual-path topology redesign;
- no source-adapter or Savant model chain change;
- no behavior-rule tuning except using existing event metadata for evidence
  status;
- no second RTSP pull;
- no downgrade of full-rate evidence storage;
- no acceptance of overlong clips as production evidence;
- no silent fallback to legacy DB annotations when post-Savant metadata is
  required.

## 4. Target Contract

### 4.1 Evidence status contract

Events and evidence tasks must expose these state families:

```text
pending          event-worker created the evidence task / record request
waiting_proof    clip-worker is waiting for post-Savant frame proof
queued           proof is ready but Replay concurrency is not available
replaying        Replay job has been created / video-file-sink is expected
finalizing       media-worker is building the evidence bundle
ready            production-ready evidence bundle exists
failed           terminal failure, with stable reason
```

If adding DB enum-like constraints is too broad, implement this as text fields
in existing payload/evidence task records, but the 8090 API must expose the
state in a stable shape.

Every non-ready state must include:

```text
status
reason
event_id
request_id
source_id
camera_id
retry_count or attempt_count
age_seconds
updated_at
```

### 4.2 Proof-wait contract

Post-Savant frame proof waiting must not consume Replay/concurrency capacity.

Valid behavior:

- wait locally for a bounded period for frame proof;
- mark event/evidence task as `waiting_proof`;
- retry proof lookup without marking the job as actively replaying;
- only enter the Replay concurrency gate after proof is selected.

Terminal proof failure must be explicit:

```text
status=failed
reason=missing_post_savant_frame_pts_window
proof_target=requested_end_pts
proof_window=requested_start_pts/requested_end_pts
runtime_epoch_id
stream_session_id
```

### 4.3 Replay concurrency contract

`CLIP_WORKER_MAX_CONCURRENT_JOBS` gates only the active Replay/finalization
portion, not proof waiting.

When concurrency is full:

- status becomes `queued`;
- request is not counted as a final retry failure just because another job is
  active;
- priority events (`watchlist_hit`, `live_search_hit`) keep priority behavior.

### 4.4 Overlay contract

For production evidence playback, bbox overlays must be frame-bound:

- prefer `frame_uuid` matched against final `sink_metadata.json`;
- then exact `frame_pts`;
- then nearest `frame_pts` only within a tight tolerance derived from the
  final metadata cadence;
- do not use `time_offset_ms` fallback for production bbox display unless the
  summary marks it explicitly safe;
- do not render the event bbox across the pre-event window;
- if a frame has no trustworthy annotation, render no bbox for that frame.

The viewer may still show diagnostic warnings, but production visual evidence
should prefer "no box" over "wrong box".

### 4.5 8090 operator contract

8090 must expose:

- latest events immediately, independent of clip readiness;
- per-event evidence state and reason;
- counts/rates for proof waits, proof failures, queued jobs, Replay jobs,
  finalization success/failure;
- recent terminal failures with source/camera/reason;
- link to evidence bundle only when `ready` and production-ready.

## 5. Implementation Plan

### Phase 0 - Baseline and Current-State Capture

No code changes in this phase.

Capture before numbers from the running stack:

```bash
curl --noproxy '*' -s http://127.0.0.1:8090/api/v1/runtime/overview | jq .
docker exec video-analytics-midterm-redis redis-cli XPENDING security.record_requests clip-workers-midterm
docker exec video-analytics-midterm-redis redis-cli XINFO GROUPS security.record_requests
docker logs --since 30m video-analytics-midterm-clip-worker
docker logs --since 30m video-analytics-midterm-event-worker
find /data/video-analytics/media/evidence -maxdepth 2 -type f -name summary.json -printf '%T@ %p\n' | sort -nr | head -20
```

Record:

- events per source;
- record requests per source;
- successful evidence bundles;
- `clip_worker_deferred`;
- `clip_worker_final_failed`;
- `missing_post_savant_frame_pts_window`;
- `max_concurrent_reached`;
- event-to-ready latency for the last 10 ready bundles;
- overlay summary fields for last 10 bundles.

Acceptance token:

```text
PASS_PHASE0_EVIDENCE_BASELINE_CAPTURED
```

### Phase 1 - Evidence Status Model and 8090 Visibility

Add stable evidence progress fields.

Implementation options:

- extend `services/clip-worker/app/repository.py::update_clip_status()` to
  write `payload.media.evidence_state`, `payload.media.evidence_reason`,
  `payload.media.evidence_state_updated_at`, and attempt counters;
- update `evidence_tasks.status` / `error_message` consistently where task id
  is available;
- extend API evidence detail/runtime overview to include status aggregates;
- update operator UI to display these states without needing log access.

Required states for this phase:

```text
waiting_proof
queued
replaying
finalizing
ready
failed
```

Tests:

```bash
pytest -q harness/tests/test_operator_runtime_overview_static.py
pytest -q harness/tests/test_api_event_evidence_camera_name.py
pytest -q harness/tests/test_clip_worker_queue_safety.py
```

Add or update tests for the new status fields.

Acceptance token:

```text
PASS_PHASE1_EVIDENCE_STATUS_VISIBLE_8090
```

### Phase 2 - Clip-worker Proof Wait Refactor

Refactor post-Savant proof waiting so it is a bounded local state, not a Redis
delivery-count side effect.

Required changes:

- add config for total proof wait budget, for example:

```text
POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S=12
POST_SAVANT_FRAME_PROOF_POLL_INTERVAL_S=0.5
```

- keep `POST_SAVANT_FRAME_PROOF_ATTEMPTS` compatible but stop relying on Redis
  redelivery as the main wait mechanism;
- set `waiting_proof` while waiting;
- log one structured final outcome per request;
- do not mark `retry_budget_exhausted` for a request that never reached Replay
  scheduling.

`missing_post_savant_frame_pts_window` should remain possible, but it should be
fast, explicit, and visible.

Tests:

```bash
pytest -q harness/tests/test_midterm_replay_epoch_isolation.py
pytest -q harness/tests/test_clip_worker_queue_safety.py
```

Add focused unit coverage for:

- proof succeeds after several polls without Redis redelivery;
- proof timeout produces `failed/missing_post_savant_frame_pts_window`;
- `missing_stream_session_id` remains terminal and is not retried.

Acceptance token:

```text
PASS_PHASE2_PROOF_WAIT_STATE_MACHINE
```

### Phase 3 - Replay Job Queue Semantics

Move active job concurrency gating after proof readiness.

Required changes:

- waiting-proof requests do not contribute to active Replay slots;
- concurrency-full requests become `queued`, not terminal failures;
- queued request age is visible;
- priority event bypass remains explicit and tested;
- avoid in-memory-only lost state on worker restart. Redis pending/reclaim must
  continue to recover requests.

Implementation note:

The current `active_jobs_until` approximation is time-based and not tied to
actual video-file-sink/media-worker completion. This phase may keep a bounded
time estimate if minimal, but it must no longer cause proof-waiting requests to
fail as `max_concurrent_reached`.

Tests:

```bash
pytest -q harness/tests/test_clip_worker_queue_safety.py
```

Add tests for:

- non-priority queued while one active job exists;
- queued request is not final-failed merely due to active job;
- priority request still proceeds.

Acceptance token:

```text
PASS_PHASE3_REPLAY_QUEUE_NO_FALSE_FAILURES
```

### Phase 4 - Overlay Frame-Identity Hardening

Make evidence overlay display frame-bound by default.

Backend sidecar requirements:

- ensure `annotations.frame_cache.identity.jsonl` rows include enough of:
  `frame_uuid`, `frame_pts`, `clip_frame_index`, `clip_frame_duration_ms`,
  `displayable`;
- add summary counters for exact vs fallback alignment;
- mark fallback rows non-displayable unless explicitly safe.

Frontend requirements:

- in `services/api/app/static/operator/evidence.js`, production overlay should
  render bbox only when annotation timing came from:
  `frame_uuid`, `frame_pts`, or `clip_frame_index`;
- disable or sharply limit `time_offset_ms` bbox display for production
  evidence;
- cap hold duration to frame cadence for sparse 8fps annotations, not several
  seconds;
- event bbox should display only near the event frame, not during pre-event
  seconds.

Mirror the same fix to `services/evidence-viewer/app/static/evidence.js` if
that standalone viewer is still used.

Tests:

```bash
pytest -q harness/tests/test_evidence_viewer_frame_identity_static.py
pytest -q harness/tests/test_evidence_viewer_alarm_machine_time.py
```

Add static tests for:

- time-offset-only bbox is not displayed in production mode;
- frame-uuid matched bbox is displayed;
- event bbox does not render at clip time 0 when event time is around 5s.

Acceptance token:

```text
PASS_PHASE4_FRAME_BOUND_OVERLAY
```

### Phase 5 - Runtime Validation

Run against the current two-source midterm stack.

Validation window: at least 10 minutes, or a shorter manual walk-through if the
user is actively testing in front of the camera.

Required checks:

```bash
curl --noproxy '*' -s http://127.0.0.1:8090/api/v1/runtime/overview | jq .
docker logs --since 10m video-analytics-midterm-clip-worker
docker exec video-analytics-midterm-redis redis-cli XPENDING security.record_requests clip-workers-midterm
find /data/video-analytics/media/evidence -maxdepth 2 -type f -name summary.json -printf '%T@ %h\n' | sort -nr | head -20
```

Pass criteria:

- new events appear in 8090 before evidence is ready;
- evidence state transitions are visible;
- no successful evidence bundle exposes overlong raw clips;
- for successful bundles, `time_domain_crop_applied=true`;
- event-to-ready latency p50 is materially lower than the observed tens of
  seconds baseline, or the remaining wait is visible as `waiting_proof` /
  `queued`;
- `missing_post_savant_frame_pts_window` failures are lower or at minimum
  immediately diagnosable by source/camera/session;
- overlay first seconds do not render event-frame bbox on unrelated pre-event
  frames.

Acceptance token:

```text
PASS_PHASE5_RUNTIME_EVIDENCE_STATUS_AND_ALIGNMENT
```

## 6. Verification Commands

Minimum local verification before each commit:

```bash
pytest -q harness/tests/test_clip_worker_queue_safety.py
pytest -q harness/tests/test_midterm_replay_epoch_isolation.py
pytest -q harness/tests/test_evidence_viewer_frame_identity_static.py
pytest -q harness/tests/test_operator_runtime_overview_static.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
git diff --check
```

Runtime verification after phases that affect containers:

```bash
docker compose -f infra/docker-compose.midterm.yml up -d --no-deps clip-worker media-worker api
curl --noproxy '*' -s http://127.0.0.1:8090/api/v1/runtime/overview | jq .
```

Do not restart Savant/source adapters unless a phase explicitly requires it.

## 7. Commit Discipline

Each phase must be committed after tests pass.

Suggested commit sequence:

1. `Add evidence status visibility contract`
2. `Refactor clip-worker proof wait state`
3. `Queue replay jobs after proof readiness`
4. `Harden evidence overlay frame matching`
5. `Document runtime validation results`

Do not combine frontend overlay hardening with clip-worker state-machine
changes in one commit unless the diff is very small.

## 8. Rollback

Rollback must be phase-local:

- Phase 1 rollback: hide new 8090 fields but keep DB payload backward
  compatible.
- Phase 2 rollback: restore old proof retry behavior while preserving failure
  visibility.
- Phase 3 rollback: set `CLIP_WORKER_MAX_CONCURRENT_JOBS=1` and restore old
  gating, but keep terminal reasons visible.
- Phase 4 rollback: disable production overlay display rather than reverting to
  time-offset-only bbox display.

If runtime evidence becomes worse, prefer disabling bbox overlay while keeping
raw clips and status visibility.

## 9. Goal Command

Use this as the next goal objective:

```text
以 specs/17_clip_worker_evidence_realtime_alignment_fix.md 为目标执行修复：
按 Phase 0-5 逐步实现 clip-worker 证据状态机、proof 等待重构、Replay 队列语义、
8090 状态展示和 evidence overlay frame-bound 对齐；每个 phase 修改后运行对应测试、
git diff --check、必要的 compose config/runtime 验证，并在通过后提交一个原子 commit。
不得破坏当前可运行的 midterm 栈，不得重新拉 RTSP，不得降低 full-rate evidence 保存。
```
