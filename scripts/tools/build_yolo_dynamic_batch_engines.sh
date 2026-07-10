#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  bash scripts/tools/build_yolo_dynamic_batch_engines.sh

Build the production batch>1 TensorRT engines for the midterm YOLO models.

Environment overrides:
  DATA_ROOT=/data/video-analytics
  BATCH_SIZE=4
  GPU_ID=0
  ENGINE_GPU_INDEX=0
  CONDA_ENV=xl
  PYTHON_CMD="python3"
  SAVANT_IMAGE=ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${1:-}" == "help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DATA_ROOT="${DATA_ROOT:-/data/video-analytics}"
BATCH_SIZE="${BATCH_SIZE:-4}"
GPU_ID="${GPU_ID:-0}"
ENGINE_GPU_INDEX="${ENGINE_GPU_INDEX:-0}"
CONDA_ENV="${CONDA_ENV:-xl}"
SAVANT_IMAGE="${SAVANT_IMAGE:-ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1}"

if [[ ! "$BATCH_SIZE" =~ ^[1-9][0-9]*$ ]]; then
  echo "BATCH_SIZE must be a positive integer: $BATCH_SIZE" >&2
  exit 2
fi

if [[ -n "${PYTHON_CMD:-}" ]]; then
  # shellcheck disable=SC2206
  python_cmd=(${PYTHON_CMD})
elif command -v conda >/dev/null 2>&1; then
  python_cmd=(conda run -n "$CONDA_ENV" python)
else
  python_cmd=(python3)
fi

echo "Patching dynamic-batch ONNX variants..."
"${python_cmd[@]}" "$REPO_ROOT/scripts/tools/patch_yolo_dynamic_batch_onnx.py"

models_root="$DATA_ROOT/models"
pose_onnx="/models/yolo26_pose/yolo26_pose.dynamic.raw56.onnx"
face_onnx="/models/yolov8_face/yolov8n-face.dynamic.onnx"
pose_engine="${pose_onnx}_b${BATCH_SIZE}_gpu${ENGINE_GPU_INDEX}_fp16.engine"
face_engine="${face_onnx}_b${BATCH_SIZE}_gpu${ENGINE_GPU_INDEX}_fp16.engine"

build_engine() {
  local onnx_path="$1"
  local engine_path="$2"
  docker run --rm --gpus "device=${GPU_ID}" \
    -v "${models_root}:/models" \
    --entrypoint bash \
    "$SAVANT_IMAGE" \
    -lc "/usr/src/tensorrt/bin/trtexec \
      --onnx=${onnx_path} \
      --minShapes=images:1x3x640x640 \
      --optShapes=images:${BATCH_SIZE}x3x640x640 \
      --maxShapes=images:${BATCH_SIZE}x3x640x640 \
      --fp16 \
      --saveEngine=${engine_path} \
      --duration=0 \
      --warmUp=0 \
      --iterations=1"
}

echo "Building pose engine: ${pose_engine}"
build_engine "$pose_onnx" "$pose_engine"

echo "Building face engine: ${face_engine}"
build_engine "$face_onnx" "$face_engine"

cat <<EOF
Built dynamic-batch engines:
  ${models_root}/yolo26_pose/$(basename "$pose_engine")
  ${models_root}/yolov8_face/$(basename "$face_engine")
EOF
