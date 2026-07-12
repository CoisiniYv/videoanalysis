# Clip / Media Worker Phase 0 Legacy Behavior Baseline

Date: 2026-07-12

Scope: Spec 34 Phase 0 and Spec 33 Phase 0 only. This document freezes the
legacy behavior before lifecycle, ACK, lease, scheduler, or module ownership is
changed.

## 1. Source And Runtime Identity

- Source baseline revision:
  `5314d0baaf8d4a4e1e4873a644513ac15dede379`
  (`docs: add frontend interface handoff`).
- The immediately preceding backend/spec revision is
  `04c593190e79988d6ad1daaa3675204c31d76af8`.
- Worktree was clean before Phase 0 started. The concurrent frontend handoff
  commit only added `docs/frontend_interface/`; this Phase does not modify
  `services/evidence-viewer` or frontend product behavior.
- Baseline source hashes:
  - Clip `worker.py`:
    `4a51282073c6e98c6a7902b5fdd71fa3f5aa4de1d6461562ec6f5822886b50f6`
  - Media `worker.py`:
    `08b9ef5f43ff635bd3e302b6ac6c6bf7073893fb6e8c3c4f42f296067eeb0ecf`
  - Pressure runner:
    `da9e541edd6755783f5f6df819df268b5d2864522fa0e84e12c1eff13dcdc303`
- Effective Compose config hash using `infra/env/midterm.env`:
  `ba38a68525e59b9d6b56f63ef9af45d417f612fdad394663554eb874539f3dd5`.
- The minimum spec command without the env file resolves to
  `5ecd549006ae62a2f426d2f17f424702f70f013e56a5738c984bed8d6fe3353f`;
  it is retained only as a comparison and is not treated as the live effective
  configuration.
- Ordered aggregate SHA-256 of tracked migrations 001-028:
  `3af023a8c70cb8c3a90bbfed0f2fcb39969a763a56b221177b296e712ebcf359`.

The local runtime uses an external dev PostgreSQL container published on host
port 5432 rather than the inactive Compose-profile `postgres` service:

| Component | Image ID | Restart count | Observed state |
| --- | --- | ---: | --- |
| clip-worker | `sha256:f23ccc2f73f97e8d655db2d3e1a964303466c2bac063d783f5420fa399ddc1cd` | 1 | running, no restart loop |
| media-worker | `sha256:b37af649b2637f80ea6b6510e886de1ac5adc27e945a0750aadc6abca731c390` | 1 | running, no restart loop |
| phase0-postgres | `sha256:be2dedd215733ac25f6d3d2413f5f579f35c82bd659e09ef47fc0f2a4bada0eb` | 0 | healthy |
| redis | `sha256:487efc0616382465781b8fdc3d6d1db449e6fd80ae23bf48432a2da6b6929908` | 0 | healthy |

The PostgreSQL server is 16.14. Migrations through 028 are represented by the
current columns/indexes, active invalid source/epoch rows are zero, active
NULL-ready rows are zero, and the Phase 0 dev database contains no evidence
tasks. Redis `security.record_requests` has `pending=0` and `lag=0`.

The normal local runtime is Replay-first and rolling materialization is off.
Relevant effective values are:

```text
EVIDENCE_TOPOLOGY=post_savant_replay
MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=4
ROLLING_CACHE_ENABLED=false
ROLLING_CACHE_MATERIALIZATION_ENABLED=false
ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=false
POST_SAVANT_FAST_RAW_CLIP_ENABLED=true
```

No credentials or secret-valued environment entries are included in this
baseline.

## 2. Comparable Pressure Artifact

The fixed comparison artifact remains:

```text
/data/video-analytics/artifacts/
  pressure60_8p1_dual1gpu_5s5s_yolob4_ada16_drain120_20260710T054418Z
```

Hashes:

| File | SHA-256 |
| --- | --- |
| `report.json` | `ca443a48f87584d56be54a09f0a34859ba2ff32ead6ff16d5266340e3d6ccb08` |
| `run_config.json` | `61ae4843d7573d434743997f1fdd554e0546a8b753073dedbc21e21726617f36` |
| `non_materialized_task_details.json` | `f755027ad996b724fe28011845222af804968a7dbfc3cc3e535b85047fea20b7` |

Effective comparison contract:

- 60 sources, 8 FPS, one GPU with two Savant branches;
- YOLO pose batch 4, YOLO face batch 4, AdaFace batch 16;
- 400-second sample, 120-second drain, 25-second prefill/postfill;
- behavior evidence 5+5 seconds; watchlist evidence image-only;
- rolling-cache ready grace 9 seconds;
- 252 playable behavior videos and 119 ready watchlist images;
- 252/252 DB timelines and 252/252 DB annotations passed;
- 11,550 retained video annotation records; bbox/person-context missing were 0;
- 371/372 tasks materialized or image-ready; one task failed.

The run's overall pressure status remains failed because of upstream
`forwarder_queue_full` and `validate_seq_iq_exceeded`; that status is not
rewritten by this baseline.

## 3. Clip Worker ACK / Slot / State Characterization

The legacy coordinator currently contains 16 direct `XACK` call sites. The
executable manifest and tests freeze these outcomes:

| Branch | Durable/state action before return | ACK | Replay slot outcome |
| --- | --- | --- | --- |
| malformed JSON/data | none | yes | none |
| duplicate in current process | existing in-memory dedupe only | yes | none |
| target already terminal | terminal DB state read | yes | none |
| stale request with no event/task | absence read from DB | yes | none |
| invalid Replay shard/source | attempts failed state projection | yes | none |
| policy terminal defer | attempts terminal deferred projection | yes | none |
| global/shard/source capacity | writes pending/queue diagnostics | no | none |
| cooldown below retry budget | writes pending/deferred diagnostics | no | none |
| cooldown/proof retry exhausted | writes failed projection | yes | none |
| proof not ready | writes waiting-proof projection | no | none |
| missing source/time/keyframe | writes failed projection | yes | none |
| atomic admission unavailable/denied | writes pending/queue diagnostics | no | none/acquire denied |
| existing terminal Replay slot | terminal slot read | yes | terminal slot retained |
| Replay transport exception | writes failed and releases reservation | yes | released |
| Replay empty/permanent response | writes failed and releases reservation | yes | released |
| Replay job created | calls status and slot-job persistence | yes | held until media completion |
| unexpected outer-loop exception | traceback only | no | branch-dependent |

Important frozen defect: the success branch currently calls
`update_clip_status()` and `record_replay_job_for_slot()` before `XACK`, but it
does not condition the ACK on either boolean result. The characterization test
records the ordering without treating it as acceptable. Phase 1/3 must replace
this with one durable-outcome ACK policy.

## 4. Media Worker Branch / Resource Characterization

The legacy source has three `ThreadPoolExecutor` construction sites, two
rolling-expiry call sites, and three `_process_sink_output()` call sites. The
main loop still performs synchronous image/snapshot/annotation work and waits
through legacy finalizer batches.

| Branch | Claim/permit | State outcome | Artifact/cleanup outcome |
| --- | --- | --- | --- |
| processed/invalid directory | none | no-op | skip |
| missing event/video or unstable sink | none | waiting by rescan | no publish |
| storage/backlog/guard refusal | no finalization claim | legacy deferred projection | no publish |
| terminal finalization claim | no new work | idempotent terminal | optional guarded sink cleanup |
| busy/claim error | no job execution | remains recoverable by rescan | no cleanup |
| finalizer exception | permit released in legacy `finally` | failed projection | diagnostics retained/processed marker |
| successful finalizer | legacy permit is released before all DB/index/cleanup follow-on | task/event converged afterward | bundle/index then cleanup |
| rolling coverage miss | runner future removed | legacy deferred/retry shape | no canonical bundle |
| rolling remux exception | runner future removed | failed projection | failed staging retained by existing policy |
| rolling remux success | in-memory future handoff | synchronous finalizer batch | canonical bundle after finalizer |

Phase 0 now emits one `media_scheduler_tick` record per legacy loop with poll
duration/gap, remux depth, permit use, and explicit `unavailable` markers for
the not-yet-existing image/finalizer lanes, DB pool, oldest-ready age, and
segment index. This is baseline instrumentation, not a V2 scheduler claim.

Each Clip delivery and Media finalization also carries:

```text
event_id, request_id, attempt_id, lease_token, replay_job_id,
runtime_epoch_id, stream_session_id, source_id
```

`lease_token` is explicitly NULL with `lease_state=not_applicable_legacy` until
the fenced lease migration exists.

## 5. Artifact And DB Projection Golden

The deterministic media fixture freezes the current DB-first successful bundle
contract:

- only `raw_clip` is published as a long-lived evidence artifact;
- fixture raw clip SHA-256 is
  `0cab1c9617404faf2b24e221e189ca5945813e14d3f766345b09ca13bbe28ffc`;
- one bundle, one artifact, one timeline row, and one overlay row are upserted;
- sidecars remain generation inputs but are not published as retained DB
  artifacts.

The historical failed task exposed a diagnostic contradiction: task reason was
the aggregate string `duration_guard_failed` while its inner duration guard was
`passed`; the actual failing category was another temporal guard. Phase 0 keeps
the legacy clip status for compatibility but writes
`materialization_guard_attribution` and uses its primary reason for the failed
task/event diagnostic. Duration, epoch, sink-window, and time-domain-crop
results remain separately visible.

## 6. Test Baseline And Acceptance

Before modification, the required targeted set passed:

```text
329 passed in 2.64s
```

After Phase 0 instrumentation and characterization were added, the expanded
targeted matrix passed:

```text
353 passed in 2.91s
```

The Phase 0 executable safety net is in:

```text
harness/fixtures/clip_media_phase0/
harness/tests/test_clip_media_legacy_behavior_baseline.py
```

It covers normal, truncated-pre, cross-session-post, missing-proof, duplicate,
capacity, Replay exception/empty response, raw sink, rolling video, watchlist
image, duplicate finalization, corrupt input, late annotation, epoch mismatch,
Replay payload, artifact hash, DB row count, and side-effect inventory cases.

Post-test runtime smoke used the tracked source bind mounts and:

```text
docker compose --env-file infra/env/midterm.env \
  -f infra/docker-compose.midterm.yml up -d \
  --no-build --force-recreate --no-deps clip-worker media-worker
```

Observed after recreate:

- both workers were running with restart count 0;
- Clip's eight consumers connected to Redis and started normally;
- Media connected to PostgreSQL and emitted
  `schema_version=phase0-scheduler-v1` once per scheduler tick;
- the first 12 samples had tick duration 0-10 ms and gap 989-1010 ms in the
  idle legacy runtime, with permit active 0/4;
- worker logs contained no `ERROR`, traceback, or exception lines;
- Redis record-request pending and lag remained 0;
- the Phase 0 dev DB remained at tasks=0, bundles=0, active=0;
- `GET /api/v1/evidence/health` returned `status=ok` and
  `index_source=database`;
- Savant, API, Viewer, evidence media, and face trajectories were not restarted
  or deleted.

Acceptance token:

```text
PASS_CLIP_MEDIA_LEGACY_BEHAVIOR_BASELINE_FROZEN
```

This token proves only Phase 0. It does not claim lifecycle ownership,
Coordinator V2, fenced lease, bounded lanes, connection pool, segment index,
crash recovery, pressure closure, or legacy deletion.
