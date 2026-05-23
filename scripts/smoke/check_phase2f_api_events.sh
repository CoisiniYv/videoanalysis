#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 2F — FastAPI Events Query API
# ---------------------------------------------------------------------------
# Prerequisites:
#   docker compose -f infra/docker-compose.phase2f.yml up -d --build
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase2f.yml"
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

echo "--- Phase 2F API Smoke Test ---"
echo ""

# ===========================================================================
# 1. Compose file exists
# ===========================================================================
if [[ -f "$COMPOSE_FILE" ]]; then
    check 1 "phase2f compose file exists" pass
else
    check 1 "phase2f compose file exists" fail
fi

# ===========================================================================
# 2. API container running
# ===========================================================================
API_STATUS="$(docker inspect phase2f-api 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")"
if [[ "$API_STATUS" == "running" ]]; then
    check 2 "API container running" pass
else
    check 2 "API container status=${API_STATUS}" fail
fi

# ===========================================================================
# 3. Postgres container running
# ===========================================================================
PG_STATUS="$(docker inspect phase2f-postgres 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")"
if [[ "$PG_STATUS" == "running" ]]; then
    check 3 "Postgres container running" pass
else
    check 3 "Postgres container status=${PG_STATUS}" fail
fi

# ===========================================================================
# 4. events table exists
# ===========================================================================
TABLE_CHECK="$(docker exec phase2f-postgres psql -U video -d video_analytics -t -c "SELECT to_regclass('public.events');" 2>/dev/null | tr -d '[:space:]' || echo "")"
if [[ "$TABLE_CHECK" == "events" ]]; then
    check 4 "events table exists" pass
else
    check 4 "events table exists" fail
fi

# ===========================================================================
# 5–16. Python: insert test events, call API endpoints, validate responses
# ===========================================================================
PY_OUTPUT="$(python3 -c "
import json, sys, time, uuid
import psycopg
import httpx

API = '${API_URL}'
pg_conn = psycopg.connect('postgresql://video:video@localhost:5432/video_analytics')
client = httpx.Client(timeout=10)

# --- Insert test events -------------------------------------------
ev1_id = str(uuid.uuid4())
ev2_id = str(uuid.uuid4())
sid1 = 'smoke:phase2f:cam_001:t_100:intrusion:5000'
sid2 = 'smoke:phase2f:cam_001:t_101:intrusion:6000'
now_iso = '2026-05-24T10:30:00+00:00'
kf_uuid = 'kf-smoke-2f-abc'

with pg_conn.cursor() as cur:
    cur.execute('''
        INSERT INTO events (id, source_event_id, event_type, camera_id, source_id, track_id,
            person_id, severity, confidence, start_ts, end_ts, event_ts_ms,
            frame_uuid, keyframe_uuid, status, recording_strategy, media_status, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_event_id) DO NOTHING
    ''', (
        ev1_id, sid1, 'intrusion', 'cam_001', 'src_01', 't_100',
        None, 'medium', 0.85, now_iso, now_iso, 5000,
        'frm-100', 'kf-100', 'new', 'reserved', 'not_implemented',
        json.dumps({'zone_id': 'full_frame', 'media': {
            'snapshot_status': 'not_implemented',
            'clip_status': 'not_implemented',
            'recording_strategy': 'reserved',
        }}),
    ))
    cur.execute('''
        INSERT INTO events (id, source_event_id, event_type, camera_id, source_id, track_id,
            person_id, severity, confidence, start_ts, end_ts, event_ts_ms,
            frame_uuid, keyframe_uuid, status, recording_strategy, media_status, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_event_id) DO NOTHING
    ''', (
        ev2_id, sid2, 'intrusion', 'cam_001', 'src_02', 't_101',
        None, 'high', 0.95, now_iso, now_iso, 6000,
        'frm-101', kf_uuid, 'new', 'reserved', 'not_implemented',
        json.dumps({'zone_id': 'full_frame', 'media': {
            'snapshot_status': 'not_implemented',
            'clip_status': 'not_implemented',
            'recording_strategy': 'reserved',
        }}),
    ))
pg_conn.close()
checks = [(5, 'insert test events', True)]

# --- /health ------------------------------------------------------
r = client.get(f'{API}/health')
checks.append((6, '/health returns 200', r.status_code == 200 and r.json()['status'] == 'ok'))

# --- /ready -------------------------------------------------------
r2 = client.get(f'{API}/ready')
body2 = r2.json()
checks.append((7, '/ready returns status key', 'status' in body2))

# --- /api/v1/events/recent ---------------------------------------
r3 = client.get(f'{API}/api/v1/events/recent?limit=10')
body3 = r3.json()
checks.append((8, '/api/v1/events/recent returns 200', r3.status_code == 200))
checks.append((9, 'response has data.events array', isinstance(body3.get('data', {}).get('events'), list)))
checks.append((10, 'events sorted newest first (check 2 events)',
    len(body3['data']['events']) >= 2))

# --- /api/v1/events with filter ----------------------------------
r4 = client.get(f'{API}/api/v1/events?event_type=intrusion&camera_id=cam_001&track_id=t_100')
body4 = r4.json()
checks.append((11, 'filter by event_type+camera_id+track_id returns 1',
    body4.get('data', {}).get('total') == 1))
checks.append((12, 'track_id is string t_100',
    body4['data']['events'][0]['track_id'] == 't_100'))

# --- /api/v1/events/{event_id} -----------------------------------
r5 = client.get(f'{API}/api/v1/events/{ev1_id}')
body5 = r5.json()
checks.append((13, 'GET /api/v1/events/{id} returns 200', r5.status_code == 200))
checks.append((14, 'event has source_event_id', body5['data']['source_event_id'] == sid1))

# --- snapshot_url / clip_url null ---------------------------------
checks.append((15, 'snapshot_url is None', body5['data']['snapshot_url'] is None))
checks.append((16, 'clip_url is None', body5['data']['clip_url'] is None))

# --- media fields -------------------------------------------------
media = body5['data']['media']
checks.append((17, 'media.snapshot_status == not_implemented',
    media.get('snapshot_status') == 'not_implemented'))
checks.append((18, 'media.clip_status == not_implemented',
    media.get('clip_status') == 'not_implemented'))
checks.append((19, 'media.recording_strategy == reserved',
    media.get('recording_strategy') == 'reserved'))

# --- keyframe_uuid in response ------------------------------------
r6 = client.get(f'{API}/api/v1/events/{ev2_id}')
body6 = r6.json()
checks.append((20, 'keyframe_uuid present in response',
    body6['data']['keyframe_uuid'] == kf_uuid))

# --- structured 404 ------------------------------------------------
r7 = client.get(f'{API}/api/v1/events/00000000-0000-0000-0000-000000000000')
body7 = r7.json()
checks.append((21, '404 has data=null', body7.get('data') is None))
checks.append((22, '404 has error object', body7.get('error') is not None))
checks.append((23, 'request_id present', 'request_id' in body7))

for n, desc, ok in checks:
    print(f'{n}|{\"pass\" if ok else \"fail\"}|{desc}')

client.close()
" 2>&1)" || true

while IFS='|' read -r num result desc; do
    if [[ -n "$num" && -n "$result" ]]; then
        check "$num" "$desc" "$result"
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
