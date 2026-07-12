# Clip / Media Phase 6 Capacity Instrumentation Checkpoint

Date: 2026-07-13

Scope: the instrumentation prerequisite for Spec 33 Phase 6, executed inside
Spec 34 Phase 4. This checkpoint does not claim capacity calibration, readiness
grace calibration, either required 60-source closure run, Spec 34 Phase 5
responsibility extraction, cross-worker soak, or legacy removal.

## 1. Result

The pressure harness can now run comparable `max_active=4/8/12` candidates
without silently changing lane topology. It records both the requested Compose
override and the observed container environment, while runtime logs supply the
effective executor and connection-pool bounds.

No Viewer/Operator file, model chain, threshold, FPS, batch size, 5+5 evidence
policy, frame UUID/PTS contract, evidence, trajectory, thumbnail, observation,
or retained Phase 5 canary was changed or deleted.

## 2. Corrected Comparison Contract

The prior pressure override derived
`ROLLING_CACHE_MATERIALIZATION_WORKERS` from source count, yielding up to 60
configured remux workers and an effective value clamped by `max_active`. That
made a 4/8/12 test change both the WIP bound and remux concurrency.

Phase 6 now fixes:

```text
MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT=4
MEDIA_WORKER_FFMPEG_X264_PRESET=ultrafast
MEDIA_WORKER_IMAGE_WORKERS=4
ROLLING_CACHE_MATERIALIZATION_WORKERS=1
MEDIA_WORKER_FINALIZER_WORKERS=32
MEDIA_WORKER_IMAGE_QUEUE_CAPACITY=4
MEDIA_WORKER_REMUX_QUEUE_CAPACITY=4
MEDIA_WORKER_FINALIZER_QUEUE_CAPACITY=4
MEDIA_WORKER_SCHEDULER_V2_ENABLED=true
MEDIA_WORKER_DB_POOL_ENABLED=true
MEDIA_WORKER_SEGMENT_INDEX_ENABLED=true
```

Only `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE` varies. The DB pool maximum is
derived from that same shared WIP bound and is verified from scheduler ticks.
Configured finalizer workers remain 32, but the long-lived resource root clamps
effective finalizer concurrency to `max_active` and the shared permit remains
the end-to-end authority.

The profile accepts the candidate through:

```text
MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=4|8|12
```

The normal pressure cleanup still removes temporary runtime sources/workers
only. The profile enables `--preserve-warmup-results`: visibility/prefill rows,
bundles, evidence directories, and trajectory observations are counted but not
deleted. Formal-window SQL uses an epoch event timestamp cutoff, with DB
creation time as the fallback for non-epoch timestamps, so retained warmup
results do not enter the 4/8/12 gates. The run does not use
`--clear-existing-evidence` or `--discard-pressure-results`; formal-window,
warmup, and playable postfill evidence remain visible.

Deterministic republish settings are explicit profile inputs. For a local file,
the harness hashes the input once and records path, SHA-256, size, and mtime in
`rtsp_republish_input_identity.json`; every source manifest row carries the same
hash and size. This prevents two nominally identical capacity runs from using
different fixture bytes.

## 3. Artifact Schema

Downstream observability schema v6 adds:

- image, remux, and finalizer lane depth distributions;
- oldest-ready age and shared WIP active/limit distributions;
- DB pool in-use, limit, peak, checkout count/wait, timeout, error, reset, and
  lost-connection distributions;
- Segment Index hit, miss, refresh, parse, stale, fallback, row-cache,
  generation, and read-pin distributions;
- effective max-active, CPU thread, lane-worker, queue-capacity, and source-cap
  values from the process startup log; and
- total DB index plus sidecar, bundle, artifact, timeline, overlay, and sidecar
  prune timings.

Every Media Worker pressure recreation writes
`compose_recreate_media_worker_rolling_cache.override.yml` beside its requested
and observed JSON. Restore and failure/interrupt paths use the same explicit
override mechanism, so a candidate cannot leak into daily runtime.

## 4. Verification

Executed checks:

```text
pressure harness focused tests:        173 passed
Spec 33 + scheduler/index regression:  341 passed
python py_compile:                      passed
profile shell syntax:                   passed
docker compose effective config:       passed
git diff --check:                       passed
```

The test fixture exercises numeric V2 scheduler ticks, effective resource
startup fields, DB-index subphase logs, the nested artifact mapping, fixed lane
configuration, CLI/profile propagation, warmup preservation/formal-window
fencing, deterministic republish hashing, and retained Compose override output.
It also executes the pressure runner directly from the repository root, so the
repository-owned lifecycle contract is importable without an ambient
`PYTHONPATH`.

The first live preflight exposed a stale pressure-camera upsert target:
Migration 012 defines the zone identity as unique `(camera_id, zone_id)`, while
the harness still targeted `(camera_id, zone_name)`. The upsert now uses the
published unique key and updates the display name as mutable metadata. A live
PostgreSQL transaction exercised provisioning and was rolled back before the
capacity run.

The next pre-sampling attempt exposed one query whose placeholder had been
converted to named `%(prefix)s` while its call still passed a tuple. The caller
now passes a mapping, and a whole-file AST contract rejects named placeholders
with sequence parameters or positional placeholders with mapping parameters.
A live two-source transaction then completed camera/zone/rule provisioning and
dual-shard source-plan generation before rolling back all DB changes.

The first 60-source visibility attempt showed that only the visibility-restart
path passed the configured 60000ms source FFmpeg timeout; initial source starts
silently used the controller's 20000ms default. Initial and restart paths now
use the same configured timeout. The contaminated warmup run was interrupted
and is not a capacity result.

That interruption also exposed a runtime epoch-barrier terminal contradiction:
the API marked an active task failed but retained `remux_running`, lease fields,
handoff/claim ownership, and an active Replay slot if present. Forced epoch
terminalization now atomically writes phase `terminal`, clears retry, lease,
handoff and claim ownership, releases/defences any active Replay slot, and
projects the terminal phase into the event payload. The retained failed event
and all visual observations remain; only stale ownership fields are repaired.
The formal-window DB summary, event/cooldown summary, non-materialized detail,
kept-evidence, covered-alias, lifecycle, and ready-to-claim SQL were also run
read-only against the live PostgreSQL schema.

## 5. Fixed-Input RTSP Ingress Preflight Correction

The first `max_active=4` attempt did not enter the 400 second sampling window:

```text
run_id: phase6_fixed60_8fps_5p5_maxactive4_20260712T191902Z
forwarder visible: 21/60
Savant visible:   21/60
source restarts:  999 total
```

This was not a Media Worker capacity result. Two independent ingress faults
were proven before changing the pressure profile:

1. `FFMPEG_TIMEOUT_MS=60000` controlled `ffmpeg_src.video_frame()` only. The
   pinned `ffmpeg_input 0.2.0` constructor retained an unexposed
   `init_timeout_ms=10000`, so simultaneous RTSP initialization entered a
   restart storm before the configured frame timeout applied.
2. The shared RTSP service at `192.168.1.105:8554` could report live publisher
   processes while individual paths were unreadable. A 20-path probe reached
   only 19/20 without targeted restart, and a 40-path probe reached only 23/40
   after one restart. PID liveness was therefore not a valid readiness gate.

The pressure path now:

- explicitly bind-mounts a narrow `sitecustomize.py` into pressure source
  adapters and passes `FFMPEG_INIT_TIMEOUT_MS=60000` to the pinned
  `FFMpegSource` constructor; normal 8090 camera adapters retain upstream
  behaviour;
- starts one run-scoped `bluenviron/mediamtx:1.11.3` container on the current
  Compose network gateway instead of sharing the external RTSP service;
- requires every republished path to pass bounded `ffprobe` readability before
  starting adapters, with one targeted publisher restart and fail-closed
  cleanup;
- repeats H.264 SPS/PPS at each keyframe for the fixed MP4 copy fixture. Without
  this, local MediaMTX exposed codec parameters in SDP but Savant stopped at
  `PREROLLING`; with Annex-B `h264_mp4toannexb,dump_extra=freq=keyframe`, the
  parser produces profile caps and enters `PLAYING`; and
- captures adapter logs before disabling cameras can remove their containers.

Retained preflight evidence:

| Artifact | Result |
|---|---:|
| `phase6_ingress_publish20_20260713T0350CST` | external RTSP 19/20, one path unreadable |
| `phase6_ingress_publish40_retry1_20260713T0407CST` | external RTSP 23/40 after 17 targeted restarts |
| `phase6_ingress_localmtx40_20260713T0412CST` | local MediaMTX 40/40, zero restart |
| `phase6_ingress_localmtx60_20260713T0414CST` | local MediaMTX 60/60, zero restart |
| `phase6_ingress_managedmtx60_20260713T0423CST` | harness-managed MediaMTX 60/60, zero restart, server removed |
| `phase6_h264_repeat_headers_20260713T0443CST` | one-source parser reached `PLAYING` |
| `phase6_ingress_savant20_headers_20260713T0450CST` | adapters/forwarder/Savant 20/20, zero restart |

The last 20-source full-exporter smoke failed only its separate steady-FPS
gate (`~5.0fps` versus fixed `7.92fps`). That run proves ingress closure but is
not a capacity pass. The model chain, model intervals, FPS and acceptance
thresholds remain unchanged because they are outside this Goal's change scope.

## 6. Remaining Phase 6 Work

Run the fixed-input 60-source workload at `max_active=4`, `8`, and `12`, select
the lowest candidate satisfying correctness, structural, CPU, connection, and
latency gates, then repeat the selected candidate. Grace remains 9 seconds
through both accepted scheduler runs. Only then may explicit segment coverage
readiness and a shorter measured grace be considered.
