# R3.2A Snapshot Capture Backend Policy

Status: design correction and implementation plan only. This document does not
implement snapshot generation, raw clip generation, annotated clip generation,
a Savant pipeline change, or a performance test.

R3.2A will add visible evidence through `metadata.json` and `snapshot.jpg`.
Before implementation, the snapshot capture backend policy is corrected to stay
aligned with the project mainline: Savant / DeepStream / GStreamer.

## Corrected Snapshot Source Strategy

`ffprobe` is allowed only for smoke preflight. It may check whether a live RTSP
URL is reachable before a smoke test starts, but it is not a production
snapshot capture backend. In short: `ffprobe` is not a production snapshot capture backend.

The production default snapshot capture backend must be one of:

```text
opencv
gstreamer-compatible capture
```

The first R3.2A MVP may use OpenCV `VideoCapture` for live RTSP current-frame
capture when the runtime image supports it. A later implementation may replace
or augment this with an explicit GStreamer-compatible capture path.

`ffmpeg` command-line capture is explicit fallback/debug only. It must not be
the default production backend.

## Metadata Requirements

For OpenCV live RTSP current-frame capture, `metadata.json` must include:

```json
{
  "capture": {
    "capture_backend": "opencv",
    "snapshot_capture_mode": "rtsp_current",
    "exact_event_frame": false,
    "source_url_redacted": true
  }
}
```

For future GStreamer-compatible capture, `metadata.json` must include:

```json
{
  "capture": {
    "capture_backend": "gstreamer",
    "snapshot_capture_mode": "rtsp_current",
    "exact_event_frame": false,
    "source_url_redacted": true
  }
}
```

For explicit ffmpeg fallback/debug capture, `metadata.json` must include:

```json
{
  "capture": {
    "capture_backend": "ffmpeg_fallback",
    "snapshot_capture_mode": "rtsp_current",
    "exact_event_frame": false,
    "source_url_redacted": true,
    "fallback_used": true,
    "fallback_reason": "opencv_capture_failed"
  }
}
```

`rtsp_current` is best-effort current-frame capture. It does not guarantee the
exact event frame. The metadata must state `exact_event_frame=false`.

Do not write RTSP credentials into `metadata.json`. If a source URL is recorded
for diagnostics, it must be redacted.

## Evidence Media Policy

R3.2A does not generate `raw_clip.mp4`.

R3.2A does not generate `annotated_clip.mp4`.

R3.2A must not create default dual video output. Overlay information belongs in
`metadata.json`, and `snapshot.jpg` may include lightweight annotation when the
event payload has enough bbox, ROI, or face-match data.

## Implementation Plan

The R3.2A media service should expose an explicit backend selection:

```text
SNAPSHOT_CAPTURE_BACKEND=opencv
SNAPSHOT_CAPTURE_BACKEND=gstreamer
SNAPSHOT_CAPTURE_BACKEND=ffmpeg_fallback
```

Default:

```text
SNAPSHOT_CAPTURE_BACKEND=opencv
```

Allowed behavior:

1. Try OpenCV/GStreamer-compatible capture by default.
2. If no frame source exists, write `metadata.json` with
   `snapshot_status=not_implemented` or `failed`.
3. If an operator explicitly enables fallback/debug, use
   `capture_backend=ffmpeg_fallback` and set `fallback_used=true` plus
   `fallback_reason`.
4. Always write metadata when the event row is available, even if snapshot
   capture fails.

## Scope Guards

R3.2A must not modify the Savant pipeline.

R3.2A must not generate `raw_clip.mp4`.

R3.2A must not generate `annotated_clip.mp4`.

R3.2A must not run performance tests.
