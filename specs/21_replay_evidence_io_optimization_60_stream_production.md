# 21_replay_evidence_io_optimization_60_stream_production.md

Date: 2026-06-20

## 1. Goal

Make the evidence media path safe for the future 60 RTSP / 2 GPU shard target
by bounding intermediate `replay-sink-output`, measuring and reducing
media-worker materialization cost, and keeping evidence generation under
explicit shard/concurrency/storage quotas.

This spec is a storage and media-materialization optimization plan for the
topology already defined by:

- `specs/16_dual_path_30x2_t4_production_optimization.md`
- `specs/20_dual_4090_as_t4_two_source_validation.md`

It does not replace the dual-path design. Replay remains the full-rate evidence
authority, while `analysis-forwarder` samples or drops only the analysis branch.

Revision 1, 2026-06-20: this spec was corrected after reviewing the current
post-Savant evidence path. The main current cost is not a second full-file copy
from sink output to evidence. With `EVIDENCE_TOPOLOGY=post_savant_replay` and
`FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED=true`, media-worker crops the Replay sink
output with `ffmpeg -c:v libx264` and writes `raw_clip.mov` directly into the
evidence bundle. The optimization plan must therefore measure and attack
per-event transcode CPU and bounded intermediate sink output, not only file-copy
write amplification.

Revision 2, 2026-06-23: the latest readiness review keeps evidence
materialization as a release-blocking workstream for 60 streams. The reported
50-event audit showed materialization queue wait around 38.3s average / 42.6s
p95, lifecycle p95 around 145.4s, and ffmpeg child CPU around 13.07s per clip.
The latest local Phase 2+ report shows the same bottleneck shape with queue
depth 115, queue wait 44.427s average / 52.793s p95, and ffmpeg child CPU
13.326s average / 16.102s p95. See
`specs/22_midterm_60_stream_readiness_risk_closure_plan.md`.

Target steady-state chain:

```text
RTSP source
  -> Replay shard full-rate storage
  -> analysis-forwarder shard -> Savant shard
  -> event-worker -> clip-worker
  -> bounded evidence materialization
  -> media-worker metadata/annotation finalization
  -> 8090 evidence viewer
```

The final production path must not depend on a large long-lived
`replay-sink-output` directory, unbounded per-event media work, or unmeasured
CPU-heavy transcoding.

## 2. Current Finding

The current midterm evidence chain is functional but too heavy for a future
60-stream target. Its active path is the post-Savant crop path, not the legacy
raw copy path:

```text
Replay RocksDB ring
  -> Replay job
  -> video-file-sink output video.mov under replay-sink-output
  -> media-worker ffmpeg time-domain crop/transcode into evidence raw_clip.mov
  -> cleanup removes terminal sink output directory
```

The 2026-06-20 runtime investigation found that two RTSP sources were enough to
make `/data/video-analytics/media/replay-sink-output` grow into tens of GB before
cleanup. The immediate cause was terminal `video-file-sink` raw outputs being
retained after evidence generation. The stopgap cleanup now deletes terminal sink
outputs for configured statuses, but it only prevents long-lived accumulation.
It does not remove the transient sink write or the media-worker crop/transcode
cost.

Current active cost model:

| Step | Operation | Cost |
| --- | --- | --- |
| A | Replay stores full-rate encoded video in RocksDB, TTL currently 300s per shard. | Required full-rate evidence ring. |
| B | Replay job sends a requested window to `video-file-sink`, which remuxes/writes `video.mov` under `replay-sink-output`. | One intermediate clip write per materialized event. |
| C | media-worker uses the post-Savant time-domain crop path and writes `raw_clip.mov` with `ffmpeg -c:v libx264 -pix_fmt yuv420p`. | CPU-heavy decode plus H.264 encode per materialized event. |
| D | media-worker cleanup removes the processed sink directory and records deletion metadata. | Bounded cleanup, already enabled by default. |

Important correction: in the current crop-enabled post-Savant path, media-worker
does **not** normally do a second full `shutil.copy2(video.mov -> raw_clip.mov)`.
That copy exists only in the non-crop path. Same-filesystem rename/move is still
useful for non-crop materialization and for organizing temporary files, but it
does not remove the current libx264 crop/transcode cost.

For 60 streams, the risk is therefore:

- Replay writes full-rate encoded video into its RocksDB ring.
- `video-file-sink` writes a transient event-window `video.mov`.
- media-worker may spend CPU decoding and re-encoding one clip per materialized
  event.
- Cleanup bounds long-lived sink output, but does not bound CPU, queue latency,
  or the transient sink/write/read path by itself.

That pattern is acceptable for two-source validation only as a rollback path. It
is not enough for the production design until measured and bounded.

## 3. Hard Constraints

1. Single RTSP ingestion per camera. Do not add a second RTSP pull for evidence.
2. Replay full-rate storage remains the evidence source of truth.
3. Analysis sampling must happen only on the analysis branch, never before
   evidence storage.
4. Evidence bundle and 8090 viewer semantics must remain stable:
   `raw_clip.mov`, metadata, annotations, preview artifacts, and database state
   must continue to resolve for materialized evidence.
5. The current baseline crop/cleanup path must remain available for rollback
   until an optimized materialization mode passes two-source and staged pressure
   tests.
6. Unknown or unmapped source IDs must fail closed for replay job routing.
7. Temporary media must be bounded by quota and age. A failed event must not
   leave unbounded full-size diagnostic video behind by default.
8. The plan must distinguish post-Savant crop/transcode from legacy/non-crop
   file-copy behavior. Optimizations that only remove a copy are not sufficient
   for the current crop-enabled path.
9. Do not require per-job filesystem output paths from the current shared
   `video-file-sink` adapter unless adapter support is added and verified.
10. Deferred materialization is useful only while Replay and annotation TTLs
    still cover the requested window.
11. Sidecar JSONL generation is not the current bottleneck. Keep it bounded, but
    do not spend optimization effort there unless measurements prove otherwise.

## 4. Target Architecture

### 4.1 Sharded Replay And Analysis

Keep the two-shard production model:

| Shard | Replay | Forwarder | Savant | GPU |
| --- | --- | --- | --- | --- |
| A | `replay-a` | `analysis-forwarder-a` | `savant-a` | GPU 0 |
| B | `replay-b` | `analysis-forwarder-b` | `savant-b` | GPU 1 |

In current validation, GPU 0 and GPU 1 are RTX 4090 cards standing in for future
T4 cards. The topology is the same; TensorRT engines must still be regenerated
or validated on the target GPU type later.

### 4.2 CPU-Aware Evidence Materialization

The primary production path must be explicit about whether an event is
materialized by crop/transcode or by non-crop file movement.

Current crop-enabled path:

```text
Replay job
  -> video-file-sink event window video.mov
  -> media-worker ffmpeg crop/transcode directly to evidence/<event_id>/raw_clip.mov
  -> cleanup removes processed sink output
```

Non-crop or future relaxed-contract path:

```text
Replay job
  -> video-file-sink event window video.mov
  -> same-filesystem rename/move/link into evidence incoming/final path
  -> cleanup removes any remaining sink wrapper directory
```

Current `video-file-sink` output location is controlled by container-level
`DIR_LOCATION` (`%source_id%/%src_filename%`). Clip-worker currently gives Replay
a ZMQ sink endpoint and labels, not a per-job filesystem path. Therefore,
"sink writes directly to `.incoming/<event_id>`" is not a Phase 1 assumption.
It is a future adapter capability unless proven otherwise.

The near-term target is:

- measure and bound crop/transcode time and CPU;
- keep sink output terminal directories short-lived and quota-guarded;
- use same-filesystem rename/move for non-crop materialization when the evidence
  contract allows it;
- keep raw clip finalization atomic from the viewer/API perspective.

The legacy `replay-sink-output` directory should become a bounded diagnostic or
rollback location, not the normal production path.

### 4.2.1 Media Materialization CPU Workstream

Add a first-class workstream for reducing media-worker CPU cost. Candidate
directions must be measured before selection:

| Candidate | Expected benefit | Tradeoff / risk |
| --- | --- | --- |
| Keyframe-bound stream-copy trim | Avoids decode/re-encode and can use cheap file operations. | Clip window is less exact; event may not be centered enough for current contract. |
| NVENC crop/transcode | Moves H.264 encode off CPU. | Competes with Savant GPU work and must be tested per GPU/shard. |
| Relax exact 8-12.5s centered-window contract | Reduces need for frame-exact crop. | Changes evidence semantics and must be accepted by product/QA. |
| Keep libx264 but bound concurrency | Lowest behavior risk. | Controls overload but does not reduce per-event cost. |

Phase 0 decides which candidate is worth implementing. Until then, assume
libx264 transcode is the current main bottleneck.

### 4.3 Manifest-First Evidence

Create or extend evidence task state so every event first has a durable manifest
before large media bytes are materialized:

```text
event_id
source_id
replay_shard_id
replay_api_url
replay_job_sink_url
anchor_frame_uuid
anchor_pts
requested_start_pts
requested_end_pts
keyframe_uuid
keyframe_pts
materialization_policy
materialization_status
final_raw_clip_path
incoming_temp_path
failure_reason
```

Default materialization policy:

| Event class | Policy |
| --- | --- |
| watchlist / live-search / high severity | materialize immediately |
| intrusion / lower priority | materialize when quota and backlog allow |
| overload or disk pressure | keep manifest, defer media materialization |

The database manifest must preserve enough replay coordinates to regenerate the
raw clip later while Replay TTL still contains the relevant window. If the
required window has expired, the task must fail with an explicit TTL-expired
reason rather than silently generating a misleading clip.

Current TTL boundary:

- Replay storage TTL is 300s in `config.midterm*.json`.
- Frame annotation TTL is 120s in `infra/env/midterm.env` and compose.

This means deferred materialization is only useful inside a short window unless
TTL sizing is changed. Raising Replay TTL increases RocksDB disk usage; raising
annotation TTL increases Redis memory pressure. Phase 4 must quantify both
costs before relying on long deferral.

## 5. Implementation Plan

### Phase 0 - Inventory And Guardrails

Purpose: make the current media materialization cost measurable before changing
the path. Do not assume disk copy is the bottleneck.

Required changes:

- Add metrics or logs for:
  - Replay job count per shard/source;
  - sink output `video.mov` bytes and duration;
  - media-worker crop/transcode command, elapsed time, exit status, input bytes,
    output bytes, and decoded frame count;
  - media-worker process CPU time where available;
  - final evidence bytes;
  - cleanup deleted bytes and skipped/failed cleanup reasons;
  - materialization queue depth, wait time, and p95/p99 latency.
- Add a static contract test that documents the current crop-enabled
  post-Savant path and the non-crop rename/move fallback.
- Keep the current terminal sink cleanup enabled while this work is underway.
- Record that `RAW_CLIP_SANITIZE_MODE=off` is not a valid switch for disabling
  post-Savant crop/transcode; either remove it from the post-Savant path or
  replace it with a valid sanitizer mode in a later cleanup.

Acceptance:

- Two-source validation can report:
  - `replay_sink_output_bytes`
  - `replay_sink_output_duration_seconds`
  - `ffmpeg_transcode_elapsed_seconds`
  - `ffmpeg_transcode_input_bytes`
  - `ffmpeg_transcode_output_bytes`
  - `media_worker_cpu_seconds`
  - `evidence_final_bytes`
  - `legacy_sink_deleted_bytes`
  - `materialization_queue_depth`
  - `materialization_p95_latency_seconds`
  - per-shard replay job counts
- `/data/video-analytics/media/replay-sink-output` has an explicit configured
  max-age and max-bytes policy.
- The Phase 0 report identifies whether the dominant two-source cost is
  transcode CPU, sink write/read IO, queue wait, or something else. Do not start
  Phase 1 optimization without this report.
- Existing tests remain green:

```bash
pytest -q harness/tests/test_midterm_deployment_contract.py \
  harness/tests/test_midterm_replay_epoch_isolation.py
docker compose --env-file infra/env/midterm.env \
  -f infra/docker-compose.midterm.yml config
git diff --check
```

### Phase 1 - CPU-Aware Materialization Path

Purpose: reduce or bound the real materialization cost while keeping the current
evidence contract intact.

Required changes:

- Add feature flag:

```text
EVIDENCE_MATERIALIZATION_OPTIMIZED_ENABLED=false
EVIDENCE_MATERIALIZATION_MODE=baseline_crop
EVIDENCE_REPLAY_SINK_LEGACY_CLEANUP_ENABLED=true
```

Supported materialization modes:

| Mode | Behavior |
| --- | --- |
| `baseline_crop` | Current post-Savant crop path: libx264 transcode to final evidence path, then cleanup sink output. |
| `bounded_crop` | Same output semantics as baseline, but with measured concurrency, timeout, queue, and CPU safeguards. |
| `stream_copy_keyframe` | Uses keyframe-aligned copy/remux when product accepts less exact clip boundaries. |
| `nvenc_crop` | Uses hardware encoding only after GPU contention testing. |
| `non_crop_move` | For non-crop evidence only: same-filesystem move/rename/link instead of full copy. |

Near-term Plan A:

- keep `video-file-sink` output as the crop input because current adapter cannot
  accept per-job filesystem paths;
- write crop output directly to evidence final/incoming path;
- enforce materialization concurrency and timeout limits;
- delete processed sink output through the existing guarded cleanup;
- for non-crop paths, prefer same-filesystem rename/move/link over `copy2`.

Future Plan B:

- only after proving adapter support, allow Replay jobs to target a per-event
  incoming root directly.

Acceptance:

- For a materialized event, terminal `video.mov` under `replay-sink-output` is
  removed or quarantined according to policy after finalization.
- The final `raw_clip.mov` is present under the evidence bundle and passes the
  existing decode/duration checks.
- Phase 1 reports before/after p50/p95 materialization latency, ffmpeg elapsed
  time, CPU time, and bytes written.
- Any selected optimization mode states its evidence-contract tradeoff.
- The baseline path can be restored by setting
  `EVIDENCE_MATERIALIZATION_OPTIMIZED_ENABLED=false`.

### Phase 2 - Manifest-First Task State

Purpose: prevent every low-priority event from forcing immediate media
materialization when 60 streams are active.

Required changes:

- Extend `evidence_tasks` payload or schema with materialization metadata listed
  in section 4.3.
- Add materialization policy selection in event-worker or clip-worker.
- Add explicit task statuses for:
  - `manifest_ready`
  - `materialization_pending`
  - `materializing`
  - `materialized`
  - `materialization_deferred`
  - `materialization_failed`
  - `materialization_expired`
- Ensure the 8090 viewer can distinguish:
  - evidence with a ready raw clip;
  - evidence with manifest only and deferred media;
  - evidence that can no longer be materialized.
- Add a TTL-aware scheduler:
  - compute `materialization_deadline_at` from Replay TTL and annotation TTL;
  - prioritize high-value items before they expire;
  - fail explicit `materialization_expired` once the Replay or annotation window
    is no longer available.

Acceptance:

- High-priority evidence still materializes automatically.
- Low-priority evidence can remain manifest-only without being treated as a
  corrupt bundle.
- Every deferred item has replay shard/source/window fields needed for later
  materialization.
- TTL-expired evidence is explicit in DB state and UI/API response.
- Deferred materialization documentation states the current effective deadlines:
  Replay video about 300s, frame annotations about 120s, unless configured
  otherwise.

### Phase 3 - Shard-Aware Routing And Quotas

Purpose: make evidence materialization safe across two Replay shards.

Required changes:

- Route replay jobs through shard config by `source_id`.
- Keep per-shard replay job concurrency bounded.
- Add global evidence materialization concurrency.
- Add per-camera cooldown and per-event-type quotas before media work starts.
- Fail closed on unknown source IDs.

Suggested flags:

```text
EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY=4
EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD=2
EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE=1
EVIDENCE_MATERIALIZATION_POLICY=priority
EVIDENCE_UNKNOWN_SOURCE_FAIL_CLOSED=true
```

Acceptance:

- Source A jobs go only to `replay-a`.
- Source B jobs go only to `replay-b`.
- Queue growth is bounded when alarms burst.
- A high alarm rate cannot create unbounded sink output, incoming temp, or
  materialization work.

### Phase 4 - Storage Quota And Auto-Degrade

Purpose: protect the host from evidence storage exhaustion at 60 streams.

Required policies:

| Storage area | Policy |
| --- | --- |
| Replay RocksDB | short TTL sized per shard and monitored |
| evidence final root | daily quota plus retention policy |
| evidence incoming root | strict max bytes and max age |
| replay-sink-output | bounded transient sink input and bounded diagnostics |
| generated previews/annotations | retained with final evidence policy |

TTL sizing requirement:

- quantify bytes/sec per stream in Replay RocksDB;
- estimate per-shard disk for 300s, 600s, 900s, and any proposed deferred window;
- quantify Redis memory impact of extending frame annotation TTL beyond 120s;
- select TTLs that make `materialization_deferred` meaningful without exhausting
  disk or Redis memory.

Auto-degrade behavior:

- At warning threshold, defer low-priority materialization.
- At critical threshold, materialize only high-priority events.
- At hard limit, stop accepting new media materialization but keep DB manifests.
- Never delete unexpired high-priority final evidence without an explicit
  retention policy.

Acceptance:

- Disk pressure produces controlled `materialization_deferred` states instead
  of large orphaned files.
- Cleanup actions are recorded in DB payload or audit metadata.
- `replay-sink-output` remains bounded during normal operation.
- Auto-degrade decisions include CPU/queue pressure, not only disk pressure.

### Phase 5 - Migration, Smoke, And Pressure Tests

Purpose: roll out without losing the validated baseline crop/cleanup path.

Required sequence:

1. Keep baseline crop/cleanup and terminal sink cleanup as default.
2. Add Phase 0 measurement and record baseline crop/transcode cost.
3. Enable the selected optimized materialization mode for one source on one
   shard.
4. Enable the selected optimized materialization mode for the current two-source
   dual-4090 validation.
5. Run staged source-count pressure tests when enough inputs are available:
   10 streams, 30 streams on one shard, then 60 streams across both shards.
6. Only after staged acceptance, make the optimized mode the production default.

Two-source acceptance token:

```text
PASS_REPLAY_EVIDENCE_IO_OPTIMIZED_TWO_SOURCE
```

60-stream acceptance token:

```text
PASS_REPLAY_EVIDENCE_IO_OPTIMIZED_60_STREAM
```

Required two-source checks:

- Both RTSP sources produce inference events through separate GPU shards.
- Both shards produce materialized evidence.
- `replay-sink-output` stays below the configured bound after terminal cleanup
  and leaves no terminal `video.mov` older than the configured max age.
- Materialization p95 latency, ffmpeg elapsed time, CPU time, and queue depth are
  reported.
- Incoming/temp/diagnostic directories are empty or explicitly quarantined after
  finalization.
- 8090 evidence list/detail/playback still works for materialized evidence.

Required 60-stream checks:

- Replay RocksDB disk usage stays within configured TTL sizing per shard.
- `replay-sink-output` remains bounded in optimized mode.
- Evidence incoming temp stays under configured max bytes.
- Materialization queues remain bounded.
- media-worker CPU and ffmpeg transcode latency remain within the selected
  operating point.
- No source-adapter restart storm is introduced.
- GPU utilization, decode utilization, and forwarder queues remain within the
  operating point defined by spec 16.
- Evidence materialization p95 latency meets the selected policy target for
  high-priority events.

## 5.1 Implementation Status - 2026-06-20

Implemented in the midterm stack after Phase 1A:

- additive migration `015_evidence_materialization_manifest_state.sql` extends
  `evidence_tasks` with manifest-first materialization metadata, replay shard
  routing fields, TTL deadlines, quota/degrade decisions, and cleanup/audit
  JSONB fields;
- event-worker creates evidence task manifests with `manifest_ready` or
  `materialization_pending`, computes deadlines from the current default Replay
  TTL of 300s and frame-annotation TTL of 120s, and preserves automatic
  materialization for high-priority events by default;
- clip-worker enforces source-shard routing and unknown-source fail-closed
  behavior when an explicit `REPLAY_SHARDS_CONFIG_PATH`/`REPLAY_SHARDS_JSON`
  map is configured, plus global/per-shard/per-source materialization
  concurrency, per-event type quota, per-camera cooldown, pressure-level
  degrade policy, and TTL expiration to `materialization_expired`;
- media-worker preserves the Phase 1A libx264 crop path, writes explicit
  `materializing`, `materialized`, `materialization_deferred`, and
  `materialization_failed` states, and defers media work when configured
  storage hard limits are exceeded;
- API/8090 database evidence responses and file-backed bundle detail responses
  expose materialization status, deadline, quota, and degrade metadata, and do
  not mark manifest-only/deferred/expired evidence as playable raw-clip
  evidence;
- `scripts/runtime/report_evidence_materialization_phase0.py` now supports the
  Phase 2+ report shape, including Phase 0 and Phase 1A reference
  comparisons, status counts, TTL sizing estimates, quota/degrade counts,
  cleanup bounds, and explicit `not_run_runtime_limited` for 60-stream
  acceptance when real 60-stream input is unavailable.
- active midterm config no longer uses the invalid
  `RAW_CLIP_SANITIZE_MODE=off` value; the default is `auto`.

Not implemented in this pass:

- stream-copy keyframe trimming and NVENC crop/transcode remain future media CPU
  candidates and are not enabled;
- the current shared Savant `video-file-sink` adapter still does not receive
  per-job filesystem output directories;
- no `PASS_REPLAY_EVIDENCE_IO_OPTIMIZED_60_STREAM` token may be emitted without
  a real 60-stream pressure run.

## 6. Rollback

Rollback must not require schema data loss.

Flags:

```text
EVIDENCE_MATERIALIZATION_OPTIMIZED_ENABLED=false
EVIDENCE_MATERIALIZATION_MODE=baseline_crop
EVIDENCE_REPLAY_SINK_LEGACY_CLEANUP_ENABLED=true
EVIDENCE_MATERIALIZATION_POLICY=priority
```

Rollback behavior:

- Clip-worker continues routing Replay jobs through the validated shard map.
- Media-worker returns to the baseline crop/transcode behavior.
- Terminal sink cleanup remains enabled to prevent the previously observed
  accumulation.
- Manifest fields may remain in DB payloads but must not break older bundle
  reads.

## 7. Files Expected To Change

Likely implementation files:

- `infra/docker-compose.midterm.yml`
- `infra/env/midterm.env`
- `infra/config/replay-shards*.json`
- `services/clip-worker/app/config.py`
- `services/clip-worker/app/worker.py`
- `services/clip-worker/app/replay_shards.py`
- `services/media-worker/app/config.py`
- `services/media-worker/app/worker.py`
- `services/media-worker/app/post_savant_evidence_bundle.py`
- `services/media-worker/app/frame_cache_sidecar_writer.py`
- `services/evidence-viewer/`
- `services/api/`
- `harness/tests/test_midterm_deployment_contract.py`
- `harness/tests/test_midterm_replay_epoch_isolation.py`
- new harness tests for materialization metrics, optimized modes, TTL expiry,
  and manifest-only states

Do not broaden this implementation into Savant model-chain changes unless a
phase explicitly requires it. TensorRT regeneration for T4 is tracked by the
dual-GPU production validation plan, not this storage/materialization plan.

## 8. Definition Of Done

The plan is complete only when all of the following are true:

- The selected optimized materialization mode is verified for the current
  two-source dual-shard runtime.
- Baseline crop mode remains available and covered by tests.
- Normal optimized-mode runs do not create persistent full-size media under
  `replay-sink-output`.
- Evidence media is materialized through an explicit selected mode with measured
  CPU, IO, queue, and latency budgets.
- Manifest-first evidence state exists for deferred materialization.
- TTL-aware deadlines prevent deferred work from silently outliving Replay or
  annotation storage.
- Quotas and auto-degrade prevent disk, CPU, and queue exhaustion.
- The two-source acceptance token is recorded.
- The staged 60-stream acceptance token is recorded on suitable hardware and
  input count.
