# C1G.1b — Operator Frontend Enhancement

## Overview

Enhanced the 8090 operator portal from a basic evidence viewer to a more usable
camera, people, face-registration, and evidence-review interface. Uses vanilla
HTML/JS/CSS, no frontend framework.

## Page Path

```
GET /                 → serves index.html from services/evidence-viewer
GET /static/operator.js → camera and people JavaScript
GET /static/evidence.js → evidence-review JavaScript
GET /static/style.css   → styles
```

All customer-facing assets are served by `services/evidence-viewer` on port
8090. Camera and people requests use same-origin `/api/v1/*` calls that the 8090
service proxies to the internal `services/api` container on compose port 8000.
That internal API image is built from `services/api/Dockerfile.face-runtime`,
which reuses `video-analytics-midterm-face-worker:latest` for the existing
ONNX Runtime/OpenCV/Numpy face-registration layer.

## Supported Configuration

### Camera Management
- List all cameras with id, name, source_id, enabled, rtsp_transport, global_alert_cooldown_s
- Create / edit camera with full field set
- Enable / disable camera
- Refresh camera list

### Camera Fields
- `id`, `name`, `source_id`, `rtsp_url`, `site_id`, `location`, `gpu_id`
- `enabled`, `input_type`, `rtsp_transport`
- `fps_policy.max_fps`, `fps_policy.min_fps`
- `alert_policy.global_alert_cooldown_s`, `alert_policy.store_suppressed_events`
- `alert_policy.suppress_record_request`, `alert_policy.critical_bypass`

### Zone (ROI) Management
- View zones per camera
- Add polygon zone (3-10 points)
- Add line zone (2 points)
- Edit zone via JSON textarea
- Delete zone

### Algorithm Rule Management
- View rules per camera with observation/alert badge
- Add rule from 9 algorithm templates
- Edit rule config via JSON textarea
- Enable / disable individual rule
- Delete rule

### Alert Policy
- View and edit per-camera alert policy
- Fields: `global_alert_cooldown_s`, `store_suppressed_events`, `suppress_record_request`, `critical_bypass`

### Full Config Viewer
- Readonly JSON view of aggregated camera + zones + rules + alert_policy
- Auto-updates when camera is selected

## Observation vs Alert Distinction

Rules are categorized by `rule_category` (returned by the API):

| Algorithm | Category | Description |
|-----------|----------|-------------|
| `face.observation` | **observation** | Records face observations, no alert generated |
| `face.watchlist` | **alert** | Generates `watchlist_hit` alerts |
| `face.live_search` | config | Configuration entry for live search, not a full job |
| `behavior.intrusion` | **alert** | Perimeter intrusion alert |
| `behavior.loitering` | **alert** | Loitering detection alert |
| `behavior.crowd_gathering` | **alert** | Crowd gathering alert |
| `behavior.fall` | **alert** | Fall detection alert |
| `behavior.running` | **alert** | Running/chasing alert |
| `behavior.wall_climb_suspicious` | **alert** | Wall climb suspicious alert |

The UI shows a colored badge next to each rule indicating its category.

## Algorithm Templates

9 templates available as one-click buttons:

1. **Face Observation** — `face.observation` with quality/confidence thresholds
2. **Watchlist** — `face.watchlist` with similarity threshold and cooldown
3. **Live Search Config** — `face.live_search` with default threshold (disabled by default)
4. **Intrusion** — `behavior.intrusion` with zone and cooldown
5. **Loitering** — `behavior.loitering` with duration/speed thresholds
6. **Crowd Gathering** — `behavior.crowd_gathering` with person count threshold
7. **Fall** — `behavior.fall` with pose-based detection parameters
8. **Running** — `behavior.running` with speed/duration thresholds
9. **Wall Climb** — `behavior.wall_climb_suspicious` with line crossing parameters

## API Endpoints Used

All endpoints are from the existing cameras router (`/api/v1/cameras/...`):

```
GET  /api/v1/cameras
POST /api/v1/cameras
PUT  /api/v1/cameras/{camera_id}
POST /api/v1/cameras/{camera_id}/enable
POST /api/v1/cameras/{camera_id}/disable
GET  /api/v1/cameras/{camera_id}/config
POST /api/v1/cameras/{camera_id}/zones
PUT  /api/v1/cameras/{camera_id}/zones/{zone_id}
DELETE /api/v1/cameras/{camera_id}/zones/{zone_id}
POST /api/v1/cameras/{camera_id}/rules
PUT  /api/v1/cameras/{camera_id}/rules/{rule_id}
DELETE /api/v1/cameras/{camera_id}/rules/{rule_id}
POST /api/v1/cameras/{camera_id}/rules/{rule_id}/enable
POST /api/v1/cameras/{camera_id}/rules/{rule_id}/disable
GET  /api/v1/cameras/{camera_id}/alert-policy
PUT  /api/v1/cameras/{camera_id}/alert-policy
```

## Evidence Review

The operator page includes an **告警证据** view backed by the existing 8090
evidence endpoints (`/api/bundles`, `/api/bundles/{event_id}/annotations`,
`/api/bundles/{event_id}/media/raw_clip`). Alarm evidence is categorized in the
frontend as 名单布控, 周界入侵, 行为异常, and 聚集风险.

## Current Limitations

1. **No runtime apply** — Saving a config to the database does not automatically apply it to
   a running Savant module. Savant restart or config reload is a separate operational step.
2. **No Savant restart** — The page does not trigger Savant container restarts.
3. **Zone points via JSON** — Zone polygon/line points are edited as raw JSON arrays.
   A visual ROI editor is a future enhancement.
4. **No auth/permissions** — The operator page has no authentication or role-based access control.
5. **No validation preview** — Zone geometry and rule config are validated server-side only.

## Tests

```bash
pytest -q \
  harness/tests/test_c1g1b_operator_frontend_static.py \
  harness/tests/test_operator_face_registration_static.py
```

## Smoke

```bash
bash scripts/smoke/current/check_operator_camera_and_face_registration.sh
```
