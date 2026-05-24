#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Smoke test: Phase 3B — Replay Media Reliability & Contract Hardening
# ---------------------------------------------------------------------------
# PREREQ: docker compose -f infra/docker-compose.phase3b.yml up -d
# ---------------------------------------------------------------------------
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${SMOKE_DIR}/../../infra/docker-compose.phase3b.yml"
REPLAY_API="${REPLAY_API:-http://127.0.0.1:8081}"
API_BASE="${API_BASE:-http://127.0.0.1:8001}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
PASS_COUNT=0; FAIL_COUNT=0

_curl() { curl --noproxy '*' -s --connect-timeout 5 "$@"; }
_pg()   { docker exec phase3b-postgres psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null | head -n1 | tr -d '[:space:]'; }
_pg_raw() { docker exec phase3b-postgres psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null; }
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

echo "--- Phase 3B Replay Media Reliability Smoke Test ---"
echo ""

# Pre-flight
REQUIRED="phase3b-redis phase3b-postgres phase3b-api phase3b-event-worker phase3b-clip-worker phase3b-media-worker phase3b-replay-service phase3b-video-file-sink"
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
SID="smoke:phase3b:phase3b:t_media:intrusion:${UNIQ}"
EVENT_TS="$(date +%s%3N)"

echo -e "${BLUE}[debug] SID=${SID} ts=${EVENT_TS}${NC}"

EVENT_JSON=$(python3 << PYEOF
import json
media = {
    "source_id": "phase3b",
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
    "source_id": "phase3b",
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
    "description": "Phase 3B smoke test event",
    "snapshot_required": True,
    "clip_required": True,
    "payload": {"zone_id": "full_frame", "media": media},
}
print(json.dumps(event))
PYEOF
)

# ===========================================================================
# [3] Inject event and verify DB insert
# ===========================================================================
_redis XADD security.events '*' type security_event source_event_id "${SID}" event_type intrusion camera_id cam_01 source_id phase3b track_id t_media severity high data "${EVENT_JSON}" > /dev/null 2>&1
echo -e "${BLUE}[debug] Event injected into security.events${NC}"
check 3 "injected event into security.events" pass

# Wait for event-worker to process
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

# ===========================================================================
# [5] Idempotency: duplicate event = exactly one DB row + one record_request
# ===========================================================================
echo -e "${BLUE}Injecting duplicate event for idempotency check...${NC}"
_redis XADD security.events '*' type security_event source_event_id "${SID}" event_type intrusion camera_id cam_01 source_id phase3b track_id t_media severity high data "${EVENT_JSON}" > /dev/null 2>&1
sleep 8

COUNT="$(_pg "SELECT COUNT(*) FROM events WHERE source_event_id='${SID}';")"
check 5 "idempotent: count=1" "$([[ "$COUNT" == "1" ]] && echo pass || echo fail)"

# ===========================================================================
# [6] Record request exists
# ===========================================================================
RR_RAW="$(_redis XRANGE security.record_requests - + COUNT 5 2>/dev/null || echo "")"
check 6 "security.record_requests has messages" "$([[ -n "$RR_RAW" && "$RR_RAW" != *"empty"* ]] && echo pass || echo fail)"

# ===========================================================================
# [7] Clip status transitions through proper states
# ===========================================================================
# Wait for clip-worker + media pipeline (90s)
echo -e "${BLUE}Waiting for media pipeline (90s)...${NC}"
CLIP_PATH=""
CLIP_STATUS=""
for i in $(seq 1 18); do
    sleep 5
    CLIP_PATH="$(_pg "SELECT clip_path FROM events WHERE source_event_id='${SID}';")"
    CLIP_STATUS="$(_pg "SELECT payload->'media'->>'clip_status' FROM events WHERE source_event_id='${SID}';")"
    [[ -n "$CLIP_PATH" && "$CLIP_PATH" != "NULL" ]] && break
    echo -n "."
done
echo ""
echo -e "${BLUE}[debug] clip_status=${CLIP_STATUS} clip_path=${CLIP_PATH}${NC}"

check 7 "media.clip_status is ready" "$([[ "$CLIP_STATUS" == "ready" ]] && echo pass || echo fail)"

# ===========================================================================
# [8] replay_job_id stored
# ===========================================================================
REPLAY_JOB_ID="$(_pg "SELECT payload->'media'->>'replay_job_id' FROM events WHERE source_event_id='${SID}';")"
echo -e "${BLUE}[debug] replay_job_id=${REPLAY_JOB_ID}${NC}"
check 8 "replay_job_id stored" "$([[ -n "$REPLAY_JOB_ID" && "$REPLAY_JOB_ID" != "NULL" && "$REPLAY_JOB_ID" != '""' ]] && echo pass || echo fail)"

# ===========================================================================
# [9] sink_output_path stored
# ===========================================================================
SINK_PATH="$(_pg "SELECT payload->'media'->>'sink_output_path' FROM events WHERE source_event_id='${SID}';")"
echo -e "${BLUE}[debug] sink_output_path=${SINK_PATH}${NC}"
check 9 "sink_output_path stored" "$([[ -n "$SINK_PATH" && "$SINK_PATH" != "NULL" ]] && echo pass || echo fail)"

# ===========================================================================
# [10] clip_url works
# ===========================================================================
EVENT_RESP="$(_curl "${API_BASE}/api/v1/events/${SID}")"
CLIP_URL="$(echo "$EVENT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('clip_url','') or '')" 2>/dev/null)"
echo -e "${BLUE}[debug] clip_url=${CLIP_URL}${NC}"

if [[ -n "$CLIP_URL" && "$CLIP_URL" != "None" ]]; then
    CLIP_HTTP="$(_curl -o /dev/null -w '%{http_code}' "${API_BASE}${CLIP_URL}")"
    echo -e "${BLUE}[debug] curl ${API_BASE}${CLIP_URL} HTTP ${CLIP_HTTP}${NC}"
    check 10 "API returns clip_url and curl 200" "$([[ "$CLIP_HTTP" == "200" ]] && echo pass || echo fail)"
else
    check 10 "API returns clip_url" fail
fi

# ===========================================================================
# [11] API media fields exposed
# ===========================================================================
MEDIA_REC_STRATEGY="$(echo "$EVENT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('media',{}).get('recording_strategy',''))" 2>/dev/null)"
MEDIA_CLIP_STATUS="$(echo "$EVENT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('media',{}).get('clip_status',''))" 2>/dev/null)"
MEDIA_SNAP_STATUS="$(echo "$EVENT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('media',{}).get('snapshot_status',''))" 2>/dev/null)"
MEDIA_JOB_ID="$(echo "$EVENT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('media',{}).get('replay_job_id','') or '')" 2>/dev/null)"
echo -e "${BLUE}[debug] media: rec_strategy=${MEDIA_REC_STRATEGY} clip_status=${MEDIA_CLIP_STATUS} snap_status=${MEDIA_SNAP_STATUS} job_id=${MEDIA_JOB_ID}${NC}"

check 11 "API exposes media.recording_strategy" "$([[ "$MEDIA_REC_STRATEGY" == "savant_replay" ]] && echo pass || echo fail)"
check 12 "API exposes media.clip_status" "$([[ "$MEDIA_CLIP_STATUS" == "ready" ]] && echo pass || echo fail)"
check 13 "API exposes media.replay_job_id" "$([[ -n "$MEDIA_JOB_ID" && "$MEDIA_JOB_ID" != "None" && "$MEDIA_JOB_ID" != '""' ]] && echo pass || echo fail)"

# ===========================================================================
# [14] Media-worker idempotency: re-run does not corrupt clip_path
# ===========================================================================
# Restart media-worker to force re-scan
docker restart phase3b-media-worker > /dev/null 2>&1 || true
sleep 15

CLIP_PATH_AFTER="$(_pg "SELECT clip_path FROM events WHERE source_event_id='${SID}';")"
check 14 "media-worker re-scan idempotent (clip_path unchanged)" "$([[ "$CLIP_PATH" == "$CLIP_PATH_AFTER" ]] && echo pass || echo fail)"

# ===========================================================================
# [15] Failed path: inject event with non-existent source_id → marks failed
# ===========================================================================
UNIQ_FAIL="$(date +%s%N)"
SID_FAIL="smoke:phase3b:bad:t_fail:intrusion:${UNIQ_FAIL}"
EVENT_TS_FAIL="$(date +%s%3N)"

EVENT_FAIL_JSON=$(python3 << PYEOF
import json
media = {
    "source_id": "nonexistent_source_xyz",
    "clip_required": True,
    "recording_strategy": "savant_replay",
    "event_ts_ms": ${EVENT_TS_FAIL},
}
event = {
    "schema_version": "1.0",
    "source_event_id": "${SID_FAIL}",
    "producer": "savant_phase2c",
    "event_type": "intrusion",
    "camera_id": "cam_01",
    "source_id": "nonexistent_source_xyz",
    "track_id": "t_fail",
    "event_ts_ms": ${EVENT_TS_FAIL},
    "confidence": 0.9,
    "severity": "high",
    "clip_required": True,
    "payload": {"media": media},
}
print(json.dumps(event))
PYEOF
)

_redis XADD security.events '*' type security_event source_event_id "${SID_FAIL}" event_type intrusion camera_id cam_01 source_id nonexistent_source_xyz track_id t_fail severity high data "${EVENT_FAIL_JSON}" > /dev/null 2>&1

echo -e "${BLUE}Waiting for failed path (30s)...${NC}"
sleep 10
for i in $(seq 1 6); do
    sleep 5
    FAIL_CLIP_STATUS="$(_pg "SELECT payload->'media'->>'clip_status' FROM events WHERE source_event_id='${SID_FAIL}';")"
    echo -e "${BLUE}[debug] fail_clip_status=${FAIL_CLIP_STATUS}${NC}"
    [[ "$FAIL_CLIP_STATUS" == "failed" ]] && break
done

FAIL_DB_ID="$(_pg "SELECT id FROM events WHERE source_event_id='${SID_FAIL}';")"
FAIL_CLIP_STATUS_FINAL="$(_pg "SELECT payload->'media'->>'clip_status' FROM events WHERE source_event_id='${SID_FAIL}';")"
FAIL_ERROR_MSG="$(_pg "SELECT payload->'media'->>'error_message' FROM events WHERE source_event_id='${SID_FAIL}';")"
echo -e "${BLUE}[debug] fail: db_id=${FAIL_DB_ID} clip_status=${FAIL_CLIP_STATUS_FINAL} error=${FAIL_ERROR_MSG}${NC}"

check 15 "failed event still inserted in DB" "$([[ -n "$FAIL_DB_ID" ]] && echo pass || echo fail)"
check 16 "failed event has clip_status=failed" "$([[ "$FAIL_CLIP_STATUS_FINAL" == "failed" ]] && echo pass || echo fail)"
check 17 "failed event has error_message" "$([[ -n "$FAIL_ERROR_MSG" && "$FAIL_ERROR_MSG" != "NULL" ]] && echo pass || echo fail)"

# ===========================================================================
# [18] Idempotency: re-processing same event does not change clip_status
# ===========================================================================
if [[ -n "$FAIL_DB_ID" ]]; then
    _redis XADD security.events '*' type security_event source_event_id "${SID_FAIL}" event_type intrusion camera_id cam_01 source_id nonexistent_source_xyz track_id t_fail severity high data "${EVENT_FAIL_JSON}" > /dev/null 2>&1
    sleep 8
    FAIL_COUNT="$(_pg "SELECT COUNT(*) FROM events WHERE source_event_id='${SID_FAIL}';")"
    check 18 "failed event still count=1 after duplicate" "$([[ "$FAIL_COUNT" == "1" ]] && echo pass || echo fail)"
fi

echo ""
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"
[[ "$FAIL_COUNT" -gt 0 ]] && exit 1
