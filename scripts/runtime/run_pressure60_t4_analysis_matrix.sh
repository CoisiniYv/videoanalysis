#!/usr/bin/env bash
set -uo pipefail

phase="${1:-all}"
case "${phase}" in
  ablation|output|cpu|timeout|mps|async|track|all) ;;
  *)
    echo "usage: $0 [ablation|output|cpu|timeout|mps|async|track|all]" >&2
    exit 2
    ;;
esac

profile="${PRESSURE_PROFILE:-4fps-t4}"
streams="${STREAMS:-60}"
duration_s="${DURATION_S:-400}"
drain_s="${DRAIN_S:-120}"
gpu_id="${GPU_ID:-0}"
isolated_cpu_profile="${ISOLATED_CPU_PROFILE:-t4-16cpu}"
matrix_id="${MATRIX_ID:-t4diag_$(date -u +%Y%m%dT%H%M%SZ)}"
runner="scripts/runtime/run_pressure60_dual1gpu_profile.sh"

run_case() {
  local case_id="$1"
  local stage="$2"
  local output_mode="$3"
  local timeout_us="$4"
  local cpu_profile="$5"
  local cuda_mps="${6:-0}"
  local adaface_async="${7:-0}"
  local face_track_id="${8:-0}"
  local run_id="pressure60_${matrix_id}_${case_id}"

  echo "MATRIX_CASE_START case=${case_id} run_id=${run_id} stage=${stage} output=${output_mode} timeout_us=${timeout_us} cpu=${cpu_profile}"
  RUN_ID="${run_id}" \
  STREAMS="${streams}" \
  DURATION_S="${duration_s}" \
  DRAIN_S="${drain_s}" \
  GPU_ID="${gpu_id}" \
  ABLATION_STAGE="${stage}" \
  OUTPUT_MODE="${output_mode}" \
  BATCH_TIMEOUT_US="${timeout_us}" \
  CPU_PROFILE="${cpu_profile}" \
  CUDA_MPS="${cuda_mps}" \
  ADAFACE_ASYNC="${adaface_async}" \
  FACE_TRACK_ID="${face_track_id}" \
    bash "${runner}" "${profile}"
  local rc=$?
  echo "MATRIX_CASE_END case=${case_id} run_id=${run_id} rc=${rc}"
  return 0
}

run_ablation() {
  run_case ab01_pose pose-only copy 40000 none
  run_case ab02_tracker pose-tracker-rules copy 40000 none
  run_case ab03_face pose-face copy 40000 none
  run_case ab04_adaface pose-face-adaface copy 40000 none
  run_case ab05_exporter full-exporter copy 40000 none
  run_case ab06_evidence full-evidence copy 40000 none
}

run_output() {
  run_case out01_copy full-exporter copy 40000 none
  run_case out02_metadata full-exporter metadata-only 40000 none
}

run_cpu() {
  run_case cpu01_shared full-evidence copy 40000 none
  run_case cpu02_isolated full-evidence copy 40000 "${isolated_cpu_profile}"
}

run_timeout() {
  run_case bt01_10ms full-exporter metadata-only 10000 none
  run_case bt02_20ms full-exporter metadata-only 20000 none
  run_case bt03_40ms full-exporter metadata-only 40000 none
}

run_mps() {
  run_case mps01_baseline full-exporter metadata-only 10000 none 0
  run_case mps02_enabled full-exporter metadata-only 10000 none 1
}

run_async() {
  run_case async01_sync full-exporter metadata-only 10000 none 0 0
  run_case async02_classifier full-exporter metadata-only 10000 none 0 1
}

run_track() {
  run_case track01_untracked full-exporter metadata-only 10000 none 0 0 0
  run_case track02_person_id full-exporter metadata-only 10000 none 0 0 1
}

case "${phase}" in
  ablation) run_ablation ;;
  output) run_output ;;
  cpu) run_cpu ;;
  timeout) run_timeout ;;
  mps) run_mps ;;
  async) run_async ;;
  track) run_track ;;
  all)
    run_ablation
    run_output
    run_cpu
    run_timeout
    run_mps
    run_async
    run_track
    ;;
esac

echo "MATRIX_COMPLETE matrix_id=${matrix_id} phase=${phase}"
