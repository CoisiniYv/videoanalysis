# T4 Savant stage analysis — local 60-stream validation

Date: 2026-07-10

Implementation commit: `a543349`

Baseline commit: `81b095b`

## Scope

Local RTX 4090 validation of the production T4 diagnostic workflow. Every case used one GPU, two Savant branches, 60 streams at 4 FPS, pose batch 4, face batch 4, and AdaFace batch 16. Short matrix cases sampled for 60 seconds; they validate instrumentation and relative behavior, not the final 400-second production gate.

## Six-stage ablation

Artifact matrix: `/data/video-analytics/artifacts/pressure60_local60_smoke_20260710T0735Z_matrix.json`

| Stage | Steady FPS | Target ratio | Batch 4 ratio | Result |
| --- | ---: | ---: | ---: | --- |
| pose only | 4.0440 | 1.0110 | 99.79% | passed |
| pose + tracker/rules | 4.0895 | 1.0224 | 99.84% | passed |
| pose + face | 4.2230 | 1.0557 | 99.77% | passed |
| pose + face + AdaFace | 4.0575 | 1.0144 | 99.84% | passed |
| full exporter | 4.1625 | 1.0406 | 99.83% | passed |
| full evidence | 4.0850 | 1.0212 | 99.84% | short-drain evidence gate failed |

No cumulative stage reduced steady throughput below 4 FPS. The full-evidence case produced 127 playable bundles but used only a 10-second drain, so `event_outcomes_unaccounted` and `materialization_unresolved_present` are not treated as a production evidence result.

## nvinfer and postprocessing split

Artifact: `/data/video-analytics/artifacts/pressure60_local60_postproc_probe_20260710T0830Z`

| Model element | Total mean | Postproc mean | Estimated inference mean |
| --- | ---: | ---: | ---: |
| YOLO26 pose | 16.687 ms | 5.106 ms | 11.581 ms |
| YOLOv8 face | 19.294 ms | 2.524 ms | 16.770 ms |
| AdaFace | 11.971 ms | 2.394 ms | 9.577 ms |

The 30-second probe sustained 4.10 FPS with 99.69% batch-4 occupancy. Model execution is larger than converter/postprocessing time in this probe, but it does not constrain the local 4 FPS target. INT8 and batch 8 therefore remain pending until the T4 matrix proves model-stage saturation.

## Savant output mode

Artifact matrix: `/data/video-analytics/artifacts/pressure60_local60_output_20260710T0755Z_matrix.json`

| Mode | Steady FPS | Savant peak CPU | Queue full | Send failures |
| --- | ---: | ---: | ---: | ---: |
| frame copy | 4.1075 | 223.13% | 0 | 0 |
| metadata only | 4.1100 | 232.70% | 0 | 0 |

Metadata-only output produced no measurable local throughput benefit at 4 FPS.

## CPU isolation

Artifact matrix: `/data/video-analytics/artifacts/pressure60_local60_cpu_20260710T0805Z_matrix.json`

| CPU mode | Steady FPS | Playable bundles | Result |
| --- | ---: | ---: | --- |
| shared | 4.1580 | 126 | passed |
| local 24-CPU isolation | 4.1200 | 120 | passed |

Isolation did not improve local throughput. Worker cpusets were verified during the run and all four worker containers were verified restored to their original empty cpuset afterward. Isolation remains a T4 diagnostic variable, not a default.

## Batch timeout

Artifact matrix: `/data/video-analytics/artifacts/pressure60_local60_timeout_20260710T0815Z_matrix.json`

| Timeout | Steady FPS | Batch 4 ratio | Queue full | Send failures |
| --- | ---: | ---: | ---: | ---: |
| 10 ms | 4.1100 | 99.81% | 0 | 0 |
| 20 ms | 4.0545 | 99.81% | 0 | 0 |
| 40 ms | 4.1610 | 99.77% | 0 | 0 |

All values meet throughput and occupancy requirements. The selection rule chooses the lowest timeout that sustains at least 99% of target FPS, at least 95% batch-4 occupancy, and zero queue/send failures. The local candidate is therefore 10 ms; the T4 run must validate it independently.

## Local conclusion

- The dual-branch batch-4 path is healthy locally; actual full-batch occupancy is above 99.7% in every 60-stream case.
- No ablation stage causes a 4 FPS throughput drop on the RTX 4090.
- Frame-copy output and CPU isolation are not local bottlenecks.
- Use 10 ms as the first T4 timeout candidate, while retaining 20/40 ms controls.
- Do not build INT8 or batch-8 engines until T4 data shows estimated inference time is the dominant saturated stage.

The next version-controlled step is deploying commit `a543349` plus this report commit to `192.168.1.100`, preserving the production SSD rolling-cache and 2 TB evidence-disk mounts, and running the same matrix on the single T4.
