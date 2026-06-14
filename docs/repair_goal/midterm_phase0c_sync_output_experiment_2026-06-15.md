# Midterm Phase 0C SYNC_OUTPUT Experiment

Date: 2026-06-15

## Scope

Phase 0C tested whether live RTSP source adapters should stop syncing output to
source timestamps. The experiment changed both source-adapter creation paths to
`SYNC_OUTPUT=false`:

- fixed compose source: `infra/docker-compose.midterm.yml`
- API dynamic source creation: `services/api/app/services/runtime_apply.py`
- script dynamic source creation: `scripts/runtime/camera_source_controller.py`

No image rebuild was required. Runtime application only recreated the two source
adapter containers; Replay, Savant, and data volumes were not recreated.

## Baseline Before Change

Window: 2026-06-14T16:18:52Z to 2026-06-14T16:20:08Z.

Runtime env:

- `video-analytics-midterm-source-adapter`: `SYNC_OUTPUT=true`,
  `RestartCount=1107`
- `video-analytics-source-source_00000000-0000-4000-8000-781078565686`:
  `SYNC_OUTPUT=true`, `RestartCount=1482`

Observed over the 75 second window:

- fixed source restart delta: 0
- dynamic source restart delta: 0
- Replay `Resource temporarily unavailable` / `Send timeout`: 0
- source-adapter `WriterResultSendTimeout` / `Internal data stream error`: not
  present in the sampled window
- Savant effective FPS: primary `8.1`, dynamic `8.1`
- Savant last-frame age at end: primary `0.0s`, dynamic `0.069s`
- GPU decoder utilization sampled on the local RTX 4090: start `2%`, end `0%`

This baseline was already stable after Phase 0A and 0B, so Phase 0C cannot be
credited as the first stabilizing lever.

## Runtime Apply

Applied at 2026-06-14T16:21:23Z.

Commands used:

```bash
docker compose -f infra/docker-compose.midterm.yml up -d --no-deps --force-recreate source-adapter
docker rm -f video-analytics-source-source_00000000-0000-4000-8000-781078565686
docker run -d --name video-analytics-source-source_00000000-0000-4000-8000-781078565686 \
  --restart unless-stopped \
  --network video-analytics-midterm_default \
  -e SOURCE_ID=source_00000000-0000-4000-8000-781078565686 \
  -e LOCATION=rtsp://10.37.57.157:8554/camera \
  -e RTSP_URI=rtsp://10.37.57.157:8554/camera \
  -e RTSP_TRANSPORT=tcp \
  -e ZMQ_ENDPOINT=dealer+connect:tcp://replay-service:5555 \
  -e SYNC_OUTPUT=false \
  -e BUFFER_LEN=2000 \
  -e EOS_ON_START=false \
  -e FFMPEG_TIMEOUT_MS=20000 \
  -e DOWNLOAD_PATH=/tmp/video-loop-cache \
  --entrypoint /opt/savant/adapters/gst/sources/rtsp.sh \
  ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0
```

Both containers started with `SYNC_OUTPUT=false`.

Note: because the experiment recreated the source containers, Docker
`RestartCount` reset to `0`. The after-window decision uses restart delta during
the new container lifetime, not the pre-recreate absolute count.

## After Change

Window: 2026-06-14T16:21:59Z to 2026-06-14T16:23:14Z.

Runtime env:

- `video-analytics-midterm-source-adapter`: `SYNC_OUTPUT=false`,
  `RestartCount=0`
- `video-analytics-source-source_00000000-0000-4000-8000-781078565686`:
  `SYNC_OUTPUT=false`, `RestartCount=0`

Observed over the 75 second window:

- fixed source restart delta: 0
- dynamic source restart delta: 0
- Replay `Resource temporarily unavailable` / `Send timeout`: 0
- source-adapter `WriterResultSendTimeout` / `Internal data stream error`: 0
- Savant effective FPS: primary `8.1`, dynamic `8.0`
- Savant last-frame age at end: primary `0.019s`, dynamic `0.0s`
- 8090 runtime health: `ok=true`
- GPU decoder utilization sampled on the local RTX 4090: start `2%`, end `0%`

## Decision

Keep `SYNC_OUTPUT=false`.

Rationale:

- It is the correct posture for live RTSP source adapters; timestamp-synced
  output is useful for file replay simulation, not for live sources.
- It did not introduce new Replay, source-adapter, Savant FPS, or last-frame-age
  regressions in the after window.
- It keeps fixed and dynamic source creation paths consistent.

This remains a mitigation and cleanup lever, not proof that `SYNC_OUTPUT=true`
was the root cause of the restart storm. Phase 0A and 0B had already flattened
the observed restart delta before this experiment.

## Validation

Planned validation before commit:

- `pytest -q harness/tests/test_midterm_deployment_contract.py harness/tests/test_camera_runtime_apply_service.py harness/tests/test_camera_source_controller.py`
- `docker compose -f infra/docker-compose.midterm.yml config`
- `git diff --check`
