# R3.2A Metadata + Snapshot Evidence MVP

Status: implementation MVP. R3.2A generates visible event evidence through
`metadata.json` and `snapshot.jpg` only.

R3.2A does not generate `raw_clip.mp4`. Raw clip generation is deferred to a
later clip worker phase.

R3.2A does not generate `annotated_clip.mp4`. Annotated clip remains
optional/on-demand only for debug or report export after the performance
baseline.

R3.2A must not create default dual video output.

R3.2A does not change the Savant pipeline and does not run performance tests.

## Scope

Supported event types for the MVP:

```text
intrusion
watchlist_hit
```

The shared flow is:

```text
event
  -> evidence_task
  -> metadata.json
  -> snapshot.jpg
  -> events.snapshot_path / payload.media.metadata_path / media_status
  -> evidence_tasks.snapshot_path / metadata_path / status
  -> GET /api/v1/events/{event_id}/evidence
```

If snapshot capture fails, metadata is still generated. The event becomes
`media_status=partial`, with `metadata_status=ready`,
`snapshot_status=failed`, and `clip_status=not_implemented`.

## Output Path

R3.2A writes controlled event evidence under:

```text
/data/video-analytics/media/events/YYYY/MM/DD/<event_id>/
  metadata.json
  snapshot.jpg
```

If `/data/video-analytics/media` is not writable, development fallback to
`./tmp/r3_2a_media` is allowed only when metadata records:

```text
storage_fallback_used=true
storage_fallback_reason=...
```

## Snapshot Capture Backend

`ffprobe is smoke preflight only`. It checks RTSP reachability before runtime
smoke and is not a production snapshot capture backend.

The default production snapshot backend is OpenCV or GStreamer-compatible
capture. R3.2A implements:

```text
capture_backend=opencv
snapshot_capture_mode=rtsp_current
exact_event_frame=false
```

`rtsp_current` is best-effort current frame capture and does not guarantee the
exact event frame.

`ffmpeg` command-line capture is explicit fallback/debug only and is not the
production default.

## Metadata Schema

`metadata.json` contains:

```json
{
  "event": {
    "event_id": "...",
    "source_event_id": "...",
    "event_type": "intrusion",
    "algorithm_type": "...",
    "camera_id": "...",
    "source_id": "...",
    "track_id": "...",
    "person_id": null,
    "severity": "medium",
    "confidence": 0.91,
    "start_ts_ms": 123,
    "end_ts_ms": 123
  },
  "evidence": {
    "media_status": "ready",
    "snapshot_status": "ready",
    "clip_status": "not_implemented",
    "metadata_status": "ready",
    "snapshot_path": ".../snapshot.jpg",
    "raw_clip_path": null,
    "annotated_clip_path": null,
    "metadata_path": ".../metadata.json"
  },
  "capture": {
    "capture_backend": "opencv",
    "snapshot_capture_mode": "rtsp_current",
    "exact_event_frame": false,
    "fallback_used": false,
    "fallback_reason": null,
    "source_url_redacted": true
  },
  "overlay": {
    "type": "behavior_event",
    "items": [],
    "overlay_missing_reason": "person_bbox, roi_polygon"
  },
  "payload": {}
}
```

RTSP credentials must not be written into metadata. Source URL details are
redacted or summarized only.

## Overlay

For `watchlist_hit`, R3.2A attempts face bbox + matched person label +
similarity from the event payload.

For `intrusion`, R3.2A attempts ROI polygon and person bbox/track from the
event payload.

If overlay data is missing, snapshot may be a plain current frame. Metadata
must include `overlay_missing_reason`.

## API

`GET /api/v1/events/{event_id}/evidence` returns 200 for `ready` and `partial`
states. The response includes:

```text
media_status
snapshot_status
clip_status
metadata_status
snapshot_path
clip_path
metadata_path
snapshot_url
clip_url
metadata_url
error_message
evidence_tasks
```

## Worker / Helper

R3.2A keeps the core implementation in `services/clip-worker/app/` and exposes
a helper:

```bash
python services/clip-worker/process_evidence_task.py --event-id <event_id>
```

This helper is used by smoke tests and can be wired into the long-running
clip-worker lifecycle in a later phase.
