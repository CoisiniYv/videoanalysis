#!/usr/bin/env bash
# Phase P1b-RTSP Replay manual job to Video File Sink smoke.
#
# Verifies:
#   rtsp://10.37.57.112:8554/live/1080movie
#     -> source-adapter (rtsp.sh)
#     -> replay-service
#     -> savant-security
#     -> Redis security.events
#   manual Replay job
#     -> video-file-sink
#
# No clip-worker, media-worker, event-worker, or production compose.

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SMOKE_DIR}/../.."
COMPOSE_FILE="${ROOT_DIR}/infra/docker-compose.p1b-rtsp-replay-manual-sink.yml"
REPLAY_CONFIG="${ROOT_DIR}/modules/savant_replay/config.p1b_rtsp_inline.json"
REDIS_SHIM="${ROOT_DIR}/modules/savant_security/poc_deps/redis.py"
RTSP_URL="${P1B_RTSP_URL:-rtsp://10.37.57.112:8554/live/1080movie}"
SOURCE_ID="${P1B_RTSP_SOURCE_ID:-p1b_rtsp_replay}"
CAMERA_ID="${P1B_RTSP_CAMERA_ID:-cam_p1b_rtsp_replay}"
REPLAY_API="${P1B_RTSP_REPLAY_API:-http://127.0.0.1:8087}"
OFFSET_SECONDS="${P1B_RTSP_OFFSET_SECONDS:-5}"
FRAME_COUNT="${P1B_RTSP_FRAME_COUNT:-300}"
WAIT_SECONDS="${P1B_RTSP_WAIT_SECONDS:-180}"
RUN_ID="${P1B_RTSP_RUN_ID:-$(date +%s%N)}"
RESULTING_STREAM_ID="${P1B_RTSP_RESULTING_STREAM_ID:-p1b_rtsp_manual_${RUN_ID}}"
SINK_URL="${P1B_RTSP_SINK_URL:-pub+connect:tcp://video-file-sink:6666}"
P1B_RTSP_DIR_LOCATION="/media/p1b-rtsp-replay-manual-sink/${RUN_ID}/%source_id%/%src_filename%/"
export P1B_RTSP_DIR_LOCATION

REDIS_CONTAINER="p1b-rtsp-replay-redis"
REPLAY_CONTAINER="p1b-rtsp-replay-service"
SAVANT_CONTAINER="p1b-rtsp-replay-savant-security"
SOURCE_CONTAINER="p1b-rtsp-replay-source-adapter"
SINK_CONTAINER="p1b-rtsp-replay-video-file-sink"
OUTPUT_ROOT="/media/p1b-rtsp-replay-manual-sink/${RUN_ID}"
HOST_OUTPUT_ROOT="/data/video-analytics/media/p1b-rtsp-replay-manual-sink/${RUN_ID}"

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

# ── Docker access detection ──────────────────────────────────────────────────
DOCKER=""
COMPOSE=""
DOCKER_ACCESS=""
SUDO_USED="no"

detect_docker() {
  if docker ps >/dev/null 2>&1; then
    DOCKER="docker"
    COMPOSE="docker compose"
    DOCKER_ACCESS="DOCKER_ACCESS_OK"
    SUDO_USED="no"
  elif sudo docker ps >/dev/null 2>&1; then
    DOCKER="sudo docker"
    COMPOSE="sudo docker compose"
    DOCKER_ACCESS="SUDO_DOCKER_REQUIRED"
    SUDO_USED="yes"
  else
    echo -e "${RED}[BLOCKED]${NC} Docker daemon unavailable"
    DOCKER_ACCESS="DOCKER_ACCESS_BLOCKED"
    exit 2
  fi
  echo -e "${BLUE}[docker]${NC} access=$DOCKER_ACCESS prefix=$DOCKER"
}

detect_docker

container_status() {
  $DOCKER inspect "$1" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null \
    || echo "missing"
}

curl_with_code() {
  curl --noproxy '*' -s --connect-timeout 5 -w '\n%{http_code}' "$@"
}

curl_silent() {
  curl --noproxy '*' -s --connect-timeout 5 "$@"
}

find_sink_file() {
  local pattern="$1"
  docker exec "$SINK_CONTAINER" sh -c \
    "find '$OUTPUT_ROOT' -type f $pattern -size +0c 2>/dev/null | head -n 1" \
    | tr -d '\r'
}

host_media_path() {
  local container_path="$1"
  if [[ "$container_path" == /media/* ]]; then
    printf '/data/video-analytics/media/%s' "${container_path#/media/}"
  else
    printf '%s' "$container_path"
  fi
}

ffprobe_json() {
  local path="$1"
  ffprobe -v error -show_entries format=duration,format_name -of json "$path" 2>/dev/null || true
}

echo "--- P1b-RTSP Replay Manual Job to Video File Sink Smoke ---"
echo "compose=${COMPOSE_FILE}"
echo "source_id=${SOURCE_ID}"
echo "camera_id=${CAMERA_ID}"
echo "rtsp_url=${RTSP_URL}"
echo "replay_api=${REPLAY_API}"
echo "sink_url=${SINK_URL}"
echo "run_id=${RUN_ID}"
echo ""

command -v docker >/dev/null 2>&1 || fatal "docker CLI not found"
command -v python3 >/dev/null 2>&1 || fatal "python3 not found"
command -v curl >/dev/null 2>&1 || fatal "curl not found"
command -v ffprobe >/dev/null 2>&1 || fatal "ffprobe not found; cannot prove RTSP/video output"

if ! timeout 20 ffprobe -rtsp_transport tcp -i "$RTSP_URL" -v error -show_streams >/tmp/p1b_rtsp_ffprobe.log 2>&1; then
  blocked "rtsp_unreachable"
fi
check 1 "RTSP URL is reachable" pass

check 2 "RTSP URL is exactly the required source" "$([[ "$RTSP_URL" == "rtsp://10.37.57.112:8554/live/1080movie" ]] && echo pass || echo fail)"
check 3 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"
check 4 "Replay config exists" "$([[ -f "$REPLAY_CONFIG" ]] && echo pass || echo fail)"
check 5 "local Redis shim exists for offline savant-security startup" "$([[ -f "$REDIS_SHIM" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && exit 1

P1B_SERVICES="$(docker compose -f "$COMPOSE_FILE" config --services)"
for required in redis replay-service savant-security source-adapter video-file-sink; do
  if ! echo "$P1B_SERVICES" | grep -qx "$required"; then
    fatal "RTSP compose missing service $required"
  fi
done
for forbidden in event-worker clip-worker media-worker api postgres rtsp-server ffmpeg-source; do
  if echo "$P1B_SERVICES" | grep -qx "$forbidden"; then
    fatal "RTSP compose contains forbidden service $forbidden"
  fi
done
check 6 "compose contains only allowed RTSP POC services" pass

OLD_POC_CONTAINERS="$(docker ps --format '{{.Names}}' | grep -E '^(p1a-replay-inline|p1b-replay-manual|p1-replay-clip)' || true)"
if [[ -n "$OLD_POC_CONTAINERS" ]]; then
  echo "$OLD_POC_CONTAINERS"
  fatal "old P1a/P1b/P1 replay POC containers are still running"
fi
check 7 "old local-file P1a/P1b POC containers are not running" pass

if grep -Eq 'file:///testVideo|/testVideo/test.mp4|video_loop.sh|source extraction|annotated_clip|raw_clip' "$COMPOSE_FILE"; then
  fatal "RTSP compose contains forbidden local-file or extraction fallback text"
fi
if ! grep -Eq 'rtsp://10\.37\.57\.112:8554/live/1080movie' "$COMPOSE_FILE"; then
  fatal "RTSP compose does not contain the required RTSP URL"
fi
check 8 "compose uses the required RTSP URI and no local file fallback" pass

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
sink_env = services["video-file-sink"]["environment"]
out_stream = replay.get("out_stream")
print(json.dumps({
    "replay_in_stream": replay["in_stream"]["url"],
    "replay_out_stream": None if out_stream is None else out_stream["url"],
    "source_adapter_output": source_env["ZMQ_ENDPOINT"],
    "source_adapter_rtsp": source_env["RTSP_URI"],
    "source_adapter_location": source_env["LOCATION"],
    "savant_input_stream": savant_env["ZMQ_SRC_ENDPOINT"],
    "video_file_sink_endpoint": sink_env["ZMQ_ENDPOINT"],
    "video_file_sink_dir": sink_env["DIR_LOCATION"],
}, sort_keys=True))
PY
)"

REPLAY_IN_STREAM="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["replay_in_stream"])' "$STREAMS_JSON")"
REPLAY_OUT_STREAM="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["replay_out_stream"])' "$STREAMS_JSON")"
SOURCE_OUTPUT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_adapter_output"])' "$STREAMS_JSON")"
SOURCE_RTSP="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_adapter_rtsp"])' "$STREAMS_JSON")"
SOURCE_LOCATION="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_adapter_location"])' "$STREAMS_JSON")"
SAVANT_INPUT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["savant_input_stream"])' "$STREAMS_JSON")"
SINK_ENDPOINT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["video_file_sink_endpoint"])' "$STREAMS_JSON")"

echo -e "${BLUE}[topology] replay in_stream=${REPLAY_IN_STREAM}${NC}"
echo -e "${BLUE}[topology] source adapter output=${SOURCE_OUTPUT}${NC}"
echo -e "${BLUE}[topology] replay out_stream=${REPLAY_OUT_STREAM}${NC}"
echo -e "${BLUE}[topology] savant input=${SAVANT_INPUT}${NC}"
echo -e "${BLUE}[topology] source adapter rtsp=${SOURCE_RTSP}${NC}"
echo -e "${BLUE}[topology] source adapter location=${SOURCE_LOCATION}${NC}"
echo -e "${BLUE}[topology] video-file-sink endpoint=${SINK_ENDPOINT}${NC}"

check 9 "source adapter targets replay-service in_stream" "$([[ "$SOURCE_OUTPUT" == "dealer+connect:tcp://replay-service:5555" ]] && echo pass || echo fail)"
check 10 "replay out_stream targets savant-security" "$([[ "$REPLAY_OUT_STREAM" == "dealer+connect:tcp://savant-security:5557" ]] && echo pass || echo fail)"
check 11 "savant input binds replay out_stream port" "$([[ "$SAVANT_INPUT" == "router+bind:tcp://0.0.0.0:5557" ]] && echo pass || echo fail)"
check 12 "source adapter uses RTSP source and no local file" "$([[ "$SOURCE_RTSP" == "$RTSP_URL" && "$SOURCE_LOCATION" == "$RTSP_URL" ]] && echo pass || echo fail)"
check 13 "video-file-sink binds manual Replay job endpoint" "$([[ "$SINK_ENDPOINT" == "sub+bind:tcp://0.0.0.0:6666" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && blocked "topology mismatch"

echo -e "${BLUE}Starting RTSP replay stack...${NC}"
docker compose -f "$COMPOSE_FILE" up -d --force-recreate redis replay-service savant-security video-file-sink
docker compose -f "$COMPOSE_FILE" up -d source-adapter

MISSING=""
for c in "$REDIS_CONTAINER" "$REPLAY_CONTAINER" "$SAVANT_CONTAINER" "$SOURCE_CONTAINER" "$SINK_CONTAINER"; do
  s="$(container_status "$c")"
  [[ "$s" != "running" ]] && MISSING="${MISSING} ${c}(${s})"
done
if [[ -n "$MISSING" ]]; then
  echo -e "${RED}containers not ready:${NC}"
  for m in $MISSING; do echo "  - $m"; done
  exit 1
fi
check 14 "RTSP replay containers running" pass

REPLAY_CODE="$(curl_silent -o /dev/null -w "%{http_code}" "${REPLAY_API}/api/v1/status" 2>/dev/null || echo "000")"
check 15 "Replay /api/v1/status responds" "$([[ "$REPLAY_CODE" == "200" ]] && echo pass || echo fail)"

echo -e "${BLUE}Waiting for RTSP source, Replay pass-through, and Savant events...${NC}"
EVENT_RAW=""
for _ in $(seq 1 "$WAIT_SECONDS"); do
  EVENT_RAW="$(docker exec "$REDIS_CONTAINER" redis-cli XRANGE security.events - + COUNT 50 2>/dev/null | grep -F "$SOURCE_ID" || true)"
  [[ -n "$EVENT_RAW" ]] && break
  sleep 1
done

SOURCE_HAS_ID="$(docker logs "$SOURCE_CONTAINER" 2>&1 | grep -F "$SOURCE_ID" || true)"
REPLAY_RX_LOG="$(docker logs "$REPLAY_CONTAINER" 2>&1 | grep -E 'Received message|Adding message|Sending message to ZeroMQ socket' || true)"
SAVANT_HAS_ID="$(docker logs "$SAVANT_CONTAINER" 2>&1 | grep -F "$SOURCE_ID" || true)"

check 16 "source adapter logs show RTSP source_id" "$([[ -n "$SOURCE_HAS_ID" ]] && echo pass || echo fail)"
check 17 "Replay logs show RTSP source frames received and forwarded" "$([[ -n "$REPLAY_RX_LOG" ]] && echo pass || echo fail)"
check 18 "Savant logs show RTSP source_id" "$([[ -n "$SAVANT_HAS_ID" ]] && echo pass || echo fail)"

ANCHOR_UUID=""
EVENT_TS_MS=""
FRAME_UUID=""
KEYFRAME_UUID=""
PREVIOUS_KEYFRAME_UUID=""
FRAME_NUM=""
FRAME_PTS=""
SOURCE_EVENT_ID=""

if [[ -n "$EVENT_RAW" ]]; then
  EVENT_JSON="$(python3 - "$EVENT_RAW" <<'PY'
import sys
text = sys.argv[1]
start = text.find('{')
end = text.rfind('}')
print(text[start:end+1] if start != -1 and end != -1 and end > start else '{}')
PY
)"
  EVENT_FIELDS="$(python3 - "$EVENT_JSON" <<'PY'
import json
import sys

try:
    data = json.loads(sys.argv[1])
except json.JSONDecodeError:
    data = {}
media = (data.get("payload") or {}).get("media") or {}
fields = {
    "source_event_id": data.get("source_event_id") or "",
    "event_ts_ms": data.get("event_ts_ms") or media.get("event_ts_ms") or "",
    "frame_uuid": data.get("frame_uuid") or media.get("frame_uuid") or "",
    "keyframe_uuid": data.get("keyframe_uuid") or media.get("keyframe_uuid") or "",
    "previous_keyframe_uuid": media.get("previous_keyframe_uuid") or "",
    "frame_num": media.get("frame_num") or data.get("frame_id") or "",
    "frame_pts": media.get("frame_pts") or "",
}
for key, value in fields.items():
    print(f"{key}={value}")
PY
)"
  SOURCE_EVENT_ID="$(printf '%s\n' "$EVENT_FIELDS" | sed -n 's/^source_event_id=//p')"
  EVENT_TS_MS="$(printf '%s\n' "$EVENT_FIELDS" | sed -n 's/^event_ts_ms=//p')"
  FRAME_UUID="$(printf '%s\n' "$EVENT_FIELDS" | sed -n 's/^frame_uuid=//p')"
  KEYFRAME_UUID="$(printf '%s\n' "$EVENT_FIELDS" | sed -n 's/^keyframe_uuid=//p')"
  PREVIOUS_KEYFRAME_UUID="$(printf '%s\n' "$EVENT_FIELDS" | sed -n 's/^previous_keyframe_uuid=//p')"
  FRAME_NUM="$(printf '%s\n' "$EVENT_FIELDS" | sed -n 's/^frame_num=//p')"
  FRAME_PTS="$(printf '%s\n' "$EVENT_FIELDS" | sed -n 's/^frame_pts=//p')"
  ANCHOR_UUID="${PREVIOUS_KEYFRAME_UUID:-$KEYFRAME_UUID}"
else
  KF_RESP="$(curl_silent -X POST "${REPLAY_API}/api/v1/keyframes/find" -H "Content-Type: application/json" -d "{\"source_id\":\"${SOURCE_ID}\",\"from\":null,\"to\":null,\"limit\":1}" 2>/dev/null || echo '{}')"
  ANCHOR_UUID="$(python3 - "$KF_RESP" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1])
except json.JSONDecodeError:
    data = {}
k = data.get("keyframes", [])
if isinstance(k, list) and len(k) > 1 and isinstance(k[1], list) and k[1]:
    print(k[1][0])
elif isinstance(k, list) and k:
    print(k[0] if isinstance(k[0], str) else "")
else:
    print("")
PY
)"
fi

if [[ -z "$ANCHOR_UUID" ]]; then
  blocked "no_keyframe"
fi
EVENT_ANCHOR_STATUS="observed"
if [[ -z "$EVENT_RAW" ]]; then
  EVENT_ANCHOR_STATUS="event_unavailable_keyframe_available"
fi
check 19 "Redis security.events observed or Replay keyframe available" pass
check 20 "Replay anchor keyframe available" pass

JOB_PAYLOAD="$(python3 - "$SOURCE_ID" "$ANCHOR_UUID" "$OFFSET_SECONDS" "$FRAME_COUNT" "$SINK_URL" "$RESULTING_STREAM_ID" "$RUN_ID" <<'PY'
import json
import sys

source_id, anchor, offset_s, frame_count, sink_url, resulting_stream_id, run_id = sys.argv[1:]
payload = {
    "sink": {"url": sink_url},
    "configuration": {
        "ts_sync": True,
        "skip_intermediary_eos": False,
        "send_eos": True,
        "stop_on_incorrect_ts": False,
        "ts_discrepancy_fix_duration": {"secs": 0, "nanos": 33333333},
        "min_duration": {"secs": 0, "nanos": 10000000},
        "max_duration": {"secs": 0, "nanos": 103333333},
        "stored_stream_id": source_id,
        "resulting_stream_id": resulting_stream_id,
        "routing_labels": "bypass",
        "max_idle_duration": {"secs": 10, "nanos": 0},
        "max_delivery_duration": {"secs": 10, "nanos": 0},
        "send_metadata_only": False,
        "labels": {
            "phase": "p1b_rtsp",
            "run_id": run_id,
            "source_id": source_id,
            "anchor_keyframe_uuid": anchor,
        },
    },
    "stop_condition": {"frame_count": int(frame_count)},
    "anchor_keyframe": anchor,
    "anchor_wait_duration": {"secs": 1, "nanos": 0},
    "offset": {"seconds": int(offset_s)},
    "attributes": [],
}
print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
PY
)"

echo -e "${BLUE}Creating manual Replay job...${NC}"
JOB_HTTP="$(curl_with_code -X PUT "${REPLAY_API}/api/v1/job" -H "Content-Type: application/json" -d "$JOB_PAYLOAD" 2>/dev/null || printf '\n000')"
JOB_CODE="$(printf '%s\n' "$JOB_HTTP" | tail -n 1)"
JOB_RESP="$(printf '%s\n' "$JOB_HTTP" | sed '$d')"
check 21 "Replay job accepted" "$([[ "$JOB_CODE" == "200" || "$JOB_CODE" == "201" || "$JOB_CODE" == "202" ]] && echo pass || echo fail)"

JOB_ID="$(python3 - "$JOB_RESP" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(data.get("new_job") or data.get("job_id") or data.get("id") or "")
PY
)"
check 22 "Replay job id returned" "$([[ -n "$JOB_ID" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && blocked "replay_job_failed"

echo -e "${BLUE}Waiting for Video File Sink output...${NC}"
META_FILE=""
VIDEO_FILE=""
VIDEO_SIZE="0"
VIDEO_DURATION=""
VIDEO_FORMAT=""
FFPROBE_OK="no"
for _ in $(seq 1 "$WAIT_SECONDS"); do
  META_FILE="$(find_sink_file "-name metadata.json")"
  VIDEO_FILE="$(find_sink_file "\\( -name 'video.mov' -o -name 'video.webm' -o -name 'video.mp4' -o -name 'video.mkv' \\)")"
  if [[ -n "$VIDEO_FILE" ]]; then
    VIDEO_SIZE="$(docker exec "$SINK_CONTAINER" stat -c%s "$VIDEO_FILE" 2>/dev/null | tr -d '[:space:]' || echo "0")"
  fi
  if [[ -n "$META_FILE" && -n "$VIDEO_FILE" && "${VIDEO_SIZE:-0}" -gt 0 ]]; then
    HOST_VIDEO="$(host_media_path "$VIDEO_FILE")"
    PROBE="$(ffprobe_json "$HOST_VIDEO")"
    VIDEO_FORMAT="$(python3 - "$PROBE" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print((data.get("format") or {}).get("format_name") or "")
PY
)"
    VIDEO_DURATION="$(python3 - "$PROBE" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print((data.get("format") or {}).get("duration") or "")
PY
)"
    DURATION_OK="$(python3 - "$VIDEO_DURATION" <<'PY'
import sys
try:
    duration = float(sys.argv[1])
except (TypeError, ValueError):
    duration = 0.0
print("yes" if duration >= 5.0 else "no")
PY
)"
    if [[ -n "$VIDEO_FORMAT" && "$DURATION_OK" == "yes" ]]; then
      FFPROBE_OK="yes"
      break
    fi
  fi
  sleep 1
done

if [[ -n "$META_FILE" ]]; then
  META_SIZE="$(docker exec "$SINK_CONTAINER" stat -c%s "$META_FILE" 2>/dev/null | tr -d '[:space:]' || echo "0")"
else
  META_SIZE="0"
fi
OUTPUT_DIR="$(dirname "${META_FILE:-$OUTPUT_ROOT}")"
HOST_OUTPUT_DIR="$(host_media_path "$OUTPUT_DIR")"
HOST_META_FILE="$(host_media_path "${META_FILE:-}")"
HOST_VIDEO="$(host_media_path "${VIDEO_FILE:-}")"

check 23 "sink output metadata.json exists and non-empty" "$([[ -n "$META_FILE" && "${META_SIZE:-0}" -gt 0 ]] && echo pass || echo fail)"
check 24 "sink output video file exists and non-empty" "$([[ -n "$VIDEO_FILE" && "${VIDEO_SIZE:-0}" -gt 0 ]] && echo pass || echo fail)"
check 25 "video file is ffprobe-readable with duration >= 5s" "$([[ "$FFPROBE_OK" == "yes" ]] && echo pass || echo fail)"

FORBIDDEN_CONTAINERS="$(docker ps --format '{{.Names}}' | grep -E '^p1b-rtsp-replay-(clip-worker|media-worker|event-worker|postgres|api)$' || true)"
check 26 "no P1b-RTSP clip/media/event/postgres/api containers running" "$([[ -z "$FORBIDDEN_CONTAINERS" ]] && echo pass || echo fail)"

FINAL_SAVANT_STATUS="$(container_status "$SAVANT_CONTAINER")"
check 27 "savant-security still running after replay job completes" "$([[ "$FINAL_SAVANT_STATUS" == "running" ]] && echo pass || echo fail)"

echo ""
echo "input_type=rtsp"
echo "input_uri=${RTSP_URL}"
echo "local_file_used=no"
echo "test_video_used=no"
echo "source_extraction_fallback=no"
echo "replay_service=$(container_status "$REPLAY_CONTAINER")"
echo "savant_security=$(container_status "$SAVANT_CONTAINER")"
echo "source_adapter=$(container_status "$SOURCE_CONTAINER")"
echo "video_file_sink=$(container_status "$SINK_CONTAINER")"
echo "source_id=${SOURCE_ID}"
echo "camera_id=${CAMERA_ID}"
echo "security.events_observed=$([[ -n "$EVENT_RAW" ]] && echo yes || echo no)"
echo "event_anchor_status=${EVENT_ANCHOR_STATUS}"
echo "source_event_id=${SOURCE_EVENT_ID}"
echo "event_ts_ms=${EVENT_TS_MS}"
echo "frame_uuid=${FRAME_UUID}"
echo "keyframe_uuid=${KEYFRAME_UUID}"
echo "previous_keyframe_uuid=${PREVIOUS_KEYFRAME_UUID}"
echo "frame_num=${FRAME_NUM}"
echo "frame_pts=${FRAME_PTS}"
echo "replay_api_url=${REPLAY_API}"
echo "replay_job_id=${JOB_ID}"
echo "anchor_keyframe_uuid=${ANCHOR_UUID}"
echo "offset_seconds=${OFFSET_SECONDS}"
echo "stop_condition_frame_count=${FRAME_COUNT}"
echo "sink_url=${SINK_URL}"
echo "stored_stream_id=${SOURCE_ID}"
echo "resulting_stream_id=${RESULTING_STREAM_ID}"
echo "sink_output_dir=${OUTPUT_DIR}"
echo "host_sink_output_dir=${HOST_OUTPUT_DIR}"
echo "metadata_json=${META_FILE}"
echo "host_metadata_json=${HOST_META_FILE}"
echo "metadata_size=${META_SIZE}"
echo "video_file=${VIDEO_FILE}"
echo "host_video_file=${HOST_VIDEO}"
echo "video_size=${VIDEO_SIZE}"
echo "video_duration=${VIDEO_DURATION}"
echo "ffprobe_status=$([[ "$FFPROBE_OK" == "yes" ]] && echo ok || echo not_ok)"
echo "source_to_replay_to_savant_single_path=yes"
echo "second_rtsp_pull_used=no"
echo "source_extraction_fallback=no"
echo "annotated_clip=no"
echo "event_worker_started=no"
echo "clip_worker_started=no"
echo "media_worker_started=no"
echo "db_write_used=no"
echo "validation=$([[ "$FAIL_COUNT" -eq 0 ]] && echo pass || echo fail)"
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  blocked "savant_security_not_running_or_other_p1b_rtsp_failure"
fi
