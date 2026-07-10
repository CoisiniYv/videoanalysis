# Midterm Evidence Bypass Diagnosis - 2026-07-06

Scope: side-channel diagnosis while another terminal may be running pressure
tests. No containers were restarted/stopped/recreated by this inspection. No
Redis writes, PostgreSQL writes, media cleanup, or service reloads were run.

## 1. Safe Commands Used

Read-only probes only:

```text
docker ps
docker logs --since ... --tail ...
docker exec <container> printenv
docker exec video-analytics-midterm-redis redis-cli XLEN/XINFO/XREVRANGE/SCAN
docker exec phase0-postgres psql -U video -d video_analytics -c SELECT...
```

## 2. Runtime Snapshot

Initial `docker ps` showed workers plus Replay shards `replay-c` through
`replay-h`, `rolling-cache-sink-a`, and `rolling-cache-sink-b` up. During the
read-only inspection the other terminal appears to have stopped/recycled parts
of the pressure topology: a later `docker ps` showed only the base midterm
chain up:

```text
video-analytics-midterm-media-worker
video-analytics-midterm-event-worker
video-analytics-midterm-clip-worker
video-analytics-midterm-video-file-sink
video-analytics-midterm-savant
video-analytics-midterm-analysis-forwarder
video-analytics-midterm-redis
video-analytics-midterm-replay-service
phase0-postgres
```

No pressure source containers were visible in the later snapshot.

## 3. Current Evidence Path Distribution

PostgreSQL was empty at the time of inspection:

```text
events=0
evidence_tasks=0
evidence_bundles=0
max_event_created=NULL
max_task_created=NULL
max_bundle_updated=NULL
```

Therefore the live DB cannot currently prove a distribution of
`rolling_cache_copy` vs Replay fallback vs expired evidence. The absence of rows
is itself important: any active pressure report that expects DB evidence must
not be judged from this database state unless the run intentionally cleaned
runtime rows before inspection.

Redis snapshot:

```text
security.record_requests XLEN = 6404
security.record_requests group clip-workers-midterm pending = 0, lag = 0
security.events XLEN = 7827
security.events group event-workers-midterm pending = 0, lag = 0
security.frame_annotations XLEN = 121
security.person_observations XLEN = 6
source-scoped frame annotation stream keys = 70
record_request dedupe keys = 0
```

Interpretation:

- Redis consumer lag is not the immediate blocker in this snapshot.
- `security.record_requests` contains historical entries, but they are not
  pending for the clip-worker group. Some sampled record requests reference DB
  `event_id` values that no longer exist because PostgreSQL is empty.
- The active database evidence state has been cleaned or not yet populated, so
  path distribution must be captured during a run before cleanup.

## 4. Runtime Config Findings

The current worker containers are not running the high-density rolling-cache
profile proposed in `specs/31_midterm_evidence_architecture_remediation_plan.md`.

event-worker:

```text
ROLLING_CACHE_ENABLED=false
ROLLING_CACHE_MATERIALIZATION_ENABLED=false
ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=false
EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED=false
RECORDING_ENABLED=true
RECORDING_STRATEGY=savant_replay
```

media-worker:

```text
ROLLING_CACHE_ENABLED=false
ROLLING_CACHE_MATERIALIZATION_ENABLED=false
EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED=false
FRAME_CACHE_SIDECAR_SOURCE_STREAM_ENABLED=false
EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE=1
MEDIA_WORKER_FINALIZER_WORKERS=32
MEDIA_WORKER_FINALIZER_MAX_PER_SOURCE_PER_POLL=4
```

clip-worker:

```text
REPLAY_SHARDS_JSON=
REPLAY_SHARDS_CONFIG_PATH=
replay_shards_enabled=False
EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE=1
FRAME_ANNOTATION_STREAM=security.frame_annotations
POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S=3
CLIP_WORKER_PENDING_CLAIM_MIN_IDLE_MS=5000
```

This means the current base runtime is still configured as a Replay-first /
global-frame-annotation path, not a rolling-cache-first / coverage-merge /
source-scoped-annotation path.

## 5. Main Blocker Judgment

Based on this safe snapshot, the strongest current blocker is configuration and
runtime path mismatch, not Redis or PostgreSQL backlog:

1. The database has no evidence rows to materialize or inspect.
2. Redis consumer groups show `pending=0` and `lag=0`.
3. The worker environment has rolling cache materialization disabled.
4. Coverage merge is disabled.
5. Source-scoped frame annotation lookup is disabled in media-worker.
6. Replay shard routing is disabled in clip-worker despite the earlier snapshot
   briefly showing multiple Replay shard containers.
7. Per-source evidence concurrency is still `1`, so any shared or collapsed
   `source_id` will serialize Replay evidence.

Therefore, if the adjacent pressure run was expected to validate the new
high-density evidence architecture, it likely did not do so with the currently
running worker configuration.

## 6. Post-Pressure Optimization Checklist

After the other terminal finishes and before the next pressure run:

1. Capture evidence path distribution before cleanup:
   - DB counts by `evidence_tasks.materialization_status`;
   - DB counts by `evidence_bundles.materialization->>'materialization_mode'`;
   - `evidence_event_links` covered alias count;
   - active Replay slots;
   - unique `source_id` count.
2. Enable a named high-density profile rather than ad hoc flags:
   - `EVIDENCE_DENSITY_PROFILE=high_density`;
   - `ROLLING_CACHE_ENABLED=true`;
   - `ROLLING_CACHE_MATERIALIZATION_ENABLED=true`;
   - `ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=true`;
   - `EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED=true`.
3. Make source identity an explicit pressure acceptance gate:
   - `unique_source_count == 60`;
   - every source has camera id, shard id, rolling-cache stream/dir, and runtime
     epoch.
4. Enable source-scoped annotation lookup:
   - `FRAME_ANNOTATION_SOURCE_STREAM_ENABLED=true`;
   - `FRAME_CACHE_SIDECAR_SOURCE_STREAM_ENABLED=true`;
   - clip-worker proof should prefer `security.frame_annotations.{source_id}`.
5. Verify Replay fallback is an exception:
   - fallback count and reason must be reported;
   - record requests should not dominate in high-density mode.
6. Keep raw clip readiness separate from annotation readiness:
   - playable raw clips should be indexed first;
   - annotation can complete later or be marked partial/degraded.
7. Re-run deterministic and live 60-source pressure only after the above config
   is visible inside the worker containers, not just in env files.

## 7. Suggested Acceptance Evidence For Next Run

The next run should not be accepted without a pressure artifact containing:

```text
unique_source_count == 60
ROLLING_CACHE_MATERIALIZATION_ENABLED=true inside media-worker
ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=true inside event-worker
EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED=true inside event-worker/media-worker
FRAME_CACHE_SIDECAR_SOURCE_STREAM_ENABLED=true inside media-worker
rolling_cache_hit_rate
Replay fallback count by reason
physical bundle count
covered alias count
playable event coverage
materialization_expired count
active_evidence_tasks_after=0
active_replay_slots_after=0
pressure_source_containers_after=0
```

## 8. Follow-up Artifact: `readyat_cd60_drain120_w60_sharedfix_20260706T145959Z`

After reading the handoff attachment and the pressure artifact under
`/data/video-analytics/artifacts/readyat_cd60_drain120_w60_sharedfix_20260706T145959Z`,
the diagnosis needs an important correction: that completed run did not fail
because retained evidence could not be materialized. It failed pressure gates
upstream of evidence generation.

Artifact `report.json`:

```text
status=failed_pressure_gates
failure_reasons=savant_send_failures,forwarder_queue_full,validate_seq_iq_exceeded
cameras=60
events=170
evidence_tasks=103
evidence_bundles=103
playable_bundles=103
active_materialization_tasks=0
task_status=materialized/materialized=103
covered_events=0
distinct_events_without_playable_evidence=67
```

The 67 events without playable evidence are terminal non-playable outcomes from
the run's policy/cooldown path, not stuck materialization tasks.

Artifact `downstream_observability_summary.json`:

```text
replay_topology=dual_shard
REPLAY_SHARDS_JSON_present=true
replay_slot_acquired=0
replay_slot_released=0
rolling_cache ready_to_claim p50=8.717s p95=68.323s max=77.050s
rolling_cache claim_to_materialized p50=1.181s p95=5.564s max=9.751s
evidence task lifecycle p50=2.134s p95=27.415s p99=34.436s
8090 checked=103 ok=103
8090 annotations checked=32 ok=32
annotation_skipped_zero_expected_count=71
```

Interpretation:

- Evidence materialization for retained evidence passed: `103/103` tasks became
  materialized and `103/103` bundles were playable.
- Replay was not the active export path in this run: `replay_slot_acquired=0`
  and live DB later showed `materialization_mode=rolling_cache_copy` for all
  retained bundles.
- Raw clip availability is no longer the blocker for this artifact.
- Annotation completeness is still a quality gap: only `32/103` retained
  bundles had positive annotations; `71/103` were marked
  `missing_frame_metadata` in the live database snapshot.

Artifact `pressure_diagnostics.json` shows the actual failing side:

```text
max_forwarder_sources=60
max_savant_sources=60
final_forwarder_frames_seen_total=149311
final_forwarder_frames_forwarded_total=80576
final_forwarder_frames_dropped_total=68735
final_forwarder_forwarded_seen_ratio=0.5397
final_forwarded_target_ratio=0.4197
max_forwarder_queue_depth=4096
queue_full_samples=10
max_savant_send_failures_total=60
savant validate_seq_iq=27160
analysis_forwarder writer_send_timeout=61
stable_samples=0
rtsp_republishers.enabled=false
```

Therefore the strongest artifact-backed blocker is upstream
forwarder/Savant pressure, not DB backlog, Redis consumer lag, Replay admission,
or media-worker finalization.

## 9. Current Live Read-only State After Artifact Restore

The live containers were inspected read-only. No restart, stop, recreate,
Redis write, PostgreSQL write, or media cleanup was done.

Current `docker ps` shows only the base chain up; pressure source containers and
extra Replay/sink shards are no longer running. The worker environments are
back to the base non-high-density settings:

```text
event-worker ROLLING_CACHE_MATERIALIZATION_ENABLED=false
event-worker ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=false
event-worker EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED=false
media-worker ROLLING_CACHE_MATERIALIZATION_ENABLED=false
media-worker FRAME_CACHE_SIDECAR_SOURCE_STREAM_ENABLED=false
media-worker EVIDENCE_TOPOLOGY=post_savant_replay
clip-worker REPLAY_SHARDS_JSON=
clip-worker REPLAY_SHARDS_CONFIG_PATH=
```

Current PostgreSQL read-only counts:

```text
events=103
distinct event source_id=54
evidence_tasks=103 materialized/materialized
evidence_bundles=103
bundle materialization_mode/evidence_state=rolling_cache_copy/generated_unverified=103
raw_clip_uri available=103/103
annotation_count > 0 = 32/103
annotation_status complete=32
annotation_status missing_frame_metadata=71
evidence_event_links=0
active_evidence_tasks=0
active_replay_slots=0
```

Current Redis read-only state remains clean for the relevant consumers:

```text
security.record_requests group clip-workers-midterm pending=0 lag=0
security.events group event-workers-midterm pending=0 lag=0
security.frame_annotations XLEN=7
```

Two caveats matter:

- The artifact recorded `cameras=60`, but the restored live DB currently has
  retained events for only 54 distinct `source_id` values. Do not use the live
  retained DB alone to claim that all 60 sources had retained evidence.
- The final successful evidence-side run used one fixed RTSP publisher with 60
  source adapters subscribing to the same video. That proves the 60-source
  inference/evidence path better than the earlier broken visibility harness, but
  it does not prove 60 independent RTSP publishers are stable.

## 10. Updated Blocker Split

Use this split for the next optimization pass:

1. Evidence materialization: passed for retained evidence in the verified
   artifact (`103/103` rolling-cache bundles, active tasks `0`).
2. Evidence annotation quality: not passed yet (`32/103` complete,
   `71/103` missing frame metadata).
3. Upstream pressure gates: failed (`forwarder_queue_full`,
   `savant_send_failures`, `validate_seq_iq_exceeded`, forwarded/seen ratio
   `0.5397`).
4. Runtime profile drift: base runtime after restore is not high-density; future
   runs must capture worker env from inside containers at run time.
5. Source identity: artifact cameras were 60, but retained-event source count
   after restore is 54, so reports must separately track configured cameras,
   visible sources, sources with events, and sources with retained evidence.

## 11. Post-pressure Optimization Changes To Prepare

Once the other pressure run has finished and runtime changes are allowed, the
next execution order should be:

1. Add/extend pressure report fields that separate evidence materialization,
   annotation completeness, and upstream pressure gates. A run where
   `103/103` evidence is playable but annotations are partial must not be
   labeled as evidence-save failure.
2. Make upstream pressure the next tuning target when evidence materialization
   passes: forwarder queue/backpressure policy, Savant send failure handling,
   source visibility stability, and validation of the harness restart rule that
   only restarts sources missing from the forwarder.
3. Promote source-scoped annotation lookup from optional to acceptance evidence:
   record `source_stream_used`, fallback count, entries scanned, source filtered
   count, epoch filtered count, and stream-session filtered count.
4. Keep raw clip readiness independent from annotation status in 8090: playable
   raw clips should remain available as `generated_unverified` or degraded when
   frame metadata is missing.
5. Preserve rolling-cache-first as the high-density evidence main path, but make
   its success criteria explicit: rolling-cache hit rate, Replay fallback count,
   fallback reasons, physical bundle count, covered alias count, and playable
   event coverage.
6. Run two separate acceptance profiles: shared-RTSP deterministic 60-source for
   evidence-path isolation, then 60 independent RTSP ingress for real camera
   ingress stability.
