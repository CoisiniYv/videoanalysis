# 16_dual_path_30x2_t4_production_optimization.md

## 1. Goal

Bring the midterm runtime to a production-grade capacity target:

- **30 RTSP streams per Savant inference module.**
- **2× NVIDIA T4 → 2 Savant modules → 60 streams total**, with full-rate
  evidence preserved for all 60.
- Eliminate the source-adapter restart storm (live 2026-06-14: source-adapter
  `RestartCount=845`, dynamic source `RestartCount=1137`).
- Performance-first: minimize wasted decode/encode/transport so a single T4
  carries 30 streams of analysis plus its share of evidence storage.

The architecture splits **evidence (full-rate)** from **analysis (sampled)**
*after a single ingestion*, so Savant slowness can never back-pressure the RTSP
ingest / recording chain, and frame-lineage evidence binding is preserved.

This spec is executable by Codex **phase-by-phase**. Do not run it as one
monolithic goal: each phase has explicit file targets, tests, `PASS_*`
acceptance tokens, and rollback. Phases are independently shippable and must
land as clean, atomic commits (see §9).

> **Revision 2 (2026-06-14):** hardened after an implementation-risk review.
> Phase 0 is now **reversible experiments**, not declared fixes; a new **Phase 0.5
> forwarder spike** is a hard gate for Phase 1; `MAX_FPS_CONTROL` is documented as
> a nvstreammux throttle (verified, not a socket-intake throttle); `SYNC_OUTPUT`
> and source-adapter env-injection are explicit verification items; the
> savant_rs/forwarder-image dependency is called out (no checked-in standalone
> wheel path in this repo).

Official Savant references (verify against the running
`ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1` image and
`savant-replay-x86:v0.6.0`):

- Streaming model: `https://savant-ai.io/docs/latest/savant_101/00_streaming_model.html`
- Adapters: `https://savant-ai.io/docs/latest/savant_101/10_adapters.html`
- Frame filtering: `https://savant-ai.io/docs/latest/advanced_topics/3_frame_filtering.html`
- Skipping frames: `https://savant-ai.io/docs/v0.6.0/advanced_topics/3_skipping_frames.html`
- Batching: `https://savant-ai.io/docs/v0.6.0/advanced_topics/0_batching.html`
- Message buffering (Buffer NG): `https://savant-ai.io/docs/latest/advanced_topics/19_message_buffering.html`
- Stream limit: `https://savant-ai.io/docs/latest/advanced_topics/0_pipeline_stream_limit.html`
- Prometheus metrics: `https://savant-ai.io/docs/latest/advanced_topics/9_prometheus_metrics.html`

Companion docs: `specs/15_savant_performance_observability.md`,
`docs/runtime_stability_fix/midterm_multi_source_runtime_stability_plan.md`,
`docs/repair_goal/midterm_alarm_frequency_backpressure_findings_2026-06-13.md`.

## 2. Confirmed Failure Chain and Open Boundary (live evidence, 2026-06-14 ~02:55Z)

Read-only diagnosis on the running stack:

| Signal | Observation | Meaning |
|---|---|---|
| source-adapter / dynamic source restart | 845 / 1137, `exit=0`, `running` | Restart storm, ongoing |
| savant container | `restart=0`, `status=running`, `health=healthy` (Up 5h) | Savant did **not** crash |
| `/opt/savant/status.txt` | `running` | Module not STOPPED |
| `va_savant_last_frame_age_seconds` | primary 0.13s / dynamic 0.0s | Savant is actively consuming both sources |
| savant log reset/patch/STOPP markers | none | source-reset race (Phase 5 patch) **not firing** |
| replay-service log | continuous `[11] Resource temporarily unavailable` on `out_stream`, retries 10→0 over ~50s, then `Send timeout, retrying sending <n>` | Replay→Savant hop HWM full; Replay reliably **re-queues (does not drop)** |
| source-adapter log | `WriterResultSendTimeout` after 3×~5s retries → `Internal data stream error (-5)` → pipeline restart | Adapter exits on back-pressure |

**Confirmed failure chain:** during the observed window, the Replay→Savant
`out_stream` stopped draining fast enough, filled its HWM, entered a ~50s
send-retry loop, and during that window did **not** drain Replay `in_stream`;
that backed up the source-adapters until their ~15s send tolerance was exceeded
and their GStreamer pipelines errored out (`exit 0`) → Docker restarted them.

**Open root-cause boundary:** the same snapshot did **not** show Savant STOPPED,
source-reset patch markers, or container crashes, so the active symptom was the
transport/mux drain path. It does **not** by itself prove a steady-state GPU
inference capacity deficit. Phase 0 must distinguish steady-state drain limits
from transient stalls, nvstreammux/gate behavior, source reconnects, and PTS
reset effects before keeping any runtime lever.

Two facts that shape the fix:

- **`PtsFpsGate` does not change what Replay records**
  (`specs`/`phase_c2_15`: gate admits frames *into the inference graph* only).
  The evidence stream is already full-rate and independent of the analysis
  throttle. The project is already dual-path at the *data* level; the bug is
  *topological* — both paths share one physical ZMQ hop.
- **Savant's `5558` output (`pub+bind`) has no consumer** in the midterm
  topology (evidence comes from Replay jobs, not Savant output). Savant is
  encoding output frames (`OUTPUT_FRAME` h264/nvenc) that nobody reads — wasted
  NVENC and pipeline cost. (Verify before changing.)
- **Naive H264 frame dropping corrupts video** (`c1e_2`: losing one P-frame
  breaks the GOP until the next keyframe). Any sampling/drop must be
  keyframe-anchored, never random ZMQ PUB/SUB drop.
- **`MAX_FPS_CONTROL` is a nvstreammux throttle, not a socket-intake throttle**
  (`modules/savant_security/savant_patches/v0.6.0/pipeline.py:1109` →
  `max-fps-control` + `overall-max-fps-n/d`). Disabling it is an experiment
  (Phase 0A) that *may* relieve source-bin→muxer backpressure or *may* just push
  full-rate frames deeper — must be measured, not assumed. The current
  `module.yml` also uses the same parameter to enable `PtsFpsGate`; Phase 0A
  must decouple those controls before changing the live env, or it will disable
  the analysis FPS gate as a side effect.

## 3. Hard Constraints (must not break)

1. **Single ingestion.** One RTSP pull per camera
   (`CLAUDE.md`; `EVIDENCE_SECOND_RTSP_PULL=false`). The dual path must **fork
   after a single ingestion**, never pull RTSP twice. Two pulls = two UUID/PTS
   domains = broken evidence binding.
2. **Frame lineage.** Replay storage and Savant analysis must share the same
   savant-rs `VideoFrame` UUID/PTS domain. `frame_uuid` is the evidence anchor
   (`CLAUDE.md` Replay alignment rules). The forwarder must pass frames through
   verbatim — no UUID regeneration, no re-encode, no PTS rewrite.
3. **Full-rate evidence.** `raw_clip.mov` is copied from Replay/video-file-sink,
   never synthesized from sampled analysis. Sampling reduces *analysis* FPS only.
4. **Deployment surface rules** (`CLAUDE.md` §部署面规则): only `midterm` files;
   neutral naming; no phase-code compose in root `infra/`; extend
   `docker-compose.midterm.yml` / `midterm.env`. `EVIDENCE_VERSION=midterm`,
   `EVIDENCE_SCHEMA_VERSION=2.0-midterm`, legacy metadata off.
5. **No blocking work in Savant `process_frame`** (`CLAUDE.md` 开发规则).
6. **Clean commits** (see §9). Update/extend `harness/tests/` before or with
   each behavioral change; `docker compose -f infra/docker-compose.midterm.yml
   config` and `git diff --check` must stay green.
7. **`modules/savant_replay/config.midterm.json` is shared** with the
   Replay/evidence workstream — coordinate edits; keep them minimal and reviewed.

## 4. Target Architecture

Single ingestion, fork into evidence + analysis, one shard per T4.

```text
Per camera (single RTSP pull):
  source-adapter (full H264)
    -> Replay in_stream  ──────────────► RocksDB ring (full-rate evidence)
                                            └► clip-worker -> Replay job
                                                 -> video-file-sink -> raw_clip.mov
    Replay out_stream
      -> analysis-forwarder              (NEW: keyframe-anchored sample to
           [per-source sample to N fps;   ANALYSIS_FPS + bounded queue, drop
            bounded queue; drop-on-full]  analysis frames when Savant is slow;
      -> Savant module                    never block the read side)
           NVDEC + nvstreammux batching + pose/face/AdaFace + Redis export
```

Two-T4 scale-out — **two self-contained shards**, sharded by `source_id`:

| Shard | GPU | Streams | Replay | Forwarder | Savant |
|---|---|---|---|---|---|
| A | T4 #0 (`device_ids: ['0']`) | 30 | `replay-a` | `analysis-forwarder-a` | `savant-a` |
| B | T4 #1 (`device_ids: ['1']`) | 30 | `replay-b` | `analysis-forwarder-b` | `savant-b` |

Shared (CPU/IO): redis, postgres, event-worker, face-worker, clip-worker,
media-worker, video-file-sink, api, evidence-viewer. Sharding keeps GPU and
RocksDB write load isolated and localizes failure (one T4 down ≠ all 60 down).

**Why fork at Replay (not a second adapter):** the source-adapter is a
single-output prebuilt binary; Replay is the storage authority that already
holds the lineage-bearing full stream. Reading Replay's `out_stream` into the
forwarder keeps one ingestion, one UUID domain, full evidence, and lets analysis
be lossy without touching evidence.

**Why a custom forwarder (not the official Buffer adapter alone):** Buffer NG
buffers + drops on overflow but does **not** reduce FPS, so Savant would still
decode the full rate in steady state (750fps/shard) — over T4 NVDEC budget. We
need proven *FPS reduction before Savant* plus drop-on-full. If Phase 0.5 proves
compressed-frame sampling is decodable and annotation-equivalent, the forwarder
is `PtsFpsGate` moved upstream of Savant + a non-blocking output queue. If not,
the forwarder is still useful as non-blocking isolation, but decode reduction
requires a decode→resample/re-encode or raw-frame design.

## 5. Performance & Capacity Model (T4)

All numbers below are **design targets to be measured on the real T4 production
hardware**, not assumed. T4: Turing, 16 GB, 1× NVENC, 2× NVDEC, 65 TFLOPS FP16.

### 5.1 NVDEC (decode) budget — the reason sampling is mandatory

| Path | Decode load per shard (30 streams) | Notes |
|---|---|---|
| Evidence (Replay/RocksDB) | **0** in steady state | stores encoded H264; decode only on clip generation |
| Analysis WITHOUT forwarder | 30 × 25 = **750 dec fps** | exceeds comfortable single-T4 NVDEC → fragile/backpressure |
| Analysis WITH forwarder @ 8fps | 30 × 8 = **240 dec fps** | comfortably within T4 NVDEC |
| Analysis WITH forwarder @ 5fps | 30 × 5 = **150 dec fps** | fallback if compute-bound |

**Task P5.1:** instrument whether Savant's `ingress_frame_filter` drops
*before* or *after* NVDEC (compare NVDEC frame count vs `pts_fps_gate` accepted
count). This determines whether the forwarder's decode-reduction benefit is
already partially realized. Design holds either way (forwarder still provides
back-pressure isolation), but record the answer.

### 5.2 GPU compute (inference) budget

Per shard at `ANALYSIS_FPS=F`, 30 streams:

- pose: `30 × F` inferences/s of YOLO26-pose (640² fp16).
- face: `30 × F / (FACE_INFER_INTERVAL+1)` of YOLOv8-face.
- AdaFace: bounded by detected faces × throttle.

Required pose throughput `≥ 30 × F`. Measured pose engine throughput at batch
`B` = `B / latency_B`. **Derive `B` and `F` from measurement** (Task P5.2):

```text
1. Build TensorRT engines for candidate batch sizes (b1, b4, b8, b16) fp16.
2. Measure per-batch latency on the T4 for pose/face/adaface.
3. Pick (B, F) so 30×F ≤ measured pose throughput with ≥30% headroom.
4. If 8fps×30 does not fit: lower F to 5–6, and/or raise POSE_INFER_INTERVAL,
   and/or evaluate INT8 for pose. Do NOT silently exceed budget.
```

Set `max_parallel_streams ≥ 30 + headroom`; `batched_push_timeout` ≈ one frame
interval at `F` (e.g. F=8 → 125ms cap; keep RTSP-realtime ~35–40ms only if it
does not starve batches — measure). `batch_size` (muxer) tuned with detector
`model.batch_size`; do not raise blindly (`docs/.../midterm_worker_savant_batching_findings.md`).

### 5.3 NVENC — remove wasted output encoding

`5558` is unconsumed (§2). **Task P5.3:** confirm no consumer, then make Savant
output metadata-only / disable output frame encoding so NVENC is free for any
future use and the output pipeline stage cost is removed. Keep evidence path
(Replay remux) unaffected.

### 5.4 Replay storage (per shard, 30 streams)

```text
working_set ≈ stream_count × bitrate × data_expiration_ttl
e.g. 30 × 6 Mbps × 300 s ≈ 6.75 GB  (+ RocksDB write amplification)
sustained write ≈ 30 × 6 Mbps ≈ 180 Mbps ≈ 22 MB/s per shard
```

**Task P5.4:** put `/opt/rocksdb` on NVMe; size `data_expiration_ttl` /
`compaction_period` from real bitrate × 30 × retained pre-roll; bound Replay job
concurrency. (Reminder already in `c1e_2:165-174`.)

### 5.5 Memory (16 GB / T4)

Engines + 30-stream NVDEC surfaces + nvstreammux buffers. **Task P5.5:** validate
peak < ~12 GB to leave headroom; cap `BUFFER_LEN` / queue sizes accordingly.

## 6. Phased Execution Plan

### Phase 0 — Reversible experiments + observability (current 2-stream topology)

**Posture (revised per 2026-06-14 review):** Phase 0 ships ONLY the 8090
observability hardening as a permanent change. Every lever below is a
**reversible, metrics-collecting experiment**, not a declared fix, because each
one's mechanism is not yet proven on the deployed 0.6.0 image.

**Ship (permanent): 8090 restart-rate observability** — see §7. Acceptance
`PASS_PHASE0_OBSERVABILITY`: dynamic-source `restart_count` collected and shown,
short-window restart rate computed, health goes red under a restart storm,
`test_operator_runtime_overview_static.py` updated.

For every experiment, capture a fixed metric set before/after over a short window
and keep it reversible (env/config revert + recreate; no data ops):

```text
per experiment (60-120s windows, before vs after):
  replay : "Resource temporarily unavailable" + "Send timeout" count / min
  savant : va_savant_frames_seen_total delta, last_frame_age, effective_fps
  adapter: RestartCount delta (fixed AND dynamic)
  gpu    : nvidia-smi decode/util/mem (if available)
```

- **0A — nvstreammux `MAX_FPS_CONTROL=false` experiment.** Mechanism confirmed:
  the env maps to nvstreammux `max-fps-control` (see §2), a muxer throttle,
  **not** a socket-intake throttle. Before any live flip, decouple the analysis
  gate enable from `parameters.max_fps_control` in `module.yml`, for example via
  `INGRESS_FPS_GATE_ENABLED`:
  `enabled: ${oc.decode:${oc.env:INGRESS_FPS_GATE_ENABLED, true}}`. Keep
  `PtsFpsGate` enabled during the experiment so inference is not flooded. **Keep
  nvstreammux `MAX_FPS_CONTROL=false` only if** the Replay `out_stream`
  EAGAIN/`Send timeout` loop stops AND adapter `RestartCount` goes flat AND
  GPU/decode stays in budget; otherwise revert the muxer setting. Do not use
  `MAX_FPS_CONTROL=false` as shorthand for disabling all FPS gating.
- **0B — Replay `out_stream` short-retry experiment.** `config.midterm.json`
  (shared file — coordinate): try `send_timeout: 1s`, `send_retries: 2`. Replay
  re-queues rather than drops, so this only shortens the in_stream-block window
  (mitigation, not root fix). Keep only if restart delta improves with no new
  evidence-clip failures.
- **0C — `SYNC_OUTPUT=false` experiment.** Both fixed and dynamic sources set
  `SYNC_OUTPUT=true`; for live RTSP (not file replay) sync-to-timestamp can
  accumulate latency / stall under backpressure. Test `false` on a short window
  and **keep the fixed-source compose and dynamic-source creation logic
  consistent** (`services/api/app/services/runtime_apply.py` /
  `scripts/runtime/camera_source_controller.py`). Keep only if metrics improve.
- **0D — source-adapter tolerance: VERIFY then apply.** Do NOT blindly add env.
  First determine which ZMQ-sink properties the prebuilt
  `savant-adapters-gstreamer:0.6.0` `rtsp.sh` actually honors from env (inspect the
  image entrypoint / `zeromq_sink` element). Apply send timeout/retry overrides
  only if proven to take effect; otherwise mark "not env-configurable — needs
  forwarder/Buffer" and stop.

`PASS_PHASE0_EXPERIMENTS`: each lever has a recorded before/after metric set and a
documented keep/revert decision; deployed config reflects only kept levers; tree
clean; `test_midterm_deployment_contract.py` updated for whatever is kept.

### Phase 0.5 — Forwarder technical spike (HARD GATE for Phase 1)

The forwarder rests on two unproven assumptions. **Phase 1 is blocked** until both
are answered on the deployed `savant-deepstream:0.6.0-7.1` image.

- **Spike S1 — H264 sampled-stream decode stability (highest risk).** Sampling
  drops encoded frames; keeping keyframes does NOT guarantee surviving P-frames
  decode (a dropped reference corrupts the GOP — `c1e_2`). Upstream wires the
  ingress filter through a `savant_rs_add_frames` element
  (`/tmp/Savant-upstream/savant/deepstream/pipeline.py:608-631`,
  `ingress-module/class/kwargs`); whether the deployed 0.6.0 zmq path filters
  before or after NVDEC is unconfirmed. Spike must measure, on the real image:
  1. does the current in-Savant `PtsFpsGate` drop before or after NVDEC (compare
     NVDEC output frame count vs gate `accepted` count);
  2. feed a pre-sampled stream (sampled to N fps, keyframe-anchored) into Savant
     and compare decode-error rate + annotation/event output vs the current
     in-Savant-gate baseline. **Forwarder is viable only if quality is equivalent
     to today's baseline.**
  3. If not equivalent: forwarder must decode→resample (re-encode H264 or send raw
     within budget), OR sampling stays inside Savant and the forwarder provides
     only drop-on-full back-pressure isolation (no NVDEC saving). Record the choice.
- **Spike S2 — savant_rs forwarder feasibility + image.** This checkout has no
  checked-in `local_wheels/savant_rs` or other standalone savant_rs wheel path;
  normal services run plain `python:3.12-slim` images without savant_rs, while
  current savant_rs imports live in Savant-side code/patches. Spike must prove
  the exact forwarder image path (Savant base image, or a pinned compatible
  savant_rs wheel) can read the Replay `out_stream`, write the Savant `5557`
  stream, and pass `VideoFrame` through preserving UUID/PTS/keyframe/content
  byte-for-byte — with a unit test and one local smoke.

`PASS_PHASE05_SPIKE`: S1 decode/annotation equivalence answered (with the chosen
sampling strategy) and S2 forwarder image + passthrough proven. Phase 1 proceeds
only on PASS.

### Phase 1 — `analysis-forwarder` service (decouple the hop)

**Prerequisite: `PASS_PHASE05_SPIKE`.** Implement the sampling strategy S1 proved
viable; build the service on the image S2 proved (Savant base image or a pinned
savant_rs wheel — **not** plain `python:3.12-slim`).

**Deliverable:** `services/analysis-forwarder/` inserted between Replay
`out_stream` and Savant, using the proven savant_rs ZMQ read/write +
`VideoFrame` passthrough.

- Extract `PtsFpsGate` sampling into a reusable sampler
  (`services/analysis-forwarder/app/sampler.py`, or shared `libs/`), reusing the
  PTS-domain, keyframe-always-pass logic from
  `modules/savant_security/custom/filters/pts_fps_gate.py`.
- Read side: bind `router+bind:tcp://0.0.0.0:5557`; Replay `out_stream` →
  `dealer+connect:tcp://analysis-forwarder:5557`.
- Write side: `dealer+connect:tcp://savant-security:5557`; Savant keeps
  `router+bind:5557`.
- **Bounded queue + drop-on-full:** never block the read side (blocking would
  back-pressure Replay). When the Savant write would block (HWM/timeout), drop an
  analysis frame, preferring to drop non-keyframes; account drops.
- **Lineage passthrough:** forward `VideoFrame` verbatim (UUID, PTS, time_base,
  keyframe, content, attributes). No re-encode, no UUID/PTS mutation.
- Keep Savant's internal `PtsFpsGate` independently enabled as a cheap secondary
  safety; the forwarder is the primary rate control. If Phase 0A kept
  nvstreammux `MAX_FPS_CONTROL=false`, preserve that muxer setting, but never use
  it to disable the internal gate.
- Metrics: `va_forwarder_frames_seen/forwarded/dropped_total{source_id}`,
  `va_forwarder_queue_depth`, `va_forwarder_savant_send_failures_total`.

Compose: add `analysis-forwarder` service; repoint Replay `out_stream`; add to
`runtime_apply` ordering (start after Replay, before/with Savant). Update
`runtime_overview` to inspect it.

**Validation / acceptance — `PASS_PHASE1_FORWARDER`:**

- Fault-inject Savant slowness (e.g., pause savant container briefly): forwarder
  `dropped_total` rises, Replay `out_stream` stays drained, **source-adapter
  `RestartCount` does not increase**, evidence stays full-rate.
- Lineage test: a watchlist/intrusion event's `frame_uuid` still resolves to a
  Replay clip (existing evidence harness passes).
- New `harness/tests/test_analysis_forwarder_*.py`: sampler admits ~N fps,
  always passes keyframes, drops under simulated back-pressure, passes frames
  through byte-identical.
- `docker compose ... config`; `git diff --check`.

**Rollback:** repoint Replay `out_stream` back to `savant-security:5557`, remove
forwarder from compose. Single-hop Phase-0 behavior restored.

### Phase 2 — Single-T4 30-stream optimization

Execute the §5 measurement tasks and lock the operating point on one T4 / one
shard.

- **Entry gate:** run
  `scripts/runtime/check_phase2_single_t4_readiness.py --runtime-overview-url http://127.0.0.1:8090/api/v1/runtime/overview`.
  It must pass with `PASS_PHASE2_SINGLE_T4_READY` on the target machine before
  starting the 30-minute pressure run. This is not the Phase 2 acceptance token;
  it only proves the machine/topology/input preconditions are present.
- **Acceptance runner:** after the entry gate passes, run
  `scripts/runtime/check_phase2_single_t4_pressure.py --report-path /data/video-analytics/artifacts/phase2_single_t4_pressure.json`.
  It performs the default 30-minute sampling window and emits
  `PASS_PHASE2_SINGLE_T4_30` only if FPS, restart, GPU/NVDEC/memory, annotation
  flow, forwarder, and evidence checks all pass.
- P5.1 decode-location measurement; P5.2 derive `(batch_size, ANALYSIS_FPS)`;
  P5.3 disable unused output encoding; P5.5 memory validation.
- Generate the chosen TensorRT engines; set `max_parallel_streams ≥ 30+headroom`,
  `batched_push_timeout`, intervals.
- Forwarder `ANALYSIS_FPS` set to the measured sustainable value (per-source
  override supported).

**Acceptance — `PASS_PHASE2_SINGLE_T4_30`:** 30 streams on one T4 sustain target
`ANALYSIS_FPS` (±10%) over a 30-min run; `RestartCount` flat; NVDEC/compute/mem
within budget with headroom; `security.events`/annotations flowing for all 30;
evidence clips generated for sampled events. Record the measured operating point
in this spec's appendix.

### Phase 3 — Dual-T4 60-stream scale-out

- Parameterize the stack into two shards (A on `device_ids:['0']`, B on `['1']`)
  via compose + env; shared Redis/PG/workers.
- **Shard-aware replay-job routing:** clip-worker must send a source's replay job
  to the Replay shard that stored it (map `source_id → shard → REPLAY_API_URL` +
  job sink). This is the key new coupling — design and test it.
- Source assignment: extend `sources.generated.yml` / runtime apply to assign
  each camera to a shard; balance ~30/30.
- **Fixed + dynamic source parity:** the compose fixed source and the
  dynamic-source creation path (`runtime_apply.py` / `camera_source_controller.py`)
  must carry identical shard assignment, `SYNC_OUTPUT`, forwarder target, and
  tolerance settings — no drift between the two creation paths.
- **clip-worker replay shard routing:** clip-worker must resolve
  `source_id → shard → REPLAY_API_URL + job sink` and reject/redirect jobs aimed at
  the wrong shard (covered by a routing test).
- **video-file-sink concurrency:** one sink likely cannot absorb 60-stream
  evidence-job bursts; decide per-shard sink vs shared sink + Replay-job concurrency
  cap, and size it against `CLIP_WORKER_MAX_CONCURRENT_JOBS` and Replay job limits.
- Staged pressure test **10 → 30 → 60** with active reattach fault injection
  (per `midterm_multi_source_runtime_stability_plan.md` Phase 4 method).

**Acceptance — `PASS_PHASE3_DUAL_T4_60`:** 60 streams across both T4s;
`RestartCount` flat in steady state; both shards' annotations advancing;
evidence complete and correctly routed per shard; reattach of one source/shard
does not disturb the other; GPU/NVDEC/mem/storage within budget on both cards.

### Phase 4 — Production hardening

- Replay TTL/storage sizing on NVMe (P5.4); bound Replay job concurrency.
- Watchdog reliability: ensure the API-owned supervisor recovers a stalled shard
  without the Docker-restart timeout seen on 2026-06-13; restart/STOPPED drills.
- 8090 dashboards: per-shard FPS, forwarder drop rate, restart rate, queue depth,
  storage headroom; red thresholds.
- Capacity headroom + runbook; document failure modes and recovery.

**Acceptance — `PASS_PHASE4_PRODUCTION`:** drills pass; dashboards red-flag the
right conditions; runbook + capacity sizing committed.

## 7. Observability (Phase-common, ship in Phase 0)

Confirmed gaps in `services/api/app/services/runtime_overview.py` +
`services/evidence-viewer/app/static/operator.js`:

- `summarize_runtime_health()` ignores `restart_count` / restart rate.
- `_list_dynamic_source_containers()` does not collect `restart_count` (dynamic
  source with 1137 restarts is invisible on 8090).
- No threshold/red-flag on restart counts.

Changes:

- Collect `restart_count` + `started_at`/`finished_at` for dynamic sources.
- Compute a short-window restart **rate** (restarts since last poll / interval).
- Add a health issue + red row when restart rate exceeds a threshold; render
  `restart_count` and rate for fixed AND dynamic sources in `operator.js`.
- Extend `harness/tests/test_operator_runtime_overview_static.py` accordingly.

Reusable read-only diagnostic (no mutation) for every phase gate:

```bash
for c in video-analytics-midterm-source-adapter \
         video-analytics-source-source_00000000-0000-4000-8000-781078565686 \
         video-analytics-midterm-savant video-analytics-midterm-replay-service; do
  docker inspect --format '{{.Name}} restart={{.RestartCount}} status={{.State.Status}} exit={{.State.ExitCode}}' "$c"
done
docker exec video-analytics-midterm-savant cat /opt/savant/status.txt
curl --noproxy '*' -s http://127.0.0.1:18080/metrics | grep -E 'va_savant_(effective_fps|last_frame_age_seconds|sources_active)'
docker logs --tail 120 video-analytics-midterm-replay-service | grep -i 'send timeout\|Resource temporarily'
```

## 8. Risks & Rollback

| Risk | Mitigation |
|---|---|
| Disabling `MAX_FPS_CONTROL` doesn't stop EAGAIN (intake not the cause) | Phase 0 measurement branch → fall back to Replay-retry + adapter-tolerance, prioritize Phase 1 forwarder |
| Forwarder becomes a new bottleneck/SPOF | bounded queue + drop (never block); per-shard forwarder; metrics + watchdog; it does pure passthrough (cheap) |
| H264 GOP corruption from sampling | Phase 0.5 S1 is a hard gate; keyframe anchoring is necessary but not sufficient. If compressed sampling is not decode/annotation-equivalent, use decode→resample/re-encode or make the forwarder isolation-only; evidence remains full-rate. |
| 8fps×30 exceeds T4 compute | Phase 2 derives sustainable F; fallbacks: lower F, raise pose interval, INT8 |
| Shard replay-job mis-routing | Phase 3 `source_id→shard` map + tests; reject jobs to wrong shard |
| Replay RocksDB write saturation at 60 | NVMe + TTL sizing (P5.4); per-shard isolation |
| Editing shared `config.midterm.json` | minimal coordinated edits; revertible; covered by deployment-contract test |

Every phase is independently revertible (env/compose/config revert + recreate;
no destructive data ops; never `down -v`).

## 9. Commit Plan (clean history)

Repo style: imperative, capitalized subject, no conventional-commit prefix
(e.g. "Add runtime overview aggregation API"). Rules:

- One logical change per commit; tests committed **with** the code they cover.
- Never mix phases in one commit; never mix code with unrelated doc edits.
- Each commit leaves `compose config`, the relevant `pytest`, and
  `git diff --check` green.
- Suggested sequence:
  - `Decouple Savant ingress FPS gate from muxer max_fps_control`
  - `Shorten Replay out_stream send retry window for midterm`
  - `Surface source restart rate on 8090 runtime overview`
  - `Add analysis-forwarder service for sampled Savant analysis path`
  - `Route Replay output through analysis-forwarder`
  - `Disable unused Savant output frame encoding`
  - `Tune Savant batch/interval/parallel-streams for single-T4 30 streams`
  - `Shard midterm runtime across two T4 GPUs`
  - `Route replay jobs to the storing Replay shard`
  - `Add staged 10/30/60 load-test harness`
  - `Document dual-path 30x2 T4 operating point and runbook`
- `Document ...` commits for spec/runbook updates kept separate from code.

## 10. Decisions To Confirm Before Each Phase

1. **Phase 0 recreate window:** OK to recreate the live `savant` container
   (~5–15s inference gap, engines cached)? If the box cannot pause inference,
   schedule a window.
2. **`ANALYSIS_FPS` target:** start 8fps; accept Phase 2 may lower it to fit T4.
3. **Shard count vs module count:** this spec assumes 2 shards = 2 Savant
   modules (one per T4), matching the stated goal. Confirm before Phase 3.
4. **Forwarder language/runtime:** Python + savant_rs bindings (matches existing
   services). Confirm no preference for a Rust forwarder.
5. **Production hardware:** perf numbers must be measured on the real 2× T4
   target, not the current dev GPU.
6. **SYNC_OUTPUT:** OK to flip fixed + dynamic sources to `SYNC_OUTPUT=false`
   together in a 0C experiment window?
7. **Shared file `config.midterm.json`:** coordinate the 0B retry edit with the
   Replay/evidence workstream before landing.
8. **Forwarder image:** acceptable for `analysis-forwarder` to be based on the
   (large) Savant image if S2 finds no standalone savant_rs wheel path?

## Appendix A — Measured Operating Point (fill in Phase 2/3)

```text
T4 model / driver / CUDA:
pose engine batch / latency / throughput:
face engine batch / latency / throughput:
adaface engine batch / latency / throughput:
chosen ANALYSIS_FPS:
max_parallel_streams / batch_size / batched_push_timeout:
per-shard NVDEC fps / GPU util / mem peak:
Replay TTL / bitrate / working set / NVMe:
```
