# Operator: Add a Camera

Status: R2 operator draft.

## Purpose

Add an RTSP camera to the c1-official-adapter runtime without editing the Savant pipeline. Source adapters connect to the running `savant-security` module over ZMQ.

## Runtime

- Compose: `infra/docker-compose.c1-official-adapter.yml`
- Savant module: `modules/savant_security`
- Controller: `scripts/runtime/camera_source_controller.py`
- Default network: `c1-official-adapter_default`
- Adapter ZMQ endpoint: `dealer+connect:tcp://savant-security:5555`

## Add the Camera

1. Add or update the camera in the camera configuration source used by the project.
2. Export runtime configs so `infra/generated/sources.generated.yml` and `modules/savant_security/config/cameras.generated.yml` contain the camera.
3. Confirm each camera has a stable `camera_id` and `source_id`.
4. Confirm the source entry uses `adapter_type: gstreamer` and a valid `rtsp://` or `rtsps://` URI.

Example source shape:

```yaml
sources:
  cam_front_gate:
    camera_id: cam_front_gate
    source_id: front_gate_rtsp
    uri: rtsp://user:password@host/path
    enabled: true
    adapter_type: gstreamer
    zmq_endpoint: dealer+connect:tcp://savant-security:5555
```

## Start the Runtime

```bash
docker compose -f infra/docker-compose.c1-official-adapter.yml up -d \
  redis postgres api savant-security event-worker face-worker metadata-sink video-file-sink
```

## Start and Stop the Adapter

```bash
python3 scripts/runtime/camera_source_controller.py list
python3 scripts/runtime/camera_source_controller.py start --source-id front_gate_rtsp
python3 scripts/runtime/camera_source_controller.py status --source-id front_gate_rtsp
python3 scripts/runtime/camera_source_controller.py stop --source-id front_gate_rtsp
```

The controller creates a Docker container named `video-analytics-source-{source_id}`. Do not restart `savant-security` just to add or remove a camera.

## Checks

Adapter:

```bash
docker ps --filter 'name=video-analytics-source-'
docker logs video-analytics-source-front_gate_rtsp --tail 100
```

Redis:

```bash
docker exec c1-official-redis redis-cli XINFO STREAM security.events
docker exec c1-official-redis redis-cli XINFO STREAM security.face_observations
```

PostgreSQL:

```bash
psql "$DATABASE_URL" -c "SELECT camera_id, source_id, COUNT(*) FROM face_observations GROUP BY camera_id, source_id ORDER BY COUNT(*) DESC LIMIT 20;"
```

## Common Troubleshooting

- Adapter exits immediately: check RTSP credentials, network reachability, and `LOCATION` in `docker inspect`.
- No events in Redis: check `savant-security` logs and ZMQ endpoint.
- No face observations: confirm the camera sees faces large enough for the configured thresholds.
- Wrong `camera_id`: check `cameras.generated.yml` source mapping.
- Duplicate source adapter: stop stale `video-analytics-source-*` containers before restarting.

Do not commit `face/`, `testVideo/`, `manual-inspection/`, or generated media output.

