# R3.2D Debug Visual Rejection Report

Status: rejected. R3.2D debug visual not accepted.

Event inspected:

```text
event_id=92033da3-b2f2-4413-947c-403a5f295876
event_dir=/data/video-analytics/media/events/2026/05/30/92033da3-b2f2-4413-947c-403a5f295876/
event_type=intrusion
source_id=r3_2d_intrusion_1780124722
track_id=16
```

R3.2D current implementation must not be committed. Do not commit R3.2D debug
visual implementation. do not commit R3.2D debug visual implementation.

## Failure Symptoms

Manual inspection failed for the generated visual files:

- `snapshot.jpg` is not the exact event frame.
- `debug_overlay_snapshot.jpg` is not the exact event frame.
- The bbox overlay in `debug_overlay_snapshot.jpg` does not match the visible
  person in the image.
- `debug_raw_clip.mp4` does not contain the frame shown in `snapshot.jpg`.
- `debug_raw_clip.mp4` appears to be a source-video segment without verified
  alignment to the event timestamp.

The files are useful only as proof that media-writing code paths can produce
files. They are not acceptable as visual evidence, and they are not acceptable
as developer-facing debug visual evidence for judging detection correctness.

## Root Cause

The inspected metadata shows:

```text
snapshot_capture_mode=rtsp_current
exact_event_frame=false
snapshot_alignment_status=best_effort
overlay_allowed=false
clip_capture_mode=raw_mp4_fallback
exact_event_clip=false
clip_alignment_status=unverified
timeline_mapping=null
clip_status=not_implemented
media_status=partial
```

The snapshot is captured from the live RTSP stream after the event, not from the
event frame. The raw clip is based on an unmapped raw MP4 fallback. The event
payload bbox comes from Savant detection metadata, while the image used for
debug drawing is a different frame. Therefore bbox and frame are not same source.

There is no verified event timestamp to raw MP4 timeline mapping. There is also
no `frame_uuid`, `keyframe_uuid`, replay locator, or equivalent source-frame
reference in the inspected event payload.

## Why Rtsp Current And Unmapped Raw Fallback Cannot Be Used

`rtsp_current` is a best-effort current-frame capture mode. It can show what the
camera sees when the worker runs, but it cannot prove what the camera saw at the
event timestamp. Because snapshot is not exact event frame, drawing event bbox,
face bbox, or ROI on that image is semantically wrong.

`raw_mp4_fallback` without a verified timeline mapping cannot prove that the
exported MP4 segment contains the event timestamp. Even when a playable file is
generated, raw clip has no verified timeline mapping and must not be presented
as aligned visual evidence.

Marking these files as debug or unverified is not sufficient. The visual output
still invites manual reviewers to compare boxes, people, and video timing that
do not share a reliable source frame. This can mislead development decisions.

## What Can Be Kept

The following outputs remain useful as metadata-only evidence lifecycle checks:

- `metadata.json`
- DB `events` row status updates
- `evidence_tasks` lifecycle and API response plumbing
- canonical media fields that correctly remain non-exact:
  - `media_status=partial`
  - `clip_status=not_implemented`
  - `clip_path=null`
  - `raw_clip_path=null`
  - `exact_event_frame=false`
  - `exact_event_clip=false`

The current visual files should not be used to validate production visual
evidence.

## R3.3 Requirement

R3.3 timeline mapping required before visual evidence can be accepted. The next
stage must provide at least one reliable alignment mechanism:

- event timestamp to source MP4 PTS mapping,
- `frame_uuid` or `keyframe_uuid` propagated from the inference path,
- replay keyframe or replay segment locator,
- source segment index with event-relative time,
- or another explicit mapping that proves bbox, snapshot frame, and clip are
  from the same timeline.

Only after this mapping exists can snapshot overlay and raw clip be evaluated as
event-aligned visual evidence.

## Current Decision

R3.2D debug visual not accepted.

Do not continue generating more debug video for this path. Do not add annotated
video. Do not run performance tests. Do not change the Savant pipeline for this
rejection report.

Do not commit R3.2D debug visual implementation.
