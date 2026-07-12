# Clip / Media Phase 3A Coordinator Ports

Date: 2026-07-12

Scope: Spec 34 Phase 3 foundation only. This checkpoint introduces delivery,
outcome, ACK, crash and repository boundaries while the production worker
continues to use the validated legacy coordinator.

## 1. Result

Phase 3A is complete, but `PASS_CLIP_WORKER_COORDINATOR_V2_RECOVERABLE` is not
claimed. The real single-message business processor, fenced Replay slot and
crash/reclaim integration remain required.

No Viewer, Savant, model, FPS, threshold, evidence-window, UUID/PTS/session or
storage behavior changed. No Redis message, database row, evidence, trajectory,
thumbnail or media file was deleted.

## 2. Delivery And ACK Boundary

`request_consumer.py` owns Redis consumer-group creation, new reads, pending
inspection, XAUTOCLAIM/XCLAIM fallback, normalized delivery envelopes,
diagnostics and idempotent ACK mechanics. It does not interpret Replay policy
or write PostgreSQL.

`contracts.py` now defines frozen:

- `DeliveryEnvelope` with delivery/reclaim count;
- `ProcessingCode` and `ProcessingOutcome`;
- explicit `AckDisposition`;
- crash points before/after Replay, durable commit and ACK.

`ClipCoordinator.process_one()` receives one envelope and one processor outcome.
`AckPolicy` ACKs only when the outcome explicitly requests ACK and is durable.
Unexpected exceptions are converted to non-durable HOLD; injected crash
exceptions escape the boundary for deterministic crash tests. The coordinator
contains no `xack`, `xreadgroup`, SQL/cursor or `create_job` call.

## 3. Repository Ports

`replay_admission_repository.py` provides named active-count, acquire,
record-job and release operations over the schema-compatible repository.
`evidence_state_repository.py` provides terminal/target/diagnostic reads and
named pending, failed, replay-created and terminal-deferred transitions. It
does not expose arbitrary SQL to the future coordinator.

Replay slot owner/token fencing is not claimed here. The current Migration-022
slot schema has no dedicated owner/token fields; adding and validating that
fence is the next Phase 3 step rather than pretending the adapter alone solves
crash ownership.

## 4. Rollout Safety

`CLIP_WORKER_COORDINATOR_V2_ENABLED=false` is tracked in Compose and the
deployment environment. While the processor is incomplete, setting it true
causes startup to fail before group/read/job side effects. There is no silent
legacy fallback and no dual execution mode.

## 5. Verification

```text
Phase 3A contract tests:                     16 passed
Phase 3A + existing Clip/deployment gate:   102 passed
Spec 34 Clip + Media combined gate:         375 passed
deployment contract:                         28 passed
compileall:                                  passed
docker compose config:                       passed
git diff --check:                            passed
```

Tests cover new/reclaimed envelope normalization, delivery counts, every ACK
matrix class, durable-before-ACK enforcement, unexpected exception HOLD,
before/after-ACK crash semantics, reclaim-before-new ordering, named repository
delegation, coordinator dependency rules and fail-closed rollout configuration.

## 6. Remaining Phase 3 Work

1. Move the one-message business path into `ClipCoordinator.process_one()`.
2. Add owner/token/generation fencing to Replay slot acquire/record/release.
3. Persist Replay job ID, resulting stream and replaying state as one durable
   commit before ACK.
4. Inject crashes before/after Replay response, commit and ACK.
5. Prove pending reclaim converges to zero or one Replay job.
6. Run one/two-source and restart recovery before making V2 the only path.
