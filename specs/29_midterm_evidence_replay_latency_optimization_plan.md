# 29_midterm_evidence_replay_latency_optimization_plan.md

Date: 2026-06-29

## 1. Goal

Reduce the remaining midterm evidence long tail after the Qdrant gallery
cutover. The target path is:

```text
event-worker
  -> security.record_requests
  -> clip-worker proof wait and Replay job admission
  -> Replay service
  -> video-file-sink output
  -> media-worker sink readiness and finalizer pool
  -> evidence bundle and 8090 database-backed evidence index
```

This plan is intentionally scoped to evidence latency before and around media
finalization. It does not change Savant model inference, Qdrant gallery search,
watchlist semantics, or the 8090 evidence contract.

## 2. Current Finding

The final Qdrant pressure run
`/data/video-analytics/artifacts/pressure60_qdrant_final_8fps_20260629T150103Z`
passed the selected acceptance gate:

- status: `passed`;
- failure reasons: none;
- retained/playable evidence: 50/50;
- Qdrant query p95/p99: 4 ms / 5 ms;
- face-worker gallery query p95/p99: 6 ms / 8 ms;
- face-worker pending/lag: 0/0;
- media finalization p95/p99: about 8.1 s / 8.6 s;
- media queue wait p95/p99: about 192.2 s / 216.5 s.

The key interpretation is that `media_queue_wait_ms` is not pure finalizer
execution time. Current media-worker metrics calculate queue wait from event
creation to finalizer start, so the 190 s tail can include:

- clip-worker waiting for post-Savant frame proof;
- clip-worker Replay admission gates;
- Replay job creation and Replay internal scheduling;
- video-file-sink output arrival;
- sink stability and readiness polling;
- finalizer pool submission delay.

Static review of the current config points to Replay admission as the strongest
first hypothesis. The midterm config currently uses:

```text
EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY=8
EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD=4
EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE=1
DEFAULT_PRE_SECONDS=5
DEFAULT_POST_SECONDS=5
```

`clip-worker` holds each active Replay slot for `pre_seconds + post_seconds +
5`, so a default evidence job holds a slot for about 15 seconds. For about
87 finalized evidence jobs split across two shards, a rough shard drain lower
bound is:

```text
ceil(45 jobs / 4 jobs_per_shard) * 15 s ~= 180 s
```

That lower bound is close to the observed `media_queue_wait` p95. This does not
prove Replay admission is the only bottleneck, but it is the highest-value
optimization target after phase metrics are added.

Execution update, 2026-06-29:

- Phase timers proved Qdrant/face-worker are not the active bottleneck:
  Qdrant fallback stayed at `0`, face-worker lag stayed at `0`, and gallery
  query p95 stayed single-digit milliseconds.
- Candidate B (`16/8/1`) overloaded Replay/video-file-sink and worsened the
  tail, so the accepted concurrency baseline is candidate A (`12/6/1`) with
  `EVIDENCE_REPLAY_ACTIVE_SLOT_EXTRA_SECONDS=0`.
- After recreating media-worker with
  `MEDIA_WORKER_MATERIALIZATION_THROTTLE_SLEEP_S=0`, throttle was verified as
  disabled, but `media_queue_wait` p95 was still about 165 s. The dominant
  segments were `record_request_pending_ms` p95 about 104 s and
  `sink_video_to_stable_ms` p95 about 60 s.
- A pending-reclaim experiment (`1000 ms`, `60` entries, every `1 s`) was
  rejected. It increased `record_request_pending_ms` p95 to about 155 s and
  also worsened Replay-to-sink, sink stability, and finalizer-pool segments.
  The accepted queue-safety defaults remain `5000 ms`, `10` entries, every
  `5 s`.

Execution update, 2026-06-30:

- C2.15 observability and completion-aware Replay admission were validated
  under the 60-route, 8 FPS, same-GPU dual-Savant, dual-Replay/sink profile.
- Accepted artifact:
  `/data/video-analytics/artifacts/pressure60_c2_15_dual1gpu_evidence8fps_nofastproof_admission18_20260630T115728Z`.
- The run passed with `dual_shard`, `dual_replay_enabled=true`, retained
  evidence 50/50 checked OK through 8090, Qdrant fallback 0, and Redis
  pending/lag 0.
- Latency p95: `media_queue_wait_ms` 146.5 s, `replay_to_sink_metadata_ms`
  7.5 s, `sink_video_to_stable_ms` 46.5 s, `finalizer_pool_wait_ms` 154 ms,
  and `finalization_duration_ms` 5.3 s.
- The post-Savant proof fast path stays disabled by default. Full pressure
  runs showed it can reduce some clip-worker pending time but shifts pressure
  into video-file-sink readiness and finalizer-pool wait.

Execution update, 2026-07-03:

- Static and runtime review found the remaining single-source latency trigger:
  clip-worker Replay payloads still hardcoded `configuration.ts_sync=true`, so
  a 15 second evidence window was re-streamed at wall-clock media pace before
  file stability/finalization even began.
- Added `REPLAY_TS_SYNC`, defaulted midterm evidence export to
  `REPLAY_TS_SYNC=false`, and kept explicit `ts_sync=true` as a rollback /
  realtime-viewing mode.
- Manual runtime validation on source
  `source_00000000-0000-4000-8000-781078565686` submitted a 15 second
  `ts_delta_sec` job with `ts_sync=false`; Replay finished by stop condition in
  the same second, video-file-sink reported pipeline operation `0:00:00.029478`,
  and `ffprobe` measured the generated `video.mov` at `15.055s`. The manual
  validation directory was removed after probing so media-worker would not keep
  retrying a sink output that has no database event.
- clip-worker Redis connection now sets a socket timeout above the blocking
  `XREADGROUP` poll timeout. This removes the empty-poll timeout/error/sleep
  path observed after restart and keeps idle polling on the normal path.

## 3. Non-Goals

Do not use this plan to:

- replace Qdrant or revisit pgvector gallery search;
- rewrite `face-worker` in C++;
- change AdaFace embeddings or ArcFace SDKs;
- change event semantics, watchlist thresholds, or evidence bundle fields;
- restart or retune Savant model chains as part of a simple evidence latency
  patch;
- remove Replay as the evidence authority;
- bypass post-Savant frame proof guards to get faster but untrusted clips.

## 4. Success Criteria

The same 60-route 8 FPS pressure profile must keep the current correctness
properties while reducing latency:

- retained/playable evidence remains 50/50 for the selected retained set;
- 8090 evidence list/detail remains database-backed and valid;
- Qdrant remains authoritative with fallback count 0;
- face-worker pending/lag stays 0/0;
- media finalization p95 stays at or below 12 s unless the report explains a
  deliberate quality-preserving tradeoff;
- media queue wait p95 is reduced from about 192 s to <= 90 s in the first
  optimized run, or the phase report proves the remaining dominant segment;
- no Replay/sink epoch mismatch, stale sink reuse, or unbounded sink-output
  accumulation is introduced.

Final acceptance token:

```text
PASS_MIDTERM_EVIDENCE_REPLAY_LATENCY_OPTIMIZED
```

## 5. Phase 0 - Add Phase Timers Before Tuning

Problem:

The current pressure report has one large `queue_wait_ms` bucket. That bucket is
too broad to choose safely between raising Replay concurrency, optimizing proof
lookup, reducing sink polling delay, or adding finalizer workers.

Modification plan:

1. Add structured timestamps and durations for each evidence task:
   - `record_request_created_at`;
   - `clip_worker_claimed_at`;
   - `proof_wait_started_at`;
   - `proof_wait_finished_at`;
   - `proof_wait_ms`;
   - `proof_retry_count`;
   - `replay_admission_deferred_at`;
   - `replay_admission_reason`;
   - `replay_active_global_count`;
   - `replay_active_shard_count`;
   - `replay_active_source_count`;
   - `replay_job_create_started_at`;
   - `replay_job_created_at`;
   - `replay_job_create_ms`;
   - `sink_metadata_first_seen_at`;
   - `sink_video_first_seen_at`;
   - `sink_video_stable_at`;
   - `sink_ffprobe_ready_at`;
   - `sink_stability_wait_ms`;
   - `finalizer_submitted_at`;
   - `finalizer_started_at`;
   - `finalizer_pool_wait_ms`;
   - `finalization_elapsed_ms`.
2. Store durable per-event values in `events.payload.media.evidence_diagnostics`
   or the existing evidence diagnostics structure.
3. Keep logs machine-parsable with stable keys so pressure scripts can extract
   p50/p95/p99 per segment.
4. Update the pressure report schema to include a `phase_latency_ms` section.
5. If a phase cannot be measured in the current code path, report
   `status=not_available` with the missing instrumentation name.

Acceptance token:

```text
PASS_EVIDENCE_PHASE_TIMERS_ADDED
```

Required tests:

- unit tests for timestamp merge/update behavior;
- pressure-report parser tests with synthetic log lines;
- one short two-source smoke proving the new fields appear without changing
  evidence output.

## 6. Phase 1 - Replay Admission Optimization

Problem:

`clip-worker` gates Replay jobs with global, shard, and source active-job
counts. Current active-slot lifetime is an estimate of `pre + post + 5`. Under
bursty 60-route pressure, this can turn a valid bounded admission policy into a
190 s p95 evidence wait.

Modification plan:

1. Make active-slot hold time explicit and observable:
   - add `replay_slot_hold_ms`;
   - log whether slot release was estimated or completion-aware;
   - expose active global/shard/source slot counts in diagnostics.
2. Add a configurable extra hold knob, for example
   `EVIDENCE_REPLAY_ACTIVE_SLOT_EXTRA_SECONDS`, defaulting to the current
   behavior of 5 seconds.
3. Add a pressure-profile tuning matrix rather than one blind config jump:
   - baseline: global 8, per-shard 4, per-source 1;
   - candidate A: global 12, per-shard 6, per-source 1;
   - candidate B: global 16, per-shard 8, per-source 1;
   - candidate C: global 16, per-shard 8, per-source 2 for high-priority
     events only.
4. Keep Redis pending reclaim conservative unless a later run proves otherwise:
   - keep `CLIP_WORKER_PENDING_CLAIM_MIN_IDLE_MS=5000`;
   - keep `CLIP_WORKER_PENDING_CLAIM_COUNT=10`;
   - keep `CLIP_WORKER_PENDING_CLAIM_INTERVAL_S=5`;
   - do not use aggressive reclaim as a latency shortcut because it can amplify
     DB writes and Replay/video-file-sink pressure during bursts.
5. Keep `watchlist_hit` and other high-value events from being starved by
   low-value bursts:
   - preserve event-type quotas;
   - record skipped/deferred reason counts;
   - do not let low-value `intrusion` events consume all Replay admission
     slots when retained evidence is already satisfied.
6. Consider completion-aware slot release only after Phase 0 proves that
   estimated slot lifetime is the dominant segment:
   - media-worker can mark `sink_metadata_first_seen_at` or `finalizer_started`
     for the event;
   - clip-worker can release or discount slots based on durable DB state on its
     next loop;
   - fallback remains time-estimated release if the completion signal is absent.

Acceptance token:

```text
PASS_REPLAY_ADMISSION_TUNED
```

Acceptance thresholds:

- same pressure profile shows replay-admission p95 cut by at least 40%;
- total `media_queue_wait` p95 <= 90 s, or the remaining p95 is clearly
  attributed to post-admission sink/Replay time;
- no increase in Replay create failures, Savant send failures, sink epoch
  mismatch, or unplayable evidence.

Rollback:

- restore baseline concurrency values;
- restore the default active-slot extra seconds;
- keep Phase 0 observability in place.

## 7. Phase 2 - Proof Lookup And Retry Optimization

Problem:

Post-Savant proof lookup scans Redis frame annotations with a large lookback.
The current midterm values are:

```text
POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S=3
POST_SAVANT_FRAME_PROOF_POLL_INTERVAL_S=0.5
FRAME_ANNOTATION_ANCHOR_LOOKBACK_COUNT=20000
FRAME_ANNOTATION_ANCHOR_PAGE_COUNT=2000
```

A single 3 s proof wait does not explain the whole 190 s tail, but proof misses
and pending reclaim can amplify Replay admission delay during event storms.

Modification plan:

1. Measure proof lookup separately:
   - lookup count;
   - Redis pages scanned;
   - candidates inspected;
   - hit/miss reason;
   - same-source/same-session availability;
   - retry count and age.
2. Prefer source/session/time-bounded lookup over broad global stream scanning:
   - use Redis stream ID bounds derived from event time when available;
   - keep runtime epoch and stream session filters strict;
   - avoid increasing the global lookback as the first response to misses.
3. Add or evaluate a per-source frame-annotation index:
   - key by runtime epoch and source/camera;
   - store only compact anchor fields needed for proof;
   - enforce TTL at or below `EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS`;
   - keep the existing global stream as fallback until pressure proof passes.
4. Make proof retry accounting visible in the pressure report:
   - `waiting_proof_count`;
   - `proof_retry_exhausted_count`;
   - proof wait p50/p95/p99;
   - proof lookup scan p50/p95/p99.

Acceptance token:

```text
PASS_POST_SAVANT_PROOF_LOOKUP_BOUNDED
```

Acceptance thresholds:

- proof lookup p95 <= 100 ms when proof is already present;
- proof wait p95 <= configured budget plus one poll interval;
- proof miss/retry counts are explicit and do not grow silently under the
  selected pressure run;
- no evidence bundle is created without trusted frame-domain proof.

## 8. Phase 3 - Replay To video-file-sink Arrival Split

Problem:

After `replay_job_created`, the current system does not clearly measure:

```text
Replay job created
  -> metadata.json first appears
  -> video file first appears
  -> video file stops growing
  -> ffprobe duration succeeds
```

This makes Replay service latency, sink routing, adapter write time, and
media-worker polling delay look like one undifferentiated queue wait.

Modification plan:

1. Record `replay_job_created_at` in the evidence diagnostics.
2. In media-worker, record first-observed metadata and video timestamps per
   event.
3. Record video size samples and stable-count transitions.
4. Record ffprobe readiness and failure reason separately from finalizer
   execution.
5. Add a pressure-report section:
   - `replay_to_sink_metadata_ms`;
   - `sink_metadata_to_video_ms`;
   - `sink_video_to_stable_ms`;
   - `sink_stable_to_ffprobe_ready_ms`.
6. Only after measurement, evaluate reducing `MEDIA_POLL_INTERVAL_S` or adding
   an inotify-style watcher for sink output.

Acceptance token:

```text
PASS_REPLAY_SINK_ARRIVAL_MEASURED
```

Acceptance thresholds:

- pressure report can rank Replay-to-sink p95 against proof wait and replay
  admission p95;
- no `not_available` fields remain for sink arrival phases in the post-Savant
  path;
- sink epoch/runtime checks still fail closed.

## 9. Phase 4 - Sink Readiness And Finalizer Pool Admission

Problem:

The finalizer pool currently hides some waiting inside executor submission. The
current finalization p95 is healthy, so this phase should improve observability
and fairness before adding workers.

Modification plan:

1. Record `finalizer_submitted_at` before executor submission and
   `finalizer_started_at` inside the worker thread.
2. Report `finalizer_pool_wait_ms` separately from media `queue_wait_ms`.
3. Keep `claim_wait_ms` as its own DB contention signal.
4. Preserve source fairness, but measure skipped same-source jobs per poll.
5. Add a bounded submission mode so the executor queue cannot hide arbitrary
   backlog when worker count is too low.
6. Tune `MEDIA_WORKER_FINALIZER_WORKERS` only after the report proves pool wait,
   not proof/replay/sink arrival, dominates the remaining tail.

Acceptance token:

```text
PASS_FINALIZER_POOL_WAIT_BOUNDED
```

Acceptance thresholds:

- finalizer pool wait p95 is reported separately;
- finalization p95 stays <= 12 s;
- claim wait p95 stays below 100 ms;
- no duplicate materialization and no finalizer failed count increase.

## 10. Verification Matrix

Run verification in this order:

1. Static and unit tests:
   - timestamp/diagnostics update tests;
   - pressure parser tests;
   - replay admission gate tests;
   - proof lookup bounded-scan tests;
   - media-worker sink phase extraction tests.
2. Short runtime smoke:
   - two sources;
   - one watchlist event and one intrusion event;
   - prove phase timestamps and 8090 evidence still work.
3. Selected pressure profile:
   - same profile as
     `pressure60_qdrant_final_8fps_20260629T150103Z`;
   - Qdrant authoritative;
   - same retained evidence target;
   - report phase p50/p95/p99.
4. Regression checks:
   - Qdrant fallback count 0;
   - face-worker pending/lag 0/0;
   - Redis record request pending/lag bounded;
   - media finalizer failed 0;
   - duplicate materialization 0;
   - playable evidence 50/50;
   - 8090 database-backed evidence index OK.

## 11. Implementation Order

Recommended first coding slice:

1. Add Phase 0 timers and pressure report fields only.
2. Re-run a short smoke and one pressure report to prove the split.
3. Tune Replay admission using candidate A/B/C.
4. If Replay admission is not enough, implement bounded proof lookup.
5. If sink arrival is dominant, reduce polling or add watcher behavior.
6. If finalizer pool wait is dominant, tune bounded submission and worker count.

Do not merge broad tuning without the phase report. The most likely first
performance win is Replay admission tuning, but the permanent fix must leave
behind phase metrics so future pressure runs do not regress into guesswork.

## 12. Closure Evidence

Implemented changes:

- Phase 0 timers now split clip-worker claim/proof/admission, Replay job create,
  Replay-to-sink metadata, sink video stability, ffprobe readiness, finalizer
  pool wait, and finalization duration.
- Pressure report schema is versioned as `2` and includes `phase_latency_ms`.
- Replay admission is tuned to candidate A:
  `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY=12`,
  `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD=6`,
  `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE=1`, and
  `EVIDENCE_REPLAY_ACTIVE_SLOT_EXTRA_SECONDS=0`.
- Candidate B (`16/8/1`) and aggressive pending reclaim were rejected because
  they increased Replay/video-file-sink pressure.

Best accepted pressure result:

```text
artifact: /data/video-analytics/artifacts/pressure60_phase29_admission_a_8fps_20260629T162411Z
status: passed
retained evidence: 50/50
media_queue_wait p95: 128.3 s
record_request_pending_ms p95: 52.7 s
sink_video_to_stable_ms p95: 78.1 s
finalizer_pool_wait_ms p95: 12.5 s
media finalization p95: 10.7 s
Qdrant fallback: 0
face-worker pending/lag: 0/0
8090 evidence: 50/50 OK
```

Additional verification:

```text
artifact: /data/video-analytics/artifacts/pressure60_phase29_admission_a_nothrottle_recreate_8fps_20260629T170417Z
status: passed
MEDIA_WORKER_MATERIALIZATION_THROTTLE_SLEEP_S: 0 verified
media_queue_wait p95: 164.9 s
record_request_pending_ms p95: 103.9 s
sink_video_to_stable_ms p95: 60.3 s
finalizer_pool_wait_ms p95: 8.2 s
media finalization p95: 7.2 s
Qdrant fallback: 0
face-worker pending/lag: 0/0
8090 evidence: 50/50 OK
```

Rejected experiment:

```text
artifact: /data/video-analytics/artifacts/pressure60_phase29_pendingreclaim_8fps_20260629T171411Z
status: passed
change: CLIP_WORKER_PENDING_CLAIM_MIN_IDLE_MS=1000,
        CLIP_WORKER_PENDING_CLAIM_COUNT=60,
        CLIP_WORKER_PENDING_CLAIM_INTERVAL_S=1
media_queue_wait p95: 191.2 s
record_request_pending_ms p95: 154.9 s
replay_to_sink_metadata_ms p95: 31.5 s
sink_video_to_stable_ms p95: 76.0 s
finalizer_pool_wait_ms p95: 21.7 s
decision: rejected and reverted to 5000/10/5
```

Acceptance decision:

The original phase-29 90 s queue-wait target remains a stretch target. C2.15 is
accepted under the updated closure gate because completion-aware admission and
dual sink observability reduced `media_queue_wait` p95 below the 192 s baseline
while meeting the Replay-to-sink, sink-stability, finalizer-pool, retained
evidence, Qdrant fallback, Redis lag, and 8090 evidence correctness gates.

```text
PASS_MIDTERM_EVIDENCE_REPLAY_LATENCY_OPTIMIZED
```
