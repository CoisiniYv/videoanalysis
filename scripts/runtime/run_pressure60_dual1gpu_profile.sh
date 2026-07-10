#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  bash scripts/runtime/run_pressure60_dual1gpu_profile.sh [8fps-stress|4fps-t4]

Profiles:
  8fps-stress  Canonical 2026-07-09 stress profile: 60 streams, 8/1 FPS.
  4fps-t4      Production-T4 probe: same topology and batches, 4/1 FPS.

Environment overrides:
  GPU_ID=0            Physical GPU used by both Savant branches.
  PYTHON_CMD=python3  Python interpreter for the pressure harness.
  RUN_ID=<id>         Override generated run id.
  RTSP_URI=<uri>      Override pressure input URI.
  DRY_RUN=1           Print the command without executing it.
USAGE
}

profile="${1:-8fps-stress}"
case "${profile}" in
  8fps-stress)
    fps="8/1"
    run_prefix="pressure60_8p1_dual1gpu_cd60"
    ;;
  4fps-t4)
    fps="4/1"
    run_prefix="pressure60_4p1_dual1gpu_cd60"
    ;;
  -h|--help|help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_id="${RUN_ID:-${run_prefix}_${timestamp}}"
gpu_id="${GPU_ID:-0}"
python_cmd="${PYTHON_CMD:-python3}"

cmd=(
  "${python_cmd}" scripts/runtime/run_midterm_pressure60.py
  --run-id "${run_id}"
  --streams 60
  --fps "${fps}"
  --min-fps 1/1
  --duration-s 400
  --sample-interval-s 30
  --drain-s 120
  --guard-wait-s 1200
  --keep-evidence -1
  --evidence-group-size 20
  --evidence-policy-groups 5:5,10:10,15:15
  --batch-size 4
  --pose-batch-size 4
  --face-detector-batch-size 4
  --face-embedding-batch-size 16
  --max-parallel-streams 64
  --batched-push-timeout 40000
  --pressure-algorithm-cooldown-s 60
  --force-runtime-restart
  --dual-shard-same-gpu
  --dual-shard-gpu "${gpu_id}"
  --dual-shard-source-mode balanced
  --evidence-shard-count 4
  --rolling-cache-evidence
  --rolling-cache-prefill-s 25
  --rolling-cache-postfill-s 25
  --pressure-source-visibility-timeout-s 300
  --pressure-source-visibility-poll-s 5
  --pressure-source-visibility-stable-samples 2
  --pressure-source-visibility-restart-attempts 1
  --pressure-source-ffmpeg-timeout-ms 60000
  --pressure-source-start-stagger-s 0.5
)

if [[ -n "${RTSP_URI:-}" ]]; then
  cmd+=(--rtsp-uri "${RTSP_URI}")
fi

printf 'pressure_profile=%s\n' "${profile}"
printf 'run_id=%s\n' "${run_id}"
printf 'fps=%s\n' "${fps}"
printf 'dual_shard_same_gpu=true\n'
printf 'dual_shard_gpu=%s\n' "${gpu_id}"
printf 'evidence_policy_groups=5:5,10:10,15:15\n'
printf 'rolling_cache_prefill_s=25\n'
printf 'rolling_cache_postfill_s=25\n'
printf 'command:'
printf ' %q' "${cmd[@]}"
printf '\n'

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  exit 0
fi

exec "${cmd[@]}"
