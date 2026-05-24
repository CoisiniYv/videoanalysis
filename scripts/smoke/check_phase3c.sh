#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 3C — Event Snapshot MVP
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

echo "--- Phase 3C Event Snapshot MVP Smoke Test ---"
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
# Inject test event with snapshot_required=true
# ===========================================================================
UNIQ="$(date +%s%N)"
SID="smoke:phase3c:phase3b:t_snap:intrusion:${UNIQ}"
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
event = {
    "schema_version": "1.0",
    "source_event_id": "${SID}",
    "producer": "savant_phase2c",
    "gpu_id": 0,
    "event_type": "intrusion",
    "camera_id": "cam_01",
    "source_id": "phase3b",
    "track_id": "t_snap",
    "person_id": None,
    "start_ts_ms": ${EVENT_TS},
    "end_ts_ms": ${EVENT_TS},
    "event_ts_ms": ${EVENT_TS},
    "frame_id": 1,
    "frame_uuid": None,
    "keyframe_uuid": None,
    "confidence": 0.9,
    "severity": "high",
    "zone": "full_frame",
    "rule_name": "debug_intrusion",
    "description": "Phase 3C snapshot smoke test event",
    "snapshot_required": True,
    "clip_required": True,
    "payload": {"zone_id": "full_frame", "media": media},
}
print(json.dumps(event))
PYEOF
)

# ===========================================================================
# [2] Inject event
# ===========================================================================
_redis XADD security.events '*' type security_event source_event_id "${SID}" event_type intrusion camera_id cam_01 source_id phase3b track_id t_snap severity high data "${EVENT_JSON}" > /dev/null 2>&1
echo -e "${BLUE}[debug] Event injected into security.events${NC}"
check 2 "injected event into security.events" pass

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
# [4-5] Wait for clip_status=ready
# ===========================================================================
echo -e "${BLUE}Waiting for clip ready (90s)...${NC}"
CLIP_STATUS=""
for i in $(seq 1 18); do
    sleep 5
    CLIP_STATUS="$(_pg "SELECT payload->'media'->>'clip_status' FROM events WHERE source_event_id='${SID}';")"
    [[ "$CLIP_STATUS" == "ready" ]] && break
    echo -n "."
done
echo ""
check 4 "media.clip_status is ready" "$([[ "$CLIP_STATUS" == "ready" ]] && echo pass || echo fail)"

# ===========================================================================
# [5] Wait for snapshot_status=ready
# ===========================================================================
echo -e "${BLUE}Waiting for snapshot ready (60s)...${NC}"
SNAP_STATUS=""
SNAP_PATH=""
SNAP_OFFSET=""
for i in $(seq 1 12); do
    sleep 5
    SNAP_STATUS="$(_pg "SELECT payload->'media'->>'snapshot_status' FROM events WHERE source_event_id='${SID}';")"
    SNAP_PATH="$(_pg "SELECT snapshot_path FROM events WHERE source_event_id='${SID}';")"
    SNAP_OFFSET="$(_pg "SELECT payload->'media'->>'snapshot_offset_seconds' FROM events WHERE source_event_id='${SID}';")"
    [[ "$SNAP_STATUS" == "ready" && -n "$SNAP_PATH" && "$SNAP_PATH" != "NULL" ]] && break
    echo -n "."
done
echo ""
echo -e "${BLUE}[debug] snap_status=${SNAP_STATUS} snap_path=${SNAP_PATH} snap_offset=${SNAP_OFFSET}${NC}"

check 5 "snapshot_status is ready" "$([[ "$SNAP_STATUS" == "ready" ]] && echo pass || echo fail)"
check 6 "snapshot_path stored in events table" "$([[ -n "$SNAP_PATH" && "$SNAP_PATH" != "NULL" ]] && echo pass || echo fail)"

# ===========================================================================
# [7] snapshot_offset_seconds is close to pre_seconds
# ===========================================================================
SNAP_OFFSET_NUM=$(echo "$SNAP_OFFSET" | tr -d '[:space:]')
OFFSET_OK="fail"
if [[ -n "$SNAP_OFFSET_NUM" && "$SNAP_OFFSET_NUM" != "NULL" ]]; then
    # Allow small rounding differences
    OFFSET_INT=$(python3 -c "print(int(float('${SNAP_OFFSET_NUM}')))")
    if [[ "$OFFSET_INT" -eq "$PRE_SEC" ]]; then
        OFFSET_OK="pass"
    fi
fi
check 7 "snapshot_offset_seconds equals pre_seconds (${PRE_SEC})" "$OFFSET_OK"

# ===========================================================================
# [8] snapshot_url in API
# ===========================================================================
EVENT_RESP="$(_curl "${API_BASE}/api/v1/events/${SID}")"
SNAP_URL="$(echo "$EVENT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('snapshot_url','') or '')" 2>/dev/null)"
echo -e "${BLUE}[debug] snapshot_url=${SNAP_URL}${NC}"

if [[ -n "$SNAP_URL" && "$SNAP_URL" != "None" ]]; then
    SNAP_HTTP="$(_curl -o /dev/null -w '%{http_code}' "${API_BASE}${SNAP_URL}")"
    echo -e "${BLUE}[debug] curl ${API_BASE}${SNAP_URL} HTTP ${SNAP_HTTP}${NC}"
    check 8 "API returns snapshot_url and curl 200" "$([[ "$SNAP_HTTP" == "200" ]] && echo pass || echo fail)"
else
    check 8 "API returns snapshot_url" fail
fi

# ===========================================================================
# [9] snapshot file exists on filesystem
# ===========================================================================
if [[ -n "$SNAP_PATH" && "$SNAP_PATH" != "NULL" ]]; then
    SNAP_EXISTS="$(docker exec phase3b-media-worker test -f "${SNAP_PATH}" && echo yes || echo no)"
    check 9 "snapshot file exists on media volume" "$([[ "$SNAP_EXISTS" == "yes" ]] && echo pass || echo fail)"
else
    check 9 "snapshot file exists on media volume" fail
fi

# ===========================================================================
# [10] API exposes media.snapshot_status
# ===========================================================================
API_SNAP_STATUS="$(echo "$EVENT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('media',{}).get('snapshot_status',''))" 2>/dev/null)"
check 10 "API exposes media.snapshot_status=ready" "$([[ "$API_SNAP_STATUS" == "ready" ]] && echo pass || echo fail)"

# ===========================================================================
# [11] media-worker re-scan idempotent — snapshot unchanged
# ===========================================================================
docker restart phase3b-media-worker > /dev/null 2>&1 || true
sleep 20

SNAP_PATH_AFTER="$(_pg "SELECT snapshot_path FROM events WHERE source_event_id='${SID}';")"
SNAP_STATUS_AFTER="$(_pg "SELECT payload->'media'->>'snapshot_status' FROM events WHERE source_event_id='${SID}';")"
check 11 "media-worker re-scan idempotent (snapshot_path unchanged)" "$([[ "$SNAP_PATH" == "$SNAP_PATH_AFTER" ]] && echo pass || echo fail)"
check 12 "media-worker re-scan preserves snapshot_status=ready" "$([[ "$SNAP_STATUS_AFTER" == "ready" ]] && echo pass || echo fail)"

# ===========================================================================
# [13] clip_status still ready after snapshot
# ===========================================================================
CLIP_STATUS_AFTER="$(_pg "SELECT payload->'media'->>'clip_status' FROM events WHERE source_event_id='${SID}';")"
check 13 "clip_status remains ready after snapshot" "$([[ "$CLIP_STATUS_AFTER" == "ready" ]] && echo pass || echo fail)"

# ===========================================================================
# [14] snapshot_required=false -> snapshot_status=not_required
# ===========================================================================
UNIQ_NR="$(date +%s%N)"
SID_NR="smoke:phase3c:phase3b:t_nosnap:intrusion:${UNIQ_NR}"
EVENT_TS_NR="$(date +%s%3N)"

EVENT_NR_JSON=$(python3 << PYEOF
import json
media = {
    "source_id": "phase3b",
    "snapshot_required": False,
    "clip_required": True,
    "snapshot_status": "not_implemented",
    "clip_status": "not_implemented",
    "recording_strategy": "savant_replay",
    "pre_seconds": 5,
    "post_seconds": 5,
    "event_ts_ms": ${EVENT_TS_NR},
}
event = {
    "schema_version": "1.0",
    "source_event_id": "${SID_NR}",
    "producer": "savant_phase2c",
    "event_type": "intrusion",
    "camera_id": "cam_01",
    "source_id": "phase3b",
    "track_id": "t_nosnap",
    "event_ts_ms": ${EVENT_TS_NR},
    "confidence": 0.9,
    "severity": "high",
    "snapshot_required": False,
    "clip_required": True,
    "payload": {"media": media},
}
print(json.dumps(event))
PYEOF
)

_redis XADD security.events '*' type security_event source_event_id "${SID_NR}" event_type intrusion camera_id cam_01 source_id phase3b track_id t_nosnap severity high data "${EVENT_NR_JSON}" > /dev/null 2>&1

echo -e "${BLUE}Waiting for snapshot_required=false event (60s)...${NC}"
NR_SNAP_STATUS=""
for i in $(seq 1 12); do
    sleep 5
    NR_DB_ID="$(_pg "SELECT id FROM events WHERE source_event_id='${SID_NR}';")"
    if [[ -n "$NR_DB_ID" ]]; then
        NR_SNAP_STATUS="$(_pg "SELECT payload->'media'->>'snapshot_status' FROM events WHERE source_event_id='${SID_NR}';")"
        [[ "$NR_SNAP_STATUS" == "not_required" ]] && break
    fi
    echo -n "."
done
echo ""
echo -e "${BLUE}[debug] not_required snap_status=${NR_SNAP_STATUS}${NC}"
check 14 "snapshot_required=false sets snapshot_status=not_required" "$([[ "$NR_SNAP_STATUS" == "not_required" ]] && echo pass || echo fail)"

echo ""
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"
if [[ "$FAIL_COUNT" -gt 0 ]]; then exit 1; fi
