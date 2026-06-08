# C2.15 Replay-First Single-Stream Evidence Baseline

Status: configuration baseline and contract before runtime apply.

## Goal

C2.15 moves the C2 development path back to the official Replay order:

```text
RTSP source
  -> source-adapter
  -> replay-service
  -> savant-security
  -> Redis security.face_observations / security.person_observations / security.events
  -> face-worker
  -> PostgreSQL face_observations / match_results / gallery lookup
  -> Redis watchlist_hit events
  -> event-worker
  -> PostgreSQL events / evidence_tasks / record_requests
  -> clip-worker Replay job
  -> video-file-sink
  -> media-worker
  -> /data/video-analytics/media/evidence/<event_id>/
  -> evidence-viewer on port 8090
```

The source adapter records the original RTSP stream into Replay before Savant
inference. Savant inference remains the existing YOLO pose + YOLOv8/SCRFD face
+ AdaFace chain. Replay is bounded with a 30 second RocksDB TTL for the single
development stream, which is enough for a 5 second pre-event plus 5 second
post-event clip and short event processing latency.

## Runtime Files

- Compose: `infra/docker-compose.c2-replay-first-dev.yml`
- Replay config: `modules/savant_replay/config.c2_replay_first_dev.json`
- Savant module: `modules/savant_security/module.yml`
- Camera config: `modules/savant_security/config/cameras.c1e_replay.yml`
- Evidence viewer: `services/evidence-viewer` on host port `8090`

## Configurable Parameters

- `C2_REPLAY_FIRST_RTSP_URI`: RTSP source, default
  `rtsp://10.37.57.112:8554/live/1080movie`.
- `MAX_FPS_CONTROL`: Savant inference throttle enabled by default.
- `MAX_FPS`: default `8/1`.
- `MIN_FPS`: default `2/1`.
- `DEFAULT_PRE_SECONDS` / `RECORDING_PRE_SECONDS`: default `5`.
- `DEFAULT_POST_SECONDS` / `RECORDING_POST_SECONDS`: default `5`.
- `WATCHLIST_THRESHOLD`: default `0.65`.
- `WATCHLIST_TARGET_EXTERNAL_PERSON_IDS`: default
  `demo:f4_3:reese,demo:f4_3:finch`.

## Evidence Contract

The production evidence bundle must contain:

- `raw_clip.mov` copied from Replay/video-file-sink output, not synthesized from
  sparse inference metadata.
- `sink_metadata.json` copied from video-file-sink metadata.
- `annotations.frame_cache.identity.jsonl` for bbox, track, and identity
  sidecar data.
- `summary.json` and `metadata.json` with clip and annotation status.

The bundle must not contain or require an `annotated_clip` video. No generated
media path is allowed: annotated video clips are not generated. The 8090 viewer
overlays JSONL annotations over the raw clip at display time.

## Face Matching Path

Savant writes face observations to Redis. `face-worker` persists observations to
PostgreSQL `face_observations`, searches `person_gallery_embeddings` with
pgvector, filters the configured Reese/Finch watchlist, and emits real
threshold-passing `watchlist_hit` events back to Redis. The event-worker remains
the writer for `events`, `evidence_tasks`, and `record_requests`.

Sidecars and reports must not include embedding vectors, image bytes, base64
images, or crop bytes.

## Current Scope

This is a one-camera development baseline for the dual-4090 dev machine. It is
not the 60-stream / two-T4 production sizing proof. Multi-stream tests come
after one replay-first stream can produce playable raw evidence with aligned
annotations in the 8090 viewer.

## Known Caveats

- Existing running C2 containers may still be the older post-Savant ring path
  until this compose is applied.
- Runtime verification and generated evidence are separate follow-up steps.
- Replay TTL, worker latency, and clip pre/post seconds must be reviewed again
  before multi-camera tests.
