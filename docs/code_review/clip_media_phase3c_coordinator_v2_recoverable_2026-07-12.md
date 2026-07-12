# Clip / Media Phase 3C Recoverable Coordinator V2

Date: 2026-07-12

Scope: Spec 34 Phase 3C. This checkpoint completes the recoverable Clip
Coordinator V2 and the minimum Media sink-receipt bridge required for a crash
between the Replay HTTP response and the durable handoff transaction. It does
not claim Media Scheduler V2, Phase 4 performance gates, Phase 5 extraction,
the cross-worker soak, the two 60-source closure runs, or legacy removal.

Acceptance token:

```text
PASS_CLIP_WORKER_COORDINATOR_V2_RECOVERABLE
```

## 1. Result

Coordinator V2 is now the tracked default. One source, two simultaneous
sources, a crash after durable commit, and a crash immediately after the Replay
response all converge without a duplicate Replay create, duplicate bundle,
incorrect Redis ACK, or dangling Replay slot.

The implementation keeps the existing proof/frame-domain and Replay payload
algorithms. It changes orchestration ownership and recovery only:

- `ClipRequestProcessorV2` owns one complete business message;
- `ClipCoordinator` is the only ACK decision boundary;
- malformed JSON is written to the dead-letter stream before ACK;
- Replay submission is typed as `created`, `permanent_rejected`, or
  `uncertain`;
- owner/token/generation/plan-hash fence every create-side transition;
- a reclaimed active slot bypasses new-request capacity/cooldown gates, so it
  cannot count itself as a competing per-source request;
- definitive Replay 4xx rejection atomically terminal-fails task and event,
  releases the slot, and only then permits ACK;
- ambiguous responses remain pending and cannot issue a second create;
- the immutable planned Replay request is persisted before the HTTP call;
- when Clip Worker dies after Replay accepts the request, Media Worker accepts
  sink output as a recovery receipt only when event ID, slot token, resulting
  stream, active epoch, sink path, and planned request all agree;
- receipt recovery records `replay_create_state=sink_confirmed` and
  `materialization_handoff.kind=replay_sink_receipt`. It intentionally leaves
  an unavailable Replay job ID empty instead of inventing one;
- normal committed releases still require token, job ID, and resulting stream.

Runtime crash injection is opt-in and one-shot through:

```text
CLIP_WORKER_CRASH_INJECT_POINT
CLIP_WORKER_CRASH_INJECT_MARKER
CLIP_WORKER_CRASH_INJECT_EXIT_CODE
```

The effective normal runtime has an empty injection point and marker.

## 2. Runtime Canaries

All canary events and their evidence remain present for audit.

| Event | Scenario | Result |
|---|---|---|
| `00000000-0000-4000-8000-000000000451` | one-source normal V2 | one job/bundle; 10.25 s; timeline 83; overlay 6; annotation complete |
| `00000000-0000-4000-8000-000000000452` | crash after durable commit, before ACK | process exit 91 and restart; one job/bundle; generation 1; pending converged to 0 |
| `00000000-0000-4000-8000-000000000453` | simultaneous source A | one job/bundle; 9.75 s; no Savant metadata expected for the temporary source |
| `00000000-0000-4000-8000-000000000454` | simultaneous source B | one job/bundle; 9.50 s; no Savant metadata expected for the temporary source |
| `00000000-0000-4000-8000-000000000455` | stopped-source diagnostic | Replay accepted once but no cached message remained; deadline terminal-failed and ACKed; no duplicate submit |
| `00000000-0000-4000-8000-000000000456` | pre-fix response-crash diagnostic | exposed missing planned labels and failure-path slot release; retained as a failed regression fixture |
| `00000000-0000-4000-8000-000000000457` | fixed crash after Replay response | process exit 91 and restart; one Replay submit; sink receipt confirmed; one 9.757667 s bundle; timeline 303; Range 206; pending/lag/active slot all 0 |

The two failed diagnostic events are evidence of the defects found during the
canary sequence. They were not deleted or rewritten into successes.

The final response-crash canary has no trigger-person object, so its annotation
contract is complete with zero overlay rows. The retained `.451/.452` canaries
prove the same V2 path with six DB-backed displayable person overlays each.

## 3. Runtime Invariants

After restoring normal runtime:

```text
CLIP_WORKER_COORDINATOR_V2_ENABLED=true
CLIP_WORKER_CRASH_INJECT_POINT=
CLIP_WORKER_CRASH_INJECT_MARKER=
Redis pending=0
Redis lag=0
active Replay slots=0
duplicate Replay jobs=0
duplicate evidence bundles=0
temporary clip-v2 source containers=0
```

No evidence, trajectory, thumbnail, or media cleanup was performed. The three
AdaFace orphan containers reported by Compose were also left untouched.

## 4. Verification

The combined Phase 3 and adjacent Clip/Media regression gate passed:

```text
227 passed, 1 skipped
python -m compileall -q services/clip-worker/app services/media-worker/app
docker compose -f infra/docker-compose.midterm.yml config
git diff --check
```

The PostgreSQL-backed owner/generation fence test ran against PostgreSQL 16;
the migration fixture remained rollback-only. Migration 031 had already passed
two consecutive real applications and its pre-migration backup remains:

```text
/data/video-analytics/artifacts/db-backups/video_analytics_pre_migration031_20260712T124019Z.dump
sha256=c17f4667a1ee8df917049faf8d73410b1ded63d130814a1d36b76eac9b510237
```

Auditable runtime artifact:

```text
/data/video-analytics/artifacts/clip_media_phase3c_coordinator_v2_20260712T134714Z
manifest sha256=4dfcbc7838e9a49859fcffe60d3a19c54892fb5a53f2dad18bf8c593d657fe83
```

The artifact contains DB and Redis snapshots, filtered Replay/Media/Clip logs,
ffprobe output, 8090 HTTP and Range checks, runtime effective flags, repository
state, and `SHA256SUMS`.

## 5. Remaining Work

Spec 34 is not complete. Phase 4 must now execute the authoritative Spec 33
Media Scheduler contract: shared WIP, fenced lease/handoff, bounded long-lived
lanes, PostgreSQL pool, non-blocking scheduler, and `RollingSegmentIndex`.
Phases 5-7, cross-worker crash/SIGTERM soak, two equivalent 60-source runs, and
legacy coordinator/scheduler removal remain mandatory.
