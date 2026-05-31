# P1c-RTSP Replay Event-triggered Evidence Bundle

Status: POC only.

## Goal

Single RTSP path:

```text
rtsp://10.37.57.112:8554/live/1080movie
  -> source-adapter
  -> replay-service
  -> savant-security
  -> Redis security.events
  -> event-worker
  -> PostgreSQL events
  -> security.record_requests
  -> clip-worker
  -> Replay job
  -> video-file-sink
  -> media-worker raw evidence finalizer
  -> /media/evidence/{event_id}/raw_clip.*
```

## Boundary

- no second RTSP pull
- no local file source
- no source extraction fallback
- no clip-worker/media-worker bypass
- no annotated_clip
- no production compose change
- no media artifacts committed

## Runtime Test Policy

P1c must follow `docs/runtime_test_policy.md`:

- Fixed RTSP source: `rtsp://10.37.57.112:8554/live/1080movie`.
- Code changes → restart container, not default rebuild.
- Replay TTL must be configured.
- POC containers must use explicit names.

## POC Inputs

- `source_id: p1c_rtsp_replay`
- `camera_id: cam_p1c_rtsp_replay`
- `input_type: rtsp`
- `input_uri: rtsp://10.37.57.112:8554/live/1080movie`

## Replay

- in_stream: `router+bind:tcp://0.0.0.0:5555`
- out_stream: `dealer+connect:tcp://savant-security:5557`

## Expected Output

The evidence bundle lands under:

```text
/data/video-analytics/media/evidence/{event_id}/
```

Files:

- `raw_clip.mov` or `raw_clip.webm` or `raw_clip.mp4`
- `metadata.json`
- `event_annotation.json`

`events.clip_path` must point to the raw clip file.

## Notes

- `event-worker` publishes `security.record_requests` from intrusion events.
- `clip-worker` prefers `previous_keyframe_uuid`, then `keyframe_uuid`, then keyframe lookup.
- `media-worker` finalizes raw evidence only when `P1_RAW_CLIP_FINALIZER_ENABLED=true`.
