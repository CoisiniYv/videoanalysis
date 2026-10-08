# Project Knowledge Network

Date: 2026-06-20

This document is the current documentation map for the repo. It links the
active deployment docs, current specs, repair-goal notes, and archived phase
records into one readable network.

Use this file first when deciding what is implemented, what is only planned,
and which document is authoritative for a topic.

## Current Spine

```text
[[Project Current Progress Summary]]
  -> [[Current Mainline Status]]
  -> [[Midterm Deployment]]
  -> [[Compose Inventory]]
  -> [[Operator Algorithm Runtime Status]]
  -> [[Dual-path 30x2 T4 Production Optimization]]
  -> [[Clip-worker Evidence Realtime Alignment Fix]]
```

Active files:

- [[README]]: `README.md`
- [[Engineering Entry Rules]]: `CLAUDE.md`
- [[Project Current Progress Summary]]:
  `docs/project_current_progress_summary.md`
- [[Current Mainline Status]]: `docs/current_mainline_status.md`
- [[Midterm Deployment]]: `docs/midterm_deployment.md`
- [[Compose Inventory]]: `docs/compose_inventory.md`

## Runtime Topology

```text
[[RTSP Source]]
  -> [[Replay Storage]]
  -> [[Analysis Forwarder]]
  -> [[Savant Inference]]
  -> [[Redis Events and Annotations]]
  -> [[event-worker]]
  -> [[clip-worker Replay Job]]
  -> [[video-file-sink]]
  -> [[media-worker Evidence Bundle]]
  -> [[8090 Operator Portal]]
```

Key invariant: [[Replay Storage]] is the full-rate evidence authority.
[[Analysis Forwarder]] samples only the analysis branch. `raw_clip.mov` must
come from Replay/video-file-sink, not from sampled analysis output.

Implementation evidence:

- `infra/docker-compose.midterm.yml`
- `infra/env/midterm.env`
- `modules/savant_replay/config.midterm.json`
- `services/analysis-forwarder/`
- `services/api/app/services/runtime_apply.py`
- `services/api/app/services/runtime_overview.py`

## Phase Network

### Replay Evidence Foundation

```text
[[C1/C2 Replay Evidence Work]]
  -> [[Replay-first Evidence]]
  -> [[Duration Guard]]
  -> [[Runtime Epoch and Stream Session Isolation]]
  -> [[Post-Savant Proof Windows]]
```

Authoritative docs:

- `specs/14_replay_evidence_duration_guard_fix.md`
- `docs/midterm_replay_intrusion_clip_duration_diagnosis.md`
- `docs/midterm_replay_routing_id_recovery.md`
- `docs/midterm_post_savant_evidence_proof_windows_2026-06-15.md`
- `specs/17_clip_worker_evidence_realtime_alignment_fix.md`

Status:

- Implemented: fail-closed duration guard, sink-window guard, Replay routing-id
  recovery, sink alias preservation, runtime epoch/stream-session protection in
  the current evidence path.
- Partially implemented: evidence status visibility and post-Savant proof-window
  handling. Runtime validation exists for the "event exists but proof fails"
  class.
- Open: frontend frame-bound overlay hardening and upstream no-event diagnosis
  for cameras where no behavior event is emitted.

### Runtime Stability And Backpressure

```text
[[Midterm Runtime Stability]]
  -> [[Replay out_stream Backpressure]]
  -> [[Phase 0 Reversible Experiments]]
  -> [[Phase 0.5 Forwarder Spike]]
  -> [[Phase 1 Analysis Forwarder]]
  -> [[Phase 2 Single T4 Gate]]
  -> [[Phase 3 Dual T4 Sharding]]
```

Authoritative docs:

- `specs/16_dual_path_30x2_t4_production_optimization.md`
- `specs/15_savant_performance_observability.md`
- `docs/repair_goal/midterm_phase0_observability_baseline_2026-06-14.md`
- `docs/repair_goal/midterm_phase0a_max_fps_control_experiment_2026-06-15.md`
- `docs/repair_goal/midterm_phase0b_replay_short_retry_experiment_2026-06-15.md`
- `docs/repair_goal/midterm_phase0c_sync_output_experiment_2026-06-15.md`
- `docs/repair_goal/midterm_phase0d_source_adapter_tolerance_verification_2026-06-15.md`
- `docs/repair_goal/midterm_phase05_forwarder_spike_2026-06-15.md`
- `docs/repair_goal/midterm_phase1_analysis_forwarder_2026-06-15.md`
- `docs/repair_goal/midterm_phase2_readiness_gate_2026-06-15.md`
- `docs/repair_goal/midterm_phase2_pressure_runner_2026-06-15.md`
- `docs/repair_goal/midterm_phase2_operating_point_appendix_2026-06-15.md`
- `docs/midterm_replay_shard_change_record_2026-06-18.md`
- `specs/20_dual_4090_as_t4_two_source_validation.md`
- `specs/21_replay_evidence_io_optimization_60_stream_production.md`

Status:

| Phase | Status | Evidence |
| --- | --- | --- |
| Phase 0 observability | Done | 8090 restart count/rate visibility, `PASS_PHASE0_OBSERVABILITY`. |
| Phase 0A | Kept | `MAX_FPS_CONTROL=false`, `INGRESS_FPS_GATE_ENABLED=true`. |
| Phase 0B | Kept | Replay analysis `out_stream` short retry. |
| Phase 0C | Kept | Fixed and dynamic live RTSP sources use `SYNC_OUTPUT=false`. |
| Phase 0D | Verified no-op | RTSP adapter entrypoint does not honor send-timeout/retry envs. |
| Phase 0.5 | Done | Savant image `savant_rs` passthrough and ZMQ ingress gate behavior proved. |
| Phase 1 | Done for current runtime | `analysis-forwarder` inserted and `PASS_PHASE1_FORWARDER` documented. |
| Phase 2 | Gated | Readiness/pressure/update scripts exist; real T4 30-stream run not done. |
| Phase 3A | Done for routing | 60 simulated source ids route 30/30 across `replay-a`/`replay-b`; source-to-Replay job routing and DB shard identity are implemented. |
| Full Phase 3 | Gated | Real 60-stream ingest, RocksDB write latency, dual-GPU inference, and evidence burst capacity still need staged pressure tests. |
| Phase 4 | Not implemented | Needs production drills, storage sizing, dashboard thresholds, runbook. |

### Operator And Algorithm Controls

```text
[[8090 Operator Portal]]
  -> [[Camera Runtime Apply]]
  -> [[Algorithm Rule Config]]
  -> [[Support Matrix Gap]]
  -> [[Evidence Policy]]
```

Authoritative docs:

- `docs/midterm_operator_portal_runtime_design.md`
- `docs/midterm_operator_algorithm_controls_runtime_status.md`
- `docs/midterm_8090_port_integration.md`
- `docs/operator_camera_and_face_registration_plan.md`

Status:

- Implemented end to end: camera source fields, zone persistence/export,
  `behavior.intrusion` rule/evidence chain, real face registration path.
- Partially implemented: `behavior.crowd_gathering`, `behavior.fall`,
  `behavior.chasing` detection modules exist but do not have default evidence
  parity with intrusion.
- Config-visible but not production-complete:
  `behavior.loitering`, `behavior.running`, `behavior.wall_climb_suspicious`,
  `face.observation`, `face.watchlist`, `face.live_search`.
- Open: stable API/UI support matrix with `configurable`,
  `runtime_detecting`, `evidence_enabled`, and `per_camera_gate`.

### Face Intelligence

```text
[[Face Observation]]
  -> [[AdaFace Embedding]]
  -> [[Gallery Match]]
  -> [[Watchlist Hit]]
  -> [[Evidence Bundle]]
```

Authoritative docs:

- `docs/midterm_operator_algorithm_controls_runtime_status.md`
- `docs/operator_camera_and_face_registration_plan.md`
- archived F-phase records under `docs/archive/phase-only/20260610/`

Status:

- Implemented: face observation export, gallery vector search, real registration
  foundations, watchlist-hit event path in the midterm shape.
- Partial: watchlist evidence has historical ready samples, but per-camera
  watchlist controls are not the runtime gate.
- Not implemented: live search as a true production runtime/evidence feature.

## Implementation Matrix

| Capability | State | Notes |
| --- | --- | --- |
| Midterm deployment entrypoint | Implemented | Use `infra/docker-compose.midterm.yml` only. |
| 8090 customer portal | Implemented | Proxies internal API and serves evidence. |
| Full-rate Replay evidence storage | Implemented | Replay remains source of truth. |
| Analysis sampling/backpressure isolation | Implemented for current runtime | `analysis-forwarder` is active. |
| `behavior.intrusion` evidence | Implemented | Current production baseline. |
| Duration guard / fail-closed clips | Implemented | See `specs/14`. |
| Post-Savant proof-window fix | Partial | Event-exists/fails-proof class fixed; overlay and no-event cases remain. |
| Savant performance metrics | Implemented enough for current smokes | Production capacity still needs T4 run. |
| Algorithm support matrix | Missing | Needed before customer-facing algorithm claims. |
| Behavior evidence parity | Partial | `crowd/fall/chasing` need recording/evidence extension. |
| Missing behavior rules | Missing | `loitering/running/wall_climb_suspicious`. |
| Face per-camera runtime gate | Missing | Current control is env/worker oriented. |
| Phase 2 single-T4 30 streams | Gated | Scripts exist; acceptance not run. |
| Phase 3 dual-T4 60 streams | Partial | Phase 3A shard routing is implemented; real 60-stream throughput acceptance is not. |

## Reading Rules

1. Prefer active docs in `docs/`, current specs in `specs/`, and current code
   over archived phase files.
2. Treat `docs/archive/phase-only/20260610/` and `specs/archive/phase-only/`
   as history, not current deployment instructions.
3. Treat `PASS_*` tokens as valid only when the corresponding current doc or
   script records the exact scope. A Phase 2 gate pass is not a Phase 2 capacity
   pass.
4. For runtime claims, verify against `infra/docker-compose.midterm.yml`,
   `infra/env/midterm.env`, `modules/savant_replay/config.midterm.json`, and
   the relevant service code before updating docs.
5. For evidence claims, keep the distinction between "no upstream event" and
   "event exists but evidence failed" explicit.
