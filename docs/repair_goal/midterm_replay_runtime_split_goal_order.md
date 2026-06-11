# Midterm Replay Runtime Split Goal Order

## Status

Date: 2026-06-11

This is the execution packet for splitting the next work into goal-sized runs.
It coordinates:

- Replay/evidence upstream fixes:
  `docs/replay_evidence_fix/midterm_replay_evidence_upstream_fix_plan.md`
- multi-source runtime stability fixes:
  `docs/runtime_stability_fix/midterm_multi_source_runtime_stability_plan.md`

Execution status: Goals 1-5 have been implemented on branch
`c2/post-savant-poc` in this order:

1. `d997e5e Fix midterm replay runtime foundations`
2. `e751c51 Gate midterm runtime sources on Savant readiness`
3. `e9f5f82 Tune midterm replay evidence duration`
4. `a15cbb9 Prefer frame identity for replay evidence alignment`
5. `cfc7059 Isolate midterm frame proofs by stream session`

Keep this document as the audit trail for why the work was split this way. The
remaining acceptance item that still needs a live deployment window is the
two-source long-run plus active source-adapter restart fault injection from
Goal 2 / runtime Phase 4.

## Non-Negotiable Invariants

Evidence alignment is frame identity first:

1. exact `frame_uuid` / `uuid`
2. same-domain `frame_pts`
3. time offset only as diagnostic fallback

Savant stream capacity is source-count derived, not manually locked:

```text
recommended_max_parallel_streams = max(2, enabled_rtsp_source_count * 2)
```

For the current two enabled RTSP sources, the recommendation is 4, but the code
should not make 4 the permanent model.

## Shared Files And Locking

Only one Codex process should edit a shared file at a time.

Shared files:

- `infra/docker-compose.midterm.yml`
  - runtime plan Phase 1 changes Savant capacity/env configurability
  - Replay plan Phase 3 changes `REPLAY_DURATION_EXTRA_SLACK_S`
- `modules/savant_security/`
  - runtime plan may inspect Savant logs/readiness behavior
  - Replay plan Phase 5 changes frame annotation `stream_session_id`

Preferred order for shared-file work:

1. runtime plan Phase 1 owns `infra/docker-compose.midterm.yml`
2. Replay plan Phase 3 may later edit `infra/docker-compose.midterm.yml`
3. Replay plan Phase 5 must wait until runtime readiness/capacity work is done
   or explicitly paused

## Goal Order

### Goal 1: Low-Risk Runtime And Replay Foundation

Owner: one Codex process, or two processes with the shared-file lock respected.

Steps:

1. Runtime Phase 1:
   derive/report `MAX_PARALLEL_STREAMS` from enabled source count, make compose
   configurable, keep `BATCH_SIZE=1`.
2. Replay Phase 1:
   set Replay RocksDB TTL/compaction to the retained-window target.
3. Replay Phase 2:
   make `max_delivery_duration` dynamic and add focused tests.

Goal 1 should not change:

- runtime apply readiness ordering
- constant-cadence strategy
- `stream_session_id`
- media-worker crop policy beyond tests required by the changed code

Validation for Goal 1:

```bash
pytest -q harness/tests/test_midterm_deployment_contract.py
pytest -q harness/tests/test_midterm_replay_evidence_duration_guard.py
pytest -q harness/tests/test_midterm_replay_epoch_isolation.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
git diff --check
```

### Goal 2: Runtime Restart Safety

Owner: runtime-stability Codex process.

Steps:

1. Runtime Phase 2:
   wait for Savant readiness after restart and before source adapters start.
   Use a 300 second default timeout.
2. Runtime Phase 3:
   preserve source lifecycle diagnostics in apply responses/logging.
3. Runtime Phase 4:
   run the two-source long-run and active source-adapter restart fault injection.

Validation for Goal 2:

```bash
pytest -q harness/tests/test_camera_runtime_apply_service.py
pytest -q harness/tests/test_midterm_deployment_contract.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
```

Runtime acceptance must include:

```bash
docker restart video-analytics-source-source_00000000-0000-4000-8000-781078565686
docker logs --tail 300 video-analytics-midterm-savant | \
  rg 'source_00000000-0000-4000-8000-781078565686|primary_rtsp|ERROR|Exception|Traceback|pad|streammux'
```

### Goal 3: Evidence Duration Tuning

Owner: Replay/evidence Codex process.

Prerequisite: Goal 1 complete; Goal 2 preferably complete.

Steps:

1. Replay Phase 3:
   lower `REPLAY_DURATION_EXTRA_SLACK_S` only after dynamic delivery duration is
   in place.
2. Add or expose diagnostics showing whether the
   `anchor_keyframe_pts <= requested_start_pts` guard branch contributed
   `pre+post+1` seconds.
3. Collect runtime samples showing final ready raw clips near the 10 second
   requested window and failed crops fail closed.

Validation:

```bash
pytest -q harness/tests/test_midterm_replay_evidence_duration_guard.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
```

### Goal 4: Frame-UUID Alignment And Cadence Attribution

Owner: Replay/evidence Codex process.

Prerequisite: evidence clips can be generated safely after Goals 1-3.

Steps:

1. Confirm media-worker and 8090 viewer alignment prefer exact `frame_uuid` /
   `uuid` matches before PTS and never depend on naked elapsed-time offset as the
   primary evidence binding.
2. Add behavior tests that freeze the current cadence payload behavior.
3. Prefer a non-cadence primary Replay payload with constant-cadence fallback, if
   runtime Replay accepts the primary payload.

Stop condition: if viewer-side naked offset conversion is found, fix or isolate
that first before attributing visual drift to cadence.

### Goal 5: Stream Session Isolation

Owner: Replay/evidence Codex process, after coordinating with runtime owner.

Steps:

1. Add per-source `stream_session_id` in the Savant frame annotation exporter.
2. Generate a new session for each source first seen by an exporter instance.
3. Bump session only on PTS rollback, not on forward jumps.
4. Carry the session through frame annotation, record request/Replay labels,
   clip-worker proof lookup, and media-worker sidecar filtering.

Acceptance:

- Savant restart under the same runtime epoch causes old and new frame-domain
  proofs not to mix.
- Source-adapter PTS rollback under the same runtime epoch causes a new session.
- Missing/mismatched session fails closed rather than publishing an unsafe clip.

## Recommended Sequence

Use this order unless live evidence proves a later phase is now the blocker:

```text
Goal 1 -> Goal 2 -> Goal 3 -> Goal 4 -> Goal 5
```

If using two Codex processes:

- process B starts Goal 1 step 1 and then Goal 2
- process A starts Goal 1 steps 2-3 after process B releases compose, then
  continues with Goals 3-5

Do not run two processes that both edit `infra/docker-compose.midterm.yml` at
the same time.
