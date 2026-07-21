#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  bash scripts/runtime/run_pressure60_dual1gpu_profile.sh [8fps-stress|4fps-t4]

Profiles:
  8fps-stress  60 native-rate streams; Savant analysis is resampled to 8/1 FPS.
  4fps-t4      Production-T4 probe; native-rate evidence, 4/1 FPS analysis.

Environment overrides:
  GPU_ID=0            Physical GPU used by both Savant branches.
  PYTHON_CMD=python3  Python interpreter for the pressure harness.
  RUN_ID=<id>         Override generated run id.
  RTSP_URI=<uri>      Override pressure input URI.
  RTSP_REPUBLISH_OUTPUT_BASE=<uri>
                      Per-source RTSP output base; supports {run_id}/{source_id}.
  RTSP_REPUBLISH_INPUT_URI=<uri-or-file>
                      Deterministic source read by every host republisher.
  RTSP_REPUBLISH_INPUT_SHA256=<sha256>
                      Optional fixed-input identity guard. The default 4090
                      fixture is pinned so a same-name wrong clip fails early.
  RTSP_REPUBLISH_MODE=copy
  RTSP_REPUBLISH_INPUT_OFFSET_S=0
  RTSP_REPUBLISH_INPUT_LOOP=0
  RTSP_REPUBLISH_H264_REPEAT_HEADERS=1
  RTSP_REPUBLISH_WARMUP_S=10
  RTSP_REPUBLISH_LOCAL_SERVER=1
                      Use a run-scoped MediaMTX instead of shared external RTSP.
  RTSP_REPUBLISH_LOCAL_SERVER_PORT=18554
                      Host port mapped to the run-scoped MediaMTX.
  STREAMS=60          Override stream count for local smoke runs.
  DURATION_S=<profile default>
                      Override measured sampling duration.
  DRAIN_S=120         Override evidence drain duration.
  PRESSURE_ALGORITHM_COOLDOWN_S=<profile default>
                      Override the pressure camera algorithm cooldown.
  PRESSURE_SOURCE_START_STAGGER_S=0.53
                      Stagger source starts without frame-period phase locking.
  EVIDENCE_GROUP_SIZE=<profile default>
                      Number of cameras assigned to each evidence window group.
  EVIDENCE_POLICY_GROUPS=<profile default>
                      Evidence pre:post windows; use 5:5 for one uniform window.
  CLEAR_EXISTING_EVIDENCE=0
                      Set to 1 to clear events/evidence but preserve trajectories.
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
  MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=<profile default>
                      Shared media-worker WIP candidate for Phase 6 A/B.
  MEDIA_WORKER_ROLLING_REMUX_WORKERS=<profile default>
                      Rolling remux lane width.
  MEDIA_WORKER_FINALIZER_WORKERS=<profile default>
                      Finalizer thread count.
  MEDIA_WORKER_FINALIZER_PROCESS_WORKERS=<profile default>
                      Finalizer bundle process count.
  MEDIA_WORKER_FINALIZER_QUEUE_CAPACITY=<profile default>
                      Bounded in-process finalizer queue capacity.
  MEDIA_WORKER_ROLLING_MAX_PER_POLL=<profile default>
                      New rolling remux admissions per scheduler poll.
  PRESERVE_WARMUP_RESULTS=1
                      Retain prefill event/evidence and fence formal gates by time.
  PRESSURE_PAUSE_REDIS_RDB=<profile default>
                      Pause automatic RDB snapshots during the measured local run;
                      the harness always restores the original Redis save schedule.
  PRESSURE_TUNE_POSTGRES_CHECKPOINTS=<profile default>
                      Use pressure-scoped WAL/checkpoint headroom and restore the
                      original PostgreSQL settings on every exit.
  PRESSURE_CACHE_TMPFS=<profile default>
                      Bind pressure rolling-cache/intermediate materialization
                      to shared host tmpfs; final evidence remains on disk.
  PRESSURE_ROLLING_CACHE_RETENTION_S=0
                      Explicit pressure-only retention. Use 300 for the daily
                      retention gate or 3840 for the endurance gate.
  ROLLING_CACHE_MIN_RAW_FPS=<profile default>
                      Minimum pre-resampler evidence cadence. The 4090 profile
                      requires 20 FPS while Savant still analyzes at 8 FPS.
  DRY_RUN=1           Print the command without executing it.
USAGE
}

profile="${1:-8fps-stress}"
case "${profile}" in
  8fps-stress)
    fps="8/1"
    min_fps="198/25"
    run_prefix="pressure60_8p1_dual1gpu_w20_r12_f8_cd30"
    duration_default_s="600"
    pressure_algorithm_cooldown_default_s="30"
    pressure_source_start_stagger_default_s="0.53"
    evidence_group_size_default="60"
    evidence_policy_groups_default="5:5"
    roi_batch_timeout_default_ms="40"
    batch_timeout_default_us="40000"
    cpu_profile_default="none"
    cuda_mps_default="0"
    mps_savant_percent_default="0"
    mps_adaface_percent_default="0"
    adaface_roi_redis_default="1"
    media_worker_materialization_max_active_default="20"
    media_worker_rolling_remux_workers_default="12"
    media_worker_finalizer_workers_default="8"
    media_worker_finalizer_process_workers_default="4"
    media_worker_finalizer_queue_capacity_default="8"
    media_worker_rolling_max_per_poll_default="8"
    pressure_pause_redis_rdb_default="1"
    pressure_tune_postgres_checkpoints_default="1"
    pressure_cache_tmpfs_default="1"
    rtsp_republish_input_uri_default="/data/video-analytics/pressure-fixtures/1080movie_o300_native24_gop12_continuous_1200s.mp4"
    rtsp_republish_input_sha256_default="688112c4172d9ab1328db717004e963cb0b9cf4f3642f5c9016ad9d1d76b1370"
    rolling_cache_min_raw_fps_default="20"
    # All 60 publishers use the same fixture. Spread them across its timeline
    # so a single high-bitrate scene does not create an artificial synchronized
    # NVDEC burst that independent production cameras would not share.
    rtsp_republish_input_offset_step_default_s="4"
    # Rolling-cache owns the encoded evidence branch. Savant only needs to
    # publish inference metadata/Redis observations in this profile.
    output_mode_default="metadata-only"
    ;;
  4fps-t4)
    fps="4/1"
    min_fps="99/25"
    run_prefix="pressure60_4p1_dual1gpu_cd60"
    duration_default_s="400"
    pressure_algorithm_cooldown_default_s="60"
    pressure_source_start_stagger_default_s="0.53"
    evidence_group_size_default="20"
    evidence_policy_groups_default="5:5,10:10,15:15"
    roi_batch_timeout_default_ms="200"
    batch_timeout_default_us="10000"
    cpu_profile_default="t4-16cpu-evidence"
    cuda_mps_default="1"
    mps_savant_percent_default="45"
    mps_adaface_percent_default="10"
    adaface_roi_redis_default="1"
    media_worker_materialization_max_active_default="4"
    media_worker_rolling_remux_workers_default="1"
    media_worker_finalizer_workers_default="4"
    media_worker_finalizer_process_workers_default="4"
    media_worker_finalizer_queue_capacity_default="4"
    media_worker_rolling_max_per_poll_default="4"
    pressure_pause_redis_rdb_default="0"
    pressure_tune_postgres_checkpoints_default="0"
    pressure_cache_tmpfs_default="0"
    rtsp_republish_input_uri_default=""
    rtsp_republish_input_sha256_default=""
    rolling_cache_min_raw_fps_default="20"
    rtsp_republish_input_offset_step_default_s="0"
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
duration_s="${DURATION_S:-${duration_default_s}}"
drain_s="${DRAIN_S:-120}"
pressure_algorithm_cooldown_s="${PRESSURE_ALGORITHM_COOLDOWN_S:-${pressure_algorithm_cooldown_default_s}}"
pressure_source_start_stagger_s="${PRESSURE_SOURCE_START_STAGGER_S:-${pressure_source_start_stagger_default_s}}"
evidence_group_size="${EVIDENCE_GROUP_SIZE:-${evidence_group_size_default}}"
evidence_policy_groups="${EVIDENCE_POLICY_GROUPS:-${evidence_policy_groups_default}}"
clear_existing_evidence="${CLEAR_EXISTING_EVIDENCE:-0}"
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
media_worker_materialization_max_active="${MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE:-${media_worker_materialization_max_active_default}}"
media_worker_rolling_remux_workers="${MEDIA_WORKER_ROLLING_REMUX_WORKERS:-${media_worker_rolling_remux_workers_default}}"
media_worker_finalizer_workers="${MEDIA_WORKER_FINALIZER_WORKERS:-${media_worker_finalizer_workers_default}}"
media_worker_finalizer_process_workers="${MEDIA_WORKER_FINALIZER_PROCESS_WORKERS:-${media_worker_finalizer_process_workers_default}}"
media_worker_finalizer_queue_capacity="${MEDIA_WORKER_FINALIZER_QUEUE_CAPACITY:-${media_worker_finalizer_queue_capacity_default}}"
media_worker_rolling_max_per_poll="${MEDIA_WORKER_ROLLING_MAX_PER_POLL:-${media_worker_rolling_max_per_poll_default}}"
pressure_pause_redis_rdb="${PRESSURE_PAUSE_REDIS_RDB:-${pressure_pause_redis_rdb_default}}"
pressure_tune_postgres_checkpoints="${PRESSURE_TUNE_POSTGRES_CHECKPOINTS:-${pressure_tune_postgres_checkpoints_default}}"
pressure_cache_tmpfs="${PRESSURE_CACHE_TMPFS:-${pressure_cache_tmpfs_default}}"
pressure_rolling_cache_retention_s="${PRESSURE_ROLLING_CACHE_RETENTION_S:-0}"
rolling_cache_min_raw_fps="${ROLLING_CACHE_MIN_RAW_FPS:-${rolling_cache_min_raw_fps_default}}"
preserve_warmup_results="${PRESERVE_WARMUP_RESULTS:-1}"
rtsp_republish_output_base="${RTSP_REPUBLISH_OUTPUT_BASE:-}"
rtsp_republish_input_uri="${RTSP_REPUBLISH_INPUT_URI:-${rtsp_republish_input_uri_default}}"
if [[ -n "${RTSP_REPUBLISH_INPUT_SHA256+x}" ]]; then
  rtsp_republish_input_sha256="${RTSP_REPUBLISH_INPUT_SHA256}"
elif [[ -z "${RTSP_REPUBLISH_INPUT_URI:-}" ]]; then
  rtsp_republish_input_sha256="${rtsp_republish_input_sha256_default}"
else
  rtsp_republish_input_sha256=""
fi
rtsp_republish_mode="${RTSP_REPUBLISH_MODE:-copy}"
rtsp_republish_input_offset_s="${RTSP_REPUBLISH_INPUT_OFFSET_S:-0}"
rtsp_republish_input_offset_step_s="${RTSP_REPUBLISH_INPUT_OFFSET_STEP_S:-${rtsp_republish_input_offset_step_default_s}}"
rtsp_republish_input_loop="${RTSP_REPUBLISH_INPUT_LOOP:-0}"
rtsp_republish_h264_repeat_headers="${RTSP_REPUBLISH_H264_REPEAT_HEADERS:-1}"
rtsp_republish_warmup_s="${RTSP_REPUBLISH_WARMUP_S:-10}"
rtsp_republish_local_server="${RTSP_REPUBLISH_LOCAL_SERVER:-1}"
rtsp_republish_local_server_port="${RTSP_REPUBLISH_LOCAL_SERVER_PORT:-18554}"

if [[ "${profile}" == "8fps-stress" && "${cuda_mps}" == "1" ]]; then
  printf '%s\n' \
    "ERROR: 8fps-stress forbids CUDA MPS with full-GPU TensorRT engines; use CUDA_MPS=0." \
    >&2
  exit 2
fi

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
  --evidence-group-size "${evidence_group_size}"
  --evidence-policy-groups "${evidence_policy_groups}"
  --batch-size 4
  --pose-batch-size 4
  --face-detector-batch-size 4
  --face-embedding-batch-size 16
  --max-parallel-streams 64
  --batched-push-timeout "${batch_timeout_us}"
  --media-worker-materialization-max-active "${media_worker_materialization_max_active}"
  --media-worker-rolling-remux-workers "${media_worker_rolling_remux_workers}"
  --media-worker-finalizer-workers "${media_worker_finalizer_workers}"
  --media-worker-finalizer-process-workers "${media_worker_finalizer_process_workers}"
  --media-worker-finalizer-queue-capacity "${media_worker_finalizer_queue_capacity}"
  --media-worker-rolling-max-per-poll "${media_worker_rolling_max_per_poll}"
  --pressure-rolling-cache-retention-s "${pressure_rolling_cache_retention_s}"
  --savant-ablation-stage "${ablation_stage}"
  --savant-output-mode "${output_mode}"
  --cpu-isolation-profile "${cpu_profile}"
  --pressure-algorithm-cooldown-s "${pressure_algorithm_cooldown_s}"
  --force-runtime-restart
  --dual-shard-same-gpu
  --dual-shard-gpu "${gpu_id}"
  --dual-shard-source-mode balanced
  --pressure-source-visibility-timeout-s 300
  --pressure-source-visibility-poll-s 5
  --pressure-source-visibility-stable-samples 2
  --pressure-source-visibility-restart-attempts 1
  --pressure-source-ffmpeg-timeout-ms 60000
  --pressure-source-ffmpeg-init-timeout-ms 60000
  --pressure-source-start-stagger-s "${pressure_source_start_stagger_s}"
)

if [[ "${ablation_stage}" == "full-evidence" ]]; then
  cmd+=(
    --keep-evidence -1
    --evidence-shard-count 4
    --rolling-cache-evidence
    --rolling-cache-min-raw-fps "${rolling_cache_min_raw_fps}"
    --rolling-cache-prefill-s 25
    --rolling-cache-postfill-s 25
  )
else
  cmd+=(--keep-evidence 0)
fi

if [[ "${pressure_pause_redis_rdb}" == "1" ]]; then
  cmd+=(--pressure-pause-redis-rdb)
fi

if [[ "${pressure_tune_postgres_checkpoints}" == "1" ]]; then
  cmd+=(--pressure-tune-postgres-checkpoints)
fi

if [[ "${pressure_cache_tmpfs}" == "1" ]]; then
  pressure_cache_host_base="/dev/shm/video-analytics-pressure/${run_id}"
  cmd+=(
    --pressure-rolling-cache-host-root
    "${pressure_cache_host_base}/rolling-cache"
    --pressure-rolling-cache-materialized-host-root
    "${pressure_cache_host_base}/rolling-cache-materialized"
  )
fi

if [[ "${clear_existing_evidence}" == "1" ]]; then
  cmd+=(--clear-existing-evidence)
fi

if [[ "${preserve_warmup_results}" == "1" ]]; then
  cmd+=(--preserve-warmup-results)
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
if [[ -n "${rtsp_republish_output_base}" || "${rtsp_republish_local_server}" == "1" ]]; then
  cmd+=(
    --rtsp-republish-mode "${rtsp_republish_mode}"
    --rtsp-republish-input-offset-s "${rtsp_republish_input_offset_s}"
    --rtsp-republish-input-offset-step-s "${rtsp_republish_input_offset_step_s}"
    --rtsp-republish-warmup-s "${rtsp_republish_warmup_s}"
    --rtsp-republish-readiness-timeout-s 120
    --rtsp-republish-readiness-parallelism 4
    --rtsp-republish-readiness-restart-attempts 1
  )
  if [[ "${rtsp_republish_local_server}" == "1" ]]; then
    cmd+=(
      --rtsp-republish-local-server
      --rtsp-republish-local-server-image bluenviron/mediamtx:1.11.3
      --rtsp-republish-local-server-network video-analytics-midterm_default
      --rtsp-republish-local-server-host-port "${rtsp_republish_local_server_port}"
    )
  else
    cmd+=(--rtsp-republish-output-base "${rtsp_republish_output_base}")
  fi
  if [[ -n "${rtsp_republish_input_uri}" ]]; then
    cmd+=(--rtsp-republish-input-uri "${rtsp_republish_input_uri}")
  fi
  if [[ "${rtsp_republish_input_loop}" == "1" ]]; then
    cmd+=(--rtsp-republish-input-loop)
  fi
  if [[ "${rtsp_republish_h264_repeat_headers}" == "1" ]]; then
    cmd+=(--rtsp-republish-h264-repeat-headers)
  fi
fi

printf 'pressure_profile=%s\n' "${profile}"
printf 'run_id=%s\n' "${run_id}"
printf 'fps=%s\n' "${fps}"
printf 'min_fps=%s\n' "${min_fps}"
printf 'streams=%s\n' "${streams}"
printf 'duration_s=%s\n' "${duration_s}"
printf 'drain_s=%s\n' "${drain_s}"
printf 'pressure_algorithm_cooldown_s=%s\n' "${pressure_algorithm_cooldown_s}"
printf 'pressure_source_start_stagger_s=%s\n' "${pressure_source_start_stagger_s}"
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
printf 'media_worker_materialization_max_active=%s\n' "${media_worker_materialization_max_active}"
printf 'media_worker_rolling_remux_workers=%s\n' "${media_worker_rolling_remux_workers}"
printf 'media_worker_finalizer_workers=%s\n' "${media_worker_finalizer_workers}"
printf 'media_worker_finalizer_process_workers=%s\n' "${media_worker_finalizer_process_workers}"
printf 'media_worker_finalizer_queue_capacity=%s\n' "${media_worker_finalizer_queue_capacity}"
printf 'media_worker_rolling_max_per_poll=%s\n' "${media_worker_rolling_max_per_poll}"
printf 'pressure_pause_redis_rdb=%s\n' "${pressure_pause_redis_rdb}"
printf 'pressure_tune_postgres_checkpoints=%s\n' "${pressure_tune_postgres_checkpoints}"
printf 'pressure_cache_tmpfs=%s\n' "${pressure_cache_tmpfs}"
printf 'pressure_rolling_cache_retention_s=%s\n' "${pressure_rolling_cache_retention_s}"
printf 'rolling_cache_min_raw_fps=%s\n' "${rolling_cache_min_raw_fps}"
printf 'preserve_warmup_results=%s\n' "${preserve_warmup_results}"
printf 'rtsp_republish_output_base=%s\n' "${rtsp_republish_output_base}"
printf 'rtsp_republish_input_uri=%s\n' "${rtsp_republish_input_uri}"
printf 'rtsp_republish_input_sha256=%s\n' "${rtsp_republish_input_sha256}"
printf 'rtsp_republish_mode=%s\n' "${rtsp_republish_mode}"
printf 'rtsp_republish_input_offset_s=%s\n' "${rtsp_republish_input_offset_s}"
printf 'rtsp_republish_input_offset_step_s=%s\n' "${rtsp_republish_input_offset_step_s}"
printf 'rtsp_republish_input_loop=%s\n' "${rtsp_republish_input_loop}"
printf 'adaface_decoupled_sharded=%s\n' "${adaface_sharded}"
printf 'adaface_roi_redis=%s\n' "${adaface_roi_redis}"
printf 'adaface_roi_batch_timeout_ms=%s\n' "${roi_batch_timeout_ms}"
printf 'dual_shard_same_gpu=true\n'
printf 'dual_shard_gpu=%s\n' "${gpu_id}"
printf 'visual_results_retained=true\n'
printf 'evidence_group_size=%s\n' "${evidence_group_size}"
printf 'evidence_policy_groups=%s\n' "${evidence_policy_groups}"
printf 'clear_existing_evidence=%s\n' "${clear_existing_evidence}"
if [[ "${ablation_stage}" == "full-evidence" ]]; then
  printf 'rolling_cache_prefill_s=25\n'
  printf 'rolling_cache_postfill_s=25\n'
fi
printf 'command:'
printf ' %q' "${cmd[@]}"
printf '\n'

if [[ -n "${rtsp_republish_input_sha256}" ]] && \
   { [[ "${DRY_RUN:-0}" != "1" ]] || [[ -n "${RTSP_REPUBLISH_INPUT_SHA256+x}" ]]; }; then
  if [[ ! -f "${rtsp_republish_input_uri}" ]]; then
    printf 'ERROR: pinned pressure fixture is missing: %s\n' \
      "${rtsp_republish_input_uri}" >&2
    exit 2
  fi
  actual_input_sha256="$(sha256sum "${rtsp_republish_input_uri}" | awk '{print $1}')"
  if [[ "${actual_input_sha256}" != "${rtsp_republish_input_sha256}" ]]; then
    printf 'ERROR: pressure fixture identity mismatch: expected=%s actual=%s path=%s\n' \
      "${rtsp_republish_input_sha256}" "${actual_input_sha256}" \
      "${rtsp_republish_input_uri}" >&2
    exit 2
  fi
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  exit 0
fi

exec "${cmd[@]}"
