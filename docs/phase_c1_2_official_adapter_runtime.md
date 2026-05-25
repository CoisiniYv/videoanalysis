# Phase C1.2 — Official Adapter Camera Runtime Control

Date: 2026-05-25
Status: **shipped** — runtime is wired, smoke is gated on a real video
source. See `docs/phase_c1_2_official_adapter_runtime_plan.md` for the
topology design rationale.

C1.2 closes the gap from C1.1's "configure a camera through the API"
to "configure a camera and actually run the existing E1 evidence loop
on it", *the official way*: external adapter ⇄ ZMQ ⇄ module, with
adapter lifecycle managed by a CLI controller.

---

## 1. Goal recap

Configure a camera + ROI + intrusion rule through the FastAPI service,
then bring up evidence on a real video stream without editing any
runtime YAML by hand and without restarting GPU inference each time a
source is added or removed.

## 2. Pieces that landed

| Component | Path | Purpose |
|---|---|---|
| Combined runtime exporter | `scripts/config/export_runtime_configs.py` | Writes both module and adapter config files from the API export |
| Camera source controller | `scripts/runtime/camera_source_controller.py` | CLI: list / start / stop / status of gstreamer adapter containers |
| Rule-runtime adapter | `modules/savant_security/custom/services/rule_runtime.py` | Bundle → per-source `{camera, rules, TrackStateStore}` map |
| Re-wired pyfunc | `modules/savant_security/custom/pyfuncs/behavior_rules.py` | C1.1 loader + per-source routing + severity/snapshot/clip pass-through |
| Module entrypoint | `modules/savant_security/module.yml` | `zeromq_source_bin` → YOLO26-pose → nvtracker → BehaviorRulesPyFunc |
| Tracker config + converter | `modules/savant_security/config/config_tracker_NvDCF_perf.yml`, `modules/savant_security/custom/converters/yolo26_pose.py` | Copied from phase3h-zmq so module.yml is self-contained |
| Compose | `infra/docker-compose.c1-official-adapter.yml` | Long-lived services only; adapters are controller-spawned |
| Smoke | `scripts/smoke/check_c1_2_official_adapter_runtime.sh` | End-to-end, gated on `C1_TEST_SOURCE_URI` |

The legacy `modules/savant_phase3h_zmq` and
`infra/docker-compose.phase3h-zmq.yml` are **untouched**. They keep
running the previous E1 loop as-is.

## 3. camera_id ↔ source_id

C1.2 follows the project rule: `camera_id` is the API/DB identifier
(human-meaningful), `source_id` is the Savant adapter/runtime
identifier (ZMQ frame-stream key). They map 1-1 through
`cameras.source_id UNIQUE` and through every config file produced by
the exporter.

| File | Keyed by | Reads | Contains source_id |
|---|---|---|---|
| `cameras.generated.yml` | `camera_id` | Module pyfunc | Yes (per camera) |
| `sources.generated.yml` | `camera_id` | Controller | Yes (per camera) |
| `SecurityEvent` | n/a | event-worker | `source_id` set from frame; `camera_id` from runtime config |

When `BehaviorRulesPyFunc` sees a frame with `source_id = X` it routes
to the runtime for that source_id, evaluates rules, then stamps the
emitted `SecurityEvent.camera_id` with the configured camera_id (NOT
the raw source_id) so downstream API/DB consumers see the
business-side identifier.

## 4. API → adapter flow

```text
POST /api/v1/cameras
POST /api/v1/cameras/{id}/zones
POST /api/v1/cameras/{id}/rules
        │
        ▼
GET /api/v1/cameras/config/export   (text/yaml)
        │
        ▼  python scripts/config/export_runtime_configs.py
        │  ── writes ──▶ modules/savant_security/config/cameras.generated.yml
        │  ── writes ──▶ infra/generated/sources.generated.yml
        │
        ▼  docker compose -f infra/docker-compose.c1-official-adapter.yml restart savant-security
        │  ── savant-security reloads cameras.generated.yml at startup
        │
        ▼  python scripts/runtime/camera_source_controller.py start --source-id <id>
        │  ── docker run gstreamer-adapter → ZMQ → savant-security:5555
        │
        ▼  Real intrusion event hits Redis → event-worker → PostgreSQL
        ▼  evidence-worker → /media/evidence/events/{event_id}/{snapshot,annotated_snapshot,clip_raw,clip_annotated}
        ▼  FastAPI serves /media/* URLs (unchanged from E1)
```

## 5. export_runtime_configs.py

```
python scripts/config/export_runtime_configs.py \
  --api-base-url http://localhost:8004 \
  --module-config-output modules/savant_security/config/cameras.generated.yml \
  --sources-output infra/generated/sources.generated.yml

# Optional flags
#   --include-disabled       keep disabled cameras in the output with enabled=false
#   --zmq-endpoint           override default dealer+connect:tcp://savant-security:5555
#   --adapter-type           default gstreamer (only choice today)
```

Both files are written atomically (`*.tmp` + `os.replace`). Parent
directories are auto-created. The console log prints only
`camera_id / source_id / name / enabled` — never `rtsp_url`. Exit
codes: 0 ok, 1 HTTP error, 2 invalid YAML, 3 missing `cameras` key,
4 file-write error, 5 sources-doc validation error.

## 6. camera_source_controller.py

```
# List configured sources (rtsp_url redacted; only scheme is shown)
python scripts/runtime/camera_source_controller.py list \
  --sources infra/generated/sources.generated.yml

# Attach one source — Docker container name is video-analytics-source-{source_id}
python scripts/runtime/camera_source_controller.py start \
  --sources infra/generated/sources.generated.yml \
  --source-id phase3h \
  --network c1-official-adapter_default \
  --testvideo-mount /home/user/video-analytics/testVideo:/testVideo:ro

# Status (queries the deterministic container name)
python scripts/runtime/camera_source_controller.py status --source-id phase3h

# Detach — module keeps running, only the adapter container is removed
python scripts/runtime/camera_source_controller.py stop --source-id phase3h
```

Behavior:

- `file://` URIs route to the gstreamer adapter's `video_loop.sh` and
  the `file://` prefix is stripped so `LOCATION` is a plain path.
- `rtsp://` / `rtsps://` URIs route to `rtsp_source.sh`.
- Disabled sources cannot be started.
- Unknown `source_id` returns a clear non-zero exit code.
- Docker failures surface as non-zero exit codes — there is no silent
  "assumed ok".
- The redacted command line is printed at start time so an operator
  can audit the spawned container without seeing credentials.

Exit codes: 0 ok, 1 sources file load failure, 2 unknown source, 3
disabled source, 4 docker run failure, 5 docker rm failure, 6 docker
ps failure, 1 (status) for "no container".

## 7. docker-compose.c1-official-adapter.yml

```bash
# Bring up long-lived services (NO source adapters)
docker compose -f infra/docker-compose.c1-official-adapter.yml up -d \
  redis postgres api savant-security event-worker metadata-sink video-file-sink

# Run evidence-worker on demand (one-shot)
docker compose -f infra/docker-compose.c1-official-adapter.yml run --rm evidence-worker

# Tear down
docker compose -f infra/docker-compose.c1-official-adapter.yml down
```

The compose deliberately ships **without** a source-adapter service:
source lifecycle is the controller's job. Container names use the
`c1-official-*` prefix and host ports (`8004` API, `5438` PG, `6385`
Redis, `5555/5556` Savant ZMQ) are chosen to not collide with
phase3h-zmq.

## 8. Smoke

```bash
C1_TEST_SOURCE_URI=file:///testVideo/test.mp4 \
  bash scripts/smoke/check_c1_2_official_adapter_runtime.sh
```

The smoke is gated on the operator providing a real source URI; it
exits with code 77 (skip) when `C1_TEST_SOURCE_URI` is missing or the
host lacks Docker / a GPU. It does the full end-to-end loop:
configure → export → restart savant → controller start → wait for
events → controller stop → evidence-worker → verify snapshots / clips
on disk and over the API.

The smoke explicitly checks that `savant-security` stays running
after the source detaches — that is the official adapter model's
contract.

## 9. Current limitations

1. **CLI controller, not a daemon.** Adding or removing cameras is an
   explicit operator action. C1.3 will likely add an API on top of the
   controller.
2. **No full hot-reload.** Editing `cameras.generated.yml` requires a
   `docker compose restart savant-security`. The metadata-sink,
   video-file-sink, API, event-worker, and evidence-worker stay up;
   only GPU inference rebinds.
3. **Single rule instance per (camera, rule_type).** Inherited from
   C1's DB schema.
4. **No multi-camera load test.** This phase exercises one adapter at
   a time. The model supports multiple, but evidence-worker capacity,
   GPU throughput, and Redis stream backpressure haven't been
   benchmarked.
5. **Retina RTSP Service deferred.** `adapter_type` is fixed to
   `gstreamer` in the source schema. Adding Retina is a future
   sub-phase (estimated C1.4) that swaps the controller's spawn
   backend without touching the camera-config layer.
6. **No authz on the API or the controller.** Anyone with network
   access can add a camera or spawn an adapter.
7. **Module restart cost.** Each `savant-security restart` rebuilds
   the TensorRT engine cache hits and warms up nvinfer — typically
   5–15 seconds.

## 10. Next steps

- **C1.3** — Camera enable/disable PATCH endpoint + controller
  integration. The exporter already emits `enabled` per source; this
  step wires "API disables a camera" to "controller stops its
  adapter".
- **C1.4** — Retina RTSP Service driver. The `sources.generated.yml`
  schema reserves `adapter_type` for this; controller gains a
  `retina` backend that calls Retina's REST API.
- **F1** — SCRFD face detection MVP. Loads onto the existing
  `cameras.generated.yml` (the loader already accepts arbitrary
  rule_types; F1 just adds `face` to the rule registry).

Yes — the C1.2 changes are sufficient to start F1 work. F1 does not
need any further runtime control surface.

---

*Written 2026-05-25. Phase C1.2 — official adapter / module model live.*
