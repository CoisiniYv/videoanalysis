# Phase R1 — Mainline Module / Compose Consolidation Plan

Date: 2026-05-25
Status: **PLAN ONLY** — R1 does no file moves or rewrites. R1 records the
single-camera evidence closure as the future mainline target and freezes
the historical phase tree as POC / reference.

This document supersedes `docs/module_and_compose_consolidation_plan.md`
(R0 plan-only inventory) by tying the inventory to the verified E1 closure
and naming the future module / compose layout.

---

## 1. Verified mainline capability (E1 closure)

The following capability is verified and accepted as of HEAD `9f0c87d`:

```
testVideo/test.mp4
  -> source-adapter (video_loop.sh, ZMQ DEALER → savant-zmq:5555)
    -> savant-zmq (zeromq_source_bin ROUTER bind)
      -> YOLO26-pose + nvtracker + intrusion rule
        -> Redis security.events
          -> event-worker -> PostgreSQL events table
          -> ZMQ sink pub+bind:tcp://0.0.0.0:5556
            -> metadata-sink (metadata_json.py) -> metadata.json
            -> video-file-sink (video_files.py) -> video.mov
              -> evidence-worker (one-shot)
                -> snapshot.jpg
                -> annotated_snapshot.jpg
                -> clip_raw.mp4
                -> clip_annotated.mp4
                -> PostgreSQL writes (snapshot_path, clip_path, payload.media.*)
                  -> FastAPI /api/v1/events/{id} + /media static
```

Capabilities accepted into the mainline:

| Capability | Source | Evidence |
|---|---|---|
| YOLO26-pose ONNX/TensorRT inference | `modules/savant_security` (config) + Phase 1d converter | Phase 3H smoke 13/13 |
| nvtracker track_id assignment | Savant nvtracker | Phase 3H smoke verified |
| Intrusion rule | `custom/rules/intrusion.py` | Phase 2C / 2I verified |
| Redis security.events export | `RedisStreamEventExporter` | Phase 2D verified |
| event-worker -> PostgreSQL | `services/event-worker` | Phase 2E / 2I verified |
| FastAPI events + media URLs | `services/api` | Phase 2F / 3C / 3E verified |
| evidence-worker (one-shot) | `services/evidence-worker` | Phase E1 / E1.1a–c verified |
| Snapshot extraction | `services/evidence-worker/app/evidence.py` | E1 manual verified |
| Annotated snapshot (bbox + label) | `services/evidence-worker/app/bbox_draw.py` | E1.1 manual verified |
| Raw clip (ffmpeg `-c copy`) | `services/evidence-worker/app/evidence.py` | E1 manual verified |
| Annotated clip (libx264, CRF 18) | `services/evidence-worker/app/evidence.py` | E1.1b/c manual verified |
| Media output directory lockdown | `docs/media_output_directory_policy.md` + check script | E1.1a smoke verified |

Reference evidence run: `docs/phase_e1_run_report.md`.

---

## 2. POC / reference content (NOT mainline)

The items below remain in tree as **read-only reference**. They are not
deleted, not moved, and not rebuilt during R1. New work must not extend
them.

### 2.1 Compose files

| File | Status | Why kept |
|---|---|---|
| `infra/docker-compose.phase3h-zmq.yml` | Active POC — currently the only working E1 entrypoint | Hosts the verified single-ingestion pipeline + evidence-worker until R1.x consolidates compose |
| `infra/docker-compose.phase3b.yml` | Frozen — Replay bypass topology | Documented as dual-loop dev path; do not run continuous sinks |
| `infra/docker-compose.phase1c.yml` … `phase2i.yml` | Historical phase smoke composes | Preserved for regression reference |
| `infra/docker-compose.phase3a.yml` | Historical Replay POC | Preserved for media validation reference |
| `infra/docker-compose.savant-smoke.yml` | Minimal Savant smoke | Preserved for module sanity checks |
| `infra/docker-compose.dev.yml` | Backend dev stack (api / workers / redis / postgres) | Will fold into future `docker-compose.dev.yml` target |

### 2.2 Savant modules

| Module | Status | Why kept |
|---|---|---|
| `modules/savant_security/` | Future sole main module — currently minimal | Receives all future rule + face work |
| `modules/savant_phase3h_zmq/` | Active POC backing E1 — official ZMQ source/sink | Reference for ZMQ topology to merge into `savant_security` |
| `modules/savant_phase1d/` … `phase2c/` | Historical phase modules | Read-only |
| `modules/savant_replay/` | Replay service module config | Reference for future production Replay path |
| `modules/savant_video_file_sink/` | Video file sink config | Reference |
| `modules/savant_smoke/` | Minimal smoke module | Reference |

### 2.3 Topologies

| Topology | Status |
|---|---|
| Phase 3H single-ingestion ZMQ (E1) | Verified dev path. Used by `docker-compose.phase3h-zmq.yml`. Frame+metadata aligned. Event-to-frame match is approximate (track_id scan). |
| Phase 3B Replay bypass (dual file loops) | Frozen. Documented as temporary in `docs/production_ingestion_topology_policy.md`. Continuous sinks must not run. |
| Production Replay topology (RTSP -> Replay -> Savant -> event-triggered clip) | Designed only. Not implemented. Future R-series phase. |

---

## 3. Future module target

```
modules/
  savant_security/                      # sole main module
    module.yml                          # pipeline definition
    config/                             # cameras, model configs, etc.
    custom/
      converters/                       # YOLO26-pose, future face converters
      pyfuncs/                          # frame probe, behavior export probe
      rules/                            # intrusion, loitering, crowd, fall, …
      models/                           # SecurityEvent, observation schemas
      services/                         # Redis exporter, etc.
```

Rules of engagement:

1. `modules/savant_security/` is the **only** module receiving new behavior
   rule / face / pipeline work.
2. **`BehaviorRulesPyFunc` is the single rule entrypoint.** All new
   behavior rules MUST be added under
   `modules/savant_security/custom/rules/` and wired through
   `BehaviorRulesPyFunc`. Do not create new `savant_phaseXX` modules.
3. The ZMQ source/sink pattern proved by `savant_phase3h_zmq` is the
   eventual `savant_security` default source — convergence is a future
   sub-phase, not part of R1.
4. Historical modules (`savant_phase1d`–`savant_phase2c`,
   `savant_phase3h_zmq`, `savant_replay`, `savant_video_file_sink`,
   `savant_smoke`) remain in place for now. Archival under
   `modules/_archive/` is deferred to a later sub-phase and must not be
   attempted in R1.

---

## 4. Future compose target

R1 records the intended compose layout. R1 does **not** move, rename, or
delete any compose file.

```
infra/
  docker-compose.dev.yml         # postgres, redis, api, workers (backend only)
  docker-compose.gpu.yml         # source-adapter + savant_security (GPU pipeline)
  docker-compose.evidence.yml    # evidence-worker + media volume mounts
  docker-compose.replay.yml      # future production Replay path
                                 # (RTSP -> Replay -> Savant -> event-triggered clip)
```

Rules of engagement:

1. New capabilities must extend the four target composes above using
   profiles, env overrides, or includes. Do **not** create new
   `docker-compose.phaseXX.yml` files.
2. Until the migration runs, `docker-compose.phase3h-zmq.yml` remains the
   only working E1 entrypoint. See
   `docs/current_runbook_single_camera_evidence.md` for the operational
   procedure.
3. Historical phase composes are not deleted in R1. Smoke scripts under
   `scripts/smoke/` continue to reference them.
4. The production Replay topology (`docker-compose.replay.yml`) is a
   placeholder for the eventual single-ingestion event-triggered Replay
   path described in `docs/production_ingestion_topology_policy.md`.

---

## 5. R1 scope boundary

R1 **does not**:

- move, rename, or delete any module under `modules/`;
- move, rename, or delete any compose under `infra/`;
- change behavior rule, evidence-worker, Savant module, or API code;
- introduce new behavior rules (loitering / crowd_gathering / fall);
- introduce face pipeline (SCRFD / ArcFace / pgvector / watchlist / live
  search);
- run multi-camera load tests;
- start or restart the Phase 3B Replay bypass stack.

R1 **does**:

- record the verified single-camera evidence closure (Section 1);
- name the future module and compose targets (Sections 3, 4);
- archive the E1 run report into `docs/phase_e1_run_report.md`;
- add the operational runbook at
  `docs/current_runbook_single_camera_evidence.md`.

Subsequent R1.x sub-phases will execute the moves once the rule / face
work has stabilised against the new module layout.

---

## 6. Next phase suggestions

After R1 lands, the recommended order (subject to user direction):

1. **R1.1** — wire `BehaviorRulesPyFunc` skeleton in
   `modules/savant_security/custom/rules/` (no behavior change; just the
   entrypoint), so future rules drop in without new phase modules.
2. **B2.1** — loitering rule + harness test + smoke (the next functional
   gap, per `docs/project_rebaseline_2026_05_25.md` Section 6).
3. **B2.2 / B2.3** — crowd_gathering and fall rules.
4. **R1.2** — compose consolidation into the four-file target once enough
   rules exist to validate the gpu / evidence split.
5. **F1+** — face intelligence pipeline (deferred but not indefinite).

The full revised mainline order lives in
`docs/project_rebaseline_2026_05_25.md` Section 6.

---

## 7. Related documents

| Document | Content |
|---|---|
| `docs/project_rebaseline_2026_05_25.md` | Authoritative roadmap and deviation log |
| `docs/module_and_compose_consolidation_plan.md` | R0 plan-only inventory (predecessor to this file) |
| `docs/phase_e1_alert_evidence_mvp.md` | Phase E1 evidence generation design |
| `docs/phase_e1_run_report.md` | Captured E1 verification run (this commit) |
| `docs/current_runbook_single_camera_evidence.md` | Operational runbook for the verified E1 path |
| `docs/media_output_directory_policy.md` | Mandatory media output directory policy |
| `docs/production_ingestion_topology_policy.md` | Production single-ingestion topology policy |

---

*Written 2026-05-25. Phase R1 — planning only, no file moves.*
