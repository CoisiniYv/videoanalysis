# Midterm Worker And Savant Batching Findings

## Status

Date: 2026-06-12

Update 2026-06-15: this note remains useful for the Savant batching and
per-source state model, but its runtime chain and `MAX_FPS_CONTROL=true`
snapshot are historical. The current midterm topology inserts
`analysis-forwarder` between Replay and Savant, keeps
`INGRESS_FPS_GATE_ENABLED=true`, and keeps `MAX_FPS_CONTROL=false` after the
Phase 0A experiment. See `docs/current_mainline_status.md` and
`specs/16_dual_path_30x2_t4_production_optimization.md` for the current
topology.

This note freezes the read-only runtime diagnosis for these questions:

- whether `clip-worker` or `media-worker` can stall the current program flow;
- whether the current Savant 8 FPS limit is frame skipping, and how it relates
  to batching and TensorRT model execution.
- how the official Savant model handles multiple streams, batching, Replay,
  external storage boundaries, and per-stream state isolation;
- whether this project should use one Redis/PostgreSQL table per camera, and
  whether `clip-worker` and `media-worker` share a cache in a way that can
  collide across cameras.
- why a newly saved enabled camera can still have no output when its
  source-adapter is not converged into the running topology.

No code change is implied by this note. It records current behavior and repair
priorities for later implementation.

Companion note for the current `lab` camera output and `source_id` naming
diagnosis:

- `docs/midterm_camera_runtime_source_identity_findings_2026-06-12.md`

## Runtime Chain Boundary

Current chain:

```text
RTSP -> Replay storage -> analysis-forwarder -> Savant inference -> Redis events/annotations
  -> event-worker -> clip-worker -> Replay job -> video-file-sink
  -> media-worker evidence sidecar -> 8090 operator portal
```

`clip-worker` and `media-worker` sit after Savant inference. They should not
block RTSP ingest, Replay storage, or Savant's main inference path directly.

They can block or degrade the evidence path. User-visible symptoms include
events stuck in `replay_job_created`, failed clips, missing evidence media,
delayed sidecars, and 8090 evidence detail pages showing incomplete media.

## Official Savant Pattern

Official Savant's production pattern is not "one pipeline process per camera"
and not "one database table per camera".

The official streaming model decouples sources and sinks from the module with
adapters. A Savant module receives many video streams through one ZeroMQ source
socket, multiplexes and de-multiplexes them internally, and sends multiplexed
output to sink adapters. Official docs explicitly call out that code holding
per-stream state must key that state by stream `source_id`.

Official source adapters require a unique `SOURCE_ID` for each stream. If
identifiers collide, processing can become unpredictable. Sink adapters can then
filter or route by `SOURCE_ID` or source-id prefix, and file sink adapters allow
`%source_id` in output paths. This is the official isolation primitive: one
shared module and shared transport, with every record/frame/event carrying a
unique stream identity.

Official batching also follows this shared-stream model. Savant documents two
batching layers:

- stream multiplexing batch: frames from one or more streams are batched by
  pipeline parameters such as `batch_size`, `max_same_source_frames`, and
  `batched_push_timeout`;
- model batch: primary models batch frames, while secondary/object models batch
  detected objects or ROIs.

Official Replay is a separate service, not an in-module per-camera table. It
keeps a recent video window in RocksDB and exposes a REST API for replay jobs.
Official message buffering similarly uses a dedicated Buffer NG service backed
by RocksDB when reliable buffering is needed. The embedded Savant KVS is for
small pipeline data exchange and is explicitly not a Redis replacement.

For external systems, official guidance separates real-time and capacity
circuits: use backpressure-capable sockets/queues for capacity paths, use
low-latency systems and hard timeouts for real-time paths, and add queues to
decouple slow non-real-time systems. In this project, Redis Streams,
PostgreSQL, Replay, `clip-worker`, and `media-worker` are application-level
pieces around Savant, not replacements for Savant's stream identity model.

## Project Data-Plane Isolation

This project currently follows the same logical direction: shared storage with
per-record stream identity, not one physical table per camera.

PostgreSQL tables are shared business tables:

- `events` contains `camera_id` and `source_id` columns and indexes.
- `face_observations` contains `camera_id` and `source_id` columns and indexes.
- `person_bbox_observations` contains `camera_id` and `source_id` columns and
  source/camera timestamp indexes.
- `evidence_tasks` contains `camera_id` and `source_id` columns.

Splitting these into one table per camera is not the right first fix. It would
increase schema churn, cross-camera query cost, and worker complexity. If scale
later requires physical isolation, use normal database partitioning or retention
policies behind the same logical schema, still keyed by `source_id`/`camera_id`.

Redis is also shared by stream purpose, not by camera table:

- Savant emits events and observations into shared streams such as
  `security.events`, `security.face_observations`,
  `security.person_observations`, and `security.frame_annotations`.
- `event-worker` emits recording requests to `security.record_requests`.
- Messages carry `source_id`, `camera_id`, and where relevant
  `runtime_epoch_id`.

`clip-worker` and `media-worker` are both used in the evidence path, but they
do not represent the same cache:

```text
event-worker -> security.record_requests -> clip-worker
  -> Replay REST job -> video-file-sink output directory
  -> media-worker -> evidence sidecars + events/evidence_tasks updates
```

The shared contract is the event identity and stream identity:
`event_id`, `source_event_id`, `source_id`, `camera_id`, frame UUID/PTS, and
`runtime_epoch_id`. A correct multi-camera path requires those fields to stay
unique and consistently propagated. With unique `source_id` values, the design
should not create a camera-to-camera cache collision merely because Redis or
PostgreSQL are shared.

The real risks are narrower:

- duplicate or reused `source_id` values;
- stale active sink-output directories being rescanned after restart;
- Redis consumer-group messages left pending without recovery;
- frame annotation lookups that omit `source_id`, `camera_id`, or
  `runtime_epoch_id`;
- record requests whose Replay job labels cannot be matched back to the DB
  event.

The current code already includes several protections in this direction:
camera config loading rejects duplicate `source_id`; runtime rule state is
built as a `source_id -> SourceRuntime` map; frame annotation selection filters
by `source_id` and `camera_id`; and newer post-Savant paths filter by
`runtime_epoch_id`. The repair target should therefore be stronger lifecycle
and pending-message handling, not per-camera Redis/PostgreSQL tables.

## Clip-Worker Findings

`clip-worker` is a single-threaded Redis Stream consumer in the current code. It
uses `XREADGROUP` with the `">"` id and processes requests in-process, one
message loop at a time:

```text
services/clip-worker/app/worker.py
run_worker() -> xreadgroup(..., {stream: ">"}, count=10)
```

Current risk points:

- There is no startup claim/replay of pending consumer-group messages. If the
  worker crashes or restarts after receiving a message but before `XACK`, that
  message can remain in the pending entries list until manually recovered.
- `CLIP_WORKER_MAX_CONCURRENT_JOBS=1` is a gate, not a real job queue. Non-priority
  requests can be marked `skipped_by_poc_limit` instead of waiting.
- Post-Savant evidence requests wait for frame-domain proof before Replay job
  creation. Current defaults allow up to `POST_SAVANT_FRAME_PROOF_ATTEMPTS=30`
  with `POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S=1.0`.
- Replay keyframe lookup has its own retry window:
  `KEYFRAME_LOOKUP_RETRIES=8`, `KEYFRAME_LOOKUP_RETRY_SLEEP_S=1.0`.
- Replay API calls use a 30 second HTTP timeout in
  `services/clip-worker/app/replay_client.py`.

Therefore a single problematic request should usually fail or advance after a
bounded wait, but while it is waiting it blocks later requests in the same
worker process. A crash before ack is the important unbounded recovery gap.

Observed runtime evidence on 2026-06-12:

```text
security.record_requests / clip-workers-midterm:
pending = 0
lag = 0
XLEN = 48..49 during inspection

recent DB media status, last 30 minutes:
not_implemented      37
failed               14
ready                 9
replay_job_created    1
skipped_by_poc_limit  1

failure reasons observed:
missing_post_savant_frame_pts_window source_id=primary_rtsp
CLIP_WORKER_MAX_CONCURRENT_JOBS reached
```

This indicates no Redis consumer backlog at the time of inspection, but the
evidence path was still losing or delaying jobs because the frame proof window
and the concurrency gate rejected some requests.

## Media-Worker Findings

`media-worker` is also single-threaded. It polls the active sink-output tree and
then runs synchronous evidence finalization:

```text
services/media-worker/app/worker.py
run_worker() -> _process_sink_output() -> snapshot/annotation/finalizer steps
```

Current risk points:

- It repeatedly scans for `metadata.json` under the active sink output.
- It keeps an in-memory `processed_dirs` set; a restart can rescan historical
  active-epoch directories.
- It performs synchronous video probing, copying/cropping, metadata selection,
  sidecar creation, and DB updates in one worker loop.
- The container inspected on 2026-06-12 did not have `ffprobe`; media-worker
  fell back to `imageio_ffmpeg`, increasing per-bundle overhead.

Observed runtime evidence on 2026-06-12:

```text
active runtime epoch:
midterm-20260612T042513Z-334eb536

sink metadata files under /media/replay-sink-output/midterm:
1566

evidence directories under /media/evidence:
2864

media-worker logs:
media_event_updated ... raw_clip.mov
ffprobe not found for raw clip duration
ffprobe unavailable; duration probed via imageio_ffmpeg
```

This indicates media-worker was still making progress, but directory scanning
and video probing are likely evidence-latency bottlenecks. It does not explain
Savant inference stalls by itself.

## Savant 8 FPS Limit

The current 8 FPS limit is project code, not just a Savant `nvinfer.interval`.

The active module installs a source-level ingress frame filter:

```text
modules/savant_security/module.yml
pipeline.source.ingress_frame_filter:
  module: custom.filters.pts_fps_gate
  class_name: PtsFpsGate
```

`PtsFpsGate` allows at most `MAX_FPS` frames per source based on PTS. When the
frame is not a keyframe and the PTS delta from the last accepted frame is below
the configured minimum interval, it returns `False`. In Savant frame-filter
terms, that frame is skipped before entering the inference graph.

Historical defaults observed during the 2026-06-12 diagnosis:

```text
MAX_FPS_CONTROL=true
MAX_FPS=8/1
MIN_FPS=2/1
SOURCE_INPUT_FPS_ESTIMATE=24
```

Current 2026-06-15 defaults keep the project FPS gate but separate it from
nvstreammux control:

```text
MAX_FPS_CONTROL=false
INGRESS_FPS_GATE_ENABLED=true
MAX_FPS=8/1
MIN_FPS=2/1
```

Observed Savant log counters:

```text
component=savant_security_pts_fps_gate_tick source_id=primary_rtsp
seen=8400 accepted=2822 enabled=True max_fps=8
```

The observed ratio is approximately 24 FPS input reduced to 8 FPS admitted to
Savant inference.

Model `interval` is a second, separate throttle after the ingress gate:

```text
POSE_INFER_INTERVAL=1
FACE_INFER_INTERVAL=2
FACE_EMBEDDING_INFER_INTERVAL=2
```

Approximate model opportunity:

```text
pose opportunity ~= 8 / (1 + 1) = 4 FPS
face detector opportunity ~= 8 / (2 + 1) = 2.67 FPS
AdaFace opportunity depends on face detections and ~= face crops every third
eligible detector frame
```

## Savant Throttling Decision

Official Savant has three different mechanisms that are easy to mix together:

| Mechanism | Layer | What it limits | Recommended role here |
| --- | --- | --- | --- |
| `max_fps_control`, `max_fps`, `min_fps` | Savant / DeepStream muxer | Per-source frame scheduling through the pipeline muxer | Keep enabled as an official guardrail. |
| `ingress_frame_filter` or top-level ROI removal | Source / frame admission or model ROI | Whether a frame, stream, or ROI enters downstream processing | Use as the project-visible throttling contract. |
| `nvinfer` `interval` | Individual model | Consecutive batches skipped by that model | Use only as secondary per-model load shedding. |

The current `PtsFpsGate` is a project implementation of Savant's official
ingress frame-filter pattern. It is more explicit than relying only on
`max_fps_control` because it records `seen` and `accepted` counts per
`source_id`, uses frame PTS instead of wall-clock time, and drops frames before
they enter the inference graph. This is a good fit for the current topology:

```text
RTSP -> Replay stores full stream -> Savant accepts sparse PTS-selected frames
```

That separation matters. Replay can still produce full-window evidence later,
while Savant inference remains bounded to the configured 5-8 FPS target.

Do not replace this with model `interval` as the only limiter. Official Savant
documents `model.interval` as consecutive-batch inference skipping and
recommends top-level ROI reset instead when a frame must be skipped. In this
project, relying only on `interval` would still admit frames into the pipeline,
would make pose/face/embedding cadence diverge in a less obvious way, and could
confuse behavior-rule timing, tracker behavior, and evidence diagnostics.

Recommended steady-state policy:

```text
1. Replay remains before Savant and stores the original stream.
2. `PtsFpsGate` remains the auditable business limiter for frames admitted to
   Savant, configured by `MAX_FPS_CONTROL` and `MAX_FPS`.
3. Official `max_fps_control` remains enabled as the muxer-level safety rail.
4. Model intervals remain model-specific cost controls:
   - pose interval should stay conservative because behavior rules and tracker
     quality depend on stable person observations;
   - face detector and AdaFace intervals can be more aggressive because they
     are not the only source of motion/behavior state.
5. Metrics must distinguish source FPS, Savant-admitted FPS, model opportunity
   FPS, object-output FPS, and Redis metadata/export FPS.
```

Current follow-up gap: `MIN_FPS` is passed into `PtsFpsGate`, but the gate does
not currently implement a real minimum-FPS guarantee. It should either gain a
well-defined meaning, such as a lower-bound admission target during sparse or
irregular PTS streams, or be removed from the custom gate configuration to avoid
suggesting a guarantee that does not exist.

## Savant Batching Model

Savant batching has two relevant layers:

- pipeline / muxer batch, configured by module-level values such as
  `parameters.batch_size`, `max_same_source_frames`, and
  `batched_push_timeout`;
- model batch, configured by each nvinfer model's `model.batch_size`.

Current project configuration:

```text
module parameters (env-backed defaults):
BATCH_SIZE=1
MAX_PARALLEL_STREAMS=4
max_same_source_frames=1
BATCHED_PUSH_TIMEOUT=40000

model batches (env-backed defaults):
POSE_BATCH_SIZE=1
FACE_DETECTOR_BATCH_SIZE=1
FACE_EMBEDDING_BATCH_SIZE=16
```

Implication:

- the current default pipeline is not using multi-frame muxer batching;
- these values are deployment env defaults, not compose literals;
- YOLO26 pose and YOLOv8 face detector are currently single-batch;
- AdaFace is already using model-level object batching with batch size 16.

Do not assume that raising `BATCH_SIZE` alone improves throughput. For this
pipeline, the detector model batch settings, converter behavior, object counts,
and stream count must all be validated together.

## TensorRT Runtime Status

The active Savant config names ONNX model files, but the running DeepStream
pipeline loads TensorRT engines generated from those ONNX files.

Observed Savant startup logs on 2026-06-12:

```text
yolo26_pose:
model-engine-file=/models/yolo26_pose/yolo26_pose.onnx_b1_gpu0_fp16.engine
deserialized trt engine

yolov8_face:
model-engine-file=/models/yolov8_face.onnx_b1_gpu0_fp16.engine
deserialized trt engine

adaface:
model-engine-file=/models/adaface/adaface_ir50_webface4m.onnx_b16_gpu0_fp16.engine
deserialized trt engine
```

Model files present under `/models`:

```text
/models/yolo26_pose/yolo26_pose.onnx
/models/yolo26_pose/yolo26_pose.onnx_b1_gpu0_fp16.engine

/models/yolov8_face/yolov8n-face.onnx
/models/yolov8_face/yolov8n-face.onnx_b1_gpu0_fp16.engine
/models/yolov8_face/yolov8n-face.onnx_b4_gpu0_fp16.engine

/models/adaface/adaface_ir50_webface4m.onnx
/models/adaface/adaface_ir50_webface4m.onnx_b16_gpu0_fp16.engine
/models/adaface/adaface_ir50_webface4m.onnx_b8_gpu0_fp16.engine
```

Current compatibility assessment:

| Model | Current batch | Current engine | Multi-batch status |
| --- | ---: | --- | --- |
| YOLO26 pose | 1 | `yolo26_pose.onnx_b1_gpu0_fp16.engine` | not currently enabled; needs explicit bN engine/config/converter validation |
| YOLOv8 face landmark | 1 | `yolov8n-face.onnx_b1_gpu0_fp16.engine` | not currently enabled; a b4 engine exists but is not active |
| AdaFace | 16 | `adaface_ir50_webface4m.onnx_b16_gpu0_fp16.engine` | already enabled as model-level object batch |

## Recommended Repair Order

1. Add `clip-worker` pending-message recovery using `XPENDING`/`XAUTOCLAIM` or
   an equivalent startup recovery path.
2. Replace `CLIP_WORKER_MAX_CONCURRENT_JOBS` skip behavior with queueing,
   delayed retry, or a DB-backed job state.
3. Move post-Savant frame proof waiting out of the single consumer loop, or
   store a retryable state so later requests can still be consumed.
4. Reduce media-worker full-tree scan cost. Persist processed sink directories
   across restart or constrain scanning to new active-epoch outputs.
5. Install `ffprobe` in the media-worker image or make the fallback cost and
   timeout explicit.
6. Keep `MAX_FPS=8/1` documented as intentional PTS-domain frame dropping
   before inference. Do not treat Redis frame annotation rate as source FPS; it
   is downstream of ingress filtering and model intervals. Clarify or remove
   the custom gate's `MIN_FPS` setting because it is not a real guarantee today.
7. Treat detector multi-batch as a separate throughput task. Start with
   controlled benchmarks and engine generation for pose/face bN; do not change
   `BATCH_SIZE`, detector `model.batch_size`, and intervals in one unmeasured
   runtime change.
8. Keep Redis/PostgreSQL as shared logical stores keyed by `source_id` and
   `camera_id`. Do not introduce one table per camera unless it is implemented
   as transparent database partitioning for scale/retention.
9. Keep `clip-worker` and `media-worker` as separate evidence-path stages, but
   harden their handoff: unique Replay job labels, source/camera/epoch filtering,
   persisted processed sink outputs, and Redis pending-message recovery.

## References

Current repo files:

- `infra/docker-compose.midterm.yml`
- `infra/env/midterm.env`
- `modules/savant_security/module.yml`
- `modules/savant_security/custom/filters/pts_fps_gate.py`
- `services/clip-worker/app/worker.py`
- `services/clip-worker/app/replay_client.py`
- `services/media-worker/app/worker.py`

Official Savant documentation checked on 2026-06-12:

- `https://savant-ai.io/docs/latest/savant_101/00_streaming_model.html`
- `https://savant-ai.io/docs/latest/savant_101/10_adapters.html`
- `https://savant-ai.io/docs/latest/savant_101/12_module_definition.html`
- `https://savant-ai.io/docs/latest/savant_101/25_top_level_roi.html`
- `https://savant-ai.io/docs/latest/savant_101/54_additional_nvinfer_parameters.html`
- `https://savant-ai.io/docs/latest/advanced_topics/0_batching.html`
- `https://savant-ai.io/docs/latest/advanced_topics/3_frame_filtering.html`
- `https://savant-ai.io/docs/latest/advanced_topics/8_ext_systems.html`
- `https://savant-ai.io/docs/latest/advanced_topics/15_embedded_kvs.html`
- `https://savant-ai.io/docs/latest/advanced_topics/17_restreaming.html`
- `https://savant-ai.io/docs/latest/advanced_topics/19_message_buffering.html`
- `https://savant-ai.io/docs/v0.6.0/advanced_topics/0_batching.html`
- `https://savant-ai.io/docs/v0.6.0/advanced_topics/3_skipping_frames.html`
- `https://savant-ai.io/docs/v0.6.0/savant_101/26_nvinfer.html`
