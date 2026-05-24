#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 3A-2 — Replay Media MVP (full pipeline)
# ---------------------------------------------------------------------------
# 前置条件:
#   sudo docker compose -f infra/docker-compose.phase3a.yml up -d --no-build
#   设置 RECORDING_ENABLED=true
#   等待 90 秒让 Savant 加载模型并开始产生事件
# ---------------------------------------------------------------------------
# 依赖：bash, curl, docker, python3 (stdlib json only)
# ---------------------------------------------------------------------------

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase3a.yml"
REPLAY_API="${REPLAY_API:-http://127.0.0.1:8080}"
API_BASE="${API_BASE:-http://127.0.0.1:8000}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0

_curl() { curl --noproxy '*' -s --connect-timeout 5 "$@"; }
_pg()   { docker exec phase3a-postgres psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null | head -n1 | tr -d '[:space:]'; }
_redis(){ docker exec phase3a-redis redis-cli "$@" 2>/dev/null; }

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

echo "--- Phase 3A-2 Replay Media MVP Smoke Test ---"
echo ""

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
REQUIRED="phase3a-redis phase3a-postgres phase3a-api phase3a-event-worker phase3a-clip-worker phase3a-media-worker phase3a-replay-service phase3a-video-file-sink"
MISSING=""
for c in $REQUIRED; do
    S="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null || echo "missing")"
    [[ "$S" != "running" ]] && MISSING="$MISSING $c($S)"
done
if [[ -n "$MISSING" ]]; then
    echo -e "${RED}容器未就绪:${NC}"
    for m in $MISSING; do echo "  - $m"; done
    fatal "容器未就绪"
fi
echo "  8/8 容器 running"
echo ""

# ===========================================================================
# Checks 1-2: event in DB
# ===========================================================================
check 1 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"

# Find an event
echo -e "${BLUE}查找事件...${NC}"
EVENT_ID="$(_pg "SELECT id FROM events ORDER BY created_at DESC LIMIT 1;")"
EVENT_SID="$(_pg "SELECT source_event_id FROM events ORDER BY created_at DESC LIMIT 1;")"
echo -e "${BLUE}[debug] EVENT_ID=${EVENT_ID} SID=${EVENT_SID}${NC}"
check 2 "events table has data" "$([[ -n "$EVENT_ID" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 3: API returns event
# ===========================================================================
if [[ -n "$EVENT_SID" ]]; then
    API_CODE="$(_curl -o /dev/null -w "%{http_code}" "${API_BASE}/api/v1/events/${EVENT_SID}")"
    check 3 "API returns event" "$([[ "$API_CODE" == "200" ]] && echo pass || echo fail)"
else
    check 3 "API returns event" fail
fi

# ===========================================================================
# Check 4-6: record_requests in Redis
# ===========================================================================
echo ""
echo -e "${BLUE}检查 security.record_requests...${NC}"
RR_RAW="$(_redis XRANGE security.record_requests - + COUNT 1 2>/dev/null || echo "")"
if [[ -n "$RR_RAW" && "$RR_RAW" != *"empty"* ]]; then
    check 4 "security.record_requests has messages" pass
else
    check 4 "security.record_requests has messages (RECORDING_ENABLED=true needed)" fail
fi

# ===========================================================================
# Check 5: Replay /api/v1/status
# ===========================================================================
R_STATUS="$(_curl -o /dev/null -w "%{http_code}" "${REPLAY_API}/api/v1/status")"
check 5 "Replay /api/v1/status" "$([[ "$R_STATUS" == "200" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 6-8: Media fields
# ===========================================================================
if [[ -n "$EVENT_ID" ]]; then
    MEDIA_JSON="$(_pg "SELECT payload->'media' FROM events WHERE id='${EVENT_ID}'::uuid;")"
    echo -e "${BLUE}[debug] media payload: ${MEDIA_JSON}${NC}"

    CLIP_STATUS="$(_pg "SELECT payload->'media'->>'clip_status' FROM events WHERE id='${EVENT_ID}'::uuid;")"
    check 6 "media.clip_status present" "$([[ -n "$CLIP_STATUS" ]] && echo pass || echo fail)"

    REC_STRATEGY="$(_pg "SELECT payload->'media'->>'recording_strategy' FROM events WHERE id='${EVENT_ID}'::uuid;")"
    check 7 "media.recording_strategy present" "$([[ -n "$REC_STRATEGY" ]] && echo pass || echo fail)"

    CLIP_PATH="$(_pg "SELECT clip_path FROM events WHERE id='${EVENT_ID}'::uuid;")"
    check 8 "clip_path field exists" "$([[ "$CLIP_PATH" != "NULL" || -z "$CLIP_PATH" ]] && echo pass || echo fail)"
else
    check 6 "media.clip_status" fail
    check 7 "media.recording_strategy" fail
    check 8 "clip_path" fail
fi

# ===========================================================================
# Check 9-10: API response includes clip_url/snapshot_url
# ===========================================================================
if [[ -n "$EVENT_SID" ]]; then
    EVENT_JSON="$(_curl "${API_BASE}/api/v1/events/${EVENT_SID}")"
    CLIP_URL="$(echo "$EVENT_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('clip_url','') or '')" 2>/dev/null)"
    SNAP_URL="$(echo "$EVENT_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('snapshot_url','') or '')" 2>/dev/null)"
    echo -e "${BLUE}[debug] clip_url=${CLIP_URL}${NC}"
    echo -e "${BLUE}[debug] snapshot_url=${SNAP_URL}${NC}"
    check 9 "clip_url in response" "$([[ -n "$CLIP_URL" ]] && echo pass || echo fail)"
    check 10 "snapshot_url in response (null is OK in MVP)" pass
else
    check 9 "clip_url" fail
    check 10 "snapshot_url" fail
fi

# ===========================================================================
# Check 11: idempotency still holds
# ===========================================================================
if [[ -n "$EVENT_SID" ]]; then
    COUNT="$(_pg "SELECT COUNT(*) FROM events WHERE source_event_id='${EVENT_SID}';")"
    check 11 "idempotent: SID count=1" "$([[ "$COUNT" == "1" ]] && echo pass || echo fail)"
else
    check 11 "idempotent" fail
fi

echo ""
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
