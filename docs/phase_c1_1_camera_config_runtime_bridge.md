# Phase C1.1 — Camera Config Runtime Bridge

Date: 2026-05-25
Status: **bridge only** — produces a runtime-shaped config file and a
loader the future runtime will read. It does **not** wire the running
Savant pipeline to honour API-configured cameras yet.

## 1. What C1 already delivered

C1 (`3f4de7d`) gave the system everything needed to *describe* a camera
through the API:

- `POST /api/v1/cameras` / `/zones` / `/rules` with full validation.
- `GET /api/v1/cameras/{id}/config` (aggregate JSON).
- `GET /api/v1/cameras/config/export` returning a
  `cameras.yml`-compatible YAML document.

The C1 export uses this schema (used everywhere from here on)::

    cameras:
      cam_001:
        enabled: true
        source_id: phase3h
        name: Test Camera
        rtsp_url: rtsp://...
        gpu_id: 0
        location: Test Area     # optional
        site_id: site_a         # optional
        zones:
          perimeter:
            type: polygon
            points: [[x, y], ...]
            payload: {...}      # optional
        rules:
          intrusion:
            enabled: true
            zone: perimeter
            min_inside_ms: 1000
            cooldown_s: 30
            severity: medium
            snapshot_required: true
            clip_required: true

## 2. Why API → evidence is still NOT one-shot

The current runtime is `modules/savant_phase3h_zmq`, mounted into the
`savant-zmq` container at `/opt/savant/src/module`. Its
`BehaviorEventExportProbe` reads
`/opt/savant/src/module/config/cameras.yml` — i.e.
`modules/savant_phase3h_zmq/config/cameras.yml` on the host — with a
**different** schema than the C1 export:

| Layer                       | Today's runtime (phase3h_zmq)              | C1 export                            |
|-----------------------------|--------------------------------------------|--------------------------------------|
| Zone polygon                | `zones.{name}.polygon: [[x,y], ...]`       | `zones.{name}.type` + `points`       |
| Rule type / name            | `rules.{name}.rule_type` field             | `rules.{rule_type}` as the key       |
| `source_id`/`rtsp_url`/`gpu_id` | absent                                 | present                              |
| Validation                  | best-effort defaults                       | strict at parse time                 |

So:

1. `POST /api/v1/cameras` writes to PostgreSQL — but nothing reads it.
2. `GET /api/v1/cameras/config/export` returns the right shape — but
   no runtime consumes that shape today.

C1.1 lands the two missing pieces of the bridge:

- a CLI that pulls the export and writes it to a runtime-shaped file,
- a pure-Python loader that future-`BehaviorRulesPyFunc` (R1.1
  entrypoint) will use to read that file.

Wiring those into the running pipeline is C1.2.

## 3. The bridge components landed by C1.1

### 3.1 Export CLI — `scripts/config/export_cameras_yml.py`

```
python scripts/config/export_cameras_yml.py \
  --api-base-url http://localhost:8001 \
  --output modules/savant_security/config/cameras.generated.yml
```

Behavior:

- Issues `GET {api-base-url}/api/v1/cameras/config/export` via stdlib
  `urllib.request`.
- Adds `?include_disabled=true` when `--include-disabled` is set.
- Validates the response body with `yaml.safe_load` and rejects
  responses lacking a top-level `cameras` mapping.
- Creates the output's parent directory on demand
  (`os.makedirs(..., exist_ok=True)`).
- Writes the body verbatim (no re-serialisation churn).
- **Never logs `rtsp_url`.** Only `camera_id`, `source_id`, `name`,
  `enabled` are printed. RTSP URLs frequently carry credentials, so
  they must not land in CI logs or operator tickets.
- Exit codes: `0` ok, `1` HTTP / transport, `2` invalid YAML,
  `3` missing `cameras` key, `4` write error.
- `VIDEO_ANALYTICS_API` env var supplies `--api-base-url` as a fallback.

### 3.2 Loader — `modules/savant_security/custom/services/camera_config.py`

```python
from custom.services.camera_config import load_camera_config

bundle = load_camera_config("/opt/savant/src/module/config/cameras.generated.yml")
cam = bundle.get_camera("cam_001")
cam = bundle.get_by_source_id("phase3h")
zone = bundle.get_zone("cam_001", "perimeter")
rule = bundle.get_rule("cam_001", "intrusion")
for cam in bundle.iter_enabled_cameras():
    ...
```

Behavior:

- Pure Python — no Savant, no DeepStream, no PostgreSQL, no HTTP.
- Validates everything important *at load time*:
  - required strings (`source_id`, `name`, `rtsp_url`) non-empty,
  - `gpu_id` integer,
  - zone `type ∈ {polygon, line, direction_line}`,
  - polygon ≥ 3 points; line / direction_line == 2 points,
  - each point `[x, y]` numeric,
  - intrusion `zone` refers to an existing zone,
  - intrusion `min_inside_ms > 0`, `cooldown_s ≥ 0`,
  - intrusion `severity ∈ {low, medium, high}`,
  - `snapshot_required` / `clip_required` boolean when provided,
  - global `source_id` uniqueness across cameras.
- Raises `CameraConfigError` (subclass of `ValueError`) with a
  fully-qualified path (`cameras.cam_001.rules.intrusion.cooldown_s`)
  on any violation.
- `iter_enabled_cameras()` returns enabled cameras sorted by id for
  deterministic runtime iteration.

## 4. Target runtime config path

Current target file (the bridge writes here):

```
modules/savant_security/config/cameras.generated.yml
```

Container target (the future `BehaviorRulesPyFunc` will read here when
C1.2 wires it in):

```
/opt/savant/src/module/config/cameras.generated.yml
```

(`modules/savant_security` mounted at `/opt/savant/src/module`.)

The existing `modules/savant_phase3h_zmq/config/cameras.yml` is **not
touched** by C1.1. The currently running pipeline continues to use it.
The two files coexist until C1.2 switches the runtime.

## 5. Not hot-reload

The loader reads YAML at construction time. There is no inotify watch,
no signal handler, no API callback. To apply a configuration change to
a running pipeline:

1. POST the change through the API (PostgreSQL is updated).
2. Run the export script (file on disk is updated).
3. Restart the relevant Savant container.

A future C1.3 / C2 phase can add SIGHUP-based reload or a config bus,
but it is not required for "describe one camera, observe one event".

## 6. C1.2 acceptance target

The next step's acceptance criterion is:

> Starting from an empty database, an operator
>
> 1. POSTs a camera (`cam_001`, `source_id=phase3h`, file:// or
>    rtsp:// source), one polygon zone, and one intrusion rule;
> 2. runs `scripts/config/export_cameras_yml.py` and confirms
>    `modules/savant_security/config/cameras.generated.yml` matches the
>    API JSON;
> 3. starts the savant_security-driven runtime (compose target TBD in
>    C1.2);
> 4. observes the existing E1 evidence loop produce
>    `snapshot.jpg` / `annotated_snapshot.jpg` / `clip_raw.mp4` /
>    `clip_annotated.mp4` for the configured zone, with no further
>    hand-editing of `cameras.yml`.

To unlock that, C1.2 must:

- wire `BehaviorRulesPyFunc` (R1.1) into a runnable `module.yml`
  pointing at `cameras.generated.yml`,
- provide a compose entry (or update `phase3h-zmq.yml`) that mounts
  `modules/savant_security` instead of `modules/savant_phase3h_zmq`,
- propagate `source_id` from the API config to the source-adapter
  `SOURCE_ID` env var (today it's hardcoded to `phase3h`).

C1.1 deliberately does NOT do any of that. The bridge file and the
loader are inert in production until C1.2 connects them.

## 7. Out of scope (explicit non-goals)

- Face / SCRFD / ArcFace configuration.
- Loitering / crowd_gathering / fall / wall_climb rule parameters.
- evidence-worker behavior or media output directory.
- Multi-camera / multi-GPU performance tuning.
- Replay Service or any production single-ingestion topology shift.
- New `savant_phaseXX` modules.

## 8. Related documents

| Document | Content |
|---|---|
| `docs/phase_c1_camera_roi_config_mvp.md` | C1 API + DB schema |
| `docs/phase_r1_mainline_consolidation_plan.md` | Mainline + R1.1 entrypoint |
| `docs/phase_e1_alert_evidence_mvp.md` | Evidence loop (unchanged) |
| `docs/current_runbook_single_camera_evidence.md` | Operational runbook (unchanged) |

---

*Written 2026-05-25. Phase C1.1 — bridge only, not runtime activation.*
