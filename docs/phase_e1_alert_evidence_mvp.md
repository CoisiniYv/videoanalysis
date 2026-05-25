# Phase E1 — Alert Evidence MVP (Single-Ingestion Dev Path)

Date: 2026-05-25
Status: **DEV PATH** — Not production. Verified as POC.

## 1. Purpose

Phase E1 closes the demo loop: when a real intrusion event is generated, the system produces snapshot, annotated snapshot (with bbox overlay), and short video clip — all from Phase 3H.2 single-ingestion aligned video+metadata — then exposes them via the FastAPI events API.

**This is a dev-path POC, not the final Replay production path.**

## 2. Topology

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
            -> evidence-worker (one-shot) -> snapshot + annotated + clip
              -> PostgreSQL (snapshot_path, clip_path, payload.media.*)
                -> API (serves /media + events endpoints)
```

All evidence is generated from the Phase 3H.2 aligned video+metadata output. No Replay Service, no clip-worker, no dual file loops.

## 3. Why Not Phase 3B Replay Bypass

| Aspect | Phase 3B (Replay bypass) | Phase E1 (This) |
|---|---|---|
| Frame source for bbox | Savant reads RTSP independently | Single ZMQ source adapter |
| Frame source for video | Separate Replay loop | Same ZMQ sink from Savant |
| Number of frame streams | 2 (independent loops) | 1 (single ingestion) |
| Bbox/snapshot alignment | Time-domain mismatch | Same-frame: metadata = video |
| Event-to-frame matching | Not possible | track_id approximation |
| Replay Service | Required | Not used |
| clip-worker | Required for Replay API | Not used |

## 4. Alignment Boundaries

| Boundary | Status | Method |
|----------|--------|--------|
| video frame ↔ metadata frame | **Aligned** | Same Savant pipeline, same ZMQ sink, single ingestion |
| event ↔ frame | **Approximate** | Matched by track_id in metadata.json — not by frame_num/frame_uuid/timestamp |
| bbox ↔ snapshot pixels | **Aligned** | Bboxes come from the same metadata frame used for snapshot extraction |

### Event-to-Frame Matching (track_id Approximation)

The evidence-worker scans `metadata.json` for frames containing the event's `track_id` as an `object_id`. It selects the middle frame from all matching frames. This produces a reasonable event frame, but:

- The frame is approximate — the intrusion rule may have fired at a slightly different frame than the one selected.
- No `frame_num` / `frame_uuid` / `pts` mapping exists in the event payload (current known limitation — CLAUDE.md Section 9.6).
- This is acceptable for dev/demo purposes but **not for production bbox/snapshot alignment claims**.

## 5. evidence-worker Design

### 5.1 Service: `services/evidence-worker/`

One-shot service: processes pending intrusion events, generates evidence, exits. Re-run to process new events.

```
services/evidence-worker/
  Dockerfile              # python:3.12-slim-bookworm + ffmpeg + Pillow + fonts
  requirements.txt        # psycopg2-binary, Pillow
  main.py                 # Entry point
  app/config.py           # Environment config (DATABASE_URL, VIDEO_DIR, output dirs)
  app/repository.py       # PostgreSQL: find events needing evidence, update with paths
  app/evidence.py         # find_metadata_file, find_event_frame, extract_snapshot, extract_clip
  app/bbox_draw.py        # Draw person bboxes (center→top-left conversion) + label block
```

### 5.2 Processing Flow

1. Query PostgreSQL for intrusion events where `snapshot_path IS NULL`
2. For each event (limited by `EVIDENCE_MAX_EVENTS`, default 5):
   - Get `track_id`, `source_id` from event row
   - Locate `metadata.json` from video-file-sink output directory
   - Scan for frames containing the track_id as an object_id
   - Select the middle matching frame
   - Extract snapshot from `video.mov` via ffmpeg
   - Draw ALL person bboxes from that frame's metadata objects
   - Extract clip segment (pre-3s + post-3s) from `video.mov` via ffmpeg
   - Write paths and status to PostgreSQL
3. Print summary: event_id, track_id, frame_num, all paths
4. Exit

### 5.3 Output Directory Structure (Phase E1.1a)

Per-event evidence directory under `EVIDENCE_EVENTS_DIR`:

```
/media/evidence/events/{event_id}/
  snapshot.jpg
  annotated_snapshot.jpg
  clip_raw.mp4
  evidence_metadata.json
```

See `docs/media_output_directory_policy.md` for the mandatory directory policy.

### 5.4 Database Updates

| Field | Location | Value |
|---|---|---|
| `snapshot_path` | `events.snapshot_path` (column) | `/data/video-analytics/media/evidence/events/{event_id}/snapshot.jpg` |
| `clip_path` | `events.clip_path` (column) | `/data/video-analytics/media/evidence/events/{event_id}/clip_raw.mp4` |
| `annotated_snapshot_path` | `payload.media.annotated_snapshot_path` (JSONB) | `.../evidence/events/{event_id}/annotated_snapshot.jpg` |
| `snapshot_status` | `payload.media.snapshot_status` | `"ready"` |
| `clip_status` | `payload.media.clip_status` | `"ready"` |
| `annotated_snapshot_status` | `payload.media.annotated_snapshot_status` | `"ready"` |
| `evidence_frame_num` | `payload.media.evidence_frame_num` | matched frame number |
| `evidence_track_id` | `payload.media.evidence_track_id` | matched track_id |
| `output_root` | `payload.media.output_root` | `/data/video-analytics/media/evidence/events/{event_id}` |

### 5.5 Bbox Coordinate Conversion

The video-file-sink metadata uses center-based coordinates (`xc`, `yc`, `width`, `height`, `angle`). The evidence-worker converts to top-left for drawing:

```
x = xc - width/2
y = yc - height/2
```

All person objects in the selected frame are drawn (not just the triggering track_id).

## 6. API Media URLs

The existing FastAPI API already supports all three URL fields. No API changes were needed:

- `snapshot_url` — from `events.snapshot_path` column
- `annotated_snapshot_url` — from `events.annotated_snapshot_path` column, with fallback to `payload.media.annotated_snapshot_path`
- `clip_url` — from `events.clip_path` column

The API mounts StaticFiles at `/media` pointing to `MEDIA_ROOT=/media`. Evidence output is mounted at `/media/evidence` inside the API container.

## 7. Pipeline Lifecycle

**CRITICAL: Do NOT run the continuous video-file-sink for extended periods.**

```
# 1. Start the full stack
docker compose -f infra/docker-compose.phase3h-zmq.yml up -d

# 2. Wait for intrusion events to generate (~30-60 seconds)
sleep 30

# 3. Stop source/savant/sinks ONLY (keep postgres, redis, api)
docker compose -f infra/docker-compose.phase3h-zmq.yml stop \
  source-adapter savant-zmq metadata-sink video-file-sink

# 4. Run evidence-worker (one-shot, exits when done)
docker compose -f infra/docker-compose.phase3h-zmq.yml up evidence-worker

# 5. Verify
bash scripts/smoke/check_phase_e1_evidence.sh

# 6. Tear down when done
docker compose -f infra/docker-compose.phase3h-zmq.yml down
```

## 8. Current Limitations

1. **Event-to-frame matching is approximate**: Uses track_id scanning, not frame_num/timestamp mapping. The evidence frame may not be the exact frame where the intrusion rule fired.
2. **Continuous sink output**: video.mov and metadata.json grow without limit while the pipeline runs. The pipeline MUST be stopped before running evidence-worker.
3. **Post-hoc clip extraction**: Clip is cut from the already-written video.mov, not generated in real-time by event trigger.
4. **Keyframe-aligned clip boundaries**: Clip start/end times are aligned to the nearest keyframe (every 25 frames ≈ 0.83s), so the ~6s clip may be slightly longer or shorter.
5. **Single source_id**: Currently only processes events with source_id matching the video-file-sink output directory name.
6. **No cooldown/severity policy**: All intrusion events are processed (up to EVIDENCE_MAX_EVENTS).
7. **No frame_uuid / keyframe_uuid in events**: These fields remain NULL in the event payload (known limitation per CLAUDE.md Section 9.6).

## 9. Production Migration Path

Phase E1 is explicitly a dev-path POC. Production will migrate to:

```
RTSP camera
  -> Savant Source Adapter
    -> Replay Service (RocksDB cache + REST API)
      -> Savant Module (GPU inference)
        -> Redis events
          -> clip-worker -> Replay REST API -> video-file-sink (event-triggered)
          -> media-worker -> snapshot + annotated snapshot
```

Key differences from Phase E1:

| Aspect | Phase E1 (Dev) | Production |
|---|---|---|
| Clip generation | Post-hoc from video.mov | Real-time event-triggered via Replay |
| Frame selection | track_id scanning | Timestamp-domain mapping + keyframe lookup |
| Event-to-frame | Approximate | Precise (frame_uuid, keyframe_uuid) |
| Video source | Continuous video.mov | Replay job with bounded frame range |
| Sink lifetime | Must stop manually | Event-triggered, auto-completes |
| Bbox source | metadata.json objects | Real-time Savant detection payload |
| Cooldown/severity | None | Configurable per event type |

## 10. Smoke Test

```bash
bash scripts/smoke/check_phase_e1_evidence.sh
```

13 checks covering: compose config, database state, file existence, API URLs, HTTP responses, and bbox pixel verification.

## 11. Related Documents

| Document | Content |
|---|---|
| `docs/phase3h_2_savant_output_video_sink_poc.md` | Phase 3H.2 video+metadata alignment proof |
| `docs/production_ingestion_topology_policy.md` | Production topology policy |
| `docs/project_rebaseline_2026_05_25.md` | Project rebaseline and revised mainline |
| `docs/module_and_compose_consolidation_plan.md` | Module/compose consolidation plan |

---

*Written 2026-05-25. Dev path — not production.*
