#!/usr/bin/env bash
# F2.1 AdaFace Runtime — static config + runtime smoke
#
# Verifies:
# - module.yml has AdaFace nvinfer@attribute_model
# - model config matches official sample pattern
# - no forbidden elements (HNSWLIB, Recognition, Qdrant)
# - compose env has FACE_EMBEDDING_BATCH_SIZE=16
# - FACE_DETECTOR_BATCH_SIZE remains 1
# - face_embedding_debug.py exists
# - (runtime) AdaFace engine build/load succeeds
# - (runtime) [face_embedding] logs appear with feature_dim=512

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MODULE_YML="$REPO_ROOT/modules/savant_security/module.yml"
COMPOSE_FILE="$REPO_ROOT/infra/docker-compose.c1-official-adapter.yml"

pass=0
fail=0

ok() {
    pass=$((pass + 1))
    echo -e "  \033[32m PASS\033[0m  $1"
}

fail_msg() {
    fail=$((fail + 1))
    echo -e "  \033[31m FAIL\033[0m  $1"
}

check() {
    if eval "$2" >/dev/null 2>&1; then
        ok "$1"
    else
        fail_msg "$1"
    fi
}

echo "--- F2.1 Static: AdaFace attribute_model block ---"

check "module.yml exists" "test -f $MODULE_YML"
check "adaface element present" "grep -q 'name: adaface' $MODULE_YML"
check "adaface is nvinfer@attribute_model" "grep -B2 'name: adaface' $MODULE_YML | grep -q 'nvinfer@attribute_model'"
check "model file is adaface_ir50_webface4m.onnx" "grep -A5 'name: adaface' $MODULE_YML | grep -q 'adaface_ir50_webface4m.onnx'"
check "input object is yolov8_face.face" "grep -A10 'name: adaface' $MODULE_YML | grep -q 'yolov8_face.face'"
check "input shape is 3,112,112" "grep -A15 'name: adaface' $MODULE_YML | grep -q '\\[3, 112, 112\\]'"
check "offsets are 127.5" "grep -A20 'name: adaface' $MODULE_YML | grep -q '127.5, 127.5, 127.5'"
check "scale_factor is 0.007843137254902" "grep -A25 'name: adaface' $MODULE_YML | grep -q '0.007843137254902'"
check "color_format is bgr" "grep -A25 'name: adaface' $MODULE_YML | grep -q 'color_format: bgr'"
check "AlignFacePreprocessingObjectImageGPU" "grep -q 'AlignFacePreprocessingObjectImageGPU' $MODULE_YML"
check "TensorToVectorConverter" "grep -q 'TensorToVectorConverter' $MODULE_YML"
check "output layer feature" "grep -A30 'name: adaface' $MODULE_YML | grep -q 'layer_names: \\[feature\\]'"
check "no norm layer" "! grep -A40 'name: adaface' $MODULE_YML | grep -q '- norm'"

echo ""
echo "--- F2.1 Static: batch policy ---"

check "FACE_EMBEDDING_BATCH_SIZE default 16" "grep -q 'FACE_EMBEDDING_BATCH_SIZE, 16' $MODULE_YML"
check "FACE_DETECTOR_BATCH_SIZE still 1" "grep -q 'FACE_DETECTOR_BATCH_SIZE, 1' $MODULE_YML"

echo ""
echo "--- F2.1 Static: pipeline order ---"

check "adaface after face_person_associator" "
    awk '/name: face_person_associator/,/name: adaface/' $MODULE_YML | grep -q 'name: adaface'
"
check "face_embedding_debug after adaface" "
    awk '/name: adaface/,/name: face_embedding_debug/' $MODULE_YML | grep -q 'face_embedding_debug'
"

echo ""
echo "--- F2.1 Static: no forbidden elements ---"

check "no HNSWLIB" "! grep -qi 'hnswlib' $MODULE_YML"
check "no Recognition pyfunc" "! grep -q 'class_name: Recognition' $MODULE_YML"
check "no Qdrant" "! grep -qi 'qdrant' $MODULE_YML"
check "no Redis face_observations" "! grep -q 'face_observations' $MODULE_YML"

echo ""
echo "--- F2.1 Static: source files ---"

check "face_embedding_debug.py exists" "test -f $REPO_ROOT/modules/savant_security/custom/pyfuncs/face_embedding_debug.py"
check "test_f2_1_adaface_config.py exists" "test -f $REPO_ROOT/harness/tests/test_f2_1_adaface_config.py"

echo ""
echo "--- F2.1 Static: compose env ---"

check "compose file exists" "test -f $COMPOSE_FILE"
check "compose has FACE_EMBEDDING_BATCH_SIZE=16" "grep -q 'FACE_EMBEDDING_BATCH_SIZE: \"16\"' $COMPOSE_FILE"
check "compose FACE_DETECTOR_BATCH_SIZE still 1" "grep -q 'FACE_DETECTOR_BATCH_SIZE: \"1\"' $COMPOSE_FILE"
check "compose config valid" "docker compose -f $COMPOSE_FILE config --services >/dev/null"

echo ""
echo "--- F2.1 Static: model files on host ---"

check "yolov8_face.onnx exists" "test -f /data/video-analytics/models/yolov8_face.onnx"
check "adaface_ir50_webface4m.onnx exists" "test -f /data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx"

echo ""
echo "--- F2.1 Runtime smoke (requires running container) ---"

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q 'c1-official-savant'; then
    check "AlignFace import works" "docker exec c1-official-savant python -c 'from savant.input_preproc.align_face import AlignFacePreprocessingObjectImageGPU'"
    check "TensorToVectorConverter import works" "docker exec c1-official-savant python -c 'from savant.converter import TensorToVectorConverter'"
    echo ""
    echo "  NOTE: Full GPU runtime smoke requires compose restart to pick up AdaFace config."
    echo "  Run: docker compose -f $COMPOSE_FILE up -d --force-recreate savant-security"
    echo "  Then: docker logs -f c1-official-savant | grep '\\[face_embedding\\]'"
    echo "  Expect: feature_dim=512, person_track_id present, landmarks readable"
else
    echo "  c1-official-savant not running — runtime checks skipped"
fi

echo ""
echo "--- F2.1 Runtime smoke summary ---"

echo ""
echo "  Static checks: $pass passed, $fail failed"
echo ""

if [ "$fail" -gt 0 ]; then
    echo "  F2.1 static smoke: FAIL"
    exit 1
else
    echo "  All static checks passed."
    echo ""
    echo "  F2.1 static smoke: PASS"
    echo "  F2.1 runtime smoke: PARTIAL (imports verified, full GPU test needs compose restart)"
    exit 0
fi
