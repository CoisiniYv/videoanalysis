# Current Mainline Status

## 2026-06-15 Midterm Project Version

The current deployable runtime in this checkout is the midterm project version.
It intentionally avoids historical codename files in the active deployment surface.

- Branch: current working-tree branch.
- Compose file: `infra/docker-compose.midterm.yml`.
- Env file: `infra/env/midterm.env`.
- Compose project: `video-analytics-midterm`.
- Containers: `video-analytics-midterm-*`.
- Source id: `primary_rtsp`.
- Operator portal / evidence viewer: host port `8090`.
- Internal API service: compose network port `8000`; not published to host and
  reached through the 8090 portal proxy.
- Replay API: host port `8098`.
- Analysis-forwarder metrics: host port `18081`.
- Worker database default: `host.docker.internal:5432`.
- Internal API runtime: `services/api/Dockerfile.face-runtime`, inheriting from
  `video-analytics-midterm-face-worker:latest` to reuse the already-installed
  ONNX Runtime/OpenCV/Numpy face-registration layer.
- Analysis path isolation: `analysis-forwarder` is inserted between
  Replay `out_stream` and Savant. Replay remains the full-rate evidence storage
  authority; the forwarder samples/drops only the analysis branch and exposes
  `va_forwarder_*` metrics.
- Face gallery search: `face-worker` currently uses Qdrant authoritative
  registered-gallery lookup with PostgreSQL exact rerank and pgvector rollback.
  PostgreSQL remains the source of truth for `persons` and
  `person_gallery_embeddings`; Qdrant is a rebuildable derived index.
- Savant v0.6.0 PTS-reset crash hardening: `savant-security` applies the
  md5-pinned overlay in `modules/savant_security/savant_patches/` before module
  startup, and the API service behind the 8090 management plane runs the
  STOPPED/stall supervisor for Savant plus primary and dynamic source adapters.
  The previous `docker:27-cli` watchdog profile has been removed.
- Operator portal design: `docs/midterm_operator_portal_runtime_design.md`.
- 8090 evidence list/detail displays alarm machine time from bundle metadata;
  new bundles write `event.alarm_machine_time` from `events.created_at`.
- 8090 evidence playback fail-closed behavior is backed by the media-worker
  post-Savant crop fix: raw clip crop now selects one contiguous sink metadata
  segment, prefers matching `frame_uuid/uuid` anchors when present, normalizes
  ffmpeg trim with `setpts`, and filters frame annotation cache by runtime
  epoch. Runtime apply/restart also resets the frame annotation Redis cache;
  see `docs/midterm_8090_port_integration.md`.
- Replay `offset.seconds` remains the positive rewind value from the anchor
  keyframe to `requested_start_pts`; do not flip it negative. Current playback
  validation used ready bundle `f208b550-6b34-44ff-a847-3219041349ea` and latest
  8090 listing `3acfac74-6c54-4045-820b-b659df3894da`.
- Operator algorithm-control runtime status:
  `docs/midterm_operator_algorithm_controls_runtime_status.md`.
- 2026-06-11 progress snapshot and next-plan baseline:
  `docs/midterm_progress_snapshot_2026-06-11.md`.
- 2026-06-15 documentation network and phase map:
  `docs/project_knowledge_network.md`.
- Dual-path / T4 production capacity plan and phase status:
  `specs/16_dual_path_30x2_t4_production_optimization.md`.
- Dual-4090 as T4 two-source validation plan:
  `specs/20_dual_4090_as_t4_two_source_validation.md`.
- Replay evidence IO optimization plan:
  `specs/21_replay_evidence_io_optimization_60_stream_production.md`.
- Clip-worker/evidence real-time alignment plan:
  `specs/17_clip_worker_evidence_realtime_alignment_fix.md`.
- Post-Savant evidence proof window fix record:
  `docs/midterm_post_savant_evidence_proof_windows_2026-06-15.md`.
- Current Replay intrusion clip-duration diagnosis:
  `docs/midterm_replay_intrusion_clip_duration_diagnosis.md`.
- Replay routing-id mismatch recovery:
  `docs/midterm_replay_routing_id_recovery.md`.
- Current `/data/video-analytics` directory inventory and cleanup record:
  `docs/midterm_data_directory_inventory.md`.

## Current Runtime Chain

```text
RTSP -> Replay storage -> analysis-forwarder -> Savant inference
  -> Redis events/annotations
  -> event-worker / face-worker -> PostgreSQL / Qdrant
  -> clip-worker -> Replay job -> video-file-sink
  -> media-worker evidence sidecar -> 8090 operator portal
```

Evidence remains full-rate because `raw_clip.mov` is generated from
Replay/video-file-sink jobs, not from the sampled analysis branch.

## Current Calibration

The midterm entrypoint keeps Savant's project ingress FPS gate enabled, disables
nvstreammux `MAX_FPS_CONTROL` after the Phase 0A experiment, and limits the
analysis branch through `analysis-forwarder`:

- `MAX_FPS_CONTROL=false`
- `INGRESS_FPS_GATE_ENABLED=true`
- `ANALYSIS_FPS=8/1`
- `MAX_FPS=8/1`
- `MIN_FPS=2/1`
- `POSE_INFER_INTERVAL=1`
- `POSE_CONFIDENCE_THRESHOLD=0.50`
- `POSE_KEYPOINT_THRESHOLD=0.35`
- `POSE_SELECTOR_CONFIDENCE_THRESHOLD=0.50`
- `POSE_SELECTOR_NMS_IOU_THRESHOLD=0.50`
- `POSE_MIN_WIDTH=60`
- `POSE_MIN_HEIGHT=100`
- `FACE_CONFIDENCE_THRESHOLD=0.50`
- `WATCHLIST_THRESHOLD=0.60`

## Phase Status

| Area | Current status |
| --- | --- |
| Midterm deployment surface | Active. Use only neutral `midterm` entrypoints. |
| Replay/Savant backpressure Phase 0 | Complete for observability and reversible experiments 0A-0D. Kept `MAX_FPS_CONTROL=false`, short Replay retry, and `SYNC_OUTPUT=false`; did not add ineffective RTSP adapter tolerance envs. |
| Phase 0.5 forwarder spike | Complete. Savant image can use `savant_rs`; current ingress gate drops before decode on the ZMQ path. |
| Phase 1 analysis-forwarder | Complete for the current two-source runtime. `PASS_PHASE1_FORWARDER` is documented. |
| Phase 2 single-T4 30 streams | Gated. Readiness and pressure-run scripts exist, but the current development host is not a T4 30-stream environment. |
| Phase 3 dual-T4 60 streams | Partial. Phase 3A shard routing and dual-4090 validation scaffolding are implemented; real 60-stream throughput, RocksDB write latency, and evidence burst capacity remain gated. |
| Face gallery search | Qdrant authoritative cutover complete for current scale gate. 60-route 8 FPS pressure fallback=0; 20,000-vector gRPC benchmark all-search p95/p99=4.037ms/6.427ms. Remaining risk is the synchronous face-worker loop, not registered-gallery vector lookup. |
| Phase 4 production hardening | Not implemented. Requires drills, dashboard thresholds, storage sizing, and runbook. |
| Evidence proof window fix | Partially implemented and runtime-validated for "event exists but proof window fails"; frontend frame-bound overlay hardening remains open. |
| Algorithm support matrix | Not implemented as a stable API/UI matrix. Current docs still define the boundary. |

## Archive Rule

Historical codename files remain in archive directories for traceability.
They are not current deployment entrypoints and should not be copied to a
project machine unless explicitly doing historical regression.
