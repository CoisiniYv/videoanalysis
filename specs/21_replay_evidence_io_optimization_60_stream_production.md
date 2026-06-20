# 21_replay_evidence_io_optimization_60_stream_production.md

Date: 2026-06-20

## 1. Goal

Make the evidence media path safe for the future 60 RTSP / 2 GPU shard target
by eliminating long-lived `replay-sink-output` media accumulation and reducing
evidence write amplification.

This spec is a storage and IO optimization plan for the topology already
defined by:

- `specs/16_dual_path_30x2_t4_production_optimization.md`
- `specs/20_dual_4090_as_t4_two_source_validation.md`

It does not replace the dual-path design. Replay remains the full-rate evidence
authority, while `analysis-forwarder` samples or drops only the analysis branch.

Target steady-state chain:

```text
RTSP source
  -> Replay shard full-rate storage
  -> analysis-forwarder shard -> Savant shard
  -> event-worker -> clip-worker
  -> direct evidence materialization
  -> media-worker metadata/annotation finalization
  -> 8090 evidence viewer
```

The final production path must not depend on a large intermediate
`replay-sink-output` directory that keeps a second full video copy after evidence
has been generated.

## 2. Current Finding

The current midterm evidence chain is functional but too IO-heavy:

```text
Replay RocksDB ring
  -> Replay job
  -> video-file-sink output video.mov
  -> media-worker reads/copies into evidence raw_clip.mov
  -> cleanup removes terminal sink output
```

The 2026-06-20 runtime investigation found that two RTSP sources were enough to
make `/data/video-analytics/media/replay-sink-output` grow into tens of GB before
cleanup. The immediate cause was terminal `video-file-sink` raw outputs being
retained after evidence generation. The stopgap cleanup now deletes terminal sink
outputs for configured statuses, but it only prevents long-lived accumulation.
It does not remove the transient write/read/copy cost.

For 60 streams, the current path would multiply disk traffic:

- Replay writes full-rate encoded video into its RocksDB ring.
- `video-file-sink` writes another raw clip file under `replay-sink-output`.
- `media-worker` reads that file and writes/copies `raw_clip.mov` into the
  evidence bundle.
- Cleanup then deletes the intermediate copy.

That pattern is acceptable for two-source validation only as a rollback path. It
is not the production design.

## 3. Hard Constraints

1. Single RTSP ingestion per camera. Do not add a second RTSP pull for evidence.
2. Replay full-rate storage remains the evidence source of truth.
3. Analysis sampling must happen only on the analysis branch, never before
   evidence storage.
4. Evidence bundle and 8090 viewer semantics must remain stable:
   `raw_clip.mov`, metadata, annotations, preview artifacts, and database state
   must continue to resolve for materialized evidence.
5. The current cleaned legacy path must remain behind a rollback flag until the
   direct path passes two-source and staged pressure tests.
6. Unknown or unmapped source IDs must fail closed for replay job routing.
7. Temporary media must be bounded by quota and age. A failed event must not
   leave unbounded full-size diagnostic video behind by default.

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

### 4.2 Direct Evidence Materialization

Replace the long-lived sink-output copy path with a direct finalization path:

```text
Replay job
  -> direct sink temp root: /data/video-analytics/media/evidence/.incoming/<event_id>/
  -> atomic finalize: /data/video-analytics/media/evidence/<event_id>/raw_clip.mov
  -> media-worker finalizes metadata, annotations, preview, and DB state
```

The media bytes should be written once into the evidence area, then atomically
renamed into the final bundle path. If the selected implementation cannot make
`video-file-sink` write the exact final artifact directly, it must still avoid a
second full video copy by using same-filesystem rename/move semantics whenever
possible.

The legacy `replay-sink-output` directory should become a bounded diagnostic or
rollback location, not the normal production path.

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

## 5. Implementation Plan

### Phase 0 - Inventory And Guardrails

Purpose: make the current write amplification measurable before changing the
media path.

Required changes:

- Add metrics or logs for bytes written by Replay, direct sink or legacy sink,
  evidence final output, incoming temp, and cleanup deletion.
- Add a static contract test that describes the intended direct path and keeps
  the legacy path feature-flagged.
- Keep the current terminal sink cleanup enabled while this work is underway.

Acceptance:

- Two-source validation can report:
  - `replay_sink_output_bytes`
  - `evidence_incoming_bytes`
  - `evidence_final_bytes`
  - `legacy_sink_deleted_bytes`
  - per-shard replay job counts
- `/data/video-analytics/media/replay-sink-output` has an explicit configured
  max-age and max-bytes policy.
- Existing tests remain green:

```bash
pytest -q harness/tests/test_midterm_deployment_contract.py \
  harness/tests/test_midterm_replay_epoch_isolation.py
docker compose --env-file infra/env/midterm.env \
  -f infra/docker-compose.midterm.yml config
git diff --check
```

### Phase 1 - Direct Final Evidence Sink

Purpose: remove the normal `replay-sink-output -> evidence` copy.

Required changes:

- Add feature flag:

```text
EVIDENCE_DIRECT_FINAL_SINK_ENABLED=false
EVIDENCE_INCOMING_ROOT=/data/video-analytics/media/evidence/.incoming
EVIDENCE_FINAL_ROOT=/data/video-analytics/media/evidence
EVIDENCE_REPLAY_SINK_LEGACY_CLEANUP_ENABLED=true
```

- When the flag is enabled, make clip-worker create replay jobs whose sink path
  writes into an event-scoped incoming directory.
- Make finalization atomic:
  - write into `.incoming/<event_id>/raw_clip.mov.tmp` or equivalent;
  - validate duration/decode according to existing evidence rules;
  - rename into `<event_id>/raw_clip.mov`;
  - persist final path and finalization state.
- Move media-worker responsibility toward metadata, annotations, previews, and
  DB transitions. It should not perform a second full video copy in the direct
  path.
- Keep legacy `video-file-sink` output for rollback only.

Acceptance:

- For a materialized event, there is no terminal full-size `video.mov` under
  `replay-sink-output` after finalization.
- The final `raw_clip.mov` is present under the evidence bundle and passes the
  existing decode/duration checks.
- Failed direct materialization removes or quarantines incoming temp media
  according to quota policy.
- The legacy path can be restored by setting
  `EVIDENCE_DIRECT_FINAL_SINK_ENABLED=false`.

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

Acceptance:

- High-priority evidence still materializes automatically.
- Low-priority evidence can remain manifest-only without being treated as a
  corrupt bundle.
- Every deferred item has replay shard/source/window fields needed for later
  materialization.
- TTL-expired evidence is explicit in DB state and UI/API response.

### Phase 3 - Shard-Aware Routing And Quotas

Purpose: make the direct materialization path safe across two Replay shards.

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
- A high alarm rate cannot create unbounded incoming temp directories.

### Phase 4 - Storage Quota And Auto-Degrade

Purpose: protect the host from evidence storage exhaustion at 60 streams.

Required policies:

| Storage area | Policy |
| --- | --- |
| Replay RocksDB | short TTL sized per shard and monitored |
| evidence final root | daily quota plus retention policy |
| evidence incoming root | strict max bytes and max age |
| replay-sink-output | near-zero in direct mode; bounded diagnostics only |
| generated previews/annotations | retained with final evidence policy |

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
- `replay-sink-output` remains near zero during direct-mode normal operation.

### Phase 5 - Migration, Smoke, And Pressure Tests

Purpose: roll out without losing the validated legacy path.

Required sequence:

1. Keep legacy path and cleanup as default.
2. Enable direct path for one source on one shard.
3. Enable direct path for the current two-source dual-4090 validation.
4. Run staged source-count pressure tests when enough inputs are available:
   10 streams, 30 streams on one shard, then 60 streams across both shards.
5. Only after staged acceptance, make direct path the production default.

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
- `replay-sink-output` stays below 100 MB after terminal cleanup, and direct
  mode leaves no terminal `video.mov` older than 5 minutes.
- Incoming temp directories are empty or explicitly quarantined after
  finalization.
- 8090 evidence list/detail/playback still works for materialized evidence.

Required 60-stream checks:

- Replay RocksDB disk usage stays within configured TTL sizing per shard.
- `replay-sink-output` remains near zero in direct mode.
- Evidence incoming temp stays under configured max bytes.
- Materialization queues remain bounded.
- No source-adapter restart storm is introduced.
- GPU utilization, decode utilization, and forwarder queues remain within the
  operating point defined by spec 16.
- Evidence materialization p95 latency meets the selected policy target for
  high-priority events.

## 6. Rollback

Rollback must not require schema data loss.

Flags:

```text
EVIDENCE_DIRECT_FINAL_SINK_ENABLED=false
EVIDENCE_REPLAY_SINK_LEGACY_CLEANUP_ENABLED=true
EVIDENCE_MATERIALIZATION_POLICY=legacy_immediate
```

Rollback behavior:

- Clip-worker returns to legacy replay-job sink output.
- Media-worker resumes copying from legacy sink output into evidence bundles.
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
- `services/evidence-viewer/`
- `services/api/`
- `harness/tests/test_midterm_deployment_contract.py`
- `harness/tests/test_midterm_replay_epoch_isolation.py`
- new harness tests for direct evidence finalization and manifest-only states

Do not broaden this implementation into Savant model-chain changes unless a
phase explicitly requires it. TensorRT regeneration for T4 is tracked by the
dual-GPU production validation plan, not this storage plan.

## 8. Definition Of Done

The plan is complete only when all of the following are true:

- Direct mode is enabled and verified for the current two-source dual-shard
  runtime.
- Legacy mode remains available and covered by tests.
- Normal direct-mode runs do not create persistent full-size media under
  `replay-sink-output`.
- Evidence media is written once into the evidence area and finalized
  atomically.
- Manifest-first evidence state exists for deferred materialization.
- Quotas and auto-degrade prevent disk exhaustion.
- The two-source acceptance token is recorded.
- The staged 60-stream acceptance token is recorded on suitable hardware and
  input count.

