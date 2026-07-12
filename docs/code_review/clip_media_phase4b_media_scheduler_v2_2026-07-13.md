# Clip / Media Phase 4B Media Scheduler V2

Date: 2026-07-13

Scope: Spec 33 Phase 4 only, implemented as the non-blocking scheduler substep
of Spec 34 Phase 4. This checkpoint does not claim the source/epoch
`RollingSegmentIndex`, finalizer scan/probe reduction, capacity calibration,
two accepted 60-source pressure runs, the cross-worker soak, or legacy
scheduler removal.

## 1. Result

Media Worker now runs one short, non-blocking scheduler tick over three
process-lifetime bounded lanes: image, rolling remux, and finalizer. The poll
thread drains completed futures without waiting, performs metadata-only
discovery, reserves lane/source/permit capacity before a DB claim, dispatches
work, and emits lane/WIP/pool metrics.

Acceptance token:

```text
PASS_MEDIA_WORKER_NONBLOCKING_THREE_LANE_SCHEDULER
```

No `services/evidence-viewer` file is part of this phase. Savant model chains,
batch sizes, thresholds, FPS, 5+5 policy, frame UUID/PTS/session contracts,
evidence layout, and the existing 8090 DB-backed annotation contract were not
changed. No evidence, trajectory, thumbnail, canary, Redis pending entry, or
media file was deleted.

## 2. Scheduler And Ownership Contract

The scheduler admission order is finalizer, rolling video/remux, then image.
`_FinalizerSchedulerV2`, `_RollingCacheMaterializationRunner`, and
`_ImageSchedulerV2` share the Phase 3 lifetime `WorkBudget`, bounded executor
lanes, source caps, PostgreSQL pool, lease heartbeat supervisor, subprocess
registry, and shutdown controller.

Every lane reserves its bounded executor slot and source slot before acquiring
or transferring a shared permit. A rolling permit moves from remux to an
accepted finalizer item without reopening WIP capacity. Discovery, sort,
reservation, submit, retry persistence, lane execution, and forced-shutdown
exceptions all release their exact lane/source/permit ownership.

The main tick does not call ffmpeg/ffprobe, extract images, build sidecars,
wait on a future, or synchronously drain a finalizer batch. A retained mixed
canary observed image, remux, and finalizer lane depths `1/1/1` with
`permit_active=3`, followed by a fully idle `0/0/0` and permit zero state.

## 3. Durable Handoff And Failure Convergence

Remux persists an immutable file-identity handoff and
`materialization_phase=finalizer_pending` before finalizer admission. The
finalizer acquires a fenced owner/token/generation lease. A stale owner cannot
publish, commit, prune sidecars, or clean the winning sink output.

Lifecycle recovery is intentionally independent from rolling admission. The
main loop advances a dedicated recovery cadence before either Scheduler V2 or
legacy admission; it scans all rolling-owned lifecycle rows, rather than only
the sources currently selected by `ROLLING_CACHE_SOURCES`. Consequently a
durable handoff created by an earlier rolling runtime remains recoverable after
`ROLLING_CACHE_ENABLED` and `ROLLING_CACHE_MATERIALIZATION_ENABLED` are turned
off. Database lease expiry and generation fencing still prevent recovery from
stealing a live lease.

Fault-injection coverage includes:

- ordinary claimed-finalizer exceptions and terminal-write failure;
- finalizer discovery, sort, lane submit, and lane-job failure;
- remux/image submit failure and retry-write failure;
- queued and running finalizer force-stop paths;
- remux-to-finalizer transferred-permit failures; and
- force requests racing immediately before and after finalizer claim.

Every retry-only path normalizes unknown text to a retryable infrastructure
reason. A failed terminal CAS no longer reports `terminal_committed=true`.
Queued/running work is fenced into durable retry before its in-memory permit is
released whenever the process is still able to execute shutdown handling.

## 4. SIGKILL Restart/Recovery Canary

The retained `.490` through `.497` group was created without deleting or
reusing earlier canaries. The worker was paused and sent `SIGKILL` precisely
after `.490` through `.493` had durable `finalizer_pending` handoffs and before
they had any bundle rows. The container exited with code 137 and restarted from
the same image and source mount.

The valid old leases were not stolen. After their configured 120-second lease
expired, the single rolling recovery pass cleared the stale owner/token,
admitted the immutable handoffs to the finalizer lane, and completed them.
Lease generation proves the boundary:

```text
.490-.493: generation 2, recovered after SIGKILL
.494-.497: generation 1, admitted after restart
```

All eight tasks converged to `materialized/terminal`. Each has exactly one
bundle, one raw-clip artifact, 82 DB timeline rows, six displayable overlay
segments, no active lease/token, and no Replay slot. There were zero duplicate
bundles and zero terminal contradictions.

### 4.1 Rolling-off retained recovery canary

An additional retained `.500` canary closed a flag-coupling gap found after the
initial Phase 4B checkpoint. It started as an immutable `finalizer_pending`
handoff with generation 1 and a valid 10-second lease while the daily worker
had Scheduler V2 and the DB pool enabled, but both rolling admission flags
disabled. A snapshot 9.54 seconds before lease expiry still showed the original
owner/token, generation 1, and zero bundles, proving that the new worker did not
steal the valid lease.

After expiry, the dedicated lifecycle pass logged
`handoff_recovered=1 rolling_admission_enabled=False`; finalizer admission then
converged the row to generation 2 and `materialized/terminal`. The result has
exactly one bundle, one raw-clip artifact, 82 timeline rows, six DB-backed
displayable `person_context` overlays, and no lease, Replay slot, lane, permit,
pool checkout, or Redis residual. 8090 returned DB-backed detail/annotations,
`fallback_used=false`, and HTTP 206 for a 1024-byte MOV Range request. The
canary and every pre/post snapshot remain retained; no prior evidence was
removed or reused.

## 5. Retained Functional Canaries And 8090

The retained functional set remains visible:

```text
.470 rolling video: verified, 82 timeline rows, 6 displayable bboxes
.471 rolling image: 3 image artifacts
.472 strict-epoch negative: rejected and retained as expected
.473 general Replay video: 82 timeline rows, 6 displayable bboxes
.474 snapshot + annotation: both image-lane outputs ready
```

The `.473` video remains `generated_unverified` because the existing runtime
has `POST_SAVANT_FAST_RAW_CLIP_ENABLED=true`; this is the pre-existing fast-copy
policy, not a Scheduler V2 failure.

8090 returned a DB-backed health response. `.470` and `.473` detail and
annotation endpoints returned HTTP 200, six displayable `person_context` bbox
records each, and `fallback_used=false`. Both MOV requests returned HTTP 206
for a 1024-byte Range. The three `.471` image artifacts and both `.474`
snapshot images returned JPEG HTTP 200.

## 6. Verification And Runtime Restoration

Executed checks:

```text
Phase 4 scheduler/regression gate:  288 passed, 7 skipped
focused fault-injection gate:       43 passed
rolling-off recovery regression:    183 passed, 7 skipped
compileall / py_compile:            passed
docker compose config:              passed
git diff --check:                    passed
mixed three-lane retained canary:   passed
SIGKILL finalizer recovery canary:  passed, 8/8 unique bundles
rolling-off finalizer recovery:     passed, generation 1 -> 2
8090 DB/media audit:                passed
global DB/Redis/runtime residuals:  zero
```

Final residual audit reported zero global active Media leases,
`finalizer_pending` rows, active Replay slots, Phase 4B Redis keys, image/remux/
finalizer lane depth, shared permits, heartbeat registrations, and DB-pool
checkouts.

The temporary canary worker was removed after an idle SIGTERM shutdown with
exit code 0. Daily `media-worker` was recreated with `--no-build`, using the
same image and bind-mounted source. Its effective state is Scheduler V2 on, DB
pool on, segment index off, rolling cache/materialization off, and all runtime
depths zero. Existing AdaFace orphan containers were deliberately untouched.

Auditable artifact:

```text
/data/video-analytics/artifacts/clip_media_phase4b_scheduler_v2_closure_20260712T162955Z
/data/video-analytics/artifacts/clip_media_phase4b_flag_independent_recovery_20260712T171945Z
```

Frozen source hashes are recorded in the artifact and match the source used by
the canaries.

## 7. Explicit Remaining Work

Spec 33 Phase 5 must implement the source/epoch `RollingSegmentIndex`, bounded
parsed-row caching and invalidation, unified rolling maintenance ownership, and
identity-safe probe reuse. Phase 6 still owns comparable 4/8/12 capacity A/B
and readiness tuning. Two accepted 60-source runs, the full cross-worker
restart soak, responsibility extraction, and legacy scheduler/flag deletion
remain unclaimed. Consequently Spec 34 Phase 4 as a whole is not complete.
