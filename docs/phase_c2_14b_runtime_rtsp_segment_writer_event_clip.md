# C2.14B Runtime RTSP Segment Writer and Event Clip

## Why C2.14B exists

C2.14A implemented the RTSP segment ring schema, index contract, retention dry-run
contract, and event clip-builder contract. It ended as
`PARTIAL_C2_14A_NO_COMPATIBLE_SEGMENTS` because the active runtime was not
writing current RTSP post-Savant output into ring-compatible chunked segments.

C2.14B connects the active post-Savant stream to a chunked video-file-sink output
under the managed ring root. The event-style Replay job remains diagnostic only;
it is not the primary evidence path and is not claimed as passed by this phase.

## Current RTSP source

- `input_type`: `rtsp`
- `source_id`: `c2_post_savant_fps_probe`
- `camera_id`: `c2_post_savant_fps_probe`
- RTSP URL is redacted in reports.

## Runtime sink config changes

The active C2 compose file now allows the C2.14B smoke to override:

- `C2_POC_REPLAY_CONFIG`
- `C2_POC_SINK_DIR_LOCATION`
- `C2_POC_SINK_CHUNK_SIZE`

C2.14B adds `modules/savant_replay/config.c2_14_ring_pass_through.json`. It keeps
Replay storage enabled and forwards the continuous post-Savant stream to
`dealer+connect:tcp://video-file-sink:6666`. This forwarding is only the
continuous stream transport into the ring writer; it is not event-style Replay
evidence.

The bounded smoke writes a run-scoped compose override that sets:

- `CHUNK_SIZE=120` by default
- `DIR_LOCATION=/media/rtsp-ring/%source_id/segments/{run_stamp}_%chunk_idx`
- `METADATA_JSON_FORMAT=native`

The official `video_files.py` adapter tokens are `%source_id`, `%src_filename`,
and `%chunk_idx`. The smoke adds a run stamp before `%chunk_idx` because the
official adapter resets chunk indexes after a sink restart; a fixed
`segments/%chunk_idx` path would overwrite older ring segments.

## Ring layout

Host ring root:

```text
/data/video-analytics/media/rtsp-ring
```

Per-source segment root:

```text
/data/video-analytics/media/rtsp-ring/c2_post_savant_fps_probe/segments/
```

Each completed segment directory is indexed only when both video and metadata
exist, metadata source IDs match `c2_post_savant_fps_probe`, and metadata has a
PTS or timestamp anchor. The indexer creates `segment_manifest.json` for live
ring segments after they are stable.

## Retention dry-run

C2.14B continues the C2.14A retention contract:

- `MEDIA_RING_TTL_SECONDS=600` by default
- `MEDIA_RING_MAX_BYTES_PER_SOURCE=5GB` by default
- `MEDIA_RING_MIN_KEEP_SECONDS=120` by default
- dry-run only in smoke

Retention only targets completed indexed segment directories under the ring
source root. It skips current active, evidence-referenced, manifest-missing, and
outside-root rows.

## Event clip-builder

The clip-builder consumes a real `watchlist_hit` or `intrusion` event with
`frame_pts` or `event_ts_ms`, resolves a ring window, copies or concatenates ring
segment video, filters sink metadata, builds
`annotations.frame_cache.identity.jsonl`, and runs the existing post-Savant video
integrity gate.

The bundle marks:

- `evidence_capture_mode=rtsp_segment_ring`
- `event_style_replay_job_passed=false`
- `fallback_used=false`
- `legacy_used_for_visual_binding=false`
- `db_window_fallback_used=false`

If no event occurs, C2.14B may still pass the segment writer readiness marker. If
an event occurs near a segment boundary and the requested pre/post window is not
covered, the result is partial rather than a fake clip pass.

## Smoke output

Smoke script:

```bash
bash scripts/smoke/current/check_c2_14b_rtsp_segment_writer_event_clip.sh
```

The smoke prints:

- input type and source id
- ring root
- chunk size
- segments written and indexed
- retention dry-run status
- selected event type if any
- evidence bundle path if generated
- video integrity status
- overall marker and output directory

## Current run result

Latest bounded smoke:

- result marker: `PASS_C2_14B_RTSP_EVENT_CLIP_READY`
- output dir:
  `/data/video-analytics/media/evidence/c2_14b_rtsp_segment_writer_20260608T041025`
- evidence bundle:
  `/data/video-analytics/media/evidence/c2_14b_rtsp_event_clip_20260608T041137`
- ring segments written: 57
- indexed rows: 56
- retention dry-run: safe, 18 delete candidates, 0 deleted,
  0 unsafe deletion targets
- selected event:
  `savant_security:c2_post_savant_fps_probe:682:intrusion:1780891860187`
- raw clip:
  `/data/video-analytics/media/evidence/c2_14b_rtsp_event_clip_20260608T041137/raw_clip.mp4`
- video integrity: pass, 10.0 seconds, 75 decoded frames, 75 sidecar frames,
  0 decode errors
- unsafe payload scan: passed

## Limitations

- Runtime smoke is bounded and does not prove long-running disk stability.
- Retention apply is not enabled unless explicitly run.
- Event-style Replay is still not a passed evidence path.
- Multi-camera ring behavior is not tested in this phase.
- If no event occurs in the bounded window, no clip evidence is generated.
- If the selected event is outside the indexed segment window, the result is a
  partial event-window coverage gap.

## Next steps

If C2.14B reaches segment writer readiness but no clip is generated, rerun with
an event-producing scene/window. If C2.14B reaches event clip readiness, proceed
to the next algorithm module. If writer readiness fails, fix the
video-file-sink/Replay runtime path before adding new algorithms.
