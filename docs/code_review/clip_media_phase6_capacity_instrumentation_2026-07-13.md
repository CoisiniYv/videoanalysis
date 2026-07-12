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
only. It does not use `--clear-existing-evidence` or
`--discard-pressure-results`; formal-window evidence and trajectories remain
visible.

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
pressure harness focused tests:        145 passed
Spec 33 + scheduler/index regression:  335 passed
python py_compile:                      passed
profile shell syntax:                   passed
docker compose effective config:       passed
git diff --check:                       passed
```

The test fixture exercises numeric V2 scheduler ticks, effective resource
startup fields, DB-index subphase logs, the nested artifact mapping, fixed lane
configuration, CLI/profile propagation, and retained Compose override output.

## 5. Remaining Phase 6 Work

Run the fixed-input 60-source workload at `max_active=4`, `8`, and `12`, select
the lowest candidate satisfying correctness, structural, CPU, connection, and
latency gates, then repeat the selected candidate. Grace remains 9 seconds
through both accepted scheduler runs. Only then may explicit segment coverage
readiness and a shorter measured grace be considered.
