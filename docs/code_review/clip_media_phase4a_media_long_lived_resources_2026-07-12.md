# Clip / Media Phase 4A Media Long-Lived Resources

Date: 2026-07-12

Scope: Spec 33 Phase 3 only, implemented as the long-lived-resource substep of
Spec 34 Phase 4. This checkpoint does not claim the non-blocking Scheduler V2,
the incremental segment index, capacity calibration, pressure closure, or
legacy scheduler removal.

## 1. Result

Media Worker now owns one process-lifetime resource composition root containing
a shared `WorkBudget`, bounded image/remux/finalizer executor lanes,
process-lifetime source caps, an optional bounded PostgreSQL connection pool,
lease-heartbeat supervision, a managed subprocess registry, and an explicit
shutdown controller.

Acceptance token:

```text
PASS_MEDIA_WORKER_LONG_LIVED_RESOURCE_BOUNDS
```

The Phase 3 change set does not include any `services/evidence-viewer` file.
Savant, model batches, thresholds, FPS, 5+5 policy, UUID/PTS/session contracts,
Evidence rolling-cache layout, and the 8090 DB-backed annotation contract were
not changed. No existing evidence, trajectory, thumbnail, or media file was
deleted. The retained canaries `.458` through `.461` remain available.

## 2. Lifetime Resource Contract

`MaterializationResources` creates at most one lifetime instance of each
resource and clamps every executor's effective worker count to the shared WIP
limit. `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=0` preserves disabled semantics:
it creates no executor or PostgreSQL pool and does not claim materialization
work; lifecycle recovery may still run.

The `WorkBudget` permit is move-only by ownership convention. A remux permit can
move to finalization without temporarily returning capacity to another job.
Image admission reserves one slot for non-image work by default. Executor and
source reservations are bounded and acquired before a remux claim; reservation
or submit failure returns all in-memory capacity.

Phase 3 keeps the legacy scheduler poll order. The long-lived lanes are now the
resource substrate for Phase 4, but image/snapshot/annotation dispatch and
non-blocking finalizer handoff are not claimed at this checkpoint.

## 3. PostgreSQL Pool And Lease Safety

The new `psycopg_pool.ConnectionPool` is optional and bounded by the effective
materialization limit. A scoped connection proxy checks out a real connection
only for a cursor or transaction and returns it immediately afterward, so
ffmpeg, ffprobe, sidecar construction, filesystem publication, and cleanup do
not retain a checkout.

The real PostgreSQL concurrency probe submitted 12 concurrent database jobs to
a pool capped at four. It observed four unique backend PIDs, `peak_in_use=4`,
zero checkout timeout/error, zero lost connection, and `in_use=0` both before
and after pool shutdown.

The heartbeat supervisor periodically renews the exact owner/token/generation
lease. Fence loss or maximum attempt age prevents stale finalizers from
publishing or committing. The retained `.461` canary performed five real
heartbeats with zero lost, expired, or error outcome.

## 4. Managed Subprocess And Shutdown Contract

All Media Worker ffmpeg/ffprobe call sites in the affected modules now run
through one managed subprocess registry. Child processes start in trackable
process groups. Forced shutdown sends TERM and then KILL after the configured
bounded timeout.

The state sequence is:

```text
running -> quiescing -> draining -> stopping -> stopped
```

The first signal closes admission and permits draining for
`MEDIA_WORKER_SHUTDOWN_GRACE_S` (default 45 seconds). A second signal or grace
expiry selects the forced path. Compose uses `stop_grace_period: 70s`, leaving
time for database convergence, subprocess termination, executor closure, and
pool closure. The retained container SIGTERM probe exited normally with code 0
in 979 ms and restart count zero.

## 5. Dependency And Rollout Contract

The existing `requirements.txt` remains unchanged. `psycopg-pool` is isolated
in `requirements.pool.txt` and installed as a separate Docker layer. This phase
therefore required a Media Worker image rebuild. The verified image is:

```text
sha256:5b451db0c4859a01471d2d05a183b3cc81f6109712e12592e42535162afa5c84
psycopg-pool=3.3.1
```

After the canary, daily runtime was restored with Scheduler V2 disabled, DB
pool disabled, segment index disabled, heartbeat interval 10 seconds, and
`max_active=4`. Media Worker was running with restart count zero and no active
task, lease, Replay slot, lane permit, or pool checkout. Known AdaFace orphan
containers were deliberately left untouched.

## 6. Retained Runtime Canary

Canary event `00000000-0000-4000-8000-000000000461` materialized one retained
H.264 video bundle:

```text
raw clip duration:                  10.25 s
decoded video frames:               82
DB timeline rows:                   83
DB overlay rows:                    82
displayable person rows/objects:     6 / 6
bundle/artifact rows:                1 / 1
8090 detail:                         HTTP 200
8090 DB annotations:                 HTTP 200
raw clip Range request:              HTTP 206, 1024 bytes
final active task/lease/Replay slot: 0 / 0 / 0
final shared permit/pool checkout:   0 / 0
```

The successful bundle retained only `raw_clip.mov`; generated sidecars were
indexed into PostgreSQL and pruned under the existing sidecar-retention policy.
The 8090 detail reported `index_source=database`, and the annotation response
reported `annotation_source=database`, `fallback_used=false`, with six
displayable person bboxes. This validates the existing DB-backed display
contract without changing the Viewer.

## 7. Verification

Executed checks:

```text
Spec 33 Phase 3 targeted gate:       280 passed
Clip/Media regression group:        173 passed, 7 skipped
compileall:                          passed
docker compose config:               passed
git diff --check:                    passed
real PostgreSQL pool probe:          passed
retained Media/8090 canary:          passed
container SIGTERM probe:             passed
```

Auditable artifact:

```text
/data/video-analytics/artifacts/clip_media_phase4a_media_resources_20260712T150003Z
```

The artifact intentionally retains two unsuccessful diagnostic requests before
their corrected probes: the Viewer list probe first used an unsupported
`/api/v1/evidence?limit=5` path before `/api/bundles?limit=5` returned HTTP 200,
and the Redis group probe first used the wrong group identifier before the
correct group showed pending zero. These are probe-history records, not product
failures and not reasons to modify the Viewer.

## 8. Explicit Remaining Work

Spec 33 Phase 4 must still make the scheduler poll non-blocking: split candidate
discovery and admission, dispatch image/snapshot/annotation work to the image
lane, hand remux results durably to the finalizer lane without batch waiting,
reserve lane/source/permit capacity before claim in every path, and make submit
failure/restart recovery durable. Phase 5 still owns the source/epoch segment
index and duplicate probe/scan reduction. Capacity A/B, two accepted 60-source
pressure gates, and legacy scheduler removal remain unclaimed.
