# Midterm Phase 0D Source Adapter Tolerance Verification

Date: 2026-06-15

## Scope

Phase 0D verified whether the prebuilt
`ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0` RTSP source adapter
honors environment variables for ZeroMQ sink send timeout / retry tolerance.

The phase was verification-only. No runtime container was changed and no image
was rebuilt.

## Evidence

Inspected the image entrypoint:

```bash
docker run --rm --entrypoint sh ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0 \
  -lc 'sed -n "1,240p" /opt/savant/adapters/gst/sources/rtsp.sh'
```

`rtsp.sh` builds `SINK_PROPERTIES` with only:

```text
source-id="${SOURCE_ID}"
socket="${ZMQ_ENDPOINT}"
sync="${SYNC_OUTPUT}"
ts-offset="${SYNC_DELAY}"
eos-on-start="${EOS_ON_START}"
```

It maps these adapter envs:

- `SOURCE_ID`
- `RTSP_URI`
- `ZMQ_ENDPOINT`
- `SYNC_OUTPUT`
- `SYNC_DELAY`
- `FPS_OUTPUT`
- `FPS_PERIOD_SECONDS`
- `FPS_PERIOD_FRAMES`
- `RTSP_TRANSPORT`
- `BUFFER_LEN`
- `FFMPEG_LOGLEVEL`
- `USE_ABSOLUTE_TIMESTAMPS`
- `EOS_ON_START`
- `FFMPEG_TIMEOUT_MS`

It does not map a `SEND_TIMEOUT`, `SEND_RETRIES`, `RECEIVE_TIMEOUT`, or
`RECEIVE_RETRIES` env into `zeromq_sink`.

Inspected the Python plugin references:

```bash
docker run --rm --entrypoint sh ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0 \
  -lc 'grep -RInE "TIMEOUT|RETR|ZMQ|zeromq|send[-_]?timeout|send[-_]?retries" /opt/savant/adapters /opt/savant'
```

Findings:

- `zeromq_sink.py` uses `receive_timeout` and `receive_retries` internally and
  passes them to `WriterConfigBuilder.with_send_timeout()` and
  `with_send_retries()`.
- `media_files.sh` and `multi_stream.sh` expose receive timeout envs into
  `zeromq_sink`.
- `rtsp.sh` does not expose those properties.

Inspected GStreamer plugin properties:

```bash
docker run --rm --entrypoint sh ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0 \
  -lc 'gst-inspect-1.0 zeromq_sink'
```

Relevant properties exist on the element:

- `receive-timeout`: receive timeout socket option
- `receive-retries`: retries to receive confirmation message
- `send-hwm`: outbound high watermark

However, the RTSP source entrypoint does not pass them.

## Decision

Do not add adapter tolerance env overrides to the midterm RTSP source adapters.

Reason:

- The image supports `zeromq_sink` properties, but the actual `rtsp.sh`
  entrypoint used by fixed and dynamic RTSP sources does not honor env vars for
  these properties.
- Adding env vars to compose/runtime paths would create a false sense of
  protection because the running pipeline would ignore them.
- Changing the entrypoint or replacing the pipeline command would be a custom
  adapter behavior change, not a reversible env experiment. That belongs to the
  forwarder/Buffer work, not Phase 0D.

## Keep / Revert

- Kept: no runtime or config change.
- Reverted: not applicable.
- Follow-up: use Phase 0.5 / Phase 1 forwarder or an official Buffer adapter for
  actual drop-on-full/back-pressure isolation. Do not rely on RTSP adapter env
  tolerance for this image.

## Validation

Planned validation before commit:

- `docker compose -f infra/docker-compose.midterm.yml config`
- `git diff --check`
