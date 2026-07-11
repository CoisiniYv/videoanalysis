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
  CPU_PROFILE=<profile default>
                      T4 uses staged t4-16cpu-evidence isolation.
  CUDA_MPS=<profile default>
                      T4 enables same-GPU CUDA MPS.
  MPS_SAVANT_PERCENT=<profile default>
                      Per-Savant MPS active-thread share (T4: 45).
  MPS_ADAFACE_PERCENT=<profile default>
                      ROI AdaFace MPS active-thread share (T4: 10).
  ADAFACE_ASYNC=0      Set to 1 for DeepStream classifier async mode canary.
  FACE_TRACK_ID=0      Set to 1 to propagate person IDs to face objects.
  ADAFACE_QUEUE=0       Set to 1 to insert a bounded queue before AdaFace.
  ADAFACE_CROP=0        Set to 1 for the diagnostic bbox crop+resize path.
  ADAFACE_PRE_GATE=0    Set to 1 to throttle face candidates before AdaFace.
  ADAFACE_DECOUPLED=0   Set to 1 for central AdaFace off the dual-YOLO path.
  ADAFACE_SHARDED=0     Set to 1 for one decoupled AdaFace sidecar per shard.
  ADAFACE_ROI_REDIS=0   Set to 1 for aligned 112x112 Redis ROI AdaFace worker.
  ROI_BATCH_TIMEOUT_MS=<profile default>
                      AdaFace ROI batch16 aggregation wait (T4: 200ms).
  DRY_RUN=1           Print the command without executing it.
USAGE
}

profile="${1:-8fps-stress}"
case "${profile}" in
  8fps-stress)
    fps="8/1"
    min_fps="198/25"
    run_prefix="pressure60_8p1_dual1gpu_cd60"
    roi_batch_timeout_default_ms="40"
    batch_timeout_default_us="40000"
    cpu_profile_default="none"
    cuda_mps_default="0"
    mps_savant_percent_default="0"
    mps_adaface_percent_default="0"
    adaface_roi_redis_default="0"
    output_mode_default="copy"
    ;;
  4fps-t4)
    fps="4/1"
    min_fps="99/25"
    run_prefix="pressure60_4p1_dual1gpu_cd60"
    roi_batch_timeout_default_ms="200"
    batch_timeout_default_us="10000"
    cpu_profile_default="t4-16cpu-evidence"
    cuda_mps_default="1"
    mps_savant_percent_default="45"
    mps_adaface_percent_default="10"
    adaface_roi_redis_default="1"
    output_mode_default="metadata-only"
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
output_mode="${OUTPUT_MODE:-${output_mode_default}}"
batch_timeout_us="${BATCH_TIMEOUT_US:-${batch_timeout_default_us}}"
cpu_profile="${CPU_PROFILE:-${cpu_profile_default}}"
cuda_mps="${CUDA_MPS:-${cuda_mps_default}}"
mps_savant_percent="${MPS_SAVANT_PERCENT:-${mps_savant_percent_default}}"
mps_adaface_percent="${MPS_ADAFACE_PERCENT:-${mps_adaface_percent_default}}"
adaface_async="${ADAFACE_ASYNC:-0}"
face_track_id="${FACE_TRACK_ID:-0}"
adaface_queue="${ADAFACE_QUEUE:-0}"
adaface_crop="${ADAFACE_CROP:-0}"
adaface_pre_gate="${ADAFACE_PRE_GATE:-0}"
adaface_decoupled="${ADAFACE_DECOUPLED:-0}"
adaface_sharded="${ADAFACE_SHARDED:-0}"
adaface_roi_redis="${ADAFACE_ROI_REDIS:-${adaface_roi_redis_default}}"
roi_batch_timeout_ms="${ROI_BATCH_TIMEOUT_MS:-${roi_batch_timeout_default_ms}}"

cmd=(
  "${python_cmd}" scripts/runtime/run_midterm_pressure60.py
  --run-id "${run_id}"
  --streams "${streams}"
  --fps "${fps}"
  --min-fps "${min_fps}"
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

if [[ "${cuda_mps}" == "1" ]]; then
  cmd+=(
    --cuda-mps
    --mps-savant-active-thread-percentage "${mps_savant_percent}"
    --mps-adaface-active-thread-percentage "${mps_adaface_percent}"
  )
fi
if [[ "${adaface_async}" == "1" ]]; then
  cmd+=(--adaface-classifier-async)
fi
if [[ "${face_track_id}" == "1" ]]; then
  cmd+=(--face-secondary-track-id)
fi
if [[ "${adaface_queue}" == "1" ]]; then
  cmd+=(--adaface-input-queue)
fi
if [[ "${adaface_crop}" == "1" ]]; then
  cmd+=(--adaface-crop-resize)
fi
if [[ "${adaface_pre_gate}" == "1" ]]; then
  cmd+=(--adaface-pre-gate)
fi
if [[ "${adaface_decoupled}" == "1" ]]; then
  cmd+=(--adaface-decoupled)
fi
if [[ "${adaface_sharded}" == "1" ]]; then
  cmd+=(--adaface-decoupled-sharded)
fi
if [[ "${adaface_roi_redis}" == "1" ]]; then
  cmd+=(--adaface-roi-redis --adaface-roi-batch-timeout-ms "${roi_batch_timeout_ms}")
fi

if [[ -n "${RTSP_URI:-}" ]]; then
  cmd+=(--rtsp-uri "${RTSP_URI}")
fi

printf 'pressure_profile=%s\n' "${profile}"
printf 'run_id=%s\n' "${run_id}"
printf 'fps=%s\n' "${fps}"
printf 'min_fps=%s\n' "${min_fps}"
printf 'streams=%s\n' "${streams}"
printf 'duration_s=%s\n' "${duration_s}"
printf 'savant_ablation_stage=%s\n' "${ablation_stage}"
printf 'savant_output_mode=%s\n' "${output_mode}"
printf 'batched_push_timeout_us=%s\n' "${batch_timeout_us}"
printf 'cpu_isolation_profile=%s\n' "${cpu_profile}"
printf 'cuda_mps=%s\n' "${cuda_mps}"
printf 'mps_savant_active_thread_percentage=%s\n' "${mps_savant_percent}"
printf 'mps_adaface_active_thread_percentage=%s\n' "${mps_adaface_percent}"
printf 'adaface_classifier_async=%s\n' "${adaface_async}"
printf 'face_secondary_track_id=%s\n' "${face_track_id}"
printf 'adaface_input_queue=%s\n' "${adaface_queue}"
printf 'adaface_crop_resize=%s\n' "${adaface_crop}"
printf 'adaface_pre_gate=%s\n' "${adaface_pre_gate}"
printf 'adaface_decoupled=%s\n' "${adaface_decoupled}"
printf 'adaface_decoupled_sharded=%s\n' "${adaface_sharded}"
printf 'adaface_roi_redis=%s\n' "${adaface_roi_redis}"
printf 'adaface_roi_batch_timeout_ms=%s\n' "${roi_batch_timeout_ms}"
printf 'dual_shard_same_gpu=true\n'
printf 'dual_shard_gpu=%s\n' "${gpu_id}"
printf 'visual_results_retained=true\n'
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
