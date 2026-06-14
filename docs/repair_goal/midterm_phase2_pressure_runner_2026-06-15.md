# Midterm Phase 2 Pressure Runner

Date: 2026-06-15

## Scope

After `PASS_PHASE1_FORWARDER` and the Phase 2 readiness gate, the next required
artifact is a repeatable way to decide whether a real single-T4 / 30-stream run
earns `PASS_PHASE2_SINGLE_T4_30`. This note records the pressure-run checker
added for that purpose.

## Added Runner

Command for the target T4 machine:

```bash
python3 scripts/runtime/check_phase2_single_t4_pressure.py \
  --report-path /data/video-analytics/artifacts/phase2_single_t4_pressure.json
```

By default the runner:

- calls `check_phase2_single_t4_readiness.py` first;
- samples 8090 runtime overview and `nvidia-smi` for 1800 seconds;
- requires at least 30 runtime sources;
- requires per-source effective FPS to stay within the configured target range
  (`8fps ±10%` by default);
- requires frame and annotation counters to advance for all sources;
- requires Docker restart counts to stay flat;
- requires forwarder queue depth and send failures to stay within bounds;
- requires GPU, decoder, and memory headroom to stay within configured limits;
- requires at least one evidence summary generated during the run.

Passing emits:

```text
PASS_PHASE2_SINGLE_T4_30
```

The current development host is not expected to run the 30-minute window because
the readiness gate fails first: it has 2 enabled sources and 0 T4 GPUs. No Phase
2 tuning was applied here.
