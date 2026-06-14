# Midterm Multi-Source Runtime Stability Plan

## Status

Date: 2026-06-11

This document freezes the multi-source runtime findings from the current
midterm checkout and turns them into a separate correction plan.

Execution status: the code/config hardening phases in this plan have been
implemented on branch `c2/post-savant-poc` in these commits:

- `d997e5e Fix midterm replay runtime foundations`
- `e751c51 Gate midterm runtime sources on Savant readiness`

Runtime acceptance status:

- code/config hardening phases are complete
- one active restart fault injection of the dynamic source adapter was run after
  a controlled runtime restart; both enabled sources resumed frame annotations,
  Savant stayed healthy, and no new Replay send timeout or Savant pad/streammux
  error was observed in the inspected logs
- the full 10-15 minute two-source long-run window still needs to be executed
  and recorded
- a later live run exposed a separate Savant v0.6.0 source-reset crash:
  non-monotonous PTS caused source reset, stale in-flight buffers hit unguarded
  `self._sources.get_source(...)` calls, Savant module entered STOPPED while the
  container stayed Up/unhealthy, and Replay/source adapters then entered send
  timeout/restart loops
- a later 2026-06-14 read-only diagnosis confirmed a separate throughput
  issue: source adapters still push full RTSP cadence into Replay while
  `MAX_FPS=8/1` is applied only inside Savant's ingress frame filter. Replay
  out_stream/source-adapter ZeroMQ backpressure is now a proven production
  blocker, not just a hypothesis. Details are in
  `docs/repair_goal/midterm_alarm_frequency_backpressure_findings_2026-06-13.md`.

The only uncommitted runtime state observed after this work was
`modules/savant_security/config/cameras.midterm.yml`, where controlled runtime
restart wrote the live `runtime_epoch_id`. That file is deployment state and is
not part of this plan commit.

Recommended owner: Codex process B.

Keep this workstream scoped to multi-source Savant capacity, runtime apply
ordering, dynamic source-adapter lifecycle, and runtime diagnostics. Do not mix
it with Replay evidence retention/job-duration work from
`docs/replay_evidence_fix/midterm_replay_evidence_upstream_fix_plan.md`.

Shared-file warning: this plan touches `infra/docker-compose.midterm.yml` in
Phase 1. Coordinate that file with the Replay/evidence workstream, which also
uses the compose file for Replay duration slack.

## Pre-Fix Verified Facts

The current runtime has two enabled RTSP sources in
`infra/generated/sources.generated.yml`:

- `primary_rtsp`
- `source_00000000-0000-4000-8000-781078565686`

The current Savant service config in `infra/docker-compose.midterm.yml` is
statically locked to:

```yaml
BATCH_SIZE: "1"
MAX_PARALLEL_STREAMS: "2"
```

That means the runtime has no stream-capacity headroom when both enabled
sources are active. This is a real operational risk.

The current code/config does not prove that zero headroom is the root cause of
any specific stop. A read-only runtime check on 2026-06-11 showed:

- `video-analytics-midterm-savant` running and healthy
- both source adapters running with `RestartCount=0`
- Savant logs showing both sources still producing ticks
- no recent pad/streammux error in the inspected log tail

Therefore, `MAX_PARALLEL_STREAMS=2` should be treated as a likely capacity risk,
not a proven stop root cause. The final fix should not be a new manual lock such
as `2 -> 4`; capacity should be derived from enabled RTSP source count with
headroom.

## Runtime Apply Ordering

`services/api/app/services/runtime_apply.py` currently performs controlled
runtime apply in this order:

1. stop source-adapter containers
2. stop worker containers
3. create runtime epoch state
4. write generated camera/source configs
5. recreate `video-file-sink`
6. restart Replay
7. restart Savant
8. start workers
9. start enabled sources

This ordering is mostly correct because sources start after Replay and Savant
restart. The missing hardening is that runtime apply does not wait for Savant to
be actually ready before starting sources. If Savant is still building/loading
models, source adapters can enter retry/timeout behavior during the unavailable
window.

The plan below adds capacity headroom and a readiness gate without broadening
the evidence pipeline changes.

## 2026-06-11 Savant Source-Reset Crash Root Cause

A later runtime failure froze evidence production after approximately
2026-06-11 16:50:51 Asia/Shanghai. The containers did not all exit. Instead:

- `video-analytics-midterm-savant` stayed running but became unhealthy.
- `security.frame_annotations` stopped advancing at the failure time.
- Replay logs repeated `Send timeout` while sending to Savant.
- both source adapters repeatedly restarted because their ZMQ writes backed up
  behind Replay/Savant.

The first fatal error in Savant was not a pad-capacity error. It was a
source-reset race in the Savant v0.6.0 framework:

1. A source delivered non-monotonous PTS. This is expected for looping movie
   RTSP streams such as the current `primary_rtsp` 1080movie input, and can also
   happen on RTSP reconnects.
2. Savant's demux/decode path resets the source on timestamp reset and removes
   the source registry entry.
3. Stale buffers for that source were still queued in muxer / nvinfer stages.
4. Those stale buffers reached unguarded `self._sources.get_source(...)` calls,
   raising `KeyError: 'source_00000000-0000-4000-8000-781078565686'`.
5. The exception was converted into a GST bus error and the module moved to
   STOPPING/STOPPED. The Python process did not exit, so Docker
   `restart: unless-stopped` did not recover it.

Verified unguarded call sites in the running
`ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1` image:

- `/opt/venv/lib/python3.12/site-packages/savant/deepstream/buffer_processor.py:150`
- `/opt/venv/lib/python3.12/site-packages/savant/deepstream/nvinfer/processor.py:271`
- `/opt/venv/lib/python3.12/site-packages/savant/deepstream/pipeline.py:952`
- `/opt/venv/lib/python3.12/site-packages/savant/deepstream/pipeline.py:1154`

Container file md5 baselines:

```text
deepstream/buffer_processor.py   ab13b915a8a7fc7acd6265d054054036
deepstream/nvinfer/processor.py  e6a05fb0e0eb7dd04c9d01e4fc2bb80f
deepstream/pipeline.py           5c418afd69e9d478a43cc1a123513837
```

This is not caused purely by having two streams. Two streams widen the race
window because there are more in-flight buffers and more branch reset activity,
but a single looping or reconnecting RTSP source can still trigger the same
class of timestamp reset.

The uploaded `savant-crash-fix-20260611.zip` was reviewed against the current
container files. Its framework overlay patches are acceptable in principle:
they only change the four `get_source(...)` call sites above from fatal
`KeyError` to warning plus stale-frame skip / late-EOS ignore, and the patched
file md5 values match the package README:

```text
deepstream/buffer_processor.py   556af89b356401efa1dc9d5c2c4d3c68
deepstream/nvinfer/processor.py  9615f7cd134f623a3950b65e5ad71fb1
deepstream/pipeline.py           7c5eabbe84697e591a0a31a1c3977f2c
```

The watchdog portion of the zip needed one local adjustment before adoption:
its default `WATCHDOG_SOURCE_CONTAINER_FILTER=video-analytics-midterm-source`
matched the compose primary adapter but did not match current dynamic source
containers named `video-analytics-source-source_...`. The adopted API
supervisor discovers both:

- `video-analytics-midterm-source-adapter`
- `video-analytics-source-*`

The final implementation uses exact-name matching for the compose source and
prefix matching for dynamic source containers, so it avoids compose `$` regex
escaping entirely.

## Fix Plan

### Phase 1: Derive Multi-Source Headroom

Change the active midterm Savant capacity rule from a fixed value to a derived
target:

```text
recommended_max_parallel_streams = max(2, enabled_rtsp_source_count * 2)
```

For the current two enabled RTSP sources, the recommendation is 4.

Implementation target:

- do not hard-code a permanent `MAX_PARALLEL_STREAMS: "4"` as the final design
- make the compose value env-configurable, for example
  `MAX_PARALLEL_STREAMS: "${MAX_PARALLEL_STREAMS:-4}"`, so deployments can
  exceed the current two-source default without editing compose
- add a runtime/export/doctor check that counts enabled RTSP sources from the
  generated source or camera config and reports whether the running/configured
  `MAX_PARALLEL_STREAMS` is below `max(2, enabled_rtsp_source_count * 2)`
- if runtime apply is expected to change this value automatically, note that a
  plain Docker restart cannot change container environment; Savant must either
  be recreated with the derived env or the system must return a clear
  "capacity below recommendation; recreate/apply compose" diagnostic

Do not change `BATCH_SIZE` in this phase. The current three nvinfer model chains
have their own batch dimensions, and changing muxer `BATCH_SIZE` in a two-source
8fps deployment has poor risk/reward. Revisit `BATCH_SIZE` only as a separate
throughput task, or when enabled source count is at least 4.

Update tests that assert the old fixed value. In the current checkout,
`harness/tests/test_midterm_deployment_contract.py` asserts
`MAX_PARALLEL_STREAMS == "2"`. Replace that with an assertion that the compose
value is configurable and that the derived recommendation is not below the
enabled source count with headroom.

Validation:

```bash
pytest -q harness/tests/test_midterm_deployment_contract.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
```

Runtime validation:

```bash
docker inspect --format '{{.Name}} restart={{.RestartCount}} state={{.State.Status}} exit={{.State.ExitCode}}' \
  video-analytics-midterm-source-adapter \
  video-analytics-source-source_00000000-0000-4000-8000-781078565686 \
  video-analytics-midterm-savant

docker logs --tail 300 video-analytics-midterm-savant | \
  rg 'primary_rtsp|source_00000000-0000-4000-8000-781078565686|ERROR|Exception|Traceback|pad|streammux'
```

### Phase 2: Add Savant Readiness Gate Before Starting Sources

Harden `services/api/app/services/runtime_apply.py` so runtime apply waits for
Savant readiness after `_restart_container(client, savant_container)` and before
starting source adapters.

Acceptable readiness signals, in order of preference:

1. recent Savant logs contain a stable pipeline-ready marker such as pipeline
   entering PLAYING/module started
2. Docker health status becomes `healthy` when a healthcheck exists
3. A bounded timeout expires and runtime apply returns a clear error instead of
   starting sources into an unavailable Savant pipeline.

Use a default timeout of 300 seconds. First TensorRT engine construction can take
minutes. Non-first startup should usually be faster because `/models` and
`/downloads` are mounted, but the readiness gate must tolerate the cold path.

Keep the wait bounded and report diagnostics in the API response:

- `savant_ready: true/false`
- `savant_ready_wait_seconds`
- `savant_ready_reason`

Do not restart management containers as part of this change.

Suggested code shape:

```text
_restart_container(client, savant_container)
_wait_for_container_ready(client, savant_container, timeout_s=...)
_start_containers(client, worker_containers)
start enabled sources
```

Starting workers before sources remains acceptable because event/clip/media
workers depend on Redis/Postgres/Replay outputs, not on RTSP adapters being
already connected.

### Phase 3: Preserve Source Lifecycle Diagnostics

Add or improve logging/return fields around dynamic source adapters:

- source container name
- source id
- RTSP URI host or redacted URI
- create/start result
- restart policy
- `FFMPEG_TIMEOUT_MS`
- whether compose primary source or dynamic source was started

When a source stop is reported, collect these diagnostics before changing more
code:

```bash
docker ps -a --filter name=video-analytics-midterm --format '{{.Names}}\t{{.Status}}\t{{.Image}}'
docker ps -a --filter name=video-analytics-source --format '{{.Names}}\t{{.Status}}\t{{.Image}}'

docker inspect --format '{{.Name}} restart={{.RestartCount}} state={{.State.Status}} exit={{.State.ExitCode}} started={{.State.StartedAt}} finished={{.State.FinishedAt}}' \
  video-analytics-midterm-source-adapter \
  video-analytics-source-source_00000000-0000-4000-8000-781078565686 \
  video-analytics-midterm-savant \
  video-analytics-midterm-replay-service

docker logs --tail 500 video-analytics-midterm-savant
docker logs --tail 300 video-analytics-midterm-source-adapter
docker logs --tail 300 video-analytics-source-source_00000000-0000-4000-8000-781078565686
docker logs --tail 300 video-analytics-midterm-replay-service
```

### Phase 4: Long-Run And Reattach Acceptance

Run a 10-15 minute two-source validation after Phases 1 and 2. This validation
must include active reattach fault injection, because a steady two-source run
does not prove that pad/headroom problems are fixed.

Acceptance:

- both source adapters remain running
- restart counts do not increase during the steady-state parts of the window
- Savant logs show ticks for both sources
- `security.frame_annotations` receives frames for both source ids
- event-worker remains running
- clip-worker does not produce repeated `missing_post_savant_frame_pts_window`
  solely because one source disappeared
- no Savant pad/streammux capacity errors appear
- after restarting one source adapter in the middle of the window, that source
  reattaches, the other source continues producing ticks, and Savant still shows
  no pad/streammux capacity errors

Useful Redis checks:

```bash
docker exec video-analytics-midterm-redis redis-cli XREVRANGE security.frame_annotations + - COUNT 20
docker exec video-analytics-midterm-redis redis-cli XLEN security.frame_annotations
```

GPU check if available:

```bash
nvidia-smi dmon -s pucvmet -c 10
```

Fault injection:

```bash
docker restart video-analytics-source-source_00000000-0000-4000-8000-781078565686
sleep 30
docker inspect --format '{{.Name}} restart={{.RestartCount}} state={{.State.Status}} exit={{.State.ExitCode}}' \
  video-analytics-source-source_00000000-0000-4000-8000-781078565686 \
  video-analytics-midterm-source-adapter \
  video-analytics-midterm-savant
docker logs --tail 300 video-analytics-midterm-savant | \
  rg 'source_00000000-0000-4000-8000-781078565686|primary_rtsp|ERROR|Exception|Traceback|pad|streammux'
```

### Phase 5: Patch Savant v0.6.0 Source-Reset Race

Adopt the framework overlay patch from `savant-crash-fix-20260611.zip`, but do
not blindly overwrite the repo's compose file.

Implementation targets:

- add `modules/savant_security/savant_patches/apply_patches.py`
- add the pinned v0.6.0 patched framework files under
  `modules/savant_security/savant_patches/v0.6.0/`
- run `apply_patches.py` from the `savant-security` entrypoint before
  `python -m savant.entrypoint`
- keep patching fail-loud by default:
  `SAVANT_PATCH_ENABLED=true`, `SAVANT_PATCH_ENFORCE=true`
- refuse to patch if installed framework md5 does not match the known v0.6.0
  baseline
- log a stable marker such as `[video-analytics patch]` on skipped zombie frames

The patch must remain narrowly scoped to these behaviours:

- missing source during input preparation: skip stale frame
- missing source during custom model output: skip stale frame and continue the
  batch
- missing source during output metadata update: skip stale frame
- missing source during late/duplicate muxer peer EOS: ignore the EOS

It must not change model configuration, inference intervals, source reset
policy, Replay topology, business pyfunc semantics, or evidence contracts.

Validation:

```bash
python -m py_compile \
  modules/savant_security/savant_patches/apply_patches.py \
  modules/savant_security/savant_patches/v0.6.0/buffer_processor.py \
  modules/savant_security/savant_patches/v0.6.0/nvinfer_processor.py \
  modules/savant_security/savant_patches/v0.6.0/pipeline.py

docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
docker compose -f infra/docker-compose.midterm.yml up -d --force-recreate savant-security
docker logs video-analytics-midterm-savant 2>&1 | rg 'savant_patches|video-analytics patch'
docker exec video-analytics-midterm-savant sh -lc \
  'md5sum /opt/venv/lib/python3.12/site-packages/savant/deepstream/buffer_processor.py \
          /opt/venv/lib/python3.12/site-packages/savant/deepstream/nvinfer/processor.py \
          /opt/venv/lib/python3.12/site-packages/savant/deepstream/pipeline.py'
```

Expected patched md5 values:

```text
556af89b356401efa1dc9d5c2c4d3c68  buffer_processor.py
9615f7cd134f623a3950b65e5ad71fb1  nvinfer/processor.py
7c5eabbe84697e591a0a31a1c3977f2c  pipeline.py
```

Runtime validation must include at least one source reset case. Accept either a
natural loop/reset of `primary_rtsp` or an explicit source-adapter restart. The
expected result is warning logs with `[video-analytics patch]`, continued
Savant `running` status, and continued frame annotations for both enabled
sources.

### Phase 6: Add API-Owned Savant Runtime Supervisor

Add the recovery supervisor after Phase 5 is in place. The supervisor is a
recovery safety net, not the primary fix for the source-reset race.

Placement decision: do not keep the reviewed zip's standalone `docker:27-cli`
watchdog. The 8090 management plane should own this operational control through
its API backend. The `api` service already has Docker socket access and runtime
apply logic, so it replaces shell-based `docker inspect` / `docker exec` /
`docker restart` calls with the existing Docker socket client and Redis checks.

Required behaviour:

- poll Savant module status from `/opt/savant/status.txt`, not Docker health
  alone
- optionally detect `security.frame_annotations` stalling while source adapters
  are running
- on STOPPING/STOPPED or prolonged stall, perform a controlled restart:
  restart Savant, wait for module `running`, then restart source adapters
- include cooldown to avoid restart flapping
- preserve Replay by default unless a later diagnosis proves Replay must also
  be restarted

Source adapter discovery must match both current container naming patterns:

```text
video-analytics-midterm-source-adapter
video-analytics-source-*
```

Do not use only a `video-analytics-midterm-source` filter because that misses
dynamic source adapters in the current runtime. The API supervisor uses exact
matching for `video-analytics-midterm-source-adapter` and prefix matching for
`video-analytics-source-`.

Also override the Savant healthcheck `start_period` from the image's 30 minutes
to a shorter but still cold-start-safe value. Use 15 minutes initially because
TensorRT engines are cached under `/models` and non-first starts should be much
faster, but cold builds can still take minutes.

Validation:

```bash
python -m py_compile services/api/app/services/savant_supervisor.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
curl --noproxy '*' http://127.0.0.1:8090/api/v1/cameras/runtime/supervisor
```

Optional watchdog drill:

```bash
docker exec video-analytics-midterm-savant sh -c 'echo stopped > /opt/savant/status.txt'
curl --noproxy '*' -X POST http://127.0.0.1:8090/api/v1/cameras/runtime/supervisor/recover
```

Expected result: the API supervisor triggers one controlled recovery, Savant returns to
running/healthy, both source adapters are restarted, and
`security.frame_annotations` resumes for both source ids.

Acceptance:

- default midterm compose has no Docker CLI watchdog image
- 8090 can show Savant module status, annotation-flow freshness, and last
  controlled recovery reason/time
- a STOPPED status drill and an annotation-stall drill both recover through the
  API-owned supervisor
- dynamic and compose-managed source adapters are both restarted
- existing camera runtime apply behaviour remains unchanged

## Do Not Treat As Proven Yet

These are not proven by the current code inspection alone:

- `MAX_PARALLEL_STREAMS=2` is the direct cause of a reported stop.
- old streammux pads remain occupied long enough to block new sessions.
- TensorRT engine build time is currently long enough to trigger adapter
  restart loops.

They are good hypotheses to verify with logs from the stop window.

## Proven Follow-Up Gap: Replay-To-Savant Analysis Throttling

The 2026-06-14 runtime diagnosis moved this item out of the hypothesis list.
Both fixed and dynamic source adapters were observed restarting repeatedly with
`WriterResultSendTimeout`, while their logs still showed full RTSP cadence
(`primary_rtsp` about 23.98 FPS and the dynamic lab source about 30 FPS).

`MAX_FPS=8/1` is currently enforced in Savant's `PtsFpsGate` after Replay has
already accepted and forwarded frames. This protects model inference, but it
does not protect:

- source-adapter -> Replay ingress
- Replay RocksDB/storage ingestion
- Replay out_stream -> Savant transport

The repair must not solve this by dropping frames before the evidence authority
path. Evidence clips must continue to come from full retained stream data. The
needed boundary is:

```text
full evidence path: RTSP -> Replay/storage -> evidence clip
analysis path:      Replay/storage -> sampled analysis stream -> Savant
```

Savant-side `PtsFpsGate` may remain as a secondary guardrail, but the primary
throughput control for the analysis branch needs to sit before Savant transport
or in Replay's analysis out_stream.

## Separation From Replay Evidence Fixes

This runtime plan intentionally avoids changing:

- `modules/savant_replay/config.midterm.json`
- `services/clip-worker/app/replay_client.py`
- `services/clip-worker/app/worker.py`
- media-worker crop and duration guard behavior

Those belong to the Replay/evidence plan. This separation lets two Codex
processes work without editing the same files in normal operation.
