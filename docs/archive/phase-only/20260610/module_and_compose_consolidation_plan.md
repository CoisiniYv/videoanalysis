# Module & Compose Consolidation Plan

Date: 2026-05-25
Status: **PLAN ONLY** — No files moved or deleted in this phase.

## 1. Current Module Inventory

### 1.1 Historical phase verification modules

| Module | Purpose | Archive? |
|---|---|---|
| `savant_phase1d` | YOLO26-pose converter + bbox selector | Yes |
| `savant_phase1e` | MetadataHandoffProbe — frame metadata extraction | Yes |
| `savant_phase1f` | Observation adapter — PersonPoseObservation conversion | Yes |
| `savant_phase2a` | Behavior rules initial structure | Yes |
| `savant_phase2b` | Behavior debug probe | Yes |
| `savant_phase2c` | SecurityEvent schema + exporter factory + intrusion rule | Yes — currently active in Phase 3B |

### 1.2 Current POC / active modules

| Module | Purpose | Future |
|---|---|---|
| `savant_security` | Intended main module — contains unified pipeline | Converge everything here |
| `savant_phase3h_zmq` | Official ZMQ source/sink POC (zeromq_source_bin) | Reference for ZMQ topology |

### 1.3 Replay / sink auxiliary modules

| Module | Purpose | Future |
|---|---|---|
| `savant_replay` | Replay Service frame storage config | Reference |
| `savant_video_file_sink` | Video file sink adapter config | Reference |
| `savant_smoke` | Minimal smoke test module | Reference |

### 1.4 In-progress (not yet committed)

| Module | Purpose | Policy |
|---|---|---|
| `savant_phase1d` init files | Unrelated in-progress module | Not part of mainline |

### 1.5 Consolidation target

```
modules/
  savant_security/          ← Sole main module (future)
  _archive/                 ← Historical modules (optional, deferred to R1)
    savant_phase1d/
    savant_phase1e/
    savant_phase1f/
    savant_phase2a/
    savant_phase2b/
    savant_phase2c/
    savant_phase3h_zmq/
    savant_replay/
    savant_video_file_sink/
    savant_smoke/
```

**Key principle**: Do NOT create new `savant_phaseXX` copy modules. All new capabilities go into `savant_security`.

---

## 2. Current Compose Inventory

### 2.1 Historical smoke composes

| File | Phase | Purpose |
|---|---|---|
| `docker-compose.phase1c.yml` | 1C | GPU inference basics |
| `docker-compose.phase1d.yml` | 1D | YOLO26-pose converter |
| `docker-compose.phase1e.yml` | 1E | Metadata handoff |
| `docker-compose.phase1f.yml` | 1F | Observation adapter |
| `docker-compose.phase2b.yml` | 2B | Behavior debug |
| `docker-compose.phase2c.yml` | 2C | Security event dry run |
| `docker-compose.phase2d.yml` | 2D | Redis event export |
| `docker-compose.phase2e.yml` | 2E | Event worker ingest |
| `docker-compose.phase2f.yml` | 2F | API events |
| `docker-compose.phase2g.yml` | 2G | Alert WebSocket |
| `docker-compose.phase2h.yml` | 2H | Event status API |
| `docker-compose.phase2i.yml` | 2I | Savant → backend e2e |

### 2.2 Media / Replay POC composes

| File | Phase | Purpose |
|---|---|---|
| `docker-compose.phase3a.yml` | 3A | Replay + Video File Sink |
| `docker-compose.phase3b.yml` | 3B | Replay media reliability (active) |

### 2.3 Official ZMQ POC composes

| File | Phase | Purpose |
|---|---|---|
| `docker-compose.phase3h-zmq.yml` | 3H | Official ZMQ source/sink (active POC) |

### 2.4 Other composes

| File | Purpose |
|---|---|
| `docker-compose.dev.yml` | Development backend (API, workers, DB, Redis) |
| `docker-compose.savant-smoke.yml` | Minimal Savant smoke test |

### 2.5 Future compose target

```
infra/
  docker-compose.dev.yml          ← Backend services + Savant (dev mode)
  docker-compose.gpu.yml          ← GPU inference with Savant (gpu profile)
  docker-compose.replay.yml       ← Replay service (replay profile or separate)
  docker-compose.prod.yml         ← Production deployment (all profiles)
```

**Key principle**: Do NOT create new `docker-compose.phaseXX.yml` files for new capabilities. Use profiles or extend existing composes.

---

## 3. Consolidation Principles

1. **This phase (R0) is plan-only.** No files moved or deleted.
2. **Phase R1 will execute the consolidation.** Move archive candidates to `_archive/`, converge active code into `savant_security`.
3. **All new functionality goes into `savant_security`.** The ZMQ source/sink pattern from `savant_phase3h_zmq` should be merged into `savant_security` as the default pipeline source.
4. **Compose consolidation requires careful migration.** The dev/gpu/replay/prod split must be designed before moving compose files.
5. **Smoke tests are preserved.** All smoke scripts in `scripts/smoke/` remain functional — even those for archived phases (for regression reference).

---

## 4. What NOT to do

- Do NOT create `savant_phaseXY` copy modules.
- Do NOT create `docker-compose.phaseXY.yml` files for new features.
- Do NOT delete historical modules or composes before R1.
- Do NOT break existing smoke scripts.
- Do NOT change module logic during consolidation.

---

*Written 2026-05-25. Execution deferred to Phase R1.*
