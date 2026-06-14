# Midterm Phase 2 Operating Point Appendix

Date: 2026-06-15

## Scope

Phase 2 acceptance requires recording the measured single-T4 operating point in
`specs/16_dual_path_30x2_t4_production_optimization.md` Appendix A. The current
development host cannot produce that measurement, but the post-pressure-run
write path is now automated and gated on a passing report.

## Added Tool

Command to run only after `PASS_PHASE2_SINGLE_T4_30`:

```bash
python3 scripts/runtime/update_phase2_operating_point.py \
  --report-path /data/video-analytics/artifacts/phase2_single_t4_pressure.json
```

The tool refuses failed reports and only updates Appendix A when the report has:

```json
{"passed": true, "pass_token": "PASS_PHASE2_SINGLE_T4_30"}
```

It writes the pressure runner's measured FPS, GPU/NVDEC/memory, source count,
duration, and evidence delta into the appendix. TensorRT engine latency and
storage sizing fields remain explicit fill-ins because they are not measured by
the runtime pressure runner.
