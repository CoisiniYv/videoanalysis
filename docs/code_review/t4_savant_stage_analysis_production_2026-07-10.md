# T4 Savant stage analysis — production results

Date: 2026-07-10

Production host: `192.168.1.100`, Tesla T4 16 GB, 16 logical CPUs

Deployed code: `62faf68` (incremental manifests under `/data/video-analytics/deployments`)

## Deployment boundary

- Evidence/media remained on the 1.8 TB `/data` disk.
- Rolling cache remained under `/home/user/video-analytics-fast` on the 512 GB NVMe.
- Docker root remained `/home/docker-data`.
- `infra/env/midterm.env` and `infra/docker-compose.midterm.yml` checksums were unchanged by deployment.
- The host has no Git checkout. Each incremental deployment contains a file list, SHA256 manifest, and backup.

## Six-stage ablation

Matrix: `/data/video-analytics/artifacts/pressure60_t4_60s_ablation_eb496cb_20260710T0910Z_matrix.json`

| Stage | Steady FPS | Result |
| --- | ---: | --- |
| pose only | 4.0460 | passed |
| pose + tracker/rules | 4.0500 | passed |
| pose + face | 4.0675 | passed |
| pose + face + AdaFace | 2.7500 | queue/send gate failed |
| full exporter | 4.0710 | passed |
| full evidence | unavailable | event quiescence exception before diagnostics |

The two YOLO models are not the 4 FPS blocker: `pose + face` sustained 4.04–4.09 FPS. The cropped AdaFace stage showed a repeatable but variable slowdown: the repeat artifact `/data/video-analytics/artifacts/pressure60_t4_adaface_repeat_eb496cb_20260710T0945Z` sampled 3.725, 2.850, and 3.700 FPS. The next, more complete exporter graph returned to 4.07 FPS, proving that pad-to-pad element latency includes queue/backpressure behavior and cannot be interpreted as pure TensorRT compute.

## Output mode

Matrix: `/data/video-analytics/artifacts/pressure60_t4_60s_output_bcc73c0_20260710T0952Z_matrix.json`

| Mode | Steady FPS | Queue full samples | Send failures |
| --- | ---: | ---: | ---: |
| frame copy | 3.1225 | 1 | 2 |
| metadata only | 2.2815 | 2 | 5 |

Metadata-only did not improve throughput, so unused frame copy is not the active T4 bottleneck.

## CPU isolation

Artifact: `/data/video-analytics/artifacts/pressure60_t4_cpu_isolated_full_exporter_bcc73c0_20260710T1005Z`

The `t4-16cpu` profile produced 2.575, 2.123, and 2.250 FPS. Splitting each Savant into six fixed logical CPUs and workers into two CPUs is worse than shared scheduling. The profile must remain diagnostic-only. All worker cpusets were restored to their original empty value.

## Batch timeout

Matrix: `/data/video-analytics/artifacts/pressure60_t4_60s_timeout_bcc73c0_20260710T1012Z_matrix.json`

| Timeout | Steady FPS | Target ratio | Batch-4 ratio | Result |
| --- | ---: | ---: | ---: | --- |
| 10 ms | 3.4895 | 0.8724 | 99.54% | passed existing gates, below 4 FPS target |
| 20 ms | 3.2250 | 0.8063 | 99.46% | queue/send gate failed |
| 40 ms | 1.9150 | 0.4788 | 99.36% | queue/send gate failed |

Batch fullness is already above 99%. A 40 ms timeout is not needed to fill batch 4 and materially worsens T4 queueing. Among tested values, 10 ms is the only viable candidate, but it is not sufficient by itself to guarantee 4 FPS.

## Rejected controlled changes

| Change | Artifact | Outcome |
| --- | --- | --- |
| AdaFace interval 7, detector interval 3 | `pressure60_t4_adaface_interval7_bt10_bcc73c0_20260710T1032Z` | 2.565/2.730/2.498 FPS |
| AdaFace batch 8 FP16 | `pressure60_t4_adaface_b8_bt10_bcc73c0_20260710T1042Z` | 3.477/2.330/2.263 FPS; rejected |
| max parallel streams 32 | `pressure60_t4_parallel32_bt10_b16_bcc73c0_20260710T1050Z` | about 2.05–2.20 FPS; rejected |
| face/AdaFace interval 4 | `pressure60_t4_face_interval4_bt10_b16_bcc73c0_20260710T1100Z` | 3.792/4.050/2.443 FPS |

The T4-built AdaFace b8 engine remains as an unused diagnostic asset at `/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx_b8_gpu0_fp16.engine`. The saved production operating point was restored to batch 16.

## GPU and preprocessing evidence

Clock/power artifact: `/data/video-analytics/artifacts/pressure60_t4_gpuclock_bt10_ae60473_20260710T1106Z`

- P-state remained P0.
- SM clock remained 1380–1575 MHz versus 1590 MHz maximum.
- Temperature was 68–74 C; no thermal throttle was observed.
- Active throttle reason `0x4` is the 70 W software power cap.
- Later samples used only 34–42% GPU while FPS fell to 1.57–1.75, so the GPU was underfed rather than thermally throttled.
- Both Savant branches slowed together; final forwarder queue depths were 2048 and 1818.

Preprocessing artifact: `/data/video-analytics/artifacts/pressure60_t4_preproc_probe_62faf68_20260710T1113Z`

- AdaFace AlignFace GPU preproc rose from about 29 ms to 41 ms.
- Face nvinfer pad wall time rose from about 449 ms to 1.11 s.
- AlignFace is measurable but does not account for the full stall.
- Element wall time includes asynchronous queue and scheduling waits. It is not a true TensorRT kernel-time metric.

## Decision

The production pressure gate is not passed. Do not switch the production defaults to batch 8, CPU isolation, metadata-only, parallel 32, or reduced face cadence. Do not build INT8 yet: the current probes do not measure true TensorRT compute time, and GPU utilization is not consistently saturated.

The next controlled implementation should preserve single-GPU dual branches and focus on:

1. real NvInfer/CUDA compute timing rather than pad wall time;
2. CUDA MPS or equivalent dual-process context scheduling on T4;
3. eliminating non-evidence materialization tasks during Savant-only matrices;
4. repeating full-exporter at 10 ms after the above, then running the 400-second/120-second evidence gate only after stable 4 FPS is demonstrated.

## Restored state

At handoff the production host had one healthy daily Savant branch, no pressure source containers or pressure processes, zero epoch-blocking evidence tasks, empty worker cpusets, and the saved defaults `FACE_EMBEDDING_BATCH_SIZE=16`, `FACE_INFER_INTERVAL=3`, `FACE_EMBEDDING_INFER_INTERVAL=3`, `BATCHED_PUSH_TIMEOUT=40000`, and `MAX_PARALLEL_STREAMS=64`.
