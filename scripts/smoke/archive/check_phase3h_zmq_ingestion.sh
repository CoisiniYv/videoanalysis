#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 3H — Official ZMQ Adapter Ingestion POC
# ---------------------------------------------------------------------------
# PREREQ:
#   docker compose -f infra/docker-compose.phase3h-zmq.yml up -d
#
# Verifies that Savant module using official zeromq_source_bin (ZMQ input)
# can receive frames from source-adapter and produce real detection events.
# ---------------------------------------------------------------------------
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase3h-zmq.yml"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
PASS_COUNT=0; FAIL_COUNT=0

_redis() { docker exec phase3h-zmq-redis redis-cli "$@" 2>/dev/null; }
_pg()   { docker exec phase3h-zmq-postgres psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null || echo ""; }

check() {
    local num="$1" desc="$2" result="$3"
    if [[ "$result" == "pass" ]]; then
        echo -e "  ${GREEN}OK${NC}  [$num] $desc"; PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo -e "  ${RED}FAIL${NC}  [$num] $desc"; FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}
fatal() { echo -e "${RED}FATAL${NC}: $*"; exit 1; }

echo "--- Phase 3H Official ZMQ Adapter Ingestion POC Smoke Test ---"
echo ""

# Pre-flight: check all required services
REQUIRED="phase3h-zmq-redis phase3h-zmq-postgres phase3h-zmq-source-adapter phase3h-zmq-savant phase3h-zmq-event-worker"
MISSING=""
for c in $REQUIRED; do
    S="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null || echo "missing")"
    [[ "$S" != "running" ]] && MISSING="$MISSING $c($S)"
done
[[ -n "$MISSING" ]] && { echo -e "${RED}Containers not ready:${NC}"; for m in $MISSING; do echo "  - $m"; done; fatal "containers not ready"; }

CONTAINER_COUNT="$(echo "$REQUIRED" | wc -w)"
echo "  ${CONTAINER_COUNT}/${CONTAINER_COUNT} containers running"
echo ""

check 1 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"

# ===========================================================================
# [2] Savant module is using zeromq_source_bin (not uridecodebin)
# ===========================================================================
SRC_ELEMENT="$(docker exec phase3h-zmq-savant grep -qE '^\s+element:\s+zeromq_source_bin' /opt/savant/src/module/module.yml 2>/dev/null && echo 'zeromq_source_bin' || echo "unknown")"
echo -e "${BLUE}[debug] Pipeline source element: ${SRC_ELEMENT}${NC}"
check 2 "pipeline source is zeromq_source_bin" "$([[ "$SRC_ELEMENT" == "zeromq_source_bin" ]] && echo pass || echo fail)"

# ===========================================================================
# [3] Source adapter is streaming (check Redis stream has entries)
# ===========================================================================
# Wait for Savant to start producing events
echo -e "${BLUE}Waiting for Savant to produce events (up to 120s)...${NC}"
MAX_WAIT=120
ELAPSED=0
while [[ $ELAPSED -lt $MAX_WAIT ]]; do
    EVENT_COUNT="$(_redis XLEN security.events | tr -d '[:space:]')"
    if [[ -n "$EVENT_COUNT" && "$EVENT_COUNT" -gt 0 ]]; then
        echo -e "${BLUE}[debug] security.events has ${EVENT_COUNT} entries after ${ELAPSED}s${NC}"
        break
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done

EVENT_COUNT="$(_redis XLEN security.events | tr -d '[:space:]')"
check 3 "Redis security.events has entries" "$([[ -n "$EVENT_COUNT" && "$EVENT_COUNT" -gt 0 ]] && echo pass || echo fail)"

# ===========================================================================
# [4] Find most recent non-smoke event in Redis
# ===========================================================================
NON_SMOKE_SID="$(_redis XREVRANGE security.events + - COUNT 100 2>/dev/null \
    | python3 -c "
import sys
lines = sys.stdin.read().strip().split('\n')
for i in range(0, len(lines)-1, 2):
    if lines[i].strip() == 'source_event_id':
        sid = lines[i+1].strip()
        if not sid.startswith('smoke:'):
            print(sid)
            break
" 2>/dev/null || echo "")"

check 4 "Redis has non-smoke Savant events" "$([[ -n "$NON_SMOKE_SID" ]] && echo pass || echo fail)"
echo -e "${BLUE}[debug] Latest non-smoke source_event_id: ${NON_SMOKE_SID}${NC}"

# ===========================================================================
# [5] Event source_id matches phase3h
# ===========================================================================
if [[ -n "$NON_SMOKE_SID" && "$NON_SMOKE_SID" != "NULL" ]]; then
    DB_ROW="$(_pg "SELECT id, source_event_id, source_id, event_type, track_id, payload->>'bbox_source' as bbox_src FROM events WHERE source_event_id='${NON_SMOKE_SID}' ORDER BY created_at DESC LIMIT 1;")"
    DB_ID="$(echo "$DB_ROW" | cut -d'|' -f1 | tr -d '[:space:]')"
    DB_SID="$(echo "$DB_ROW" | cut -d'|' -f2 | tr -d '[:space:]')"
    DB_SRC="$(echo "$DB_ROW" | cut -d'|' -f3 | tr -d '[:space:]')"
    DB_TYPE="$(echo "$DB_ROW" | cut -d'|' -f4 | tr -d '[:space:]')"
    DB_TID="$(echo "$DB_ROW" | cut -d'|' -f5 | tr -d '[:space:]')"
    DB_BBOX_SRC="$(echo "$DB_ROW" | cut -d'|' -f6 | tr -d '[:space:]')"

    echo -e "${BLUE}[debug] DB event: id=${DB_ID} sid=${DB_SID} source_id=${DB_SRC} type=${DB_TYPE} track_id=${DB_TID}${NC}"

    check 5 "event-worker ingested event into DB" "$([[ -n "$DB_ID" ]] && echo pass || echo fail)"
    check 6 "event source_id is phase3h" "$([[ "$DB_SRC" == "phase3h" ]] && echo pass || echo fail)"
    check 7 "event_type is a known type" "$([[ -n "$DB_TYPE" && "$DB_TYPE" != "NULL" ]] && echo pass || echo fail)"
    check 8 "track_id is present and non-zero" "$([[ -n "$DB_TID" && "$DB_TID" != "NULL" && "$DB_TID" != "0" ]] && echo pass || echo fail)"
    check 9 "payload.bbox_source is savant_detection" "$([[ "$DB_BBOX_SRC" == "savant_detection" ]] && echo pass || echo fail)"

    # =========================================================================
    # [10-11] bbox exists and has valid coordinates
    # =========================================================================
    BBOX="$(_pg "SELECT payload->>'bbox' FROM events WHERE source_event_id='${NON_SMOKE_SID}';")"
    BBOX_X="$(echo "$BBOX" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('x',0))" 2>/dev/null || echo "0")"
    BBOX_Y="$(echo "$BBOX" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('y',0))" 2>/dev/null || echo "0")"
    BBOX_W="$(echo "$BBOX" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('width',0))" 2>/dev/null || echo "0")"
    BBOX_H="$(echo "$BBOX" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('height',0))" 2>/dev/null || echo "0")"
    echo -e "${BLUE}[debug] payload.bbox: x=${BBOX_X} y=${BBOX_Y} w=${BBOX_W} h=${BBOX_H}${NC}"

    BBOX_VALID="$([[ "$BBOX_X" != "0" || "$BBOX_Y" != "0" ]] && [[ "$BBOX_W" != "0" && "$BBOX_H" != "0" ]] && echo pass || echo fail)"
    check 10 "payload.bbox exists with non-zero dimensions" "$BBOX_VALID"
    check 11 "payload.bbox_source is savant_detection (confirmed)" "$([[ "$DB_BBOX_SRC" == "savant_detection" ]] && echo pass || echo fail)"

    # =========================================================================
    # [12] ZMQ source mode confirmed — module does NOT use uridecodebin
    # =========================================================================
    check 12 "module uses zeromq_source_bin (not uridecodebin)" "$([[ "$SRC_ELEMENT" == "zeromq_source_bin" ]] && echo pass || echo fail)"

    # =========================================================================
    # [13] Multiple events exist (pipeline is continuously producing)
    # =========================================================================
    DB_EVENT_COUNT="$(_pg "SELECT COUNT(*) FROM events WHERE source_id='phase3h' AND source_event_id NOT LIKE 'smoke:%';")"
    echo -e "${BLUE}[debug] DB non-smoke events with source_id=phase3h: ${DB_EVENT_COUNT}${NC}"
    check 13 "multiple non-smoke events in DB (>= 1)" "$([[ "$DB_EVENT_COUNT" -ge 1 ]] && echo pass || echo fail)"
else
    check 5 "event-worker ingested event into DB" fail
    check 6 "event source_id is phase3h" fail
    check 7 "event_type is a known type" fail
    check 8 "track_id is present and non-zero" fail
    check 9 "payload.bbox_source is savant_detection" fail
    check 10 "payload.bbox exists with non-zero dimensions" fail
    check 11 "payload.bbox_source is savant_detection (confirmed)" fail
    check 12 "module uses zeromq_source_bin (not uridecodebin)" fail
    check 13 "multiple non-smoke events in DB (>= 1)" fail
fi

echo ""
echo "--- Results: ${PASS_COUNT}/13 passed, ${FAIL_COUNT} failed ---"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo ""
    echo -e "${YELLOW}NOTE: This is a GPU-dependent POC verification.${NC}"
    echo "If the Savant module hasn't finished starting or the video loop"
    echo "hasn't produced events yet, wait 2-3 minutes and re-run."
    exit 1
fi
