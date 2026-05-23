#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 2H — Event Status Handling API
# ---------------------------------------------------------------------------
# 部署前置步骤（必须由用户在外部终端执行）：
#
#   # 第一步：构建镜像（仅第一次，或代码变更后）
#   sudo docker compose -f infra/docker-compose.phase2h.yml build --pull=false
#
#   # 第二步：启动 compose 栈
#   sudo docker compose -f infra/docker-compose.phase2h.yml up -d --no-build
#
#   # 第三步：等待 healthy（约 20 秒）
#   sleep 20
#
#   # 第四步：运行本 smoke
#   sudo bash scripts/smoke/check_phase2h_event_status_api.sh
#
# 注意：
#   - 本脚本不会自动执行 docker pull 或 docker compose build
#   - 本脚本不会自动启动容器
#   - 如果容器不存在或未运行，本脚本会明确提示并退出
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase2h.yml"
API_URL="${API_URL:-http://localhost:8000}"

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
MISSING_CONTAINERS=""

for c in $REQUIRED_CONTAINERS; do
    STATUS="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")"
    if [[ "$STATUS" != "running" ]]; then
        MISSING_CONTAINERS="$MISSING_CONTAINERS $c(status=$STATUS)"
    fi
done

if [[ -n "$MISSING_CONTAINERS" ]]; then
    echo -e "${RED}容器未就绪:${NC}"
    for m in $MISSING_CONTAINERS; do
        echo "  - $m"
    done
    echo ""
    echo -e "${YELLOW}请先启动 compose 栈:${NC}"
    echo ""
    echo "  # 如果镜像不存在，请先 build:"
    echo "  sudo docker compose -f ${COMPOSE_FILE} build --pull=false"
    echo ""
    echo "  # 启动:"
    echo "  sudo docker compose -f ${COMPOSE_FILE} up -d --no-build"
    echo ""
    echo "  # 等待 healthy:"
    echo "  sleep 20"
    echo ""
    fatal "容器未就绪，无法继续 smoke 测试"
fi

echo "  所有容器 running, 可以继续 smoke 测试"
echo ""

# ===========================================================================
# 1. Compose file exists
# ===========================================================================
[[ -f "$COMPOSE_FILE" ]] && check 1 "compose file exists" pass || check 1 "compose file exists" fail

# ===========================================================================
# 2. events table exists
# ===========================================================================
TABLE_CHECK="$(docker exec phase2h-postgres psql -U video -d video_analytics -t -c "SELECT to_regclass('public.events');" 2>/dev/null | tr -d '[:space:]' || echo "")"
[[ "$TABLE_CHECK" == "events" ]] && check 2 "events table exists" pass || check 2 "events table exists (got: ${TABLE_CHECK})" fail

# ===========================================================================
# 3. audit_logs table exists
# ===========================================================================
AUDIT_TABLE="$(docker exec phase2h-postgres psql -U video -d video_analytics -t -c "SELECT to_regclass('public.audit_logs');" 2>/dev/null | tr -d '[:space:]' || echo "")"
[[ "$AUDIT_TABLE" == "audit_logs" ]] && check 3 "audit_logs table exists" pass || check 3 "audit_logs table exists (got: ${AUDIT_TABLE})" fail

# ===========================================================================
# 4. /health
# ===========================================================================
HEALTH_RESP="$(curl -s -o /dev/null -w "%{http_code}" "${API_URL}/health" 2>/dev/null || echo "000")"
[[ "$HEALTH_RESP" == "200" ]] && check 4 "/health returns 200" pass || check 4 "/health returns ${HEALTH_RESP}" fail

# ===========================================================================
# 5. /ready
# ===========================================================================
READY_RESP="$(curl -s "${API_URL}/ready" 2>/dev/null || echo '{}')"
READY_STATUS="$(echo "$READY_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")"
[[ "$READY_STATUS" == "ready" ]] && check 5 "/ready status=ready" pass || check 5 "/ready status=${READY_STATUS}" fail

# ===========================================================================
# 6–22. Python: insert event, test all 4 endpoints, verify audit, check transitions
# ===========================================================================
PY_OUTPUT="$(python3 -c "
import json, sys, uuid
import psycopg
import httpx

API = '${API_URL}'
pg_conn = psycopg.connect('postgresql://video:video@localhost:5432/video_analytics')
client = httpx.Client(timeout=10)

# --- Insert test event ------------------------------------------
ev_id = str(uuid.uuid4())
sid = 'smoke:phase2h:cam_01:t_h1:intrusion:1000'
with pg_conn.cursor() as cur:
    cur.execute('''
        INSERT INTO events (id, source_event_id, event_type, camera_id, source_id, track_id,
            severity, confidence, start_ts, end_ts, event_ts_ms, status, payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (source_event_id) DO NOTHING
        RETURNING id
    ''', (
        ev_id, sid, 'intrusion', 'cam_01', 'src_h1', 't_h1',
        'medium', 0.85, '2026-05-24T10:00:00+00:00', '2026-05-24T10:01:00+00:00', 60000,
        'new', json.dumps({'media': {
            'snapshot_status': 'not_implemented',
            'clip_status': 'not_implemented',
            'recording_strategy': 'reserved',
        }}),
    ))
    row = cur.fetchone()
    if row:
        ev_id = str(row['id'])
        print(f'6|pass|inserted event id={ev_id}')
    else:
        print(f'6|fail|insert event failed (duplicate?)')

# --- 7. /api/v1/events/recent ----------------------------------
r = client.get(f'{API}/api/v1/events/recent?limit=5')
body = r.json()
events = body.get('data', {}).get('events', [])
check = (7, '/api/v1/events/recent returns events', len(events) >= 1)
print(f'{check[0]}|{\"pass\" if check[2] else \"fail\"}|{check[1]}')

# --- 8. POST acknowledge ---------------------------------------
r2 = client.post(f'{API}/api/v1/events/{ev_id}/acknowledge',
    json={'operator': 'smoke_op', 'comment': 'acceptance test ack'})
body2 = r2.json()
ack_ok = r2.status_code == 200 and body2.get('data', {}).get('status') == 'acknowledged'
print(f'8|{\"pass\" if ack_ok else \"fail\"}|POST acknowledge -> 200 + status=acknowledged')

media2 = body2.get('data', {}).get('media', {})
media_ok = (
    media2.get('snapshot_status') == 'not_implemented' and
    media2.get('clip_status') == 'not_implemented' and
    media2.get('recording_strategy') == 'reserved'
)
print(f'9|{\"pass\" if media_ok else \"fail\"}|acknowledge response includes media fields')

# --- 9. updated_at changed -------------------------------------
created = body2.get('data', {}).get('created_at', '')
updated = body2.get('data', {}).get('updated_at', '')
print(f'10|{\"pass\" if created != updated else \"fail\"}|updated_at changed after acknowledge')

# --- 10. POST confirm ------------------------------------------
r3 = client.post(f'{API}/api/v1/events/{ev_id}/confirm',
    json={'operator': 'smoke_op', 'comment': 'acceptance test confirm'})
conf_ok = r3.status_code == 200 and r3.json().get('data', {}).get('status') == 'confirmed'
print(f'11|{\"pass\" if conf_ok else \"fail\"}|POST confirm -> 200 + status=confirmed')

# --- 11. POST resolve ------------------------------------------
r4 = client.post(f'{API}/api/v1/events/{ev_id}/resolve',
    json={'operator': 'smoke_op', 'comment': 'acceptance test resolve'})
res_ok = r4.status_code == 200 and r4.json().get('data', {}).get('status') == 'resolved'
print(f'12|{\"pass\" if res_ok else \"fail\"}|POST resolve -> 200 + status=resolved')

# --- 12. Resolved -> acknowledge 409 ---------------------------
r5 = client.post(f'{API}/api/v1/events/{ev_id}/acknowledge',
    json={'operator': 'smoke_op'})
conflict_ok = r5.status_code == 409
body5 = r5.json()
err_msg = body5.get('error', {}).get('message', '')
print(f'13|{\"pass\" if conflict_ok else \"fail\"}|resolved -> acknowledge 409 (got {r5.status_code})')
print(f'14|{\"pass\" if \"Invalid transition\" in err_msg else \"fail\"}|409 body contains \"Invalid transition\"')

# --- 13. Status still resolved after rejected request ----------
r6 = client.get(f'{API}/api/v1/events/{ev_id}')
still_resolved = r6.json().get('data', {}).get('status') == 'resolved'
print(f'15|{\"pass\" if still_resolved else \"fail\"}|status still resolved after rejected acknowledge')

# --- 14. source_event_id lookup --------------------------------
r7 = client.post(f'{API}/api/v1/events/{sid}/resolve',
    json={'operator': 'smoke_op'})
# Already resolved, expect 409
print(f'16|{\"pass\" if r7.status_code == 409 else \"fail\"}|source_event_id lookup works (got {r7.status_code})')

# --- 15. Invalid event_id 404 ----------------------------------
r8 = client.post(f'{API}/api/v1/events/00000000-0000-0000-0000-000000000000/acknowledge',
    json={'operator': 'smoke_op'})
print(f'17|{\"pass\" if r8.status_code == 404 else \"fail\"}|invalid event_id 404 (got {r8.status_code})')

# --- 16. Audit log rows exist ----------------------------------
with pg_conn.cursor() as cur:
    cur.execute(\"SELECT actor, action, entity_type, payload FROM audit_logs WHERE actor = 'smoke_op' ORDER BY created_at\")
    rows = cur.fetchall()
pg_conn.close()

audit_count = len(rows)
print(f'18|{\"pass\" if audit_count >= 3 else \"fail\"}|audit_logs rows >= 3 (got {audit_count})')

# Print audit details for debugging
for i, r in enumerate(rows):
    payload = r[3] if isinstance(r[3], dict) else json.loads(r[3])
    print(f'# audit[{i}]: actor={r[0]} action={r[1]} entity={r[2]} prev={payload.get(\"previous_status\",\"?\")} new={payload.get(\"new_status\",\"?\")}')

client.close()
" 2>&1)" || true

while IFS='|' read -r num result desc; do
    if [[ -n "$num" && -n "$result" ]]; then
        # Lines starting with # are debug output, skip them
        if [[ "$num" == "#"* ]]; then
            echo "  $num"
        else
            check "$num" "$desc" "$result"
        fi
    elif [[ -n "$num" ]]; then
        # Debug output without pipe separator
        echo "  $num"
    fi
done <<< "$PY_OUTPUT"

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
