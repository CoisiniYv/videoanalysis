# C2.15 Replay Sink Observability and Completion-Aware Admission

## Scope

C2.15 fixes the evidence latency diagnosis and Replay admission mechanism.
It does not change face recognition, overlay semantics, event algorithms, or
business behavior.

## Current Diagnosis

Replay job creation is not the current bottleneck: pressure artifacts show
`replay_job_create_ms` p95 in the tens of milliseconds. Media finalization is
also not the primary tail: `finalization_duration_ms` p95 is typically
single-digit seconds and ffprobe usually completes in tens to hundreds of
milliseconds.

The evidence tail is dominated before finalizer start, primarily in
video-file-sink writeout, EOS, and file stability visibility. The no-throttle
run showed `replay_to_sink_metadata_ms` p95 around 13.6 seconds and
`sink_video_to_stable_ms` p95 around 60.3 seconds. A live single-event sample
showed metadata visible around 3.2 seconds while `sink_video_to_stable_ms` was
31.1 seconds.

Historical sink logs also show that each event creates a new writer because the
Replay payload uses a per-event `resulting_stream_id`. This is left unchanged in
C2.15; reusing stream IDs could cause mixed jobs, EOS leakage, or output
directory collisions.

## C2.15A - Sink Observability

Implemented artifacts must answer:

- current replay topology: `single_sink` or `dual_shard`;
- whether dual replay is actually enabled in clip-worker env and shard
  containers;
- per-sink writer count, resident writer peak, pending reclaim peak, EOS count,
  GStreamer warnings/errors, and pipeline operation duration p50/p95/p99;
- event phase timings already emitted by clip-worker and media-worker:
  Replay job create, metadata visible, video visible, video stable, ffprobe
  ready, and finalizer start wait.

Pressure artifacts:

- `video_file_sink_logs_since_start.txt`
- `video_file_sink_a_logs_since_start.txt`
- `video_file_sink_b_logs_since_start.txt`
- `video_file_sink_pressure_summary.json`
- `replay_topology_summary.json`
- `downstream_observability_summary.json`

If the runtime is single sink, the topology artifact must explicitly report:

- `replay_topology = single_sink`
- `dual_replay_enabled = false`
- `reason = REPLAY_SHARDS_CONFIG_PATH empty or replay/video-file-sink shard containers stopped`

If the runtime is dual shard, the topology artifact must include:

- `replay-a` and `replay-b` status;
- `video-file-sink-a` and `video-file-sink-b` status;
- per-shard video-file-sink metrics.

Marker:

`PASS_C2_15A_REPLAY_SINK_OBSERVABILITY_READY`

Status: implemented in the pressure runner and parser tests.

## C2.15B - Completion-Aware Admission

C2.15B replaces fixed-time in-memory active slots with DB-backed Replay slot
lifecycle state. The slot is acquired after successful Replay job creation and
released by media-worker at `sink_video_stable`. This release point is earlier
than final clip materialization and directly relieves Replay admission pressure;
finalization duration is backfilled later when available.

Required slot fields:

- `event_id`
- `replay_job_id`
- `source_id`
- `camera_id`
- `replay_shard_id`
- `sink_url` or sink instance
- `resulting_stream_id`
- `replay_duration_seconds_effective`
- `replay_slot_acquired_at`
- `replay_slot_deadline_at`
- `replay_slot_status`
- `replay_slot_released_at`
- `release_reason`
- `sink_video_to_stable_ms`
- `finalization_duration_ms`

The timeout deadline must be based on effective Replay duration plus media poll,
sink stability, finalizer budget, and grace. It must not regress to `pre+post`
when Replay used a longer explicit or computed duration.

Effective duration selection order:

1. `request.replay_duration_seconds`
2. `duration_seconds_override`
3. `offset_seconds_override + post_seconds`
4. `pre_seconds + post_seconds`

The pressure artifact includes a `replay_admission` section with:

- slot acquired count;
- slot released count;
- release reason counts;
- effective Replay duration distribution;
- timeout budget distribution;
- sink-video-to-stable release latency distribution.

Marker:

`PASS_C2_15B_COMPLETION_AWARE_REPLAY_ADMISSION_READY`

Status: implemented with DB-backed active slots, timeout fallback, media-worker
release at `sink_video_stable`, and harness coverage for acquire, release,
timeout, DB count recovery, and effective duration selection.

## C2.15 Pressure Closure

Accepted 60-route, 8 FPS, same-GPU dual-Savant, dual-Replay/sink artifact:

`/data/video-analytics/artifacts/pressure60_c2_15_dual1gpu_evidence8fps_nofastproof_admission18_20260630T115728Z`

Result:

- status: `passed`;
- topology: `dual_shard`, `dual_replay_enabled=true`;
- retained evidence: 50/50 checked OK through the 8090 evidence API;
- Qdrant fallback: 0;
- Redis record/events/face pending and lag: 0;
- `record_request_pending_ms` p95: 112.0 s;
- `media_queue_wait_ms` p95: 146.5 s, down from the 192 s baseline;
- `replay_to_sink_metadata_ms` p95: 7.5 s;
- `sink_video_to_stable_ms` p95: 46.5 s;
- `finalizer_pool_wait_ms` p95: 154 ms;
- `finalization_duration_ms` p95: 5.3 s.

The accepted run uses `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY=18`,
`EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD=9`,
`MEDIA_POLL_INTERVAL_S=2`, `MIDTERM_SINK_STABILITY_CHECKS=2`, and keeps the
post-Savant proof fast path disabled. The fast path remains available as a
diagnostic knob, but it is off by default because 60-route runs showed it
shifted pressure into video-file-sink/finalizer timing and worsened the tail.

Marker:

`PASS_MIDTERM_EVIDENCE_REPLAY_LATENCY_OPTIMIZED`
