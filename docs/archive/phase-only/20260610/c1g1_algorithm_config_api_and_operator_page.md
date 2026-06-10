# C1G.1 Algorithm Configuration API + Simple Operator Frontend

## Goal

C1G.1 adds the first DB-backed configuration loop for cameras, ROIs, algorithm rules, and camera alert policy:

```text
FastAPI -> PostgreSQL -> simple operator HTML page
```

This phase does not hot-update Savant. A later phase can read this DB config, generate `cameras.yml`, and restart source adapter / Savant modules.

## Observation / Match / Alert Semantics

The runtime is split into three layers:

1. Observation / fact layer: valid person or face observations are stored by default as a timeline and trajectory source. A clear face observation is not an alert.
2. Match / derived layer: face observations can be matched against gallery or live-search targets. `match_results` are derived facts and are not automatically alerts.
3. Alert layer: only enabled algorithm rules emit `security.events`, alerts, record requests, and evidence.

`face.observation` means "persist qualified face observations for history and trajectory." It is not a watchlist alert. `face.watchlist` and future `face.live_search` hits are alert-producing rules when their target-match conditions are met.

## API

Camera endpoints:

```text
GET  /api/v1/cameras
POST /api/v1/cameras
GET  /api/v1/cameras/{camera_id}
PUT  /api/v1/cameras/{camera_id}
POST /api/v1/cameras/{camera_id}/enable
POST /api/v1/cameras/{camera_id}/disable
GET  /api/v1/cameras/{camera_id}/config
```

Zone / ROI endpoints:

```text
GET    /api/v1/cameras/{camera_id}/zones
POST   /api/v1/cameras/{camera_id}/zones
PUT    /api/v1/cameras/{camera_id}/zones/{zone_id}
DELETE /api/v1/cameras/{camera_id}/zones/{zone_id}
```

Algorithm rule endpoints:

```text
GET    /api/v1/cameras/{camera_id}/rules
POST   /api/v1/cameras/{camera_id}/rules
GET    /api/v1/cameras/{camera_id}/rules/{rule_id}
PUT    /api/v1/cameras/{camera_id}/rules/{rule_id}
DELETE /api/v1/cameras/{camera_id}/rules/{rule_id}
POST   /api/v1/cameras/{camera_id}/rules/{rule_id}/enable
POST   /api/v1/cameras/{camera_id}/rules/{rule_id}/disable
```

Alert policy endpoints:

```text
GET /api/v1/cameras/{camera_id}/alert-policy
PUT /api/v1/cameras/{camera_id}/alert-policy
```

Supported `algorithm_id` values:

```text
behavior.intrusion
behavior.loitering
behavior.crowd_gathering
behavior.fall
behavior.running
behavior.wall_climb_suspicious
face.observation
face.watchlist
face.live_search
```

## Database

Migration `010_c1g1_algorithm_config_api.sql` extends the existing C1 tables:

`cameras` adds:

```text
input_type
rtsp_transport
fps_policy JSONB
alert_policy JSONB
```

`camera_zones` adds:

```text
zone_id
coordinate_space
enabled
unique(camera_id, zone_id)
```

`camera_rules` adds:

```text
rule_id
algorithm_id
unique(camera_id, rule_id)
```

Legacy `rule_type` is retained for compatibility with existing C1/R3 code.

## Alert Policy

The camera alert policy stores:

```json
{
  "global_alert_cooldown_s": 30,
  "store_suppressed_events": true,
  "suppress_record_request": true,
  "critical_bypass": false
}
```

The event-worker now has a minimal `AlertPolicyService`. It persists the incoming `SecurityEvent` first, then checks the camera global cooldown. If the camera is still inside cooldown, the event is marked `status=suppressed`, the decision is recorded in `payload.alert_policy`, and the worker skips Redis alert publication, record request creation, and evidence triggering.

This suppression is not implemented in Savant or face-worker. Producers should still emit facts and events; alert policy is applied at the alert/evidence boundary.

## Simple Operator Page

The API serves a lightweight operator page:

```text
GET /operator
```

Static assets live under:

```text
services/api/app/static/operator/
```

The page can list cameras, create/update a camera, enable/disable it, edit the camera cooldown, add a simple ROI, and add/update algorithm-rule JSON with template buttons.

This page is not the production dashboard, has no authentication, and does not replace the C1F evidence viewer on port `8090`.

## Smoke

```bash
pytest -q harness/tests/test_c1g1_camera_algorithm_config_api.py
pytest -q harness/tests/test_c1g1_alert_policy_contract.py
bash scripts/smoke/check_c1g1_algorithm_config_api.sh
docker compose -f infra/docker-compose.c1-official-replay-dev.yml config
git diff --check
```

The smoke starts only `postgres`, `redis`, and `api`, applies migration `010`, creates or updates deterministic camera `cam_c1g1_test`, adds ROI and rules, validates full config, and verifies `/operator`.

## Known Limits

This phase does not modify YOLO26-pose, YOLOv8-Face, AdaFace, Redis observation schema, C1F evidence viewer, or the evidence production pipeline. It does not implement live-search jobs, a formal watchlist management UI, RBAC, or Savant hot reload.
