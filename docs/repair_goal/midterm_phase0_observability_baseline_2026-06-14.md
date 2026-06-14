# Midterm Phase 0 Observability Baseline - 2026-06-14

Scope: `specs/16_dual_path_30x2_t4_production_optimization.md` Phase 0
observability and pre-experiment baseline. No Phase 0A/0B/0C/0D runtime lever
has been kept yet.

## Phase 0 Observability

Commit:

- `c816907 Surface restart storm health in runtime overview`

Validation:

- `pytest -q harness/tests/test_runtime_overview_api.py harness/tests/test_operator_runtime_overview_static.py`
  - result: `7 passed`
- `docker compose -f infra/docker-compose.midterm.yml config`
  - result: valid config
- `git diff --check`
  - result: clean

Deployment applied after commit:

- Rebuilt/recreated only `api` and `evidence-viewer` with
  `docker compose -f infra/docker-compose.midterm.yml up -d --build api evidence-viewer`.
- Data path containers were not intentionally restarted: Savant, Replay,
  source adapters, Redis, workers, and video-file-sink were left running under
  their existing policies.

Runtime overview verification after deploy:

- `/api/v1/runtime/overview` now includes `restart_count`,
  `restart_rate_per_min`, `restart_count_delta`,
  `restart_rate_window_seconds`, and `restart_warning` for fixed containers.
- Dynamic source rows are now inspected individually and include real
  `restart_count` and restart warning fields.
- Health is red with issue `container_restart_count_high`.
- High restart containers:
  - `video-analytics-midterm-source-adapter`
  - `video-analytics-source-source_00000000-0000-4000-8000-781078565686`

## Baseline Snapshot

Time window: 2026-06-14 14:04-14:12 UTC.

Container state:

| Container | RestartCount | State | Notes |
|---|---:|---|---|
| `video-analytics-midterm-source-adapter` | 1069 -> 1071 | running | restart storm still active |
| `video-analytics-source-source_00000000-0000-4000-8000-781078565686` | 1435 -> 1438 | running | restart storm still active |
| `video-analytics-midterm-savant` | 0 | running / healthy | `/opt/savant/status.txt` = `running` |
| `video-analytics-midterm-replay-service` | 0 | running | no container restart |
| `video-analytics-midterm-api` | 0 | running | recreated only for observability deploy |
| `video-analytics-midterm-evidence-viewer` | 0 | running | recreated only for observability deploy |

Savant metrics:

- `va_savant_sources_active = 2`
- Short-window effective FPS observed:
  - `primary_rtsp`: about 4.1-4.6 fps
  - `source_00000000-0000-4000-8000-781078565686`: about 4.0-8.0 fps
- `va_savant_last_frame_age_seconds` stayed near zero for both sources during
  the snapshot.
- Queue pressure was visible in Savant stage metrics:
  - `stage_queue_length{stage_name="source"}` observed at 50-201
  - `stage_queue_length{stage_name="decode"}` observed at 14
  - `stage_queue_length{stage_name="muxer"}` observed at 4-5
  - `stage_queue_length{stage_name="demuxer"}` observed at 34

Log evidence:

- `video-analytics-midterm-source-adapter` repeatedly hit
  `WriterResultSendTimeout` after ZeroMQ `Resource temporarily unavailable`,
  then exited the GStreamer pipeline with `Internal data stream error (-5)`.
- Dynamic source adapter showed the same `WriterResultSendTimeout` pattern.
- `video-analytics-midterm-replay-service` showed continuous
  `Received message with mismatched routing_id` warnings during the snapshot.
- `video-analytics-midterm-savant` stayed running and continued emitting
  pipeline stats and project metrics.

GPU probe:

- `nvidia-smi --query-gpu=...` failed on this host with
  `NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver`.
  Phase 0 GPU/decode comparisons cannot rely on host `nvidia-smi` until the
  driver visibility issue is resolved or an alternate probe is used.

## Gate Status

`PASS_PHASE0_OBSERVABILITY`: achieved for code and deployed runtime overview.

Next allowed step: Phase 0A code change to decouple Savant `PtsFpsGate.enabled`
from nvstreammux `MAX_FPS_CONTROL`. Do not flip the live `MAX_FPS_CONTROL`
setting until that code change is tested and committed.
