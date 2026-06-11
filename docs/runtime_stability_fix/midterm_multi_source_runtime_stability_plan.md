# Midterm Multi-Source Runtime Stability Plan

## Status

Date: 2026-06-11

This document freezes the multi-source runtime findings from the current
midterm checkout and turns them into a separate correction plan.

Execution status: the code/config hardening phases in this plan have been
implemented on branch `c2/post-savant-poc` in these commits:

- `d997e5e Fix midterm replay runtime foundations`
- `e751c51 Gate midterm runtime sources on Savant readiness`

The long-run and active source-adapter restart fault injection remains a runtime
acceptance activity for the deployment environment.

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

## Do Not Treat As Proven Yet

These are not proven by the current code inspection alone:

- `MAX_PARALLEL_STREAMS=2` is the direct cause of a reported stop.
- old streammux pads remain occupied long enough to block new sessions.
- Replay out_stream backpressure is currently causing source-adapter self
  restarts.
- TensorRT engine build time is currently long enough to trigger adapter
  restart loops.

They are good hypotheses to verify with logs from the stop window.

## Separation From Replay Evidence Fixes

This runtime plan intentionally avoids changing:

- `modules/savant_replay/config.midterm.json`
- `services/clip-worker/app/replay_client.py`
- `services/clip-worker/app/worker.py`
- media-worker crop and duration guard behavior

Those belong to the Replay/evidence plan. This separation lets two Codex
processes work without editing the same files in normal operation.
