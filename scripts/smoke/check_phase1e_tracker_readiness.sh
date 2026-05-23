#!/usr/bin/env bash
# ===========================================================================
# check_phase1e_tracker_readiness.sh — Phase 1E Step 3: nvtracker readiness
#
# Verifies that:
#   1. Module container is running
#   2. YOLO26-pose engine is loaded
#   3. nvtracker is configured in the pipeline
#   4. metadata_handoff_ok=true  (from Phase 1E Step 2)
#   5. official_person_count > 0
#   6. official_keypoints_value_len == 51
#   7. official_tracked_person_count > 0
#   8. track_id_ready=true
#   9. first_valid_track_id is a valid non-zero integer (not UINT64_MAX)
#  10. All pass criteria met
# ===========================================================================

set -u
FAIL=0
PASS=0
TOTAL=0

CONTAINER_NAME="phase1e-savant"
COMPOSE_FILE="infra/docker-compose.phase1e.yml"
MODULE_YML="modules/savant_phase1e/module.yml"
CONFIG_DIR="modules/savant_phase1e/config"

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

# ===========================================================================
# Check 1: Container is running
# ===========================================================================
inc
CONTAINER_STATUS=$(docker inspect --format='{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "missing")
if [ "$CONTAINER_STATUS" = "running" ]; then
    pass "Container '$CONTAINER_NAME' is running."
else
    warn "Container '$CONTAINER_NAME' status: $CONTAINER_STATUS (expected 'running')."
fi

# ===========================================================================
# Check 2: Module entry point started
# ===========================================================================
inc
if log_contains "savant\.entrypoint\|Starting pipeline\|module.yml\|Pipeline starting"; then
    pass "Savant module entry point has started."
else
    warn "No Savant module startup message found in logs."
fi

# ===========================================================================
# Check 3: YOLO26-pose engine loaded
# ===========================================================================
inc
if log_contains "yolo26_pose.*engine\|Engine.*yolo26_pose\|model.*yolo26_pose.*loaded\|nvinfer.*yolo26_pose"; then
    pass "YOLO26-pose engine loaded."
else
    warn "No YOLO26-pose engine loaded message found in logs."
fi

# ===========================================================================
# Check 4: nvtracker is configured in module.yml
# ===========================================================================
inc
if grep -q "nvtracker" "$MODULE_YML" 2>/dev/null; then
    pass "nvtracker element found in module.yml."
else
    warn "nvtracker element NOT found in $MODULE_YML."
fi

# ===========================================================================
# Check 5: nvtracker config file exists
# ===========================================================================
inc
CONFIG_FILE="$CONFIG_DIR/config_tracker_NvDCF_perf.yml"
if [ -f "$CONFIG_FILE" ]; then
    pass "Tracker config file exists: $CONFIG_FILE"
else
    warn "Tracker config file missing: $CONFIG_FILE"
fi

# ===========================================================================
# Check 6: metadata_handoff_ok=true (from any frame)
# ===========================================================================
inc
if grep -q "phase=phase1e_official_probe_summary.*metadata_handoff_ok=true" < <(get_logs); then
    pass "metadata_handoff_ok=true found in probe summary."
else
    warn "metadata_handoff_ok=true NOT found. Converter -> metadata handoff may still have issues."
fi

# ===========================================================================
# Check 7: official_person_count > 0
# ===========================================================================
inc
PERSON_LINE=$(grep "phase=phase1e_official_probe_summary" < <(get_logs) | grep "official_person_count=" | tail -1)
if echo "$PERSON_LINE" | grep -qP "official_person_count=[1-9][0-9]*"; then
    PERSON_COUNT=$(echo "$PERSON_LINE" | grep -oP "official_person_count=\K[0-9]+")
    pass "official_person_count=$PERSON_COUNT (>0)."
else
    warn "official_person_count is 0 or missing. No person objects detected."
fi

# ===========================================================================
# Check 8: official_keypoints_value_len == 51
# ===========================================================================
inc
KP_LINE=$(grep "phase=phase1e_official_probe_summary" < <(get_logs) | grep "official_keypoints_value_len=" | tail -1)
if echo "$KP_LINE" | grep -qP "official_keypoints_value_len=51"; then
    pass "official_keypoints_value_len=51 (correct keypoints dimension)."
else
    KP_LEN=$(echo "$KP_LINE" | grep -oP "official_keypoints_value_len=\K[0-9]+" 2>/dev/null || echo "missing")
    warn "official_keypoints_value_len=$KP_LEN (expected 51)."
fi

# ===========================================================================
# Check 9: official_tracked_person_count > 0 (nvtracker is assigning IDs)
# ===========================================================================
inc
TRACKED_LINE=$(grep "phase=phase1e_official_probe_summary" < <(get_logs) | grep "official_tracked_person_count=" | tail -1)
if echo "$TRACKED_LINE" | grep -qP "official_tracked_person_count=[1-9][0-9]*"; then
    TRACKED_COUNT=$(echo "$TRACKED_LINE" | grep -oP "official_tracked_person_count=\K[0-9]+")
    pass "official_tracked_person_count=$TRACKED_COUNT (>0). nvtracker is assigning track_ids."
else
    TRACKED_COUNT=$(echo "$TRACKED_LINE" | grep -oP "official_tracked_person_count=\K[0-9]+" 2>/dev/null || echo "0")
    # Check if any frame shows tracked > 0
    ANY_TRACKED=$(grep "phase=phase1e_official_probe_summary" < <(get_logs) | grep -cP "official_tracked_person_count=[1-9][0-9]*" 2>/dev/null || echo "0")
    if [ "$ANY_TRACKED" -gt 0 ]; then
        pass "official_tracked_person_count > 0 on at least one frame."
    else
        warn "official_tracked_person_count=$TRACKED_COUNT. No tracked persons found. nvtracker may not be assigning IDs."
    fi
fi

# ===========================================================================
# Check 10: track_id_ready=true
# ===========================================================================
inc
if grep -q "phase=phase1e_official_probe_summary.*track_id_ready=true" < <(get_logs); then
    pass "track_id_ready=true found. Tracker readiness confirmed."
else
    warn "track_id_ready=true NOT found in any probe summary."
    # Show what track_id_ready values exist
    grep "phase=phase1e_official_probe_summary" < <(get_logs) | grep -oP "track_id_ready=\S+" | head -3 | while read -r val; do
        echo "  [INFO] observed: $val"
    done
fi

# ===========================================================================
# Check 11: first_valid_track_id is valid (non-zero, not UINT64_MAX, not "none")
# ===========================================================================
inc
TID_LINE=$(grep "phase=phase1e_official_probe_summary" < <(get_logs) | grep "track_id_ready=true" | head -1)
TID=$(echo "$TID_LINE" | grep -oP "first_valid_track_id=\K\S+")
if [ -n "$TID" ] && [ "$TID" != "none" ] && [ "$TID" != "18446744073709551615" ] && [ "$TID" != "0" ]; then
    pass "first_valid_track_id=$TID (valid track_id)."
elif [ -z "$TID" ]; then
    warn "first_valid_track_id not found (track_id_ready may be false)."
else
    warn "first_valid_track_id=$TID (expected a valid non-zero integer < UINT64_MAX)."
fi

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "============================================="
echo " Phase 1E Step 3 — nvtracker Readiness Report"
echo "============================================="
echo "  Total checks : $TOTAL"
echo "  Passed       : $PASS"
echo "  Failed       : $FAIL"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "  FAILED checks: Some nvtracker readiness criteria not met."
    echo "  Actions to consider:"
    echo "    - Wait for more frames: docker logs phase1e-savant --tail 50"
    echo "    - Restart module: sudo docker compose -f infra/docker-compose.phase1e.yml restart savant-phase1e"
    echo "    - Check nvtracker config syntax: cat modules/savant_phase1e/config/config_tracker_NvDCF_perf.yml"
    echo "    - Verify nvtracker element in pipeline: cat modules/savant_phase1e/module.yml"
    echo ""
    exit 1
fi

echo ""
echo "  *** ALL CHECKS PASSED ***"
echo "  nvtracker is operational and assigning valid track_ids."
echo ""
exit 0
