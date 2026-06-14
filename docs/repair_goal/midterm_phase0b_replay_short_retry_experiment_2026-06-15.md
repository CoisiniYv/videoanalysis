# Midterm Phase 0B Replay Short-Retry Experiment - 2026-06-15

Scope: `specs/16_dual_path_30x2_t4_production_optimization.md` Phase 0B.
This experiment shortened the Replay analysis `out_stream` retry window from
about 50 seconds to about 2 seconds:

- before: `send_timeout=5s`, `send_retries=10`
- after: `send_timeout=1s`, `send_retries=2`

The evidence Replay-job sink defaults were not changed.

## Preconditions

Committed prerequisites:

- `5b5debb Decouple Savant ingress FPS gate from muxer control`
- `decc4b6 Keep midterm max fps control experiment`

Live state before 0B:

- `MAX_FPS_CONTROL=false`
- `INGRESS_FPS_GATE_ENABLED=true`
- Fixed and dynamic source restart deltas were already flat after Phase 0A.

## Before Window

Time window: 2026-06-14 16:07-16:09 UTC.

Runtime overview:

| Container | RestartCount | Restart delta | Notes |
|---|---:|---:|---|
| `video-analytics-midterm-source-adapter` | 1107 | 0 | flat after 0A |
| `video-analytics-source-source_00000000-0000-4000-8000-781078565686` | 1482 | 0 | flat after 0A |
| `video-analytics-midterm-savant` | 0 | 0 | running / healthy |
| `video-analytics-midterm-replay-service` | 0 | 0 | running |

Savant metrics:

- `va_savant_sources_active = 2`
- `va_savant_effective_fps` over the 10s window:
  - `primary_rtsp`: 8.1 fps
  - `source_00000000-0000-4000-8000-781078565686`: 8.1 fps
- `va_savant_last_frame_age_seconds` stayed near zero.

Log evidence:

- Replay logs over the five-minute before window had no
  `Resource temporarily unavailable`, `Send timeout`, `WriterResultSendTimeout`,
  or `Internal data stream error`.

## Experiment

Changed `modules/savant_replay/config.midterm.json`:

- `out_stream.options.send_timeout` -> `{"secs": 1, "nanos": 0}`
- `out_stream.options.send_retries` -> `2`

Validation before applying live:

- `python3 -m json.tool modules/savant_replay/config.midterm.json`
- `pytest -q harness/tests/test_midterm_deployment_contract.py harness/tests/test_savant_perf_metrics_contract.py`
  - result: `24 passed`
- `docker compose -f infra/docker-compose.midterm.yml config`
  - result: valid config
- `git diff --check`
  - result: clean

Live apply:

- Restarted only `video-analytics-midterm-replay-service` with `docker restart`.
- No image rebuild was required.
- No `down -v` or data operation was used.

## After Window

Time window: 2026-06-14 16:09-16:12 UTC.

Runtime overview:

| Container | RestartCount | Restart delta | Notes |
|---|---:|---:|---|
| `video-analytics-midterm-source-adapter` | 1107 | 0 | flat |
| `video-analytics-source-source_00000000-0000-4000-8000-781078565686` | 1482 | 0 | flat |
| `video-analytics-midterm-savant` | 0 | 0 | running / healthy |
| `video-analytics-midterm-replay-service` | 0 | 0 | running after manual process restart |

Savant metrics:

- `va_savant_sources_active = 2`
- `va_savant_effective_fps` over the 10s window:
  - `primary_rtsp`: 8.1 fps
  - `source_00000000-0000-4000-8000-781078565686`: 8.0 fps
- `va_savant_last_frame_age_seconds` stayed near zero:
  - `primary_rtsp`: about 0.10s
  - `source_00000000-0000-4000-8000-781078565686`: 0.0s
- Queue pressure stayed low:
  - most stages at 0
  - `stage_queue_length{stage_name="decode"}` about 2

Log evidence:

- Replay continued receiving packets after the restart.
- Replay accepted new evidence jobs and logged successful cleanup for jobs after
  the restart.
- Replay logs in the after window had no new `Resource temporarily unavailable`,
  `Send timeout`, `WriterResultSendTimeout`, or `Internal data stream error`.
- Fixed and dynamic source adapters had no restart-count increase.
- `media-worker` and `video-file-sink` had no new errors in the same window.
- `clip-worker` continued to show existing Replay API fallback warnings and a
  cooldown final-fail. Those are evidence-job payload/cooldown behavior and not
  Replay analysis `out_stream` send failures.

## Decision

Keep Phase 0B.

Reason:

- The shorter Replay analysis retry window did not introduce source-adapter
  restarts in the measured after window.
- It did not break Replay ingestion or evidence job completion in the measured
  after window.
- It reduces the future worst-case Replay analysis `out_stream` blocking window
  from roughly 50 seconds to roughly 2 seconds if Savant stalls again.

Committed config should therefore keep:

- `out_stream.options.send_timeout = {"secs": 1, "nanos": 0}`
- `out_stream.options.send_retries = 2`

## Follow-Up

This keeps only Phase 0B. Continue with Phase 0C `SYNC_OUTPUT=false` as a
separate reversible experiment, keeping fixed and dynamic source creation paths
consistent if it is retained.
