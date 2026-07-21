# Rolling-cache sink

This replaces Savant 0.6.0 `video_files.py` for the rolling cache only. It keeps
one H264 passthrough GStreamer pipeline per active source session and rotates
MOV fragments with `splitmuxsink` at the configured time/keyframe boundary.

The build context is the repository root:

```sh
docker build -f services/rolling-cache-sink/Dockerfile .
```

Required environment: `ZMQ_ENDPOINT`. Existing rolling-cache variables remain
valid, especially `ROLLING_CACHE_ROOT`, `ROLLING_CACHE_NAMESPACE`,
`ROLLING_CACHE_SEGMENT_SECONDS`, `ROLLING_CACHE_RUNTIME_EPOCH_ID`, and
`RUNTIME_EPOCH_STATE_PATH`. `ROLLING_CACHE_SINK_HTTP_PORT` defaults to `8080`
and serves `/metrics`, `/healthz`, and `/readyz`.

Published segments retain the consumer contract:

```text
<root>/midterm/epochs/<runtime_epoch_id>/<source_id>/segments/<segment_id>/
  video.mov
  metadata.json   # native Savant JSONL; publication commit marker
```

Finalized fragments are staged outside the source directory and become visible
through one same-filesystem directory rename. Failed or interrupted fragments
remain under `.rolling-cache-staging` without a visible `metadata.json` in any
source tree; the service never deletes or overwrites them.
