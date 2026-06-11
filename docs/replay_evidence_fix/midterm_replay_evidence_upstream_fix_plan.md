# Midterm Replay Evidence Upstream Fix Plan

## Status

Date: 2026-06-11

This document freezes the code-verified findings from the replay/evidence
duration investigation and turns them into the next correction plan.

Execution status: the plan has been implemented on branch
`c2/post-savant-poc` in these commits:

- `d997e5e Fix midterm replay runtime foundations`
- `e9f5f82 Tune midterm replay evidence duration`
- `a15cbb9 Prefer frame identity for replay evidence alignment`
- `cfc7059 Isolate midterm frame proofs by stream session`
- `bcd4a0a Harden post-Savant evidence crop safety`

The findings below intentionally preserve the pre-fix baseline that justified
the work. Re-check the current tree before using any "currently has" statement
as live state.

Execution record update: implementation and targeted tests are complete. The
remaining work is runtime acceptance with real evidence samples: verify final
ready clips are near the requested 10 second window, bad sink windows fail
closed, and the 31.28s/61.35s failure shape no longer publishes unsafe raw
clips.

Recommended owner: Codex process A.

Keep this workstream scoped to Replay storage, clip-worker Replay job payloads,
frame-domain proof/session isolation, and evidence-duration validation. Do not
mix it with multi-source runtime capacity work from
`docs/runtime_stability_fix/midterm_multi_source_runtime_stability_plan.md`.

Shared-file warning: this plan touches `infra/docker-compose.midterm.yml` in
Phase 3. Coordinate that file with the runtime-stability workstream, which also
uses the compose file for Savant capacity settings.

## Pre-Fix Verified Facts

The current deployable surface is the midterm stack:

- compose: `infra/docker-compose.midterm.yml`
- env: `infra/env/midterm.env`
- Replay config: `modules/savant_replay/config.midterm.json`
- clip-worker Replay client: `services/clip-worker/app/replay_client.py`
- clip-worker job construction: `services/clip-worker/app/worker.py`
- media-worker post-Savant finalizer: `services/media-worker/app/worker.py`

The following findings were verified against the current working tree.

### Replay RocksDB TTL Is Too Tight

`modules/savant_replay/config.midterm.json` currently has:

```json
"data_expiration_ttl": {"secs": 30, "nanos": 0},
"compaction_period": {"secs": 30, "nanos": 0}
```

This is a real configuration problem for post-Savant evidence jobs. The current
event evidence window is still 5 seconds pre + 5 seconds post. The clip-worker
waits for post-window frame-domain proof before creating the Replay job.

Do not mix up the two retry loops when estimating retention needs:

- Replay keyframe lookup is `KEYFRAME_LOOKUP_RETRIES=8` in the midterm compose,
  which means up to 9 attempts including the first try.
- post-Savant frame proof uses `POST_SAVANT_FRAME_PROOF_ATTEMPTS=30` and
  `POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S=1.0`, so proof wait can still approach
  30 seconds.

That means old pre-window frames can age out of Replay storage before the job is
even created.

Important nuance: `CLIP_WORKER_MAX_CONCURRENT_JOBS=1` and
`CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS=30` do not create a normal queue for
non-priority requests; the clip-worker skips gated requests. The TTL risk is
still real, but the stronger current explanation is proof wait + short Replay
retention, not a guaranteed request queue.

### Replay Delivery Duration Is Hard-Coded

`services/clip-worker/app/replay_client.py` builds every Replay payload with:

```python
"max_delivery_duration": {"secs": 30, "nanos": 0},
```

`services/clip-worker/app/worker.py` can calculate `replay_duration_seconds`
above 30 seconds after accounting for rewind offset, decoder keyframe coverage,
and `REPLAY_DURATION_EXTRA_SLACK_S`.

This is consistent with observed bad clips near 31.28 seconds. A Replay job can
be cut off by the 30 second delivery ceiling while the evidence request still
expects a longer Replay output that media-worker can crop back to the 10 second
event window.

### Replay Duration Slack Is Large

`infra/docker-compose.midterm.yml` defaults:

```yaml
REPLAY_DURATION_EXTRA_SLACK_S: "${REPLAY_DURATION_EXTRA_SLACK_S:-15}"
```

The extra slack helps guarantee coverage, but it also makes Replay emit more
than the target window. Once media-worker crop fails, the previous unsafe
behavior was to publish an overlong raw clip. Current media-worker code is
already moving toward fail-closed behavior for failed time-domain crop, but the
upstream Replay job should still avoid unnecessary overrun.

### Constant Cadence Is Still Applied In The Primary Payload

`REPLAY_FORCE_CONSTANT_CADENCE=false` currently affects fallback payload
selection only. The primary payload still includes:

```python
"ts_discrepancy_fix_duration": frame_duration,
"min_duration": frame_duration,
"max_duration": frame_duration,
```

with `frame_duration` derived from `REPLAY_FPS`, currently defaulting to 24 in
the midterm compose. If the source is actually 25 or 30 fps, forcing a 24 fps
cadence can create time-axis drift unless Replay output metadata remains in the
original PTS domain.

This is a plausible chronic alignment risk, but it is not yet proven to be the
primary cause of the 31.28s or 61.35s bad clips.

### Evidence Alignment Must Be Frame-UUID First

Evidence identity alignment should be based on frame identity, not naked time
offsets.

Correct priority:

1. exact `frame_uuid` / `uuid` match in sink metadata
2. same-domain `frame_pts` window after runtime/session/source filtering
3. wall-clock or relative time offset only as a diagnostic fallback

Cadence can still matter for video trimming and PTS continuity, but it must not
be the primary identity binding. Before blaming cadence drift, confirm the
media-worker and 8090 viewer path use frame UUID or same-domain PTS mapping
rather than raw elapsed-time offset.

### Runtime Epoch Does Not Detect Source-Adapter PTS Reset

The current runtime epoch is created by controlled runtime apply/restart and by
video-file-sink startup. The frame annotation exporter reads the current epoch
from env/state, but a source-adapter automatic restart does not create a new
per-source stream session.

Current guards filter by `runtime_epoch_id`, `source_id`, and `camera_id`, but
they do not include a per-source `stream_session_id`. If a source adapter
restarts and PTS returns to a lower value under the same runtime epoch, stale and
current PTS domains can still be mixed.

This is a real design gap. The exact claim that a 61.35s clip is two 30 second
segments stitched across a PTS reset remains a hypothesis until runtime metadata
proves it.

### Media-Worker Already Has A Fail-Closed Direction

`docs/midterm_replay_intrusion_clip_duration_diagnosis.md` identifies the direct
published-artifact failure: media-worker used to copy full Replay output into
`raw_clip.mov` after time-domain crop failure.

Current working-tree code now deletes or avoids the raw clip on failed
time-domain crop and marks the bundle as duration-guard failed. That protects
evidence publication, but it does not fix the upstream Replay/storage/session
causes listed above.

## Fix Plan

### Phase 1: Increase Replay Storage Retention

Change `modules/savant_replay/config.midterm.json`:

- `data_expiration_ttl`: 30s -> 300s or 600s
- `compaction_period`: 30s -> 120s, or another value that does not erase the
  needed pre-window during normal proof waits

Prefer matching older validated active configs that used 300s TTL and 120s
compaction, unless current disk budget requires a different value.

Add or update a contract test if there is already a midterm deployment contract
for Replay config. If no suitable test exists, add one narrow test that asserts
the active midterm Replay TTL is not below the expected evidence retention.
RocksDB TTL expiration is applied during compaction, so tests should assert the
configured TTL lower bound, not an exact data-survival wall-clock duration. With
TTL=300s and compaction=120s, practical retention is approximately 300-420s.

Validation:

```bash
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
pytest -q harness/tests/test_midterm_deployment_contract.py
```

### Phase 2: Make `max_delivery_duration` Dynamic

Update `services/clip-worker/app/replay_client.py` so
`max_delivery_duration` is derived from the actual planned Replay duration.

Recommended rule:

```text
max_delivery_duration >= ceil(expected_seconds + delivery_slack_s)
```

Use at least 10 seconds delivery slack at first. Preserve a minimum value of 30
seconds only if needed for short jobs:

```text
max_delivery_duration = max(30, ceil(expected_seconds + 10))
```

`expected_seconds` already follows `duration_seconds_override` when supplied, so
the dynamic value should use the same effective duration used by
`stop_condition`.

`ts_sync=true` makes delivery wall-clock time track the expected Replay
duration. Keep the delivery slack tied to `anchor_wait_duration` and
`max_idle_duration`: the initial 10 second slack is intended to cover the 1
second anchor wait plus one idle pause near the current 10 second idle ceiling.
If `max_idle_duration` grows later, revisit this formula.

Add unit coverage around `build_job_payload()` for:

- 10 second default window keeps at least 30 seconds delivery duration
- 36 second override produces delivery duration greater than 36 seconds
- fallback payloads preserve the same dynamic delivery duration

### Phase 3: Reduce Replay Extra Slack After Dynamic Delivery Is Safe

After Phase 2 is covered, reduce the midterm default:

```text
REPLAY_DURATION_EXTRA_SLACK_S=15 -> 3 or 5
```

Do not remove slack completely until runtime smoke proves post-window coverage is
still reliable under event_keyframe anchoring.

Validation should inspect Replay job labels and sink metadata:

- `replay_duration_seconds`
- whether the `anchor_keyframe_pts <= requested_start_pts` guard branch
  contributed the extra `pre+post+1` seconds
- `requested_start_pts`
- `requested_end_pts`
- actual sink metadata first/last PTS
- final `raw_clip.mov` duration
- `time_domain_crop_applied`
- `duration_guard_failed`

### Phase 4: Decide Constant-Cadence Policy With Evidence

First add a test that documents the current behavior:

- primary payload always carries `min_duration` and `max_duration`
- `REPLAY_FORCE_CONSTANT_CADENCE=false` only controls fallback construction

Then choose one of these implementation paths, with option 1 preferred unless
runtime evidence shows the current Replay API cannot support it:

1. Allow primary non-cadence payload when Replay accepts it, and keep
   constant-cadence fallback for older Replay behavior.
2. Use real source fps for `REPLAY_FPS`.
3. Keep cadence fields but stop binding them to a guessed 24 fps default.

Do not remove the compatibility fallback blindly. Prior validation showed older
Replay behavior could reject a non-cadence request and require a cadence retry.

Runtime validation must compare source fps, Replay payload fps, sink metadata
PTS, final clip duration, and the 8090 viewer mapping path. A sample is not
useful for cadence attribution until viewer-side naked time-offset conversion has
been ruled out.

### Phase 5: Add Per-Source Stream Session Isolation

Introduce a per-source `stream_session_id` generated in the Savant-side frame
annotation path.

The session rule must account for Savant container restarts as well as adapter
PTS rollback:

- when a frame annotation exporter instance first sees a source, assign a new
  session id for that source
- when `current_frame_pts < last_frame_pts - rollback_threshold_ns`, bump the
  session id for that source
- do not bump on large forward jumps; forward jumps can be normal long stalls
  and should not be treated as proof of a new session

Suggested detection:

```text
if source first seen by this exporter instance:
    stream_session_id = new stable per-source session id
elif current_frame_pts < last_frame_pts - rollback_threshold_ns:
    stream_session_id = new stable per-source session id
```

The ID must be carried through:

- frame annotation message
- record_request / Replay labels when applicable
- clip-worker frame-domain proof lookup
- media-worker frame cache sidecar and freshness guard

Frame-domain proofs should be considered same-domain only when:

```text
(runtime_epoch_id, source_id, stream_session_id)
```

matches.

This is larger than the TTL/delivery fix. It should be implemented after the
short-retention and delivery-ceiling issues are corrected, unless new runtime
evidence proves PTS rollback is the dominant current failure.

## Acceptance Criteria

A fix in this workstream is not complete until it proves both safety and
alignment:

- failed crop does not publish `raw_clip.mov`
- ready bundles have final raw clip duration near 10 seconds
- bad sink windows are marked fail-closed with `raw_clip_path=""`
- Replay jobs for normal 10 second evidence no longer hit delivery ceiling
- sink metadata selected for crop overlaps `requested_start_pts..requested_end_pts`
- sink metadata and viewer alignment prefer exact `frame_uuid` / `uuid` matches
  before falling back to same-domain PTS
- cadence-drift samples have first ruled out 8090 viewer naked time-offset
  conversion
- frame annotation and media-worker sidecar both use the same runtime/session
  domain

Suggested targeted checks:

```bash
pytest -q \
  harness/tests/test_midterm_replay_evidence_duration_guard.py \
  harness/tests/test_midterm_replay_epoch_isolation.py

docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
```

Runtime evidence to collect after implementation:

```bash
docker logs --tail 300 video-analytics-midterm-clip-worker
docker logs --tail 300 video-analytics-midterm-media-worker
docker logs --tail 300 video-analytics-midterm-replay-service
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 <raw_clip.mov>
```

## Do Not Treat As Proven Yet

These are plausible but not yet confirmed root causes:

- 61.35s clips being exactly two 30s delivery windows joined across PTS reset
- constant-cadence drift being the primary cause of the observed 31.28s/61.35s
  failures
- clip-worker concurrency/cooldown creating a normal long queue

The current highest-confidence fixes are:

1. Replay TTL/compaction retention.
2. Dynamic `max_delivery_duration`.
3. Lower replay extra slack after dynamic delivery is safe.
4. Frame-UUID-first validation of media-worker and 8090 viewer alignment.
5. Per-source stream session isolation.
