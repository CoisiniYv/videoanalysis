#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 2H — Event Status Handling API
# ---------------------------------------------------------------------------
# 前置条件（在外部终端执行）：
#   sudo docker compose -f infra/docker-compose.phase2h.yml down -v
#   sudo docker compose -f infra/docker-compose.phase2h.yml build --pull=false api event-worker
#   sudo docker compose -f infra/docker-compose.phase2h.yml up -d --no-build
#   sleep 20
# ---------------------------------------------------------------------------
# 依赖：bash, curl, docker, python3 (stdlib json only)
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase2h.yml"
API_BASE="${API_BASE:-http://127.0.0.1:8000}"
PG_CMD="docker exec phase2h-postgres psql -U video -d video_analytics -t -A"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0

# ---- curl helper: always --noproxy '*', silent, show HTTP code ----------
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

# ---- DB helpers ---------------------------------------------------------
_db_once()  { docker exec phase2h-postgres psql -U video -d video_analytics -t -A -c "$1" 2>/dev/null | tr -d '[:space:]'; }
_db_table() { docker exec phase2h-postgres psql -U video -d video_analytics -c "$1" 2>/dev/null; }

# ---- JSON helpers (stdlib only) -----------------------------------------
_json() { python3 -c "import sys,json; $1" 2>/dev/null || echo ""; }

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
echo "--- Phase 2H Event Status API Smoke Test ---"
echo ""

REQUIRED_CONTAINERS="phase2h-redis phase2h-postgres phase2h-api phase2h-event-worker"
MISSING=""
for c in $REQUIRED_CONTAINERS; do
    STATUS="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null || echo "missing")"
    [[ "$STATUS" != "running" ]] && MISSING="$MISSING $c(status=$STATUS)"
done
if [[ -n "$MISSING" ]]; then
    echo -e "${RED}容器未就绪:${NC}"
    for m in $MISSING; do echo "  - $m"; done
    echo ""
    echo -e "${YELLOW}请先启动 compose 栈:${NC}"
    echo "  sudo docker compose -f ${COMPOSE_FILE} build --pull=false api event-worker"
    echo "  sudo docker compose -f ${COMPOSE_FILE} up -d --no-build"
    echo "  sleep 20"
    fatal "容器未就绪"
fi
echo "  全部容器 running"
echo ""

# ===========================================================================
# Check 1-3: files and tables
# ===========================================================================
check 1 "compose file exists"    "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"

EVENTS_TABLE="$(_db_once "SELECT to_regclass('public.events');")"
check 2 "events table exists"    "$([[ "$EVENTS_TABLE" == "events" ]] && echo pass || echo fail)"

AUDIT_TABLE="$(_db_once "SELECT to_regclass('public.audit_logs');")"
check 3 "audit_logs table exists" "$([[ "$AUDIT_TABLE" == "audit_logs" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 4: /health
# ===========================================================================
HCODE="$(_curl -o /dev/null -w "%{http_code}" "${API_BASE}/health")"
check 4 "/health 200" "$([[ "$HCODE" == "200" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 5: /ready
# ===========================================================================
RSTATUS="$(_curl "${API_BASE}/ready" | _json "print(json.load(sys.stdin).get('status','error'))")"
check 5 "/ready status=ready" "$([[ "$RSTATUS" == "ready" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 6: /api/v1/events/recent
# ===========================================================================
RECENT="$(_curl "${API_BASE}/api/v1/events/recent?limit=5")"
check 6 "/api/v1/events/recent returns 200" "$([[ -n "$RECENT" ]] && echo pass || echo fail)"

# ===========================================================================
# Insert test event
# ===========================================================================
SID="smoke:phase2h:cam01:t_h1:intrusion:1000"
EVENT_UUID="$(uuidgen 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())")"
echo -e "${BLUE}[debug] SID=${SID}${NC}"
echo -e "${BLUE}[debug] EVENT_UUID=${EVENT_UUID}${NC}"

# Clean up any previous run's event
_db_once "DELETE FROM audit_logs WHERE payload->>'source_event_id'='${SID}';" >/dev/null || true
_db_once "DELETE FROM events WHERE source_event_id='${SID}';" >/dev/null || true

INSERT_ID="$(_db_once "
INSERT INTO events (id, source_event_id, event_type, camera_id, source_id, track_id,
    severity, confidence, start_ts, end_ts, event_ts_ms, status, payload)
VALUES ('${EVENT_UUID}', '${SID}', 'intrusion', 'cam_01', 'src_h1', 't_h1',
    'medium', 0.85, '2026-05-24T10:00:00+00:00', '2026-05-24T10:01:00+00:00', 60000,
    'new', '{\"media\":{\"snapshot_status\":\"not_implemented\",\"clip_status\":\"not_implemented\",\"recording_strategy\":\"reserved\"}}')
RETURNING id;
")"
echo -e "${BLUE}[debug] INSERT returned id=${INSERT_ID}${NC}"

if [[ -n "$INSERT_ID" ]]; then
    EVENT_UUID="$INSERT_ID"
    check 7 "insert test event (id=${EVENT_UUID})" pass
else
    check 7 "insert test event" fail
fi

DB_STATUS="$(_db_once "SELECT status FROM events WHERE source_event_id='${SID}';")"
echo -e "${BLUE}[debug] DB status after insert: ${DB_STATUS}${NC}"

# ===========================================================================
# acknowledge
# ===========================================================================
echo ""
echo -e "${BLUE}--- acknowledge ---${NC}"
ACK_URL="${API_BASE}/api/v1/events/${EVENT_UUID}/acknowledge"
echo -e "${BLUE}[debug] POST ${ACK_URL}${NC}"

ACK_RESP="$(_curl -w "\n%{http_code}" -X POST "$ACK_URL" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op","comment":"acceptance test ack"}')"

ACK_CODE="$(echo "$ACK_RESP" | tail -1)"
ACK_BODY="$(echo "$ACK_RESP" | sed '$d')"
echo -e "${BLUE}[debug] HTTP ${ACK_CODE}${NC}"
echo -e "${BLUE}[debug] body: $(echo "$ACK_BODY" | python3 -c "import sys; print(sys.stdin.read()[:200])" 2>/dev/null || echo '<parse error>')${NC}"

# Verify DB
DB_STATUS="$(_db_once "SELECT status FROM events WHERE source_event_id='${SID}';")"
echo -e "${BLUE}[debug] DB status after acknowledge: ${DB_STATUS}${NC}"

check 8 "acknowledge HTTP 200"    "$([[ "$ACK_CODE" == "200" ]] && echo pass || echo fail)"
check 9 "DB status acknowledged"  "$([[ "$DB_STATUS" == "acknowledged" ]] && echo pass || echo fail)"

# Media fields
MEDIA_SNAP="$(echo "$ACK_BODY" | _json "print(json.load(sys.stdin).get('data',{}).get('media',{}).get('snapshot_status','error'))")"
MEDIA_CLIP="$(echo "$ACK_BODY" | _json "print(json.load(sys.stdin).get('data',{}).get('media',{}).get('clip_status','error'))")"
MEDIA_REC="$(echo "$ACK_BODY" | _json "print(json.load(sys.stdin).get('data',{}).get('media',{}).get('recording_strategy','error'))")"
echo -e "${BLUE}[debug] media: snap=${MEDIA_SNAP} clip=${MEDIA_CLIP} rec=${MEDIA_REC}${NC}"
check 10 "media snapshot_status=not_implemented" "$([[ "$MEDIA_SNAP" == "not_implemented" ]] && echo pass || echo fail)"
check 11 "media recording_strategy=reserved"     "$([[ "$MEDIA_REC" == "reserved" ]] && echo pass || echo fail)"

# updated_at
CREATED="$(echo "$ACK_BODY" | _json "print(json.load(sys.stdin).get('data',{}).get('created_at',''))")"
UPDATED="$(echo "$ACK_BODY" | _json "print(json.load(sys.stdin).get('data',{}).get('updated_at',''))")"
echo -e "${BLUE}[debug] created_at=${CREATED} updated_at=${UPDATED}${NC}"
check 12 "updated_at changed" "$([[ "$CREATED" != "$UPDATED" && -n "$CREATED" && -n "$UPDATED" ]] && echo pass || echo fail)"

# ===========================================================================
# confirm
# ===========================================================================
echo ""
echo -e "${BLUE}--- confirm ---${NC}"
CONF_URL="${API_BASE}/api/v1/events/${EVENT_UUID}/confirm"
echo -e "${BLUE}[debug] POST ${CONF_URL}${NC}"

CONF_RESP="$(_curl -w "\n%{http_code}" -X POST "$CONF_URL" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op","comment":"confirmed"}')"
CONF_CODE="$(echo "$CONF_RESP" | tail -1)"
echo -e "${BLUE}[debug] HTTP ${CONF_CODE}${NC}"

DB_STATUS="$(_db_once "SELECT status FROM events WHERE source_event_id='${SID}';")"
echo -e "${BLUE}[debug] DB status after confirm: ${DB_STATUS}${NC}"

check 13 "confirm HTTP 200"    "$([[ "$CONF_CODE" == "200" ]] && echo pass || echo fail)"
check 14 "DB status confirmed" "$([[ "$DB_STATUS" == "confirmed" ]] && echo pass || echo fail)"

# ===========================================================================
# resolve
# ===========================================================================
echo ""
echo -e "${BLUE}--- resolve ---${NC}"
RESV_URL="${API_BASE}/api/v1/events/${EVENT_UUID}/resolve"
echo -e "${BLUE}[debug] POST ${RESV_URL}${NC}"

RESV_RESP="$(_curl -w "\n%{http_code}" -X POST "$RESV_URL" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op","comment":"resolved"}')"
RESV_CODE="$(echo "$RESV_RESP" | tail -1)"
echo -e "${BLUE}[debug] HTTP ${RESV_CODE}${NC}"

DB_STATUS="$(_db_once "SELECT status FROM events WHERE source_event_id='${SID}';")"
echo -e "${BLUE}[debug] DB status after resolve: ${DB_STATUS}${NC}"

check 15 "resolve HTTP 200"   "$([[ "$RESV_CODE" == "200" ]] && echo pass || echo fail)"
check 16 "DB status resolved" "$([[ "$DB_STATUS" == "resolved" ]] && echo pass || echo fail)"

# ===========================================================================
# reject: resolved -> acknowledge (expect 409)
# ===========================================================================
echo ""
echo -e "${BLUE}--- reject resolved->acknowledge ---${NC}"
REJ_URL="${API_BASE}/api/v1/events/${EVENT_UUID}/acknowledge"
echo -e "${BLUE}[debug] POST ${REJ_URL}${NC}"

REJ_RESP="$(_curl -w "\n%{http_code}" -X POST "$REJ_URL" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op"}')"
REJ_CODE="$(echo "$REJ_RESP" | tail -1)"
echo -e "${BLUE}[debug] HTTP ${REJ_CODE} (expect 409)${NC}"

DB_STATUS="$(_db_once "SELECT status FROM events WHERE source_event_id='${SID}';")"
echo -e "${BLUE}[debug] DB status after rejected request: ${DB_STATUS}${NC}"

check 17 "resolved->acknowledge 409"     "$([[ "$REJ_CODE" == "409" ]] && echo pass || echo fail)"
check 18 "status still resolved"         "$([[ "$DB_STATUS" == "resolved" ]] && echo pass || echo fail)"

# ===========================================================================
# audit_logs
# ===========================================================================
echo ""
AUDIT_ACK="$(_db_once "SELECT COUNT(*) FROM audit_logs WHERE payload->>'source_event_id'='${SID}' AND action='event.acknowledge';")"
AUDIT_CFM="$(_db_once "SELECT COUNT(*) FROM audit_logs WHERE payload->>'source_event_id'='${SID}' AND action='event.confirm';")"
AUDIT_RES="$(_db_once "SELECT COUNT(*) FROM audit_logs WHERE payload->>'source_event_id'='${SID}' AND action='event.resolve';")"
echo -e "${BLUE}[debug] audit counts: ack=${AUDIT_ACK} confirm=${AUDIT_CFM} resolve=${AUDIT_RES}${NC}"

check 19 "audit: event.acknowledge" "$([[ "$AUDIT_ACK" -ge 1 ]] && echo pass || echo fail)"
check 20 "audit: event.confirm"     "$([[ "$AUDIT_CFM" -ge 1 ]] && echo pass || echo fail)"
check 21 "audit: event.resolve"     "$([[ "$AUDIT_RES" -ge 1 ]] && echo pass || echo fail)"

# ===========================================================================
# source_event_id lookup + 404
# ===========================================================================
echo ""
SID_CODE="$(_curl -o /dev/null -w "%{http_code}" -X POST "${API_BASE}/api/v1/events/${SID}/resolve" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op"}')"
echo -e "${BLUE}[debug] source_event_id lookup HTTP ${SID_CODE} (expect 409, already resolved)${NC}"
check 22 "source_event_id lookup works (409=found)" "$([[ "$SID_CODE" == "409" ]] && echo pass || echo fail)"

NF_CODE="$(_curl -o /dev/null -w "%{http_code}" -X POST "${API_BASE}/api/v1/events/00000000-0000-0000-0000-000000000000/acknowledge" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op"}')"
check 23 "invalid event_id 404" "$([[ "$NF_CODE" == "404" ]] && echo pass || echo fail)"

# ===========================================================================
# Print audit log details
# ===========================================================================
echo ""
echo "--- Audit log details ---"
_db_table "SELECT actor, action, entity_type, payload->>'previous_status' as prev, payload->>'new_status' as new_status, payload->>'comment' as comment FROM audit_logs WHERE payload->>'source_event_id'='${SID}' ORDER BY created_at;"
echo ""

# ===========================================================================
# Summary
# ===========================================================================
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
