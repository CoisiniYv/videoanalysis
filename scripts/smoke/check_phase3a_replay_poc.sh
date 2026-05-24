#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 3A-1 — Replay Service + Video File Sink POC
# ---------------------------------------------------------------------------
# 前置条件:
#   sudo docker compose -f infra/docker-compose.phase3a.yml up -d --no-build
#   等待 60 秒让 Savant 和 Replay Service 初始化
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
# Pre-flight
# ---------------------------------------------------------------------------
echo "--- Phase 3A-1 Replay + Video File Sink POC Smoke Test ---"
echo ""

REQUIRED="phase3a-redis phase3a-postgres phase3a-api phase3a-event-worker phase3a-rtsp-server phase3a-ffmpeg-source phase3a-savant phase3a-replay-service phase3a-video-file-sink"
MISSING=""
for c in $REQUIRED; do
    S="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null || echo "missing")"
    [[ "$S" != "running" ]] && MISSING="$MISSING $c($S)"
done
if [[ -n "$MISSING" ]]; then
    echo -e "${RED}容器未就绪:${NC}"
    for m in $MISSING; do echo "  - $m"; done
    echo ""
    echo -e "${YELLOW}请先: sudo docker compose -f ${COMPOSE_FILE} up -d --no-build${NC}"
    fatal "容器未就绪"
fi
echo "  9/9 容器 running"
echo ""

# ===========================================================================
# Check 1-2: compose + backend health
# ===========================================================================
check 1 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"
HCODE="$(_curl -o /dev/null -w "%{http_code}" "${API_BASE}/health")"
check 2 "/health 200" "$([[ "$HCODE" == "200" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 3: Replay Service /api/v1/status
# ===========================================================================
echo ""
echo -e "${BLUE}--- Replay Service ---${NC}"
REPLAY_STATUS="$(_curl "${REPLAY_API}/api/v1/status" 2>/dev/null || echo '{}')"
REPLAY_CODE="$(_curl -o /dev/null -w "%{http_code}" "${REPLAY_API}/api/v1/status" 2>/dev/null || echo "000")"
echo -e "${BLUE}[debug] Replay /api/v1/status HTTP ${REPLAY_CODE}${NC}"
echo -e "${BLUE}[debug] ${REPLAY_STATUS}${NC}"

if [[ "$REPLAY_CODE" == "200" ]]; then
    check 3 "Replay /api/v1/status 200" pass
else
    check 3 "Replay /api/v1/status 200 (got ${REPLAY_CODE})" fail
fi

# ===========================================================================
# Check 4-5: keyframes/find
# ===========================================================================
echo ""
echo -e "${BLUE}--- Keyframes Find ---${NC}"
# Try with a sample source_id; Replay registers source_id from its input
KF_RESP="$(_curl "${REPLAY_API}/api/v1/keyframes/find?source_id=phase3a&ts=10000" 2>/dev/null || echo '{}')"
KF_CODE="$(_curl -o /dev/null -w "%{http_code}" "${REPLAY_API}/api/v1/keyframes/find?source_id=phase3a&ts=10000" 2>/dev/null || echo "000")"
echo -e "${BLUE}[debug] keyframes/find HTTP ${KF_CODE}${NC}"
echo -e "${BLUE}[debug] ${KF_RESP}${NC}"

# 200 = found, 404 = no keyframes (Replay may not have buffered enough frames yet)
check 4 "keyframes/find returns 200 (found keyframes)" "$([[ "$KF_CODE" == "200" ]] && echo pass || echo fail)"
[[ "$KF_CODE" == "404" ]] && echo -e "${YELLOW}  keyframes/find 404: No keyframes found; check source_id and Replay input stream.${NC}"

# ===========================================================================
# Check 5: /api/v1/job (create re-streaming job)
# ===========================================================================
echo ""
echo -e "${BLUE}--- Create Replay Job ---${NC}"
JOB_PAYLOAD='{"source_id":"phase3a","offset":{"seconds":5},"stop_condition":{"seconds":10},"sink":{"url":"pub+connect:tcp://video-file-sink:6666"}}'
echo -e "${BLUE}[debug] PUT /api/v1/job payload: ${JOB_PAYLOAD}${NC}"
JOB_RESP="$(_curl -X PUT "${REPLAY_API}/api/v1/job" \
    -H "Content-Type: application/json" \
    -d "${JOB_PAYLOAD}" 2>/dev/null || echo '{}')"
JOB_CODE="$(_curl -o /dev/null -w "%{http_code}" -X PUT "${REPLAY_API}/api/v1/job" \
    -H "Content-Type: application/json" \
    -d "${JOB_PAYLOAD}" 2>/dev/null || echo "000")"
echo -e "${BLUE}[debug] PUT /api/v1/job HTTP ${JOB_CODE}${NC}"
echo -e "${BLUE}[debug] ${JOB_RESP}${NC}"

check 5 "PUT /api/v1/job accepted" "$([[ "$JOB_CODE" == "200" || "$JOB_CODE" == "201" || "$JOB_CODE" == "202" ]] && echo pass || echo fail)"

# ===========================================================================
# Check 6-7: Video File Sink output
# ===========================================================================
echo ""
echo -e "${BLUE}--- Video File Sink Output ---${NC}"
# Check if sink output directory has files
SINK_FILES="$(docker exec phase3a-video-file-sink find /media/replay-sink-output -type f \( -name '*.mkv' -o -name '*.mov' -o -name '*.webm' -o -name '*.mp4' -o -name '*.json' \) 2>/dev/null | head -10 || echo "")"
echo -e "${BLUE}[debug] Sink files: ${SINK_FILES}${NC}"

if [[ -n "$SINK_FILES" ]]; then
    check 6 "sink output directory has files" pass
else
    check 6 "sink output directory has files (may need replay job to complete; wait 30s)" fail
fi

# Check metadata.json
META_SIZE="$(docker exec phase3a-video-file-sink stat -c%s /media/replay-sink-output/metadata.json 2>/dev/null || echo "0")"
check 7 "metadata.json exists and non-empty" "$([[ "${META_SIZE:-0}" -gt 0 ]] && echo pass || echo fail)"

# ===========================================================================
# Check 8: video file
# ===========================================================================
VIDEO_COUNT="$(docker exec phase3a-video-file-sink find /media/replay-sink-output -type f \( -name '*.mkv' -o -name '*.mov' -o -name '*.webm' -o -name '*.mp4' \) 2>/dev/null | wc -l | tr -d ' \n' || echo "0")"
VIDEO_SIZE="$(docker exec phase3a-video-file-sink find /media/replay-sink-output -type f \( -name '*.mkv' -o -name '*.mov' -o -name '*.webm' -o -name '*.mp4' \) -printf '%s\n' 2>/dev/null | awk '{s+=$1} END {print s+0}' || echo "0")"
echo -e "${BLUE}[debug] Video files found: ${VIDEO_COUNT}, total bytes: ${VIDEO_SIZE}${NC}"
check 8 "video file exists and non-empty" "$([[ "${VIDEO_COUNT:-0}" -gt 0 && "${VIDEO_SIZE:-0}" -gt 0 ]] && echo pass || echo fail)"

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
fi
