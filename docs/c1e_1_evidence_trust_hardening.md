# C1E.1 Evidence Trust Hardening

Status: dev-only evidence trust hardening on top of C1E.

## Scope

C1E.1 keeps the C1E single-event Replay evidence POC scope:

```text
RTSP -> source-adapter -> replay-service -> savant-security
  -> event-worker -> record_request -> clip-worker
  -> Replay job -> video-file-sink -> media-worker/finalizer
```

It does not add incident coalescing, continuous recording, frontend/API work,
multi-camera behavior, gallery recognition, or performance testing.

## Duration Verification

`services/media-worker/app/worker.py` now probes `raw_clip` duration before
writing business metadata.

Metadata schema:

```json
{
  "media": {
    "raw_clip_duration": 10.05,
    "duration_probe_status": "ok"
  }
}
```

The helper is `_probe_video_duration_seconds(path)`. It uses `ffprobe` when
available:

```bash
ffprobe -v error -show_entries format=duration -of json <raw_clip>
```

If duration cannot be read, metadata uses:

```json
{
  "raw_clip_duration": null,
  "duration_probe_status": "failed"
}
```

The C1E smoke requires `duration_probe_status=ok` and checks:

```text
8.0 <= raw_clip_duration <= 15.0
```

This prevents a `raw_clip_duration=0` evidence bundle from passing.

## Keyframe Fallback Policy

Normal C1E evidence must use the event-provided Replay anchor:

```text
previous_keyframe_uuid preferred
keyframe_uuid fallback
```

`clip-worker` no longer performs an implicit unbounded keyframe lookup when a
record request lacks both fields. Default behavior is:

```text
ALLOW_UNBOUNDED_KEYFRAME_FALLBACK=false
missing keyframe -> failed
error=missing_keyframe_uuid_and_anchored_lookup_unavailable
```

`ReplayClient.find_keyframe(ts_ms > 0)` now keeps the timestamp window and sends
`from` / `to` to Replay instead of silently clearing them. Unbounded lookup is
only reachable when a caller explicitly enables the fallback path.

The C1E smoke reports:

```text
normal_anchor_source
keyframe_lookup_used
unbounded_keyframe_fallback_default
```

Expected C1E normal path:

```text
normal_anchor_source=previous_keyframe_uuid|keyframe_uuid
keyframe_lookup_used=no
unbounded_keyframe_fallback_default=false
```

## ROI Annotation Lookup

`event_annotation.json` now attempts ROI overlay lookup in this order:

```text
payload.roi_polygon
payload.zone_polygon
payload.media.roi_polygon
payload.media.zone_polygon
camera config by camera_id/source_id + zone_id
```

When found from camera config:

```json
{
  "type": "roi_polygon",
  "zone_id": "c1e_rtsp_full_frame",
  "points": [[0.0, 0.0], [1920.0, 0.0], [1920.0, 1080.0], [0.0, 1080.0]],
  "source": "camera_config"
}
```

If no ROI can be found, the annotation stays partial:

```json
{
  "annotation_status": "partial",
  "missing": ["roi_polygon"],
  "roi_lookup_status": "not_found"
}
```

No ROI is fabricated.

## Evidence-worker Filtering

`services/evidence-worker/app/evidence.py::find_metadata_file(video_dir, source_id)`
is not part of the current C1E Replay raw clip chain. C1E uses the
media-worker/finalizer path instead.

The old evidence-worker function is still hardened: it now returns only a
`metadata.json` whose path contains the requested `source_id`, including sink
path variants with trailing `%` or URL-decoded directory names. If no matching
source exists, it returns `None` instead of the first metadata file.

## Known Limitation

Video File Sink path may contain trailing `%` and `unknown%` directory segments:

```text
/media/replay-sink-output/c1e/<run_id>/replay-event-<event_id>%/unknown%/
```

media-worker currently handles this path. H/P2 cleanup may normalize sink
naming later. C1E.1 does not refactor sink path generation.

## Boundaries

Maintained:

```text
input_type = rtsp
input_uri = rtsp://10.37.57.112:8554/live/1080movie
no local file
no source extraction fallback
no second RTSP pull
no annotated_clip
no production compose change
no media artifacts committed
```
