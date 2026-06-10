# R3.1B Face Match Evidence MVP

Status: implementation MVP for face match evidence events. This phase does not
change the Savant pipeline, does not implement a new face detector, does not
generate real snapshot or clip files, and does not run performance tests.

## Scope

R3.1B implements the first face intelligence alarm path:

```text
face_observation
  -> active person_gallery_embeddings
  -> pgvector gallery match
  -> watchlist_hit SecurityEvent
  -> Redis security.events
  -> event-worker
  -> events
  -> evidence_tasks
  -> media_status=not_implemented
  -> GET /api/v1/events/{event_id}/evidence
```

`live_search_hit` remains contract-only/deferred in this phase. It must reuse
the same `face_intelligence` algorithm type and the same SecurityEvent ->
EvidenceTask path when implemented later.

## Watchlist Hit MVP

The MVP producer is outside Savant:

```text
services/face-worker/emit_face_match_events.py
services/face-worker/app/face_match_event_service.py
```

It reads stored `face_observations`, searches active gallery embeddings through
`FaceVectorStore.search_gallery()`, and emits `watchlist_hit` events to Redis
`security.events`. The event-worker remains the only component that writes
`events` and `evidence_tasks`.

Default threshold:

```text
FACE_MATCH_THRESHOLD=0.50
```

Smoke tests may use a lower calibration threshold, for example
`R3_1B_MATCH_THRESHOLD=0.35`, and must report that explicitly.

## Idempotency

R3.1B uses this idempotent source event id:

```text
watchlist_hit:{source_observation_id}:{person_id}
```

The same face observation and matched person therefore map to the same
`events.source_event_id`.

## SecurityEvent

R3.1B emits:

```text
event_type=watchlist_hit
algorithm_type=face_intelligence
snapshot_required=true
clip_required=true
evidence_policy={snapshot_required:true, clip_required:true, pre_seconds:5, post_seconds:10}
```

The payload includes:

```json
{
  "matched_person": {
    "person_id": 318,
    "external_person_id": "demo:f4_3:finch",
    "name": "Finch"
  },
  "match": {
    "similarity": 0.72,
    "threshold": 0.50,
    "gallery_embedding_id": 267,
    "source_observation_id": "face:..."
  },
  "observation": {
    "camera_id": "cam_001",
    "source_id": "source_001",
    "track_id": "3",
    "timestamp_ms": 12345,
    "face_bbox": [0, 0, 10, 10],
    "landmarks": [],
    "quality": 1.0
  },
  "overlay": {
    "type": "face_match",
    "label": "Finch 0.72",
    "face_bbox": [0, 0, 10, 10],
    "person_bbox": null
  },
  "media": {
    "snapshot_required": true,
    "clip_required": true,
    "media_status": "not_implemented",
    "raw_clip_path": null,
    "annotated_clip_path": null,
    "metadata_path": null
  }
}
```

## Evidence Media Policy

R3.1B follows the R3.1 production evidence media policy:

1. `raw_clip.mp4` is the canonical production evidence video.
2. `annotated_clip.mp4` is optional/on-demand only for debug, report export, or
   explicit operator request.
3. The default production path must not generate two video files per event.
4. Overlay information is stored in the event payload and metadata-ready
   structure; frontend clients render dynamic overlays from `metadata.json`.
5. `snapshot.jpg` may be a lightweight annotated still image in a later media
   worker pass.
6. Annotated video generation is deferred until after the performance baseline
   or report/export phase.

R3.1B does not generate `raw_clip.mp4`, `annotated_clip.mp4`, `snapshot.jpg`, or
`metadata.json`. The shared evidence lifecycle records `media_status` as
`not_implemented`, with null paths and a clear error message.

## Boundaries

R3.1B does not change the Savant pipeline.

R3.1B does not:

1. change `modules/savant_security/module.yml`;
2. change YOLOv8-Face, AdaFace, or the dual-primary pipeline;
3. re-register Finch/Reese faces;
4. implement trajectory / appearances API;
5. restore F4.3 debug scripts as production evidence;
6. run performance tests.
