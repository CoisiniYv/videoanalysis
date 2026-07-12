# Clip / Media Phase 2 Clip Pure Plan Parity

Date: 2026-07-12

Scope: Spec 34 Phase 2 only. This checkpoint extracts typed Clip contracts,
pure request/gate/window/anchor/Replay planning and a bounded proof-resolution
port without changing Redis ACK, PostgreSQL transition, Replay admission or
Replay HTTP side-effect ownership.

## 1. Result

Acceptance token:

```text
PASS_CLIP_WORKER_PURE_PLAN_PARITY
```

The active Clip path now normalizes one parsed record request into a frozen
contract, evaluates gate decisions through a pure policy/context function,
uses the pure planner for requested PTS windows, frame-proof anchor projection
and Replay labels, and creates a stable primary Replay plan before admission.

No `services/evidence-viewer` file was modified. Savant, model settings, FPS,
thresholds, 5+5 policy, UUID/PTS/session rules and 8090 semantics were not
changed. No evidence, trajectory, thumbnail, media, Redis message or database
row was deleted.

## 2. New Internal Boundaries

`services/clip-worker/app/contracts.py` contains dependency-free frozen
contracts for:

- normalized record requests;
- frame anchors and frame-domain proofs;
- gate policy/context/decision;
- Replay slot timing and active-job observations;
- stable Replay plans;
- ready/not-ready/invalid proof outcomes.

`services/clip-worker/app/replay_planner.py` imports no Redis, psycopg, httpx or
subprocess package. Its pure functions own:

- request identity, event-type fallback, keyframe precedence and post-Savant
  classification;
- requested PTS-window derivation;
- full, truncated-pre and cross-session-post anchor projection;
- pressure/quota/global/shard/source/cooldown gate decisions;
- Replay labels and primary REST payload construction;
- canonical JSON serialization and SHA-256 plan hashing;
- strict legacy-oracle parity assertion.

`services/clip-worker/app/proof_resolver.py` owns the typed bounded resolver port
and process-local lookup concurrency gate. It owns no Redis/DB/Replay client,
slot or ACK policy and cannot create a Replay job. The already validated
UUID/PTS/session frame scan remains the injected legacy callable backend in
this phase; moving its low-level Redis paging helpers does not precede parity
proof and remains part of the Coordinator/proof adapter cleanup.

## 3. Legacy Oracle And Stable Hashes

The existing pure helper implementations remain temporarily under explicit
`_legacy_*` names. Tests compare every planner result to those oracles instead
of silently choosing one result. A mismatch raises `ReplayPlanParityError`.

The Phase 0 Replay payload fixtures now also have independent Phase 2 hashes:

```text
normal_full_window          12f8f4a5a11cc20e3e1ec4a72f72f7697388b46c458e9515fcedea5be02c32a9
truncated_pre_window        af613978aaf0b8fa9f41c7f6c754908e422e189ad70f42b69efc691736c4423e
cross_session_post_proof    dc78083a994d2b5282f3b3bd4b9141590fb5b1d6d2ac5a8dd367344e21e0b2eb
```

Canonical serialization is insensitive to input mapping order and sensitive
to contract changes. The plan schema/hash is written into existing Clip phase
diagnostics without adding a second state writer.

## 4. Runtime Shadow Safety

`CLIP_WORKER_PLANNER_SHADOW_ENABLED=true` is tracked in Compose and the
deployment environment. Before atomic Replay admission, the worker builds the
pure primary plan and compares it with the legacy payload builder using one
immutable startup snapshot of constant-cadence and ts-sync controls.

The shadow path performs no HTTP, SQL or Redis command. Static AST tests prove
that `worker.py` contains one `create_job()` call site and the planner/proof
modules contain zero. The active path passes the already planned labels and
explicit cadence/ts-sync snapshot to that one Replay call.

## 5. Verification

Executed checks:

```text
Phase 2 planner/contract tests:              14 passed
Expanded Clip replay/session gate:          126 passed
Spec 34 Clip + Media combined gate:         359 passed
Deployment contract:                        28 passed
compileall (Clip):                           passed
docker compose config --quiet:               passed
git diff --check:                            passed
```

The planner suite covers dependency direction, frozen contracts, three
request-normalization variants, all gate branches, full/truncated/cross-session
anchor combinations, full legacy payload equality, fixed hashes, strict
mismatch failure, proof typed outcomes, proof concurrency <=2 in the injected
test, and zero additional Replay call sites.

The local `clip-worker` was recreated with `--no-build --force-recreate
--no-deps`. It started with restart count zero, one parent plus eight consumer
processes, `planner_shadow_enabled=True`, constant cadence true and ts-sync
false. Redis record-request pending and lag remained zero; PostgreSQL remained
`events=0`, `evidence_tasks=0`, `active=0`; 8090 DB-backed evidence health
returned HTTP 200; no planner mismatch, traceback or worker-loop error was
logged. The known AdaFace orphan containers were left untouched.

## 6. Explicit Remaining Work

Phase 2 deliberately does not move ACK, consumer-group, admission repository,
evidence repository or Replay transport ownership. The low-level legacy Redis
frame-proof scanner remains behind the new bounded typed port, preserving the
validated frame-domain algorithm while Coordinator V2 is introduced.

Spec 34 Phase 3 must create `ClipCoordinator.process_one()`, centralize ACK
policy, split admission/evidence repositories, inject crash points and prove
pending reclaim converges to zero or one Replay job. Legacy pure oracles and
the planner shadow flag are removed only after Coordinator parity/recovery
gates; Media Phase 3+ and final pressure/soak gates also remain pending.
