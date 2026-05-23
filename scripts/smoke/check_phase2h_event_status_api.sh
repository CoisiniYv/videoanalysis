#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 2H — Event Status Handling API
# ---------------------------------------------------------------------------
# Prerequisites:
#   docker compose -f infra/docker-compose.phase2h.yml up -d --build
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase2h.yml"
API_URL="${API_URL:-http://localhost:8000}"

RED='\033[0;31m'
GREEN='\033[0;32m'
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

echo "--- Phase 2H Event Status API Smoke Test ---"
echo ""

# ===========================================================================
# 1-3. Basic checks
# ===========================================================================
[[ -f "$COMPOSE_FILE" ]] && check 1 "compose file exists" pass || check 1 "compose file exists" fail

API_STATUS="$(docker inspect phase2h-api 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")"
[[ "$API_STATUS" == "running" ]] && check 2 "API running" pass || check 2 "API status=${API_STATUS}" fail

PG_STATUS="$(docker inspect phase2h-postgres 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")"
[[ "$PG_STATUS" == "running" ]] && check 3 "Postgres running" pass || check 3 "Postgres status=${PG_STATUS}" fail

# ===========================================================================
# 4–19. Python: insert event, test all 4 endpoints, verify audit, check transitions
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
        ON CONFLICT (source_event_id) DO UPDATE SET id=events.id
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
    if row: ev_id = str(row['id'])
pg_conn.close()

checks = [(4, 'insert test event', bool(ev_id))]

# --- 5. Acknowledge ---------------------------------------------
r = client.post(f'{API}/api/v1/events/{ev_id}/acknowledge',
    json={'operator': 'smoke_op', 'comment': 'smoke test ack'})
checks.append((5, 'POST acknowledge 200', r.status_code == 200))
body = r.json()
checks.append((6, 'status == acknowledged', body.get('data', {}).get('status') == 'acknowledged'))
checks.append((7, 'media fields present', 'media' in body.get('data', {})))

# --- 6. Confirm ------------------------------------------------
r2 = client.post(f'{API}/api/v1/events/{ev_id}/confirm',
    json={'operator': 'smoke_op', 'comment': 'smoke test confirm'})
checks.append((8, 'POST confirm 200', r2.status_code == 200))
checks.append((9, 'status == confirmed', r2.json()['data']['status'] == 'confirmed'))

# --- 7. False-positive (needs fresh event) ----------------------
ev2_id = str(uuid.uuid4())
sid2 = 'smoke:phase2h:cam_01:t_h2:intrusion:2000'
pg_conn2 = psycopg.connect('postgresql://video:video@localhost:5432/video_analytics')
with pg_conn2.cursor() as cur:
    cur.execute('''
        INSERT INTO events (id, source_event_id, event_type, camera_id, source_id, track_id,
            severity, confidence, start_ts, end_ts, event_ts_ms, status, payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (source_event_id) DO UPDATE SET id=events.id
        RETURNING id
    ''', (
        ev2_id, sid2, 'intrusion', 'cam_02', 'src_h2', 't_h2',
        'low', 0.3, '2026-05-24T10:02:00+00:00', '2026-05-24T10:03:00+00:00', 120000,
        'new', json.dumps({'media': {
            'snapshot_status': 'not_implemented',
            'clip_status': 'not_implemented',
            'recording_strategy': 'reserved',
        }}),
    ))
    row2 = cur.fetchone()
    if row2: ev2_id = str(row2['id'])
pg_conn2.close()

r3 = client.post(f'{API}/api/v1/events/{ev2_id}/false-positive',
    json={'operator': 'smoke_op', 'comment': 'bird'})
checks.append((10, 'POST false-positive 200', r3.status_code == 200))
checks.append((11, 'status == false_positive', r3.json()['data']['status'] == 'false_positive'))

# --- 8. Resolve from false_positive ----------------------------
r4 = client.post(f'{API}/api/v1/events/{ev2_id}/resolve',
    json={'operator': 'smoke_op'})
checks.append((12, 'POST resolve (from false_positive) 200', r4.status_code == 200))
checks.append((13, 'status == resolved', r4.json()['data']['status'] == 'resolved'))

# --- 9. Invalid transition (resolved -> acknowledge) -----------
r5 = client.post(f'{API}/api/v1/events/{ev2_id}/acknowledge',
    json={'operator': 'smoke_op'})
checks.append((14, 'resolved -> acknowledge 409', r5.status_code == 409))

# --- 10. Invalid event_id 404 ----------------------------------
r6 = client.post(f'{API}/api/v1/events/00000000-0000-0000-0000-000000000000/acknowledge',
    json={'operator': 'smoke_op'})
checks.append((15, 'invalid event_id 404', r6.status_code == 404))

# --- 11. source_event_id lookup works --------------------------
r7 = client.post(f'{API}/api/v1/events/{sid}/acknowledge',
    json={'operator': 'smoke_op'})
# sid was already acknowledged+confirmed, so it should be 409 or 200
checks.append((16, 'source_event_id lookup', r7.status_code in (200, 409)))

# --- 12. Audit log row exists ----------------------------------
pg_conn3 = psycopg.connect('postgresql://video:video@localhost:5432/video_analytics')
with pg_conn3.cursor() as cur:
    cur.execute(\"SELECT COUNT(*) FROM audit_logs WHERE actor = 'smoke_op'\")
    count = cur.fetchone()[0]
pg_conn3.close()
checks.append((17, 'audit_logs rows for smoke_op >= 3', count >= 3))

# --- 13. updated_at changed ------------------------------------
r8 = client.get(f'{API}/api/v1/events/{ev_id}')
body8 = r8.json()
checks.append((18, 'updated_at differs from created_at',
    body8['data']['updated_at'] != body8['data']['created_at'] if
    body8['data'].get('created_at') and body8['data'].get('updated_at') else
    body8['data']['status'] != 'new'))

# --- 14. Media fields in final response -------------------------
media = body8['data'].get('media', {})
checks.append((19, 'final media snapshot_status', media.get('snapshot_status') == 'not_implemented'))
checks.append((20, 'final media recording_strategy', media.get('recording_strategy') == 'reserved'))

for n, desc, ok in checks:
    print(f'{n}|{\"pass\" if ok else \"fail\"}|{desc}')

client.close()
" 2>&1)" || true

while IFS='|' read -r num result desc; do
    if [[ -n "$num" && -n "$result" ]]; then
        check "$num" "$desc" "$result"
    fi
done <<< "$PY_OUTPUT"

echo ""
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
