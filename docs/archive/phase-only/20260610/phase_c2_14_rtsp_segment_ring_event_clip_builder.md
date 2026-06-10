# C2.14 RTSP Segment Ring + Event Clip Builder MVP

## Why This Phase Exists

C2.13R and C2.13P proved real RTSP `watchlist_hit` and `intrusion` event
chains on `c2_post_savant_fps_probe`. C2.13V then proved the missing production
piece: valid RTSP video evidence bundles were not generated, event-style Replay
was not reliable enough to claim PASS, and media outputs lacked a bounded
retention policy outside Replay RocksDB.

C2.14 changes the primary evidence path from ad hoc event Replay to a
post-Savant segment ring plus event clip builder.

## RTSP Source

- `input_type`: `rtsp`
- `source_id`: `c2_post_savant_fps_probe`
- RTSP URL: redacted in reports

## Why Replay Is Not The Main Path

Savant Replay remains useful as a short TTL store and diagnostic fallback, but
it only bounds Replay RocksDB. It does not bound `video-file-sink` output or
evidence directories. C2.13V also showed that event-style Replay can create
clips that are accepted by the sink but fail the video integrity gate.

C2.14 therefore requires `event_style_replay_job_passed=false` for the segment
ring evidence path. Replay output cannot satisfy event evidence PASS in this
phase.

## Segment Ring Architecture

Target runtime path:

```text
RTSP input
  -> Savant post-Savant output
  -> segment ring writer
  -> segment_index.jsonl
  -> watchlist_hit / intrusion event
  -> event clip-builder
  -> evidence bundle
  -> video integrity gate
  -> viewer/report
```

Managed ring root:

```text
/data/video-analytics/media/rtsp-ring/c2_post_savant_fps_probe/
  segment_index.jsonl
  segments/
    <segment_id>/
      video.mov or video.mp4
      metadata.json
      segment_manifest.json
```

The current C2.14A implementation can inspect runtime sink configuration,
adopt compatible existing post-Savant sink outputs into the ring, build the
index, run retention dry-run, and resolve event-centered windows. Runtime
chunked sink writing is the next C2.14B step if no compatible live segments
exist.

## Current Video Sink Findings

Official Savant `video_files.py` supports `CHUNK_SIZE` and `%chunk_idx`.
`CHUNK_SIZE=0` is not a bounded evidence ring; for video files it means chunks
are split only by EOS. A C2.14B runtime writer should configure non-zero
`CHUNK_SIZE` and a ring path that includes `%chunk_idx`.

Current C2 sink output under
`/data/video-analytics/media/c2-post-savant-replay-fps-probe` is treated as
read-only input for `index-existing`. It is not a cleanup target.

## Index Schema

Each index row contains:

- `schema_version`
- `source_id`
- `segment_id`
- `segment_dir`
- `video_path`
- `metadata_path`
- `first_frame_pts`
- `last_frame_pts`
- `first_timestamp_ms`
- `last_timestamp_ms`
- `first_frame_uuid`
- `last_frame_uuid`
- `frame_count`
- `keyframe_count`
- `size_bytes`
- `created_at`
- `expires_at`
- `evidence_refs`
- `cleanup_eligible`
- `completed`

Rows must include `source_id`, `video_path`, `metadata_path`, `frame_count`,
and either first/last PTS or first/last timestamp.

## Retention Policy

Defaults:

- `MEDIA_RING_TTL_SECONDS=600`
- `MEDIA_RING_MAX_BYTES_PER_SOURCE=5368709120`
- `MEDIA_RING_MIN_KEEP_SECONDS=120`
- dry-run by default in smoke

Rules:

- delete only under `/data/video-analytics/media/rtsp-ring/{source_id}/segments`;
- delete only completed indexed segments;
- never delete segments referenced by evidence bundles;
- never delete the current active segment;
- delete oldest expired segments first;
- if size still exceeds max bytes, delete oldest cleanup-eligible segments;
- never delete historical sink outputs, evidence bundles, Replay RocksDB, or
  non-ring media.

## Event Clip Builder Design

`scripts/tools/build_c2_14_event_clip_from_ring.py` accepts an event JSON,
source id, event PTS or timestamp, pre/post seconds, ring root, and output
directory. It resolves the segment window using `segment_index.jsonl`, copies
or concatenates source segments, filters metadata into `sink_metadata.json`,
builds `annotations.frame_cache.identity.jsonl`, runs the video integrity gate,
and writes:

```text
raw_clip.mov or raw_clip.mp4
sink_metadata.json
annotations.frame_cache.identity.jsonl
summary.json
event.json
evidence_join_report.json
video_integrity_report.json
operator_rtsp_event_clip_report.html
```

Clip evidence PASS requires:

- selected segment window covers the event;
- raw clip exists;
- metadata is filtered to the same time window;
- decoded frame count and sidecar frame count reconcile;
- video integrity passes;
- `fallback_used=false`;
- `legacy_used_for_visual_binding=false`;
- `db_window_fallback_used=false`;
- `event_style_replay_job_passed=false`;
- `evidence_capture_mode=rtsp_segment_ring`.

## C2.14A Status

Implemented:

- `scripts/tools/manage_c2_14_rtsp_segment_ring.py`
- `scripts/tools/build_c2_14_event_clip_from_ring.py`
- `scripts/smoke/current/check_c2_14a_rtsp_segment_ring.sh`
- `harness/tests/test_c2_14_rtsp_segment_ring.py`

The A-phase smoke produces:

- `runtime_ring_inspect.json`
- `segment_index_report.json`
- `retention_dry_run_report.json`
- `decision_summary.json`

Possible C2.14A markers:

- `PASS_C2_14A_RTSP_SEGMENT_RING_READY`
- `PARTIAL_C2_14A_NO_COMPATIBLE_SEGMENTS`
- `FAIL_C2_14_RTSP_SEGMENT_RING_BLOCKED`

## Limitations

C2.14A does not start or reconfigure the live RTSP pipeline. If current
video-file-sink outputs do not contain the requested source id and frame/time
anchors, `index-existing` skips them and reports partial rather than faking a
ring segment.

C2.14A does not claim event video evidence PASS. C2.14B must configure a live
chunked post-Savant sink writer, capture real RTSP events, build a bundle from
ring segments, and pass the video integrity gate.

## Next Steps For C2.14B

1. Configure a runtime post-Savant `video-file-sink` with non-zero
   `CHUNK_SIZE` and `%chunk_idx` under the ring root.
2. Keep the ring manager retention dry-run on for the first runtime smoke.
3. Capture a bounded real RTSP watchlist or intrusion event.
4. Build an evidence bundle from ring segments only.
5. Require video integrity PASS and unsafe payload scan PASS before claiming
   `PASS_C2_14B_RTSP_EVENT_CLIP_BUILDER_READY`.
