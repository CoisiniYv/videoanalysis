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
  Successful runs also write `source_containers_before_stop.json`, preserving
  the pre-cleanup running/restart snapshot instead of reporting only the
  expected post-cleanup zero-container state.

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
| `phase6_ingress_pose60_b650952_20260713T0500CST` | adapters/forwarder/Savant 60/60, zero visibility restart |

The 20-source full-exporter and 60-source pose-only smokes failed only their
separate steady-FPS gates (both about `5.0fps` versus fixed `7.92fps`). These
runs prove ingress closure but are not capacity passes. The model chain, model
intervals, FPS and acceptance thresholds remain unchanged because they are
outside this Goal's change scope.

## 6. Fixed-Input `max_active=4` Result

The corrected ingress path completed the first comparable capacity candidate:

```text
run_id: phase6_fixed60_8fps_5p5_maxactive4_0312216_20260713T0520CST
artifact: /data/video-analytics/artifacts/phase6_fixed60_8fps_5p5_maxactive4_0312216_20260713T0520CST
visibility: 60/60, zero source restart
formal bounded evidence tasks: 548
retained evidence tasks (including playable postfill): 551
playable retained bundles: 195/195
materialization expired: 356
WIP active p50/p95/max: 1/3/3 (limit 4)
remux depth p50/p95/max: 1/1/1
oldest ready p50/p95/max: 224s/286s/304s
```

This candidate fails Phase 6. Its retained 195 watchlist image bundles (192 in
the bounded sampling window and three playable postfill results) are all
playable and visible through the 8090 DB-backed path, but no intrusion video
completed before 300-second rolling retention expired. Redis lag/pending and
DB pool timeout/error/lost-connection counts were zero. The measured topology
therefore did not saturate shared WIP; the fixed single remux lane plus coverage
retry service rate remained below event arrival rate. `max_active=4` cannot be
selected from this result.

Two harness-only reporting defects were found while auditing the artifact:

- rolling-cache raw FPS was probed from the unrelated default `rtsp_uri`
  (23.976fps), not the fixed republish input (8fps), although 1,620 measured
  segments had p50 8.085fps across 60/60 sources; and
- the observed-window summary had only a lower sampling fence, so retained
  playable postfill events extended the measured span to about 548 seconds.

The harness now probes `rtsp_republish_input_uri` and applies both sampling
start and sampling-end event-time fences (with DB creation-time fallback) to
the post-cleanup formal ingest/cooldown summary. This preserves postfill
evidence and trajectories for operator use while excluding them from formal
window gates. A read-only live recomputation returned 1,534 total rows, 548 new
events/tasks, and a 407.183-second event-time span instead of the contaminated
548-second span. Neither correction changes the real 356-expiry failure.

## 7. Fixed-Input Capacity Matrix Result

The fixed-input 4/8/12 matrix has no selectable `max_active` candidate:

| max_active | formal new events | intrusion | watchlist | retained bundles | video bundles | WIP p95/max | remux p95/max | oldest-ready p95 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 548 | 356 | 192 | 195 | 0 | 3/3 | 1/1 | 285.9s |
| 8 | 430 | 347 | 83 | 87 | 0 | 1/7 | 1/1 | 283.2s |
| 12 | 564 | 353 | 211 | 214 | 0 | 2/7 | 1/1 | 285.8s |

The comparable `max_active=8` artifact is:

```text
/data/video-analytics/artifacts/phase6_fixed60_8fps_5p5_maxactive8_fair_fb02531_20260713T0558CST
```

The `max_active=12` artifact is:

```text
/data/video-analytics/artifacts/phase6_fixed60_8fps_5p5_maxactive12_loop_fb02531_20260713T0538CST
```

Every candidate had 60/60 source visibility, zero source restart, full-rate
rolling segments, no DB pool timeout/error/lost connection, DB-backed retained
image visibility, and zero video bundle. All failed on materialization expiry,
steady FPS and nonzero forwarder queue. WIP never reached either 8 or 12 while
the remux lane remained exactly one and oldest-ready converged near rolling
retention. Increasing shared WIP therefore does not address video service rate.

One discarded `max_active=8` attempt omitted the fixture loop flag and was
interrupted after the 520-second input ended. Another informative 8 run began
with face consumer lag inherited from that interruption. Neither is used for
the table. The fair 8 run and the 12 run both started with face lag zero. Event
mix still varied because publishers begin playback before runtime setup, so a
future deterministic regression decision must also freeze sampling-to-fixture
phase, not merely fixture bytes.

The harness now exposes `--media-worker-rolling-remux-workers`, defaulting to
one. This does not revise the completed max-active matrix. A non-default value
is explicitly a separate remux-lane experiment and is recorded/restored with
the same Compose override contract.

## 8. Remaining Phase 6 Work

Run a separately labeled bounded remux-lane canary with the fixed workload and
prove that configured/effective workers, WIP, CPU, leases and DB pool remain
bounded. If it produces videos, run the full gate twice with identical fixture
phase. If it does not, investigate coverage retry/segment selection before any
further concurrency increase. Grace remains 9 seconds until two accepted runs.
