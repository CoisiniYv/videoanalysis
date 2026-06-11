# 15_savant_performance_observability.md

## 1. Goal

Define the Savant-aligned performance observability contract for the midterm
runtime:

- how official Savant collects pipeline performance statistics;
- what this project must measure for throughput, inference activity, runtime
  state, and resource usage;
- how to diagnose performance problems such as low FPS, stuck pipelines,
  queue growth, CPU saturation, and GPU saturation.

This spec is based on Savant official documentation checked on 2026-06-11.
The current repo image is `ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1`;
the public latest documentation currently renders as Savant 0.6.1/develop, so
exact metric names must be discovered from the running image's `/metrics`
endpoint before dashboard rules are finalized.

Official references:

- Savant Prometheus metrics:
  `https://savant-ai.io/docs/latest/advanced_topics/9_prometheus_metrics.html`
- Savant OpenTelemetry:
  `https://savant-ai.io/docs/latest/advanced_topics/9_open_telemetry.html`
- Savant benchmarking and optimization:
  `https://savant-ai.io/docs/latest/advanced_topics/0_pipeline_benchmarking.html`
- Savant stream limit:
  `https://savant-ai.io/docs/latest/advanced_topics/0_pipeline_stream_limit.html`
- Savant pipeline idle monitor:
  `https://savant-ai.io/docs/latest/advanced_topics/3_pipeline_idle_monitor.html`
- Savant pipeline watchdog:
  `https://savant-ai.io/docs/latest/advanced_topics/16_pipeline_watchdog.html`
- Official metrics sample:
  `https://github.com/insight-platform/Savant/tree/develop/samples/pass_through_processing`

## 2. Official Savant Performance Statistics Model

Savant exposes performance data through three complementary paths.

### 2.1 Prometheus / OpenMetrics

Savant supports Prometheus-format metrics in two categories:

1. System-level metrics collected by the pipeline and adapters.
2. User-level metrics created by project PyFunc code.

Official system-level metrics are intended to cover pipeline operation such as:

- queues;
- frames;
- detected objects;
- latencies;
- other runtime information related to pipeline operation.

Metrics collection is configured under `parameters.telemetry.metrics`:

```yaml
parameters:
  telemetry:
    metrics:
      frame_period: ${oc.decode:${oc.env:METRICS_FRAME_PERIOD, 10000}}
      time_period: ${oc.decode:${oc.env:METRICS_TIME_PERIOD, null}}
      history: ${oc.decode:${oc.env:METRICS_HISTORY, 100}}
      extra_labels: ${json:${oc.env:METRICS_EXTRA_LABELS, null}}
```

The default embedded Savant web server port is controlled by `WEBSERVER_PORT`
and defaults to `8080` in Savant's default config. The official
`pass_through_processing` sample scrapes module containers at `:8080` with
Prometheus.

User-level metrics are created in PyFunc code with Savant's metric helpers:

```python
from savant.metrics import get_or_create_counter, get_or_create_gauge
```

Savant supports `CounterFamily` and `GaugeFamily`. The official sample counts
processed frames per source and reads recent runtime metrics with
`self.get_runtime_metrics(1)` to derive queue length.

### 2.2 OpenTelemetry Trace Profiling

Savant automatically creates spans for pipeline stages and lets PyFunc code
create nested spans. In Savant, a frame can carry a trace id, which makes it
possible to profile one frame through concurrent pipeline stages without
guessing from interleaved logs.

OpenTelemetry is the right tool for deep performance debugging:

- per-frame stage duration;
- slow PyFunc sections;
- end-to-end latency through chained modules;
- sampled profiling in production-like runs.

Tracing is configured under `parameters.telemetry.tracing` or a provider config
file. Production must use sampling; full tracing of every frame is too costly
for long 30/60-camera tests.

### 2.3 Benchmarking Modes

Official Savant benchmarking separates:

- non-real-time single-stream benchmark;
- non-real-time multi-stream benchmark;
- real-time multi-stream benchmark.

It also separates:

- isolated pipeline benchmarking without adapters or real sinks;
- end-to-end benchmarking with adapters, sinks, Redis/Postgres, and external
  systems included.

For real-time multi-stream capacity, Savant's model is to vary the number of
parallel streams and measure whether aggregate FPS still satisfies the target
per-stream real-time requirement. With a 30 FPS source, official docs describe
the capacity relation as `N = X / 30`, where `X` is aggregate pipeline FPS and
`N` is stream count.

For this project, source FPS is intentionally gated lower than camera-native
FPS, so the same idea becomes:

```text
required_aggregate_fps = active_source_count * configured_effective_source_fps
```

For midterm defaults:

```text
configured_effective_source_fps = 5..8 FPS per source
MAX_FPS=8/1
MIN_FPS=2/1
POSE_INFER_INTERVAL=1
FACE_INFER_INTERVAL=2
FACE_EMBEDDING_INFER_INTERVAL=2
```

## 3. Current Midterm Gap

Current files inspected:

- `modules/savant_security/module.yml`
- `infra/docker-compose.midterm.yml`
- `infra/env/midterm.env`

Observed state:

- The Savant module does not explicitly set `parameters.telemetry.metrics`.
  It relies on Savant defaults and environment variables if provided.
- The midterm compose does not expose or publish Savant's web server metrics
  port.
- The midterm compose does not include Prometheus or Grafana services.
- The midterm env file does not set `METRICS_FRAME_PERIOD`,
  `METRICS_TIME_PERIOD`, `METRICS_HISTORY`, or `METRICS_EXTRA_LABELS`.
- Existing runtime checks rely on Docker state, logs, Redis stream activity,
  and manual `nvidia-smi dmon`, not official Savant metrics.

This means the project can currently infer that frames or events are flowing,
but it cannot reliably answer the production questions:

- how many frames reached Savant per source;
- how many frames passed each inference-related stage;
- whether queues are growing;
- which stage is slow;
- whether low FPS is CPU-bound, GPU-bound, Python/GIL-bound, or source-bound.

## 4. Required Metric Groups

### 4.1 Pipeline Throughput

Required answers:

- How many frames entered Savant?
- How many frames reached post-inference export?
- What is the effective FPS per source?
- Is any source stale?

Required metrics:

```text
va_savant_frames_seen_total{source_id}
va_savant_frame_annotations_exported_total{source_id}
va_savant_effective_fps{source_id,window}
va_savant_last_frame_age_seconds{source_id}
va_savant_sources_active
```

Preferred source:

1. Official Savant system frame metrics from `/metrics`, once exact names are
   discovered for the running image.
2. Project PyFunc counters as stable project-level aliases.
3. Redis `security.frame_annotations` growth as an external verification path.

Do not treat Redis stream length alone as Savant FPS. Redis can be throttled,
trimmed, blocked, or disconnected independently from the pipeline.

### 4.2 Inference Activity

Required answers:

- How many frames had pose-stage output opportunity?
- How many frames produced person detections?
- How many frames had face detections?
- How many face embeddings were produced?
- Are configured inference intervals and FPS gates matching the expected load?

Required project metrics:

```text
va_savant_pose_stage_frames_total{source_id}
va_savant_pose_frames_with_person_total{source_id}
va_savant_pose_objects_total{source_id}
va_savant_face_stage_frames_total{source_id}
va_savant_face_frames_with_face_total{source_id}
va_savant_face_objects_total{source_id}
va_savant_adaface_embeddings_total{source_id}
va_savant_person_observations_exported_total{source_id}
va_savant_face_observations_exported_total{source_id}
```

Important semantic rule:

`nvinfer.interval` means not every incoming frame triggers a fresh model
inference. A counter after a model stage is a processed-frame or observed-output
counter unless it is wired directly to a reliable per-inference signal. The
dashboard must label these as `stage_frames`, `frames_with_output`, or
`objects_total`; do not label them as exact GPU inference calls unless that
has been proven from the runtime metric source.

For current midterm defaults, expected upper bounds are approximately:

```text
pose inference opportunity ~= gated_input_fps / (POSE_INFER_INTERVAL + 1)
face inference opportunity ~= gated_input_fps / (FACE_INFER_INTERVAL + 1)
embedding opportunity ~= detected_faces_after_gate / (FACE_EMBEDDING_INFER_INTERVAL + 1)
```

These are configuration-derived expectations, not measured counters.

### 4.3 Queue And Latency

Required answers:

- Is the pipeline keeping up, or are queues growing?
- Which stage is slow?
- What is the event latency from source frame to DB/API?

Required metrics:

```text
savant official queue length by stage
savant official stage latency by stage
va_savant_total_queue_length
va_savant_stage_latency_seconds{stage}
va_event_latency_seconds{event_type,source_id}
va_redis_stream_pending{stream,group}
va_redis_stream_lag{stream,group}
```

The first two should come from official Savant system metrics or
OpenTelemetry. `va_savant_total_queue_length` may be a user-level gauge derived
from `self.get_runtime_metrics(1)` as shown in the official sample.

### 4.4 Runtime State

Required answers:

- Is Savant alive?
- Is it actually processing frames?
- Are source adapters attached?
- Are restart loops happening?

Required metrics/checks:

```text
container_running{container}
container_healthy{container}
container_restart_count{container}
va_savant_metrics_scrape_up
va_savant_frame_flow_recent{source_id}
va_savant_log_error_count{pattern}
va_source_adapter_running{source_id}
va_replay_service_running
```

Runtime state must not rely on Docker `healthy` alone. A container can be
healthy while Savant receives no frames. A valid readiness gate needs all of:

```text
container is running
metrics endpoint is scrapeable
recent frame counter or frame annotation exists for each enabled source
restart count is stable
no recent fatal pad/streammux/model errors
```

### 4.5 GPU, CPU, Memory, And IO

Official Savant docs recommend `nvidia-smi`, `tegrastats`, `sar`, `nvtop`,
`htop`, OpenTelemetry, and ClientSDK for benchmarking analysis. For this
project, production-grade resource metrics should come from exporters rather
than manual commands.

Required resource metrics:

```text
gpu_utilization_percent{gpu}
gpu_memory_used_bytes{gpu}
gpu_memory_total_bytes{gpu}
gpu_decoder_utilization_percent{gpu}
gpu_encoder_utilization_percent{gpu}
gpu_temperature_celsius{gpu}
gpu_power_watts{gpu}
container_cpu_usage_percent{container}
container_memory_working_set_bytes{container}
host_cpu_usage_percent
host_memory_available_bytes
disk_read_write_bytes_total{device}
```

Preferred collection:

- GPU: NVIDIA DCGM exporter, or `nvidia-smi` fallback in smoke scripts.
- Container CPU/memory: cAdvisor or Docker metrics.
- Host CPU/memory/disk: node exporter or `sar` fallback.

## 5. Recommended Midterm Wiring

### 5.1 Enable Savant Metrics

Set explicit metrics env for `savant-security`:

```yaml
environment:
  WEBSERVER_PORT: "8080"
  METRICS_FRAME_PERIOD: "1000"
  METRICS_TIME_PERIOD: "5"
  METRICS_HISTORY: "100"
  METRICS_EXTRA_LABELS: '{"service":"savant-security","profile":"midterm"}'
```

Prometheus should scrape the internal Docker network target:

```yaml
scrape_configs:
  - job_name: savant-midterm
    scrape_interval: 5s
    static_configs:
      - targets:
          - video-analytics-midterm-savant:8080
```

Publishing the metrics port to the host is optional for development but should
not be required in production. If published locally, use a non-conflicting port,
for example `18080:8080`.

### 5.2 Add A Lightweight Metrics PyFunc

Add one dedicated metrics PyFunc only if official system metrics do not expose
stable project-level answers. It should:

- run after tracker/person metadata is available;
- count per-source frames and objects;
- count frames with person and face outputs;
- count exported observations if those exporters do not already expose metrics;
- publish last-frame-age gauge;
- read `self.get_runtime_metrics(1)` for queue length when available.

It must not:

- write images;
- call PostgreSQL;
- block on Redis;
- run heavy numpy/image work;
- change event semantics.

### 5.3 Add External Resource Exporters

For sustained 30/60-camera tests, add:

- Prometheus;
- Grafana;
- DCGM exporter;
- cAdvisor;
- node exporter.

If this is too much for the immediate midterm demo, keep the Savant metrics
endpoint and use command-line fallbacks in the smoke script.

### 5.4 Add Observability Smoke

Add a smoke script that proves metrics are present before running a long
performance test:

```bash
curl --noproxy '*' -fsS http://127.0.0.1:18080/metrics
docker inspect --format '{{.State.Status}} {{.State.Health.Status}} {{.RestartCount}}' \
  video-analytics-midterm-savant
docker exec video-analytics-midterm-redis redis-cli XLEN security.frame_annotations
nvidia-smi dmon -s pucvmet -c 10
```

The smoke must print:

```text
PASS_SAVANT_PERF_OBSERVABILITY_READY
```

only when:

- `/metrics` is scrapeable;
- at least one frame counter or project alias is non-zero;
- Savant restart count is stable during the sample window;
- Redis frame annotations advance or a direct Savant frame counter advances;
- GPU metrics are available, or the script explicitly reports
  `gpu_metrics_unavailable` for CPU-only/dev machines.

## 6. Dashboard Contract

The Grafana dashboard should include these panels.

### 6.1 Ingest And FPS

- Effective FPS per source over 1m and 5m windows.
- Total aggregate FPS.
- Active source count.
- Last frame age per source.
- Frame drop or filter count if exposed by official metrics or ingress gate.

### 6.2 Inference Outputs

- Person detections per second.
- Pose frames with at least one person.
- Face detections per second.
- AdaFace embeddings per second.
- Face observations exported per second.
- Person observations exported per second.

### 6.3 Queue And Latency

- Queue length by stage.
- Total queue length.
- Stage latency p50/p95 when available.
- Event latency p50/p95 by event type.
- Redis consumer lag and pending count.

### 6.4 Runtime Health

- Savant up/scrape status.
- Container restart count by service.
- Source adapter state by source.
- Replay service state.
- Last runtime apply epoch if available.
- Recent error count for `ERROR`, `Exception`, `Traceback`, `pad`,
  `streammux`, `nvinfer`, and `TensorRT`.

### 6.5 Resource Headroom

- GPU utilization.
- GPU memory.
- GPU decoder/encoder utilization.
- GPU temperature and power.
- Savant container CPU and memory.
- Host CPU, memory, and disk IO.
- Replay RocksDB disk usage and write rate.

## 7. Performance Problem Diagnosis

Use the following interpretation rules.

### 7.1 Source-Bound Or Input-Bound

Symptoms:

- GPU and CPU are low.
- Savant queues are low.
- Source FPS is below target.
- Source adapter logs show reconnects, RTSP timeout, or low incoming cadence.

Likely causes:

- RTSP source not delivering frames.
- Network jitter.
- source-adapter stalled.
- Replay is not forwarding to Savant.

First checks:

```bash
docker logs --tail 300 video-analytics-midterm-source-adapter
docker logs --tail 300 video-analytics-midterm-replay-service
docker exec video-analytics-midterm-redis redis-cli XREVRANGE security.frame_annotations + - COUNT 5
```

### 7.2 Pipeline Stuck But Container Healthy

Symptoms:

- Docker says Savant is running or healthy.
- `/metrics` is up.
- frame counters stop advancing.
- Redis frame annotations stop advancing.

Likely causes:

- ZMQ route/source id mismatch.
- Replay routing identity problem.
- streammux pad/session issue after source restart.
- source adapter connected to wrong endpoint.

First checks:

```bash
docker logs --tail 500 video-analytics-midterm-savant | \
  rg 'source_id|routing|pad|streammux|ERROR|Exception|Traceback'
docker inspect --format '{{.RestartCount}} {{.State.Status}}' \
  video-analytics-midterm-savant video-analytics-midterm-source-adapter
```

### 7.3 GIL-Bound Python

Official Savant docs call out a common pattern:

```text
one CPU core near 100%
other CPU cores underused
GPU underused
pipeline FPS below target
```

Likely causes:

- heavy pure-Python PyFunc logic;
- synchronous Redis/Postgres/API work in the pipeline;
- expensive per-frame JSON/image operations.

Fix direction:

- move heavy business logic out of Savant;
- use NumPy/OpenCV/Cython/Rust/Numba for hot code;
- split modules through Savant pipeline chaining;
- run multiple pipeline instances when appropriate.

### 7.4 CPU-Bound

Symptoms:

- many CPU cores high;
- GPU underused;
- queues grow;
- FPS below target.

Likely causes:

- decode, pre/post-processing, tracker, or PyFunc CPU work dominates;
- Redis/export work too frequent;
- host CPU not enough for source count.

Fix direction:

- reduce export frequency;
- increase inference interval or ingress FPS gating;
- move CPU-heavy logic out of the pipeline;
- use more CPU cores or split pipelines.

### 7.5 GPU-Bound

Symptoms:

- GPU utilization high;
- GPU memory near limit;
- stage latency grows around `nvinfer`;
- queues grow before or after model stages;
- CPU has headroom.

Likely causes:

- model chain too heavy for stream count;
- batch/interval not tuned;
- TensorRT precision or engine not optimized;
- too many streams per GPU.

Fix direction:

- raise model intervals for lower-priority stages;
- reduce input FPS;
- use FP16/INT8 where validated;
- split pose and face modules;
- shard streams across GPUs;
- prune/replace models.

### 7.6 Backpressure From External Systems

Symptoms:

- Savant inference looks healthy.
- Redis/Postgres/API workers lag.
- event latency increases.
- stream pending counts grow.

Likely causes:

- event-worker/face-worker/clip-worker bottleneck;
- Redis stream consumer group lag;
- PostgreSQL slow writes or locks;
- evidence/Replay jobs saturating IO.

Fix direction:

- scale workers;
- reduce pipeline export volume;
- cap evidence concurrency;
- separate performance tests for inference and evidence path.

## 8. Acceptance For Performance Runs

A performance run is valid only if it records:

- run id and timestamp;
- git commit and dirty worktree summary;
- compose file and env file used;
- active source count and source ids;
- model intervals and FPS gate settings;
- Savant metrics scrape output or Prometheus snapshot;
- GPU and CPU sample output;
- Redis stream lag and frame annotation growth;
- container restart counts before and after;
- event latency summary when events are part of the run.

Minimum pass criteria for a 10-15 minute midterm run:

```text
Savant metrics scrape succeeds for the full run.
Each enabled source has recent frame flow.
Per-source effective FPS stays within the configured target window unless source input is lower.
Total queue length does not grow unbounded.
Savant restart count does not increase.
No fatal pad/streammux/nvinfer/TensorRT error appears.
GPU memory does not approach OOM.
CPU/GPU bottleneck classification is recorded.
Redis consumer lag is bounded.
Event latency p95 is recorded; target remains under 2 seconds for main alerts where applicable.
```

For future 30/60-camera validation, the run must produce a saved metrics bundle
under:

```text
/data/video-analytics/artifacts/perf/<run_id>/
  run_config.json
  prometheus_snapshot.txt
  savant_metrics_head.txt
  docker_stats.jsonl
  nvidia_smi.jsonl
  redis_streams.json
  summary.json
```

## 9. Commands For Manual Verification

Discover Savant metrics names:

```bash
curl --noproxy '*' -fsS http://127.0.0.1:18080/metrics | sed -n '1,220p'
```

Check frame annotation flow:

```bash
docker exec video-analytics-midterm-redis \
  redis-cli XREVRANGE security.frame_annotations + - COUNT 10
docker exec video-analytics-midterm-redis \
  redis-cli XLEN security.frame_annotations
```

Check runtime state:

```bash
docker inspect --format '{{.Name}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restart={{.RestartCount}}' \
  video-analytics-midterm-savant \
  video-analytics-midterm-source-adapter \
  video-analytics-midterm-replay-service \
  video-analytics-midterm-event-worker \
  video-analytics-midterm-face-worker
```

Check GPU:

```bash
nvidia-smi dmon -s pucvmet -c 10
nvidia-smi --query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw \
  --format=csv
```

Check container resource usage:

```bash
docker stats --no-stream \
  video-analytics-midterm-savant \
  video-analytics-midterm-source-adapter \
  video-analytics-midterm-replay-service \
  video-analytics-midterm-event-worker \
  video-analytics-midterm-face-worker
```

Check Savant logs for performance blockers:

```bash
docker logs --tail 500 video-analytics-midterm-savant | \
  rg 'ERROR|Exception|Traceback|pad|streammux|nvinfer|TensorRT|FPS|queue|source_id'
```

## 10. PromQL Starting Points

Exact official metric names must be discovered from `/metrics`. After aliases
or exact names are known, use queries like:

```promql
rate(va_savant_frames_seen_total[1m])
sum(rate(va_savant_frames_seen_total[1m]))
increase(va_savant_pose_stage_frames_total[10m])
rate(va_savant_pose_objects_total[1m])
rate(va_savant_face_objects_total[1m])
rate(va_savant_adaface_embeddings_total[1m])
max_over_time(va_savant_last_frame_age_seconds[1m])
max(va_savant_total_queue_length)
histogram_quantile(0.95, rate(va_event_latency_seconds_bucket[5m]))
```

Do not promote a dashboard to production until every panel has a known source:

```text
official Savant metric
project PyFunc metric
container/exporter metric
Redis/Postgres worker metric
manual smoke fallback
```
