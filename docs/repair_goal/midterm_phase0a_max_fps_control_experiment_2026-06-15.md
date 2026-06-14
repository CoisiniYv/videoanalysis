# Midterm Phase 0A MAX_FPS_CONTROL Experiment - 2026-06-15

Scope: `specs/16_dual_path_30x2_t4_production_optimization.md` Phase 0A.
This experiment tested disabling Savant nvstreammux `MAX_FPS_CONTROL` while
keeping the independent ingress `PtsFpsGate` enabled.

## Preconditions

Committed prerequisite:

- `5b5debb Decouple Savant ingress FPS gate from muxer control`

The committed prerequisite split the controls:

- `MAX_FPS_CONTROL` controls Savant/nvstreammux `max-fps-control`.
- `INGRESS_FPS_GATE_ENABLED` controls the project `PtsFpsGate`.

Live experiment state:

- `MAX_FPS_CONTROL=false`
- `INGRESS_FPS_GATE_ENABLED=true`
- Recreated only `video-analytics-midterm-savant`.
- No image rebuild was required.
- No `down -v` or data operation was used.

## Before Window

Time window: 2026-06-14 15:46-15:49 UTC.

Container restart counts:

| Container | RestartCount delta | Notes |
|---|---:|---|
| `video-analytics-midterm-source-adapter` | 1102 -> 1103 | still restarting |
| `video-analytics-source-source_00000000-0000-4000-8000-781078565686` | 1476 -> 1477 | still restarting |
| `video-analytics-midterm-savant` | 0 | running / healthy |
| `video-analytics-midterm-replay-service` | 0 | running |

Savant metrics:

- `va_savant_sources_active = 2`
- `va_savant_effective_fps` over the 10s window:
  - `primary_rtsp`: about 4.0-4.1 fps
  - `source_00000000-0000-4000-8000-781078565686`: about 4.0-4.1 fps
- `va_savant_last_frame_age_seconds` stayed near zero.
- Queue pressure was visible:
  - `stage_queue_length{stage_name="source"}` around 104-201
  - `stage_queue_length{stage_name="decode"}` around 14-17
  - `stage_queue_length{stage_name="muxer"}` around 4-7
  - `stage_queue_length{stage_name="demuxer"}` around 37

Log evidence:

- Replay repeatedly logged `Resource temporarily unavailable` on the outbound
  ZeroMQ writer and `Send timeout, retrying sending ...`.
- Source adapter logs showed `WriterResultSendTimeout`, followed by
  `Internal data stream error (-5)` and adapter restart.

## After Window

Time window: 2026-06-14 16:01-16:04 UTC.

Container restart counts:

| Container | RestartCount delta | Notes |
|---|---:|---|
| `video-analytics-midterm-source-adapter` | 1107 -> 1107 | flat |
| `video-analytics-source-source_00000000-0000-4000-8000-781078565686` | 1482 -> 1482 | flat |
| `video-analytics-midterm-savant` | 0 | running / healthy |
| `video-analytics-midterm-replay-service` | 0 | running |

Savant metrics:

- `va_savant_sources_active = 2`
- `va_savant_effective_fps` over the 10s window:
  - `primary_rtsp`: 8.1 fps
  - `source_00000000-0000-4000-8000-781078565686`: 8.1 fps
- `va_savant_last_frame_age_seconds` stayed near zero:
  - `primary_rtsp`: 0.0s
  - `source_00000000-0000-4000-8000-781078565686`: about 0.06s
- Queue pressure dropped:
  - `stage_queue_length{stage_name="source"}` 0
  - `stage_queue_length{stage_name="muxer"}` 0
  - `stage_queue_length{stage_name="demuxer"}` 0
  - `stage_queue_length{stage_name="decode"}` about 2

Log evidence:

- Replay logs in the after window had no new `Resource temporarily unavailable`,
  `Send timeout`, `WriterResultSendTimeout`, or `Internal data stream error`.
- Fixed and dynamic source adapters logged normal `Processed 1000 frames`
  progress and no new send-timeout errors.

GPU probe:

- Host `nvidia-smi` was available during this run.
- Runtime hardware was 2x GeForce RTX 4090, not the target T4 production host.
- Snapshot showed GPU 0 at about 3% utilization and about 1222 MiB used.

## Decision

Keep Phase 0A.

Reason:

- Replay outbound send failures stopped during the measured after window.
- Fixed and dynamic source adapter restart deltas were flat during the measured
  after window.
- Savant stayed running/healthy with frame age near zero.
- Effective analysis FPS reached the intended `MAX_FPS=8/1` while
  `INGRESS_FPS_GATE_ENABLED=true` kept the project ingress FPS gate enabled.
- Queue pressure was materially lower after disabling nvstreammux
  `MAX_FPS_CONTROL`.

Committed config should therefore keep:

- `MAX_FPS_CONTROL=false`
- `INGRESS_FPS_GATE_ENABLED=true`

## Follow-Up

This does not complete all Phase 0 experiments. It keeps only Phase 0A. Continue
with 0B/0C/0D as reversible experiments, each with its own before/after
measurement and keep/revert decision.
