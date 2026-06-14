# Midterm Phase 1 Analysis Forwarder

Date: 2026-06-15

## Scope

Phase 1 implements the `PASS_PHASE05_SPIKE` decision from
`specs/16_dual_path_30x2_t4_production_optimization.md`: insert an
`analysis-forwarder` between Replay and Savant so the analysis branch can be
rate-limited and drop-on-backpressure without blocking full-rate Replay intake.

This phase does not start Phase 2/3. The current host/runtime is still a
two-source validation environment, not a real T4 + 30/60 stream production
benchmark.

## Topology

Before:

```text
source adapter -> Replay -> Savant
                 Replay -> video-file-sink, on demand evidence jobs
```

After:

```text
source adapter -> Replay -> analysis-forwarder -> Savant
                 Replay -> video-file-sink, on demand evidence jobs
```

Important contract:

- Replay remains the media authority and still receives the source stream at the
  original rate.
- Evidence clip generation still replays from Replay to `video-file-sink`; it is
  not sourced from the sampled analysis branch.
- `analysis-forwarder` only samples frames sent to Savant for annotation and
  inference.
- Accepted `VideoFrame` messages are forwarded with the same source id, UUID,
  PTS/time_base, keyframe flag, attributes, and external content bytes.
- The forwarder has a bounded queue. When Savant cannot drain fast enough, the
  analysis queue drops frames instead of blocking Replay and source adapters.
- Savant's internal `PtsFpsGate` remains enabled as a second protection layer.

## Implementation

Files added:

- `services/analysis-forwarder/Dockerfile`
- `services/analysis-forwarder/app/main.py`
- `services/analysis-forwarder/app/sampler.py`
- `services/analysis-forwarder/app/queueing.py`
- `harness/tests/test_analysis_forwarder.py`

Files changed:

- `modules/savant_replay/config.midterm.json`
  - Replay `out_stream` now sends to
    `dealer+connect:tcp://analysis-forwarder:5557`.
- `infra/docker-compose.midterm.yml`
  - Adds `analysis-forwarder` built from the Savant DeepStream base image.
  - Exposes metrics on container port `8081` and host port `18081`.
  - Sets default analysis sampling to `ANALYSIS_FPS=8/1`.
- `services/api/app/services/runtime_apply.py`
  - Controlled runtime apply now restarts Replay, then forwarder, then Savant,
    before source containers are started.
- `services/api/app/services/runtime_overview.py`
  - 8090 API now aggregates forwarder metrics and inspects the forwarder
    container.
- `services/evidence-viewer/app/static/index.html`
  - Adds the `分析限流` runtime panel.
- `services/evidence-viewer/app/static/operator.js`
  - Renders forwarder queue depth, seen/forwarded/dropped frames, drop
    percentage, and Savant send failures.

## Runtime Apply

The new service required one image build because it is a new container image and
must carry the Savant-compatible `savant_rs` package proved in Phase 0.5:

```bash
docker compose -f infra/docker-compose.midterm.yml build analysis-forwarder
docker compose -f infra/docker-compose.midterm.yml up -d --no-deps analysis-forwarder
docker restart video-analytics-midterm-replay-service
```

The evidence-viewer static assets also required a rebuild/recreate so the 8090
page served the new runtime panel:

```bash
docker compose -f infra/docker-compose.midterm.yml build evidence-viewer
docker compose -f infra/docker-compose.midterm.yml up -d --no-deps evidence-viewer
docker restart video-analytics-midterm-api
```

No `docker compose down -v` was used.

## Runtime Evidence

8090 API snapshot after Phase 1:

```text
health.ok=true
health.issues=[]
forwarder.global.queue_depth=0
forwarder.global.running=1
forwarder.primary_rtsp seen=21940 forwarded=7372 dropped=14568 send_failures=0
forwarder.source_00000000-0000-4000-8000-781078565686 seen=27452 forwarded=7322 dropped=20130 send_failures=0
```

Docker container snapshot:

```text
video-analytics-midterm-analysis-forwarder restart=0 status=running health=healthy
video-analytics-midterm-replay-service restart=0 status=running
video-analytics-midterm-savant restart=0 status=running health=healthy
video-analytics-midterm-source-adapter restart=0 status=running
video-analytics-source-source_00000000-0000-4000-8000-781078565686 restart=0 status=running
```

Recent Replay/source-adapter logs were searched for the failure signatures that
previously explained the restart storm:

```text
Replay: Resource temporarily unavailable / Send timeout / EAGAIN / error / panic -> none in sampled window
source-adapter: WriterResultSendTimeout / Internal data stream error / EAGAIN / error -> none in sampled window
```

Fault injection was also run by pausing Savant briefly and then unpausing it.
Observed result: Replay/source/forwarder/Savant `RestartCount` stayed flat,
forwarder drop counters increased, queue depth returned to `0`, and 8090 health
returned to `ok=true`.

Evidence lineage still resolves through Replay. Example generated after Phase 1:

```text
event_id=e5059d87-65ec-4930-9758-66716f51d35d
source_id=primary_rtsp
raw_clip_path=raw_clip.mov
evidence_anchor_strategy=uuid_first_pts_verified
event_frame_uuid=019ec718-1752-7201-aa51-d4c68a0e05be
anchor_keyframe_uuid=019ec718-14cc-77e2-bf1d-d714e8f133a4
start_window_frame_uuid=019ec718-0032-7d10-94fd-f4c6b0521c07
post_window_frame_uuid=019ec718-2ae1-7990-bfbb-f8b212512e5b
```

## Validation

Executed:

```bash
pytest -q harness/tests/test_analysis_forwarder.py \
  harness/tests/test_midterm_deployment_contract.py \
  harness/tests/test_camera_runtime_apply_service.py \
  harness/tests/test_runtime_overview_api.py \
  harness/tests/test_operator_runtime_overview_static.py \
  harness/tests/test_savant_perf_metrics_contract.py
docker compose -f infra/docker-compose.midterm.yml config
python3 -m json.tool modules/savant_replay/config.midterm.json
python3 -m py_compile services/analysis-forwarder/app/main.py \
  services/analysis-forwarder/app/sampler.py \
  services/analysis-forwarder/app/queueing.py
git diff --check
bash scripts/smoke/current/check_midterm_deployment.sh
bash scripts/smoke/current/check_savant_perf_observability.sh
curl --noproxy '*' -s http://127.0.0.1:8090/api/v1/runtime/overview
curl --noproxy '*' -s http://127.0.0.1:18081/metrics
```

## Decision

Keep Phase 1.

Rationale:

- It preserves full-rate Replay evidence while limiting only the analysis branch.
- It turns Savant-side stalls into analysis-frame drops instead of source adapter
  restarts.
- 8090 now exposes the forwarder and restart-rate signals through the API
  aggregation path.
- Runtime validation showed no source, Replay, forwarder, or Savant restarts in
  the sampled post-change window.

## PASS Token

`PASS_PHASE1_FORWARDER`

Phase 2 remains gated on a real T4 environment and 30 live/replayable input
streams.
