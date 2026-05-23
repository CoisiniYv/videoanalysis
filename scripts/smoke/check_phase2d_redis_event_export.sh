#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 2D — Redis Stream Events export
# ---------------------------------------------------------------------------
# Prerequisites:
#   docker compose -f infra/docker-compose.phase2d.yml up -d
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase2d.yml"
MODULE_FILE="${SMOKE_DIR}/../../modules/savant_phase2c/module.yml"
REDIS_CONTAINER="phase2d-redis"
SAVANT_CONTAINER="phase2d-savant"

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

echo "--- Phase 2D Redis Smoke Test ---"
echo ""

# ===========================================================================
# 1. Compose file exists
# ===========================================================================
if [[ -f "$COMPOSE_FILE" ]]; then
    check 1 "phase2d compose file exists" pass
else
    check 1 "phase2d compose file exists" fail
fi

# ===========================================================================
# 2. module.yml exists
# ===========================================================================
if [[ -f "$MODULE_FILE" ]]; then
    check 2 "phase2c module.yml exists" pass
else
    check 2 "phase2c module.yml exists" fail
fi

# ===========================================================================
# 3. Redis container is running
# ===========================================================================
REDIS_INSPECT="$(docker inspect "$REDIS_CONTAINER" 2>/dev/null || true)"
REDIS_STATUS="$(echo "$REDIS_INSPECT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['State']['Status'])" 2>/dev/null || echo "missing")"

if [[ "$REDIS_STATUS" == "running" ]]; then
    check 3 "Redis container ${REDIS_CONTAINER}: running" pass
else
    check 3 "Redis container ${REDIS_CONTAINER}: status=${REDIS_STATUS}" fail
fi

# ===========================================================================
# 4. Compose services include redis
# ===========================================================================
COMPOSE_SERVICES="$(docker compose -f "$COMPOSE_FILE" config --services 2>/dev/null || true)"
if echo "$COMPOSE_SERVICES" | grep -qx "redis"; then
    check 4 "Redis service in compose config" pass
else
    check 4 "Redis service in compose config" fail
fi

# ===========================================================================
# 5. Compose services do NOT include postgres/api/event-worker
# ===========================================================================
UNWANTED="$(echo "$COMPOSE_SERVICES" | grep -c -E "postgres|api|event-worker" 2>/dev/null || true)"
if [[ "$UNWANTED" -eq 0 ]]; then
    check 5 "No postgres/api/event-worker in compose services" pass
else
    check 5 "No postgres/api/event-worker in compose services (found ${UNWANTED})" fail
fi

# ===========================================================================
# 6. Redis PING
# ===========================================================================
PING_RESULT="$(docker exec "$REDIS_CONTAINER" redis-cli ping 2>/dev/null || echo "FAILED")"
if [[ "$PING_RESULT" == "PONG" ]]; then
    check 6 "Redis PING → PONG" pass
else
    check 6 "Redis PING (got: ${PING_RESULT})" fail
fi

# ===========================================================================
# 7–15. Python-based Redis stream validation
# ===========================================================================
PY_OUTPUT="$(python3 -c "
import json, os, sys, time

# Build a canonical SecurityEvent
event = {
    'schema_version': '1.0',
    'source_event_id': 'savant_phase2c:cam_01:3:intrusion:1000',
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
    'description': 'Track 3 intruded zone full_frame',
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

# Connect to Redis
import redis
r = redis.Redis(host='localhost', port=6379, db=0)

# XADD
stream = 'smoke.test.phase2d'
event_json = json.dumps(event, separators=(',', ':'), ensure_ascii=False)

fields = {
    'type': 'security_event',
    'source_event_id': event['source_event_id'],
    'event_type': event['event_type'],
    'camera_id': event['camera_id'],
    'track_id': str(event['track_id']),
    'start_ts_ms': str(event['start_ts_ms']),
    'end_ts_ms': str(event['end_ts_ms']),
    'severity': event['severity'],
    'data': json.dumps(event, ensure_ascii=False),
}

msg_id = r.xadd(stream, fields)

# XRANGE read back
results = r.xrange(stream, '-', '+')

# Clean up
r.delete(stream)

if not results:
    print('7|fail|XADD succeeded but XRANGE returned empty', end='')
    sys.exit(0)

_, data = results[0]

checks = [
    (7,  'XADD + XRANGE round-trip',     bool(msg_id and len(results) == 1)),
    (8,  'source_event_id preserved',     data[b'source_event_id'].decode() == event['source_event_id']),
    (9,  'event_type == intrusion',       data[b'event_type'].decode() == 'intrusion'),
    (10, 'camera_id == cam_01',           data[b'camera_id'].decode() == 'cam_01'),
    (11, 'track_id preserved',            data[b'track_id'].decode() == '3'),
    (12, 'schema_version == 1.0',         json.loads(data[b'data'])['schema_version'] == '1.0'),
    (13, 'snapshot_status == not_implemented', json.loads(data[b'data'])['payload']['media']['snapshot_status'] == 'not_implemented'),
    (14, 'clip_status == not_implemented',     json.loads(data[b'data'])['payload']['media']['clip_status'] == 'not_implemented'),
    (15, 'recording_strategy == reserved',     json.loads(data[b'data'])['payload']['media']['recording_strategy'] == 'reserved'),
]

for num, desc, ok in checks:
    print(f'{num}|{\"pass\" if ok else \"fail\"}|{desc}')
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
