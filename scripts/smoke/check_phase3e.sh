#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 3E — Annotated Snapshot MVP
# ---------------------------------------------------------------------------
# PREREQ: docker compose -f infra/docker-compose.phase3b.yml up -d
# ---------------------------------------------------------------------------
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase3b.yml"
API_BASE="${API_BASE:-http://127.0.0.1:8001}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
PASS_COUNT=0; FAIL_COUNT=0

_curl() { curl --noproxy '*' -s --connect-timeout 5 "$@"; }
_pg()   { docker exec phase3b-postgres psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null | head -n1 | tr -d '[:space:]'; }
_redis(){ docker exec phase3b-redis redis-cli "$@" 2>/dev/null; }

check() {
    local num="$1" desc="$2" result="$3"
    if [[ "$result" == "pass" ]]; then
        echo -e "  ${GREEN}OK${NC}  [$num] $desc"; PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo -e "  ${RED}FAIL${NC}  [$num] $desc"; FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}
fatal() { echo -e "${RED}FATAL${NC}: $*"; exit 1; }

echo "--- Phase 3E Annotated Snapshot MVP Smoke Test ---"
echo ""

# Pre-flight
REQUIRED="phase3b-redis phase3b-postgres phase3b-api phase3b-event-worker phase3b-clip-worker phase3b-media-worker"
MISSING=""
for c in $REQUIRED; do
    S="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null || echo "missing")"
    [[ "$S" != "running" ]] && MISSING="$MISSING $c($S)"
done
[[ -n "$MISSING" ]] && { echo -e "${RED}Containers not ready:${NC}"; for m in $MISSING; do echo "  - $m"; done; fatal "containers not ready"; }
echo "  6/6 containers running"
echo ""

check 1 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"

# ===========================================================================
# Inject test event with bbox data and snapshot_required=true
# ===========================================================================
UNIQ="$(date +%s%N)"
SID="smoke:phase3e:phase3b:t_ann:intrusion:${UNIQ}"
EVENT_TS="$(date +%s%3N)"
PRE_SEC=5

echo -e "${BLUE}[debug] SID=${SID} ts=${EVENT_TS} pre_seconds=${PRE_SEC}${NC}"

EVENT_JSON=$(python3 << PYEOF
import json
media = {
    "source_id": "phase3b",
    "snapshot_required": True,
    "clip_required": True,
    "snapshot_status": "not_implemented",
    "clip_status": "not_implemented",
    "recording_strategy": "savant_replay",
    "pre_seconds": ${PRE_SEC},
    "post_seconds": 5,
    "event_ts_ms": ${EVENT_TS},
}
payload = {
    "zone_id": "perimeter",
    "bbox": {"x": 120, "y": 80, "width": 300, "height": 220},
    "media": media,
}
event = {
    "schema_version": "1.0",
    "source_event_id": "${SID}",
    "producer": "savant_phase2c",
    "event_type": "intrusion",
    "camera_id": "cam_01",
    "source_id": "phase3b",
    "track_id": "t_ann",
    "event_ts_ms": ${EVENT_TS},
    "confidence": 0.92,
    "severity": "high",
    "zone": "perimeter",
    "snapshot_required": True,
    "clip_required": True,
    "payload": payload,
}
print(json.dumps(event))
PYEOF
)

# ===========================================================================
# [2] Inject event
# ===========================================================================
_redis XADD security.events '*' type security_event source_event_id "${SID}" event_type intrusion camera_id cam_01 source_id phase3b track_id t_ann severity high data "${EVENT_JSON}" > /dev/null 2>&1
echo -e "${BLUE}[debug] Event injected${NC}"
check 2 "injected event with bbox into security.events" pass

# ===========================================================================
# [3] Wait for event in DB
# ===========================================================================
echo -e "${BLUE}Waiting for event-worker (30s)...${NC}"
DB_ID=""
for i in $(seq 1 6); do
    sleep 5
    DB_ID="$(_pg "SELECT id FROM events WHERE source_event_id='${SID}';")"
    [[ -n "$DB_ID" ]] && break
    echo -n "."
done
echo ""
check 3 "event in PostgreSQL" "$([[ -n "$DB_ID" ]] && echo pass || echo fail)"

# ===========================================================================
# [4-5] Wait for snapshot_status=ready (raw snapshot)
# ===========================================================================
echo -e "${BLUE}Waiting for raw snapshot ready (90s)...${NC}"
SNAP_STATUS=""
SNAP_PATH=""
for i in $(seq 1 18); do
    sleep 5
    SNAP_STATUS="$(_pg "SELECT payload->'media'->>'snapshot_status' FROM events WHERE source_event_id='${SID}';")"
    SNAP_PATH="$(_pg "SELECT snapshot_path FROM events WHERE source_event_id='${SID}';")"
    [[ "$SNAP_STATUS" == "ready" && -n "$SNAP_PATH" && "$SNAP_PATH" != "NULL" ]] && break
    echo -n "."
done
echo ""
echo -e "${BLUE}[debug] raw_snap_status=${SNAP_STATUS} snap_path=${SNAP_PATH}${NC}"

check 4 "raw snapshot_status is ready" "$([[ "$SNAP_STATUS" == "ready" ]] && echo pass || echo fail)"
check 5 "raw snapshot_path stored" "$([[ -n "$SNAP_PATH" && "$SNAP_PATH" != "NULL" ]] && echo pass || echo fail)"

# ===========================================================================
# [6-7] Wait for annotated_snapshot_status=ready
# ===========================================================================
echo -e "${BLUE}Waiting for annotated snapshot ready (30s)...${NC}"
ANN_STATUS=""
ANN_PATH=""
ZONE_OVERLAY=""
for i in $(seq 1 6); do
    sleep 5
    ANN_STATUS="$(_pg "SELECT payload->'media'->>'annotated_snapshot_status' FROM events WHERE source_event_id='${SID}';")"
    ANN_PATH="$(_pg "SELECT payload->'media'->>'annotated_snapshot_path' FROM events WHERE source_event_id='${SID}';")"
    ZONE_OVERLAY="$(_pg "SELECT payload->'media'->>'zone_overlay_status' FROM events WHERE source_event_id='${SID}';")"
    [[ "$ANN_STATUS" == "ready" && -n "$ANN_PATH" && "$ANN_PATH" != "NULL" ]] && break
    echo -n "."
done
echo ""
echo -e "${BLUE}[debug] ann_status=${ANN_STATUS} ann_path=${ANN_PATH} zone_overlay=${ZONE_OVERLAY}${NC}"

check 6 "annotated_snapshot_status is ready" "$([[ "$ANN_STATUS" == "ready" ]] && echo pass || echo fail)"
check 7 "annotated_snapshot_path stored in payload.media" "$([[ -n "$ANN_PATH" && "$ANN_PATH" != "NULL" ]] && echo pass || echo fail)"

# ===========================================================================
# [8] zone_overlay_status present
# ===========================================================================
check 8 "zone_overlay_status is skipped_missing_polygon" "$([[ "$ZONE_OVERLAY" == "skipped_missing_polygon" ]] && echo pass || echo fail)"

# ===========================================================================
# [9] API returns annotated_snapshot_url → curl HTTP 200
# ===========================================================================
EVENT_RESP="$(_curl "${API_BASE}/api/v1/events/${SID}")"
ANN_URL="$(echo "$EVENT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('annotated_snapshot_url','') or '')" 2>/dev/null)"
echo -e "${BLUE}[debug] annotated_snapshot_url=${ANN_URL}${NC}"

if [[ -n "$ANN_URL" && "$ANN_URL" != "None" ]]; then
    ANN_HTTP="$(_curl -o /dev/null -w '%{http_code}' "${API_BASE}${ANN_URL}")"
    echo -e "${BLUE}[debug] curl ${API_BASE}${ANN_URL} HTTP ${ANN_HTTP}${NC}"
    check 9 "API returns annotated_snapshot_url and curl 200" "$([[ "$ANN_HTTP" == "200" ]] && echo pass || echo fail)"
else
    check 9 "API returns annotated_snapshot_url" fail
fi

# ===========================================================================
# [10] Annotated file exists on filesystem
# ===========================================================================
if [[ -n "$ANN_PATH" && "$ANN_PATH" != "NULL" ]]; then
    ANN_FILE_EXISTS="$(docker exec phase3b-media-worker test -f "${ANN_PATH}" && echo yes || echo no)"
    check 10 "annotated snapshot file exists on media volume" "$([[ "$ANN_FILE_EXISTS" == "yes" ]] && echo pass || echo fail)"
else
    check 10 "annotated snapshot file exists on media volume" fail
fi

# ===========================================================================
# [11] Raw snapshot and clip_status unchanged after annotation
# ===========================================================================
CLIP_STATUS_AFTER="$(_pg "SELECT payload->'media'->>'clip_status' FROM events WHERE source_event_id='${SID}';")"
SNAP_STATUS_AFTER="$(_pg "SELECT payload->'media'->>'snapshot_status' FROM events WHERE source_event_id='${SID}';")"
check 11 "clip_status remains ready after annotation" "$([[ "$CLIP_STATUS_AFTER" == "ready" ]] && echo pass || echo fail)"
check 12 "snapshot_status remains ready after annotation" "$([[ "$SNAP_STATUS_AFTER" == "ready" ]] && echo pass || echo fail)"

# ===========================================================================
# [13] Media-worker re-scan idempotent — annotation unchanged
# ===========================================================================
docker restart phase3b-media-worker > /dev/null 2>&1 || true
sleep 20

ANN_STATUS_AFTER="$(_pg "SELECT payload->'media'->>'annotated_snapshot_status' FROM events WHERE source_event_id='${SID}';")"
ANN_PATH_AFTER="$(_pg "SELECT payload->'media'->>'annotated_snapshot_path' FROM events WHERE source_event_id='${SID}';")"
check 13 "media-worker re-scan idempotent (annotated_snapshot_path unchanged)" "$([[ "$ANN_PATH" == "$ANN_PATH_AFTER" ]] && echo pass || echo fail)"
check 14 "media-worker re-scan preserves annotated_snapshot_status=ready" "$([[ "$ANN_STATUS_AFTER" == "ready" ]] && echo pass || echo fail)"

echo ""
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"
if [[ "$FAIL_COUNT" -gt 0 ]]; then exit 1; fi
