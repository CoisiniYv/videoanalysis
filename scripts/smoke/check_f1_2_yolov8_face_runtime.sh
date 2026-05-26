#!/usr/bin/env bash
# check_f1_2_yolov8_face_runtime.sh
#
# Phase F1.2 smoke — verify YOLOv8-Face detector is reachable and
# produces face objects + landmarks without breaking C1.2 path.
#
# Prerequisites:
#   - Docker and docker compose available
#   - C1 official adapter compose: infra/docker-compose.c1-official-adapter.yml
#   - Model symlink: /data/video-analytics/models/yolov8_face.onnx
#   - Test video available (C1_TEST_SOURCE_URI or default)
#
# This script does NOT run GPU inference inside this process; it
# validates config, model path, and container compose startup.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/docker-compose.c1-official-adapter.yml"
MODULE_YML="${REPO_ROOT}/modules/savant_security/module.yml"
MODEL_PATH="/data/video-analytics/models/yolov8_face.onnx"

PASS=0
FAIL=0

green()  { echo -e "\033[32m$*\033[0m"; }
red()    { echo -e "\033[31m$*\033[0m"; }

check() {
    local desc="$1" result="$2"
    if [[ "$result" -eq 0 ]]; then
        green "  PASS  $desc"
        PASS=$((PASS + 1))
    else
        red "  FAIL  $desc"
        FAIL=$((FAIL + 1))
    fi
}

section() { echo; echo "--- $* ---"; }

# ------------------------------------------------------------------
section "F1.2 Pre-flight: model path"
# ------------------------------------------------------------------
if [[ -f "$MODEL_PATH" ]]; then
    check "model file exists at $MODEL_PATH" 0
    check "model file > 1 MB" "$(test "$(stat -Lc%s "$MODEL_PATH" 2>/dev/null || echo 0)" -gt 1048576 && echo 0 || echo 1)"
else
    check "model file exists at $MODEL_PATH" 1
fi

# ------------------------------------------------------------------
section "F1.2 Pre-flight: module.yml references"
# ------------------------------------------------------------------
grep -q 'yolov8_face' "$MODULE_YML" && check "module.yml mentions yolov8_face" 0 || check "module.yml mentions yolov8_face" 1
grep -q 'YoloV8faceConverter' "$MODULE_YML" && check "module.yml uses official YoloV8faceConverter" 0 || check "module.yml uses official YoloV8faceConverter" 1
grep -q 'label: face' "$MODULE_YML" && check "module.yml has face label object" 0 || check "module.yml has face label object" 1
grep -q 'landmarks' "$MODULE_YML" && check "module.yml has landmarks attribute" 0 || check "module.yml has landmarks attribute" 1
grep -q 'FACE_DETECTOR_BATCH_SIZE' "$MODULE_YML" && check "module.yml has FACE_DETECTOR_BATCH_SIZE param" 0 || check "module.yml has FACE_DETECTOR_BATCH_SIZE param" 1

# ------------------------------------------------------------------
section "F1.2 Pre-flight: batch policy"
# ------------------------------------------------------------------
FACE_BATCH_DEFAULT=$(grep -oP "FACE_DETECTOR_BATCH_SIZE,\s*\K\d+" "$MODULE_YML" 2>/dev/null | head -1 || echo "not-found")
if [[ "$FACE_BATCH_DEFAULT" == "1" ]]; then
    check "FACE_DETECTOR_BATCH_SIZE default is 1" 0
else
    check "FACE_DETECTOR_BATCH_SIZE default is 1 (got: $FACE_BATCH_DEFAULT)" 1
fi

grep -q 'FACE_DETECTOR_BATCH_SIZE.*"1"' "$COMPOSE_FILE" && check "compose sets FACE_DETECTOR_BATCH_SIZE=1" 0 || check "compose sets FACE_DETECTOR_BATCH_SIZE=1" 1

# ------------------------------------------------------------------
section "F1.2 Pre-flight: full-frame detector (no input.object)"
# ------------------------------------------------------------------
YOLOV8_BLOCK=$(awk '/name: yolov8_face/,/^    - element:/' "$MODULE_YML" | head -200)
if echo "$YOLOV8_BLOCK" | grep -q 'input'; then
    INPUT_LINE=$(echo "$YOLOV8_BLOCK" | grep 'input' | grep -v 'shape' || true)
    if echo "$INPUT_LINE" | grep -q 'object'; then
        check "yolov8_face has NO input.object (full-frame)" 1
    else
        check "yolov8_face has NO input.object (full-frame)" 0
    fi
else
    check "yolov8_face has NO input.object (full-frame)" 0
fi

# ------------------------------------------------------------------
section "F1.2 Pre-flight: compose file structure"
# ------------------------------------------------------------------
test -f "$COMPOSE_FILE" && check "compose file exists" 0 || check "compose file exists" 1
grep -q 'savant-security' "$COMPOSE_FILE" && check "compose has savant-security service" 0 || check "compose has savant-security service" 1

# ------------------------------------------------------------------
section "F1.2 Pre-flight: model is not committed to git"
# ------------------------------------------------------------------
if git -C "$REPO_ROOT" ls-files --error-unmatch "models/yolov8_face.onnx" >/dev/null 2>&1; then
    check "model NOT tracked by git" 1
else
    check "model NOT tracked by git" 0
fi

# ------------------------------------------------------------------
section "F1.2 Container: compose config syntax check"
# ------------------------------------------------------------------
docker compose -f "$COMPOSE_FILE" config --quiet >/dev/null 2>&1 && check "docker compose config valid" 0 || check "docker compose config valid" 1

# ------------------------------------------------------------------
section "F1.2 Runtime smoke summary"
# ------------------------------------------------------------------
echo
echo "  Static checks: $PASS passed, $FAIL failed"
echo

if [[ "$FAIL" -gt 0 ]]; then
    echo "  NEXT: fix failures above, then re-run."
    echo "  AFTER static checks pass, run full runtime smoke:"
    echo "    C1_TEST_SOURCE_URI=<rtsp-or-file> bash $0 --runtime"
    exit 1
fi

echo "  All static checks passed."
echo "  GPU runtime smoke requires: docker compose -f $COMPOSE_FILE up -d"
echo "  Then watch logs: docker logs c1-official-savant | grep '\[face_debug\]'"
echo "  Expect: face objects + landmarks logged every ~30 frames."
echo
echo "  F1.2 static smoke: PASS"
echo "  F1.2 runtime smoke: NOT RUN (requires GPU + test video)"
exit 0
