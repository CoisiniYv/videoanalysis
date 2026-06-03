#!/usr/bin/env bash
# Phase P1 single-stream Replay raw clip evidence smoke.
#
# This smoke must return BLOCKED rather than PASS when the required inline
# source-adapter -> replay-service -> savant-security topology is unavailable.
# It must not use source extraction fallback or a second RTSP/source pull.

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SMOKE_DIR}/../.."
COMPOSE_FILE="${ROOT_DIR}/infra/docker-compose.p1-replay-clip-poc.yml"
REPLAY_CONFIG="${ROOT_DIR}/modules/savant_replay/config.json"
REPLAY_API="${REPLAY_API:-http://127.0.0.1:8082}"
SOURCE_ID="${P1_SOURCE_ID:-p1_single_stream}"
PG_CONTAINER="p1-replay-clip-postgres"
REDIS_CONTAINER="p1-replay-clip-redis"
SINK_CONTAINER="p1-replay-clip-video-file-sink"
MEDIA_CONTAINER="p1-replay-clip-media-worker"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
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

blocked() {
  echo -e "${YELLOW}BLOCKED${NC}: $*"
  exit 2
}

_curl() { curl --noproxy '*' -s --connect-timeout 5 "$@"; }
_pg() {
  docker exec "$PG_CONTAINER" psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null | head -n1 | tr -d '[:space:]'
}
_redis() { docker exec "$REDIS_CONTAINER" redis-cli "$@" 2>/dev/null; }

echo "--- P1 Single-Stream Replay Clip Output Smoke ---"
echo ""

check 1 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"
check 2 "replay config exists" "$([[ -f "$REPLAY_CONFIG" ]] && echo pass || echo fail)"

OUT_STREAM="$(python3 - "$REPLAY_CONFIG" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
print("null" if data.get("out_stream") is None else "configured")
PY
)"

if [[ "$OUT_STREAM" == "null" ]]; then
  echo -e "${YELLOW}[blocker] Replay config out_stream is null.${NC}"
  echo "Required P1 topology is source-adapter -> replay-service -> savant-security."
  echo "Current repo cannot prove the inline replay pass-through hop."
  echo "No source extraction fallback or second RTSP/source pull was attempted."
  blocked "replay inline topology"
fi

echo -e "${BLUE}Starting P1 POC compose...${NC}"
docker compose -f "$COMPOSE_FILE" up -d --build \
  redis postgres api event-worker clip-worker media-worker replay-service video-file-sink savant-security source-adapter

REQUIRED="p1-replay-clip-redis p1-replay-clip-postgres p1-replay-clip-event-worker p1-replay-clip-clip-worker p1-replay-clip-media-worker p1-replay-clip-replay-service p1-replay-clip-video-file-sink p1-replay-clip-savant-security p1-replay-clip-source-adapter"
MISSING=""
for c in $REQUIRED; do
  S="$(docker inspect "$c" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null || echo "missing")"
  [[ "$S" != "running" ]] && MISSING="$MISSING $c($S)"
done
if [[ -n "$MISSING" ]]; then
  echo -e "${RED}containers not ready:${NC}"
  for m in $MISSING; do echo "  - $m"; done
  exit 1
fi
check 3 "POC containers running" pass

REPLAY_CODE="$(_curl -o /dev/null -w "%{http_code}" "${REPLAY_API}/api/v1/status" 2>/dev/null || echo "000")"
check 4 "Replay /api/v1/status 200" "$([[ "$REPLAY_CODE" == "200" ]] && echo pass || echo fail)"

echo -e "${BLUE}Clearing old P1 stream/output state...${NC}"
_redis DEL security.events security.record_requests >/dev/null || true
docker exec "$SINK_CONTAINER" sh -c "rm -rf /media/replay-sink-output/p1" >/dev/null 2>&1 || true
_pg "DELETE FROM events WHERE source_id='${SOURCE_ID}' OR source_event_id LIKE 'p1:%';" >/dev/null || true
check 5 "old P1 state cleared" pass

echo -e "${BLUE}Waiting for a real intrusion event from Savant...${NC}"
SID=""
for _ in $(seq 1 36); do
  sleep 5
  SID="$(_pg "SELECT source_event_id FROM events WHERE source_id='${SOURCE_ID}' AND event_type='intrusion' ORDER BY created_at DESC LIMIT 1;")"
  [[ -n "$SID" ]] && break
done
check 6 "intrusion event inserted" "$([[ -n "$SID" ]] && echo pass || echo fail)"
[[ -z "$SID" ]] && exit 1

EVENT_ID="$(_pg "SELECT id FROM events WHERE source_event_id='${SID}';")"
FRAME_UUID="$(_pg "SELECT frame_uuid FROM events WHERE source_event_id='${SID}';")"
KEYFRAME_UUID="$(_pg "SELECT keyframe_uuid FROM events WHERE source_event_id='${SID}';")"
echo -e "${BLUE}[debug] event_id=${EVENT_ID} source_event_id=${SID} frame_uuid=${FRAME_UUID} keyframe_uuid=${KEYFRAME_UUID}${NC}"

RR_RAW="$(_redis XRANGE security.record_requests - + COUNT 10 || true)"
check 7 "record_request published" "$([[ "$RR_RAW" == *"$EVENT_ID"* ]] && echo pass || echo fail)"

echo -e "${BLUE}Waiting for Replay job request...${NC}"
JOB_ID=""
for _ in $(seq 1 18); do
  sleep 5
  JOB_ID="$(_pg "SELECT payload->'media'->>'replay_job_id' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  [[ -n "$JOB_ID" && "$JOB_ID" != "NULL" ]] && break
done
check 8 "Replay job created" "$([[ -n "$JOB_ID" && "$JOB_ID" != "NULL" ]] && echo pass || echo fail)"

echo -e "${BLUE}Waiting for video-file-sink output and evidence bundle...${NC}"
RAW_CLIP=""
for _ in $(seq 1 24); do
  sleep 5
  RAW_CLIP="$(_pg "SELECT clip_path FROM events WHERE id='${EVENT_ID}'::uuid;")"
  [[ -n "$RAW_CLIP" && "$RAW_CLIP" != "NULL" ]] && break
done
CLIP_STATUS="$(_pg "SELECT payload->'media'->>'clip_status' FROM events WHERE id='${EVENT_ID}'::uuid;")"
EVIDENCE_DIR="$(_pg "SELECT payload->'media'->>'evidence_dir' FROM events WHERE id='${EVENT_ID}'::uuid;")"
REC_STRATEGY="$(_pg "SELECT payload->'media'->>'recording_strategy' FROM events WHERE id='${EVENT_ID}'::uuid;")"

check 9 "events.clip_path non-empty" "$([[ -n "$RAW_CLIP" && "$RAW_CLIP" != "NULL" ]] && echo pass || echo fail)"
check 10 "payload.media.clip_status=generated" "$([[ "$CLIP_STATUS" == "generated" ]] && echo pass || echo fail)"
check 11 "payload.media.recording_strategy=savant_replay" "$([[ "$REC_STRATEGY" == "savant_replay" ]] && echo pass || echo fail)"
check 12 "raw_clip file non-empty" "$(docker exec "$MEDIA_CONTAINER" test -s "$RAW_CLIP" && echo pass || echo fail)"
check 13 "event_annotation.json exists" "$(docker exec "$MEDIA_CONTAINER" test -f "${EVIDENCE_DIR}/event_annotation.json" && echo pass || echo fail)"
check 14 "metadata.json exists" "$(docker exec "$MEDIA_CONTAINER" test -f "${EVIDENCE_DIR}/metadata.json" && echo pass || echo fail)"
check 15 "annotated_clip absent" "$(docker exec "$MEDIA_CONTAINER" test ! -e "${EVIDENCE_DIR}/annotated_clip.mp4" && echo pass || echo fail)"

echo ""
echo "event_id=${EVENT_ID}"
echo "replay_job_id=${JOB_ID}"
echo "evidence_dir=${EVIDENCE_DIR}"
echo "raw_clip=${RAW_CLIP}"
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

[[ "$FAIL_COUNT" -gt 0 ]] && exit 1
