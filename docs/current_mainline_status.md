# Current Mainline Status

## 2026-06-09 Midterm Project Version

The current deployable runtime in this checkout is the midterm project version.
It intentionally avoids C1/C2/phase naming in the active deployment surface.

- Branch: `c2/post-savant-poc`.
- Compose file: `infra/docker-compose.midterm.yml`.
- Env file: `infra/env/midterm.env`.
- Compose project: `video-analytics-midterm`.
- Containers: `video-analytics-midterm-*`.
- Source id: `primary_rtsp`.
- Evidence viewer: host port `8090`.
- Replay API: host port `8098`.
- Worker database default: `host.docker.internal:5432`.

## Current Runtime Chain

```text
RTSP -> Replay storage -> Savant inference -> Redis events/annotations
  -> event-worker -> clip-worker -> Replay job -> video-file-sink
  -> media-worker evidence sidecar -> evidence-viewer
```

## Current Calibration

The midterm entrypoint enables Savant ingress FPS control and stricter detection
quality defaults:

- `MAX_FPS_CONTROL=true`
- `MAX_FPS=8/1`
- `MIN_FPS=2/1`
- `POSE_INFER_INTERVAL=1`
- `POSE_CONFIDENCE_THRESHOLD=0.50`
- `POSE_KEYPOINT_THRESHOLD=0.35`
- `POSE_SELECTOR_CONFIDENCE_THRESHOLD=0.50`
- `POSE_SELECTOR_NMS_IOU_THRESHOLD=0.50`
- `POSE_MIN_WIDTH=60`
- `POSE_MIN_HEIGHT=100`
- `FACE_CONFIDENCE_THRESHOLD=0.50`
- `WATCHLIST_THRESHOLD=0.60`

## Archive Rule

Historical C1/C2/phase files remain in archive directories for traceability.
They are not current deployment entrypoints and should not be copied to a
project machine unless explicitly doing historical regression.
