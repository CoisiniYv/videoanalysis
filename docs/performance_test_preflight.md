# Performance Test Preflight

Status: R2 operator draft. Performance testing starts after R2, not during R2.

## Purpose

Before single-GPU batch performance work, confirm the mainline is repeatable and media output is organized.

## Required Preflight Checks

1. Mainline compose is `infra/docker-compose.c1-official-adapter.yml`.
2. `savant-security` uses `modules/savant_security`.
3. Source adapters can be started and stopped repeatedly with `scripts/runtime/camera_source_controller.py`.
4. Face registration through `register_face_image.py` is repeatable.
5. `security.face_observations` and PostgreSQL `face_observations` are stable.
6. F4.3 debug recognition smoke is repeatable.
7. Media outputs go under `/data/video-analytics/media`.
8. `face/`, `testVideo/`, `manual-inspection/`, and `/data` outputs are not committed.
9. Do not use one 40 minute movie run as a substitute for formal performance testing.

## Next Performance Matrix

Run progressively:

- 1 stream.
- 4 streams.
- 8 streams.
- 16 streams.
- Single GPU.
- pose + tracker.
- pose + tracker + YOLOv8-Face.
- pose + tracker + YOLOv8-Face + AdaFace.
- pose + tracker + YOLOv8-Face + AdaFace + face-worker pgvector.

## Metrics

- input fps.
- processed fps.
- GPU utilization.
- GPU memory.
- Redis stream length.
- Redis lag.
- `face_observations` per minute.
- DB write latency.
- pgvector search latency.

## Common Troubleshooting

- Throughput falls after adding AdaFace: inspect `FACE_EMBEDDING_BATCH_SIZE` and face count per frame.
- Redis lag grows: inspect consumer group pending and face-worker logs.
- DB latency grows: inspect indexes, batch size, and pgvector search timing.
- GPU memory pressure: reduce stream count or batch sizes before changing algorithms.

No performance result is valid unless the exact source count, model stack, thresholds, and output sinks are recorded.

