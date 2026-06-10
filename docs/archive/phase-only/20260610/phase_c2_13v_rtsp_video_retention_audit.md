# C2.13V RTSP Video Evidence and Retention Audit

## Why This Phase Exists

C2.13R and C2.13P proved that the real RTSP source can drive watchlist and
intrusion event chains. They did not prove that those events have usable video
evidence, and they did not prove that Replay, video-file-sink, or media outputs
are bounded for production.

C2.13V audits both gaps before adding more algorithms or packaging another demo.

## Current RTSP Source

- `input_type`: `rtsp`
- `source_id`: `c2_post_savant_fps_probe`
- `camera_id`: `c2_post_savant_fps_probe`
- RTSP URL is redacted in reports.

## Services Inspected

- `source-adapter`
- `savant-security`
- `replay-service`
- `video-file-sink`
- `media-worker` if present
- `evidence-viewer`

The audit records runtime container env/mounts, static compose/env/config hints,
Replay config, active stream ids, and active host media paths.

## Retention Policy

Replay config for the C2 post-Savant path contains a RocksDB
`data_expiration_ttl` and compaction period, so Replay itself is TTL-configured.

The video-file-sink path is treated as an unbounded production risk unless
rotation, cleanup, or max disk usage is configured or runtime deletion is
observed. `CHUNK_SIZE=0` means no sink chunk rotation was found.

Evidence directories are also treated as unbounded unless an explicit cleanup or
quota policy is found.

## Disk Growth Observations

`scripts/tools/run_c2_13v_rtsp_video_retention_audit.py` samples these paths:

- `/data/video-analytics/media`
- `/data/video-analytics/media/c2-post-savant-replay-fps-probe`
- active Replay RocksDB host path
- active video-file-sink output root
- `/data/video-analytics/media/evidence`
- `/data/video-analytics/media/evidence_audit`

The report includes initial/final size, byte deltas, file count deltas, newest
files, top growing directories, and whether automatic removal was observed.

## Event Capture

The audit captures bounded real RTSP `watchlist_hit` and `intrusion` events from
Redis and PostgreSQL. It records event ids, source ids, camera ids, track ids,
source observation ids, timestamps/frame PTS, person ids, external person ids,
and ROI/rule fields where present.

No synthetic event is emitted by C2.13V.

## Evidence Association

Priority order:

1. Existing RTSP/post-Savant sink coverage.
2. One bounded event-style Replay job using `ts_delta_sec`.
3. Explicit partial result with a concrete gap reason.

Video evidence PASS requires:

- raw clip exists;
- event source and timestamp/frame coverage verified;
- same-stream metadata/sidecar exists;
- decoded video and sidecar frame counts match or an explicit safe crop explains
  reconciliation;
- video integrity gate passes;
- no DB window fallback;
- no legacy annotation fallback;
- no embedding or image/crop/base64 payload leaks.

Fixed `frame_count=240` is rejected as a 10 second duration proxy.

## Replay Status

C2.13V does not claim event-style Replay passed unless a generated clip passes
the video integrity gate and source/time/metadata join. Replay failures are
reported as `replay_event_video_failed`.

## Production Risk

Production risk remains if:

- video-file-sink/evidence output lacks TTL, rotation, cleanup, or max disk
  policy;
- RTSP events cannot be joined to a valid video clip;
- Replay event clips fail integrity or timestamp/source join.

## Recommendations

- Add ring buffer, TTL, or max-size cleanup for video-file-sink and evidence
  output if missing.
- Enable stable RTSP sink retention if event evidence should come from
  continuous post-Savant output.
- Fix event evidence clip generation if Replay/sink association cannot produce
  valid video.
- Defer new algorithms until video evidence and retention risk are acceptable.
