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
  STREAMS=60          Override stream count for local smoke runs.
  DURATION_S=400      Override measured sampling duration.
  DRAIN_S=120         Override evidence drain duration.
  ABLATION_STAGE=full-evidence
                      pose-only|pose-tracker-rules|pose-face|
                      pose-face-adaface|full-exporter|full-evidence.
  OUTPUT_MODE=copy    copy|metadata-only Savant output experiment.
  BATCH_TIMEOUT_US=40000
                      nvstreammux batched-push-timeout in microseconds.
  CPU_PROFILE=none    none|local-24cpu|t4-16cpu temporary cpuset layout.
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
streams="${STREAMS:-60}"
duration_s="${DURATION_S:-400}"
drain_s="${DRAIN_S:-120}"
ablation_stage="${ABLATION_STAGE:-full-evidence}"
output_mode="${OUTPUT_MODE:-copy}"
batch_timeout_us="${BATCH_TIMEOUT_US:-40000}"
cpu_profile="${CPU_PROFILE:-none}"

cmd=(
  "${python_cmd}" scripts/runtime/run_midterm_pressure60.py
  --run-id "${run_id}"
  --streams "${streams}"
  --fps "${fps}"
  --min-fps 1/1
  --duration-s "${duration_s}"
  --sample-interval-s 30
  --drain-s "${drain_s}"
  --guard-wait-s 1200
  --evidence-group-size 20
  --evidence-policy-groups 5:5,10:10,15:15
  --batch-size 4
  --pose-batch-size 4
  --face-detector-batch-size 4
  --face-embedding-batch-size 16
  --max-parallel-streams 64
  --batched-push-timeout "${batch_timeout_us}"
  --savant-ablation-stage "${ablation_stage}"
  --savant-output-mode "${output_mode}"
  --cpu-isolation-profile "${cpu_profile}"
  --pressure-algorithm-cooldown-s 60
  --force-runtime-restart
  --dual-shard-same-gpu
  --dual-shard-gpu "${gpu_id}"
  --dual-shard-source-mode balanced
  --pressure-source-visibility-timeout-s 300
  --pressure-source-visibility-poll-s 5
  --pressure-source-visibility-stable-samples 2
  --pressure-source-visibility-restart-attempts 1
  --pressure-source-ffmpeg-timeout-ms 60000
  --pressure-source-start-stagger-s 0.5
)

if [[ "${ablation_stage}" == "full-evidence" ]]; then
  cmd+=(
    --keep-evidence -1
    --evidence-shard-count 4
    --rolling-cache-evidence
    --rolling-cache-prefill-s 25
    --rolling-cache-postfill-s 25
  )
else
  cmd+=(--keep-evidence 0)
fi

if [[ -n "${RTSP_URI:-}" ]]; then
  cmd+=(--rtsp-uri "${RTSP_URI}")
fi

printf 'pressure_profile=%s\n' "${profile}"
printf 'run_id=%s\n' "${run_id}"
printf 'fps=%s\n' "${fps}"
printf 'streams=%s\n' "${streams}"
printf 'duration_s=%s\n' "${duration_s}"
printf 'savant_ablation_stage=%s\n' "${ablation_stage}"
printf 'savant_output_mode=%s\n' "${output_mode}"
printf 'batched_push_timeout_us=%s\n' "${batch_timeout_us}"
printf 'cpu_isolation_profile=%s\n' "${cpu_profile}"
printf 'dual_shard_same_gpu=true\n'
printf 'dual_shard_gpu=%s\n' "${gpu_id}"
printf 'evidence_policy_groups=5:5,10:10,15:15\n'
if [[ "${ablation_stage}" == "full-evidence" ]]; then
  printf 'rolling_cache_prefill_s=25\n'
  printf 'rolling_cache_postfill_s=25\n'
fi
printf 'command:'
printf ' %q' "${cmd[@]}"
printf '\n'

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  exit 0
fi

exec "${cmd[@]}"
