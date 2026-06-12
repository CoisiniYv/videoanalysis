# Midterm Worker And Savant Batching Findings

## Status

Date: 2026-06-12

This note freezes the read-only runtime diagnosis for two questions:

- whether `clip-worker` or `media-worker` can stall the current program flow;
- whether the current Savant 8 FPS limit is frame skipping, and how it relates
  to batching and TensorRT model execution.

No code change is implied by this note. It records current behavior and repair
priorities for later implementation.

## Runtime Chain Boundary

Current chain:

```text
RTSP -> Replay storage -> Savant inference -> Redis events/annotations
  -> event-worker -> clip-worker -> Replay job -> video-file-sink
  -> media-worker evidence sidecar -> 8090 operator portal
```

`clip-worker` and `media-worker` sit after Savant inference. They should not
block RTSP ingest, Replay storage, or Savant's main inference path directly.

They can block or degrade the evidence path. User-visible symptoms include
events stuck in `replay_job_created`, failed clips, missing evidence media,
delayed sidecars, and 8090 evidence detail pages showing incomplete media.

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

Current defaults:

```text
MAX_FPS_CONTROL=true
MAX_FPS=8/1
MIN_FPS=2/1
SOURCE_INPUT_FPS_ESTIMATE=24
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

## Savant Batching Model

Savant batching has two relevant layers:

- pipeline / muxer batch, configured by module-level values such as
  `parameters.batch_size`, `max_same_source_frames`, and
  `batched_push_timeout`;
- model batch, configured by each nvinfer model's `model.batch_size`.

Current project configuration:

```text
module parameters:
BATCH_SIZE=1
MAX_PARALLEL_STREAMS=4
max_same_source_frames=1
BATCHED_PUSH_TIMEOUT=40000

model batches:
POSE_BATCH_SIZE=1
FACE_DETECTOR_BATCH_SIZE=1
FACE_EMBEDDING_BATCH_SIZE=16
```

Implication:

- the current pipeline is not using multi-frame muxer batching;
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
6. Keep `MAX_FPS=8/1` documented as intentional frame dropping before inference.
   Do not treat Redis frame annotation rate as source FPS; it is downstream of
   ingress filtering and model intervals.
7. Treat detector multi-batch as a separate throughput task. Start with
   controlled benchmarks and engine generation for pose/face bN; do not change
   `BATCH_SIZE`, detector `model.batch_size`, and intervals in one unmeasured
   runtime change.

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

- `https://savant-ai.io/docs/v0.6.0/advanced_topics/0_batching.html`
- `https://savant-ai.io/docs/v0.6.0/advanced_topics/3_skipping_frames.html`
- `https://savant-ai.io/docs/v0.6.0/savant_101/26_nvinfer.html`
