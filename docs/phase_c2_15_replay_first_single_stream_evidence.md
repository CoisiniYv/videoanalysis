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

## Replay Timeline Alignment

C2.15 follows Savant Replay semantics directly instead of post-processing the
raw clip into an artificial event-centered file:

- `keyframes/find` is bounded with Unix-second `from` / `to` values around the
  event frame timestamp. The worker requests multiple candidates and chooses the
  first UUIDv7 keyframe at or after the event when an event-keyframe strategy is
  active.
- Replay jobs anchor on that keyframe and use `offset.seconds =
  DEFAULT_PRE_SECONDS`. Replay starts from a decodable keyframe selected by the
  service, so the event is required to be inside the clip, not exactly centered
  at 5 seconds.
- `ts_sync=true` is kept for delivery pacing. Replay does not rewrite encoded
  PTS/DTS, so raw video and annotations are aligned by the final
  `video-file-sink` `sink_metadata.json` PTS/UUID timeline.
- The video-file-sink receives final EOS from Replay with `CHUNK_SIZE=0`, so the
  media-worker only finalizes outputs after video, metadata, and duration probe
  are all available.

The first runtime proof after this correction was:

- Evidence bundle:
  `/data/video-analytics/media/evidence/e61a566e-fb72-468f-8a06-ba532b3dfd07`
- Raw clip: H.264 `raw_clip.mov`, `24000/1001`, duration `10.010013`, 240
  decoded frames, zero ffmpeg decode errors.
- Continuity diagnostic: 240 raw frames, zero raw PTS/DTS gaps, 240
  `VideoFrame` metadata rows plus EOS, zero metadata PTS order anomalies, zero
  sorted metadata gaps.
- Sidecar alignment: `production_ready=true`, `canonical_clip=true`,
  `event_pts_inside_clip=true`, `event_projected_t_s=8.800456`,
  `event_center_required=false`, `freshness_guard_mode=metadata_pts`, and no
  forbidden embedding/image/base64/crop payload in the sidecar/report surface.

## Face Matching Path

Savant writes face observations to Redis. `face-worker` persists observations to
PostgreSQL `face_observations`, searches `person_gallery_embeddings` with
pgvector, filters the configured Reese/Finch watchlist, and emits real
threshold-passing `watchlist_hit` events back to Redis. The event-worker remains
the writer for `events`, `evidence_tasks`, and `record_requests`.

The default development compose points worker `DATABASE_URL` values at the
existing host PostgreSQL on `host.docker.internal:5432`, because that database
already contains the registered Reese and Finch gallery embeddings. A separate
empty `c2-replay-first-postgres` service remains available behind the
`c2-local-postgres` profile for future isolated database tests, but it is not
the default watchlist runtime.

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
