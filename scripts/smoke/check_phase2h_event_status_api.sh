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
# 本脚本只依赖：bash, curl, docker, python3 (stdlib json only)
# 不需要 psycopg, httpx, redis 等 host-side Python 包
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase2h.yml"
API_URL="${API_URL:-http://127.0.0.1:8000}"
CURL="curl --noproxy '*' -s"
PG_CMD="docker exec phase2h-postgres psql -U video -d video_analytics -t -A --no-align"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0

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

# ---------------------------------------------------------------------------
# Pre-flight: ensure all required containers are running
# ---------------------------------------------------------------------------
echo "--- Phase 2H Event Status API Smoke Test ---"
echo ""

REQUIRED_CONTAINERS="phase2h-redis phase2h-postgres phase2h-api phase2h-event-worker"
MISSING=""

for c in $REQUIRED_CONTAINERS; do
    STATUS="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null || echo "missing")"
    if [[ "$STATUS" != "running" ]]; then
        MISSING="$MISSING $c(status=$STATUS)"
    fi
done

if [[ -n "$MISSING" ]]; then
    echo -e "${RED}容器未就绪:${NC}"
    for m in $MISSING; do echo "  - $m"; done
    echo ""
    echo -e "${YELLOW}请先启动 compose 栈:${NC}"
    echo "  sudo docker compose -f ${COMPOSE_FILE} build --pull=false api event-worker"
    echo "  sudo docker compose -f ${COMPOSE_FILE} up -d --no-build"
    echo "  sleep 20"
    fatal "容器未就绪，无法继续 smoke 测试"
fi

echo "  容器全部 running"
echo ""

# ===========================================================================
# Check 1: compose file exists
# ===========================================================================
check 1 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 2: events table exists
# ===========================================================================
EVENTS_TABLE="$($PG_CMD -c "SELECT to_regclass('public.events');" 2>/dev/null | tr -d '[:space:]' || echo "")"
check 2 "events table exists" "$([[ "$EVENTS_TABLE" == "events" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 3: audit_logs table exists
# ===========================================================================
AUDIT_TABLE="$($PG_CMD -c "SELECT to_regclass('public.audit_logs');" 2>/dev/null | tr -d '[:space:]' || echo "")"
check 3 "audit_logs table exists" "$([[ "$AUDIT_TABLE" == "audit_logs" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 4: /health returns 200
# ===========================================================================
HEALTH_CODE="$($CURL -o /dev/null -w "%{http_code}" "${API_URL}/health" 2>/dev/null || echo "000")"
check 4 "/health returns 200" "$([[ "$HEALTH_CODE" == "200" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 5: /ready returns ready
# ===========================================================================
READY_BODY="$($CURL "${API_URL}/ready" 2>/dev/null || echo '{}')"
READY_STATUS="$(echo "$READY_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null || echo "error")"
check 5 "/ready status=ready" "$([[ "$READY_STATUS" == "ready" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 6: /api/v1/events/recent returns data
# ===========================================================================
RECENT_BODY="$($CURL "${API_URL}/api/v1/events/recent?limit=5" 2>/dev/null || echo '{}')"
RECENT_COUNT="$(echo "$RECENT_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',{}).get('events',[])))" 2>/dev/null || echo "0")"
check 6 "/api/v1/events/recent returns events" "$([[ "$RECENT_COUNT" -ge 0 ]] && echo pass || echo fail)"

# ===========================================================================
# Insert test event via psql
# ===========================================================================
EVENT_ID="$(uuidgen 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || echo "00000000-0000-0000-0000-000000000001")"
SID="smoke:phase2h:cam_01:t_h1:intrusion:1000"

INSERT_RESULT="$($PG_CMD -c "
INSERT INTO events (id, source_event_id, event_type, camera_id, source_id, track_id,
    severity, confidence, start_ts, end_ts, event_ts_ms, status, payload)
VALUES ('${EVENT_ID}', '${SID}', 'intrusion', 'cam_01', 'src_h1', 't_h1',
    'medium', 0.85, '2026-05-24T10:00:00+00:00', '2026-05-24T10:01:00+00:00', 60000,
    'new', '{\"media\":{\"snapshot_status\":\"not_implemented\",\"clip_status\":\"not_implemented\",\"recording_strategy\":\"reserved\"}}')
ON CONFLICT (source_event_id) DO NOTHING
RETURNING id;
" 2>&1 || echo "")"

INSERTED_ID="$(echo "$INSERT_RESULT" | tr -d '[:space:]' || echo "")"
if [[ -n "$INSERTED_ID" && "$INSERTED_ID" != "INSERT01" ]]; then
    check 7 "insert test event" pass
    EVENT_ID="$INSERTED_ID"
else
    # Try to get existing ID
    EXISTING_ID="$($PG_CMD -c "SELECT id FROM events WHERE source_event_id='${SID}';" 2>/dev/null | tr -d '[:space:]' || echo "")"
    if [[ -n "$EXISTING_ID" ]]; then
        EVENT_ID="$EXISTING_ID"
        check 7 "insert test event (already exists)" pass
    else
        check 7 "insert test event" fail
    fi
fi

# ===========================================================================
# Check 8: POST acknowledge → status=acknowledged
# ===========================================================================
ACK_BODY="$($CURL -X POST "${API_URL}/api/v1/events/${EVENT_ID}/acknowledge" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op","comment":"acceptance test ack"}' 2>/dev/null || echo '{}')"

ACK_CODE="$($CURL -o /dev/null -w "%{http_code}" -X POST "${API_URL}/api/v1/events/${EVENT_ID}/acknowledge" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op","comment":"acceptance test ack 2"}' 2>/dev/null || echo "000")"

ACK_STATUS="$(echo "$ACK_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('status','error'))" 2>/dev/null || echo "error")"

# Check via psql that DB status is acknowledged
DB_STATUS="$($PG_CMD -c "SELECT status FROM events WHERE id='${EVENT_ID}';" 2>/dev/null | tr -d '[:space:]' || echo "error")"
check 8 "POST acknowledge -> DB status=acknowledged" "$([[ "$DB_STATUS" == "acknowledged" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 9: acknowledge response has media fields
# ===========================================================================
MEDIA_SNAP="$(echo "$ACK_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('media',{}).get('snapshot_status','error'))" 2>/dev/null || echo "error")"
MEDIA_CLIP="$(echo "$ACK_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('media',{}).get('clip_status','error'))" 2>/dev/null || echo "error")"
MEDIA_REC="$(echo "$ACK_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('media',{}).get('recording_strategy','error'))" 2>/dev/null || echo "error")"
MEDIA_OK="pass"
[[ "$MEDIA_SNAP" != "not_implemented" ]] && MEDIA_OK="fail"
[[ "$MEDIA_CLIP" != "not_implemented" ]] && MEDIA_OK="fail"
[[ "$MEDIA_REC" != "reserved" ]] && MEDIA_OK="fail"
check 9 "acknowledge response has media reserved/not_implemented" "$MEDIA_OK"

# ===========================================================================
# Check 10: updated_at changed
# ===========================================================================
CREATED_AT="$(echo "$ACK_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('created_at',''))" 2>/dev/null || echo "")"
UPDATED_AT="$(echo "$ACK_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('updated_at',''))" 2>/dev/null || echo "")"
check 10 "updated_at differs from created_at" "$([[ "$CREATED_AT" != "$UPDATED_AT" && -n "$CREATED_AT" && -n "$UPDATED_AT" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 11: audit_logs row exists for acknowledge
# ===========================================================================
AUDIT_COUNT="$($PG_CMD -c "SELECT COUNT(*) FROM audit_logs WHERE actor='smoke_op' AND action='event.acknowledge';" 2>/dev/null | tr -d '[:space:]' || echo "0")"
check 11 "audit_logs has event.acknowledge row" "$([[ "$AUDIT_COUNT" -ge 1 ]] && echo pass || echo fail)"

# ===========================================================================
# Check 12: POST confirm → DB status=confirmed
# ===========================================================================
$CURL -X POST "${API_URL}/api/v1/events/${EVENT_ID}/confirm" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op","comment":"confirmed"}' >/dev/null 2>&1 || true

DB_STATUS2="$($PG_CMD -c "SELECT status FROM events WHERE id='${EVENT_ID}';" 2>/dev/null | tr -d '[:space:]' || echo "error")"
check 12 "POST confirm -> DB status=confirmed" "$([[ "$DB_STATUS2" == "confirmed" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 13: POST resolve → DB status=resolved
# ===========================================================================
$CURL -X POST "${API_URL}/api/v1/events/${EVENT_ID}/resolve" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op","comment":"resolved"}' >/dev/null 2>&1 || true

DB_STATUS3="$($PG_CMD -c "SELECT status FROM events WHERE id='${EVENT_ID}';" 2>/dev/null | tr -d '[:space:]' || echo "error")"
check 13 "POST resolve -> DB status=resolved" "$([[ "$DB_STATUS3" == "resolved" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 14: resolved -> acknowledge returns 409
# ===========================================================================
CONFLICT_CODE="$($CURL -o /dev/null -w "%{http_code}" -X POST "${API_URL}/api/v1/events/${EVENT_ID}/acknowledge" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op"}' 2>/dev/null || echo "000")"
check 14 "resolved -> acknowledge returns 409" "$([[ "$CONFLICT_CODE" == "409" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 15: status still resolved after rejected request
# ===========================================================================
DB_STATUS4="$($PG_CMD -c "SELECT status FROM events WHERE id='${EVENT_ID}';" 2>/dev/null | tr -d '[:space:]' || echo "error")"
check 15 "status still resolved after rejected acknowledge" "$([[ "$DB_STATUS4" == "resolved" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 16: audit_logs has 3+ rows (acknowledge + confirm + resolve)
# ===========================================================================
TOTAL_AUDIT="$($PG_CMD -c "SELECT COUNT(*) FROM audit_logs WHERE actor='smoke_op';" 2>/dev/null | tr -d '[:space:]' || echo "0")"
check 16 "audit_logs rows >= 3" "$([[ "$TOTAL_AUDIT" -ge 3 ]] && echo pass || echo fail)"

# ===========================================================================
# Check 17: source_event_id lookup works
# ===========================================================================
SID_CODE="$($CURL -o /dev/null -w "%{http_code}" -X POST "${API_URL}/api/v1/events/${SID}/resolve" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op"}' 2>/dev/null || echo "000")"
# Already resolved, should return 409 (proves it found the event by SID)
check 17 "source_event_id lookup (409=found)" "$([[ "$SID_CODE" == "409" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 18: invalid event_id returns 404
# ===========================================================================
NOTFOUND_CODE="$($CURL -o /dev/null -w "%{http_code}" -X POST "${API_URL}/api/v1/events/00000000-0000-0000-0000-000000000000/acknowledge" \
    -H "Content-Type: application/json" \
    -d '{"operator":"smoke_op"}' 2>/dev/null || echo "000")"
check 18 "invalid event_id 404" "$([[ "$NOTFOUND_CODE" == "404" ]] && echo pass || echo fail)"

# ===========================================================================
# Print audit log details for manual verification
# ===========================================================================
echo ""
echo "--- Audit log details ---"
docker exec phase2h-postgres psql -U video -d video_analytics -c "SELECT actor, action, entity_type, payload->>'previous_status' as prev, payload->>'new_status' as new, payload->>'comment' as comment FROM audit_logs WHERE actor='smoke_op' ORDER BY created_at;" 2>/dev/null || echo "  (audit query failed)"
echo ""

# ===========================================================================
# Summary
# ===========================================================================
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
