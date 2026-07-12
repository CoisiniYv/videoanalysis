# Clip / Media Phase 3B Replay Slot Fencing

Date: 2026-07-12

Scope: Spec 34 Phase 3 database and repository foundation. This checkpoint does
not enable Coordinator V2 and does not claim
`PASS_CLIP_WORKER_COORDINATOR_V2_RECOVERABLE`.

## 1. Result

Migration 031 adds an additive Replay create-side fence to `evidence_tasks`:

- current `replay_slot_owner`;
- stable logical `replay_slot_token`;
- monotonic `replay_slot_generation`;
- explicit reserved/submitting/committed create state and timestamps;
- pure Replay `plan_hash`, request identity and Redis delivery identity;
- active-fence and uncommitted-create recovery indexes.

Legacy slot columns and APIs remain available while V2 is disabled. No Viewer,
Savant, model, FPS, threshold, evidence-window, UUID/PTS/session, media file,
trajectory, thumbnail or evidence row was removed or changed.

## 2. Fence Semantics

The token identifies one logical Replay attempt and remains stable across a
pending-delivery ownership transfer. The owner and generation identify the
clip process currently allowed to mutate the create side. A takeover keeps the
token, changes owner and increments generation. Every create-start and durable
handoff write checks all three values plus the pure plan hash.

The successful handoff is one SQL statement. It conditionally updates the task
with job ID, resulting stream, `materializing` state and replay handoff, then
projects the same identity into the event. Redis ACK remains outside this
repository and is still controlled by `ClipCoordinator` only after the method
returns a durable result.

## 3. Runtime Database Safety

Before applying Migration 031, the actual local PostgreSQL database was idle:
zero evidence tasks and zero active Replay slots. A PostgreSQL 16 custom-format
backup was created and its archive catalog was read successfully:

```text
path=/data/video-analytics/artifacts/db-backups/video_analytics_pre_migration031_20260712T124019Z.dump
size=253095
sha256=c17f4667a1ee8df917049faf8d73410b1ded63d130814a1d36b76eac9b510237
```

Migration 031 was then applied twice with `ON_ERROR_STOP=1`. The second
application produced only expected already-exists notices, proving idempotent
upgrade behavior.

## 4. Verification

```text
new static/fake/real PostgreSQL fencing tests:  6 passed
existing Coordinator and slot tests:           32 passed
Migration 031 first apply:                      passed
Migration 031 second apply:                     passed
PostgreSQL version:                             16.14
```

The real PostgreSQL test inserted one event/task inside a transaction, acquired
generation 1 as owner A, persisted `submitting`, transferred ownership to owner
B at generation 2, and proved:

- owner A generation 1 durable commit: rejected;
- owner B generation 2 durable commit: accepted;
- committed job/resulting stream/create state: readable and consistent;
- test transaction: rolled back, leaving no test row.

## 5. Remaining Phase 3 Work

1. Add typed Replay transport outcomes and recover an uncertain response by
   logical token/resulting stream without creating a second job.
2. Move the complete one-message business path behind `ClipCoordinator`.
3. Return one `ProcessingOutcome` from every branch and remove V2-path ACKs.
4. Inject crashes before/after Replay, durable commit and ACK.
5. Prove pending reclaim converges to zero or one Replay job.
6. Pass one-source, two-source and restart/recovery canaries before enabling V2.
