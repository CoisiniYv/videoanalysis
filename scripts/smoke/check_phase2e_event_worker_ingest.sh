#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 2E — Event Worker PostgreSQL ingestion
# ---------------------------------------------------------------------------
# Prerequisites:
#   docker compose -f infra/docker-compose.phase2e.yml up -d --build
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase2e.yml"
MIGRATION_FILE="${SMOKE_DIR}/../../db/migrations/002_phase2e_events.sql"

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

echo "--- Phase 2E Event Worker Smoke Test ---"
echo ""

# ===========================================================================
# 1. Compose file exists
# ===========================================================================
if [[ -f "$COMPOSE_FILE" ]]; then
    check 1 "phase2e compose file exists" pass
else
    check 1 "phase2e compose file exists" fail
fi

# ===========================================================================
# 2. Migration file exists
# ===========================================================================
if [[ -f "$MIGRATION_FILE" ]]; then
    check 2 "phase2e migration file exists" pass
else
    check 2 "phase2e migration file exists" fail
fi

# ===========================================================================
# 3. Redis container running
# ===========================================================================
REDIS_STATUS="$(docker inspect phase2e-redis 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")"
if [[ "$REDIS_STATUS" == "running" ]]; then
    check 3 "Redis container running" pass
else
    check 3 "Redis container status=${REDIS_STATUS}" fail
fi

# ===========================================================================
# 4. Postgres container running
# ===========================================================================
PG_STATUS="$(docker inspect phase2e-postgres 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")"
if [[ "$PG_STATUS" == "running" ]]; then
    check 4 "Postgres container running" pass
else
    check 4 "Postgres container status=${PG_STATUS}" fail
fi

# ===========================================================================
# 5. Migration applied (events table exists)
# ===========================================================================
TABLE_CHECK="$(docker exec phase2e-postgres psql -U video -d video_analytics -t -c "SELECT to_regclass('public.events');" 2>/dev/null | tr -d '[:space:]' || echo "")"
if [[ "$TABLE_CHECK" == "events" ]]; then
    check 5 "events table exists in PostgreSQL" pass
else
    # Try applying migration manually
    docker exec phase2e-postgres psql -U video -d video_analytics -f /docker-entrypoint-initdb.d/002_phase2e_events.sql 2>/dev/null || true
    TABLE_CHECK2="$(docker exec phase2e-postgres psql -U video -d video_analytics -t -c "SELECT to_regclass('public.events');" 2>/dev/null | tr -d '[:space:]' || echo "")"
    if [[ "$TABLE_CHECK2" == "events" ]]; then
        check 5 "events table exists in PostgreSQL (created now)" pass
    else
        check 5 "events table exists in PostgreSQL" fail
    fi
fi

# ===========================================================================
# 6. Event worker container running
# ===========================================================================
EW_STATUS="$(docker inspect phase2e-event-worker 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")"
if [[ "$EW_STATUS" == "running" ]]; then
    check 6 "event-worker container running" pass
else
    check 6 "event-worker container status=${EW_STATUS}" fail
fi

# ===========================================================================
# 7–16. Python: XADD event → wait → verify DB → test idempotency
# ===========================================================================
PY_OUTPUT="$(python3 -c "
import json, os, sys, time

# --- Build test SecurityEvent --------------------------------
sid = 'smoke:phase2e:cam_01:3:intrusion:1000'
event = {
    'schema_version': '1.0',
    'source_event_id': sid,
    'producer': 'savant_phase2c',
    'gpu_id': 0,
    'event_type': 'intrusion',
    'camera_id': 'cam_01',
    'source_id': 'src_01',
    'track_id': 3,
    'person_id': 0,
    'start_ts_ms': 1000,
    'end_ts_ms': 2500,
    'event_ts_ms': 2000,
    'frame_id': 42,
    'frame_uuid': None,
    'keyframe_uuid': None,
    'confidence': 0.85,
    'severity': 'medium',
    'zone': 'full_frame',
    'rule_name': 'debug_intrusion',
    'description': 'Smoke test event',
    'snapshot_required': False,
    'clip_required': False,
    'payload': {
        'zone_id': 'full_frame',
        'inside_ms': 1500,
        'bbox': {'x': 100, 'y': 200, 'width': 300, 'height': 400},
        'rule': 'debug_intrusion',
        'media': {
            'snapshot_required': False,
            'clip_required': False,
            'snapshot_status': 'not_implemented',
            'clip_status': 'not_implemented',
            'recording_strategy': 'reserved',
            'pre_seconds': 5,
            'post_seconds': 5,
            'source_id': 'src_01',
            'event_ts_ms': 2000,
            'frame_uuid': None,
            'keyframe_uuid': None,
        },
    },
}

event_json = json.dumps(event, ensure_ascii=False)

# --- Connect to Redis ----------------------------------------
import redis
r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=False)

# --- XADD event to security.events ---------------------------
stream = 'security.events'
fields = {
    'type': 'security_event',
    'source_event_id': sid,
    'event_type': 'intrusion',
    'camera_id': 'cam_01',
    'track_id': '3',
    'start_ts_ms': '1000',
    'end_ts_ms': '2500',
    'severity': 'medium',
    'data': event_json,
}
msg_id_1 = r.xadd(stream, fields)
print(f'7|pass|XADD first event ({msg_id_1.decode() if isinstance(msg_id_1, bytes) else msg_id_1})')

# --- Wait for event-worker to process ------------------------
time.sleep(3)

# --- Query PostgreSQL ----------------------------------------
import psycopg
pg_conn = psycopg.connect('postgresql://video:video@localhost:5432/video_analytics')
with pg_conn.cursor() as cur:
    cur.execute('SELECT id, source_event_id, event_type, camera_id, track_id, payload FROM events WHERE source_event_id = %s', (sid,))
    row = cur.fetchone()

if row:
    print('8|pass|event inserted into PostgreSQL')
    db_id, db_sid, db_type, db_cam, db_track, db_payload = row
    checks = [
        (9,  'event_type == intrusion',              db_type == 'intrusion'),
        (10, 'camera_id == cam_01',                  db_cam == 'cam_01'),
        (11, 'track_id == 3',                        db_track == 3),
        (12, 'source_event_id matches',              db_sid == sid),
    ]
    payload = db_payload if isinstance(db_payload, dict) else json.loads(db_payload)
    media = payload.get('media', {})
    checks += [
        (13, 'snapshot_status == not_implemented',   media.get('snapshot_status') == 'not_implemented'),
        (14, 'clip_status == not_implemented',       media.get('clip_status') == 'not_implemented'),
        (15, 'recording_strategy == reserved',       media.get('recording_strategy') == 'reserved'),
    ]
    for n, desc, ok in checks:
        print(f'{n}|{\"pass\" if ok else \"fail\"}|{desc}')
else:
    for i in range(8, 16):
        print(f'{i}|fail|event not found in PostgreSQL')
    pg_conn.close()
    sys.exit(0)

# --- Idempotency: XADD same event again --------------------
msg_id_2 = r.xadd(stream, fields)
time.sleep(2)

with pg_conn.cursor() as cur:
    cur.execute('SELECT COUNT(*) FROM events WHERE source_event_id = %s', (sid,))
    count = cur.fetchone()[0]

if count == 1:
    print('16|pass|idempotent: duplicate not inserted (count=1)')
else:
    print(f'16|fail|idempotent: count={count} (expected 1)')

# --- Verify Redis ACK (pending should be 0 for this group) --
# event-workers group should have processed and ACKed
try:
    pending_info = r.xpending(stream, 'event-workers')
    pending_count = pending_info.get('pending', 0) if isinstance(pending_info, dict) else 0
    if pending_count == 0:
        print('17|pass|Redis ACK confirmed (pending=0)')
    else:
        print(f'17|fail|Redis pending={pending_count} (expected 0)')
except Exception as e:
    # group may not exist if worker hasn't started yet
    if 'NOGROUP' in str(e):
        print('17|fail|consumer group event-workers not found')
    else:
        print(f'17|fail|Redis ACK check error: {e}')

pg_conn.close()
r.close()
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
