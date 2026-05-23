#!/usr/bin/env bash
# ===========================================================================
# check_phase1f_observation_adapter.sh — Phase 1F smoke test
#
# Verifies:
#   1. Compose file exists
#   2. module.yml includes yolo26_pose, nvtracker, person_pose_observation_probe
#   3. Container starts
#   4. Logs contain stage=phase1f_person_pose_observation
#   5. observation_adapter_ok=true
#   6. observation_count > 0
#   7. first_valid_track_id is a valid positive integer
#   8. first_keypoints_flat_len == 51
#   9. first_keypoints_len == 17
#  10. 0 failed
#
# Because the probe logs every 15 frames (~0.5s at 30fps + pipeline init),
# the script waits up to 30 s for the diagnostic line to appear.
# ===========================================================================

set -u
FAIL=0
PASS=0
TOTAL=0

CONTAINER_NAME="phase1f-savant"
COMPOSE_FILE="infra/docker-compose.phase1f.yml"
MODULE_YML="modules/savant_phase1f/module.yml"
WAIT_SECONDS=30
SLEEP_INTERVAL=3

RED=""; GREEN=""; NC=""
if [ -t 1 ]; then
    RED="\033[0;31m"
    GREEN="\033[0;32m"
    NC="\033[0m"
fi

inc()  { TOTAL=$((TOTAL + 1)); }
pass() { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $*"; }
warn() { echo -e "  ${RED}FAIL${NC} $*"; FAIL=$((FAIL + 1)); }

# ---- Helpers ----
get_logs() {
    docker logs "$CONTAINER_NAME" 2>&1
}

log_contains() {
    local pattern="$1"
    get_logs | grep -q "$pattern"
}

wait_for_log() {
    local pattern="$1"
    local waited=0
    while [ "$waited" -lt "$WAIT_SECONDS" ]; do
        if log_contains "$pattern"; then
            return 0
        fi
        sleep "$SLEEP_INTERVAL"
        waited=$((waited + SLEEP_INTERVAL))
    done
    # One last check before giving up
    log_contains "$pattern"
}

# ===========================================================================
# Check 1: Compose file exists
# ===========================================================================
inc
if [ -f "$COMPOSE_FILE" ]; then
    pass "Compose file exists: $COMPOSE_FILE"
else
    warn "Compose file missing: $COMPOSE_FILE"
fi

# ===========================================================================
# Check 2: module.yml includes yolo26_pose
# ===========================================================================
inc
if grep -q "yolo26_pose" "$MODULE_YML" 2>/dev/null; then
    pass "module.yml includes yolo26_pose element."
else
    warn "module.yml missing yolo26_pose element."
fi

# ===========================================================================
# Check 3: module.yml includes nvtracker
# ===========================================================================
inc
if grep -q "nvtracker" "$MODULE_YML" 2>/dev/null; then
    pass "module.yml includes nvtracker element."
else
    warn "module.yml missing nvtracker element."
fi

# ===========================================================================
# Check 4: module.yml includes person_pose_observation_probe
# ===========================================================================
inc
if grep -q "person_pose_observation_probe" "$MODULE_YML" 2>/dev/null; then
    pass "module.yml includes person_pose_observation_probe element."
else
    warn "module.yml missing person_pose_observation_probe element."
fi

# ===========================================================================
# Check 5: Container is running
# ===========================================================================
inc
CONTAINER_STATUS=$(docker inspect --format='{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "missing")
if [ "$CONTAINER_STATUS" = "running" ]; then
    pass "Container '$CONTAINER_NAME' is running."
else
    warn "Container '$CONTAINER_NAME' status: $CONTAINER_STATUS (expected 'running')."
fi

# ===========================================================================
# Check 6: Wait for stage=phase1f_person_pose_observation (up to 30 s)
# ===========================================================================
inc
if wait_for_log "stage=phase1f_person_pose_observation"; then
    pass "stage=phase1f_person_pose_observation found in logs."
else
    warn "stage=phase1f_person_pose_observation NOT found in logs after ${WAIT_SECONDS}s."
fi

# ===========================================================================
# Check 7: observation_adapter_ok=true
# ===========================================================================
inc
if grep -q "stage=phase1f_person_pose_observation.*observation_adapter_ok=true" < <(get_logs); then
    pass "observation_adapter_ok=true found."
else
    warn "observation_adapter_ok=true NOT found in logs."
fi

# ===========================================================================
# Check 8: observation_count > 0
# ===========================================================================
inc
OBS_LINE=$(grep "stage=phase1f_person_pose_observation" < <(get_logs) | grep "observation_adapter_ok=true" | head -1)
if echo "$OBS_LINE" | grep -qP "observation_count=[1-9][0-9]*"; then
    OBS_COUNT=$(echo "$OBS_LINE" | grep -oP "observation_count=\K[0-9]+")
    pass "observation_count=$OBS_COUNT (>0)."
else
    OBS_COUNT=$(echo "$OBS_LINE" | grep -oP "observation_count=\K[0-9]+" 2>/dev/null || echo "0")
    warn "observation_count=$OBS_COUNT (expected >0)."
fi

# ===========================================================================
# Check 9: first_valid_track_id is valid positive integer
# ===========================================================================
inc
TID=$(echo "$OBS_LINE" | grep -oP "first_valid_track_id=\K\S+" 2>/dev/null || echo "none")
if [ -n "$TID" ] && [ "$TID" != "none" ] && [ "$TID" != "18446744073709551615" ] && [ "$TID" != "0" ] && [ "$TID" -gt 0 ] 2>/dev/null; then
    pass "first_valid_track_id=$TID (valid positive integer)."
else
    warn "first_valid_track_id=$TID (expected valid positive integer > 0)."
fi

# ===========================================================================
# Check 10: first_keypoints_flat_len == 51
# ===========================================================================
inc
FLAT_LEN=$(echo "$OBS_LINE" | grep -oP "first_keypoints_flat_len=\K[0-9]+" 2>/dev/null || echo "0")
if [ "$FLAT_LEN" = "51" ]; then
    pass "first_keypoints_flat_len=51 (correct keypoints dimension)."
else
    warn "first_keypoints_flat_len=$FLAT_LEN (expected 51)."
fi

# ===========================================================================
# Check 11: first_keypoints_len == 17
# ===========================================================================
inc
KP_LEN=$(echo "$OBS_LINE" | grep -oP "first_keypoints_len=\K[0-9]+" 2>/dev/null || echo "0")
if [ "$KP_LEN" = "17" ]; then
    pass "first_keypoints_len=17 (17 COCO keypoints)."
else
    warn "first_keypoints_len=$KP_LEN (expected 17)."
fi

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "============================================="
echo " Phase 1F — Observation Adapter Smoke Report"
echo "============================================="
echo "  Total checks : $TOTAL"
echo "  Passed       : $PASS"
echo "  Failed       : $FAIL"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "  FAILED checks: Some observation adapter criteria not met."
    echo "  Actions to consider:"
    echo "    - Check container logs: docker logs $CONTAINER_NAME --tail 80"
    echo "    - Restart module: sudo docker compose -f $COMPOSE_FILE restart savant-phase1f"
    echo "    - Verify module.yml: cat $MODULE_YML"
    echo ""
    exit 1
fi

echo ""
echo "  *** ALL CHECKS PASSED ***"
echo "  PersonPoseObservation adapter is operational."
echo ""
exit 0
