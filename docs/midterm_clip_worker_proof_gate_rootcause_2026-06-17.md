# Midterm Clip-Worker Proof/Gate Root Cause and Solution

Date: 2026-06-17

This note is a root-cause analysis and solution proposal that builds on
`docs/midterm_lab_alarm_and_60_stream_readiness_findings_2026-06-16.md`. It is an
analysis record, not yet an implemented change. All code claims below were read
directly from the current `c2/post-savant-poc` checkout and are tagged
**[verified]** (read from code) or **[inferred]** (deduced from the observed
runtime metrics and needs a log/DB check before it drives a fix).

## 1. Symptoms (observed, from the prior 20-minute window)

- `clip_worker_deferred=103`
- `missing_post_savant_frame_pts_window=188`
- `final_failed=6`
- `max_concurrent_reached=12`
- `replay_job_created=13`
- `CLIP_WORKER_MAX_CONCURRENT_JOBS=1`, `POST_SAVANT_FRAME_PROOF_ATTEMPTS=1`
- `security.record_requests` lag=0, pending=1 (so this is **not** a Redis
  queue-backlog problem; it is clip-worker internal strategy).

User-visible effect: standing in front of the lab camera does not produce 8090
evidence in real time; successful evidence arrives tens of seconds late, and a
non-trivial fraction fails with `missing_post_savant_frame_pts_window`.

## 2. Correction to the 2026-06-16 findings

The 2026-06-16 findings attributed latency/failure to `CLIP_WORKER_MAX_CONCURRENT_JOBS=1`
plus `FRAME_ANNOTATION_TTL_SECONDS=120`. Both contribute, but the dominant
amplifier was missed: **in the post-savant path the expensive frame-proof wait
runs before the concurrency gate, and it is re-run on every requeue.** This one
ordering bug explains all four observations (slow success, high failure rate,
single-concurrency saturation, and "was provable then failed").

## 3. Root cause (code-level)

Single-threaded consume loop in `services/clip-worker/app/worker.py`. Life of one
post-savant `record_request`:

| Step | Location | Behavior |
| --- | --- | --- |
| First gate | `worker.py:2365` | Called with `active_job_count=0`, so the concurrency limit is **never** enforced here. **[verified]** |
| Proof blocking wait | `worker.py:2397` -> `_prepare_post_savant_replay_request` | Polls Redis for the event+post-window frame, blocking up to the wait budget. **[verified]** |
| Real concurrency gate | `worker.py:2633` | `max_concurrent_jobs` is only checked here, *after* the proof wait. **[verified]** |

### 3.1 `POST_SAVANT_FRAME_PROOF_ATTEMPTS=1` is dead config

In `_prepare_post_savant_replay_request`, when `wait_budget_s > 0` the attempt
count is derived from the budget, overriding `attempts`
(`worker.py:1830-1833`):

```text
attempts = max(1, int(wait_budget_s / poll_interval_s) + 1)
```

`infra/env/midterm.env` does **not** set `POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S`,
so it defaults to **12s** (`config.py:115-117`); poll interval falls back to
`POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S=1.0` (`midterm.env:31`,
`config.py:118-126`). Effective behavior: **~13 polls, ~12s blocking per
request**, not "one attempt". **[verified]**

### 3.2 Requeue re-runs the full proof wait

`max_concurrent_reached` routes to `_queue_clip_request` (`worker.py:2643`), which
updates DB state to `queued` and **does not `xack`** (`worker.py:1135-1178`). The
message stays in the consumer PEL and is reclaimed via `xautoclaim` every
`CLIP_WORKER_PENDING_CLAIM_INTERVAL_S=5` (`worker.py:2299-2312`, claim logic
`923-978`). The reclaimed message re-enters the loop top and **re-runs the ~12s
proof wait** (it is not added to `seen_requests` when queued, so it is not
skipped). **[verified]**

Arithmetic for the observed ~70-90s-to-fail: with `deferred`/requeue cycling,
`12 reclaim cycles x (5s idle + ~12s proof) ~= 60-85s`. **[inferred]** — must be
confirmed against `clip_worker_queued` / `clip_worker_frame_proof_wait` logs
before being used as a fix premise. The alternative contributor is the cooldown
defer path (`CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS=30` x
`CLIP_WORKER_DEFERRED_RETRY_MAX_ATTEMPTS=12`).

### 3.3 Proof miss is an immediate final failure

For post-savant requests, a proof miss does not defer; it final-fails and acks
immediately (`worker.py:2450-2511`, `_fail_clip_request` at `2495`). For a
real-time link this is too aggressive: a post-window annotation that arrives a few
seconds late (forwarder backlog, Savant lag, or a reclaim re-proof landing between
exported frames) becomes a permanent `missing_post_savant_frame_pts_window` instead
of a short wait. **[verified code path]**

**Correction (codex review 2026-06-17, re-verified against code):** an earlier draft
claimed the miss was caused by `FRAME_ANNOTATION_TTL_SECONDS=120` evicting the
annotation. That is wrong. `ttl_seconds` is only written into the message payload
and stream fields (`frame_annotation_builder.py:132`, `frame_annotation_exporter.py:178`)
and validated to 1..3600 (`frame_annotation_builder.py:598-600`); the exporter trims
**only** by `XADD ... MAXLEN ~20000` (`frame_annotation_exporter.py:185-190`) with no
`EXPIRE` and no time-based `MINID`, and clip-worker reads by `xrevrange` count plus a
`min_stream_ms` time *floor* (`worker.py:558`), never by `ttl_seconds`. There is no
120s entry expiry. See 3.6 for the real retention/miss factors. **[verified]**

### 3.4 `active_jobs_until` is a wall-clock guess, not real concurrency

clip-worker does not run the replay (Replay service + `video-file-sink` do). On
job creation it appends `now + pre+post+5 = 15s` to `active_jobs_until`
(`worker.py:2755-2757`). So `max_concurrent_jobs=1` effectively means "after
creating one job, treat the worker as full for 15s", which is why
`max_concurrent_reached` fires so often. **[verified]**

### 3.5 Frame annotations are not person-gated (rules out one hypothesis)

`frame_annotation_exporter.py` exports a message for every processed frame
regardless of detections (`frame_objects` may be empty), subject only to the PTS
throttle `min_interval_ms` (`exporter ~lines 271-354`). So the lab "sparse person
detection" (17-35%) does **not** directly starve the proof stream. The post-window
proof misses come from (a) the frame not yet exported when looked up too early,
(b) trimming by `MAXLEN` / lookback after very late processing (only relevant at
60-stream scale), (c) forwarder drop of lab frames under pressure, and (d) tight
PTS/session tolerances — not from person-gating.
**[verified that export is not person-gated; (a)-(d) are the candidate miss
causes].**

### 3.6 Real retention / proof-miss factors (corrected)

The window in which a post-window proof can be found is bounded by, in order of
current relevance:

- `FRAME_ANNOTATION_REDIS_MAXLEN=20000` (the only trim, via XADD MAXLEN) and
  `FRAME_ANNOTATION_ANCHOR_LOOKBACK_COUNT=20000` (clip-worker scan depth);
- the `min_stream_ms` / `min_frame_uuid_ms` time floors and PTS/session tolerances
  (`FRAME_ANNOTATION_ANCHOR_PTS_TOLERANCE_S=1.0`, runtime_epoch / stream_session match);
- analysis-forwarder drop under pressure (dropped frames never reach Savant, so they
  are never exported);
- Replay encoded-video storage `data_expiration_ttl=300s`
  (`modules/savant_replay/config.midterm.json:44`). Replay stores H.264
  encoded packets (not decoded raw frames) in RocksDB; decoding only happens
  inside the Savant GPU inference pipeline.

At 1-2 streams, `maxlen=20000` @ ~8fps is tens of minutes of history, so it is
**not** the binding constraint today. At 60 streams, `20000 / (60*8) ~= 42s` becomes
the binding window (findings-doc section 6). Practical takeaway: at current scale,
proof misses are a *timing/ordering* problem (looked up too early, or
forwarder-dropped), not an eviction problem.

## 4. Impact

- Evidence latency: event -> ready is tens of seconds even on success.
- Spurious permanent failures: `missing_post_savant_frame_pts_window` final-failed
  immediately on a request whose post-window frame was only seconds from arriving.
- Single-concurrency saturation under any burst, because each request holds the
  one loop slot for up to ~12s of proof polling.

## 5. Solution (ordered per 2026-06-16 findings section 8)

### P0 - clip-worker internal strategy (highest leverage)

1. **Move the proof wait to after the scheduling gate.** Run `schedule_gate`
   first; only requests that can actually create a job now should spend proof-wait
   time. (Reorder `worker.py:2397` proof block vs `worker.py:2633` gate.)
2. **Make a proof miss a bounded re-deferral, not an immediate final fail.** Route
   `missing_post_savant_frame_pts_window` through `_defer_clip_request`
   (`worker.py:1076`) bounded by `deferred_retry_max_attempts`; the retryable
   predicate already exists (`_post_savant_anchor_error_retryable`,
   `worker.py:1055-1073`). Only final-fail when the post-window has aged past TTL
   with still no frame.
3. **Stop holding the loop on proof, or raise real concurrency.** Minimal env
   mitigation (no code): in `infra/env/midterm.env`
   - `CLIP_WORKER_MAX_CONCURRENT_JOBS=4`
   - `POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S=8`
   - `POST_SAVANT_FRAME_PROOF_POLL_INTERVAL_S=0.5`
   Structural fix: decouple proof waiting from the consume loop so a waiting
   request does not block unrelated sources.
4. **Drop/replace the `active_jobs_until` wall-clock guess** so concurrency
   accounting reflects real outstanding replay jobs (or remove it once proof no
   longer blocks the loop).

### P1 - size Redis/Replay against worst-case evidence delay

- Retention is governed by `FRAME_ANNOTATION_REDIS_MAXLEN` + lookback count, not by
  `FRAME_ANNOTATION_TTL_SECONDS` (see the 3.3 correction and 3.6). At 1-2 streams the
  current `20000` is ample; do **not** treat the 120s value as an expiry.
- The binding window must exceed worst-case (proof wait + queue + reclaim + Replay
  consume). Replay raw storage is `data_expiration_ttl=300s`; once P0 cuts
  control-plane delay to seconds, 300s Replay + 20000 maxlen is comfortable at 1-2
  streams.
- For P2 (60 streams) the findings-doc maxlen math holds (`20000/480 ~= 42s`): raise
  maxlen or shard the stream then, and add a real time-based trim (MINID by
  timestamp) if a wall-clock retention guarantee is needed.

### P2 - 60-stream sharding (explicitly deferred)

There is currently **no** `source_id -> shard -> Replay/Savant` routing in code
(repo-wide `shard` search hits only one unrelated script), and clip-worker hard-codes
a single `REPLAY_API_URL` (`config.py:55`). This is findings-doc step 5 and must
not start before P0/P1 and a 30-stream single-T4 soak pass.

## 6. Non-goals

- Not changing Savant inference, pose FPS, or the intrusion rule (`min_inside_ms`)
  in this work item.
- Not implementing shard routing (P2) now.
- Not rewriting media playback/overlay (spec 18 detail boundary stands).

## 7. Config switches and state semantics

Relevant env (current values, `infra/env/midterm.env`): `CLIP_WORKER_MAX_CONCURRENT_JOBS=1`,
`POST_SAVANT_FRAME_PROOF_ATTEMPTS=1` (dead under budget path),
`POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S=1.0`,
`FRAME_ANNOTATION_REDIS_MAXLEN=20000` (the real trim),
`FRAME_ANNOTATION_TTL_SECONDS=120` (**payload metadata only, not an enforced Redis
expiry** — see 3.3),
`CLIP_WORKER_PENDING_CLAIM_INTERVAL_S=5`, `CLIP_WORKER_DEFERRED_RETRY_MAX_ATTEMPTS=12`,
`CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS=30`. Defaults not set in env:
`POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S=12`, `POST_SAVANT_FRAME_PROOF_POLL_INTERVAL_S`
falls back to retry sleep (`config.py:115-126`).

Evidence-state family (`services/clip-worker/app/repository.py:12-20`):
`pending, waiting_proof, queued, replaying, finalizing, ready, failed`. The DB
already records `evidence_state` + `evidence_reason` + `evidence_diagnostics` per
event (`update_clip_status`).

## 8. Already built - do not rebuild

Findings section 7 observability is mostly present in the backend; the gap is
frontend surfacing:

- `GET /api/v1/runtime/overview` already aggregates `va_savant_effective_fps`,
  `va_savant_last_frame_age_seconds`, `va_savant_pose_frames_with_person_total`
  (`services/api/app/services/runtime_overview.py:57-68`), evidence-state
  `GROUP BY` counts + recent + failure lists (`runtime_overview.py:212-351`), and
  container restart rates (`annotate_container_restart_rates`, `:186`).
- Spec 18 `/api/v1/evidence/bundles` already returns `evidence_state`,
  `evidence_reason`, `latest_task_status` (`services/api/app/routers/evidence.py:142-145`)
  and is mounted (`services/api/app/main.py:38`).
- Gap: the operator list renders only `clipStatusLabel(bundle.clip_status)`
  (`services/evidence-viewer/app/static/evidence.js`); it does not surface the
  `evidence_state` / `evidence_reason` already returned by the API. This is a
  frontend wiring task, not a new observability system.

Note: `RAW_CLIP_UNAVAILABLE_STATUSES` in `evidence.py:19-27` lists `pending` and
`queued` but not `waiting_proof` / `replaying` / `finalizing`; review for
consistency when surfacing state.

## 9. Reproduce / verify

Before fix (capture evidence for the codex discussion):

```bash
# clip-worker behavior over a window
docker logs --since 20m video-analytics-midterm-clip-worker 2>&1 \
  | grep -cE "clip_worker_frame_proof_wait|frame_domain_proof_waiting"
docker logs --since 20m video-analytics-midterm-clip-worker 2>&1 \
  | grep -cE "max_concurrent_reached"
docker logs --since 20m video-analytics-midterm-clip-worker 2>&1 \
  | grep -cE "missing_post_savant_frame_pts_window"
docker logs --since 20m video-analytics-midterm-clip-worker 2>&1 \
  | grep -E "clip_worker_frame_proof_wait" | tail -20   # frame_proof_wait_seconds

# record_requests health (expect lag~0)
redis-cli XINFO GROUPS security.record_requests

# evidence-state distribution from DB
psql "$DATABASE_URL" -c "SELECT payload->'media'->>'evidence_state' AS state,
  count(*) FROM events WHERE media_status IS NOT NULL GROUP BY 1 ORDER BY 1;"
```

After fix, expect: `frame_proof_wait_seconds` drops, `max_concurrent_reached`
trends to zero, no `missing_post_savant_frame_pts_window` on streams that are
actively annotating, and event->ready latency in single-digit seconds.

Tests (per CLAUDE.md: update/add harness before changing strategy):
`harness/tests/test_clip_worker_queue_safety.py` (exists) plus a new case
asserting the gate runs before the proof wait and that a proof miss defers rather
than final-fails until TTL.

## 10. Rollback

P0 env-only mitigation is revertible by restoring the four `infra/env/midterm.env`
values and re-running `docker compose -f infra/docker-compose.midterm.yml up -d`.
Code changes are isolated to `services/clip-worker/app/worker.py`; revert the
gate/defer reorder commit. No schema or evidence-format change is implied, so no
data migration to undo.

## 11. Open questions for the codex discussion

1. Confirm the ~70-90s failure path with logs: is it the max_concurrent requeue
   loop (3.2) or the cooldown defer path? Fix differs slightly per case.
2. Is the forwarder dropping lab frames near the post-window (contributing to 3.5c)?
   Need forwarder drop metrics; `services/analysis-forwarder/app/queueing.py` is a
   single bounded drop-on-full queue (`queue_full` at `:57`) - per-source fairness
   is not confirmed present.
3. Decouple proof from the consume loop vs simply raising concurrency: which fits
   the 30/60-stream target better without re-introducing the serialization risk?
4. Should `waiting_proof` / `replaying` / `finalizing` be operator-visible as
   distinct states in the 8090 list, or collapsed into "generating"?
