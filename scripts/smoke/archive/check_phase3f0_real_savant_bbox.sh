#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 3F0 — Real Savant Bbox Media Evidence
# ---------------------------------------------------------------------------
# PREREQ:
#   docker compose -f infra/docker-compose.phase3b.yml up -d
#   docker compose -f infra/docker-compose.phase3b.yml --profile gpu up -d savant
#   docker compose -f infra/docker-compose.phase3b.yml up -d ffmpeg-source
#
# This is a MANUAL VERIFICATION script. It does NOT inject events.
# It checks that real Savant detection events have produced trusted bbox
# annotated snapshots through the full media pipeline.
# ---------------------------------------------------------------------------
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase3b.yml"
API_BASE="${API_BASE:-http://127.0.0.1:8001}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
PASS_COUNT=0; FAIL_COUNT=0

_curl() { curl --noproxy '*' -s --connect-timeout 5 "$@"; }
_pg()   { docker exec phase3b-postgres psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null || echo ""; }
_redis(){ docker exec phase3b-redis redis-cli "$@" 2>/dev/null; }

check() {
    local num="$1" desc="$2" result="$3"
    if [[ "$result" == "pass" ]]; then
        echo -e "  ${GREEN}OK${NC}  [$num] $desc"; PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo -e "  ${RED}FAIL${NC}  [$num] $desc"; FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}
fatal() { echo -e "${RED}FATAL${NC}: $*"; exit 1; }

echo "--- Phase 3F0 Real Savant Bbox Media Evidence Verification ---"
echo ""

# Pre-flight: check all required services
REQUIRED="phase3b-redis phase3b-postgres phase3b-api phase3b-event-worker phase3b-clip-worker phase3b-media-worker phase3b-savant phase3b-ffmpeg-source"
MISSING=""
for c in $REQUIRED; do
    S="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null || echo "missing")"
    [[ "$S" != "running" ]] && MISSING="$MISSING $c($S)"
done
[[ -n "$MISSING" ]] && { echo -e "${RED}Containers not ready:${NC}"; for m in $MISSING; do echo "  - $m"; done; fatal "containers not ready"; }
echo "  8/8 containers running (including GPU Savant + ffmpeg-source)"
echo ""

check 1 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"

# ===========================================================================
# [2] Savant is running and producing events
# ===========================================================================
SAVANT_EVENT_COUNT="$(_redis XLEN security.events | tr -d '[:space:]')"
echo -e "${BLUE}[debug] security.events total entries: ${SAVANT_EVENT_COUNT}${NC}"

# Find most recent non-smoke event
NON_SMOKE_SID="$(_redis XREVRANGE security.events + - COUNT 50 2>/dev/null \
    | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
for i in range(0, len(lines)-1, 2):
    if lines[i].strip() == 'source_event_id':
        sid = lines[i+1].strip()
        if not sid.startswith('smoke:'):
            print(sid)
            break
" 2>/dev/null || echo "")"

check 2 "Redis has non-smoke Savant events" "$([[ -n "$NON_SMOKE_SID" ]] && echo pass || echo fail)"
echo -e "${BLUE}[debug] Latest non-smoke source_event_id: ${NON_SMOKE_SID}${NC}"

# ===========================================================================
# [3] DB has non-smoke Savant event
# ===========================================================================
DB_ROW="$(_pg "SELECT id, source_event_id, payload->>'bbox_source' as bbox_source FROM events WHERE source_event_id NOT LIKE 'smoke:%' AND payload->'media'->>'annotated_snapshot_status' = 'ready' AND payload->>'bbox_source' = 'savant_detection' ORDER BY created_at DESC LIMIT 1;")"
DB_ID="$(echo "$DB_ROW" | cut -d'|' -f1 | tr -d '[:space:]')"
DB_SID="$(echo "$DB_ROW" | cut -d'|' -f2 | tr -d '[:space:]')"
DB_BBOX_SRC="$(echo "$DB_ROW" | cut -d'|' -f3 | tr -d '[:space:]')"

check 3 "DB has non-smoke event" "$([[ -n "$DB_ID" ]] && echo pass || echo fail)"
echo -e "${BLUE}[debug] DB event: id=${DB_ID} sid=${DB_SID} bbox_source=${DB_BBOX_SRC}${NC}"

# ===========================================================================
# [4] bbox_source=savant_detection
# ===========================================================================
check 4 "payload.bbox_source is savant_detection" "$([[ "$DB_BBOX_SRC" == "savant_detection" ]] && echo pass || echo fail)"

# ===========================================================================
# [5-10] Media pipeline results for this event
# ===========================================================================
if [[ -n "$DB_SID" && "$DB_SID" != "NULL" ]]; then
    MEDIA="$(_pg "SELECT
        payload->'media'->>'clip_status',
        payload->'media'->>'snapshot_status',
        payload->'media'->>'annotated_snapshot_status',
        payload->'media'->>'bbox_overlay_status',
        payload->'media'->>'annotated_snapshot_path',
        payload->'media'->>'recording_strategy'
    FROM events WHERE source_event_id='${DB_SID}';")"

    CLIP_STATUS="$(echo "$MEDIA" | cut -d'|' -f1 | tr -d '[:space:]')"
    SNAP_STATUS="$(echo "$MEDIA" | cut -d'|' -f2 | tr -d '[:space:]')"
    ANN_STATUS="$(echo "$MEDIA" | cut -d'|' -f3 | tr -d '[:space:]')"
    BBOX_OVERLAY="$(echo "$MEDIA" | cut -d'|' -f4 | tr -d '[:space:]')"
    ANN_PATH="$(echo "$MEDIA" | cut -d'|' -f5 | tr -d '[:space:]')"
    REC_STRATEGY="$(echo "$MEDIA" | cut -d'|' -f6 | tr -d '[:space:]')"

    echo -e "${BLUE}[debug] media: clip=${CLIP_STATUS} snap=${SNAP_STATUS} ann=${ANN_STATUS} bbox=${BBOX_OVERLAY} rec=${REC_STRATEGY}${NC}"

    check 5 "recording_strategy is savant_replay" "$([[ "$REC_STRATEGY" == "savant_replay" ]] && echo pass || echo fail)"
    check 6 "clip_status is ready" "$([[ "$CLIP_STATUS" == "ready" ]] && echo pass || echo fail)"
    check 7 "snapshot_status is ready" "$([[ "$SNAP_STATUS" == "ready" ]] && echo pass || echo fail)"
    check 8 "annotated_snapshot_status is ready" "$([[ "$ANN_STATUS" == "ready" ]] && echo pass || echo fail)"
    check 9 "bbox_overlay_status is ready" "$([[ "$BBOX_OVERLAY" == "ready" ]] && echo pass || echo fail)"

    # =========================================================================
    # [10] Annotated snapshot URL returns HTTP 200
    # =========================================================================
    if [[ -n "$ANN_PATH" && "$ANN_PATH" != "NULL" ]]; then
        ANN_URL="/media/snapshots/annotated/$(basename "$ANN_PATH")"
        ANN_HTTP="$(_curl -o /dev/null -w '%{http_code}' "${API_BASE}${ANN_URL}")"
        echo -e "${BLUE}[debug] curl ${API_BASE}${ANN_URL} HTTP ${ANN_HTTP}${NC}"
        check 10 "annotated_snapshot_url HTTP 200" "$([[ "$ANN_HTTP" == "200" ]] && echo pass || echo fail)"
    else
        check 10 "annotated_snapshot_url HTTP 200" fail
    fi

    # =========================================================================
    # [11] Annotated file exists on filesystem
    # =========================================================================
    if [[ -n "$ANN_PATH" && "$ANN_PATH" != "NULL" ]]; then
        ANN_FILE_EXISTS="$(docker exec phase3b-media-worker test -f "${ANN_PATH}" && echo yes || echo no)"
        check 11 "annotated snapshot file exists on media volume" "$([[ "$ANN_FILE_EXISTS" == "yes" ]] && echo pass || echo fail)"
    else
        check 11 "annotated snapshot file exists on media volume" fail
    fi

    # =========================================================================
    # [12] DB event has real payload.bbox from detection
    # =========================================================================
    BBOX="$(_pg "SELECT payload->>'bbox' FROM events WHERE source_event_id='${DB_SID}';")"
    BBOX_HAS_DATA="$([[ -n "$BBOX" && "$BBOX" != "NULL" && "$BBOX" != "null" ]] && echo pass || echo fail)"
    check 12 "payload.bbox exists and is non-null" "$BBOX_HAS_DATA"
    echo -e "${BLUE}[debug] payload.bbox=${BBOX}${NC}"

    # =========================================================================
    # [13] Manual inspection path
    # =========================================================================
    if [[ -n "$ANN_PATH" && "$ANN_PATH" != "NULL" ]]; then
        echo ""
        echo -e "${BLUE}=====================================================================${NC}"
        echo -e "${GREEN}Manual inspection:${NC}"
        echo ""
        echo "  mkdir -p manual-inspection"
        echo "  docker cp phase3b-media-worker:${ANN_PATH} manual-inspection/latest_phase3f0_real_bbox.jpg"
        echo "  xdg-open manual-inspection/latest_phase3f0_real_bbox.jpg"
        echo ""
        echo -e "${YELLOW}Verify:${NC}"
        echo "  - Red bbox rectangle IS present (real Savant YOLO detection)"
        echo "  - Text label block is readable (event ID, type, camera, confidence)"
        echo "  - Image is a real frame from the test video with people"
        echo -e "${BLUE}=====================================================================${NC}"
        check 13 "manual inspection path printed" pass

        # Auto-copy to manual-inspection for convenience
        mkdir -p manual-inspection 2>/dev/null || true
        docker cp "phase3b-media-worker:${ANN_PATH}" "manual-inspection/latest_phase3f0_real_bbox.jpg" 2>/dev/null && \
            echo -e "${GREEN}Auto-copied to manual-inspection/latest_phase3f0_real_bbox.jpg${NC}" || \
            echo -e "${YELLOW}Could not auto-copy (may need sudo). Run copy command above.${NC}"
    else
        check 13 "manual inspection path printed" fail
    fi
else
    check 5 "recording_strategy is savant_replay" fail
    check 6 "clip_status is ready" fail
    check 7 "snapshot_status is ready" fail
    check 8 "annotated_snapshot_status is ready" fail
    check 9 "bbox_overlay_status is ready" fail
    check 10 "annotated_snapshot_url HTTP 200" fail
    check 11 "annotated snapshot file exists on media volume" fail
    check 12 "payload.bbox exists and is non-null" fail
    check 13 "manual inspection path printed" fail
fi

echo ""
echo "--- Results: ${PASS_COUNT}/13 passed, ${FAIL_COUNT} failed ---"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
    echo ""
    echo -e "${YELLOW}NOTE: This is a GPU-dependent manual verification script.${NC}"
    echo "Some checks may fail if the media pipeline hasn't completed yet."
    echo "Wait 2-3 minutes and re-run."
    exit 1
fi
