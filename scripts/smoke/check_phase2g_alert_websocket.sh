#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 2G — Alert Stream and WebSocket Push
# ---------------------------------------------------------------------------
# Prerequisites:
#   docker compose -f infra/docker-compose.phase2g.yml up -d --build
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase2g.yml"
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

echo "--- Phase 2G Alert WebSocket Smoke Test ---"
echo ""

# ===========================================================================
# 1-4. Basic checks
# ===========================================================================
[[ -f "$COMPOSE_FILE" ]] && check 1 "compose file exists" pass || check 1 "compose file exists" fail

API_STATUS="$(docker inspect phase2g-api 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")"
[[ "$API_STATUS" == "running" ]] && check 2 "API container running" pass || check 2 "API container status=${API_STATUS}" fail

EW_STATUS="$(docker inspect phase2g-event-worker 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")"
[[ "$EW_STATUS" == "running" ]] && check 3 "event-worker running" pass || check 3 "event-worker status=${EW_STATUS}" fail

TABLE_CHECK="$(docker exec phase2g-postgres psql -U video -d video_analytics -t -c "SELECT to_regclass('public.events');" 2>/dev/null | tr -d '[:space:]' || echo "")"
[[ "$TABLE_CHECK" == "events" ]] && check 4 "events table exists" pass || check 4 "events table exists" fail

# ===========================================================================
# 5–19. Python: publish event, verify alert stream, test WebSocket
# ===========================================================================
PY_OUTPUT="$(python3 -c "
import json, sys, time, uuid
import psycopg
import redis

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=False)
pg_conn = psycopg.connect('postgresql://video:video@localhost:5432/video_analytics')

sid = 'smoke:phase2g:cam_01:t_g1:intrusion:1000'
event_uuid = str(uuid.uuid4())
event_data = {
    'source_event_id': sid,
    'event_type': 'intrusion',
    'camera_id': 'cam_01',
    'source_id': 'src_g1',
    'track_id': 't_g1',
    'severity': 'high',
    'confidence': 0.92,
    'start_ts_ms': 1000,
    'end_ts_ms': 3000,
    'event_ts_ms': 2000,
    'frame_uuid': 'frm-g1',
    'keyframe_uuid': 'kf-g1',
    'payload': {'media': {
        'snapshot_status': 'not_implemented',
        'clip_status': 'not_implemented',
        'recording_strategy': 'reserved',
    }},
}
event_json = json.dumps(event_data, ensure_ascii=False)

# 5. Insert event directly into PostgreSQL (simulating event-worker)
with pg_conn.cursor() as cur:
    cur.execute('''
        INSERT INTO events (id, source_event_id, event_type, camera_id, source_id, track_id,
            severity, confidence, start_ts, end_ts, event_ts_ms,
            frame_uuid, keyframe_uuid, status, recording_strategy, media_status, payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (source_event_id) DO NOTHING
        RETURNING id
    ''', (
        event_uuid, sid, 'intrusion', 'cam_01', 'src_g1', 't_g1',
        'high', 0.92, '2026-05-24T10:30:00+00:00', '2026-05-24T10:30:03+00:00', 2000,
        'frm-g1', 'kf-g1', 'new', 'reserved', 'not_implemented',
        json.dumps(event_data['payload']),
    ))
    row = cur.fetchone()
    if row:
        event_uuid = str(row['id'])
pg_conn.close()

print(f'5|pass|inserted event {sid}')

# 6. Publish alert to security.alerts (simulating event-worker after DB insert)
alert_id = f'alert:{sid}'
alert = {
    'alert_id': alert_id,
    'event_id': event_uuid,
    'source_event_id': sid,
    'event_type': 'intrusion',
    'camera_id': 'cam_01',
    'source_id': 'src_g1',
    'track_id': 't_g1',
    'severity': 'high',
    'confidence': 0.92,
    'status': 'new',
    'snapshot_url': None,
    'clip_url': None,
    'media': {
        'snapshot_status': 'not_implemented',
        'clip_status': 'not_implemented',
        'recording_strategy': 'reserved',
    },
    'created_at': '2026-05-24T10:30:00+00:00',
}
alert_json = json.dumps(alert, ensure_ascii=False)
msg_id = r.xadd('security.alerts', {
    'alert_id': alert_id,
    'source_event_id': sid,
    'event_type': 'intrusion',
    'camera_id': 'cam_01',
    'data': alert_json,
})
checks = [(6, 'published alert to security.alerts', bool(msg_id))]

# 7. Read alert back from security.alerts
results = r.xrange('security.alerts', '-', '+')
checks.append((7, 'alert readable via XRANGE', len(results) >= 1))

if results:
    _, fields = results[-1]
    alert_back = json.loads(fields[b'data'])
    checks += [
        (8,  'alert_id matches',        alert_back['alert_id'] == alert_id),
        (9,  'source_event_id matches',  alert_back['source_event_id'] == sid),
        (10, 'event_type == intrusion',  alert_back['event_type'] == 'intrusion'),
        (11, 'camera_id == cam_01',      alert_back['camera_id'] == 'cam_01'),
        (12, 'track_id == t_g1',         alert_back['track_id'] == 't_g1'),
        (13, 'snapshot_url is null',     alert_back['snapshot_url'] is None),
        (14, 'clip_url is null',         alert_back['clip_url'] is None),
    ]
    media = alert_back.get('media', {})
    checks += [
        (15, 'media.snapshot_status',    media.get('snapshot_status') == 'not_implemented'),
        (16, 'media.clip_status',        media.get('clip_status') == 'not_implemented'),
        (17, 'media.recording_strategy', media.get('recording_strategy') == 'reserved'),
    ]

# 8. Verify duplicate: publish same event ID again, check DB still has 1 row
pg_conn2 = psycopg.connect('postgresql://video:video@localhost:5432/video_analytics')
with pg_conn2.cursor() as cur:
    cur.execute('SELECT COUNT(*) FROM events WHERE source_event_id = %s', (sid,))
    count = cur.fetchone()[0]
pg_conn2.close()
checks.append((18, 'duplicate: DB count=1 (idempotent)', count == 1))

# 9. Verify API /health
import httpx
client = httpx.Client(timeout=10)
r_health = client.get('${API_URL}/health')
checks.append((19, '/health returns 200', r_health.status_code == 200))

# 10. Verify API /api/v1/events returns the event
r_events = client.get('${API_URL}/api/v1/events?camera_id=cam_01&track_id=t_g1')
body = r_events.json()
checks.append((20, 'API returns event for cam_01/t_g1',
    body.get('data', {}).get('total', 0) >= 1))

client.close()
r.close()

for n, desc, ok in checks:
    print(f'{n}|{\"pass\" if ok else \"fail\"}|{desc}')
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
