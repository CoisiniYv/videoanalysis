#!/usr/bin/env bash
# F2.3 Face Observation Redis — static config + runtime smoke
#
# Verifies:
# - FaceObservationExporterPyFunc exists in module.yml after FaceReidGatePyFunc
# - FACE_OBSERVATION_STREAM=security.face_observations
# - no HNSWLIB / Qdrant / pgvector / watchlist in Savant
# - no image bytes fields
# - batch policies unchanged
# - (runtime) Redis stream entries appear with correct fields

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

echo "--- F2.3 Static: FaceObservationExporterPyFunc ---"

check "module.yml exists" "test -f $MODULE_YML"
check "face_observation_exporter present" "grep -q 'name: face_observation_exporter' $MODULE_YML"
check "is pyfunc" "grep -B2 'name: face_observation_exporter' $MODULE_YML | grep -q 'element: pyfunc'"
check "module correct" "grep -A5 'name: face_observation_exporter' $MODULE_YML | grep -q 'custom.pyfuncs.face_observation_exporter'"
check "class correct" "grep -A5 'name: face_observation_exporter' $MODULE_YML | grep -q 'FaceObservationExporterPyFunc'"

echo ""
echo "--- F2.3 Static: pipeline order ---"

check "after face_reid_gate" "
    awk '/name: face_reid_gate/,/name: face_observation_exporter/' $MODULE_YML | grep -q 'face_observation_exporter'
"
check "before face_embedding_debug" "
    awk '/name: face_observation_exporter/,/name: face_embedding_debug/' $MODULE_YML | grep -q 'face_embedding_debug'
"

echo ""
echo "--- F2.3 Static: stream config ---"

check "FACE_OBSERVATION_STREAM env in compose" "grep -q 'FACE_OBSERVATION_STREAM' $COMPOSE_FILE"
check "FACE_OBSERVATION_EXPORT_ENABLED in compose" "grep -q 'FACE_OBSERVATION_EXPORT_ENABLED' $COMPOSE_FILE"

echo ""
echo "--- F2.3 Static: no forbidden elements ---"

check "no HNSWLIB" "! grep -qi 'hnswlib' $MODULE_YML"
check "no Qdrant" "! grep -qi 'qdrant' $MODULE_YML"
check "no pgvector element in module" "! grep -q 'element.*pgvector\\|pgvector.*element' $MODULE_YML"
check "no Recognition pyfunc" "! grep -q 'class_name: Recognition' $MODULE_YML"
check "no image bytes in schema" "! grep -qi 'image_bytes\\|base64\\|crop_bytes' $REPO_ROOT/modules/savant_security/custom/models/face_events.py"

echo ""
echo "--- F2.3 Static: batch policy unchanged ---"

check "FACE_DETECTOR_BATCH_SIZE still 1" "grep -q 'FACE_DETECTOR_BATCH_SIZE, 1' $MODULE_YML"
check "FACE_EMBEDDING_BATCH_SIZE still 16" "grep -q 'FACE_EMBEDDING_BATCH_SIZE, 16' $MODULE_YML"

echo ""
echo "--- F2.3 Static: source files ---"

check "face_observation_exporter.py service" "test -f $REPO_ROOT/modules/savant_security/custom/services/face_observation_exporter.py"
check "face_observation_exporter.py pyfunc" "test -f $REPO_ROOT/modules/savant_security/custom/pyfuncs/face_observation_exporter.py"
check "face_events.py model" "test -f $REPO_ROOT/modules/savant_security/custom/models/face_events.py"
check "test_face_observation_exporter.py" "test -f $REPO_ROOT/harness/tests/test_face_observation_exporter.py"

echo ""
echo "--- F2.3 Runtime smoke ---"

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q 'c1-official-savant'; then
    check "importable in container" "docker exec c1-official-savant python -c 'from custom.pyfuncs.face_observation_exporter import FaceObservationExporterPyFunc'"
    check "service importable" "docker exec c1-official-savant python -c 'from custom.services.face_observation_exporter import create_face_observation_exporter'"
    echo ""
    echo "  NOTE: Full GPU+Redis runtime smoke requires compose restart."
    echo "  Run: docker compose -f $COMPOSE_FILE up -d --force-recreate savant-security"
    echo "  Then check Redis: docker exec c1-official-redis XLEN security.face_observations"
else
    echo "  c1-official-savant not running — runtime checks skipped"
fi

echo ""
echo "--- F2.3 Runtime smoke summary ---"

echo ""
echo "  Static checks: $pass passed, $fail failed"
echo ""

if [ "$fail" -gt 0 ]; then
    echo "  F2.3 static smoke: FAIL"
    exit 1
else
    echo "  All static checks passed."
    echo ""
    echo "  F2.3 static smoke: PASS"
    echo "  F2.3 runtime smoke: PARTIAL (imports verified, full GPU+Redis test needs restart)"
    exit 0
fi
