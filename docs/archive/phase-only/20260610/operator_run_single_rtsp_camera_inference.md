# Operator: Run Single RTSP Camera Inference

Status: R2.5 operator smoke.

## Purpose

Validate one RTSP camera stream through the current dual-primary logical pipeline. This is a single-camera smoke, not a performance test.

```text
RTSP stream
  -> official GStreamer RTSP source adapter
  -> ZMQ dealer+connect:tcp://savant-security:5555
  -> modules/savant_security
  -> YOLO26-pose
  -> nvtracker
  -> behavior_rules / ROI intrusion rule
  -> YOLOv8-Face full-frame primary
  -> face-person association
  -> AdaFace
  -> Redis security.events / security.face_observations
  -> event-worker / face-worker
  -> PostgreSQL events / face_observations
```

Short form: `RTSP -> source adapter -> Savant -> Redis -> DB`.

## Stream From Another Machine

The RTSP publisher runs on another LAN host. The Savant host only needs to reach the final RTSP URL.

Example publisher command:

```bash
ffmpeg -re -stream_loop -1 \
  -i 1080movie.mp4 \
  -an \
  -c:v libx264 \
  -preset veryfast \
  -tune zerolatency \
  -pix_fmt yuv420p \
  -r 15 \
  -g 30 \
  -f rtsp \
  -rtsp_transport tcp \
  rtsp://<STREAM_SERVER_IP>:8554/1080movie
```

MediaMTX or any other RTSP server is fine. This host only uses the resulting `rtsp://...` URL.

## Verify RTSP Reachability

```bash
export R2_5_RTSP_URL="rtsp://192.168.1.50:8554/1080movie"
ffprobe -rtsp_transport tcp "$R2_5_RTSP_URL"
```

Or, for visual inspection:

```bash
ffplay -rtsp_transport tcp "$R2_5_RTSP_URL"
```

## Configure One Camera

Use the existing camera config API/CLI and runtime exporter. Do not hand-edit the Savant pipeline.

Required camera fields:

- `camera_id`: operator-facing camera id, for example `cam_r2_5_rtsp`.
- `source_id`: Savant stream id, for example `r2_5_rtsp_1080movie`.
- `rtsp_url`: final reachable RTSP URL.
- `name`: display name, for example `R2.5 RTSP Movie Test`.
- `enabled`: `true`.

The smoke script performs these steps:

```bash
R2_5_RTSP_URL="rtsp://192.168.1.50:8554/1080movie" \
R2_5_CAMERA_ID="cam_r2_5_rtsp_movie" \
R2_5_SOURCE_ID="r2_5_rtsp_movie_$(date +%s)" \
R2_5_CAMERA_NAME="R2.5 RTSP Movie Camera" \
R2_5_WAIT_SECONDS=180 \
R2_5_MIN_FACE_OBSERVATIONS=1 \
R2_5_MIN_EVENTS=0 \
bash scripts/smoke/check_r2_5_single_rtsp_camera_inference.sh
```

The runtime source manifest must contain:

```yaml
sources:
  <camera_id>:
    camera_id: <camera_id>
    source_id: <source_id>
    uri: rtsp://...
    enabled: true
    adapter_type: gstreamer
    zmq_endpoint: dealer+connect:tcp://savant-security:5555
```

## Configure ROI

Current camera config stores ROI points as pixel frame coordinates. The behavior rule tests the tracked person's foot point against that polygon.

For operator convenience, the R2.5 smoke accepts this normalized full-frame default:

```bash
R2_5_ROI_POLYGON='[[0.05,0.05],[0.95,0.05],[0.95,0.95],[0.05,0.95]]'
```

When all coordinates are between `0.0` and `1.0`, the smoke converts them to pixel points using a 1920x1080 frame assumption. For other frame sizes, either pass pixel coordinates directly or override `R2_5_FRAME_WIDTH` / `R2_5_FRAME_HEIGHT`. If you already know the frame-space polygon, pass pixel coordinates instead, for example:

```bash
R2_5_ROI_POLYGON='[[100,100],[1820,100],[1820,980],[100,980]]'
```

The script creates a polygon zone named `r2_5_full_frame` and binds the current `intrusion` rule to that zone with `min_inside_ms=1`. Verify the generated runtime config:

```bash
grep -A30 "$R2_5_CAMERA_ID" modules/savant_security/config/cameras.generated.yml
```

No ordinary ROI or rule change requires editing Savant pipeline YAML.

## Start The RTSP Source Adapter

The source adapter is controlled by:

```bash
python3 scripts/runtime/camera_source_controller.py start --source-id "$R2_5_SOURCE_ID"
```

The controller reads `infra/generated/sources.generated.yml`, attaches the adapter container to Docker network `c1-official-adapter_default`, and chooses the official RTSP entrypoint for `rtsp://` or `rtsps://` sources.

Key adapter environment:

- `SOURCE_ID=<source_id>`
- `LOCATION=<rtsp_url>`
- `ZMQ_ENDPOINT=dealer+connect:tcp://savant-security:5555`

## Check Results

Redis:

```bash
docker exec c1-official-redis redis-cli XLEN security.face_observations
docker exec c1-official-redis redis-cli XREVRANGE security.face_observations + - COUNT 5
docker exec c1-official-redis redis-cli XLEN security.events
docker exec c1-official-redis redis-cli XREVRANGE security.events + - COUNT 5
```

PostgreSQL face observations:

```sql
SELECT source_observation_id, camera_id, source_id, track_id,
       timestamp_ms, quality, embedding_model, embedding_dim,
       ROUND(embedding_norm::numeric, 6) AS embedding_norm, created_at
FROM face_observations
WHERE source_id = '<source_id>'
ORDER BY created_at DESC
LIMIT 10;
```

Expected face-observation invariants:

- `embedding_model = 'adaface'`
- `embedding_dim = 512`
- `embedding_norm BETWEEN 0.90 AND 1.10`

If the ROI / intrusion event is triggered, query events:

```sql
SELECT id, source_event_id, event_type, camera_id, source_id,
       track_id, confidence, created_at
FROM events
WHERE source_id = '<source_id>'
ORDER BY created_at DESC
LIMIT 10;
```

No intrusion event is acceptable for this stage when `R2_5_MIN_EVENTS=0`.

## Limits

1. R2.5 is a single RTSP camera smoke, not a performance test.
2. R2.5 does not generate production evidence video.
3. R2.5 does not implement watchlist or live_search.
4. R2.5 does not change the Savant pipeline.
5. YOLOv8-Face remains the full-frame primary face detector.
