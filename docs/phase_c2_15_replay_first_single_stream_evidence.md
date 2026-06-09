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

Replay output and Savant output are deliberately different products. Replay is
the media authority: it stores the original RTSP frames and later replays a
video-file-sink clip. Savant is the inference authority: it consumes Replay
frames and emits person, face, embedding, frame annotation, intrusion, and
watchlist metadata tied to those frames. Evidence receives a Savant event frame
identity and cuts media from Replay; it must not use Redis publish time,
database broad windows, or `event_ts_ms` as visual binding anchors.

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

`MAX_FPS_CONTROL` is wired into the Savant `zeromq_source_bin`
`ingress_frame_filter` via `custom.filters.pts_fps_gate.PtsFpsGate`. The gate
uses per-source frame PTS to admit frames into the inference graph at about
`MAX_FPS`; it does not change what Replay records. `MIN_FPS` is currently a
configuration/reporting field for the development topology, not a second dynamic
scheduler.

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

- The event-worker preserves `event_frame_uuid`, `frame_pts`,
  `event_frame_pts`, `anchor_keyframe_uuid`, `anchor_keyframe_pts`,
  `previous_keyframe_uuid`, `keyframe_uuid`, `requested_start_pts`, and
  `requested_end_pts` in each post-Savant `record_request`. For the default 5
  second pre-event and 5 second post-event policy, the requested PTS window is
  `event_frame_pts - 5s` through `event_frame_pts + 5s`.
- UUID remains the primary frame identity. The Savant alarm frame `frame_uuid`
  identifies the event frame and is used for visual binding and stale-loop
  rejection; it is not automatically a valid Replay `anchor_keyframe`.
  Clip-worker must submit `anchor_keyframe_uuid` from the event/record_request
  keyframe UUID family: `previous_keyframe_uuid` first, otherwise
  `keyframe_uuid`, unless the alarm frame is itself proven to be a keyframe. PTS
  fields define the requested media window and verification math; they must not
  replace UUID identity.
- The clip-worker looks in `security.frame_annotations` for two fresh
  same-source, same-camera frame-domain proofs: a real keyframe at or before
  `requested_start_pts`, and a frame annotation whose `frame_pts` is at or after
  `requested_end_pts`. The first records coverage/crop diagnostics for the
  earliest decodable GOP; the second proves the complete post-event window has
  reached Savant/Redis. Neither proof may replace `anchor_keyframe_uuid`.
- When the event has a UUIDv7 `frame_uuid`, post-window candidate UUID
  timestamps must be at or after the event frame UUID timestamp, so a high PTS
  frame from the previous RTSP loop cannot be bound to the current event.
  `event_ts_ms` is not a media anchor for this path; if a post-Savant
  `record_request` does not carry a frame PTS/window, the clip-worker fails
  closed instead of falling back to timestamp keyframe lookup.
- Replay `anchor_keyframe` is the event/record_request `anchor_keyframe_uuid`.
  If that UUID is missing, clip-worker may call Replay keyframes/find only after
  same-source PTS-window proof exists, then must verify the returned keyframe
  against same-source, same-camera frame annotation for that exact UUID and PTS
  window. A keyframes/find result that is merely the start-window or post-window
  proof keyframe is rejected instead of becoming the Replay anchor.
  If `anchor_keyframe_pts` is missing for a provided anchor UUID, clip-worker may
  recover it only from frame annotation rows for that exact UUID; otherwise it
  fails closed with `missing_anchor_keyframe_pts`. `offset.seconds` is the PTS
  delta from `anchor_keyframe_pts` back to `requested_start_pts`; it is not
  derived from UUID wall-clock time. The post-window frame UUID remains a
  completeness proof and the start-window frame UUID remains a coverage/crop
  helper, both recorded separately from the Replay keyframe anchor.
- The Replay `stop_condition` duration is a coverage duration from the earliest
  required GOP through `requested_end_pts`. It may be longer than the final 10
  second evidence window because Replay must start on a decodable keyframe.
  `REPLAY_DURATION_EXTRA_SLACK_S` extends this raw Replay/video-file-sink
  coverage window only; it does not change the requested evidence window. The
  C2 replay-first development compose defaults this extra slack to 15 seconds so
  sink output can absorb Replay decoder/keyframe lead-in and delivery skew.
  Media-worker then crops raw video and sink metadata to
  `requested_start_pts` / `requested_end_pts` before writing the final evidence
  bundle. The final evidence clip, not the raw Replay job output, is expected to
  place the event frame at `pre_seconds`.
- `video-file-sink` is only the ZMQ file sink named in the Replay job
  `sink.url`. It writes the frames Replay sends and finalizes on Replay EOS; it
  does not choose the time window. Extending the window therefore belongs in the
  clip-worker Replay job `stop_condition`/duration calculation, not in the sink
  container.
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

This follows the Savant Replay job model: a job is controlled by its sink,
anchor frame/keyframe, offset, configuration, and stop condition. The REST
payload uses `anchor_keyframe`, `offset`, and `stop_condition`; `ts_delta_sec`
stops by timestamp delta, while `configuration.ts_sync=true` replays frames in
their timestamp domain. See the official Replay job and REST API documentation:
`https://insight-platform.github.io/savant-rs/services/replay/3_jobs.html` and
`https://insight-platform.github.io/savant-rs/services/replay/4_api.html`.

## 2026-06-09 Evidence Box Diagnosis

Viewer "auto" mode intentionally fails closed. It only displays
`annotations.frame_cache.identity.jsonl` when
`summary.frame_cache.identity.json` has `production_ready=true` and
`timeline_domain="final_canonical_clip"`. If the summary contains
`event_pts_outside_clip`, `event_not_centered_in_clip`, `no_sidecar_rows`, or
`time_domain_crop_failed`, the viewer shows no boxes instead of showing stale or
uncertain boxes.

Observed signatures:

- Good bundle `87831139-0298-4a23-8aab-91d7265a2d78`: requested PTS
  `27577688888..37577688888`, sink metadata covered
  `27614400000..37540988888`, event PTS `32577688888`, final clip duration about
  9.97 seconds, event projected at about 4.96 seconds, and
  `production_ready=true`.
- Good bundle `8c5c401e-fbe7-4e0c-bad2-63ab984d0ccd`: requested PTS
  `663793555555..673793555555`, sink metadata covered
  `663830266666..673756844444`, event PTS `668793555555`, final clip duration
  about 9.97 seconds, event projected at about 4.96 seconds, and
  `production_ready=true`.
- Bad bundle `c0214c6f-ef50-46fb-8e50-f4d7e17f62e6`: requested PTS
  `697869266666..707869266666`, event PTS `702869266666`, but sink metadata only
  covered `685852266666..697447177777`. The raw Replay output ended before the
  event/requested window, so media-worker wrote zero sidecar rows and correctly
  marked the bundle not production ready.

The repair target is therefore: keep UUID-first anchor selection, make the raw
Replay/video-file-sink output reliably cover at least
`requested_start_pts..requested_end_pts` plus configured slack, and let
media-worker crop to the final canonical 10 second PTS window. Do not bypass the
viewer `production_ready` gate to force boxes onto unverified media.

Runtime evidence after this correction:

- Older evidence bundles showed two invalid patterns: some raw clips were longer
  than the requested window, and some sidecars accumulated too many boxes within
  a few seconds. Those symptoms are treated as time-domain bugs, not accepted
  evidence.
- The current clip-worker correction fails closed when it cannot prove a fresh
  same-camera frame annotation anchor for the requested post-event PTS target.
  That avoids generating playable but unprovable evidence while the live
  Replay-to-Savant path is stabilized.
- A runtime backpressure diagnosis found Replay-to-Savant ZMQ send timeouts and
  RTSP loop resets. The Savant ingress PTS FPS gate was added so Replay still
  records original frames, while Savant inference/Redis metadata runs at a
  bounded development rate.

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
