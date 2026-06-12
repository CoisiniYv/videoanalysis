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
two-source 10-15 minute long-run from Goal 2 / runtime Phase 4. One active
restart fault injection of the dynamic source adapter has already been run after
a controlled runtime restart and passed the inspected checks.

Current completion summary:

- Replay/evidence plan: implementation and targeted tests complete; real
  evidence-sample acceptance still needs to confirm 10 second ready clips and
  fail-closed handling of bad sink windows.
- multi-source runtime plan: implementation and one active reattach fault
  injection complete; the full 10-15 minute two-source long-run still needs to
  be executed and recorded.
- a later two-source runtime run exposed a separate Savant v0.6.0 source-reset
  race. Non-monotonous PTS removed a source registry entry while stale buffers
  were still in flight, causing `KeyError` in Savant framework code and leaving
  the container Up/unhealthy. This is now tracked as Goals 6-7 below.
- runtime-generated `modules/savant_security/config/cameras.midterm.yml`
  `runtime_epoch_id` changes are deployment state and should not be committed as
  code-plan evidence.

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

### Goal 6: Savant Source-Reset Crash Patch

Owner: runtime-stability Codex process.

New evidence: `savant-crash-fix-20260611.zip` was reviewed against the current
running `savant-deepstream:0.6.0-7.1` image. Its framework overlay patches are
acceptable in principle because they only guard the four verified
`self._sources.get_source(...)` call sites that can receive stale buffers after
PTS-reset source removal:

- `deepstream/buffer_processor.py`
- `deepstream/nvinfer/processor.py`
- `deepstream/pipeline.py` output metadata path
- `deepstream/pipeline.py` late/duplicate muxer EOS path

Steps:

1. Add the pinned v0.6.0 overlay patch files under
   `modules/savant_security/savant_patches/`.
2. Run the patch applier from the `savant-security` entrypoint before
   `python -m savant.entrypoint`.
3. Keep md5 enforcement fail-loud so future Savant image changes cannot be
   silently patched with the wrong overlay.
4. Recreate Savant and verify patched md5 values inside the container.
5. Validate a source reset no longer stops the Savant module.

Validation:

```bash
python -m py_compile modules/savant_security/savant_patches/apply_patches.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
docker logs video-analytics-midterm-savant 2>&1 | rg 'savant_patches|video-analytics patch'
```

### Goal 7: API-Owned Savant Recovery Supervisor

Owner: runtime-stability Codex process.

Goal 7 is a recovery net after Goal 6, not a replacement for the framework
guard. It should restart Savant and source adapters if the module enters
STOPPING/STOPPED or if frame annotations stall while sources are running.

The reviewed zip's standalone `docker:27-cli` watchdog is not the final
deployment shape. Recovery belongs in the API service behind the 8090 management
plane, using the existing Docker socket client and Redis client. Source adapter
discovery must restart both naming families:
`video-analytics-midterm-source-adapter` and `video-analytics-source-*`.

Validation:

```bash
python -m py_compile services/api/app/services/savant_supervisor.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
docker exec video-analytics-midterm-savant sh -c 'echo stopped > /opt/savant/status.txt'
curl --noproxy '*' -X POST http://127.0.0.1:8090/api/v1/cameras/runtime/supervisor/recover
```

## Recommended Sequence

Use this order unless live evidence proves a later phase is now the blocker:

```text
Goal 1 -> Goal 2 -> Goal 3 -> Goal 4 -> Goal 5 -> Goal 6 -> Goal 7
```

If using two Codex processes:

- process B starts Goal 1 step 1 and then Goal 2
- process A starts Goal 1 steps 2-3 after process B releases compose, then
  continues with Goals 3-5
- after the source-reset crash finding, process B should resume with Goal 6
  before re-running the two-source long-run; process B should add Goal 7 only
  after Goal 6 validates cleanly

Do not run two processes that both edit `infra/docker-compose.midterm.yml` at
the same time.
