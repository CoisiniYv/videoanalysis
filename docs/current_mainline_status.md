# Current Mainline Status

## 2026-06-11 Midterm Project Version

The current deployable runtime in this checkout is the midterm project version.
It intentionally avoids historical codename files in the active deployment surface.

- Branch: current working-tree branch.
- Compose file: `infra/docker-compose.midterm.yml`.
- Env file: `infra/env/midterm.env`.
- Compose project: `video-analytics-midterm`.
- Containers: `video-analytics-midterm-*`.
- Source id: `primary_rtsp`.
- Operator portal / evidence viewer: host port `8090`.
- Internal API service: compose network port `8000`; not published to host and
  reached through the 8090 portal proxy.
- Replay API: host port `8098`.
- Worker database default: `host.docker.internal:5432`.
- Internal API runtime: `services/api/Dockerfile.face-runtime`, inheriting from
  `video-analytics-midterm-face-worker:latest` to reuse the already-installed
  ONNX Runtime/OpenCV/Numpy face-registration layer.
- Savant v0.6.0 PTS-reset crash hardening: `savant-security` applies the
  md5-pinned overlay in `modules/savant_security/savant_patches/` before module
  startup, and the optional `savant-watchdog` profile provides STOPPED/stall
  recovery for Savant plus primary and dynamic source adapters.
- Operator portal design: `docs/midterm_operator_portal_runtime_design.md`.
- 8090 evidence list/detail displays alarm machine time from bundle metadata;
  new bundles write `event.alarm_machine_time` from `events.created_at`.
- 8090 evidence playback fail-closed behavior is backed by the media-worker
  post-Savant crop fix: raw clip crop now selects one contiguous sink metadata
  segment, prefers matching `frame_uuid/uuid` anchors when present, normalizes
  ffmpeg trim with `setpts`, and filters frame annotation cache by runtime
  epoch. Runtime apply/restart also resets the frame annotation Redis cache;
  see `docs/midterm_8090_port_integration.md`.
- Replay `offset.seconds` remains the positive rewind value from the anchor
  keyframe to `requested_start_pts`; do not flip it negative. Current playback
  validation used ready bundle `f208b550-6b34-44ff-a847-3219041349ea` and latest
  8090 listing `3acfac74-6c54-4045-820b-b659df3894da`.
- Operator algorithm-control runtime status:
  `docs/midterm_operator_algorithm_controls_runtime_status.md`.
- 2026-06-11 progress snapshot and next-plan baseline:
  `docs/midterm_progress_snapshot_2026-06-11.md`.
- Current Replay intrusion clip-duration diagnosis:
  `docs/midterm_replay_intrusion_clip_duration_diagnosis.md`.
- Replay routing-id mismatch recovery:
  `docs/midterm_replay_routing_id_recovery.md`.
- Current `/data/video-analytics` directory inventory and cleanup record:
  `docs/midterm_data_directory_inventory.md`.

## Current Runtime Chain

```text
RTSP -> Replay storage -> Savant inference -> Redis events/annotations
  -> event-worker -> clip-worker -> Replay job -> video-file-sink
  -> media-worker evidence sidecar -> 8090 operator portal
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

Historical codename files remain in archive directories for traceability.
They are not current deployment entrypoints and should not be copied to a
project machine unless explicitly doing historical regression.
