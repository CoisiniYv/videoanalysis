# Phase C1 — Camera / RTSP / ROI Configuration MVP

Date: 2026-05-25
Status: **MVP** — configuration storage and export only. No runtime
hot-reload, no front-end ROI drawing, no algorithm changes.

C1 lands the smallest useful configuration layer on top of the verified
single-camera evidence loop. After R0 (rebaseline), E1 (evidence MVP),
R1 (mainline consolidation), and R1.1 (rule entrypoint), C1 closes the
"how do operators tell the system about a camera" loop.

---

## 1. Background

After R1.1, the project has:

- a verified single-camera intrusion → snapshot/annotated/clip/annotated-clip
  pipeline behind `infra/docker-compose.phase3h-zmq.yml`,
- a registry-based behavior-rule entrypoint in
  `modules/savant_security/custom/rules/`,
- a `cameras.yml` consumed by the in-container Savant module (currently
  hand-edited).

What was still missing: any way for an operator to add a camera,
configure its ROI polygons, or set intrusion rule thresholds without
editing files in the repo. C1 fills that gap **without** touching the
running runtime — operators can POST configuration into the API today;
wiring that configuration into the live pipeline is deferred to C1.1.

## 2. Goals

- Add an RTSP / file-source camera through the API.
- Persist `source_id`, `camera_id`, `name`, `location`, `rtsp_url`,
  `enabled`, `gpu_id`.
- Persist polygon (and line / direction_line) ROIs per camera.
- Persist intrusion-rule parameters (`min_inside_ms`, `cooldown_s`,
  `severity`, `snapshot_required`, `clip_required`) per camera.
- Export the full configuration as a `cameras.yml`-compatible YAML doc.
- Validate everything at the API boundary, never at runtime.

Out of scope for C1 (explicit non-goals):
- Runtime hot-reload into the running phase3h-zmq Savant module.
- Front-end ROI drawing.
- Face / watchlist / live-search configuration.
- Loitering / crowd / fall / running / wall_climb parameters.
- Multi-rule-instance per (camera, rule_type) — see Section 7.

## 3. API surface

All endpoints are mounted under `/api/v1/cameras` and reuse the
existing `{data, error, request_id}` response envelope.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/cameras` | Create a camera |
| `GET`  | `/api/v1/cameras` | List all cameras |
| `GET`  | `/api/v1/cameras/{camera_id}` | Camera detail |
| `POST` | `/api/v1/cameras/{camera_id}/zones` | Create a polygon / line / direction_line zone |
| `GET`  | `/api/v1/cameras/{camera_id}/zones` | List zones for a camera |
| `POST` | `/api/v1/cameras/{camera_id}/rules` | Create an intrusion (or future) rule |
| `GET`  | `/api/v1/cameras/{camera_id}/rules` | List rules for a camera |
| `GET`  | `/api/v1/cameras/{camera_id}/config` | Aggregate camera + zones + rules JSON |
| `GET`  | `/api/v1/cameras/config/export` | `cameras.yml`-compatible YAML export |

### Validation summary

| Check | Status |
|---|---|
| `cameras.id` non-empty, globally unique | enforced (PK) |
| `cameras.source_id` non-empty, globally unique | enforced (UNIQUE) |
| `zone_name` unique per camera | enforced (UNIQUE) |
| `zone_type ∈ {polygon, line, direction_line}` | enforced |
| `polygon` requires ≥ 3 points | enforced |
| `line` / `direction_line` require exactly 2 points | enforced |
| `points[i]` shape `[x, y]` numeric | enforced |
| `rule_type` unique per camera (C1-lite) | enforced (UNIQUE) |
| `intrusion.config.zone` references existing zone | enforced (400) |
| `intrusion.config.min_inside_ms` positive int | enforced (400) |
| `intrusion.config.cooldown_s` non-negative int | enforced (400) |
| `intrusion.config.severity ∈ {low, medium, high}` | enforced (400) |
| intrusion defaults: `severity=medium`, `snapshot_required=true`, `clip_required=true` | applied on create |

`409 Conflict` is returned for duplicate camera id, duplicate zone_name
on a camera, and duplicate rule_type on a camera. `404 Not Found` is
returned when creating a zone or rule against an unknown `camera_id`.

## 4. Schema

Migration: `db/migrations/004_phase_c1_camera_config.sql`. It drops the
unused 001 placeholders (UUID-typed `cameras` / `camera_zones` /
`camera_rules` that were never wired anywhere — `events.camera_id` is
plain TEXT) and recreates them per the C1 spec. The same pattern was
used by `002_phase2e_events.sql` for the events table.

```sql
cameras (
  id            TEXT PRIMARY KEY,
  source_id     TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  rtsp_url      TEXT NOT NULL,
  site_id       TEXT,
  location      TEXT,
  gpu_id        INTEGER NOT NULL DEFAULT 0,
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
INDEX cameras_enabled_idx ON cameras(enabled);

camera_zones (
  id            BIGSERIAL PRIMARY KEY,
  camera_id     TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
  zone_name     TEXT NOT NULL,
  zone_type     TEXT NOT NULL,
  points        JSONB NOT NULL,
  payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at, updated_at
  UNIQUE (camera_id, zone_name)
)
INDEX camera_zones_camera_idx ON camera_zones(camera_id);

camera_rules (
  id            BIGSERIAL PRIMARY KEY,
  camera_id     TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
  rule_type     TEXT NOT NULL,
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  config        JSONB NOT NULL,
  created_at, updated_at
  UNIQUE (camera_id, rule_type)   -- C1-lite: one rule per type
)
INDEX camera_rules_camera_type_idx ON camera_rules(camera_id, rule_type);
```

`events`, `audit_logs`, `persons`, and any media/face-related schema
are **not** touched.

## 5. Export example

`GET /api/v1/cameras/config/export` returns `Content-Type: text/yaml`.
Sample body (with one camera, one polygon zone, one intrusion rule):

```yaml
cameras:
  cam_001:
    enabled: true
    source_id: phase3h
    name: Test Camera
    rtsp_url: rtsp://example.local/stream
    gpu_id: 0
    location: Test Area
    site_id: site_a
    zones:
      perimeter:
        type: polygon
        points:
          - [100, 300]
          - [900, 300]
          - [900, 700]
          - [100, 700]
    rules:
      intrusion:
        enabled: true
        zone: perimeter
        min_inside_ms: 1000
        cooldown_s: 30
        severity: medium
        snapshot_required: true
        clip_required: true
```

### Export rules

- Default: only `enabled=true` cameras are exported.
- `?include_disabled=true`: disabled cameras are included **with
  `enabled: false`** so an operator can review the disabled set.
- Database internal fields (`id` on zone/rule, `camera_id` redundancy,
  `created_at`, `updated_at`) are stripped.
- `zone_name` becomes the key under `cameras.<id>.zones`. `zone_type`
  becomes `type`. `points` is a list of `[x, y]` pairs.
- `rule_type` becomes the key under `cameras.<id>.rules`. The rule's
  `config` is flattened into the rule entry alongside `enabled`.

## 6. Current limitations

1. **No runtime hot-reload.** The phase3h-zmq Savant module continues
   reading its `cameras.yml` from the in-container path. C1 stores the
   data; it does not push it into the live runtime yet.
2. **No front-end ROI drawing.** C1 is an API and a schema. Operators
   POST `points` arrays directly.
3. **No multi-instance-per-rule_type.** A camera can have at most one
   `intrusion` rule under C1-lite. Multiple zones with multiple
   intrusion rules per camera require a future
   `(camera, rule_type, rule_name)` key.
4. **Validation only catches intrusion shape.** Future rule types
   (`loitering`, `crowd_gathering`, `fall`, ...) are accepted without
   strict config validation. Each rule will add its own validator in
   `app/schemas/cameras.py`.
5. **No camera update / delete endpoints.** C1 is create + read only.
   Deletions can still happen at the SQL level (ON DELETE CASCADE
   cleans up zones/rules). PATCH endpoints are deferred until the
   runtime can actually react to changes.
6. **Authentication / authorisation are out of scope.** Anyone with
   network access to the API can write configuration.

## 7. Next steps

- **C1.1** — feed the exported `cameras.yml` into the phase3h-zmq or
  `savant_security` config mount so the running Savant module honours
  API-managed configuration. This is the smallest possible step toward
  configuration actually changing pipeline behavior.
- **C1.2** — replace `BehaviorRulesPyFunc`'s in-container YAML read
  with a unified config loader that pulls from the FastAPI export
  endpoint (or shared file). Keeps the contract narrow.
- **B2.1** — loitering rule and its `apply_loitering_defaults` /
  `validate_loitering_config` pair in `app/schemas/cameras.py`. The
  registry from R1.1 already accepts new `rule_type` values.
- **C1.3** — relax `UNIQUE(camera_id, rule_type)` to allow multiple
  intrusion rules per camera once the runtime can iterate
  `(zone, rule_name)` pairs cleanly.
- **F1** — face intelligence config (SCRFD / ArcFace) layers on top of
  the same `cameras` row, not a separate config tree.

## 8. Related documents

| Document | Content |
|---|---|
| `docs/phase_r1_mainline_consolidation_plan.md` | Mainline consolidation plan + R1.1 entrypoint |
| `docs/phase_e1_alert_evidence_mvp.md` | Evidence-worker design (unchanged by C1) |
| `docs/media_output_directory_policy.md` | Media directory policy (unchanged by C1) |
| `docs/project_rebaseline_2026_05_25.md` | Authoritative roadmap (C1 falls under Section 6 "C1") |

---

*Written 2026-05-25. Phase C1 — configuration only.*
