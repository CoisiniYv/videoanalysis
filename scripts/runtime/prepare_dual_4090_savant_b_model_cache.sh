#!/usr/bin/env bash
set -euo pipefail

SOURCE_MODEL_ROOT="${SOURCE_MODEL_ROOT:-/data/video-analytics/models}"
SAVANT_B_MODEL_ROOT="${SAVANT_B_MODEL_ROOT:-/data/video-analytics/models-savant-b}"

mkdir -p "$SAVANT_B_MODEL_ROOT"
if [[ ! -w "$SAVANT_B_MODEL_ROOT" ]]; then
  docker run --rm --entrypoint sh \
    -v "$SAVANT_B_MODEL_ROOT:/model-cache" \
    redis:7-alpine \
    -c "chown -R $(id -u):$(id -g) /model-cache"
fi
rsync -a --exclude='*.engine' "$SOURCE_MODEL_ROOT"/ "$SAVANT_B_MODEL_ROOT"/

if [[ -e "$SAVANT_B_MODEL_ROOT/yolov8_face/yolov8n-face.onnx" ]]; then
  ln -sfn yolov8_face/yolov8n-face.onnx "$SAVANT_B_MODEL_ROOT/yolov8_face.onnx"
  ln -sfn yolov8_face/yolov8n-face.onnx_b1_gpu0_fp16.engine \
    "$SAVANT_B_MODEL_ROOT/yolov8_face.onnx_b1_gpu0_fp16.engine"
fi

find "$SAVANT_B_MODEL_ROOT" -maxdepth 3 -type f ! -name '*.engine' -printf '%P\n' | sort
