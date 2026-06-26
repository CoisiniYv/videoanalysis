# 19_clip_worker_proof_gate_scheduling_fix.md

## 1. Goal

Make midterm intrusion/watchlist evidence become `ready` within single-digit
seconds and stop spurious `missing_post_savant_frame_pts_window` failures, by
fixing the clip-worker scheduling order:

- the real concurrency/scheduling gate runs **before** the post-Savant frame-proof
  wait;
- a retryable proof miss **defers (bounded)** instead of final-failing;
- a reclaimed request does **not** re-run a long proof wait.

This is a control-plane change in clip-worker plus a small 8090 frontend surface.
It does not change Savant inference, the intrusion rule, Replay storage, the
evidence format, or shard topology.

## 2. Background and verified root cause

Full analysis: `docs/midterm_clip_worker_proof_gate_rootcause_2026-06-17.md`
(which builds on `docs/midterm_lab_alarm_and_60_stream_readiness_findings_2026-06-16.md`).

Verified in `c2/post-savant-poc` (`services/clip-worker/app/worker.py` unless noted):

- proof wait runs before the real gate: `:2365` (gate with `active_job_count=0`,
  never enforces concurrency) -> `:2397` (proof wait) -> `:2633` (real gate);
- proof attempts are budget-derived, so `POST_SAVANT_FRAME_PROOF_ATTEMPTS=1` is dead
  config; default budget `12s` @ `1s` poll => ~13 polls / ~12s blocking
  (`config.py:115-126`, `worker.py:1830-1833`);
- `max_concurrent_reached` -> `_queue_clip_request` writes DB `queued` and does
  **not** `xack` (`:1135`, `:2643`); the message is reclaimed via `xautoclaim` every
  `5s` (`:2305`) and re-runs the full proof wait;
- a proof miss final-fails and acks immediately (`:2450-2511`, `_fail_clip_request`
  at `:2495`);
- `active_jobs_until = now + pre+post+5` (15s) is a wall-clock guess, not real
  Replay/`video-file-sink` completion (`:2755-2757`).

Correction carried from the codex review (re-verified): frame-annotation retention
is bound by `XADD MAXLEN` (`frame_annotation_exporter.py:185-190`) and the
clip-worker lookback count, **not** by `FRAME_ANNOTATION_TTL_SECONDS`, which is
payload metadata only. At 1-2 streams `maxlen=20000` is not the bottleneck, so
today's proof misses are a timing/ordering problem, not eviction.

## 3. Non-goals

- No shard routing / multi-Replay; `REPLAY_API_URL` stays single. That is the
  separate 60-stream goal and must not start before this spec + a 30-stream
  single-T4 soak pass.
- No change to Savant, pose FPS, `min_inside_ms`, or the evidence schema.
- No media-playback/overlay rewrite (spec 18 detail boundary stands).
- Replacing `active_jobs_until` with true Replay/sink completion tracking is out of
  scope here (follow-up in section 9).

## 4. Target behavior

Per-request order in the consume loop becomes:

```text
read / reclaim request
  -> dedupe (seen_requests)
  -> resolve event_type / camera / pre,post
  -> SCHEDULE GATE (real active_job_count, cooldown, max_jobs)
       max_concurrent_reached -> queue (no ack, reclaim later)   [NO proof wait]
       cooldown               -> bounded defer                    [NO proof wait]
  -> (slot available) POST-SAVANT FRAME PROOF WAIT (short, bounded budget)
       proof found                          -> create Replay job
       retryable miss + proof budget left   -> bounded defer (reclaim retries)
       non-retryable OR proof budget spent  -> final fail (with diagnostics)
```

Invariants this spec must enforce (and test):

1. A request blocked by concurrency or cooldown never enters the proof wait.
2. Each delivery's proof wait is short (<= a few seconds); total waiting is spread
   across reclaim cycles via bounded defer, not one long in-loop block.
3. A retryable proof miss (`_post_savant_anchor_error_retryable`, `worker.py:1055`)
   defers up to its proof-retry budget before final failure.
4. The proof-retry budget is tracked **separately** from the Redis delivery count,
   so concurrency requeues do not consume the proof-retry budget (see Phase B).

## 5. Implementation plan

### Phase A - gate before proof (`services/clip-worker/app/worker.py`)

- Prune `active_jobs_until` and compute the real `active_job_count`, then run
  `_clip_gate_decision` **before** the `if post_savant_media_request:` block.
- `max_concurrent_reached`: `_queue_clip_request(...)` then `continue` — unchanged
  queue semantics, but now reached without a proof wait.
- `cooldown`: `_defer_clip_request(...)` then `continue`, no proof wait.
- Only when the gate allows do we call `_prepare_post_savant_replay_request`.
- Remove the misleading early `active_job_count=0` gate (`:2365`) or fold it into the
  single real gate.

### Phase B - proof miss becomes bounded defer

- When `_prepare_post_savant_replay_request` returns an `anchor_error`:
  - if `_post_savant_anchor_error_retryable(req, anchor_error)` **and** the
    proof-retry budget is not exhausted -> `_defer_clip_request(..., reason="missing_post_savant_frame_proof")`
    (DB stays `waiting_proof`/pending, no ack, reclaim retries);
  - else -> `_fail_clip_request(...)` with diagnostics (current behavior).
- Track the proof-retry count in `evidence_diagnostics` (persisted via
  `update_clip_status`), NOT via the Redis `times_delivered` count, because that
  counter also increments on concurrency requeue. This keeps invariant 4.
- Keep emitting `evidence_state=waiting_proof` on each wait tick (already done by
  `_mark_waiting_proof`, `:2402`).

### Phase C - short, single-shot proof on reclaim

- Bound per-delivery proof occupancy using `retry_count`:
  - first delivery (`retry_count == 0`): budget = `POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S`
    (small, e.g. 3s);
  - reclaim delivery (`retry_count > 0`): single-shot check (attempts=1, no sleep);
    defer on miss.
- Result: no delivery blocks the loop for more than the small budget, and a reclaim
  never repeats a long wait. With `PENDING_CLAIM_INTERVAL_S=5` and
  `DEFERRED_RETRY_MAX_ATTEMPTS=12`, the worst-case proof window is ~60s, well inside
  Replay `data_expiration_ttl=300s`.

### Phase D - env defaults (`infra/env/midterm.env`)

- `CLIP_WORKER_MAX_CONCURRENT_JOBS=4`
- `POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S=3`
- `POST_SAVANT_FRAME_PROOF_POLL_INTERVAL_S=0.5`
- keep `CLIP_WORKER_DEFERRED_RETRY_MAX_ATTEMPTS=12`,
  `CLIP_WORKER_PENDING_CLAIM_INTERVAL_S=5`.
- Drop the now-dead `POST_SAVANT_FRAME_PROOF_ATTEMPTS`, or annotate it as legacy in
  the env file so it is not mistaken for a live knob.

### Phase E - 8090 surfacing (frontend only)

- `services/evidence-viewer/app/static/evidence.js`: render `bundle.evidence_state`
  and `bundle.evidence_reason` (already returned by `evidence.py:142-143`) alongside
  `clip_status` (`:370`) as a state badge + reason tooltip. Group
  `waiting_proof / queued / replaying / finalizing` as "generating", show `failed`
  with its reason, `ready` as today.
- Review `RAW_CLIP_UNAVAILABLE_STATUSES` (`evidence.py:19-27`) so it also covers
  `waiting_proof / replaying / finalizing` for list correctness.
- No backend/API change required; `runtime_overview` and the evidence router already
  expose the data.

## 6. State semantics

The evidence-state family is unchanged (`repository.py:12-20`):
`pending, waiting_proof, queued, replaying, finalizing, ready, failed`. This spec
changes only **when** a request enters `waiting_proof` (after the gate, not before)
and whether a proof miss goes to `failed` (only when non-retryable or proof-retry
exhausted) versus back to `waiting_proof`/`queued` for another reclaim.

## 7. Tests and acceptance

Extend `harness/tests/test_clip_worker_queue_safety.py`:

1. a request hitting `max_concurrent_reached` does NOT invoke the proof-wait path
   (assert `_prepare_post_savant_replay_request` / the frame-annotation Redis lookup
   is not called) and is left un-acked (queued);
2. a retryable proof miss with budget left -> deferred (DB `waiting_proof`/pending,
   no ack), not `failed`;
3. proof-retry budget exhaustion -> `failed` with diagnostics;
4. per-delivery proof wait is bounded; a reclaim delivery does a single-shot check;
5. concurrency requeues do not consume the proof-retry budget (invariant 4).

Static / contract:

```bash
python -m pytest -q harness/tests/test_clip_worker_queue_safety.py
python -m pytest -q harness/tests/test_midterm_deployment_contract.py
docker compose -f infra/docker-compose.midterm.yml config
git diff --check
```

Runtime (when a midterm stack with the lab camera is available): event->ready
latency single-digit seconds; `max_concurrent_reached` does not co-occur with
`clip_worker_frame_proof_wait`; no `missing_post_savant_frame_pts_window` final-fail
on an actively-annotating stream. Capture before/after greps from the root-cause
doc section 9.

## 8. Rollback

- Phase D is env-only: restore the values and re-run
  `docker compose -f infra/docker-compose.midterm.yml up -d`.
- Phases A-C are isolated to `services/clip-worker/app/worker.py`; revert the commit.
- Phase E is isolated to `evidence.js` (+ optional `evidence.py` constant).
- No schema/format migration, so nothing to undo on the data side.

## 9. Remaining risk / follow-up

- `active_jobs_until` is still a wall-clock estimate; under bursts it can over- or
  under-count outstanding Replay jobs. Follow-up: track real completion from
  media-worker / sink. Not in this spec.
- analysis-forwarder drop is a separate proof-miss contributor and needs per-source
  drop metrics; the forwarder is a single bounded drop-on-full queue
  (`services/analysis-forwarder/app/queueing.py:57`).
- 60-stream maxlen window (~42s) and shard routing remain the next goal.
- Confirm the ~70-90s failure path (concurrency requeue vs cooldown defer) from logs
  before claiming this spec fully closes it (root-cause doc 3.2 is `[inferred]`).

## 10. Implementation status

Not started. Analysis verified and frozen in the root-cause doc on 2026-06-17;
codex cross-review corrected the `FRAME_ANNOTATION_TTL_SECONDS` claim (no entry
expiry). Update this section with commit hash, validation window, and remaining
items after the change lands and passes.
