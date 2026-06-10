# Phase 3A — Official Replay Media MVP Summary

## Goal

Implement Savant Replay Service + Video File Sink Adapter based media recording pipeline.
Use the official Savant recommended path: Replay → Video File Sink, not ffmpeg-based clip generation.

## Final Architecture

```
test.mp4
  → video_loop.sh (official source adapter)
    → dealer+connect:tcp://replay-service:5555
      → Replay Service (router+bind, buffer 60s, RocksDB, REST :8080)
        │ GET  /api/v1/status
        │ POST /api/v1/keyframes/find  → {"keyframes":["phase3a",["uuid"]]}
        │ PUT  /api/v1/job             → replay job
        │   sink.url: pub+connect:tcp://video-file-sink:6666
        ▼
      Video File Sink (sub+bind :6666, official adapter)
        → /media/replay-sink-output/{resulting_stream_id}-{seq}/
          metadata.json (NDJSON) + video.mov

Savant inference (separate, unchanged):
  RTSP → Savant (YOLO26-pose) → SecurityEvent → Redis security.events

Backend pipeline:
  security.events → event-worker → PostgreSQL events
    ├── security.alerts (WebSocket push)
    └── security.record_requests (RECORDING_ENABLED=true)

  security.record_requests → clip-worker
    ├── Replay API: keyframes/find → anchor_keyframe
    └── Replay API: PUT /api/v1/job → Video File Sink

  /media/replay-sink-output/ → media-worker (poll)
    └── UPDATE events SET clip_path, payload.media.*
        └── FastAPI → /api/v1/events/{id} → clip_url
```

## Component Responsibilities

| Component | Role | Image |
|---|---|---|
| source-adapter | Reads test.mp4 in loop, sends Savant ZMQ frames to Replay ingress | `savant-adapters-gstreamer:0.6.0` |
| replay-service | Buffers 60s of video, indexes keyframes, REST API, creates replay jobs | `savant-replay-x86:v0.6.0` |
| video-file-sink | Receives ZMQ frames from replay job, writes metadata.json + video.mov | `savant-adapters-gstreamer:0.6.0` |
| event-worker | Consumes security.events, inserts into PostgreSQL, publishes alerts + record_requests | `phase3a-event-worker:latest` (built from source) |
| clip-worker | Consumes security.record_requests, calls Replay API (keyframes/find + job) | `phase3a-clip-worker:latest` (built from source) |
| media-worker | Polls sink output dir, parses NDJSON metadata, updates events.clip_path + payload.media | `phase3a-media-worker:latest` (built from source) |
| api | FastAPI: /media static mount, clip_url/snapshot_url in event responses | `phase3a-api:latest` (built from source) |

## Key Fixes During Implementation

### Replay Service Config
- Removed unsupported `sources` field (Replay receives Savant ZMQ frames, not RTSP)
- Added `common` section with `management_port`, `stats_period`, `default_job_sink_options`
- Added `in_stream` with `router+bind` and full ZMQ options
- Added `storage.rocksdb` with `data_expiration_ttl` and `compaction_period`
- All duration fields use `{"secs": N, "nanos": 0}` format

### Source Adapter
- Replaced hand-written `gst-launch` pipeline with official `video_loop.sh`
- Uses `media_files_src_bin` with `loop-file=true` directly from test video
- ZMQ endpoint: `dealer+connect:tcp://replay-service:5555`
- `SOURCE_ID=phase3a` for Replay source identification

### API Corrections
- keyframes/find: changed from GET query to POST with JSON body
- PUT /api/v1/job: complete payload with `configuration.ts_sync`, `anchor_keyframe`, `stop_condition.frame_count`, `anchor_wait_duration`, `attributes`
- keyframes response parsing: `keyframes[1][0]` = UUID, `keyframes[0]` = source_id

### Media Pipeline Wiring
- event-worker: `_resolve_source_id()` priority: media.source_id > event.source_id > `DEFAULT_REPLAY_SOURCE_ID` env
- media-worker: NDJSON line-by-line parsing (Video File Sink outputs JSON Lines)
- media-worker: `_extract_event_id()` with regex UUID extraction from `source_id`, `resulting_stream_id`, directory basename
- API: `_media_url()` prevents `/media//media` double prefix

### Build/Image Issues
- event-worker and api were using old Phase 2H images without Phase 3A code
- Changed to build from source with Phase 3A-specific image tags
- `RECORDING_ENABLED` set to `"true"` directly in compose (no shell env passthrough)

## Smoke Results

### Phase 3A-1: Replay POC
```
sudo bash scripts/smoke/check_phase3a_replay_poc.sh
Result: 8 passed, 0 failed
```
- Replay /api/v1/status = 200
- keyframes/find returns 200 with keyframe UUID
- PUT /api/v1/job accepted
- Video File Sink generates metadata.json and video.mov

### Phase 3A-2: Media MVP
```
sudo bash scripts/smoke/check_phase3a_replay_media_mvp.sh
Result: 11 passed, 0 failed
```
- event injected into security.events
- PostgreSQL event inserted
- security.record_requests has message
- Replay /api/v1/status = 200
- media.clip_status present
- media.recording_strategy = savant_replay
- clip_path non-null
- API returns clip_url
- curl clip_url returns 200
- Idempotency: source_event_id count = 1

## Known Boundaries (MVP)

1. **Media fields**: `snapshot_status=not_implemented`, `clip_status=ready`, `recording_strategy=savant_replay`
2. **No snapshot/image generation**: Phase 3A generates video clips only; snapshots deferred to Image File Sink or clip frame extraction
3. **No real-time clip streaming**: Clips are generated on-demand via replay jobs, not pre-recorded
4. **Replay buffer limited to 60s**: Events older than 60s cannot be replayed
5. **Single source_id**: Phase 3A uses `phase3a` as the only source; multi-camera production needs dynamic source_id mapping
6. **NDJSON metadata**: Video File Sink outputs JSON Lines format; media-worker must parse line-by-line
7. **Docker build required**: event-worker, api, clip-worker, media-worker must be built from Phase 3A source
8. **`pip install redis` on Savant container**: Runtime pip install at container start (Phase 2 tech debt, documented in phase2_summary.md)

## Technical Debt

| Item | Priority | Phase |
|---|---|---|
| Savant container runtime `pip install redis` | High | 2I |
| Replay config.json must be manually kept in sync with compose env vars | Medium | 3A |
| source_id mapping from Savant events to Replay sources is hardcoded | Medium | 3B+ |
| media-worker polls filesystem; no event-driven notification | Low | 3B+ |

## Phase 3B Suggestions

1. **Image File Sink for snapshots**: Add official Savant Image File Sink adapter for keyframe snapshot generation
2. **Dynamic source_id mapping**: Map Savant camera_id/source_id to Replay source_id automatically
3. **Multi-camera support**: Multiple Replay instances or source_id multiplexing
4. **Replay buffer persistence**: Survive Replay service restart without losing keyframe index
5. **media-worker event-driven**: Replace filesystem polling with Redis notification or inotify
6. **clip expiration/cleanup**: Automatic cleanup of old clip files from /media/replay-sink-output
