# Phase C1.3 — Operator Camera Config CLI + ROI Polygon Validation MVP

Date: 2026-05-26
Status: **landed** — adds the operator-facing CLI for the C1 / C1.1 /
C1.2 plumbing, plus a clarified 3..10-vertex polygon rule enforced
identically at the API and at the Savant runtime loader.

C1.3 does NOT change C1.2's official adapter runtime architecture. It
sits *on top of* the already-verified API → exporter → controller →
evidence loop, and removes the operator friction of hand-rolling curl,
ROI coordinates, and two separate sub-scripts.

---

## 1. Why C1.3

C1.2 left the operator workflow at:

1. Compose ~120 characters of curl per camera, zone, and rule.
2. Author ROI coordinates as raw JSON in a one-liner.
3. Run `scripts/config/export_runtime_configs.py`.
4. Run `scripts/runtime/camera_source_controller.py start`.
5. ...and there is no way to *visually* preview a polygon before posting.

That works for a smoke test but is brittle for real operators. C1.3
collapses the surface into one CLI plus a Pillow-based preview.

## 2. ROI polygon rule (final, identical in API and loader)

| Zone type | Vertex count | Notes |
|---|---|---|
| `polygon` | **3 ≤ n ≤ 10** | Quadrilateral is just one shape; pentagons, hexagons up to 10-gons are all allowed. |
| `line` | exactly 2 | Trip-wire. |
| `direction_line` | exactly 2 | Directional trip-wire (oriented start → end). |

Each point is `[x, y]` of two numeric (int or float) coordinates. A
point must NOT be empty, NOT contain `null`, NOT contain a string.

Coordinate convention: pixel coordinates with origin at the **top-left**
of the camera frame, **x increasing rightward**, **y increasing
downward**. Points must be supplied in either clockwise or
counter-clockwise order along the zone boundary; either direction is
accepted.

**Out of scope:** self-intersection detection. A polygon whose edges
cross itself is currently accepted; the intrusion rule will treat it
according to the standard even-odd point-in-polygon algorithm. Adding
a self-intersection guard is a candidate for a future sub-phase.

Bound constants (locked by tests):

- `services/api/app/schemas/cameras.py`: `POLYGON_MIN_POINTS = 3`,
  `POLYGON_MAX_POINTS = 10`.
- `modules/savant_security/custom/services/camera_config.py`: same
  constants. The runtime loader will refuse any YAML the API accepted
  loosely, and vice versa.

Operator-facing error messages:

- `"polygon zone requires 3 to 10 points (got 2)"` — API 400
- `"line zone requires exactly 2 points (got 3)"` — API 400
- `"cameras.cam_001.zones.perimeter.points polygon requires 3 to 10 points (got 11)"` — loader `CameraConfigError`
- `"point #4 has non-numeric coordinate(s): '10,abc'"` — CLI parser

## 3. The CLI

`scripts/camera_config_cli.py`. Default API base URL: `http://localhost:8004`
(overridable via `--api-base-url` or `$VIDEO_ANALYTICS_API`).

### 3.1 Subcommand inventory

| Subcommand | Backing endpoint or script |
|---|---|
| `add-camera` | `POST /api/v1/cameras` |
| `add-zone` | `POST /api/v1/cameras/{camera_id}/zones` |
| `add-intrusion-rule` | `POST /api/v1/cameras/{camera_id}/rules` |
| `show-camera` | `GET /api/v1/cameras/{camera_id}/config` (rtsp_url redacted) |
| `export-runtime` | delegates to `scripts/config/export_runtime_configs.py` |
| `start-source` | delegates to `scripts/runtime/camera_source_controller.py start` |
| `stop-source` | delegates to `scripts/runtime/camera_source_controller.py stop` |
| `preview-roi` | Pillow — local only, no API call |

Exit codes are stable across subcommands: `0` ok, `1` transport / I/O,
`2` API 404 or unexpected, `3` API 409 (already exists), `4` validation
/ parser failure, `5` Pillow not installed.

### 3.2 Adding a local test video source — full flow

```bash
# 1) Define the camera (uri points at a file mounted into the source-adapter)
python scripts/camera_config_cli.py add-camera \
  --camera-id cam_001 \
  --source-id cam_001_src \
  --name "测试摄像头" \
  --location "测试区域" \
  --uri file:///testVideo/test.mp4 \
  --gpu-id 0 \
  --enabled true

# 2) Define a hexagonal ROI
python scripts/camera_config_cli.py add-zone \
  --camera-id cam_001 \
  --zone-name perimeter \
  --polygon "100,120;600,80;1200,160;1700,500;1300,960;250,860"

# 3) Add the intrusion rule
python scripts/camera_config_cli.py add-intrusion-rule \
  --camera-id cam_001 \
  --zone perimeter \
  --min-inside-ms 1000 \
  --cooldown-s 30 \
  --severity medium \
  --snapshot-required true \
  --clip-required true

# 4) Export the runtime config files
python scripts/camera_config_cli.py export-runtime

# 5) Restart Savant so it loads the fresh cameras.generated.yml
docker compose -f infra/docker-compose.c1-official-adapter.yml restart savant-security

# 6) Attach the source adapter (controller spawns a docker container)
python scripts/camera_config_cli.py start-source \
  --source-id cam_001_src \
  --testvideo-mount "$PWD/testVideo:/testVideo:ro"
```

### 3.3 Adding an RTSP camera

Identical to the file flow, but:

```bash
python scripts/camera_config_cli.py add-camera \
  --camera-id cam_north_gate \
  --source-id cam_north_gate_src \
  --name "North Gate" \
  --uri "rtsp://OPERATOR_INPUT_REDACTED@cam.local:554/stream" \
  --enabled true
```

The CLI prints the URI scheme (`rtsp`) only — credentials never appear
in logs. Do NOT paste real credentials into git commits, issue
threads, or PR descriptions.

`start-source` does not need `--testvideo-mount` for RTSP.

### 3.4 Adding a 6-vertex ROI

```bash
python scripts/camera_config_cli.py add-zone \
  --camera-id cam_001 \
  --zone-name perimeter \
  --polygon "100,120;600,80;1200,160;1700,500;1300,960;250,860"
```

Any 3..10 point count works. Coordinate order follows the polygon
boundary in either direction.

### 3.5 ROI preview

```bash
python scripts/camera_config_cli.py preview-roi \
  --image /data/video-analytics/media/evidence/events/<event_id>/snapshot.jpg \
  --polygon "100,120;600,80;1200,160;1700,500;1300,960;250,860" \
  --zone-name perimeter \
  --output /tmp/roi_preview.jpg
```

- The polygon is drawn as a red outline with circular vertex markers.
- A small label box is added near the first vertex when `--zone-name`
  is supplied.
- The source image is NOT overwritten — only `--output` is written.
- `--line "x1,y1;x2,y2"` is accepted as an alternative to `--polygon`.
- Output format follows the file extension (`.jpg` → quality 92, `.png`
  → lossless).
- Pillow is the only dependency; the evidence-worker already requires
  it, so no new runtime image needs a rebuild.

## 4. What about the C1.2 smoke?

**No GPU re-run needed.** The C1.2 smoke (`scripts/smoke/check_c1_2_official_adapter_runtime.sh`)
posts a four-vertex polygon: `[[100,100],[1820,100],[1820,980],[100,980]]`.
That falls inside the new 3..10 range and continues to pass validation
at both layers.

A future operator who wants to re-run the smoke after C1.3 can use the
new CLI in step 3 of the smoke flow instead of curl — the underlying
endpoints have not changed.

## 5. Updated diagrams

```
Operator
  └──▶ scripts/camera_config_cli.py
         ├── add-camera / add-zone / add-intrusion-rule / show-camera   ──▶ FastAPI
         ├── export-runtime          ──▶ scripts/config/export_runtime_configs.py
         ├── start-source / stop-source ──▶ scripts/runtime/camera_source_controller.py
         └── preview-roi             ──▶ Pillow (local-only)
```

## 6. Current limitations

1. **No web UI yet.** ROI is still entered as `"x,y;x,y;..."` strings.
   A future C1.4 / C1.5 could add a small Web ROI page that emits the
   same CLI command.
2. **No mouse-driven ROI picking.** The preview requires an existing
   snapshot.
3. **No hot-reload.** `export-runtime` writes to disk; Savant module
   only re-reads on `docker compose restart savant-security`. Metadata
   sink, video-file sink, API, event-worker, and evidence-worker stay
   running across that restart.
4. **No multi-rule-instance.** Inherited from C1 (`UNIQUE(camera_id,
   rule_type)`).
5. **No self-intersection detection.** Operators are expected to enter
   simple polygons.
6. **No multi-source auto-orchestration.** `start-source` attaches one
   adapter at a time; C1.3 has no batch loop. The controller still
   supports running multiple adapters concurrently — just one CLI
   invocation per source.
7. **No authn / authz on the API or the controller.** Network-level
   access control is the operator's responsibility.

## 7. Next steps

- **C1.4** (optional) — minimal Web ROI picker page that POSTs the same
  CLI commands. Would unlock non-engineer operators.
- **F1** — SCRFD face detection runtime integration. The F0 readiness
  harness is already on master; F1 layers onto C1's `cameras` row
  without runtime control changes.

## 8. Related documents

| Document | Content |
|---|---|
| `docs/phase_c1_camera_roi_config_mvp.md` | C1 API + schema |
| `docs/phase_c1_1_camera_config_runtime_bridge.md` | C1.1 bridge / loader |
| `docs/phase_c1_2_official_adapter_runtime_plan.md` | C1.2 topology design rationale |
| `docs/phase_c1_2_official_adapter_runtime.md` | C1.2 implementation notes |

---

*Written 2026-05-26. Phase C1.3 — operator CLI + tightened polygon rule.*
