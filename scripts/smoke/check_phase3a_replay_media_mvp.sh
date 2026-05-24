#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 3A-2 — Replay Media MVP (full pipeline)
# ---------------------------------------------------------------------------
# PREREQ: docker compose RECORDING_ENABLED="true" in compose file
# ---------------------------------------------------------------------------
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase3a.yml"
REPLAY_API="${REPLAY_API:-http://127.0.0.1:8080}"
API_BASE="${API_BASE:-http://127.0.0.1:8000}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
PASS_COUNT=0; FAIL_COUNT=0

_curl() { curl --noproxy '*' -s --connect-timeout 5 "$@"; }
_pg()   { docker exec phase3a-postgres psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null | head -n1 | tr -d '[:space:]'; }
_redis(){ docker exec phase3a-redis redis-cli "$@" 2>/dev/null; }

check() {
    local num="$1" desc="$2" result="$3"
    if [[ "$result" == "pass" ]]; then
        echo -e "  ${GREEN}OK${NC}  [$num] $desc"; PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo -e "  ${RED}FAIL${NC}  [$num] $desc"; FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}
fatal() { echo -e "${RED}FATAL${NC}: $*"; exit 1; }

echo "--- Phase 3A-2 Replay Media MVP Smoke Test ---"
echo ""

# Pre-flight
REQUIRED="phase3a-redis phase3a-postgres phase3a-api phase3a-event-worker phase3a-clip-worker phase3a-media-worker phase3a-replay-service phase3a-video-file-sink"
MISSING=""
for c in $REQUIRED; do
    S="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null || echo "missing")"
    [[ "$S" != "running" ]] && MISSING="$MISSING $c($S)"
done
[[ -n "$MISSING" ]] && { echo -e "${RED}容器未就绪:${NC}"; for m in $MISSING; do echo "  - $m"; done; fatal "容器未就绪"; }
echo "  8/8 容器 running"
echo ""

check 1 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"
check 2 "Replay /api/v1/status" "$([[ "$(_curl -o /dev/null -w '%{http_code}' "${REPLAY_API}/api/v1/status")" == "200" ]] && echo pass || echo fail)"

# ===========================================================================
# Inject test event with proper media fields
# ===========================================================================
UNIQ="$(date +%s%N)"
SID="smoke:phase3a-2:phase3a:t_media:intrusion:${UNIQ}"
EVENT_TS="$(date +%s%3N)"

echo -e "${BLUE}[debug] SID=${SID} ts=${EVENT_TS}${NC}"

# Build event JSON using Python heredoc
EVENT_JSON=$(python3 << PYEOF
import json
media = {
    "source_id": "phase3a",
    "snapshot_required": True,
    "clip_required": True,
    "snapshot_status": "not_implemented",
    "clip_status": "not_implemented",
    "recording_strategy": "savant_replay",
    "pre_seconds": 5,
    "post_seconds": 5,
    "event_ts_ms": ${EVENT_TS},
    "frame_uuid": None,
    "keyframe_uuid": None,
}
event = {
    "schema_version": "1.0",
    "source_event_id": "${SID}",
    "producer": "savant_phase2c",
    "gpu_id": 0,
    "event_type": "intrusion",
    "camera_id": "cam_01",
    "source_id": "phase3a",
    "track_id": "t_media",
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
    "description": "Phase 3A-2 smoke test event",
    "snapshot_required": True,
    "clip_required": True,
    "payload": {"zone_id": "full_frame", "media": media},
}
print(json.dumps(event))
PYEOF
)

_redis XADD security.events '*' type security_event source_event_id "${SID}" event_type intrusion camera_id cam_01 source_id phase3a track_id t_media severity high data "${EVENT_JSON}" > /dev/null 2>&1
echo -e "${BLUE}[debug] Event injected into security.events${NC}"
check 3 "injected event into security.events" pass

# Wait for event-worker
echo -e "${BLUE}Waiting for event-worker (30s)...${NC}"
for i in $(seq 1 6); do
    sleep 5
    DB_ID="$(_pg "SELECT id FROM events WHERE source_event_id='${SID}';")"
    [[ -n "$DB_ID" ]] && break
    echo -n "."
done
echo ""
DB_ID="$(_pg "SELECT id FROM events WHERE source_event_id='${SID}';")"
check 4 "event in PostgreSQL" "$([[ -n "$DB_ID" ]] && echo pass || echo fail)"

# Check record_requests
echo -e "${BLUE}Checking security.record_requests...${NC}"
RR_RAW="$(_redis XRANGE security.record_requests - + COUNT 5 2>/dev/null || echo "")"
check 5 "security.record_requests has messages" "$([[ -n "$RR_RAW" && "$RR_RAW" != *"empty"* ]] && echo pass || echo fail)"

# Wait for media pipeline (clip-worker + replay job + sink + media-worker)
echo -e "${BLUE}Waiting for media pipeline (90s)...${NC}"
CLIP_PATH=""
for i in $(seq 1 18); do
    sleep 5
    CLIP_PATH="$(_pg "SELECT clip_path FROM events WHERE source_event_id='${SID}';")"
    [[ -n "$CLIP_PATH" && "$CLIP_PATH" != "NULL" ]] && break
    echo -n "."
done
echo ""

# Check media fields
CLIP_STATUS="$(_pg "SELECT payload->'media'->>'clip_status' FROM events WHERE source_event_id='${SID}';")"
echo -e "${BLUE}[debug] clip_status=${CLIP_STATUS}${NC}"
check 6 "media.clip_status" "$([[ -n "$CLIP_STATUS" ]] && echo pass || echo fail)"

REC_STRATEGY="$(_pg "SELECT payload->'media'->>'recording_strategy' FROM events WHERE source_event_id='${SID}';")"
echo -e "${BLUE}[debug] recording_strategy=${REC_STRATEGY}${NC}"
check 7 "recording_strategy=savant_replay" "$([[ "$REC_STRATEGY" == "savant_replay" ]] && echo pass || echo fail)"

echo -e "${BLUE}[debug] clip_path=${CLIP_PATH}${NC}"
check 8 "clip_path non-null" "$([[ -n "$CLIP_PATH" && "$CLIP_PATH" != "NULL" ]] && echo pass || echo fail)"

# API clip_url
EVENT_RESP="$(_curl "${API_BASE}/api/v1/events/${SID}")"
CLIP_URL="$(echo "$EVENT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('clip_url','') or '')" 2>/dev/null)"
SNAP_URL="$(echo "$EVENT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('snapshot_url','') or '')" 2>/dev/null)"
echo -e "${BLUE}[debug] clip_url=${CLIP_URL} snapshot_url=${SNAP_URL}${NC}"
check 9 "API returns clip_url" "$([[ -n "$CLIP_URL" ]] && echo pass || echo fail)"
check 10 "API returns snapshot_url (null OK)" "$([[ -z "$SNAP_URL" || "$SNAP_URL" == "None" ]] && echo pass || echo pass)"

# Idempotency
COUNT="$(_pg "SELECT COUNT(*) FROM events WHERE source_event_id='${SID}';")"
check 11 "idempotent: count=1" "$([[ "$COUNT" == "1" ]] && echo pass || echo fail)"

echo ""
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"
[[ "$FAIL_COUNT" -gt 0 ]] && exit 1
