# Phase 3H.1 — Official Metadata Sink Verification

Date: 2026-05-25
Status: **VERIFIED** — Both smoke tests 13/13 passed.

## 1. Purpose

Phase 3H proved official ZMQ source ingestion (source-adapter → Savant → Redis/DB). Phase 3H.1 proves the **other half** of the official Savant data plane: that Savant's processed output metadata (with objects, bbox, track_id) flows through the official `zeromq_sink` and can be consumed by an official sink adapter.

## 2. Topology

```
testVideo/test.mp4
  -> source-adapter (video_loop.sh -> ZMQ DEALER connect:tcp://savant-zmq:5555)
    -> savant-zmq (zeromq_source_bin ROUTER bind)
      -> YOLO26-pose / nvtracker / behavior_event_export_probe
        -> Redis security.events -> event-worker -> PostgreSQL
        -> ZMQ sink pub+bind:tcp://0.0.0.0:5556
          -> metadata-sink (metadata_json.py sub+connect) -> NDJSON
```

## 3. What Was Changed

### 3.1 Compose changes

**`infra/docker-compose.phase3h-zmq.yml`**:
- `ZMQ_SINK_ENDPOINT` changed from `pub+connect:ipc:///tmp/sink.ipc` to `pub+bind:tcp://0.0.0.0:5556`
- New `metadata-sink` service using official `metadata_json.py` adapter:
  - Image: `ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0`
  - Entrypoint: `python -m adapters.python.sinks.metadata_json`
  - Env: `ZMQ_ENDPOINT=sub+connect:tcp://savant-zmq:5556`, `FILENAME_PATTERN=/media/phase3h-metadata/%source_id%-metadata.ndjson`, `SKIP_FRAMES_WITHOUT_OBJECTS=false`

### 3.2 New files

| File | Purpose |
|---|---|
| `scripts/smoke/check_phase3h_metadata_sink.sh` | 13-check smoke test |
| `docs/phase3h_1_metadata_sink_verification.md` | This document |

### 3.3 What was NOT changed

- No existing module, compose service, or smoke test was modified beyond the ZMQ_SINK_ENDPOINT line.
- Phase 3H ZMQ ingestion pipeline is unaffected (verified: smoke test still 13/13).
- No replay-service, clip-worker, media-worker, or video-file-sink added.

## 4. Metadata Output Evidence

### 4.1 File statistics

- **File**: `media/phase3h-metadata/phase3h%-metadata.ndjson`
- **Size**: 46 MB (continuously growing)
- **Total frames**: 6,000+
- **Frames with objects**: 3,570+
- **Objects in last 500 frames**: 5,765

### 4.2 Sample frame metadata

```json
{
  "source_id": "phase3h",
  "framerate": "30/1",
  "width": 1920,
  "height": 1080,
  "pts": 1246899999918,
  "keyframe": true,
  "codec": "h264",
  "frame_num": 1871,
  "metadata": {
    "objects": [
      {
        "model_name": "yolo26_pose",
        "label": "person",
        "object_id": 7,
        "bbox": {
          "xc": 1625.25,
          "yc": 1022.97,
          "width": 142.5,
          "height": 112.06,
          "angle": 0.0
        },
        "confidence": 0.606,
        "attributes": [
          {
            "element_name": "yolo26_pose",
            "name": "keypoints",
            "value": [1635.0, 1006.59, 0.710, ...],
            "confidence": 1.0
          }
        ]
      }
    ]
  }
}
```

### 4.3 Verified fields

| Field | Present | Evidence |
|---|---|---|
| `source_id` | phase3h | All frames |
| `frame_num` | Incrementing (0 → 6000+) | Smoke check [11] |
| `pts` | Nanosecond timestamps | Smoke check [12] |
| `metadata.objects[].label` | person | Smoke check [7] |
| `metadata.objects[].bbox` | xc, yc, width, height, angle | Smoke check [8] |
| `metadata.objects[].object_id` | Integer track_id (nvtracker) | Smoke check [9] |
| `metadata.objects[].confidence` | Float confidence | Verified |
| `metadata.objects[].attributes` | keypoints (17 COCO triplets) | Verified |
| Tracker continuity | Same object_id across 3+ frames | Smoke check [13] |

### 4.4 Bbox coordinate system

The ZMQ sink metadata uses **center-based (xc, yc)** bbox format. This is the Savant internal representation. The event-worker's `person_pose_adapter.py` converts from center (xc, yc) to top-left (x, y) before writing to the DB event payload:

```
x = xc - width/2
y = yc - height/2
```

This confirms the coordinate conversion chain:
- **Savant ZMQ sink**: center-based (xc, yc, width, height)
- **person_pose_adapter**: converts to top-left (x, y, width, height)
- **PostgreSQL event payload.bbox**: top-left (x, y, width, height)

## 5. Relationship to Redis Events

The same Savant module produces **both**:

| Output | Format | Consumer | Content |
|---|---|---|---|
| ZMQ sink (metadata) | Savant protocol VideoFrame | metadata-sink → NDJSON | All frames, all objects, raw bbox (center-based) |
| Redis events (behavior) | SecurityEvent JSON | event-worker → PostgreSQL | Only behavior-triggered events, event bbox (top-left), event metadata |

The ZMQ sink metadata is the **raw frame-level output** — every frame, every object, every attribute. The Redis events are the **filtered behavioral output** — only frames that trigger intrusion/loitering/etc. rules, with additional event context (event_type, camera_id, track_time, etc.).

## 6. Smoke Test Results

### Phase 3H.1 metadata sink smoke: 13/13 passed

```
--- Results: 13/13 passed, 0 failed ---
```

### Phase 3H ingestion smoke (regression): 13/13 passed

```
--- Results: 13/13 passed, 0 failed ---
```

## 7. What This Enables

1. **Full official data plane verified**: ZMQ source input → Savant processing → ZMQ sink output. Both ends of the official Savant adapter architecture confirmed.
2. **Replay integration path**: The metadata-sink adapter pattern proves a downstream consumer can read Savant's processed output. Replay Service can subscribe to the same ZMQ sink stream to cache keyframes.
3. **Frame-level alignment**: When Replay is added to this topology, bbox metadata from Savant and keyframes from Replay come from the **same ZMQ frame stream** — single ingestion, natural alignment.

## 8. Next Steps

Per `docs/production_ingestion_topology_policy.md`:
1. Add replay-service back, consuming the same ZMQ sink stream
2. Implement timestamp-domain mapping (UUIDv7 keyframe UUID → Savant PTS)
3. Add clip-worker and media-worker back for end-to-end bbox/snapshot verification
