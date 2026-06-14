# Midterm Phase 2 Readiness Gate

Date: 2026-06-15

## Scope

Phase 2 requires a real single-T4 30-stream environment before any operating
point can be measured or accepted. This note records the gate added after
`PASS_PHASE1_FORWARDER` so future runs cannot accidentally treat the current
development box as a valid Phase 2 pressure-test host.

## Added Gate

Command:

```bash
python3 scripts/runtime/check_phase2_single_t4_readiness.py \
  --runtime-overview-url http://127.0.0.1:8090/api/v1/runtime/overview
```

The command checks:

- `PASS_PHASE1_FORWARDER` is documented.
- Replay `out_stream` targets `analysis-forwarder`.
- `analysis-forwarder` targets Savant.
- Source adapters still push full-rate input into Replay.
- Savant depends on the forwarder.
- At least 30 enabled RTSP/gstreamer sources are present.
- At least one NVIDIA T4 is present.
- When the 8090 runtime overview URL is supplied, the runtime is healthy,
  forwarder metrics are available, the forwarder container is running, and the
  runtime reports at least 30 sources.

Passing this gate emits:

```text
PASS_PHASE2_SINGLE_T4_READY
```

This is an entry token only. It does not replace
`PASS_PHASE2_SINGLE_T4_30`, which still requires a 30-minute 30-stream T4 run
with stable restarts, measured GPU/NVDEC/memory headroom, flowing annotations,
and evidence clips.

## Current Host Result

The current host is intentionally not Phase-2-ready:

- GPU inventory: 2x NVIDIA GeForce RTX 4090, not T4.
- Enabled RTSP/gstreamer sources: 2, not 30.
- 8090 runtime source count: 2, not 30.

Therefore Phase 2/3 remain gated. No Phase 2 pressure-test tuning was applied in
this environment.
