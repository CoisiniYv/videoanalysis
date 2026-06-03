#!/usr/bin/env bash
# Phase P1a Replay inline pass-through smoke.
#
# Verifies the single path:
#   source-adapter -> replay-service -> savant-security -> Redis
#
# This script must not start video-file-sink, clip-worker, media-worker,
# a second RTSP source, or any source extraction fallback.

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SMOKE_DIR}/../.."
COMPOSE_FILE="${ROOT_DIR}/infra/docker-compose.p1a-replay-inline-poc.yml"
REPLAY_CONFIG="${ROOT_DIR}/modules/savant_replay/config.p1a_inline.json"
SOURCE_ID="${P1A_SOURCE_ID:-p1a_replay_inline}"
REPLAY_API="${P1A_REPLAY_API:-http://127.0.0.1:8086}"
WAIT_SECONDS="${P1A_WAIT_SECONDS:-180}"

REDIS_CONTAINER="p1a-replay-inline-redis"
REPLAY_CONTAINER="p1a-replay-inline-replay-service"
SAVANT_CONTAINER="p1a-replay-inline-savant-security"
SOURCE_CONTAINER="p1a-replay-inline-source-adapter"

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

fatal() {
  echo -e "${RED}FATAL${NC}: $*"
  exit 1
}

container_status() {
  docker inspect "$1" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null \
    || echo "missing"
}

redis_cli() {
  docker exec "$REDIS_CONTAINER" redis-cli "$@" 2>/dev/null
}

curl_silent() {
  curl --noproxy '*' -s --connect-timeout 5 "$@"
}

echo "--- P1a Replay Inline Pass-through Smoke ---"
echo "compose=${COMPOSE_FILE}"
echo "source_id=${SOURCE_ID}"
echo ""

command -v docker >/dev/null 2>&1 || fatal "docker CLI not found"
command -v python3 >/dev/null 2>&1 || fatal "python3 not found"
command -v curl >/dev/null 2>&1 || fatal "curl not found"

check 1 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"
check 2 "P1a Replay config exists" "$([[ -f "$REPLAY_CONFIG" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && exit 1

SERVICES="$(docker compose -f "$COMPOSE_FILE" config --services)"
FORBIDDEN_REGEX='(^|[[:space:]])(video-file-sink|clip-worker|media-worker|event-worker|metadata-sink|postgres|rtsp-server|ffmpeg-source)($|[[:space:]])'
if echo "$SERVICES" | grep -Eq "$FORBIDDEN_REGEX"; then
  echo "$SERVICES"
  fatal "P1a compose contains a forbidden service"
fi
check 3 "compose contains no forbidden P1 clip/sink/source services" pass

if grep -Eq 'rtsp://|rtsps://' "$COMPOSE_FILE"; then
  fatal "P1a compose contains an RTSP URL; second RTSP pull is forbidden"
fi
check 4 "no RTSP URL configured" pass

STREAMS_JSON="$(python3 - "$REPLAY_CONFIG" "$COMPOSE_FILE" <<'PY'
import json
import sys
from pathlib import Path

import yaml

replay = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
compose = yaml.safe_load(Path(sys.argv[2]).read_text(encoding="utf-8"))
services = compose["services"]
source_env = services["source-adapter"]["environment"]
savant_env = services["savant-security"]["environment"]
out_stream = replay.get("out_stream")
print(json.dumps({
    "replay_in_stream": replay["in_stream"]["url"],
    "replay_out_stream": None if out_stream is None else out_stream["url"],
    "source_adapter_output": source_env["ZMQ_ENDPOINT"],
    "savant_input_stream": savant_env["ZMQ_SRC_ENDPOINT"],
}, sort_keys=True))
PY
)"

REPLAY_IN_STREAM="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["replay_in_stream"])' "$STREAMS_JSON")"
REPLAY_OUT_STREAM="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["replay_out_stream"])' "$STREAMS_JSON")"
SOURCE_OUTPUT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_adapter_output"])' "$STREAMS_JSON")"
SAVANT_INPUT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["savant_input_stream"])' "$STREAMS_JSON")"

echo -e "${BLUE}[topology] replay in_stream=${REPLAY_IN_STREAM}${NC}"
echo -e "${BLUE}[topology] source adapter output=${SOURCE_OUTPUT}${NC}"
echo -e "${BLUE}[topology] replay out_stream=${REPLAY_OUT_STREAM}${NC}"
echo -e "${BLUE}[topology] savant input=${SAVANT_INPUT}${NC}"

check 5 "source adapter targets replay-service in_stream" "$([[ "$SOURCE_OUTPUT" == "dealer+connect:tcp://replay-service:5555" ]] && echo pass || echo fail)"
check 6 "replay out_stream is non-null" "$([[ "$REPLAY_OUT_STREAM" != "None" && -n "$REPLAY_OUT_STREAM" ]] && echo pass || echo fail)"
check 7 "replay out_stream targets savant-security" "$([[ "$REPLAY_OUT_STREAM" == "dealer+connect:tcp://savant-security:5557" ]] && echo pass || echo fail)"
check 8 "savant input binds replay out_stream port" "$([[ "$SAVANT_INPUT" == "router+bind:tcp://0.0.0.0:5557" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && blocked "inline stream configuration mismatch"

echo -e "${BLUE}Starting P1a redis/replay/savant...${NC}"
docker compose -f "$COMPOSE_FILE" stop source-adapter >/dev/null 2>&1 || true
docker compose -f "$COMPOSE_FILE" up -d --force-recreate redis replay-service savant-security

for _ in $(seq 1 30); do
  [[ "$(container_status "$REDIS_CONTAINER")" == "running" ]] && break
  sleep 1
done
[[ "$(container_status "$REDIS_CONTAINER")" == "running" ]] || fatal "redis did not start"
redis_cli DEL security.events >/dev/null || true
check 9 "Redis stream cleared before source start" pass

echo -e "${BLUE}Starting source adapter after Replay and Savant are up...${NC}"
docker compose -f "$COMPOSE_FILE" up -d source-adapter

MISSING=""
for c in "$REDIS_CONTAINER" "$REPLAY_CONTAINER" "$SAVANT_CONTAINER" "$SOURCE_CONTAINER"; do
  s="$(container_status "$c")"
  [[ "$s" != "running" ]] && MISSING="${MISSING} ${c}(${s})"
done
if [[ -n "$MISSING" ]]; then
  echo -e "${RED}containers not ready:${NC}"
  for m in $MISSING; do echo "  - $m"; done
  exit 1
fi
check 10 "P1a containers running" pass

REPLAY_CODE="$(curl_silent -o /dev/null -w "%{http_code}" "${REPLAY_API}/api/v1/status" 2>/dev/null || echo "000")"
check 11 "Replay /api/v1/status responds" "$([[ "$REPLAY_CODE" == "200" ]] && echo pass || echo fail)"

echo -e "${BLUE}Waiting for Replay input, pass-through, and Savant Redis output...${NC}"
EVENT_RAW=""
for _ in $(seq 1 "$WAIT_SECONDS"); do
  EVENT_RAW="$(redis_cli XRANGE security.events - + COUNT 50 | grep "$SOURCE_ID" || true)"
  [[ -n "$EVENT_RAW" ]] && break
  sleep 1
done

SOURCE_HAS_ID="$(docker logs "$SOURCE_CONTAINER" 2>&1 | grep -F "$SOURCE_ID" || true)"
REPLAY_RX_LOG="$(docker logs "$REPLAY_CONTAINER" 2>&1 | grep -E 'Received message|Adding message|mismatched routing_id' || true)"
REPLAY_MISSING_WRITER="$(docker logs "$REPLAY_CONTAINER" 2>&1 | grep -F 'No output writer, skipping' || true)"
SAVANT_HAS_ID="$(docker logs "$SAVANT_CONTAINER" 2>&1 | grep -F "$SOURCE_ID" || true)"

check 12 "source adapter started with P1a source_id" "$([[ -n "$SOURCE_HAS_ID" ]] && echo pass || echo fail)"
check 13 "Replay logs show source messages received" "$([[ -n "$REPLAY_RX_LOG" ]] && echo pass || echo fail)"
check 14 "Replay logs do not report missing output writer" "$([[ -z "$REPLAY_MISSING_WRITER" ]] && echo pass || echo fail)"
check 15 "Savant logs show P1a source_id" "$([[ -n "$SAVANT_HAS_ID" ]] && echo pass || echo fail)"
check 16 "Redis security.events observed for P1a source_id" "$([[ -n "$EVENT_RAW" ]] && echo pass || echo fail)"

FORBIDDEN_CONTAINERS="$(docker ps --format '{{.Names}}' | grep -E '^p1a-replay-inline-(video-file-sink|clip-worker|media-worker)$' || true)"
check 17 "no P1a video-file-sink/clip-worker/media-worker containers running" "$([[ -z "$FORBIDDEN_CONTAINERS" ]] && echo pass || echo fail)"

echo ""
echo "replay_input_stream=${REPLAY_IN_STREAM}"
echo "source_adapter_output=${SOURCE_OUTPUT}"
echo "replay_out_stream=${REPLAY_OUT_STREAM}"
echo "savant_input_stream=${SAVANT_INPUT}"
echo "source_id=${SOURCE_ID}"
echo "redis_event_observed=$([[ -n "$EVENT_RAW" ]] && echo yes || echo no)"
echo "second_rtsp_pull_used=no"
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  blocked "P1a inline pass-through runtime verification failed"
fi
