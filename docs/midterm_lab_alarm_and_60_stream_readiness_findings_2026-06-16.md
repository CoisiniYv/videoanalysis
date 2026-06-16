# Midterm Lab Alarm and 60-Stream Readiness Findings

Date: 2026-06-16

This note freezes the read-only runtime findings from the lab-camera alarm
diagnosis and the 30/60-stream production-readiness review. It is a findings
record, not an implementation plan.

## 1. Lab camera alarm behavior

The current lab behavior is not "no alarm at all". During the 2026-06-15
evening live check, the lab camera produced intrusion records and most of the
latest record-request events produced evidence successfully.

Observed lab events:

| Created CST | Event status | Evidence status | Notes |
| --- | --- | --- | --- |
| 2026-06-15 21:11:44 | stored | ready | `/media/evidence/72c5986a-d184-47b7-9bfb-fbf9b643144b/raw_clip.mov` |
| 2026-06-15 21:12:14 | stored | ready | `/media/evidence/161f1fd1-437c-401e-87c6-17209a63208a/raw_clip.mov` |
| 2026-06-15 21:12:46 | stored | ready | `/media/evidence/49543594-8fed-4eaa-90c2-bf3fab2fcced/raw_clip.mov` |
| 2026-06-15 21:13:16 | stored | ready | `/media/evidence/38db0dbe-a611-4d7b-bb00-7b0a8a3abf27/raw_clip.mov` |
| 2026-06-15 21:13:32 | stored | not_implemented | suppressed by `camera_algorithm_cooldown`; no record request/evidence expected |
| 2026-06-15 21:14:14 | stored | ready | `/media/evidence/3531cd12-2394-4292-8f6a-37167b0a39a6/raw_clip.mov` |

The user-visible gap is that "appearing in front of the lab camera" does not
mean evidence appears immediately in 8090. The runtime path is:

```text
Savant intrusion event
  -> event-worker DB row / alert / record_request
  -> clip-worker waits for post-window frame proof
  -> Replay job writes a 10s evidence clip
  -> media-worker finalizes the bundle
  -> 8090 shows ready evidence
```

The latest lab evidence was typically ready tens of seconds after the event,
not in real time. The observed delay came from post-window proof waiting,
Replay/media finalization, and contention with `CLIP_WORKER_MAX_CONCURRENT_JOBS=1`.

## 2. `min_inside_ms` semantics

`min_inside_ms` is the minimum continuous time that the same tracked person must
remain inside the configured intrusion zone before an intrusion event can fire.

The implementation uses the person bbox foot point, not the bbox center:

```text
foot_point = (bbox.x + bbox.width / 2, bbox.y + bbox.height)
```

For each track, the rule checks whether the latest foot point is inside the ROI
polygon. If it is, the rule scans backward through the same track until the
first continuously-inside observation and computes:

```text
inside_ms = latest_observation.timestamp_ms - first_inside_observation.timestamp_ms
```

An intrusion event is emitted only when:

- the rule is enabled;
- the track has observations;
- the latest foot point is inside the zone;
- `inside_ms >= min_inside_ms`;
- the per-track/per-zone cooldown allows emission.

Current difference between the two runtime cameras:

| Source | `min_inside_ms` | Practical effect |
| --- | ---: | --- |
| `primary_rtsp` | 1 | Almost immediate once a person is detected inside the full-frame zone |
| `source_00000000-0000-4000-8000-781078565686` (`lab`) | 1000 | Requires about 1s of stable tracking inside the lab ROI |

If the pose detector or tracker drops the person for a short period, the
continuous-inside clock can reset. This makes lab feel less immediate than the
primary movie source even when the stream is healthy.

## 3. Lab detection-rate finding

The lab source was active and annotated around 8fps, but person detection was
not uniformly present across frames. During the live check, a later short
window showed the person being detected, but the longer-window ratio still
showed sparse detection:

```text
lab_last_2m  frames=960  frames_with_person=240  pct=25.00%
lab_last_5m  frames=2401 frames_with_person=863  pct=35.94%
lab_last_10m frames=4802 frames_with_person=863  pct=17.97%
```

Earlier in the same investigation, the accumulated runtime counters also
showed a large gap between primary and lab detections:

```text
primary_rtsp pose_objects_total ~= 37k
lab          pose_objects_total ~= 417
```

This means missed or delayed lab alarms should not be debugged only from the
clip-worker side. There are two separate questions:

1. Did Savant produce an intrusion event?
2. If it did, did the evidence pipeline make that event visible and ready?

For the latest test window, the answer to both was mostly yes. For earlier
lab tests, the main gap was often before evidence: sparse detection/tracking or
cooldown suppression meant fewer record-request events than expected.

## 4. Evidence pipeline status

The post-Savant evidence proof fix improved the "event exists but no evidence"
path. New lab events after the fix can become `ready`.

Remaining evidence-path issues:

- `CLIP_WORKER_MAX_CONCURRENT_JOBS=1` is still a major latency source under
  event bursts. It serializes proof/replay work across sources.
- Suppressed events are stored but do not create record requests because the
  configured policy is `suppress_record_request=true`.
- 8090 must distinguish event records, suppressed records, queued/replaying
  evidence, ready evidence, and failed evidence. Showing only ready evidence
  makes a delayed event look like a missing alarm.
- Media finalization currently logs `ffprobe not found` and falls back to
  `imageio_ffmpeg`; this worked in the observed run, but it should be cleaned up
  for production diagnostics.

## 5. Current 60-stream readiness judgement

The current architecture direction is correct:

```text
RTSP source-adapter
  -> Replay full-rate storage
  -> analysis-forwarder sampled/drop-on-pressure path
  -> Savant inference
  -> Redis/PostgreSQL
  -> clip-worker Replay job
  -> video-file-sink
  -> media-worker evidence bundle
  -> 8090
```

This separates the full-rate evidence path from the sampled analysis path. It
prevents a slow or stalled Savant analysis branch from directly backpressuring
source capture in the same way as the old inline Replay-to-Savant path.

However, the current checkout is not ready to claim stable 60-stream operation.
A static readiness check reported that Phase 1 forwarder topology is present,
but the local runtime is not a 30/60-stream validation environment:

```text
phase1_forwarder_passed = true
enabled_rtsp_sources actual = 1, required >= 30
gpu_t4_count actual = 0, required >= 1
```

60 streams require at least two production shards:

```text
Shard A: 30 sources -> replay-a -> analysis-forwarder-a -> savant-a on T4 #0
Shard B: 30 sources -> replay-b -> analysis-forwarder-b -> savant-b on T4 #1
```

The shared API/Redis/PostgreSQL/evidence UI can remain shared, but all workers
that touch Replay jobs must become shard-aware.

## 6. 60-stream gaps to fix before production

The current single-shard stack should not be scaled to 60 sources by only adding
more cameras. Known gaps:

- no first-class `source_id -> shard -> Replay/Savant` routing map;
- runtime source generation still needs per-shard Replay endpoints;
- clip-worker currently assumes one `REPLAY_API_URL`; it must route jobs to the
  Replay shard that stored the source;
- `MAX_PARALLEL_STREAMS` must be set and validated for 30 streams plus headroom
  per Savant module;
- analysis-forwarder must be per-shard and must expose per-source fairness/drop
  metrics;
- the unused Savant H264/NVENC output should be disabled or made
  metadata-only before scale testing;
- `video-file-sink`, clip-worker, and media-worker are currently single
  bottlenecks for evidence bursts;
- Redis stream sizing is too small for 60 streams unless retention is raised or
  streams are sharded.

The Redis sizing risk is especially concrete:

```text
60 streams * 8fps ~= 480 frame_annotation entries/s
FRAME_ANNOTATION_REDIS_MAXLEN=20000
20000 / 480 ~= 42s of frame annotations
```

If clip-worker/media-worker delay exceeds that window, frame proof lookups can
fail because the needed post-Savant annotation has already been trimmed.

## 7. Required UI/observability improvements

8090 should show the event pipeline as separate state, not only ready evidence.
At minimum, per source:

- effective analysis fps;
- frames with person ratio over recent windows;
- last frame age;
- last intrusion event time;
- last record_request time;
- latest evidence state and reason;
- suppressed/cooldown count;
- clip-worker queued/replaying/failed counts;
- source/container restart count and restart rate;
- forwarder drop rate and queue depth.

This is required for operator trust. Without it, a healthy stream with delayed
evidence or cooldown-suppressed events looks like "no alarm".

## 8. Next execution guidance

Do not jump directly to 60 streams. Execute in this order:

1. Harden the 8090 event/evidence observability so lab tests show event records
   immediately, even while evidence is queued or replaying.
2. Raise or redesign clip-worker/media-worker concurrency with explicit
   queueing and backpressure limits.
3. Size Redis streams and Replay TTL against worst-case evidence delay, not only
   against the 5s pre/5s post clip length.
4. Run a 10-stream soak, then a 30-stream single-T4 shard test.
5. Only after Phase 2 passes, implement dual-shard source routing and Replay job
   routing for 60 streams.

Acceptance for scale testing must include:

- source adapter restart count stays flat;
- Replay has no sustained EAGAIN/send-timeout storm;
- forwarder drop is bounded and per-source fair;
- Savant per-source fps and last-frame age stay within target;
- Redis proof retention exceeds observed queue/replay delay;
- evidence success rate and ready latency are measured, not inferred.
