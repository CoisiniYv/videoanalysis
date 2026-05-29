# R3.1A Behavior Event Evidence MVP

Status: implementation MVP for behavior event evidence. Intrusion is the first
supported behavior event because it already exists in the YOLO26-pose +
nvtracker + ROI behavior rule path.

This phase does not implement new algorithm logic, does not run performance
tests, and does not change the Savant pipeline.

## Scope

R3.1A covers:

```text
intrusion SecurityEvent
  -> event-worker
  -> PostgreSQL events
  -> evidence_tasks
  -> media_status
  -> API evidence query
```

R3.1A does not cover:

```text
loitering / fall / chasing / crowd_gathering / wall_climb logic
watchlist_hit / live_search_hit
trajectory / appearances API
production snapshot extraction
production clip extraction
annotated video generation
performance testing
```

## Lifecycle

Supported evidence task states:

```text
pending
processing
ready
failed
not_implemented
```

The current R3.1A deployment records evidence tasks and marks behavior-event
media generation as `not_implemented` with a clear reason:

```text
R3.1A behavior evidence MVP created the evidence task, but production
snapshot/clip/metadata generation is not implemented in this deployment.
```

This is intentional. Event ingestion must remain successful even when media is
not available. Snapshot and clip generation will move to a media worker in a
later pass.

For legacy intrusion producers that still emit `snapshot_required=false` and
`clip_required=false`, the event-worker applies an R3.1A-only default evidence
policy for `intrusion` events:

```json
{
  "snapshot_required": true,
  "clip_required": true,
  "pre_seconds": 5,
  "post_seconds": 10
}
```

This default is scoped to the existing intrusion event path and does not add
new behavior algorithm logic.

## Metadata JSON

The target metadata file is:

```text
/data/video-analytics/media/events/YYYY/MM/DD/<event_id>/metadata.json
```

Target structure:

```json
{
  "event_id": "event uuid",
  "source_event_id": "savant_security:cam:track:intrusion:ts",
  "event_type": "intrusion",
  "camera_id": "cam_001",
  "source_id": "source_001",
  "track_id": "3",
  "start_ts_ms": 1000,
  "end_ts_ms": 2000,
  "severity": "medium",
  "confidence": 0.91,
  "snapshot_required": true,
  "clip_required": true,
  "media_status": "not_implemented",
  "snapshot_path": null,
  "clip_path": null,
  "payload": {}
}
```

R3.1A does not write this file yet. The API and database schema expose
`metadata_path`, but it remains `null` while media generation is
`not_implemented`.

## Media Policy

R3.1A keeps real media generation disabled, but it fixes the production policy
for later implementation:

1. `raw_clip.mp4` is the canonical evidence video when `clip_required=true`.
2. `annotated_clip.mp4` is optional/on-demand only for debug, report export, or
   an explicit operator request.
3. The default production path must not generate two video files per event.
4. Overlay data should be stored in `metadata.json`; frontend clients should
   render dynamic overlays from that metadata while playing `raw_clip.mp4`.
5. `snapshot.jpg` may be a lightweight annotated still image.
6. Annotated video generation is deferred until after a performance baseline or
   a report/export phase.

## API

`GET /api/v1/events/{event_id}/evidence` returns a stable response even when
media is not generated:

```json
{
  "event_id": "event uuid",
  "source_event_id": "...",
  "event_type": "intrusion",
  "media_status": "not_implemented",
  "snapshot_path": null,
  "clip_path": null,
  "metadata_path": null,
  "snapshot_url": null,
  "clip_url": null,
  "metadata_url": null,
  "error_message": "R3.1A behavior evidence MVP created the evidence task...",
  "event": {},
  "evidence_tasks": []
}
```

The endpoint must return status and task details rather than a 500 when
evidence is unavailable.

## Storage

The default evidence output path remains:

```text
/data/video-analytics/media/events/YYYY/MM/DD/<event_id>/
```

If a future smoke or development worker falls back to `./tmp`, it must record:

```text
storage_fallback_used=true
```

R3.1A does not write media outputs into the repository working directory.

## Smoke Verification

The R3.1A smoke uses the existing single MediaMTX RTSP path to trigger
intrusion events, then verifies:

1. an intrusion event exists for the source id;
2. an evidence task exists for the event;
3. `events.media_status` is one of the supported lifecycle states;
4. the API evidence endpoint returns a stable response;
5. `snapshot_path`, `clip_path`, and `metadata_path` may be null when
   `media_status=not_implemented`.

This is not a performance test.
