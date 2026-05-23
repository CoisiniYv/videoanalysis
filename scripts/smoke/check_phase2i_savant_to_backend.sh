#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 2I — Savant-to-Backend End-to-End Pipeline
# ---------------------------------------------------------------------------
# 前置条件（在外部终端执行）：
#   sudo docker compose -f infra/docker-compose.phase2i.yml down -v
#   sudo docker compose -f infra/docker-compose.phase2i.yml up -d --no-build
#   等待 90 秒让 Savant 加载模型并开始产生事件
# ---------------------------------------------------------------------------
# 依赖：bash, curl, docker, python3 (stdlib json only)
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase2i.yml"
API_BASE="${API_BASE:-http://127.0.0.1:8000}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0

_curl() { curl --noproxy '*' -s "$@"; }

check() {
    local num="$1" desc="$2" result="$3"
    if [[ "$result" == "pass" ]]; then
        echo -e "  ${GREEN}OK${NC}  [$num] $desc"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo -e "  ${RED}FAIL${NC}  [$num] $desc"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

fatal() {
    echo -e "${RED}FATAL${NC}: $*"
    exit 1
}

_pg()   { docker exec phase2i-postgres psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null | head -n1 | tr -d '[:space:]'; }
_redis(){ docker exec phase2i-redis redis-cli "$@" 2>/dev/null; }
_json() { python3 -c "import sys,json; $1" 2>/dev/null || echo ""; }

# ===========================================================================
# Pre-flight
# ===========================================================================
echo "--- Phase 2I Savant-to-Backend End-to-End Smoke Test ---"
echo ""

REQUIRED="phase2i-redis phase2i-postgres phase2i-api phase2i-event-worker phase2i-rtsp-server phase2i-ffmpeg-source phase2i-savant"
MISSING=""
for c in $REQUIRED; do
    S="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null || echo "missing")"
    [[ "$S" != "running" ]] && MISSING="$MISSING $c($S)"
done
if [[ -n "$MISSING" ]]; then
    echo -e "${RED}容器未就绪:${NC}"
    for m in $MISSING; do echo "  - $m"; done
    echo ""
    echo -e "${YELLOW}请先: sudo docker compose -f ${COMPOSE_FILE} up -d --no-build${NC}"
    fatal "容器未就绪"
fi
echo "  7/7 容器 running"
echo ""

# ===========================================================================
# Check 1-5: infrastructure
# ===========================================================================
check 1 "compose file exists"   "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"
check 2 "events table exists"   "$([[ "$(_pg "SELECT to_regclass('public.events');")" == "events" ]] && echo pass || echo fail)"
check 3 "audit_logs table"      "$([[ "$(_pg "SELECT to_regclass('public.audit_logs');")" == "audit_logs" ]] && echo pass || echo fail)"
check 4 "/health 200"           "$([[ "$(_curl -o /dev/null -w "%{http_code}" "${API_BASE}/health")" == "200" ]] && echo pass || echo fail)"
RSTATUS="$(_curl "${API_BASE}/ready" | _json "print(json.load(sys.stdin).get('status','error'))")"
check 5 "/ready"                "$([[ "$RSTATUS" == "ready" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 6-7: Wait for Savant → Redis security.events
# ===========================================================================
echo ""
echo -e "${BLUE}等待 Savant 产生 SecurityEvent → Redis security.events (最长 180s)...${NC}"
EVENT_IN_REDIS=""
for i in $(seq 1 36); do
    RAW="$(_redis XRANGE security.events - + COUNT 1 2>/dev/null || echo "")"
    if [[ -n "$RAW" && "$RAW" != *"empty"* ]]; then
        EVENT_IN_REDIS="$RAW"
        echo ""
        echo -e "${BLUE}[debug] Savant event in Redis after $((i*5))s${NC}"
        break
    fi
    sleep 5
    echo -n "."
done
echo ""

check 6 "Redis security.events has Savant event" "$([[ -n "$EVENT_IN_REDIS" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 7-8: event-worker → PostgreSQL
# ===========================================================================
echo -e "${BLUE}等待 event-worker 写入 PostgreSQL (最长 60s)...${NC}"
EVENT_ID=""
EVENT_SID=""
for i in $(seq 1 12); do
    ROW="$(_pg "SELECT id||'|'||source_event_id FROM events ORDER BY created_at DESC LIMIT 1;")"
    if [[ -n "$ROW" && "$ROW" == *"|"* ]]; then
        EVENT_ID="${ROW%%|*}"
        EVENT_SID="${ROW##*|}"
        echo ""
        echo -e "${BLUE}[debug] event in DB after $((i*5))s${NC}"
        break
    fi
    sleep 5
    echo -n "."
done
echo ""

check 7 "PostgreSQL has event" "$([[ -n "$EVENT_ID" ]] && echo pass || echo fail)"

if [[ -n "$EVENT_SID" ]]; then
    echo -e "${BLUE}[debug] EVENT_ID=${EVENT_ID}${NC}"
    echo -e "${BLUE}[debug] EVENT_SID=${EVENT_SID}${NC}"

    DB_STATUS="$(_pg "SELECT status FROM events WHERE id='${EVENT_ID}'::uuid;")"
    check 8 "event status new" "$([[ "$DB_STATUS" == "new" ]] && echo pass || echo fail)"
else
    check 8 "event status new" fail
fi

# ===========================================================================
# Check 9: API returns event
# ===========================================================================
if [[ -n "$EVENT_SID" ]]; then
    API_RESP="$(_curl "${API_BASE}/api/v1/events/${EVENT_SID}")"
    API_STATUS="$(_curl -o /dev/null -w "%{http_code}" "${API_BASE}/api/v1/events/${EVENT_SID}")"
    echo -e "${BLUE}[debug] GET /api/v1/events/${EVENT_SID} HTTP ${API_STATUS}${NC}"
    check 9 "API returns event by source_event_id" "$([[ "$API_STATUS" == "200" ]] && echo pass || echo fail)"

    # Media fields
    MEDIA_SNAP="$(echo "$API_RESP" | _json "print(json.load(sys.stdin).get('data',{}).get('media',{}).get('snapshot_status','error'))")"
    MEDIA_REC="$(echo "$API_RESP" | _json "print(json.load(sys.stdin).get('data',{}).get('media',{}).get('recording_strategy','error'))")"
    check 10 "media snapshot_status=not_implemented" "$([[ "$MEDIA_SNAP" == "not_implemented" ]] && echo pass || echo fail)"
    check 11 "media recording_strategy=reserved"     "$([[ "$MEDIA_REC" == "reserved" ]] && echo pass || echo fail)"
else
    check 9  "API returns event" fail
    check 10 "media fields" fail
    check 11 "media fields" fail
fi

# ===========================================================================
# Check 12-14: security.alerts
# ===========================================================================
echo ""
echo -e "${BLUE}检查 security.alerts...${NC}"
ALERT_RAW="$(_redis XRANGE security.alerts - + COUNT 1 2>/dev/null || echo "")"
check 12 "Redis security.alerts has alert" "$([[ -n "$ALERT_RAW" && "$ALERT_RAW" != *"empty"* ]] && echo pass || echo fail)"

# Verify alert media by extracting and parsing the data field
if [[ -n "$ALERT_RAW" && "$ALERT_RAW" != *"empty"* ]]; then
    # Use redis-cli with raw output to get just the data field
    ALERT_DATA="$(_redis XRANGE security.alerts - + COUNT 1 2>/dev/null)"
    # Extract JSON from data field via Python
    ALERT_JSON="$(echo "$ALERT_DATA" | python3 -c "
import sys
lines = [l.strip().strip('\"') for l in sys.stdin if l.strip().startswith('\"{') or l.strip().startswith('{')]
print(lines[0] if lines else '{}')
" 2>/dev/null || echo "{}")"
    MEDIA_SNAP="$(echo "$ALERT_JSON" | _json "print(json.load(sys.stdin).get('media',{}).get('snapshot_status','error'))")"
    MEDIA_REC="$(echo "$ALERT_JSON" | _json "print(json.load(sys.stdin).get('media',{}).get('recording_strategy','error'))")"
    check 13 "alert media snapshot_status" "$([[ "$MEDIA_SNAP" == "not_implemented" ]] && echo pass || echo fail)"
    check 14 "alert media recording_strategy" "$([[ "$MEDIA_REC" == "reserved" ]] && echo pass || echo fail)"
else
    check 13 "alert media" fail
    check 14 "alert media" fail
fi

# ===========================================================================
# Check 15: idempotency
# ===========================================================================
if [[ -n "$EVENT_SID" ]]; then
    BEFORE="$(_pg "SELECT COUNT(*) FROM events WHERE source_event_id='${EVENT_SID}';")"
    echo -e "${BLUE}[debug] idempotency: before=${BEFORE}${NC}"

    # Push duplicate event
    SIM="{\"source_event_id\":\"${EVENT_SID}\",\"event_type\":\"intrusion\",\"camera_id\":\"cam_01\",\"track_id\":\"t_dup\",\"start_ts_ms\":1,\"end_ts_ms\":1,\"event_ts_ms\":1,\"source_id\":\"src\",\"payload\":{\"media\":{\"snapshot_status\":\"not_implemented\",\"clip_status\":\"not_implemented\",\"recording_strategy\":\"reserved\"}}}"
    _redis XADD security.events '*' type security_event source_event_id "${EVENT_SID}" event_type intrusion camera_id cam_01 data "${SIM}" >/dev/null 2>&1 || true

    sleep 10
    AFTER="$(_pg "SELECT COUNT(*) FROM events WHERE source_event_id='${EVENT_SID}';")"
    echo -e "${BLUE}[debug] idempotency: after=${AFTER}${NC}"
    check 15 "idempotent: count=1" "$([[ "${BEFORE:-0}" -eq 1 && "${AFTER:-0}" -eq 1 ]] && echo pass || echo fail)"
else
    check 15 "idempotent" fail
fi

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
