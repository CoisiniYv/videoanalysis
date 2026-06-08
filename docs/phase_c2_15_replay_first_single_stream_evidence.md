# C2.15 Replay-First Single-Stream Evidence Baseline

Status: replay-first runtime alignment baseline with PTS-window evidence
contracts.

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

C2.15 uses the Replay service as the media source and preserves the Savant event
frame timeline instead of using a broad database window to visually bind
annotations after the fact:

- The event-worker preserves `frame_pts`, `event_frame_pts`,
  `requested_start_pts`, and `requested_end_pts` in each post-Savant
  `record_request`. For the default 5 second pre-event and 5 second post-event
  policy, the requested PTS window is `event_frame_pts - 5s` through
  `event_frame_pts + 5s`.
- The clip-worker first looks in `security.frame_annotations` for a fresh
  same-source, same-camera frame annotation whose `frame_pts` is at or after
  `requested_end_pts`. This selects a Replay anchor that covers the complete
  post-event window.
- The selected frame annotation supplies the Replay `anchor_keyframe`
  `frame_uuid`. `offset.seconds` becomes `pre_seconds + (anchor_pts -
  event_frame_pts)`, so Replay starts at the requested pre-event boundary even
  when the decodable anchor is later than the alarm frame.
- The Replay `stop_condition` duration is the requested PTS window duration,
  normally `pre_seconds + post_seconds`. It is not `offset + post_seconds`.
  This prevents 14 second or longer raw clips when the anchor is several seconds
  after the event.
- `ts_sync=true` remains enabled. Replay/video-file-sink deliver the raw media
  and native `sink_metadata.json`; media-worker then applies the same
  `requested_start_pts` / `requested_end_pts` window to both video and metadata
  before writing `raw_clip.mov` and
  `annotations.frame_cache.identity.jsonl`.
- Event alignment is evaluated by PTS/UUID, not by a fixed frame count. A
  24 FPS RTSP clip may have a slightly variable decoded frame count; the hard
  invariant is that the event frame appears at `pre_seconds` within the final
  evidence clip and that raw video, sink metadata, sidecar JSONL, and the 8090
  viewer report share the same per-camera PTS/UUID timeline.
- If no fresh same-camera frame annotation anchor can cover the requested PTS
  window within the configured tolerance, the clip-worker fails closed. It does
  not use stale Redis rows, cross-loop frame UUIDs, broad DB window fallback, or
  legacy visual binding.
- The video-file-sink receives final EOS from Replay with `CHUNK_SIZE=0`, so
  media-worker only finalizes outputs after video, metadata, and duration probe
  are all available.

Runtime evidence after this correction:

- Evidence bundle
  `/data/video-analytics/media/evidence/3913caea-ffdc-4973-8eea-ec58e7b72e9e`
  was finalized as `production_ready=true` with `time_domain_crop_applied=true`,
  239 decoded video frames, 146 sidecar frames, requested PTS window
  `13438566666..23438566666`, event PTS `18438566666`, requested duration
  `10.0`, and actual metadata duration about `9.926588889`.
- Evidence bundle
  `/data/video-analytics/media/evidence/5f37ef8e-a00b-47bb-98b6-36a4ca892010`
  was finalized as `production_ready=true` with `time_domain_crop_applied=true`,
  239 decoded video frames, 158 sidecar frames, requested PTS window
  `3972244444..13972244444`, event PTS `8972244444`, requested duration `10.0`,
  and actual metadata duration about `9.926577778`.
- A later event
  `1a382d50-c4be-4a78-96b1-1083f3248959` had no fresh/near frame annotation
  anchor for its requested post-event PTS target, so clip-worker did not create
  a Replay job. This is the intended fail-closed behavior until live anchor
  availability is improved.

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

- This step proves the one-camera replay-first evidence alignment path, not the
  final two-T4 / 60-stream sizing target.
- The current strict anchor policy may fail to create a Replay job when Redis
  does not yet contain a fresh same-camera frame annotation near
  `requested_end_pts`. That is safer than generating a playable but misbound
  clip.
- Existing generated bundles remain runtime artifacts under
  `/data/video-analytics/media/evidence`; `/data/video-analytics` is not cleaned
  by this work.
- Replay TTL, worker latency, and clip pre/post seconds must be tuned again
  before multi-camera tests.
