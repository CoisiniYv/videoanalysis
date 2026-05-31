# C1E.3 Replay Upstream Compressed-Frame Continuity Diagnosis

Date: 2026-06-01

## Status

C1E.2 was not accepted as a decode-clean result for the fixed RTSP source during
C1E.3.

Completed and preserved from C1E.2:

- Downstream Replay job -> Video File Sink uses reliable DEALER/ROUTER sockets.
- Replay job sink options are explicit: 5s send/receive timeouts, 5 retries, 10000 send/receive HWM, 100 inflight ops.
- Replay job pacing is explicit via `REPLAY_FPS=30`.
- Replay RocksDB C1E dev TTL is 300s.
- `clip_validation` makes decode corruption visible with `generated`, `generated_corrupt`, and `generated_unverified`.
- C1E.1 evidence trust hardening remains in place.

Interpretation updated in C1E.4:

- The fixed RTSP source is non-golden and direct FFmpeg pulls also show H.264
  reference errors.
- `generated_corrupt` is therefore a valid surfaced evidence state when the
  bundle is complete and source corruption is visible.
- Strict `decode_error_count=0` acceptance should be reserved for a
  decode-clean/golden RTSP source.

## Clean Runtime Verification

The C1 official Replay dev stack was stopped and recreated cleanly. Runtime artifacts and Replay RocksDB cache were cleared before the clean smoke runs:

- `docker compose -f infra/docker-compose.c1-official-replay-dev.yml down --remove-orphans`
- Cleared `/data/video-analytics/replay-c1-official-replay-dev`
- Cleared C1E replay sink output under `/data/video-analytics/media/replay-sink-output/c1e`
- Removed C1E evidence dirs for `cam_c1e_rtsp_replay`
- Recreated containers with `docker compose -f infra/docker-compose.c1-official-replay-dev.yml up -d --no-build --force-recreate --pull never`

Actual runtime values were inspected from containers, not only compose files:

- source-adapter `LOCATION=rtsp://10.37.57.112:8554/live/1080movie`
- source-adapter `RTSP_URI=rtsp://10.37.57.112:8554/live/1080movie`
- clip-worker `REPLAY_JOB_SINK_URL=dealer+connect:tcp://video-file-sink:6666`
- video-file-sink `ZMQ_ENDPOINT=router+bind:tcp://0.0.0.0:6666`
- clip-worker `REPLAY_FPS=30`
- clip-worker `REPLAY_STOP_CONDITION_MODE=ts_delta_sec`
- clip-worker `ALLOW_UNBOUNDED_KEYFRAME_FALLBACK=false`
- Replay config in container: `data_expiration_ttl=300s`
- Replay config in container: `compaction_period=120s`

Clean smoke results were intermittent:

- Run 1 failed after clean reset: event `5e4aa835-971f-416d-9fbe-8f9b8e3d64f9`, `clip_status=generated_corrupt`, `decode_error_count=2`.
- Run 2 passed after another clean reset: event `94a1fefd-6993-44c4-a875-f7788e30fde8`, `clip_status=generated`, `decode_error_count=0`.
- Run 3 failed after another clean reset with stack retained for logs: event `2b375cd1-4605-4c88-ac1f-97a1d16fe560`, `clip_status=generated_corrupt`, `decode_error_count=2`.

Conclusion: old Replay RocksDB cache or stale containers are not the sole cause. The failure is intermittent under clean runtime conditions.

## Evidence Bundle Diagnosis

Primary failed sample:

- Evidence dir: `/data/video-analytics/media/evidence/2b375cd1-4605-4c88-ac1f-97a1d16fe560`
- Raw clip: `/data/video-analytics/media/evidence/2b375cd1-4605-4c88-ac1f-97a1d16fe560/raw_clip.mov`
- Metadata: `/data/video-analytics/media/evidence/2b375cd1-4605-4c88-ac1f-97a1d16fe560/metadata.json`
- Sink metadata: `/data/video-analytics/media/evidence/2b375cd1-4605-4c88-ac1f-97a1d16fe560/sink_metadata.json`
- Event annotation: `/data/video-analytics/media/evidence/2b375cd1-4605-4c88-ac1f-97a1d16fe560/event_annotation.json`
- `raw_clip_duration=10.05`
- `expected_duration_seconds=10.0`
- `duration_probe_status=ok`
- `clip_validation.ok=false`
- metadata `clip_validation.decode_error_count=2`
- diagnostic `decode_probe_error_count=3` including `mmco: unref short failure`
- `clip_status=generated_corrupt`
- ROI polygon was present in `event_annotation.json`
- No source extraction fallback was used.
- No second RTSP pull was used.
- No production `annotated_clip` was generated.

Decode error sample:

```text
Missing reference picture, default is 65596
Missing reference picture, default is 65604
mmco: unref short failure
```

Replay job payload properties:

- `anchor_keyframe=019e7f00-a5f2-71b2-aa43-0aad59acbec8`
- `offset.seconds=5.0`
- `stop_condition={"ts_delta_sec": {"max_delta_sec": 10.0}}`
- sink URL: `dealer+connect:tcp://video-file-sink:6666`
- sink options: 5s timeouts, 5 retries, 10000 send/receive HWM, 100 inflight ops
- `min_duration={"secs": 0, "nanos": 33333333}`
- `max_duration={"secs": 0, "nanos": 33333333}`
- `ts_discrepancy_fix_duration={"secs": 0, "nanos": 33333333}`
- `stored_stream_id=c1e_rtsp_replay`
- `resulting_stream_id=replay-event-2b375cd1-4605-4c88-ac1f-97a1d16fe560`

## Frame Timeline

Diagnostic command:

```bash
python scripts/debug/diagnose_replay_clip_continuity.py \
  --evidence-dir /data/video-analytics/media/evidence/2b375cd1-4605-4c88-ac1f-97a1d16fe560
```

Raw clip frame timeline:

- `raw_clip_frame_count=237`
- keyframe positions: `[0, 106, 165]`
- expected DTS delta: approximately `0.041719s`
- raw clip DTS gap count: `4`
- raw clip DTS gap samples:
  - index 32, delta `0.083021s`, B frame after B frame
  - index 57, delta `0.083021s`, B frame after B frame
  - index 58, delta `0.083020s`, B frame after B frame
  - index 92, delta `0.083855s`, B frame after B frame
- raw clip best-effort PTS gap count: `4`

Sink metadata timeline:

- `sink_metadata_frame_count=238`
- keyframe positions: `[0, 106, 165]`
- expected DTS delta: `41708333ns`
- sink metadata DTS gap count: `4`
- sink metadata DTS gap samples:
  - index 34, frame_num 33 -> 34, delta `83000000ns`
  - index 59, frame_num 58 -> 59, delta `83000000ns`
  - index 60, frame_num 59 -> 60, delta `83000000ns`
  - index 94, frame_num 93 -> 94, delta `84000000ns`
- sink metadata sorted PTS gap count: `4`
- frame_num jump count: `0`
- raw clip gaps align with sink metadata gaps: `yes`

The PTS values in metadata are not monotonic in received order because the stream carries B-frames. The useful PTS signal is the sorted PTS gap list, which also shows four gaps.

## Logs Reviewed

source-adapter:

- Started RTSP source adapter for `rtsp://10.37.57.112:8554/live/1080movie`.
- Sent EOS on start to reset decoder state.
- Initialized FFmpeg input successfully.
- No explicit drop or reconnect message was observed in the retained failure logs.
- GStreamer plugin-loader warning was present, but it is a startup environment warning and not correlated to the specific clip gap.

replay-service:

- Received, added, and forwarded frames for `c1e_rtsp_replay`.
- Repeated ZeroMQ receive EAGAIN timeout debug lines appeared after traffic; these are receive wait timeouts, not explicit frame drop errors.
- Packet counter reached `Packets: 549, Bytes: 29602335`.
- No explicit drop/HWM error was observed.

video-file-sink:

- Confirmed runtime socket: `ZMQ_ENDPOINT=router+bind:tcp://0.0.0.0:6666`.
- Created writer for `replay-event-2b375cd1-4605-4c88-ac1f-97a1d16fe560`.
- Wrote `/media/replay-sink-output/c1e/1780247180333056515/replay-event-2b375cd1-4605-4c88-ac1f-97a1d16fe560%/unknown%/video.mov`.
- Received EOS and closed metadata normally.
- No muxer error was observed.

clip-worker:

- Started with `stop_condition_mode=ts_delta_sec`, `replay_fps=30`, `allow_unbounded_keyframe_fallback=False`.
- Used event-provided `previous_keyframe_uuid`.
- Created Replay job `019e7f00-a7b2-7971-974a-0df2917daf53`.
- Replay job payload used the reliable sink URL and options.

media-worker:

- Finalized the evidence bundle and updated the event.
- Host/container image lacked `ffprobe`; duration fallback used `imageio_ffmpeg`.
- Decode validation reported corruption in metadata as `generated_corrupt`.

savant-security and event-worker:

- Savant processed `c1e_rtsp_replay` and exported intrusion events.
- event-worker published exactly one record request, then skipped later intrusion events due to `max_requests_reached`.

## Failure Stage

Diagnosis: `upstream_gap`.

Reasoning:

- `sink_metadata.json` already contains DTS gaps before final evidence copy.
- `raw_clip.mov` has matching DTS/PTS gaps.
- Decode errors are H.264 reference errors at the same class of discontinuity.
- Video File Sink logs show normal writer startup, EOS, metadata flush, and close; no muxer failure was logged.

This does not prove whether the first loss occurs in the RTSP source itself, the source-adapter ingest path, Replay RocksDB storage, or Replay job re-emission. It does rule out a pure finalizer-only problem and makes Video File Sink muxing a secondary suspect for this sample.

## Next Step Recommendation

Recommended next phase:

```text
C1E.4 source-adapter/replay continuity fix
```

Work items:

- Add a compressed-frame continuity probe at Source Adapter -> Replay ingest and at Replay job output.
- Compare source ingest frame timeline with Replay job output timeline using frame UUID, DTS, keyframe, and seq_id.
- Investigate source-adapter ingest parameters and RTSP/H.264 continuity behavior.
- Investigate whether Replay stores the discontinuity in RocksDB or introduces it during job re-emission.
- Keep Video File Sink muxer strategy as a secondary track only if a future sample shows continuous sink metadata but corrupt raw clip.

Do not move to random tuning of `REPLAY_FPS`, `BUFFER_LEN`, or stop condition mode. The current evidence points to compressed-frame continuity, not a blind pacing value issue.

## Boundaries Preserved

- No second RTSP path was used.
- No source extraction was used.
- No ffmpeg manual source clip fallback was used.
- No production `annotated_clip` was generated.
- No media artifacts are committed.
- C1E.1 protections remain: bounded keyframe fallback, ffprobe duration metadata, `duration_probe_status`, ROI annotation lookup, and source_id metadata filtering.
- C1E.2 protections remain: dealer/router sink, reliable sink options, `clip_validation`, and status mapping.
