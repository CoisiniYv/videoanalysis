# 33_midterm_media_worker_evidence_scheduler_remediation_plan.md

Date: 2026-07-10

Status: 执行中；Phase 4 正确性门于 2026-07-21 重新打开，Phase 6 容量校准进行中，Phase 7 尚未执行

Corrective checkpoint (2026-07-21): the 2026-07-20 one-hour local
60-source/8fps run invalidated the earlier assumption that every durable remux
handoff either entered the finalizer lane or was immediately returned to
PostgreSQL. Of 5,144 handoff candidates, 5,072 were immediately admitted; the
72-item gap exactly matched `handoff_recovered=72`. The Phase 4 correctness
gate is therefore reopened until admission rejection performs an exact
owner/token/generation CAS that preserves `finalizer_pending` and the immutable
handoff, clears the lease, applies short jitter, and releases WIP only after
the fenced transition attempt. This does not weaken the recovery query's
`lease_token IS NULL` fence. The same run had 733 expired tasks with
`materialization_attempt_count=0`, so Phase 6 must separately increase and
calibrate remux/shared-WIP throughput; adding finalizer workers alone is not an
accepted remediation. Design and validation evidence is recorded in
`docs/code_review/media_worker_finalizer_admission_fenced_retry_2026-07-21.md`.

Implementation checkpoint (2026-07-12):

- Phase 0 now emits job-local probe/correlation fields, legacy scheduler poll
  metrics and explicit behavior-video/watchlist-image counts while preserving
  the existing scheduling path;
- the trusted baseline and hashes are recorded in
  `docs/code_review/clip_media_phase0_legacy_behavior_baseline_2026-07-12.md`;
- acceptance token: `PASS_MEDIA_WORKER_SCHEDULER_BASELINE_TRUSTED` is satisfied
  only as the Spec 33 Phase 0 observability/baseline gate; no later scheduler
  acceptance token is claimed.
- Phase 1 applies Migrations 029/030, makes deferred claim-terminal, separates
  retry time from immutable ready time, introduces phase/owner/fenced leases
  and durable handoff, and gives media-worker sole ownership of rolling
  deadline/lease recovery;
- Clip expiry is now Replay-only, worker/API/report/drain state sets share the
  dependency-free lifecycle contract, and finalizer terminal task CAS plus the
  basic event projection is one fenced PostgreSQL transition;
- fresh 001-030, 028 upgrade, repeated 029/030, real-PostgreSQL stale-owner and
  recovery tests, service recreate, Redis/DB and 8090 smoke are recorded in
  `docs/code_review/clip_media_phase1_lifecycle_contract_2026-07-12.md`;
- acceptance token: `PASS_EVIDENCE_MATERIALIZATION_STATE_CONTRACT_UNIFIED`.
  Phase 1 does not by itself claim later scheduler tokens;
- Phase 2 extracts one `_finalize_one()` boundary, moves WIP permit ownership
  to its outermost `finally`, removes recursive sink scans and private guards
  from the finalizer pool, and keeps the shared permit through terminal CAS,
  DB index and bounded cleanup;
- finalizer outputs are built under `.incoming/{event_id}/{generation-token}`,
  fenced immediately before atomic canonical publish, and converged without
  overwriting an already complete canonical bundle. Recovery validates source,
  runtime epoch, attempt token, sink path and immutable file identity;
- cleanup failure is persisted as `cleanup_pending` without reversing terminal
  state, and both cleanup and durable `finalizer_pending` recovery run from the
  common poll path. The temporary rollback flag is wired into effective
  Compose config and really bypasses the V2 pool when disabled;
- implementation and disposable-PostgreSQL proof are recorded in
  `docs/code_review/clip_media_phase2_single_finalizer_boundary_2026-07-12.md`;
- acceptance token: `PASS_MEDIA_WORKER_SINGLE_FINALIZER_BOUNDARY`. This does
  not claim long-lived executors, connection pooling, scheduler V2, segment
  indexing, pressure closure or legacy removal.
- Phase 3 introduces one process-lifetime `WorkBudget`, bounded image/remux/
  finalizer lanes, process-lifetime source caps, an optional bounded
  `psycopg_pool.ConnectionPool`, short-checkout connection proxies, fenced
  lease heartbeat/max-attempt-age enforcement, managed ffmpeg process groups,
  and the `running -> quiescing -> draining -> stopping -> stopped` shutdown
  state machine;
- reservations precede rolling remux claim and a permit can move from remux to
  finalization without reopening WIP capacity. `max_active=0` creates no
  executor/pool and claims no work;
- the real pool probe, retained `.461` Media/8090 canary, bounded TERM/KILL
  shutdown and daily-runtime restoration are recorded in
  `docs/code_review/clip_media_phase4a_media_long_lived_resources_2026-07-12.md`;
- acceptance token: `PASS_MEDIA_WORKER_LONG_LIVED_RESOURCE_BOUNDS`. This does
  not claim the Phase 4 non-blocking three-lane scheduler, segment index,
  capacity/pressure closure or legacy removal.
- Phase 4 enables the non-blocking Scheduler V2 over separate bounded image,
  remux and finalizer lanes. Scheduler ticks drain completed futures without
  waiting, perform metadata-only discovery, and reserve lane/source/permit
  capacity before claim;
- ordinary exceptions, terminal-write failures, submit/discovery failures and
  forced shutdown converge through fenced durable retry without leaking WIP,
  lane/source reservations, heartbeats or DB checkouts;
- retained mixed video/image/general/snapshot canaries and the `.490-.497`
  `finalizer_pending` SIGKILL/restart group passed with one bundle per video,
  visible DB-backed bbox/person context, zero active lease/Replay slot and zero
  runtime residuals;
- implementation and runtime proof are recorded in
  `docs/code_review/clip_media_phase4b_media_scheduler_v2_2026-07-13.md`;
- acceptance token: `PASS_MEDIA_WORKER_NONBLOCKING_THREE_LANE_SCHEDULER`.
  This does not claim the Phase 5 segment index, Phase 6 capacity/readiness
  calibration, two 60-source pressure gates or Phase 7 legacy removal.
- Phase 5 adds one process-lifetime source/epoch `RollingSegmentIndex`, bounded
  identity-keyed metadata-row caching, incremental refresh plus periodic
  reconcile, read pins, and one lock-arbitrated retention/byte-quota owner;
- rolling remux carries an immutable duration probe into finalization and
  reuses it only while device/inode/size/mtime match. Sidecar generation and DB
  bundle, artifact, timeline, and overlay indexing expose separate durations;
- retained `.510/.511/.512` canaries proved same-source catalog reuse without
  reparsing, symmetric read-pin release, zero fallback scans, zero finalizer
  ffprobe, DB-backed bbox/person context, playable media and zero global
  lease/slot/pending residuals;
- implementation and runtime proof are recorded in
  `docs/code_review/clip_media_phase5_segment_index_2026-07-13.md`;
- acceptance token: `PASS_MEDIA_WORKER_INCREMENTAL_SEGMENT_AND_FINALIZER_PATH`.
  This does not claim Phase 6 capacity/readiness calibration, either required
  60-source closure, or Phase 7 legacy removal.

## 0. 执行摘要

本方案把当前问题限定为 `media-worker` 内部调度、资源生命周期和任务状态
合同修复，不把 rolling-cache 写盘、Replay admission、上游 forwarder 压力或
watchlist 产品语义混成同一个问题。

目标不是继续增加线程，而是把现有链路改成一个短周期调度器和三条长期、
有界的执行线路：image、video remux、finalizer。主 poll 只做查询、claim、
dispatch 和非阻塞结果回收，不再同步抽帧，也不再等待一批 finalizer 收口。

执行顺序固定为：

1. 冻结同口径基线并修正 per-job 指标；
2. 统一 deferred/retry/ready/lease 状态合同；
3. 抽出完整的单任务 finalizer 边界；
4. 引入共享 WIP permit、长期 executor 和 PostgreSQL 连接池；
5. 拆开 image/video 调度，并做 durable `finalizer_pending` 恢复；
6. 引入 source/epoch 长生命周期 segment index；
7. 用 4/8/12 同负载 A/B 选择容量，再调整固定 grace；
8. 通过两轮 60 路验收后删除旧同步分支、临时 pool 和假 fallback 配置。

第一轮结构 A/B 必须保持 video `5+5`、watchlist image-only、grace `9s`、
采样 `400s`、drain `120s`，否则不能证明排队结构已经修好。当前
`ROLLING_CACHE_FALLBACK_TO_REPLAY=true` 没有生产调用，不能作为回滚保障；
完整回滚必须暂停新 admission、drain rolling 任务、恢复 record request，
再用真实 Replay canary 验证。

## 1. Decision

The next evidence performance change will be a focused remediation of the
`media-worker` scheduler, resource lifetime, and task-state contract.

This is an execution sub-plan of
`specs/31_midterm_evidence_architecture_remediation_plan.md`. It supersedes the
old conclusion that the temporary internal finalizer pool is sufficient under
high-density load, but it does not replace the product-level decisions in
Specs 30-32.

The implementation will use one process with long-lived, bounded execution
lanes before considering another service:

```text
short scheduler tick
  -> one expiry/recovery pass
  -> drain completed results without waiting
  -> finalizer admission
  -> independent video admission
  -> independent image admission

image lane:     frame lookup/extract -> image artifacts -> DB terminal state
video lane:     segment lookup -> remux -> durable finalizer handoff
finalizer lane: bundle -> sidecar/probe -> DB terminal commit -> cleanup audit

shared lifetime resources:
  WorkBudget + PostgreSQL ConnectionPool + RollingSegmentIndex
```

The main poll loop must never run ffmpeg, build a sidecar, wait for a finalizer
batch, or wait for an executor worker. PostgreSQL remains the durable queue;
in-memory queues are bounded execution aids, not the source of truth.

## 2. Evidence And Baseline

The fixed comparison artifact is:

```text
run_id=pressure60_8p1_dual1gpu_5s5s_yolob4_ada16_drain120_20260710T054418Z
streams=60
sample=400s
drain=120s
behavior_video=5s pre + 5s post
watchlist=image-only
video_bundles=252
image_bundles=119
```

The evidence relevant to this plan is:

| Metric | Baseline |
| --- | ---: |
| rolling metadata visibility p95 | 0.816s |
| task ready-to-claim p95 | 33.95s |
| media queue p95 | 39.16s |
| finalizer pool wait p95 | 15.22s |
| DB claim wait p95 | 0.545s |
| evidence lifecycle p95 | 45.13s |
| media-worker peak CPU | 1475.75% |

This means the first repair is not rolling-cache writer tuning and is not a new
PostgreSQL index campaign. Segments are visible quickly and row claiming is
quick; work waits inside `media-worker`.

The source structure confirms the runtime result:

- `services/media-worker/app/worker.py:6007-6018` runs expiry, then synchronous
  image work, then the video runner;
- `services/media-worker/app/worker.py:6186-6251` processes image tasks one by
  one, including ffmpeg extraction;
- `services/media-worker/app/worker.py:6951-7037` has a persistent remux pool,
  but its completed-future drain synchronously enters finalization;
- `services/media-worker/app/worker.py:5803-5869` creates a new finalizer
  executor for each batch and waits for all futures;
- `services/media-worker/app/worker.py:5901-5945` opens one PostgreSQL
  connection and creates one `_MaterializationGuard(1)` per finalizer job;
- the outer guard created at `services/media-worker/app/worker.py:8173` does
  not govern the pool branch;
- the current guard is released before later task/event convergence, expanded
  DB indexing, sidecar pruning, and cleanup complete;
- `services/media-worker/app/rolling_cache.py:66-103` recursively finds and
  parses metadata on lookup, and `_select_rows()` reads selected metadata again;
- overdue expiry runs once in `_process_rolling_cache_tasks()` and again in the
  persistent runner;
- process-global probe counters are differenced by concurrent jobs, so current
  per-job probe attribution is not trustworthy.

## 3. Scope

This plan owns:

1. the rolling image/video/finalizer scheduling topology inside `media-worker`;
2. the meaning and enforcement of `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE`;
3. durable handoff and restart recovery between remux and finalization;
4. the retryable versus terminal evidence-task state contract;
5. a long-lived PostgreSQL connection pool;
6. a long-lived source/epoch segment index;
7. job-local phase/probe metrics;
8. removal of the obsolete synchronous rolling path and related dead code after
   the new path has passed soak tests.

The following are separate acceptance axes or follow-up plans:

- forwarder queue pressure and `validate_seq_iq` failures;
- Replay admission and shard tuning when the workload actually uses Replay;
- the product/API split between raw-clip-ready and annotation-ready in Spec 31;
- watchlist policy. Spec 32 already defines watchlist evidence as image-first;
  image work must be scheduled correctly, not removed or silently converted to
  video;
- model batching, watchlist thresholds, and Savant inference semantics.

The first structural A/B run must keep the current 5+5 window and 9-second
grace. Grace is tuned only after the scheduler has passed, so a shorter policy
wait cannot hide an unchanged queue.

## 4. Non-Negotiable Invariants

### 4.1 Main-loop latency

One scheduler tick may query, claim, submit, and reap completed results. It may
not execute media work or wait for a future. Slow image extraction and slow
finalization must not stretch the effective poll interval.

### 4.2 One global WIP limit

`MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE` becomes the end-to-end in-flight task
limit for image and video materialization.

For a new rolling video task:

```text
acquire permit
  -> atomic DB claim
  -> remux
  -> durable finalizer_pending handoff
  -> finalizer
  -> bundle/sidecar/DB terminal commit
  -> cleanup attempt/audit
release permit in outermost finally
```

For image evidence, the same permit spans extraction through the terminal DB
write. A finalizer never creates or acquires a second private guard for a video
job that already owns a permit.

Permit transfer is move-only, not a second acquisition. When remux completes in
the same process, ownership may move to the accepted finalizer queue item. If
that bounded queue is full, submit fails, shutdown begins, or the process exits,
the worker must atomically persist `finalizer_pending`, release the in-process
permit, clear/release the active lease, and let recovery acquire a new fenced
lease and permit later. While an accepted item waits briefly in the bounded
finalizer queue, the scheduler owns its lease heartbeat. A permit is never
assumed to survive a process restart.

The executor sizes are queue/service capacities, not permission to exceed the
shared limit. At every sample:

```text
in_flight_total <= configured_max_active
image_active + remux_active + finalizer_active <= configured_max_active
```

`max_active=0` means materialization is disabled and no task is claimed. It
must not mean unlimited concurrency.

The implementation must not simply pass the existing shared guard into the old
pool. Existing early `continue` branches can bypass its current release point,
and later DB/index/cleanup work occurs after that release point. Permit ownership
must live at the new job runner's outermost boundary.

### 4.3 Claim only runnable work

The scheduler acquires a permit and an executor queue slot before it claims a
new DB task. A task must not be claimed and then wait behind an unbounded
executor queue.

Video, image, and finalizer-pending work use separate bounded lanes. A bounded
executor wrapper reserves both a lane queue slot and the keyed source slot
before DB claim; `ThreadPoolExecutor`'s unbounded internal queue is not treated
as admission control.

In mixed image/video mode, `max_active` must be at least 2 and at least one slot
is never loaned to image work. Therefore an image burst cannot occupy every
permit, and a ready video can be claimed within two polls. Finalizer/video
arbitration is priority/deadline aware, while oldest-ready age promotion
prevents starvation. With no ready video, the reserved non-image slot may serve
finalizer work but not image work.

### 4.4 Durable handoff and idempotency

Remux completion is not represented only by an in-memory future. Each attempt
writes to an attempt-token staging directory; it must not delete or rebuild the
canonical event directory before it wins the lease fence. Before the remux
runner returns, one compare-and-swap transaction persists `sink_output_path`,
an immutable file identity/hand-off document, `finalizer_pending`, and the
lease token. On restart, the worker must recover that exact artifact rather
than remuxing or publishing a duplicate.

All bundle and terminal transitions remain idempotent. A repeated claim may
converge an existing output, but it may not create a second bundle or overwrite
a terminal result with an active state.

### 4.5 Business deadline is not a worker lease

`materialization_deadline_at` remains the evidence business/SLA deadline.
Worker ownership uses a separate owner, fencing token/generation, and expiry.
Long work heartbeats the lease. Every phase/terminal DB write, canonical file
publish, and destructive cleanup verifies the current owner/token; a stale
worker that lost its lease may only delete its own attempt staging directory.

A crash may expire a lease and permit recovery; it must not silently extend the
business deadline or immediately convert recoverable work into a terminal
failure.

### 4.6 Deadline And Lease Decisions

The single expiry/recovery pass follows this table:

| State | Deadline | Lease/artifact | Action |
| --- | --- | --- | --- |
| waiting/pending | future | no lease | remain eligible by ready/next-attempt |
| waiting/pending | past | no valid artifact | terminal deadline miss |
| running | crosses deadline | valid heartbeat lease | allow the bounded attempt to finish; record SLA miss |
| finalizer_pending | any | valid immutable artifact | recover/finalize; do not discard playable work |
| running | future | expired lease | fence old owner and recover/retry |
| running | past | expired lease + valid artifact | recover finalizer and mark late |
| running | past | expired lease + no valid artifact | terminal deadline miss |

Lease renewal has a configured maximum attempt age. A heartbeat cannot keep a
hung subprocess alive forever. Business deadline miss and evidence salvage are
reported separately.

### 4.7 Runtime epoch and source isolation

Every queue item, segment-index snapshot, lease, output path, and recovery query
is scoped by non-empty `source_id` and `runtime_epoch_id`. Migration may backfill
an epoch only from an exact task/event payload match. Ambiguous active rows are
quarantined with an auditable terminal/manual-repair reason; they are never
bound to whatever epoch happens to be current. No compatibility fallback may
cross an epoch.

## 5. State Contract

### 5.1 Resolve `materialization_deferred`

Today `materialization_deferred` has incompatible meanings:

- a covered child or intentional terminal no-physical-materialization outcome;
- a transient rolling coverage miss that the worker retries;
- a resource-pressure deferral.

Migration 027 and pressure/runtime reporting already treat reason-filled
`materialization_deferred` as terminal, while the rolling worker claims it
again. Existing tests assert both sides independently and therefore pass while
the cross-layer contract is contradictory.

The repaired contract is:

- `materialization_deferred` is claim-terminal;
- covered child rows leave it only through covered-parent reconciliation;
- transient coverage, DB, capacity, or storage retry uses
  `materialization_pending`;
- retry timing is stored in `materialization_next_attempt_at`;
- retry cause is stored in `materialization_retry_reason`;
- `materialization_ready_at` remains the original natural/policy readiness time
  and is never moved forward by a retry.

Reason authority is also exclusive:

- pending/retryable rows use `materialization_retry_reason` and clear
  `materialization_defer_reason`;
- terminal deferred rows use `materialization_defer_reason` and clear
  `materialization_retry_reason`;
- failed/expired rows use their existing failure/expiry field and clear both
  retry/defer fields.

New code emits normalized reason codes instead of parsing exception prose:

| Class | Normalized examples | Result |
| --- | --- | --- |
| retryable coverage | `coverage_not_complete`, `no_overlapping_final_segment`, `segment_not_stable`, `segment_disappeared` | pending + next-attempt |
| retryable infrastructure | `db_unavailable`, `db_pool_timeout`, `temporary_io_error` | pending + next-attempt |
| permanent input | `missing_event_frame_pts`, `invalid_requested_window`, `missing_source_id`, `missing_runtime_epoch`, `stable_metadata_invalid` | failed/manual quarantine |
| intentional terminal | `covered_by_existing_evidence`, explicit quota/policy skip, hard storage stop | deferred/skipped according to product contract |

Retry delay is
`max(retry_hint, min(10s, 0.5s * 2^(attempt-1))) + 0..250ms jitter`,
bounded by a default 12 attempts and the business deadline. Invalid-window or
identity errors never loop. Migration 029 maps legacy prefixes explicitly and
writes the chosen normalized code into audit; unknown text is not guessed.

Candidate eligibility becomes conceptually:

```sql
materialization_status IN ('manifest_ready', 'materialization_pending')
AND materialization_ready_at IS NOT NULL
AND materialization_ready_at <= now()
AND COALESCE(materialization_next_attempt_at, materialization_ready_at) <= now()
```

Every independently maintained terminal/active set in media-worker,
clip-worker expiry, API/runtime apply, drain checks, artifact analysis, and
tests must import or validate one canonical contract. `materialization_deferred`
must be removed from rolling candidates, active backlog counts, general
finalizer claims, and overdue-active expiry.

### 5.2 Add phase and lease fields

A new migration after the currently published/untracked 025-028 set adds at
least:

```text
materialization_phase
materialization_phase_updated_at
materialization_next_attempt_at
materialization_retry_reason
materialization_lease_owner
materialization_lease_token
materialization_lease_expires_at
materialization_handoff
```

The handoff document contains at least attempt token, source, runtime epoch,
frozen requested window, selected segment IDs, staging/canonical path, device /
inode (when meaningful), size, mtime-ns, and optional/required content digest by
commit phase. Recovery re-stats and validates it before reuse.

Expected non-terminal phase values are:

```text
waiting_ready
waiting_coverage
image_running
remux_running
finalizer_pending
finalizing
```

The public status remains compatible during this scheduler repair. Phase is
diagnostic/recovery state, not a new 8090 product state.

`materialization_status` is the sole scheduling authority. `status` is a
compatibility projection updated by the same transition helper; event payload
media fields are a downstream projection and are never queried to decide task
admission. Each transition has one field-reset contract:

- retry clears owner/token/lease and any invalid handoff;
- finalizer-pending retains the fenced immutable handoff;
- terminal clears owner/token/lease/next-attempt/retry reason;
- cleanup state is separate and cannot move a materialized task backward.

The transition helper owns this compatibility table:

| `materialization_status` | Phase | compatibility `status` | Event projection |
| --- | --- | --- | --- |
| `manifest_ready` | `waiting_ready` | `pending` | pending/materializing |
| `materialization_pending` | `waiting_ready` / `waiting_coverage` | `materialization_pending` | pending/materializing |
| `materializing` | `image_running` / `remux_running` | `materializing` | materializing |
| `materializing` | `finalizer_pending` / `finalizing` | `finalizing` | materializing |
| `materialized` | terminal | `materialized` | materialized/image_ready as appropriate |
| `materialization_deferred` | terminal/covered | `materialization_deferred` | deferred/covered |
| `materialization_failed` | terminal | `materialization_failed` | failed |
| `materialization_expired` | terminal | `materialization_expired` | expired |

The image/video product distinction is retained in bundle/event fields; it does
not create a second scheduler status authority.

### 5.3 Migration layout

Do not edit migrations 025-028. They may already have been applied manually on
the runtime host even though they are currently untracked in this checkout.

Use two new migrations:

1. `029` is transactional schema/data normalization.
2. `030` rebuilds affected hot-path indexes with
   `CREATE/DROP INDEX CONCURRENTLY` outside a transaction.

Migration 029 classifies old deferred rows:

- covered-by rows remain terminal deferred;
- known retryable rolling/pressure reasons that are still before their business
  deadline become pending with `next_attempt_at`;
- already expired retryable rows move to the existing failed/expired contract;
- reason-empty deferred rows before deadline become pending with the normalized
  `legacy_empty_deferred` retry audit; expired ones become terminal;
- unknown reason-filled rows remain terminal deferred for manual audit.

It also normalizes legacy `pending` materialization values to
`materialization_pending`, backfills source/epoch only from exact persisted
identity, and quarantines ambiguous active rows. Before admission resumes there
must be zero active rows with NULL ready-at, empty source, or empty epoch. A row
that cannot be repaired is terminal/manual-quarantine, not a permanently
unclaimable active row.

Migration 030 aligns all active/ready indexes, including older Migration 015,
020, 024, 025, 026, and 027 shapes, with the canonical state set. It also adds
the ready/next-attempt ordering needed by the scheduler.

The final ready index predicate contains only `manifest_ready` and
`materialization_pending` with non-NULL ready-at. The active runtime-epoch index
contains those states plus `materializing`; neither index contains deferred.
Integration tests assert the exact definitions rather than only searching for a
status word in migration text.

Old databases do not auto-apply repository migrations on container restart.
Execution must explicitly run both files with `ON_ERROR_STOP`, record their
hashes, and verify index definitions afterward.

### 5.4 Ready-time authority

The event-worker is the only producer of `materialization_ready_at`.
Media-worker reads it and does not invent a second NULL fallback formula for
new rows.

During the first implementation pass, preserve current pressure semantics:

```text
video: event + post 5s + grace 9s = 14s
image: event + segment 4s + grace 9s = 13s
```

Migration 029 backfills old active NULL values using persisted task policy plus
an explicitly recorded deployment value for segment/grace. It must not silently
hard-code the pressure value for every environment. If the effective historical
value cannot be established, the migration leaves the row for an audited
one-time repair/quarantine step instead of inventing readiness; admission does
not resume while such a row is still active. Extending a coverage parent's post
window must recompute its ready time. This removes the current mismatch where
the media fallback can add post + segment + grace.

Coverage-window extension is allowed only while the parent is unleased and in a
waiting phase. Atomic claim freezes the requested window into the fenced job
contract. A later child may attach only when its full window fits that frozen
contract; otherwise it creates/uses another parent instead of extending a
running clip.

## 6. Target Components

The change is incremental, not an 8,000-line rewrite.

### 6.1 `MaterializationScheduler`

Owns one short `tick()`, admission ordering, bounded queues, shared permits,
lease recovery, and executor lifecycle. It does not contain bundle business
logic.

Recovery is a shared component that remains enabled regardless of the temporary
Scheduler V2 admission flag. Legacy admission may be selected during canary,
but durable `finalizer_pending` work is always understood by the same fenced
recovery/finalizer code.

Suggested location:

`services/media-worker/app/materialization_scheduler.py`

### 6.2 `FinalizerRunner`

Owns a long-lived finalizer executor and one explicit
`finalize_one(job, connection_provider)` boundary. It replaces recursive use of
`_process_sink_output()` as a batch executor.

The single-job boundary must include the full completion path:

```text
claim/validate
  -> bundle/raw clip work
  -> sidecar/probes
  -> evidence bundle/artifact/timeline/overlay DB index
  -> event/task terminal commit
  -> sidecar/sink cleanup audit
```

Source locks/in-flight caps are process-lifetime keyed resources, not dictionaries
rebuilt for each batch.

The commit point is explicit:

1. verify the lease fence and immutable handoff identity;
2. build/validate outputs in an attempt-specific staging directory;
3. verify the fence again and atomically rename/publish the canonical files;
4. in one recoverable DB transaction, upsert bundle/artifact/timeline/overlay
   rows and task/event terminal projections with owner/token CAS;
5. perform sink/staging/sidecar cleanup after commit;
6. if cleanup fails, keep materialized state and record `cleanup_pending` for a
   separate idempotent retry.

A crash after file publish but before DB commit is recovered by identity and
idempotent upsert. A stale lease holder cannot publish, commit, or delete the
winner's files. Tests inject a crash before/after each step.

Suggested location:

`services/media-worker/app/evidence_finalizer.py`

### 6.3 `RollingSegmentIndex`

Owns immutable snapshots keyed by `(source_id, runtime_epoch_id)`. It performs
one initial source/epoch scan, then incremental refresh based on path, mtime,
and size. It must:

- ignore half-written metadata until both metadata and video are stable;
- invalidate entries deleted by retention;
- acquire a bounded segment read lease/pin before remux so retention cannot
  remove an input while ffmpeg is reading it;
- never return entries from another epoch;
- use a bounded LRU for parsed frame rows;
- expose hit, miss, refresh, parse, stale-entry, and fallback-scan counters;
- retain `find_segments()` as a compatibility facade during rollout.

Incremental discovery uses a generation/watermark and a bounded periodic
reconciliation scan. Any watcher overflow or inconsistent watermark triggers a
source/epoch rebuild; it never silently freezes the catalog.

One rolling-cache maintenance owner performs retention deletion and honors
segment read leases. The dual sink services no longer each run a full-root
cleanup process. Deletions advance the catalog generation so stale entries are
invalidated deterministically.

Steady-state task lookup must not recursively `rglob` the source tree. Metadata
parsing should scale with newly finalized/changed segments, not with
`task_count * retained_segment_count`.

Suggested location:

`services/media-worker/app/segment_index.py`

### 6.4 `ConnectionPool`

Use `psycopg_pool.ConnectionPool` for worker DB operations. Keep one control
connection for scheduler queries and a bounded pool for job transactions.

The pool maximum must not exceed the chosen WIP limit; expected total service
connections are approximately `1 + max_active`, not finalizer thread count.
Connection checkout occurs only around DB work. ffmpeg and filesystem/sidecar
work do not hold an idle DB connection.

Pool checkout wait, in-use count, timeout, reset, reconnect, and close behavior
must be observable. Adding `psycopg_pool` changes dependency/image inputs, so
this phase requires rebuilding the media-worker image rather than only
recreating its container.

### 6.5 Job-local metrics

Replace global before/after probe deltas with a context passed through one job.
Record separately:

```text
ready_to_claim_ms
claim_to_remux_start_ms
remux_ms
remux_to_finalizer_enqueue_ms
finalizer_queue_wait_ms
finalizer_ms
sidecar_ms
db_bundle_index_ms
db_timeline_index_ms
db_overlay_index_ms
cleanup_ms
raw_clip_ready_at
db_index_ready_at
terminal_at
ffprobe_invocation_count/duration_ms
ffmpeg_invocation_count/duration_ms
```

Process totals remain available under synchronization. Python thread CPU may be
reported as thread CPU; subprocess and whole-process CPU must be labeled as
such. Do not present `time.process_time()` deltas from concurrent jobs as
per-job CPU.

### 6.6 Expected Change Surface

Primary files/modules are:

```text
db/migrations/029_*.sql
db/migrations/030_*.sql
services/event-worker/app/repository.py
services/clip-worker/app/repository.py
services/clip-worker/app/worker.py
services/media-worker/app/config.py
services/media-worker/app/worker.py
services/media-worker/app/rolling_cache.py
services/media-worker/app/materialization_scheduler.py
services/media-worker/app/evidence_finalizer.py
services/media-worker/app/segment_index.py
services/media-worker/requirements.txt
services/api/app/services/runtime_apply.py
services/api/app/routers/evidence.py
infra/docker-compose.midterm.yml
infra/env/midterm.env
scripts/runtime/rolling_cache_sink_entrypoint.sh
scripts/runtime/run_midterm_pressure60.py
scripts/tools/analyze_midterm_pressure_artifact.py
```

The pressure/drain/runtime status code must change in the same state-contract
phase; otherwise the worker can be correct while reports still classify work
with the old deferred/active sets.

## 7. Implementation Phases

### Phase 0 - Freeze The Contract And Baseline

No scheduling behavior changes.

1. Preserve the July 10 artifact and effective config snapshot.
2. Make the pressure report explicitly separate 252 behavior videos and 119
   watchlist images.
3. Add job-local phase/probe instrumentation while retaining old aggregate
   fields for one compatibility cycle.
4. Fix the observed `duration_guard_failed` versus inner
   `duration_guard_status=passed` attribution contradiction.
5. Add scheduler poll duration/gap, lane depth, oldest-ready age, permit usage,
   pool usage, and segment-index counters to the artifact schema.

No-go before implementation if PostgreSQL is unavailable, evidence workers are
restart-looping, source/epoch identity is inconsistent, or the fixed input and
effective policy cannot be reproduced.

Acceptance token:

```text
PASS_MEDIA_WORKER_SCHEDULER_BASELINE_TRUSTED
```

### Phase 1 - Repair State, Ready-Time, And Lease Contracts

1. Add and apply Migrations 029/030.
2. Normalize retryable deferred rows to pending + next-attempt.
3. Make deferred claim-terminal across every service/report path.
4. Separate lease expiry from business deadline.
5. Make event-worker ready time authoritative and backfill NULL rows.
6. Add fencing token, heartbeat, CAS transitions, immutable handoff, and the
   deadline/lease decision table.
7. Make media-worker recovery the only owner of rolling materialization
   expiry/recovery. Clip-worker expiry must exclude rolling-mode tasks and own
   only its Replay phases.
8. Run rolling overdue expiry/recovery exactly once per scheduler tick.
9. Add a cross-module state-set contract test so individual tests cannot assert
   contradictory meanings again.

Acceptance token:

```text
PASS_EVIDENCE_MATERIALIZATION_STATE_CONTRACT_UNIFIED
```

### Phase 2 - Extract A Single Finalizer Boundary

Keep scheduling synchronous while changing internal ownership.

1. Extract `finalize_one()` from the current scan/batch recursion.
2. Put permit release in the outermost job `finally`, after terminal DB commit
   and the bounded cleanup attempt. Cleanup failure becomes `cleanup_pending`
   and never reverses materialized state.
3. Preserve idempotent claim, source fairness, duration guards, bundle layout,
   DB index, and cleanup behavior.
4. Add tests for terminal/missing/busy/exception paths and prove no permit leak.
5. Keep the old runner behind a temporary scheduler feature flag for rollback.
6. Make fenced finalizer-pending recovery common to both legacy and V2
   admission before V2 is allowed to emit that phase.

Acceptance token:

```text
PASS_MEDIA_WORKER_SINGLE_FINALIZER_BOUNDARY
```

### Phase 3 - Introduce Long-Lived Resources

1. Create one `WorkBudget` for the worker lifetime.
2. Create one bounded PostgreSQL connection pool.
3. Create long-lived image, remux, and finalizer executors.
4. Clamp effective executor concurrency to the shared WIP limit.
5. Add process-lifetime source locks/caps.
6. Add lease heartbeat and maximum-attempt-age enforcement.
7. Implement a shutdown state machine:
   `running -> quiescing -> draining -> stopping -> stopped`.
8. On first signal, stop admission and drain until
   `MEDIA_WORKER_SHUTDOWN_GRACE_S` (default 45s); compose
   `stop_grace_period` must be greater (at least 60s).
9. At grace expiry, persist/release queued handoffs, terminate active ffmpeg
   process groups with TERM then KILL after a bounded interval, converge DB
   leases, then close executors and pool in order. A second signal selects the
   forced path immediately.

This phase changes the media-worker dependency set and requires an image
rebuild. Later pure-Python scheduler phases require recreate/restart unless
their Docker/dependency inputs also change.

Acceptance token:

```text
PASS_MEDIA_WORKER_LONG_LIVED_RESOURCE_BOUNDS
```

Implementation checkpoint (2026-07-12): complete. The lifetime resource root,
bounded lanes/source caps, optional bounded PostgreSQL pool, short-checkout
proxy, heartbeat/max-attempt-age fence, managed process groups and bounded
shutdown state machine are implemented and verified. The proof is recorded in
`docs/code_review/clip_media_phase4a_media_long_lived_resources_2026-07-12.md`.
This checkpoint does not enable or claim Scheduler V2.

### Phase 4 - Enable Scheduler V2

1. Split image and video candidate discovery and admission.
2. Move rolling image extraction, pending snapshot generation, and pending
   annotation generation entirely off the main poll thread as individual image
   lane jobs.
3. Have remux persist `finalizer_pending` and submit to the long-lived finalizer
   lane without synchronously draining it.
4. Route general Replay sink output through the same finalizer lane after it
   acquires a shared permit.
5. Keep general sink discovery metadata-only; stability checks, ffprobe, and
   media validation run inside the finalizer lane, not the poll thread.
6. Enforce video reservation/weighted fairness and oldest-ready promotion.
7. Recover remux-complete/finalizer-pending work on startup.
8. Reserve the bounded executor slot, source slot, and permit before claim;
   submit failure must return the task to a fenced recoverable state.
9. Prove no executor queue is unbounded and no DB task is claimed without
   immediate execution capacity.

Acceptance token:

```text
PASS_MEDIA_WORKER_NONBLOCKING_THREE_LANE_SCHEDULER
```

Implementation checkpoint (2026-07-13): complete. Scheduler V2 dispatches
rolling images, snapshots and annotations to the bounded image lane, rolling
video to the remux lane, and both durable handoff recovery and general Replay
sink output to the bounded finalizer lane. The main poll does not run media
work or wait for futures. Submit/discovery/job/shutdown failures release their
exact ownership or first persist a fenced retry. A retained SIGKILL at durable
`finalizer_pending` recovered eight unique bundles after lease expiry with no
duplicate, lease, slot, permit, lane or pool residual. The proof is recorded in
`docs/code_review/clip_media_phase4b_media_scheduler_v2_2026-07-13.md`. This
checkpoint does not enable or claim the Phase 5 segment index.

Post-checkpoint closure: lifecycle recovery now has its own cadence before the
Scheduler V2/legacy admission branches, scans all rolling-owned rows, and runs
even when both rolling admission flags are false. Retained canary `.500` proved
that a valid generation-1 handoff lease was not stolen, then recovered after
expiry to one generation-2 terminal bundle with DB-backed timeline/bbox and
zero runtime residual. Evidence is retained under
`/data/video-analytics/artifacts/clip_media_phase4b_flag_independent_recovery_20260712T171945Z`.

Corrective gate reopening (2026-07-21): the earlier checkpoint covered crash
recovery but missed ordinary capacity rejection after remux had already
persisted a handoff. The 2026-07-20 one-hour run proved a 72-item exact gap
between remux handoff candidates and immediate finalizer admission, followed by
72 lease-expiry recoveries. Phase 4 is reopened until every unsubmitted
transfer in a batch, including items after a lane-full `break`, performs an
exact fenced retry before WIP release; accepted in-memory queue items must keep
the transferred lease heartbeat active until executor start. PostgreSQL
`finalizer_pending` remains the durable queue, and recovered unleased handoffs
remain in that phase when capacity is still unavailable. The new correctness
gate requires zero normal-pressure lease-expiry handoff recovery, zero fenced
retry failure, fully accounted candidate/admission gaps, no duplicate bundle,
and zero lease/WIP/lane/pending residual.

### Phase 5 - Add Segment Index And Reduce Finalizer Work

1. Replace per-poll recursive lookup with `RollingSegmentIndex`.
2. Add bounded parsed-row caching and epoch/retention invalidation.
3. Replace dual sink cleanup loops with one lease-aware rolling maintenance
   owner and report effective retention/byte-quota settings. Any unimplemented
   byte-quota flag is removed rather than advertised.
4. Carry one authoritative immutable probe result from remux to finalizer and
   reuse it only while file identity still matches; otherwise re-probe.
5. Keep one authoritative final-artifact probe/guard result rather than probing
   multiple intermediate copies without a correctness reason.
6. Measure sidecar, bundle index, timeline index, and overlay index separately.
7. Optimize/batch a DB subphase only if the new metrics show it still dominates;
   do not preemptively weaken expanded-row correctness.

Acceptance token:

```text
PASS_MEDIA_WORKER_INCREMENTAL_SEGMENT_AND_FINALIZER_PATH
```

Implementation checkpoint (2026-07-13): complete. The process-lifetime index
is keyed by source/epoch, performs one bounded initial scan, incrementally
refreshes changed directories, periodically reconciles, and invalidates on the
maintenance generation marker. Parsed rows and catalogs are bounded; malformed
or half-written identities remain pending without per-task reparsing. Both
rolling lanes use lease-aware read pins. Retention and implemented byte-quota
cleanup have one filesystem-lock-arbitrated owner in single and dual-sink
topologies. Remux/finalizer duration reuse is identity-fenced, general sink
readiness carries one authoritative probe, and DB index subphases are timed
separately. The retained same-source/video/image canary, 8090 proof, tests, and
zero-residual audit are recorded in
`docs/code_review/clip_media_phase5_segment_index_2026-07-13.md`. Phase 6
capacity/readiness calibration and the two 60-source closure runs remain open.

### Phase 6 - Tune Capacity, Then Readiness

Use identical workload A/B for `max_active` candidates such as 4, 8, and 12.
The selected value is the lowest one that meets throughput/latency gates while
staying inside CPU, connection, and correctness limits. Neither the current 4
nor the current 32 finalizer threads is assumed to be correct.

`MAX_ACTIVE` is a WIP limit, not a CPU-core limit. Record ffmpeg/x264 thread
settings and validate the combined CPU budget separately.

Only after the scheduler gate passes twice with grace still set to 9 seconds:

1. add an explicit `segment_coverage_ready_at` observation;
2. distinguish policy wait, segment visibility wait, runnable-to-claim wait,
   and retry wait;
3. reduce or replace the fixed grace using measured p99 visibility plus bounded
   safety margin;
4. rerun duration, coverage, and retry gates before accepting the shorter wait.

Acceptance token:

```text
PASS_MEDIA_WORKER_CAPACITY_AND_READY_POLICY_CALIBRATED
```

Instrumentation checkpoint (2026-07-13): complete; capacity calibration is
still open. The pressure harness now accepts an explicit shared WIP candidate,
writes the complete Media Worker pressure environment to a retained Compose
override, verifies the recreated container environment, and restores every
overridden value afterward. Comparable runs fix CPU/ffmpeg threads at 4,
image workers at 4, rolling remux workers at 1, configured finalizer workers at
32, queue capacities at 4, Scheduler V2/DB pool/Segment Index enabled, and vary
only `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE`. Artifact schema v6 includes all
three lane depths, oldest-ready age, WIP, pool, lease-heartbeat, segment-index,
read-pin, DB-index subphase, sidecar-prune, and effective resource metrics. No
Phase 6 acceptance token is claimed until the fixed-input 4/8/12 runs and the
required repeat pass complete. The fixed-input profile preserves warmup visual
results and excludes them from formal gates with an event-time/creation-time
cutoff; local republisher input bytes are SHA-256 identified in the artifact.

Fixed-input ingress checkpoint (2026-07-13): the first candidate was rejected
before sampling at 21/60 visible sources and is not a capacity result. The
preflight now uses a run-scoped pinned MediaMTX, requires all per-source paths
to be readable, applies the separate `ffmpeg_input` constructor timeout, repeats
H.264 parameter sets at keyframes, retains adapter failure logs, and removes
the temporary server on every exit path. Publisher-only probes reached 40/40
and 60/60 with zero restart; a full-exporter smoke reached 20/20
adapter/forwarder/Savant visibility with zero restart, and the 60-source
pose-only closure reached 60/60 without a visibility restart. Their separate
steady-FPS gates remained below the unchanged minimum, so neither the Phase 6
token nor a capacity candidate is claimed.

Fixed-input candidate checkpoint (2026-07-13): the corrected 60-source ingress
completed `max_active=4` with 60/60 visibility and zero source restart. An
upper-bounded sampling-window recomputation found 548 formal evidence tasks;
356 expired and no intrusion video bundle completed. The retained artifact has
three additional playable postfill tasks, for 551 tasks and 195 image bundles.
Observed WIP peaked at 3 of 4 while the remux lane stayed at depth 1 and oldest
ready age reached 304 seconds, identifying service-rate/retention failure rather
than shared-WIP saturation. The candidate is rejected. Artifact audit also
corrected two harness-only gates: rolling raw FPS is now probed from the actual
fixed republish input, and retained postfill events are excluded from formal
observed-window statistics by an upper event-time/creation-time sampling fence.
These corrections do not waive or alter the expiry, FPS, queue, duration,
annotation, or correctness gates. Candidates 8 and 12 remain required.

Capacity matrix checkpoint (2026-07-13): comparable 4/8/12 candidates all
completed with 60/60 source visibility and zero source restart, but all produced
zero video bundles. WIP p95/max was 3/3, 1/7 and 2/7 respectively; remux depth
remained 1/1 for every candidate and oldest-ready p95 remained about 283-286
seconds. There is no selectable `max_active`; increasing shared WIP only absorbs
short image bursts. The harness now exposes a default-one, artifact-audited and
restored `--media-worker-rolling-remux-workers` parameter solely for a separately
labeled remux-lane experiment. This does not retroactively change the matrix or
claim `PASS_MEDIA_WORKER_CAPACITY_AND_READY_POLICY_CALIBRATED`. Fixture bytes
were fixed, but event mix still showed that publisher-to-sampling phase must be
frozen before the required repeat runs.

Bounded remux-lane checkpoint (2026-07-13): a separately labeled
`max_active=8`, remux-workers=4 canary proved that the configured lane change
was effective: observed remux depth p95/p99/max was 0/1/4 and shared WIP peaked
at 7. It nevertheless produced zero video bundles. Of 267 formal events, 195
intrusion tasks expired as `business_deadline_expired`; the other 78 terminal
failures were watchlist image tasks whose event media records
`face_image_no_rolling_cache_segments`, not remux failures. Event ingestion was
already too late for the retained media window: the maximum event-timestamp to
DB-created-at delay was 344.776 seconds, oldest-ready p95/max was
284.518/331.014 seconds, and finalizer depth stayed zero. Therefore remux
concurrency is not the next tuning axis. Phase 6 must first split and correct
event timestamp to event insertion to evidence-task creation/readiness delay,
and the harness must preserve the concrete image failure reason instead of
reporting it as `unknown`. No further remux increase, grace calibration, or
Phase 6 acceptance token is allowed until a fixed-phase short canary produces
nonzero video. Fixture phase must remain frozen before a deterministic
scheduler regression decision.

Event-gate and read-pin closure checkpoint (2026-07-13): the late intrusion
tasks were traced to a pressure prefill Event Worker recreate that left the
consumer unavailable for about five minutes. Prefill activation now uses a
run-scoped Redis gate with lower event-time fencing and no consumer recreate.
Postfill writes an upper event-time fence: later buffered Savant deliveries are
retained as events/trajectories but cannot create tasks outside retained rolling
coverage. The key has TTL and explicit restore cleanup. Keep-all pressure runs
now delete zero event, face-observation, person-observation, or evidence rows;
formal reports use bounded start/end event-time predicates instead of deleting
postfill results.

The same checkpoint closed three evidence correctness gaps. Image readiness now
requires both `image_ready` DB state and a real image artifact, so
`playback_kind=image` cannot hide `image_missing`. Segment identity changes are
fenced retries in image, compatibility video, and Scheduler V2 video lanes.
`rolling_cache_event_frame_not_covered` is classified as retryable
`coverage_not_complete`. Pressure rolling sinks are gracefully stopped after
source/event quiescence so the current tail chunk is finalized before drain.

The retained short structural proof is:

```text
/data/video-analytics/artifacts/phase6_v2pinfinal_canary60_8fps_5p5_ma8_r4_20260713T1140CST
```

It produced 102/102 materialized tasks with zero failed, expired, or pending:
85 behavior videos and 17 watchlist images. All 85 videos passed 5+5 duration,
timeline, and annotation validation. The 8090 DB-backed audit passed 102/102
details, 85/85 timelines, and 85/85 annotations with zero missing bbox,
person-context, or fallback. The formal window was 73/73 materialized. The
postfill fence retained 95 later events while creating zero out-of-tail tasks,
and the preservation audit recorded zero deletions. Redis event/face/person
consumer lag and pending were zero.

This does not issue `PASS_MEDIA_WORKER_CAPACITY_AND_READY_POLICY_CALIBRATED`.
The run was only a 60-second canary and still failed the separate local 8fps
throughput gate at 5.247fps versus 7.92fps. Production T4 4fps validation and
two comparable 400-second plus 120-second-drain passes remain mandatory.

2026-07-21 local one-hour checkpoint: a later fixed-input single-GPU,
dual-branch 60-source run sustained 8.0246 fps but materialized 5,057/5,790
formal tasks. Successful finalization itself stayed fast (p50 1.46s, p95
2.68s); remux was full in about 41.1% of samples, shared WIP in 23.4%, and the
finalizer lane in only 5.4%. All 733 expired rows had attempt count zero. After
the reopened Phase 4 gate is repaired, the first separately labeled capacity
candidate is WIP=20, remux=12, finalizer threads=8, finalizer queue=8, rolling
max-per-poll=8, while finalizer process workers remain 4 pending a corrected
process-pool wait measurement. A large in-memory queue is explicitly rejected:
PostgreSQL owns durable waiting and deadline/fairness ordering.

### Phase 7 - Remove Legacy And Misleading Contracts

After two accepted 60-source runs and one restart-recovery soak:

1. delete the production-unreachable synchronous rolling branch;
2. remove per-batch `ThreadPoolExecutor` creation;
3. remove duplicate `_metadata_labels()` definitions;
4. remove the duplicate expiry call;
5. remove process-global per-job metric differencing;
6. split stable scheduler/finalizer/state code out of `worker.py`;
7. remove `ROLLING_CACHE_FALLBACK_TO_REPLAY` from effective media-worker
   configuration until Spec 31 implements a real producer/consumer transition;
8. update Specs 26/30/31 to point at this plan's actual scheduler and rollback
   contract.

After this phase there is no legacy branch to toggle. Scheduler rollback then
means redeploying the recorded previous schema-compatible V2 image or applying
a forward fix; full path rollback uses the coordinated Replay procedure below.

The fallback flag is not a valid rollback today. With suppressed record
requests, a rolling miss does not publish a Replay request. Actual Replay
fallback remains a separate Spec 31 phase and must not be implied by a boolean
that no production code consumes.

Acceptance token:

```text
PASS_MEDIA_WORKER_LEGACY_SCHEDULER_REMOVED
```

## 8. Test Matrix

### 8.1 State and migration tests

- fresh migration chain through 030;
- upgrade fixture from 024-028 through 030;
- idempotent rerun of 029 and 030;
- covered, retryable, expired, and unknown deferred-row normalization;
- reason-empty deferred normalization;
- retry miss becomes pending + next-attempt without changing original ready-at;
- zero active NULL-ready, empty-source, or empty-epoch rows after migration;
- terminal deferred is never selected by rolling/general finalizer, active drain,
  or expiry queries;
- covered child can become materialized only by parent reconciliation;
- business deadline and worker lease expiry behave independently;
- stale owner versus renewed/recovered owner races at every CAS transition;
- valid lease crossing deadline and expired lease with/without artifact follow
  the decision table;
- a claimed coverage parent cannot extend its frozen window;
- partial-index predicates and `EXPLAIN` match candidate ordering.

### 8.2 Scheduler tests

- 119 slow image tasks do not stop a ready video claim;
- under an image-only competing burst, the oldest ready video is admitted via
  the reserved non-image permit within two configured poll intervals;
- a slow finalizer does not block the poll loop;
- finalizer-pending work is durable and recovered after restart;
- legacy and V2 admission flags both run the common finalizer-pending recovery;
- every terminal, busy, missing, submit-failure, exception, cancel, and shutdown
  branch returns its permit;
- configured 32 finalizer threads still cannot exceed the shared active limit;
- source caps/fairness persist across ticks;
- no queue exceeds its configured bound and no claimed work waits outside the
  declared WIP limit;
- lane/source/permit reservation happens before claim and submit failure
  converges phase, lease, and permit;
- rolling image, general snapshot, and general annotation jobs do not execute
  on the main loop;
- lease-loss worker cannot publish/commit/cleanup after a recovered worker wins;
- crash injection around staging, rename, handoff, DB commit, and cleanup
  recovers exactly one terminal bundle;
- graceful and forced shutdown finish within the compose grace period and leave
  only recoverable durable state;
- duplicate finalization produces one bundle and one terminal transition.

### 8.3 Connection-pool tests

- N jobs reuse a bounded number of connections;
- peak worker connections are at most `1 + pool_max`;
- checkout timeout becomes a retryable task outcome, not a terminal corruption;
- DB restart/reset does not leak checked-out connections;
- pool closes cleanly on worker shutdown;
- filesystem/ffmpeg work does not hold a checked-out connection.
- `max_active=0` starts no job executor/pool and claims no work.

### 8.4 Segment-index tests

- source and runtime epoch isolation;
- initial rebuild and incremental new-segment discovery;
- half-written metadata ignored until stable;
- retention deletion invalidates stale entries;
- lease-aware maintenance cannot delete an actively remuxed segment;
- dual sink mode runs exactly one cleanup owner;
- bounded row-cache eviction;
- file identity change invalidates carried probe results;
- steady repeated task lookup performs no full recursive scan;
- legacy layout fallback is bounded and observable.

### 8.5 Existing targeted suites

At minimum, update and run:

```text
harness/tests/test_evidence_materialization_phase0.py
harness/tests/test_evidence_materialization_phase2plus.py
harness/tests/test_media_worker_perf_safety.py
harness/tests/test_rolling_cache_materialization.py
harness/tests/test_midterm_worker_indexes_static.py
harness/tests/test_midterm_pressure60_script.py
harness/tests/test_midterm_pressure_artifact_analyzer.py
harness/tests/test_evidence_db_index.py
```

Also run `compileall`, compose config validation, migration integration tests,
and `git diff --check` for each implementation phase.

## 9. Runtime Validation Ladder

Every rung must preserve artifacts before the next rung starts:

1. deterministic unit/concurrency tests with injected slow image/finalizer work;
2. one-source video and image correctness smoke;
3. two-source mixed image/video head-of-line test;
4. eight-source canary with worker restart during finalizer-pending work;
5. fixed-input 60-source 400s A/B with 120s drain and grace still 9s;
6. live 60-source 400s validation;
7. Spec 31 deterministic and live 600s soak only after this scheduler plan
   passes.

The fixed-input A/B must use the same input hash/URI, source count, runtime
epoch rules, FPS, model batches, cooldown, 5+5 policy, image/video policy,
prefill/postfill, and drain duration as the accepted baseline. Reports with a
different event mix are informative but not a scheduler regression decision.
For deterministic A/B, created-task mapping must be exact and unsuppressed
image/video counts must match by type; for live A/B, each type must remain
within a predeclared 5% tolerance and latency must also be normalized by arrival
rate.

## 10. Go/No-Go Gates

### 10.1 Correctness gates

All are mandatory:

- `materialization_expired=0` for the comparable scheduler run;
- duplicate bundles/materializations = 0;
- terminal status contradictions = 0;
- finalizer failures = 0, except a separately proven invalid input artifact;
- after 120s drain, active tasks, active Replay slots, executor queues, checked
  out DB connections, and pressure source containers are all zero;
- retained image evidence passes its image/8090 checks;
- retained video evidence is playable and passes duration, timeline, annotation,
  and 8090 checks;
- no task is dropped, suppressed, or converted to another evidence type to make
  latency pass.

### 10.2 Structural gates

- scheduler poll gap p95 is no more than two configured poll intervals under an
  injected slow image/finalizer;
- with the reserved non-image permit free, image burst to first-ready-video
  claim is no more than two poll intervals;
- observed in-flight peak never exceeds effective `max_active`;
- all permits return after drain and after forced exception/shutdown tests;
- worker DB connections never exceed the pool contract;
- no per-job PostgreSQL connect burst remains;
- steady segment parses track new/changed segments, not task count multiplied by
  retained segments;
- no finalizer executor is created per batch and no main-loop future wait
  remains.

### 10.3 Performance gates

The first Scheduler V2 release gate is:

| Metric | Release gate | Final closure target |
| --- | ---: | ---: |
| ready-to-claim p95 | <=15s and >=50% better than 33.95s | <=5s |
| finalizer queue/pool wait p95 | <=5s | <=5s |
| media queue p95 | <=15s | <=10s |
| evidence lifecycle p95 | <=35s | <=30s |
| DB claim wait p95 | <=1s | <=1s |
| rolling metadata visibility p95 | <=2s | <=2s |

The selected release run must also reduce media-worker peak CPU by at least 30%
from `1475.75%`, without losing work or moving the backlog past the drain
window. If hardware noise makes a single peak unreliable, the report must also
provide p95 CPU and active-stage counts; bounded concurrency remains a hard
gate.

Passing the release column enables broader soak. This plan is complete only
when the final closure column passes twice with comparable workloads.

### 10.4 Separate upstream gate

Forwarder queue-full and `validate_seq_iq` remain separate. An upstream failure
does not erase valid isolated scheduler evidence when source visibility and
event workload are comparable, but it prohibits claiming full-machine
production readiness.

## 11. Rollout

### 11.1 Preconditions

1. Inventory and checkpoint the current dirty worktree. Do not normalize or
   overwrite unrelated changes.
2. Record the exact code revision, migration hashes, image IDs, effective env,
   compose config, runtime epoch, and baseline artifact path.
3. Treat 025-028 as published migrations and never rewrite them.
4. Confirm the actual PostgreSQL server is healthy before migration or runtime
   comparison.

### 11.2 Migration/deploy order

The current repository is better served by a short maintenance window:

1. build and identify the candidate media-worker image before downtime;
2. stop the API service or enable a proven maintenance barrier that blocks
   runtime-apply/state writes, stop new evidence admission, and quiesce event-,
   clip-, and media-worker;
3. capture row counts/state distribution and a DB backup;
4. apply transactional Migration 029;
5. apply non-transactional concurrent-index Migration 030;
6. verify state distributions, index predicates, and query plans;
7. deploy compatible state-set changes to event/clip/media/API/reporting code;
8. recreate the affected services from the recorded candidate images;
9. verify worker/schema health, release the maintenance barrier for one
   controlled source, and run the one-source postflight;
10. progress through the validation ladder only after the postflight converges.

Each phase should be a reviewable commit. Do not combine schema normalization,
scheduler concurrency, segment indexing, grace tuning, and legacy deletion in
one change.

### 11.3 Temporary rollout flags

Temporary flags may isolate rollback during canary:

```text
MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED
MEDIA_WORKER_SCHEDULER_V2_ENABLED
MEDIA_WORKER_SEGMENT_INDEX_ENABLED
MEDIA_WORKER_DB_POOL_ENABLED
```

`MEDIA_WORKER_SINGLE_FINALIZER_V2_ENABLED` is the Phase 2 runner bridge. It
defaults to `true`; `false` selects legacy synchronous admission while the
fenced lifecycle and durable recovery remain common. Its owner is
`media-worker`, and it is removed together with the legacy runner in Phase 7.

The isolated Phase 2 finalizer boundary defaults to `true`. At the accepted
Phase 5 checkpoint Scheduler V2, its required DB pool, and the segment index
default to `true`. Canary overrides are recorded in effective config. Valid
combinations for the later three flags are:

| V2 | DB pool | Segment index | Result |
| --- | --- | --- | --- |
| false | false/true | false/true | legacy admission canary with common recovery |
| true | true | false | V2 with bounded compatibility segment scan |
| true | true | true | target path |
| true | false | any | startup error |

V2 mixed image/video mode also fails startup when `max_active < 2`. With
`max_active=0`, no job pool/executor is created and no claim occurs. Startup
logs and runtime health expose requested/effective flag values, migration/state
contract version, pool bounds, WIP limit, and lane reservations.

The flags must be removed, or their legacy branch deleted, after Phase 7. They
are a deployment bridge, not permanent alternate architectures.

## 12. Rollback

### 12.1 Scheduler-only rollback Before Phase 7

1. stop new claims;
2. wait for active jobs to reach terminal or durable `finalizer_pending` state;
3. preserve the failed-run artifact and DB state snapshot;
4. disable Scheduler V2 and recreate media-worker;
5. let the common, flag-independent startup recovery converge any durable
   pending work;
6. keep additive schema/index changes in place.

If the recovery component itself is under suspicion, V2 cannot be disabled
until `finalizer_pending=0`, active leases are zero, and all handoffs are
accounted for. Segment index can be disabled independently during canary; the
DB pool cannot be disabled while V2 is enabled.

Any duplicate evidence, unplayable retained artifact, connection exhaustion,
permit leak, non-convergent 120s drain, or p95 regression greater than 20%
against the comparable baseline is an immediate no-go.

After Phase 7, rollback is deployment of the recorded previous
schema-compatible V2 image or a forward fix. The deleted legacy scheduler flag
is no longer a valid target.

### 12.2 Full rolling-path rollback

Do not use `ROLLING_CACHE_FALLBACK_TO_REPLAY=true` as a rollback switch; no
production transition currently consumes it.

To return safely to Replay:

1. pause source/event admission so new events cannot enter both paths;
2. drain or explicitly account for existing rolling tasks;
3. set `ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=false` and disable rolling
   materialization/sinks in one coordinated deployment;
4. stop/remove the `rolling-cache` and `rolling-cache-dual` profile services and
   verify no rolling sink container or writer process remains;
5. recreate event-worker, clip-worker, and media-worker with the Replay
   configuration;
6. resume admission with one canary event;
7. prove a record request, Replay job, sink output, final bundle, no duplicate
   rolling output, and 8090 result
   before normal traffic resumes.

## 13. Definition Of Done

This plan is not complete when code compiles or when one small canary passes.
It is complete when:

```text
PASS_MEDIA_WORKER_SCHEDULER_BASELINE_TRUSTED
PASS_EVIDENCE_MATERIALIZATION_STATE_CONTRACT_UNIFIED
PASS_MEDIA_WORKER_SINGLE_FINALIZER_BOUNDARY
PASS_MEDIA_WORKER_LONG_LIVED_RESOURCE_BOUNDS
PASS_MEDIA_WORKER_NONBLOCKING_THREE_LANE_SCHEDULER
PASS_MEDIA_WORKER_INCREMENTAL_SEGMENT_AND_FINALIZER_PATH
PASS_MEDIA_WORKER_CAPACITY_AND_READY_POLICY_CALIBRATED
PASS_MEDIA_WORKER_LEGACY_SCHEDULER_REMOVED
PASS_MIDTERM_MEDIA_WORKER_EVIDENCE_SCHEDULER_READY
```

The final report must link the two comparable passing 60-source artifacts,
state/migration verification, restart-recovery proof, targeted test output,
compose effective config, selected capacity rationale, and rollback smoke.

## 14. Future Execution Prompt

```text
Execute specs/33_midterm_media_worker_evidence_scheduler_remediation_plan.md
phase by phase.

Start by re-reading the current worktree and July 10 baseline; preserve unrelated
dirty changes. Do not modify migrations 025-028. Keep behavior video at 5+5,
watchlist image-only, grace at 9s, sample at 400s, and drain at 120s for the
first structural A/B.

Unify deferred/retry/lease state before introducing asynchronous queues. Then
add fenced lease token/generation, heartbeat, CAS transitions, exact retry
taxonomy, and the deadline/lease decision table. Use per-attempt staging and an
atomic immutable handoff; stale workers must not publish, commit, or clean the
winner's files.

Extract one complete finalizer job boundary, add the bounded DB pool and shared
end-to-end WorkBudget, and enable independent long-lived image, remux, and
finalizer lanes. Reserve non-image capacity in mixed mode, reserve bounded
lane/source slots before claim, and move legacy snapshot/annotation work off the
main thread too. The main scheduler tick must never execute ffmpeg/sidecar work
or wait for a future. Add flag-independent durable finalizer_pending recovery
and bounded TERM/KILL shutdown before pressure testing.

The source/epoch RollingSegmentIndex and job-local metrics are complete. Next,
tune max_active with comparable 4/8/12 candidates; do not assume 4 or 32 is
correct. Only after the structural gate passes twice may fixed grace be reduced
from measured segment coverage visibility.

At each phase run the specified state, migration, concurrency, pool, index, and
existing targeted tests. Rebuild media-worker when the psycopg pool dependency
changes; otherwise recreate affected services. Preserve every canary/pressure
artifact and stop at any no-go gate. Never claim Replay fallback from the
currently unused flag.

Finish only after all Definition Of Done tokens and two comparable 60-source
closure artifacts pass.
```

## 15. 2026-07-21 Phase 4 Reopen And Phase 6 Checkpoint

Candidate B 的 10 分钟短测通过，但同配置 1 小时正式测失败，不能关闭
Phase 6。正式 artifact 为：

```text
/data/video-analytics/artifacts/pressure60_8p1_admissionfix_1h_20260721T092938Z
```

正式窗口 5,778 个任务中 4,921 materialized、857 attempt=0 expired；
oldest-ready p95=299.06s。输入 60/60、8.0246 fps、零 send/queue/raw loss，
因此失败归于 media-worker 持续服务率。实测 tick-gap p95=7.40s、remux
p95=12/12、WIP p95=20/20、真实 process-pool wait p95=3.65s。

Phase 4 同时保持 reopened：一次正常 lane-full 已正确执行 0.523s fenced
retry，但另一个已 admitted 的 remux handoff 在 handoff 事务提交前被新连接
用 `SKIP LOCKED` claim，误判 busy，约 120s 后发生 lease-expiry recovery。
修复必须把 remux 的原始 MaterializationLease 继续传到 finalizer job，并使用
exact owner/token/generation 的阻塞式行锁转移；普通 recovered/unleased handoff
仍保留 `lease_token IS NULL` 与 `SKIP LOCKED` 语义。

下一容量候选为 WIP=32、remux=16、rolling max-per-poll=16、finalizer
threads=8、queue=8、process workers=8。先通过 10–15 分钟 A/B，且满足
handoff lease-expiry recovery=0、attempt=0 expiry=0、oldest-ready 不累积，
再重跑 1 小时。Candidate B 不得成为默认容量结论。

### 15.1 Candidate C closure result

exact-lease 修复提交 `2a57f20` 后，Candidate C 的 15 分钟短测通过
scheduler/media 正确性与吞吐子门：正式任务 1,427/1,427 materialized，
candidates=1,474、gap=122、fenced retry=122、retry failed/recovery/
claim-busy/duplicate=0。短测 artifact：

```text
/data/video-analytics/artifacts/pressure60_8p1_exactlease_c15m_20260721T110655Z
```

同配置 1 小时正式 artifact：

```text
/data/video-analytics/artifacts/pressure60_8p1_exactlease_c_1h_20260721T114530Z
```

正式窗口 5,781 个任务中 4,742 materialized（82.03%）、1,039 attempt=0
expired（17.97%）。输入 60/60、8.0245 fps，send/queue/raw loss=0；含
warmup/postfill 的 4,826 个 bundle 全部通过 ffprobe 与 8090 detail/
timeline/annotation/bbox/person-context。drain 后 active task/lease/WIP/lane/
finalizer_pending 全为 0。

本次 exact-lease 子门通过：candidates=4,826、immediate admitted=4,807、
gap=19、fenced retry=19、retry failed=0、handoff recovered=0、claim-busy=0、
duplicate=0。一次 heartbeat CAS false 没有伴随 expiry、recovery、candidate
缺口或残留状态；需要补 event key/phase 观测，但不等同于 lease-expiry
handoff recovery。Phase 4 的 admission/exact-transfer reopen 子门可关闭。

Phase 6 仍失败，而且 Candidate C 不得默认化。ready-to-remux p50/p95 为
249.69s/285.49s，实际 remux p95 仅 1.186s，handoff-to-admission p95 仅
0.167s；oldest-ready p95=301.22s。remux/finalizer lane p95 均为 16，WIP
p95=32，poll-gap p95=13.21s，finalizer process-pool wait p95=5.576s。
Candidate C 相比 B 的 85.17% 成功率反而降到 82.03%，说明同时把
max-per-poll、WIP/remux 和 process workers 放大造成阶段振荡/资源争用。

下一步不再盲目扩容。先补全 scheduler loop total/poll-gap 分解（现有
`tick_duration_ms` 不覆盖 snapshot/logging/sleep 后段），再正交比较
process workers 4/8、max-per-poll 8/12/16 与 WIP/remux 配对；finalizer
queue 保持 8，PostgreSQL 继续作为唯一 durable queue。只有短测证明持续
service rate 高于到达率、oldest-ready 可回落且 attempt=0 expiry=0，才允许
再次进入 1 小时 Phase 6 验收。
