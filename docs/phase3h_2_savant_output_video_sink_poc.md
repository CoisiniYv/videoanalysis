# Phase 3H.2 — Savant Output Video File Sink POC

Date: 2026-05-25
Status: **VERIFIED** — All three smoke tests 13/13 passed.

## 1. Purpose

Phase 3H proved official ZMQ source ingestion. Phase 3H.1 proved ZMQ sink metadata output. Phase 3H.2 proves the **complete** single-ingestion video evidence chain: Savant's output (encoded video frames + detection metadata) flows through the official ZMQ sink to a video-file-sink, producing frame-aligned video and metadata from the **same** frame stream.

## 2. Full Topology

```
testVideo/test.mp4
  -> source-adapter (video_loop.sh -> ZMQ DEALER connect:tcp://savant-zmq:5555)
    -> savant-zmq (zeromq_source_bin ROUTER bind)
      -> YOLO26-pose / nvtracker / behavior_event_export_probe
        -> GPU NVENC encode (h264, nvenc)
        -> Redis security.events -> event-worker -> PostgreSQL
        -> ZMQ sink pub+bind:tcp://0.0.0.0:5556
          -> metadata-sink (metadata_json.py) -> NDJSON
          -> video-file-sink (video_files.py) -> video.mov + metadata.json
```

All consumers of the ZMQ sink receive the **same** frames with the **same** metadata. Frames and objects are inherently aligned — no Replay offset, no dual loop.

## 3. What Was Changed

### 3.1 Compose changes (`infra/docker-compose.phase3h-zmq.yml`)

**savant-zmq**: Added `OUTPUT_FRAME` env var to enable GPU frame encoding:
```yaml
OUTPUT_FRAME: '{"codec":"h264","encoder":"nvenc","encoder_params":{"iframeinterval":25}}'
```

**video-file-sink**: New service using official `video_files.py` adapter:
```yaml
video-file-sink:
  image: ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0
  entrypoint: ["python", "/opt/savant/adapters/gst/sinks/video_files.py"]
  environment:
    ZMQ_ENDPOINT: sub+connect:tcp://savant-zmq:5556
    DIR_LOCATION: /media/phase3h-savant-output/%source_id%/%src_filename%/
    CHUNK_SIZE: "0"
```

### 3.2 New files

| File | Purpose |
|---|---|
| `scripts/smoke/check_phase3h_video_file_sink.sh` | 13-check smoke test |
| `media/phase3h-savant-output/evidence/evidence_frame.jpg` | Visual evidence: annotated frame |
| `docs/phase3h_2_savant_output_video_sink_poc.md` | This document |

### 3.3 What was NOT changed

- No existing module, compose service, or smoke test was modified (beyond adding OUTPUT_FRAME to savant-zmq).
- YOLO26-pose model, nvtracker, behavior rules unchanged.
- Event schema unchanged.
- Redis event exporter unchanged.
- Phase 3H / 3H.1 verified paths unaffected (regression smoke tests 13/13 each).

## 4. Evidence

### 4.1 Video output

| Property | Value |
|---|---|
| Codec | h264 (NVENC GPU) |
| Resolution | 1920 x 1080 |
| Frame rate | 30/1 |
| Duration | ~76s (continuously growing) |
| File size | 77MB (continuously growing) |

### 4.2 Metadata output

| Statistic | Value |
|---|---|
| Total frames | 5,200+ |
| Frames with objects | 5,200+ (100%) |
| Total objects | 60,000+ |
| Object label | person |
| Object bbox format | xc, yc, width, height, angle (center-based) |
| Track IDs | Integer, nvtracker-assigned, persistent across frames |
| Keypoints | 17 COCO keypoints per person |

### 4.3 Evidence frame

**File**: `media/phase3h-savant-output/evidence/evidence_frame.jpg`

- Frame: 200 from video.mov
- track_id=26, confidence=0.742
- Bbox center: xc=780.4, yc=993.0, w=158.2, h=172.0
- Converted to top-left: x=701, y=907, w=158, h=172
- 14 person objects drawn with bbox overlay
- Same frame metadata = same frame image (single ingestion, no offset)

### 4.4 Bbox coordinate system

The video-file-sink metadata uses center-based (xc, yc) coordinates — the Savant internal representation. For drawing, the evidence frame generation converts to top-left:

```
x = xc - width/2
y = yc - height/2
```

This matches the conversion in `person_pose_adapter.py` used by the Redis event exporter.

## 5. Smoke Test Results

### Phase 3H.2 video file sink: 13/13 passed

```
OK  [1] compose file exists
OK  [2] Savant OUTPUT_FRAME is configured
OK  [3] video.mov exists
OK  [4] metadata.json exists
OK  [5] video.mov has content (>100KB)
OK  [6] metadata.json has content (>1KB)
OK  [7] metadata.json has frames with objects
OK  [8] object label 'person' found
OK  [9] object has bbox (xc/yc/width/height)
OK  [10] object has integer object_id (track_id)
OK  [11] video dimensions match metadata
OK  [12] evidence_frame.jpg exists
OK  [13] metadata source_id is phase3h
```

### Phase 3H ingestion (regression): 13/13 passed

### Phase 3H.1 metadata sink (regression): 13/13 passed

## 6. Phase 3B Comparison — Why This Is Different

| Aspect | Phase 3B (Replay bypass) | Phase 3H.2 (Savant output) |
|---|---|---|
| Frame source for bbox | Savant reads RTSP independently | Source-adapter → ZMQ → Savant |
| Frame source for video | Source-adapter → ZMQ → Replay | Same ZMQ sink from Savant |
| Number of frame streams | 2 (independent loops) | 1 (single ingestion) |
| Bbox/snapshot alignment | Time-domain mismatch | Same-frame: metadata = video |
| Keyframe lookup | Unbounded (from_ns=None, to_ns=None) | Not needed (same stream) |
| Encoder | None (Replay stores raw) | NVENC h264 in Savant pipeline |
| Evidence frame | Requires Replay clip extraction at offset | Direct frame from Savant output |

The critical difference: Phase 3H.2 produces video and metadata from the **same frame stream** through the **same Savant pipeline**. The bbox in metadata.json at frame N corresponds to the person in frame N of video.mov — guaranteed by single-ingestion architecture.

## 7. What This Enables

1. **Single-Ingestion Evidence**: Frames and metadata are inherently aligned. No more Replay offset guessing.
2. **Production Media Pipeline**: The video-file-sink pattern can be extended to produce clip segments on-demand (via Replay REST API triggering selective frame extraction).
3. **Bbox/Snapshot Alignment**: When Replay is re-integrated into this topology, both Savant detection and Replay storage receive the same ZMQ frames — natural alignment without timestamp mapping.
4. **Downstream Integration**: The video.mov + metadata.json output is directly consumable by clip-worker and media-worker for snapshot/clip generation with accurate bbox overlay.

## 8. WARNING: Continuous Sink Output

**The video-file-sink in this POC writes continuous, unbounded output.** While containers are running:

- `video.mov` grows without limit (every frame encoded by NVENC and muxed).
- `metadata.json` grows without limit (one NDJSON line per frame).
- `phase3h%-metadata.ndjson` (metadata-sink) grows without limit.

**This is NOT event-triggered clip generation.** There is no chunk policy, no Replay job control, and no media retention limit. This is acceptable only for short-lived POC verification.

**The Phase 3H.2 compose stack MUST be stopped after verification:**

```bash
docker compose -f infra/docker-compose.phase3h-zmq.yml down
```

| Metric | Final size at stop |
|---|---|
| video.mov | 428 MB |
| metadata.json | 357 MB |
| metadata NDJSON | 846 MB |
| evidence_frame.jpg | 296 KB |

**Production requires:**

- Event-triggered clip extraction (not continuous recording).
- Chunk policy (max frames, max duration, or max size per clip).
- Replay Service for keyframe storage and on-demand job control.
- Media retention limits (per-camera quotas, TTL).
- Cooldown / severity policy for snapshot and clip generation.

Do NOT run this compose stack unattended for extended periods.

## 9. Media POC Freeze

Phase 3H.2 is the **last media POC phase**. Per `docs/project_rebaseline_2026_05_25.md`, media POC expansion is frozen at this point. The Phase 3H ZMQ frame+metadata single-ingestion topology is preserved as architecture reference for future production design.

**Next mainline priorities (no media):**
1. Complete missing behavior rules (loitering, crowd_gathering, fall).
2. Camera zones / rules configuration API.
3. Face pipeline (SCRFD → ArcFace → pgvector → watchlist/live-search).

Replay, clip-worker, media-worker, and video-file-sink integration will be revisited after the face pipeline is operational.
