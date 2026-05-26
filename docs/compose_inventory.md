# Compose Inventory

Date: 2026-05-26
Status: **inventory only** — no files moved, no files deleted, no service
logic touched.

This file is the authoritative answer to "which compose do I run?"
when a developer or future Claude session opens `infra/`.

---

## 1. Current main file

**`infra/docker-compose.c1-official-adapter.yml`** is the current main
runtime. It implements the C1.2 official adapter ↔ module topology
and was the target of the verified C1.2 smoke. All operator workflows
(`scripts/camera_config_cli.py`, the C1.3 CLI, the smoke script) point
at this compose. Future face-pipeline phases (F1.1b, F1.2, F2, F3)
will integrate into this compose, not a sibling.

Bring-up:

```bash
docker compose -f infra/docker-compose.c1-official-adapter.yml up -d \
  redis postgres api savant-security event-worker metadata-sink video-file-sink
```

See `docs/phase_c1_2_official_adapter_runtime.md` for the full
runbook.

## 2. Classification table

| File | Class | Notes |
|---|---|---|
| `docker-compose.c1-official-adapter.yml` | **current-main** | C1.2 runtime; mounted with `modules/savant_security`; controller-managed source adapters |
| `docker-compose.dev.yml` | **future-target** | Filename matches the R1 §4 reserved slot (backend-only dev stack: redis + postgres + api + event-worker). Current content (`phase0-dev`) is a pre-R1 partial; R1.x consolidation will refine it. Not used by current-main. |
| `docker-compose.phase1c.yml` | **legacy-phase-poc** | Phase 1C smoke (GPU inference basics) |
| `docker-compose.phase1d.yml` | **legacy-phase-poc** | Phase 1D smoke (YOLO26-pose converter) |
| `docker-compose.phase1e.yml` | **legacy-phase-poc** | Phase 1E smoke (metadata handoff) |
| `docker-compose.phase1f.yml` | **legacy-phase-poc** | Phase 1F smoke (observation adapter) |
| `docker-compose.phase2b.yml` | **legacy-phase-poc** | Phase 2B smoke (behavior debug probe) |
| `docker-compose.phase2c.yml` | **legacy-phase-poc** | Phase 2C smoke (security event dry run) |
| `docker-compose.phase2d.yml` | **legacy-phase-poc** | Phase 2D smoke (Redis event export) |
| `docker-compose.phase2e.yml` | **legacy-phase-poc** | Phase 2E smoke (event-worker ingest) |
| `docker-compose.phase2f.yml` | **legacy-phase-poc** | Phase 2F smoke (FastAPI events) |
| `docker-compose.phase2g.yml` | **legacy-phase-poc** | Phase 2G smoke (alert WebSocket) |
| `docker-compose.phase2h.yml` | **legacy-phase-poc** | Phase 2H smoke (event status API) |
| `docker-compose.phase2i.yml` | **legacy-phase-poc** | Phase 2I smoke (Savant → backend e2e) |
| `docker-compose.phase3a.yml` | **legacy-phase-poc** | Phase 3A smoke (Replay + video-file-sink POC) |
| `docker-compose.phase3b.yml` | **legacy-phase-poc** | Phase 3B Replay-bypass topology. **CLAUDE.md §9.5 explicitly bans starting its continuous sinks.** |
| `docker-compose.phase3h-zmq.yml` | **legacy-phase-poc** | Phase 3H official ZMQ POC. Was the C1.1 / E1 runtime; **superseded by `c1-official-adapter.yml` in C1.2**. Still functional for historical evidence smoke but no longer the mainline target. |
| `docker-compose.savant-smoke.yml` | **legacy-phase-poc** | Phase 1B minimal Savant smoke (single pyfunc) |

Every entry classed as `legacy-phase-poc` carries a `# LEGACY / PHASE
POC — do not use for mainline runtime` banner at line 1 of the file
itself (added in the same commit as this inventory). The banner is
purely documentary — `docker compose` ignores comment lines.

## 3. What "legacy-phase-poc" means

- These composes were the runtime entrypoint for a specific phase
  milestone in the past.
- They are **preserved for regression reference**: the matching smoke
  script under `scripts/smoke/check_phaseX*.sh` still expects this
  compose to exist.
- They are **not deleted** because the R1 consolidation plan
  (`docs/phase_r1_mainline_consolidation_plan.md`) explicitly defers
  module / compose removal to a later sub-phase.
- They are **not the mainline runtime**. New work must NOT target them.
- Specifically:
  - Do not add new services to these files.
  - Do not extend their feature surface.
  - Do not point new smoke scripts at them.
  - Do not start `docker-compose.phase3b.yml`'s continuous sinks at
    all (`phase3b-video-file-sink`, `phase3b-media-worker`,
    `phase3b-clip-worker`, `phase3b-replay-service` — banned by
    CLAUDE.md §9.5 and by `scripts/smoke/check_media_output_lockdown.sh`).

## 4. What "future-target" means

- `docker-compose.dev.yml` occupies a filename slot reserved by the
  R1 plan for the consolidated backend-only dev stack (redis +
  postgres + api + event-worker, no GPU).
- The current file content already implements that shape but predates
  R1; a future R1.x sub-phase will refine and reconcile it with the
  C1.2 service definitions.
- Not used by `c1-official-adapter.yml` today.
- Do NOT delete or rename it — the R1 plan needs this slot.

## 5. Why no `unknown` entries

Every compose file in `infra/` matches one of the three classes above.
This row of the user's classification template is intentionally empty.

## 6. Reference policy

| Rule | Status |
|---|---|
| Add a new compose for a new phase? | **No.** R1 §4 forbids new `docker-compose.phaseXX.yml` files. Extend the current-main or future-target slots via profiles / env. |
| Edit a legacy compose for a new feature? | **No.** Legacy composes are read-only regression scaffolds. |
| Delete a legacy compose? | **No.** Deferred to a future R1.x consolidation phase. |
| Rename a legacy compose? | **No.** The matching `scripts/smoke/check_phaseX*.sh` and historical phase docs hard-code these names. |
| Start the legacy `phase3b` continuous sinks? | **Banned.** See CLAUDE.md §9.5 + `scripts/smoke/check_media_output_lockdown.sh`. |

---

## 7. Related documents

| Document | Content |
|---|---|
| `docs/phase_c1_2_official_adapter_runtime.md` | C1.2 runtime / current-main compose details |
| `docs/phase_r1_mainline_consolidation_plan.md` | R1 plan that defines the future-target compose slots (`dev` / `gpu` / `evidence` / `replay`) |
| `docs/project_rebaseline_2026_05_25.md` | Authoritative roadmap; classifies historical phase composes |
| `CLAUDE.md` §9.5 | Hard ban on starting `phase3b` continuous sinks |
| `scripts/smoke/check_media_output_lockdown.sh` | Smoke that enforces the §9.5 ban |

---

*Written 2026-05-26. Inventory only, no service logic changes.*
