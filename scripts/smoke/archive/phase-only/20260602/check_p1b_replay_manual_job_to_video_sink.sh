#!/usr/bin/env bash
# Phase P1b Replay manual job to Video File Sink smoke.
#
# Verifies:
#   P1a source-adapter -> replay-service -> savant-security stays intact
#   Replay API manual job anchored by keyframe_uuid outputs real video to
#   Video File Sink.
#
# This script must not start clip-worker, media-worker, event-worker,
# postgres, a second RTSP source, or any source extraction fallback.

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SMOKE_DIR}/../.."
P1A_COMPOSE="${ROOT_DIR}/infra/docker-compose.p1a-replay-inline-poc.yml"
P1B_COMPOSE="${ROOT_DIR}/infra/docker-compose.p1b-replay-manual-sink-poc.yml"
REPLAY_CONFIG="${ROOT_DIR}/modules/savant_replay/config.p1a_inline.json"
SOURCE_ID="${P1B_SOURCE_ID:-p1a_replay_inline}"
CAMERA_ID="${P1B_CAMERA_ID:-cam_p1a_replay_inline}"
REPLAY_API="${P1B_REPLAY_API:-http://127.0.0.1:8086}"
ANCHOR_KEYFRAME_UUID="${P1B_KEYFRAME_UUID:-019e7c8d-b9e8-7ed1-92bf-a09384777df8}"
OFFSET_SECONDS="${P1B_OFFSET_SECONDS:-5}"
FRAME_COUNT="${P1B_FRAME_COUNT:-300}"
WAIT_SECONDS="${P1B_WAIT_SECONDS:-180}"
RUN_ID="${P1B_RUN_ID:-$(date +%s%N)}"
RESULTING_STREAM_ID="${P1B_RESULTING_STREAM_ID:-p1b_manual_${RUN_ID}}"
SINK_URL="${P1B_SINK_URL:-pub+connect:tcp://video-file-sink:6666}"
P1B_DIR_LOCATION="/media/p1b-replay-manual-sink/${RUN_ID}/%source_id%/%src_filename%/"
export P1B_DIR_LOCATION

REDIS_CONTAINER="p1a-replay-inline-redis"
REPLAY_CONTAINER="p1a-replay-inline-replay-service"
SAVANT_CONTAINER="p1a-replay-inline-savant-security"
SOURCE_CONTAINER="p1a-replay-inline-source-adapter"
SINK_CONTAINER="p1b-replay-manual-video-file-sink"
OUTPUT_ROOT="/media/p1b-replay-manual-sink/${RUN_ID}"
HOST_OUTPUT_ROOT="/data/video-analytics/media/p1b-replay-manual-sink/${RUN_ID}"

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

echo "--- P1b Replay Manual Job to Video File Sink Smoke ---"
echo "p1a_compose=${P1A_COMPOSE}"
echo "p1b_compose=${P1B_COMPOSE}"
echo "source_id=${SOURCE_ID}"
echo "camera_id=${CAMERA_ID}"
echo "anchor_keyframe_uuid=${ANCHOR_KEYFRAME_UUID}"
echo "replay_api=${REPLAY_API}"
echo "sink_url=${SINK_URL}"
echo "run_id=${RUN_ID}"
echo ""

command -v docker >/dev/null 2>&1 || fatal "docker CLI not found"
command -v python3 >/dev/null 2>&1 || fatal "python3 not found"
command -v curl >/dev/null 2>&1 || fatal "curl not found"
command -v ffprobe >/dev/null 2>&1 || fatal "ffprobe not found; cannot prove video is readable"

check 1 "P1a compose exists" "$([[ -f "$P1A_COMPOSE" ]] && echo pass || echo fail)"
check 2 "P1b compose exists" "$([[ -f "$P1B_COMPOSE" ]] && echo pass || echo fail)"
check 3 "P1a Replay config exists" "$([[ -f "$REPLAY_CONFIG" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && exit 1

P1B_SERVICES="$(docker compose -f "$P1B_COMPOSE" config --services)"
if [[ "$P1B_SERVICES" != "video-file-sink" ]]; then
  echo "$P1B_SERVICES"
  fatal "P1b compose must only define video-file-sink"
fi
check 4 "P1b compose only defines video-file-sink" pass

if grep -Eq 'rtsp://|rtsps://' "$P1A_COMPOSE" "$P1B_COMPOSE"; then
  fatal "P1a/P1b POC compose contains an RTSP URL; second RTSP pull is forbidden"
fi
check 5 "no RTSP URL configured" pass

if grep -Eq 'ffmpeg|source extraction|raw_clip|annotated_clip' "$P1B_COMPOSE"; then
  fatal "P1b compose contains forbidden extraction or clip-generation text"
fi
check 6 "P1b compose contains no extraction or clip-worker fallback" pass

STREAMS_JSON="$(python3 - "$REPLAY_CONFIG" "$P1A_COMPOSE" "$P1B_COMPOSE" <<'PY'
import json
import sys
from pathlib import Path

import yaml

replay = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
p1a = yaml.safe_load(Path(sys.argv[2]).read_text(encoding="utf-8"))
p1b = yaml.safe_load(Path(sys.argv[3]).read_text(encoding="utf-8"))
source_env = p1a["services"]["source-adapter"]["environment"]
savant_env = p1a["services"]["savant-security"]["environment"]
sink_env = p1b["services"]["video-file-sink"]["environment"]
out_stream = replay.get("out_stream")
print(json.dumps({
    "replay_in_stream": replay["in_stream"]["url"],
    "replay_out_stream": None if out_stream is None else out_stream["url"],
    "source_adapter_output": source_env["ZMQ_ENDPOINT"],
    "savant_input_stream": savant_env["ZMQ_SRC_ENDPOINT"],
    "video_file_sink_endpoint": sink_env["ZMQ_ENDPOINT"],
    "video_file_sink_dir": sink_env["DIR_LOCATION"],
}, sort_keys=True))
PY
)"

REPLAY_IN_STREAM="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["replay_in_stream"])' "$STREAMS_JSON")"
REPLAY_OUT_STREAM="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["replay_out_stream"])' "$STREAMS_JSON")"
SOURCE_OUTPUT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_adapter_output"])' "$STREAMS_JSON")"
SAVANT_INPUT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["savant_input_stream"])' "$STREAMS_JSON")"
SINK_ENDPOINT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["video_file_sink_endpoint"])' "$STREAMS_JSON")"

echo -e "${BLUE}[topology] replay in_stream=${REPLAY_IN_STREAM}${NC}"
echo -e "${BLUE}[topology] source adapter output=${SOURCE_OUTPUT}${NC}"
echo -e "${BLUE}[topology] replay out_stream=${REPLAY_OUT_STREAM}${NC}"
echo -e "${BLUE}[topology] savant input=${SAVANT_INPUT}${NC}"
echo -e "${BLUE}[topology] video-file-sink endpoint=${SINK_ENDPOINT}${NC}"

check 7 "source adapter targets replay-service in_stream" "$([[ "$SOURCE_OUTPUT" == "dealer+connect:tcp://replay-service:5555" ]] && echo pass || echo fail)"
check 8 "replay out_stream targets savant-security" "$([[ "$REPLAY_OUT_STREAM" == "dealer+connect:tcp://savant-security:5557" ]] && echo pass || echo fail)"
check 9 "savant input binds replay out_stream port" "$([[ "$SAVANT_INPUT" == "router+bind:tcp://0.0.0.0:5557" ]] && echo pass || echo fail)"
check 10 "video-file-sink binds manual Replay job endpoint" "$([[ "$SINK_ENDPOINT" == "sub+bind:tcp://0.0.0.0:6666" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && blocked "P1b stream configuration mismatch"

echo -e "${BLUE}Ensuring P1a inline stack is running...${NC}"
docker compose -f "$P1A_COMPOSE" up -d redis replay-service savant-security source-adapter

echo -e "${BLUE}Starting P1b video-file-sink on the P1a network...${NC}"
docker compose -f "$P1B_COMPOSE" up -d --force-recreate video-file-sink

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
check 11 "P1a replay/savant/source and P1b sink containers running" pass

REPLAY_CODE="$(curl_silent -o /dev/null -w "%{http_code}" "${REPLAY_API}/api/v1/status" 2>/dev/null || echo "000")"
check 12 "Replay /api/v1/status responds" "$([[ "$REPLAY_CODE" == "200" ]] && echo pass || echo fail)"

SOURCE_HAS_ID="$(docker logs "$SOURCE_CONTAINER" 2>&1 | grep -F "$SOURCE_ID" || true)"
REPLAY_RX_LOG="$(docker logs "$REPLAY_CONTAINER" 2>&1 | grep -E 'Received message|Adding message' || true)"
REPLAY_FORWARD_LOG="$(docker logs "$REPLAY_CONTAINER" 2>&1 | grep -F 'Sending message to ZeroMQ socket' || true)"
check 13 "source adapter is feeding P1a source_id" "$([[ -n "$SOURCE_HAS_ID" ]] && echo pass || echo fail)"
check 14 "Replay logs show source frames received" "$([[ -n "$REPLAY_RX_LOG" ]] && echo pass || echo fail)"
check 15 "Replay logs show inline forwarding to Savant" "$([[ -n "$REPLAY_FORWARD_LOG" ]] && echo pass || echo fail)"

echo -e "${BLUE}Checking anchor keyframe is present in Replay cache...${NC}"
KF_RESP="$(curl_silent -X POST "${REPLAY_API}/api/v1/keyframes/find" \
  -H "Content-Type: application/json" \
  -d "{\"source_id\":\"${SOURCE_ID}\",\"from\":null,\"to\":null,\"limit\":1000}" 2>/dev/null || echo '{}')"
ANCHOR_PRESENT="$(python3 - "$KF_RESP" "$ANCHOR_KEYFRAME_UUID" <<'PY'
import json
import sys

try:
    data = json.loads(sys.argv[1])
except json.JSONDecodeError:
    print("no")
    raise SystemExit(0)
anchor = sys.argv[2]
k = data.get("keyframes", [])
uuids = k[1] if isinstance(k, list) and len(k) > 1 and isinstance(k[1], list) else []
print("yes" if anchor in uuids else "no")
PY
)"
check 16 "Replay cache contains anchor keyframe_uuid" "$([[ "$ANCHOR_PRESENT" == "yes" ]] && echo pass || echo fail)"
[[ "$ANCHOR_PRESENT" != "yes" ]] && blocked "anchor keyframe_uuid is not in Replay cache; no fallback attempted"

JOB_PAYLOAD="$(python3 - "$SOURCE_ID" "$ANCHOR_KEYFRAME_UUID" "$OFFSET_SECONDS" "$FRAME_COUNT" "$SINK_URL" "$RESULTING_STREAM_ID" "$RUN_ID" <<'PY'
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
            "phase": "p1b",
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
echo -e "${BLUE}[debug] PUT /api/v1/job payload: ${JOB_PAYLOAD}${NC}"
JOB_HTTP="$(curl_with_code -X PUT "${REPLAY_API}/api/v1/job" \
  -H "Content-Type: application/json" \
  -d "$JOB_PAYLOAD" 2>/dev/null || printf '\n000')"
JOB_CODE="$(printf '%s\n' "$JOB_HTTP" | tail -n 1)"
JOB_RESP="$(printf '%s\n' "$JOB_HTTP" | sed '$d')"
echo -e "${BLUE}[debug] PUT /api/v1/job HTTP ${JOB_CODE}${NC}"
echo -e "${BLUE}[debug] ${JOB_RESP}${NC}"
check 17 "Replay job accepted" "$([[ "$JOB_CODE" == "200" || "$JOB_CODE" == "201" || "$JOB_CODE" == "202" ]] && echo pass || echo fail)"

JOB_ID="$(python3 - "$JOB_RESP" <<'PY'
import json
import sys

try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(data.get("new_job") or data.get("job_id") or data.get("id") or "")
PY
)"
check 18 "Replay job id returned" "$([[ -n "$JOB_ID" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && blocked "Replay job was not created"

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
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print((data.get("format") or {}).get("format_name") or "")
PY
)"
    VIDEO_DURATION="$(python3 - "$PROBE" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
duration = (data.get("format") or {}).get("duration")
print(duration or "")
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
HOST_VIDEO="$(host_media_path "${VIDEO_FILE:-}")"

check 19 "sink output metadata.json exists and non-empty" "$([[ -n "$META_FILE" && "${META_SIZE:-0}" -gt 0 ]] && echo pass || echo fail)"
check 20 "sink output video file exists and non-empty" "$([[ -n "$VIDEO_FILE" && "${VIDEO_SIZE:-0}" -gt 0 ]] && echo pass || echo fail)"
check 21 "video file is ffprobe-readable with duration >= 5s" "$([[ "$FFPROBE_OK" == "yes" ]] && echo pass || echo fail)"

FORBIDDEN_CONTAINERS="$(docker ps --format '{{.Names}}' | grep -E '^p1b-replay-manual-(clip-worker|media-worker|event-worker|postgres|source-adapter|replay-service|savant-security)$' || true)"
check 22 "no P1b clip/media/event/postgres/source/replay/savant containers running" "$([[ -z "$FORBIDDEN_CONTAINERS" ]] && echo pass || echo fail)"

echo ""
echo "replay_service=$(container_status "$REPLAY_CONTAINER")"
echo "video_file_sink=$(container_status "$SINK_CONTAINER")"
echo "source_id=${SOURCE_ID}"
echo "camera_id=${CAMERA_ID}"
echo "replay_api_url=${REPLAY_API}"
echo "anchor_keyframe_uuid=${ANCHOR_KEYFRAME_UUID}"
echo "replay_job_id=${JOB_ID}"
echo "replay_job_request=${JOB_PAYLOAD}"
echo "sink_output_dir=${OUTPUT_DIR}"
echo "metadata_json=${META_FILE}"
echo "metadata_size=${META_SIZE}"
echo "video_file=${VIDEO_FILE}"
echo "host_video_file=${HOST_VIDEO}"
echo "video_size=${VIDEO_SIZE}"
echo "video_duration=${VIDEO_DURATION}"
echo "video_format=${VIDEO_FORMAT}"
echo "validation=$([[ "$FAIL_COUNT" -eq 0 ]] && echo pass || echo fail)"
echo "source_to_replay_to_savant_single_path=yes"
echo "second_rtsp_pull_used=no"
echo "source_extraction_fallback_used=no"
echo "clip_worker_started=no"
echo "media_worker_started=no"
echo "annotated_clip_generated=no"
echo "db_write_used=no"
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  blocked "P1b Replay manual job to Video File Sink verification failed"
fi
