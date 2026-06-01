#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 2B — Behavior debug probe integration
#
# Checks:
#   1. Container phase2b-savant is running.
#   2. stage=phase2b_probe_init present.
#   3. stage=phase2b_converter_init present.
#   4. stage=phase2b_behavior_debug present.
#   5. stage=phase2b_intrusion_debug_event present.
#   6. observation_count > 0 in debug lines.
#   7. tracked_count > 0 in debug lines.
#   8. track_count > 0 in debug lines.
#   9. intrusion_event=yes in debug lines.
#  10. event_type=intrusion in event lines.
#  11. track_id > 0 in event lines.
#  12. Zero lines contain "FAILED" or "|ERROR|".
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase2b.yml"
CONTAINER_NAME="phase2b-savant"
WAIT_SECONDS=${WAIT_SECONDS:-60}

# --- Colours ---------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Colour

# --- Helpers ---------------------------------------------------------------
check() {
    local num="$1" desc="$2" result="$3"
    if [[ "$result" == "pass" ]]; then
        echo -e "  ${GREEN}✓${NC}  [${num}] ${desc}"
    else
        echo -e "  ${RED}✘${NC}  [${num}] ${desc}"
    fi
}

# --- 1. Container running --------------------------------------------------
echo "--- Phase 2B Smoke Test ---"
echo ""

PASS_COUNT=0
FAIL_COUNT=0

if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    check 1 "Container ${CONTAINER_NAME} is running" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 1 "Container ${CONTAINER_NAME} is running" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- Gather logs -----------------------------------------------------------
LOGS=$(docker logs "$CONTAINER_NAME" 2>&1 || true)

# --- 2. phase2b_probe_init -------------------------------------------------
if echo "$LOGS" | grep -q "stage=phase2b_probe_init"; then
    check 2 "stage=phase2b_probe_init present" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 2 "stage=phase2b_probe_init present" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- 3. phase2b_converter_init ---------------------------------------------
if echo "$LOGS" | grep -q "stage=phase2b_converter_init"; then
    check 3 "stage=phase2b_converter_init present" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 3 "stage=phase2b_converter_init present" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- 4. phase2b_behavior_debug ---------------------------------------------
if echo "$LOGS" | grep -q "stage=phase2b_behavior_debug"; then
    check 4 "stage=phase2b_behavior_debug present" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 4 "stage=phase2b_behavior_debug present" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- 5. phase2b_intrusion_debug_event --------------------------------------
if echo "$LOGS" | grep -q "stage=phase2b_intrusion_debug_event"; then
    check 5 "stage=phase2b_intrusion_debug_event present" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 5 "stage=phase2b_intrusion_debug_event present" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- 6. observation_count > 0 ----------------------------------------------
DEBUG_LINES=$(echo "$LOGS" | grep "stage=phase2b_behavior_debug" | head -10 || true)
OBS_OK=false
while IFS= read -r line; do
    VAL=$(echo "$line" | grep -oP 'observation_count=\K[0-9]+' || true)
    if [[ -n "$VAL" && "$VAL" -gt 0 ]]; then
        OBS_OK=true
        break
    fi
done <<< "$DEBUG_LINES"

if [[ "$OBS_OK" == "true" ]]; then
    check 6 "observation_count > 0" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 6 "observation_count > 0" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- 7. tracked_count > 0 --------------------------------------------------
TRACKED_OK=false
while IFS= read -r line; do
    VAL=$(echo "$line" | grep -oP 'tracked_count=\K[0-9]+' || true)
    if [[ -n "$VAL" && "$VAL" -gt 0 ]]; then
        TRACKED_OK=true
        break
    fi
done <<< "$DEBUG_LINES"

if [[ "$TRACKED_OK" == "true" ]]; then
    check 7 "tracked_count > 0" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 7 "tracked_count > 0" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- 8. track_count > 0 ----------------------------------------------------
TC_OK=false
while IFS= read -r line; do
    VAL=$(echo "$line" | grep -oP 'track_count=\K[0-9]+' || true)
    if [[ -n "$VAL" && "$VAL" -gt 0 ]]; then
        TC_OK=true
        break
    fi
done <<< "$DEBUG_LINES"

if [[ "$TC_OK" == "true" ]]; then
    check 8 "track_count > 0" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 8 "track_count > 0" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- 9. intrusion_event=yes ------------------------------------------------
IE_OK=false
while IFS= read -r line; do
    if echo "$line" | grep -q "intrusion_event=yes"; then
        IE_OK=true
        break
    fi
done <<< "$DEBUG_LINES"

if [[ "$IE_OK" == "true" ]]; then
    check 9 "intrusion_event=yes" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 9 "intrusion_event=yes" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- 10. event_type=intrusion ----------------------------------------------
EVENT_LINES=$(echo "$LOGS" | grep "stage=phase2b_intrusion_debug_event" | head -10 || true)
EVENT_TYPE_OK=false
while IFS= read -r line; do
    if echo "$line" | grep -q "event_type=intrusion"; then
        EVENT_TYPE_OK=true
        break
    fi
done <<< "$EVENT_LINES"

if [[ "$EVENT_TYPE_OK" == "true" ]]; then
    check 10 "event_type=intrusion" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 10 "event_type=intrusion" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- 11. track_id > 0 in event lines ---------------------------------------
TID_OK=false
while IFS= read -r line; do
    VAL=$(echo "$line" | grep -oP 'track_id=\K[0-9]+' || true)
    if [[ -n "$VAL" && "$VAL" -gt 0 ]]; then
        TID_OK=true
        break
    fi
done <<< "$EVENT_LINES"

if [[ "$TID_OK" == "true" ]]; then
    check 11 "track_id > 0 in event lines" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 11 "track_id > 0 in event lines" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- 12. Zero FAILED or |ERROR| lines --------------------------------------
ERROR_LINES=$(echo "$LOGS" | grep -c -E "FAILED|\|ERROR\|" 2>/dev/null || true)
if [[ "$ERROR_LINES" -eq 0 ]]; then
    check 12 "Zero FAILED or |ERROR| lines" pass
    PASS_COUNT=$((PASS_COUNT + 1))
else
    check 12 "Zero FAILED or |ERROR| lines (found ${ERROR_LINES})" fail
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# --- Summary ---------------------------------------------------------------
echo ""
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
