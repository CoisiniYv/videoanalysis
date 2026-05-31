# C1E.2 Replay Clip Garbling Fix

Status: dev-only Replay evidence trust default.

## Scope

C1E.2 keeps the C1E single RTSP path:

```text
rtsp://10.37.57.112:8554/live/1080movie
  -> source-adapter
  -> replay-service
  -> savant-security
  -> Redis security.events
  -> event-worker -> PostgreSQL
  -> clip-worker -> Replay job
  -> video-file-sink
  -> media-worker/finalizer
```

No second RTSP pull, source extraction, source-file ffmpeg fallback, or
production `annotated_clip` is added.

## Garbling Cause

The Replay job sink previously used `pub+connect` to `sub+bind`. PUB/SUB can
drop frames silently when the sink is under back pressure. Losing one predicted
frame can make the rest of that GOP fail to decode until the next keyframe,
which appears as intermittent macroblock corruption in `raw_clip.mov`.

C1E.2 changes the Replay job sink to a reliable socket pair:

```text
clip-worker REPLAY_JOB_SINK_URL = dealer+connect:tcp://video-file-sink:6666
video-file-sink ZMQ_ENDPOINT = router+bind:tcp://0.0.0.0:6666
```

The Replay job payload also includes per-job sink options:

```text
send_timeout=5s
send_retries=5
receive_timeout=5s
receive_retries=5
send_hwm=10000
receive_hwm=10000
inflight_ops=100
```

The C1E/P1c source adapter also sets `BUFFER_LEN=2000` so short downstream
back-pressure does not force the RTSP ingest queue to discard frames before
Replay can persist them.

## Real-Time Pacing

Replay delivery is paced at the configured `REPLAY_FPS` default of `30`.
`min_duration`, `max_duration`, and `ts_discrepancy_fix_duration` are all set to
one frame interval:

```text
fps=30 -> 33,333,333 ns
fps=25 -> 40,000,000 ns
```

Frame count is calculated as:

```text
(pre_seconds + post_seconds) * replay_fps
```

`offset.seconds` remains the pre-roll duration as a floating-point seconds
value.

## Replay TTL

`modules/savant_replay/config.p1c_rtsp_inline.json` now uses:

```text
data_expiration_ttl: 300s
compaction_period: 120s
```

The 300s TTL is a C1E.2 dev/evidence trust default. Before any 60-stream
production deployment, Replay TTL capacity must be recalculated from stream
count, bitrate, retained pre-roll, RocksDB write amplification, and available
NVMe throughput/capacity.

## Clip Validation

The media-worker preserves C1E.1 `ffprobe` duration probing and adds full-clip
decode validation. Business metadata now records:

```text
raw_clip_duration
expected_duration_seconds
duration_probe_status
clip_validation.ok
clip_validation.decode_error_count
clip_validation.decode_error_sample
clip_validation.duration_ok
clip_validation.probe_tool
```

Status mapping:

```text
clip_validation.ok == true -> clip_status=generated
decode_error_count > 0 -> clip_status=generated_corrupt
probe unavailable or unverifiable -> clip_status=generated_unverified
```

Missing duration or probe failure is not treated as success.

## Preserved C1E.1 Protections

C1E.2 does not relax C1E.1 trust hardening:

```text
bounded/safe keyframe fallback remains the default
unbounded keyframe fallback remains disabled by default
ffprobe raw_clip_duration remains
duration_probe_status remains
ROI polygon annotation lookup remains
evidence-worker source_id metadata filtering remains
```

## Not Done In This Stage

```text
no second RTSP
no source extraction
no production annotated_clip
no 60-stream production capacity conclusion
```

## Production Capacity Reminder

Before moving this to production:

```text
use NVMe for 60 streams
size Replay TTL storage by stream count and bitrate
limit Replay job concurrency
run staged 10 -> 30 -> 60 stream pressure tests
```
