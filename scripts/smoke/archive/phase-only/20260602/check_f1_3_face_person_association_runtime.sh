#!/usr/bin/env bash
# F1.3 Face-Person Association — static config smoke
#
# Verifies:
# - module.yml has YOLOv8-Face full-frame detector
# - FacePersonAssociatorPyFunc appears after YOLOv8-Face
# - FaceDebugPyFunc still present
# - no AdaFace block yet
# - no Redis face stream producer yet

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

echo "--- F1.3 Pre-flight: module.yml structure ---"

check "module.yml exists" "test -f $MODULE_YML"
check "module.yml mentions yolov8_face" "grep -q 'yolov8_face' $MODULE_YML"
check "YOLOv8-Face has no input.object (full-frame)" "! grep -A5 'name: yolov8_face' $MODULE_YML | grep -q 'input:'"
check "FACE_DETECTOR_BATCH_SIZE=1" "grep -q 'FACE_DETECTOR_BATCH_SIZE, 1' $MODULE_YML"

echo ""
echo "--- F1.3 Pre-flight: pipeline element order ---"

# Check FacePersonAssociatorPyFunc appears in module.yml
check "FacePersonAssociatorPyFunc present" "grep -q 'face_person_associator' $MODULE_YML"

# Check FacePersonAssociatorPyFunc appears AFTER yolov8_face nvinfer block
check "face_person_associator after yolov8_face" "
    awk '/name: yolov8_face/,/name: face_person_associator/' $MODULE_YML | grep -q 'face_person_associator'
"

# Check FaceDebugPyFunc still present
check "FaceDebugPyFunc still present" "grep -q 'face_debug' $MODULE_YML"

# Check FaceDebugPyFunc appears after face_person_associator
check "face_debug after face_person_associator" "
    awk '/name: face_person_associator/,/name: face_debug/' $MODULE_YML | grep -q 'face_debug'
"

echo ""
echo "--- F1.3 Pre-flight: no forbidden elements ---"

check "no AdaFace block" "! grep -qi 'adaface' $MODULE_YML"
check "no Redis face_observations" "! grep -q 'face_observations' $MODULE_YML"
check "no HNSWLIB" "! grep -qi 'hnswlib' $MODULE_YML"
check "no Qdrant" "! grep -qi 'qdrant' $MODULE_YML"

echo ""
echo "--- F1.3 Pre-flight: source files ---"

check "face_person_association.py exists" "test -f $REPO_ROOT/modules/savant_security/custom/services/face_person_association.py"
check "face_person_associator.py exists" "test -f $REPO_ROOT/modules/savant_security/custom/pyfuncs/face_person_associator.py"
check "test_face_person_association.py exists" "test -f $REPO_ROOT/harness/tests/test_face_person_association.py"

echo ""
echo "--- F1.3 Pre-flight: compose ---"

check "compose file exists" "test -f $COMPOSE_FILE"
check "compose has savant-security" "grep -q 'savant-security' $COMPOSE_FILE"
check "compose config valid" "docker compose -f $COMPOSE_FILE config --services >/dev/null"

echo ""
echo "--- F1.3 Runtime smoke summary ---"

echo ""
echo "  Static checks: $pass passed, $fail failed"
echo ""

if [ "$fail" -gt 0 ]; then
    echo "  F1.3 static smoke: FAIL"
    exit 1
else
    echo "  All static checks passed."
    echo "  GPU runtime smoke requires: docker compose -f $COMPOSE_FILE up -d"
    echo "  Then watch logs: docker logs c1-official-savant | grep '\\[face_assoc\\]'"
    echo "  Expect: face/person/association counts + person_track_id in logs."
    echo ""
    echo "  F1.3 static smoke: PASS"
    echo "  F1.3 runtime smoke: NOT RUN (requires GPU + test video with person+face)"
    exit 0
fi
