# R3.3 Timeline Mapping Evidence Plan

Status: planning only. This document does not implement production clips, does
not change the Savant pipeline, and does not run a performance test.

R3.3 exists because R3.2 production RTSP visual evidence is not yet aligned.
R3.2A can write `metadata.json` and a best-effort `snapshot.jpg`; R3.2B can
write a controlled raw clip fallback. Those files prove the lifecycle path, but
they do not prove exact visual evidence for RTSP events.

## Current R3.2 Alignment Failure

RTSP evidence is currently not exact because:

- `event_ts_ms` / `timestamp_ms` for behavior events is wall-clock-like or
  adapter-derived time, not mapped to a recorded media segment.
- `snapshot.jpg` is captured from `rtsp_current`, so it is a current frame, not
  the event frame.
- `raw_mp4_fallback` has no event timestamp to video PTS mapping.
- `frame_uuid` and `keyframe_uuid` are present in the event contract and
  `events` table, but current producers set them to `null`.
- `pipeline_ts_ns` is not stored in `events` or `face_observations`.
- Bbox metadata, snapshot frames, and raw clips can come from different
  sources/timelines.

No production exact visual evidence may be claimed before timeline mapping.

## Why Local Video Debug Aligns

R3.2E local-video-debug aligns because inference and export use the same local
MP4:

```text
source_mp4_path -> Savant local video adapter -> face_observations.timestamp_ms
same source_mp4_path -> cv2.CAP_PROP_POS_MSEC(timestamp_ms) -> exact frame
```

In that mode, `timestamp_ms` is a local video offset in milliseconds. RTSP does
not have that property by default. RTSP events need an explicit mapping from
event time or frame identity to recorded media.

## Target Production Chain

R3.3 target chain:

```text
SecurityEvent / face_observation
  -> source_id + event_ts_ms
  -> frame_uuid / keyframe_uuid or pipeline_ts_ns
  -> recording segment index
  -> segment_path + segment start epoch_ms or start_pts
  -> offset_ms within segment
  -> exact event frame
  -> exact raw_clip.mp4
  -> bbox and image/video from the same source timeline
```

The production `metadata.json` should then record:

```text
exact_event_frame=true
snapshot_alignment_status=exact
exact_event_clip=true
clip_alignment_status=exact
timeline_mapping={...}
```

Until that chain is proven, production media must remain `partial`,
`not_implemented`, or explicitly `unverified`.

## Timestamp And Frame Roles

`event_ts_ms`:

- Event time used by event-worker and evidence tasks.
- For behavior events, currently derived from pose observation timestamps.
- For face match events, currently copied from `face_observations.timestamp_ms`.
- Not sufficient by itself unless mapped to recording segment time.

`frame_uuid`:

- Intended exact frame identifier.
- Exists in `SecurityEvent`, `events`, and record request payloads.
- Currently always `null`.

`keyframe_uuid`:

- Intended Replay anchor.
- Exists in `SecurityEvent`, `events`, and record request payloads.
- Currently always `null`.

`pipeline_ts_ns`:

- Needed if using Savant Replay or sink metadata timestamp domain.
- Current metadata-sink output includes per-frame `pts`, `dts`, `duration`,
  `frame_num`, and `keyframe`, but DB rows do not preserve a `pipeline_ts_ns`
  field.

`epoch_ms`:

- Needed for operator-facing event time and recording segment index.
- Controlled segment recording needs `start_epoch_ms` and `end_epoch_ms` so
  `event_ts_ms` can become `offset_ms`.

## Exactness States

Snapshot exactness:

```text
exact_event_frame=false -> no event bbox overlay on production snapshot
exact_event_frame=true  -> bbox/ROI/face overlay allowed
```

Clip exactness:

```text
exact_event_clip=false -> clip_path/raw_clip_path must not be production ready
exact_event_clip=true  -> raw_clip.mp4 can be canonical production evidence
```

R3.3 completion requires exactness fields to be supported by a recorded mapping,
not by file existence alone.

## Planning Boundary

This planning phase makes no Savant pipeline change in planning, creates no
production recording service, creates no annotated video, and performs no
performance test.
