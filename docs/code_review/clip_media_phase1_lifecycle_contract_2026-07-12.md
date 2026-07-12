# Clip / Media Worker Phase 1 Lifecycle Contract

Date: 2026-07-12

Scope: Spec 34 Phase 1 and Spec 33 Phase 1 only. This checkpoint unifies state,
ownership, ready-time, lease, recovery and Redis ACK contracts. It does not
claim the Phase 2 finalizer boundary or any later scheduler/pressure token.

## 1. Result

Phase 1 is complete against the local code, PostgreSQL schema and idle runtime.

The materialization authority is now `evidence_tasks`. Compatibility fields in
`events` are task-first projections. The shared dependency-free contract in
`libs/evidence_lifecycle` defines canonical status, phase, active/terminal sets,
reason classification and retry delay for workers, API and operator scripts.

Acceptance tokens:

```text
PASS_EVIDENCE_MATERIALIZATION_STATE_CONTRACT_UNIFIED
PASS_CLIP_MEDIA_LIFECYCLE_OWNERSHIP_UNIFIED
```

No `services/evidence-viewer` file was modified. No evidence, trajectory,
thumbnail or media file was deleted.

## 2. State And Ownership Contract

Canonical claimable states are:

```text
manifest_ready
materialization_pending
```

`materialization_deferred` is terminal and is absent from rolling candidates,
active backlog/drain sets and expiry sweeps. Retryable coverage, capacity,
cooldown, proof, Replay and temporary dependency outcomes use pending plus
`materialization_next_attempt_at`; they do not move
`materialization_ready_at`.

Ownership is explicit:

- event-worker writes the original ready time and initial owner;
- clip-worker only transitions Replay-owned work and only expires Replay
  phases;
- media-worker owns rolling deadline/lease recovery;
- task terminal CAS precedes event projection;
- a stale owner cannot heartbeat, retry, fail, hand off or terminally commit;
- terminal deferred cannot be reclaimed by rolling or finalizer paths.

The deadline/lease decision table now preserves a valid running lease past the
business deadline, recovers an expired lease with an immutable handoff, retries
an expired lease without an artifact only while the business deadline remains,
and expires unrecoverable overdue work.

## 3. Clip Redis ACK Contract

Clip status writes are task-first CAS followed by event projection.
`update_clip_status()` returns success only when the durable outcome is
projected. ACK is withheld when persistence fails.

Capacity, proof wait and Replay transport/empty-response failures remain
pending for bounded retry and are not ACKed on the first transient failure.
Permanent invalid input, verified duplicate terminal targets and persisted
terminal outcomes may ACK. Replay job success ACKs only after both the job state
and completion-aware slot handoff are durable.

The immutable Phase 0 manifest still records 16 historical direct `xack` call
sites. Phase 1 source has fewer scattered sites and tests protect the
persist-before-ACK behavior without rewriting that historical baseline.

## 4. Media Repository And Finalizer Fence

`services/media-worker/app/materialization_repository.py` owns named rolling
claim, heartbeat, retry, failure, handoff, finalizer claim, completion and
deadline/lease recovery transitions. Schema capability is cached per database
connection so mixed-order code/schema deployment remains valid.

Finalizer completion uses one writable PostgreSQL CTE: the fenced task terminal
CAS and basic event projection either both happen or neither happens. Rich
bundle metadata, DB index and cleanup are reached only after that transition
wins. This closes the Phase 1 event-before-task stale-owner write window.

The permanent-invalid-sink branch also initializes an explicit optional lease,
so invalid media before claim converges through the unclaimed terminal path
instead of raising `UnboundLocalError`.

## 5. Migration Proof

The disposable fresh database passed migrations 001 through 030 and six real
PostgreSQL repository tests. Those tests cover:

- fenced claim/retry and immutable ready time;
- stale owner heartbeat/retry/failure rejection;
- durable handoff, finalizer retry and reclaim;
- deadline versus lease recovery decisions;
- terminal deferred claim exclusion;
- task-first finalizer completion and Clip Replay ownership.

An independent 028 upgrade fixture covered:

| Legacy row | Result after 029 |
| --- | --- |
| empty deferred reason + rolling audit | pending / waiting_coverage / rolling |
| empty deferred reason without rolling evidence | pending / waiting_ready / replay |
| coverage reason | pending / waiting_coverage / rolling |
| unknown reason-filled deferred | terminal deferred |
| ambiguous active identity | failed / manual_quarantine / terminal |

Migration 029 and non-transactional Migration 030 were each rerun. The semantic
row snapshot was unchanged and illegal active rows remained zero.

Before applying the migrations to the local runtime database, a custom-format
backup was saved as:

```text
/data/video-analytics/artifacts/
  phase1_pre_migration029_20260712T110435Z.dump
sha256=413ff01faed06b4c472024d049014aaab6f56765ada5f2486c5daf2e7be61ecc
```

Post-apply verification:

```text
lifecycle columns = 11
lifecycle hot-path indexes = 6
illegal active rows = 0
events = 0
evidence_tasks = 0
```

## 6. Verification

Executed checks include:

```text
Clip contract group:                         53 passed
Media legacy/new-state group:               77 passed
Combined Phase 0/1 group:                  130 passed, 5 skipped
Fresh DB repository contract:                6 passed
Event/API/runtime/storage/pressure/drain:   258 passed
Expanded Clip/Media/index group:           146 passed, 6 skipped
Duration-guard plus Media state group:       84 passed, 6 skipped
Fresh-DB combined Phase 1 gate:             411 passed
compileall:                                  passed
docker compose config -q:                    passed
deployment contract:                        28 passed
git diff --check:                            passed
```

The skipped repository tests require a disposable Migration-029 database and
were run separately as the six passing fresh-DB tests above.

Affected services were recreated with:

```text
docker compose --env-file infra/env/midterm.env \
  -f infra/docker-compose.midterm.yml up -d \
  --no-build --force-recreate --no-deps \
  event-worker clip-worker media-worker api
```

Observed after recreate:

- all four services were running with restart count zero;
- event/clip/media imported `/app/libs/evidence_lifecycle` from the tracked
  bind mount;
- Clip started eight consumers; Event connected to Redis/PostgreSQL; Media
  emitted regular idle scheduler ticks;
- Redis event and record-request consumer-group pending counts were zero;
- active and terminal-deferred evidence task counts were zero;
- API `/health`, Viewer `/health`, and the 8090 DB-backed bundle endpoint
  returned HTTP 200;
- no startup traceback or lifecycle error was observed.

## 7. Explicit Remaining Work

Phase 1 does not claim `PASS_MEDIA_WORKER_SINGLE_FINALIZER_BOUNDARY`.

The current legacy finalizer still builds directly in the canonical event
directory, releases the legacy permit before DB-index/cleanup follow-on, and
uses a per-batch finalizer executor. Spec 33 Phase 2 must introduce one
attempt-scoped finalizer job boundary, fence canonical publish, keep the permit
through terminal commit and bounded cleanup, and make cleanup failure a durable
retryable outcome. Lease heartbeats and long-lived bounded resources remain
Spec 33 Phase 3 work.

Coordinator V2, three-lane scheduling, connection pool, segment index,
crash/restart soak, two accepted 60-source runs and legacy deletion also remain
pending under Specs 33/34.
