# Clip / Media Phase 2 Single-Finalizer Boundary

Date: 2026-07-12

Scope: Spec 33 Phase 2 only, implemented as an early substep of Spec 34 Phase 4.
This checkpoint does not claim Spec 34 Phase 2 Clip planner parity or Spec 33
Phase 3 and later scheduler tokens.

## 1. Result

The Media finalizer now has one explicit `_finalize_one()` job boundary. A
shared materialization permit is acquired before claim and released in the
outermost job `finally` only after the terminal CAS, event projection, bundle
index attempt and bounded sink-cleanup attempt have returned.

Acceptance token:

```text
PASS_MEDIA_WORKER_SINGLE_FINALIZER_BOUNDARY
```

No `services/evidence-viewer` file was modified. Savant, model batches,
thresholds, FPS, 5+5 policy, UUID/PTS and 8090 product contracts were not
changed. All filesystem canaries used disposable temporary directories; no
existing evidence, trajectory, thumbnail or media file was deleted.

## 2. Ownership And Runner Boundary

`_HeldMaterializationPermit` is move-only by ownership convention and has an
idempotent release. Both the synchronous path and the temporary finalizer pool
use the process-wide `_MaterializationGuard`; a pool job no longer creates
`_MaterializationGuard(1)` or recursively invokes `_process_sink_output()`.

The boundary explicitly handles:

- terminal claim: mark the sink directory processed and release the permit;
- missing task: perform only the bounded orphan cleanup, then release;
- busy/claim error: leave the task recoverable and release;
- build/guard failure: persist the fenced terminal failure before release;
- stale publish or terminal CAS: stop before event enrichment, DB index and
  terminal sink cleanup;
- success: hold the permit through terminal state, index and cleanup audit.

`MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=0` remains disabled semantics: no
finalizer claim occurs. It is not interpreted as unlimited capacity.

## 3. Fenced Artifact Publication

Each finalizer attempt builds under:

```text
{evidence_root}/.incoming/{event_id}/{lease_generation}-{lease_token}
```

Immediately before publication, the worker heartbeats the exact
owner/token/generation fence. Fence loss removes only that attempt directory;
it cannot touch a canonical winner. A winning attempt uses atomic rename into
`{evidence_root}/{event_id}`. An existing complete canonical bundle is
converged without overwrite; an incomplete predecessor is moved to quarantine,
not deleted.

Published in-memory paths and retained JSON sidecars are rebased to canonical
paths before DB writes. The disposable integration canary checks
`evidence_tasks`, `events`, `evidence_bundles`, `evidence_artifacts`,
`evidence_frame_timeline` and `evidence_overlay_segments` and proves no
`/.incoming/` path is persisted.

## 4. Durable Recovery And Cleanup

`finalizer_pending` recovery now reconstructs metadata only when all of the
following match the durable handoff:

- non-empty attempt token;
- exact source ID and runtime epoch;
- exact sink/staging path;
- canonical file remains below that sink directory;
- size and mtime-ns match, plus device/inode when present.

Invalid or cross-epoch handoffs are terminalized through the unclaimed fenced
repository path but their source artifact is retained for diagnosis.

A cleanup I/O failure records `cleanup_pending` in `evidence_tasks.cleanup_audit`
and the event media projection. A later poll retries it idempotently. The task
remains materialized throughout; cleanup cannot reverse terminal state.

## 5. Rollback Contract

`MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED=true` is tracked in the Compose and
environment contracts. With multiple configured finalizer workers, setting it
to `false` now genuinely bypasses the V2 pool and selects the legacy
synchronous runner. Fenced lifecycle repositories and durable recovery remain
common and are not rolled back. This temporary flag and old runner are removed
after the required soak gates in Phase 7.

## 6. Verification

Executed checks:

```text
Finalizer boundary tests:                   22 passed
Spec 34 Clip + Media minimum gate:         345 passed
Fresh PostgreSQL 001-030 combined gate:     28 passed
Deployment contract:                        28 passed
compileall (Clip, Media and new tests):      passed
docker compose config --quiet:              passed
effective finalizer V2 flag:                true
git diff --check:                            passed
```

The fresh database was created on the isolated local PostgreSQL test instance,
migrated through every tracked migration from 001 to 030, used for repository,
boundary and full finalizer/index/cleanup integration tests, and dropped by a
shell trap. The media canary used only temporary sink/evidence roots.

The local `media-worker` was then recreated with `--no-build
--force-recreate --no-deps`. It started on the existing image with restart
count zero and logged `single_finalizer_boundary_v2=True`, `max_active=4`,
`finalizer_workers=32` and repeated idle ticks with `permit_active=0`. Runtime
DB counts remained `events=0`, `evidence_tasks=0`, `active=0` and
`cleanup_pending=0`; Redis returned `PONG`; Viewer health and the 8090
DB-backed evidence health/list endpoints returned HTTP 200 with
`index_source=database`. No startup traceback or worker-loop error was found.
Compose reported the known AdaFace orphan containers; they were deliberately
left untouched and `--remove-orphans` was not used.

## 7. Explicit Remaining Work

Phase 2 intentionally keeps synchronous batch scheduling and the per-batch
`ThreadPoolExecutor`. Spec 33 Phase 3 still owns the lifetime `WorkBudget`,
bounded PostgreSQL pool, long-lived executors/source locks, periodic lease
heartbeat, maximum attempt age and shutdown state machine.

Spec 34 Phase 2 Clip typed contracts/pure planner parity remains the next
uncompleted primary-plan phase. Three-lane non-blocking scheduling, segment
indexing, crash/restart injection, two accepted 60-source gates and legacy
removal also remain pending.
