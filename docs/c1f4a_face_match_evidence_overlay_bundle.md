# C1F.4a Face Match Evidence Overlay Bundle

## Goal

C1F.4a connects the existing face match event producer to the existing Replay
evidence pipeline and adds continuous overlay JSON for frontend rendering.

The production default is:

```text
watchlist_hit SecurityEvent
-> event-worker record_request
-> clip-worker Replay job
-> video-file-sink raw media
-> media-worker evidence bundle
-> raw_clip + annotations.jsonl + metadata
```

This phase does not create a parallel evidence pipeline. The repo already has:

- `services/face-worker/app/face_match_event_service.py`
- `services/face-worker/emit_face_match_events.py`
- `services/event-worker/app/record_request.py`
- `services/clip-worker/app/replay_client.py`
- `services/media-worker/app/worker.py`

## Why C1F.3c Had No Visual Output

C1F.3c verified the production-like face observation and gallery search path,
but it did not emit `security.events`. Without a `watchlist_hit` event, the
chain below never started:

```text
security.events
-> event-worker
-> security.record_requests
-> clip-worker
-> Replay
-> video-file-sink
-> media-worker
```

So there was no evidence bundle to visualize.

## Frame Anchors

`face_observations.payload.media` already carries Replay frame anchors from
Savant frame metadata:

- `frame_uuid`
- `keyframe_uuid`
- `previous_keyframe_uuid`
- `frame_pts`
- `frame_num`
- `ntp_timestamp`
- `metadata_source=video_frame`

`face_match_event_service` copies these fields into the `watchlist_hit` event,
so `record_request` and `clip-worker` can anchor Replay jobs without a second
RTSP pull or source extraction.

## Evidence Bundle

The media-worker finalizer writes:

```text
/data/video-analytics/media/evidence/{event_id}/
  raw_clip.mov|webm|mp4
  metadata.json
  sink_metadata.json
  event_annotation.json
  annotations.jsonl
  summary.json
```

`event_annotation.json` remains the older single event-frame annotation format.
`annotations.jsonl` is the new continuous overlay timeline for the whole Replay
window.

`annotated_clip` is not default output. The frontend overlay composes the picture
from `raw_clip` plus `annotations.jsonl` when an operator views the event.

`metadata.json` declares:

```json
{
  "media": {
    "raw_clip_path": "...",
    "annotated_clip_status": "not_generated"
  },
  "annotations": {
    "annotations_jsonl_path": ".../annotations.jsonl",
    "summary_json_path": ".../summary.json",
    "frontend_overlay_required": true,
    "annotation_mode": "continuous_jsonl"
  }
}
```

## Continuous Annotation Schema

Each JSONL line represents a timestamp/frame group. Lines are sorted by
`timestamp_ms`, and `time_offset_ms` is relative to raw clip start:

```json
{
  "schema_version": "1.0",
  "source_id": "c1e_rtsp_replay",
  "camera_id": "cam_c1e_rtsp_replay",
  "timestamp_ms": 619734,
  "time_offset_ms": 5000,
  "frame_num": 22360,
  "frame_uuid": "...",
  "keyframe_uuid": "...",
  "previous_keyframe_uuid": "...",
  "frame_pts": 933674066666,
  "objects": [
    {
      "object_type": "face",
      "object_id": "face:...",
      "track_id": "636",
      "bbox": {
        "format": "cxcywh",
        "values": [100.0, 200.0, 50.0, 70.0],
        "confidence": 0.91
      },
      "landmarks": {
        "format": "5_point",
        "points": []
      },
      "identity": {
        "status": "matched",
        "person_id": 2,
        "external_person_id": "test:archive:reese",
        "display_name": "Reese",
        "similarity": 0.5115,
        "rank": 1,
        "threshold": 0.5,
        "match_status": "above_threshold"
      },
      "pose": {
        "status": "unavailable",
        "reason": "pose_observation_not_yet_persisted"
      },
      "action": {
        "status": "none",
        "event_type": null,
        "severity": null
      },
      "style": {
        "bbox_color": "#D50000",
        "label_color": "#D50000",
        "line_width": 3,
        "label": "Reese 0.51",
        "priority": 100,
        "reason": "alert_hit"
      }
    }
  ]
}
```

The schema includes `pose` and `action` now even when unavailable. They are
reserved fields so later pose/action behavior evidence can use the same overlay
format without changing frontend contracts.

No annotation line stores embeddings, JPEG/PNG/RAW bytes, crop bytes, or base64.

## Style Policy

Colors come from `services/media-worker/app/annotation_style.py`, not from
scattered hardcoded call sites.

Initial style policy:

- Watchlist or live search hit: red, `alert_hit`, priority 100.
- High behavior event: orange/red, `behavior_event`, priority 90.
- Identity match above threshold: green, `identity_match`, priority 50.
- Low similarity candidate: yellow, `low_similarity_candidate`, priority 30.
- Unknown face: gray, `unknown_face`, priority 10.

Future behavior events can drive color changes by setting action/severity and
letting the style policy choose the higher-priority visual state.

## Smoke Result Semantics

- `PASS_FACE_MATCH_EVIDENCE_BUNDLE_READY`: a face match event went through
  event-worker, record_request, clip-worker, Replay, video-file-sink, and
  media-worker; the bundle has raw media and continuous annotations.
- `PASS_NO_MATCH_EVENT_NOT_EMITTED`: gallery and observations exist, but no
  Finch/Reese match met the configured threshold, so no event evidence should
  be produced.
- `FAIL_EVENT_NOT_INGESTED`: `security.events` was published but event-worker
  did not persist the event.
- `FAIL_RECORD_REQUEST_NOT_CREATED`: event-worker did not publish a
  `security.record_requests` message.
- `FAIL_REPLAY_JOB_NOT_CREATED`: clip-worker did not create a Replay job.
- `FAIL_RAW_CLIP_NOT_GENERATED`: video-file-sink/media-worker did not produce a
  non-empty raw clip.
- `FAIL_ANNOTATIONS_NOT_GENERATED`: `annotations.jsonl` or `summary.json` was
  missing or invalid.
- `FAIL_EMBEDDING_LEAK`: annotation output leaked an embedding vector.
- `FAIL_IMAGE_BYTES_LEAK`: annotation output leaked image/crop/base64 bytes.

`C1F4A_MATCH_THRESHOLD` defaults to `0.5`. Setting
`C1F4A_MATCH_THRESHOLD=0.35` is allowed for debug calibration; reports must mark
`threshold_calibration=true`.

## Scope Boundaries

C1F.4a does not implement:

- Full watchlist/live_search API behavior.
- API or frontend.
- Production evidence clip policy beyond the event-triggered raw bundle.
- Default `annotated_clip` rendering.
- Second RTSP intake.
- Source extraction or manual clipping from RTSP.
- Changes to YOLOv8-Face full-frame primary detector topology.

## Disk Notes

Replay cache remains a TTL rolling cache. Evidence clips are generated only for
events/hits. The system does not save full continuous video for all 60 cameras.

## Next Steps

If `PASS_FACE_MATCH_EVIDENCE_BUNDLE_READY`, the next useful step is either
C1F.4b frontend overlay preview or C1F.5 watchlist/live_search audit.

If `PASS_NO_MATCH_EVENT_NOT_EMITTED`, use a video segment with Finch/Reese or run
a positive self-match production control.
