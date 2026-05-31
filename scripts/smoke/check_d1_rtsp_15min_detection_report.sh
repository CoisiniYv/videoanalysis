#!/usr/bin/env bash
# Phase D1 15-minute RTSP detection-only report smoke.
#
# Fixed single path:
#   rtsp://10.37.57.112:8554/live/1080movie
#     -> source-adapter
#     -> replay-service
#     -> savant-security
#     -> Redis security.events / security.face_observations
#
# No clip-worker, media-worker, video-file-sink, local file fallback,
# source extraction fallback, or second RTSP pull.

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SMOKE_DIR}/../.."
COMPOSE_FILE="${ROOT_DIR}/infra/docker-compose.d1-rtsp-15min-detection.yml"
CAMERA_CONFIG="${ROOT_DIR}/modules/savant_security/config/cameras.d1_rtsp_15min.yml"
EXPORTER="${ROOT_DIR}/scripts/debug/export_d1_detection_report.py"
OUTPUT_DIR="${ROOT_DIR}/manual-inspection/d1_15min_detection_latest"
REQUIRED_RTSP_URL="rtsp://10.37.57.112:8554/live/1080movie"
INPUT_URI="${D1_INPUT_URI:-$REQUIRED_RTSP_URL}"
SOURCE_ID="${D1_SOURCE_ID:-d1_rtsp_15min}"
CAMERA_ID="${D1_CAMERA_ID:-cam_d1_rtsp_15min}"
DURATION_SECONDS="${D1_DURATION_SECONDS:-900}"
D1_ALLOW_BUILD="${D1_ALLOW_BUILD:-0}"
REDIS_URL="${D1_REDIS_URL:-redis://127.0.0.1:6392/0}"

REDIS_CONTAINER="d1-rtsp-redis"
REPLAY_CONTAINER="d1-rtsp-replay-service"
SAVANT_CONTAINER="d1-rtsp-savant-security"
SOURCE_CONTAINER="d1-rtsp-source-adapter"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0
DOCKER=""
COMPOSE=""
DOCKER_ACCESS=""
SUDO_USED="no"
BUILD_USED="no"
PULL_USED="no"
SERVICES_STARTED=""
ACTUAL_CONTAINERS_STARTED=""
SOURCE_ADAPTER_STOPPED_AFTER_RUN="no"
COMPOSE_STARTED="no"

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
  echo "Result=BLOCKED"
  echo "Reason=$*"
  [[ -n "${DOCKER_ACCESS:-}" ]] && echo "docker_access=${DOCKER_ACCESS}"
  [[ -n "${DOCKER:-}" ]] && echo "docker_command_prefix=${DOCKER}"
  [[ -n "${COMPOSE:-}" ]] && echo "compose_command_prefix=${COMPOSE}"
  [[ -n "${SUDO_USED:-}" ]] && echo "sudo_used=${SUDO_USED}"
  exit 2
}

fatal() {
  echo -e "${RED}FATAL${NC}: $*"
  echo "Result=FAIL"
  echo "Reason=$*"
  exit 1
}

cleanup() {
  if [[ "$COMPOSE_STARTED" == "yes" ]]; then
    $COMPOSE -f "$COMPOSE_FILE" stop source-adapter >/dev/null 2>&1 || true
    $COMPOSE -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# Docker access detection. Status is one of:
#   DOCKER_ACCESS_OK
#   SUDO_DOCKER_REQUIRED
#   DOCKER_ACCESS_BLOCKED
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
    DOCKER_ACCESS="DOCKER_ACCESS_BLOCKED"
    SUDO_USED="no"
    echo "docker_access=${DOCKER_ACCESS}"
    blocked "docker_daemon_unavailable"
  fi
  echo -e "${BLUE}[docker]${NC} access=${DOCKER_ACCESS} prefix=${DOCKER}"
}

detect_docker

container_status() {
  $DOCKER inspect "$1" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['State']['Status'])" 2>/dev/null \
    || echo "missing"
}

wait_for_container_running() {
  local container="$1"
  local wait_seconds="${2:-60}"
  local status=""
  for _ in $(seq 1 "$wait_seconds"); do
    status="$(container_status "$container")"
    [[ "$status" == "running" ]] && return 0
    sleep 1
  done
  echo "$container status=${status}"
  return 1
}

summary_value() {
  local key="$1"
  python3 - "$OUTPUT_DIR/summary.json" "$key" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = data
for part in sys.argv[2].split("."):
    value = value.get(part) if isinstance(value, dict) else None
print("" if value is None else str(value).lower() if isinstance(value, bool) else value)
PY
}

echo "--- D1 RTSP 15-minute Detection Report Smoke ---"
command -v python3 >/dev/null 2>&1 || fatal "python3 not found"
command -v ffprobe >/dev/null 2>&1 || fatal "ffprobe not found"

if [[ "$INPUT_URI" != "$REQUIRED_RTSP_URL" ]]; then
  fatal "fixed_rtsp_uri_mismatch"
fi
if [[ ! "$DURATION_SECONDS" =~ ^[0-9]+$ ]] || [[ "$DURATION_SECONDS" -le 0 ]]; then
  fatal "invalid_duration_seconds"
fi

DURATION_MINUTES="$(python3 -c 'import sys; print(int(sys.argv[1]) / 60.0)' "$DURATION_SECONDS")"
echo "compose=${COMPOSE_FILE}"
echo "input_type=rtsp"
echo "input_uri=${INPUT_URI}"
echo "duration_seconds=${DURATION_SECONDS}"
echo "duration_minutes=${DURATION_MINUTES}"
echo "source_id=${SOURCE_ID}"
echo "camera_id=${CAMERA_ID}"
echo "recording_enabled=false"
echo "clip_worker_disabled=true"
echo "media_worker_disabled=true"
echo ""

if ! timeout 20 ffprobe -rtsp_transport tcp -i "$INPUT_URI" -v error -show_streams >/tmp/d1_rtsp_ffprobe.log 2>&1; then
  blocked "rtsp_unreachable"
fi
check 1 "fixed RTSP URL is reachable" pass

check 2 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"
check 3 "camera config exists" "$([[ -f "$CAMERA_CONFIG" ]] && echo pass || echo fail)"
check 4 "exporter exists" "$([[ -f "$EXPORTER" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && exit 1

D1_SERVICES="$($COMPOSE -f "$COMPOSE_FILE" config --services)"
for required in redis replay-service savant-security source-adapter; do
  if ! echo "$D1_SERVICES" | grep -qx "$required"; then
    fatal "d1_compose_missing_service_${required}"
  fi
done
for forbidden in postgres event-worker face-worker clip-worker media-worker video-file-sink metadata-sink evidence-worker api rtsp-server ffmpeg-source; do
  if echo "$D1_SERVICES" | grep -qx "$forbidden"; then
    fatal "d1_compose_contains_forbidden_service_${forbidden}"
  fi
done
check 5 "D1 compose defines detection-only services" pass

if grep -Eiq '\.mp4|file://|testVideo|test source|looping file source|video_path|video_loop\.sh|ffmpeg-source|rtsp-server' "$COMPOSE_FILE" "$CAMERA_CONFIG"; then
  fatal "local_file_or_test_video_in_d1_rtsp_path"
fi
if ! grep -Fq "$REQUIRED_RTSP_URL" "$COMPOSE_FILE"; then
  fatal "d1_compose_missing_fixed_rtsp_uri"
fi
if ! grep -Fq "$REQUIRED_RTSP_URL" "$CAMERA_CONFIG"; then
  fatal "d1_camera_config_missing_fixed_rtsp_uri"
fi
check 6 "fixed RTSP source and no local fallback" pass

RUNTIME_JSON="$($COMPOSE -f "$COMPOSE_FILE" config | python3 -c '
import json
import sys
import yaml

doc = yaml.safe_load(sys.stdin.read())
services = doc.get("services", {})
source = services["source-adapter"].get("environment", {})
savant = services["savant-security"].get("environment", {})
print(json.dumps({
    "source_id": source.get("SOURCE_ID"),
    "rtsp_uri": source.get("RTSP_URI"),
    "location": source.get("LOCATION"),
    "source_output": source.get("ZMQ_ENDPOINT"),
    "savant_input": savant.get("ZMQ_SRC_ENDPOINT"),
    "face_export": savant.get("FACE_OBSERVATION_EXPORT_ENABLED"),
    "event_exporter": savant.get("EVENT_EXPORTER"),
    "recording_enabled": savant.get("RECORDING_ENABLED"),
    "clip_worker_disabled": savant.get("CLIP_WORKER_DISABLED"),
    "media_worker_disabled": savant.get("MEDIA_WORKER_DISABLED"),
}, sort_keys=True))
')"
SOURCE_OUTPUT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_output"])' "$RUNTIME_JSON")"
SAVANT_INPUT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["savant_input"])' "$RUNTIME_JSON")"
FACE_EXPORT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["face_export"])' "$RUNTIME_JSON")"
EVENT_EXPORTER="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["event_exporter"])' "$RUNTIME_JSON")"
RECORDING_ENV="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["recording_enabled"])' "$RUNTIME_JSON")"
CLIP_DISABLED="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["clip_worker_disabled"])' "$RUNTIME_JSON")"
MEDIA_DISABLED="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["media_worker_disabled"])' "$RUNTIME_JSON")"
check 7 "source adapter targets replay-service only" "$([[ "$SOURCE_OUTPUT" == "dealer+connect:tcp://replay-service:5555" ]] && echo pass || echo fail)"
check 8 "replay targets savant-security input" "$([[ "$SAVANT_INPUT" == "router+bind:tcp://0.0.0.0:5557" ]] && echo pass || echo fail)"
check 9 "Savant exports events and face observations to Redis" "$([[ "$FACE_EXPORT" == "true" && "$EVENT_EXPORTER" == "redis" ]] && echo pass || echo fail)"
check 10 "recording workers disabled by environment" "$([[ "$RECORDING_ENV" == "false" && "$CLIP_DISABLED" == "true" && "$MEDIA_DISABLED" == "true" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && fatal "d1_static_contract_mismatch"

if [[ "$D1_ALLOW_BUILD" == "1" ]]; then
  BUILD_USED="yes"
  fatal "d1_build_not_supported"
fi
BUILD_USED="no"
PULL_USED="no"

MISSING_IMAGES=""
for image in \
  redis:7-alpine \
  ghcr.io/insight-platform/savant-replay-x86:v0.6.0 \
  ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1 \
  ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0; do
  if ! $DOCKER image inspect "$image" >/dev/null 2>&1; then
    MISSING_IMAGES="${MISSING_IMAGES} ${image}"
  fi
done
if [[ -n "$MISSING_IMAGES" ]]; then
  echo "missing_images=${MISSING_IMAGES# }"
  blocked "image_missing_and_pull_not_allowed"
fi

echo -e "${BLUE}Resetting prior D1 compose state...${NC}"
$COMPOSE -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true

echo -e "${BLUE}Starting D1 detection services without build or pull...${NC}"
$COMPOSE -f "$COMPOSE_FILE" up -d --no-build --force-recreate --pull never \
  redis replay-service savant-security
COMPOSE_STARTED="yes"
SERVICES_STARTED="redis replay-service savant-security source-adapter"

wait_for_container_running "$REDIS_CONTAINER" 60 || fatal "redis_not_running"
wait_for_container_running "$REPLAY_CONTAINER" 60 || fatal "replay_service_not_running"
wait_for_container_running "$SAVANT_CONTAINER" 60 || fatal "savant_security_not_running"
check 11 "base detection containers running" pass

$DOCKER exec "$REDIS_CONTAINER" redis-cli FLUSHDB >/dev/null

STARTED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo -e "${BLUE}Starting D1 RTSP source adapter for ${DURATION_SECONDS}s...${NC}"
$COMPOSE -f "$COMPOSE_FILE" up -d --no-build --force-recreate --pull never source-adapter
wait_for_container_running "$SOURCE_CONTAINER" 60 || fatal "source_adapter_not_running"
ACTUAL_CONTAINERS_STARTED="$($DOCKER ps --format '{{.Names}}' | grep -E '^d1-rtsp-' | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
check 12 "source adapter running" pass

sleep "$DURATION_SECONDS"

echo -e "${BLUE}Stopping D1 source adapter after run...${NC}"
$COMPOSE -f "$COMPOSE_FILE" stop source-adapter >/dev/null
SOURCE_STATUS="$(container_status "$SOURCE_CONTAINER")"
if [[ "$SOURCE_STATUS" == "exited" ]]; then
  SOURCE_ADAPTER_STOPPED_AFTER_RUN="yes"
fi
ENDED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
check 13 "source adapter stopped after run" "$([[ "$SOURCE_ADAPTER_STOPPED_AFTER_RUN" == "yes" ]] && echo pass || echo fail)"

FORBIDDEN_RUNNING="$($DOCKER ps --format '{{.Names}}' | grep -E '^d1-rtsp-(clip-worker|media-worker|video-file-sink)$' || true)"
if [[ -n "$FORBIDDEN_RUNNING" ]]; then
  echo "$FORBIDDEN_RUNNING"
  fatal "recording_service_started"
fi
check 14 "recording services were not started" pass

EVENT_STREAM_COUNT="$($DOCKER exec "$REDIS_CONTAINER" redis-cli XLEN security.events | tr -d '[:space:]')"
FACE_STREAM_COUNT="$($DOCKER exec "$REDIS_CONTAINER" redis-cli XLEN security.face_observations | tr -d '[:space:]')"
echo "redis_security_events=${EVENT_STREAM_COUNT}"
echo "redis_face_observations=${FACE_STREAM_COUNT}"

D1_REDIS_URL="$REDIS_URL" \
D1_OUTPUT_DIR="$OUTPUT_DIR" \
D1_SOURCE_ID="$SOURCE_ID" \
D1_CAMERA_ID="$CAMERA_ID" \
D1_INPUT_URI="$INPUT_URI" \
D1_DURATION_SECONDS="$DURATION_SECONDS" \
D1_STARTED_AT="$STARTED_AT" \
D1_ENDED_AT="$ENDED_AT" \
  python3 "$EXPORTER" >/tmp/d1_export_summary.json

check 15 "summary.json exists" "$([[ -f "$OUTPUT_DIR/summary.json" ]] && echo pass || echo fail)"
check 16 "people_tracks.json exists" "$([[ -f "$OUTPUT_DIR/people_tracks.json" ]] && echo pass || echo fail)"
check 17 "face_observations.json exists" "$([[ -f "$OUTPUT_DIR/face_observations.json" ]] && echo pass || echo fail)"
check 18 "report.md exists" "$([[ -f "$OUTPUT_DIR/report.md" ]] && echo pass || echo fail)"

SUMMARY_INPUT_URI="$(summary_value input_uri)"
SUMMARY_CLIP_GENERATED="$(summary_value clip_generated)"
SUMMARY_RECORDING_ENABLED="$(summary_value recording_enabled)"
SUMMARY_SOURCE_FALLBACK="$(summary_value source_extraction_fallback)"
SUMMARY_SECOND_RTSP="$(summary_value second_rtsp_pull)"
SUMMARY_LOCAL_FILE="$(summary_value local_file_used)"
SUMMARY_TEST_VIDEO="$(summary_value test_video_used)"
SUMMARY_CLIP_WORKER="$(summary_value clip_worker_started)"
SUMMARY_MEDIA_WORKER="$(summary_value media_worker_started)"
SUMMARY_VIDEO_SINK="$(summary_value video_file_sink_started)"

check 19 "summary records fixed RTSP URI" "$([[ "$SUMMARY_INPUT_URI" == "$REQUIRED_RTSP_URL" ]] && echo pass || echo fail)"
check 20 "summary records no recording or clips" "$([[ "$SUMMARY_CLIP_GENERATED" == "false" && "$SUMMARY_RECORDING_ENABLED" == "false" ]] && echo pass || echo fail)"
check 21 "summary records no fallback and no second RTSP" "$([[ "$SUMMARY_SOURCE_FALLBACK" == "false" && "$SUMMARY_SECOND_RTSP" == "false" && "$SUMMARY_LOCAL_FILE" == "false" && "$SUMMARY_TEST_VIDEO" == "false" ]] && echo pass || echo fail)"
check 22 "summary records no recording services" "$([[ "$SUMMARY_CLIP_WORKER" == "false" && "$SUMMARY_MEDIA_WORKER" == "false" && "$SUMMARY_VIDEO_SINK" == "false" ]] && echo pass || echo fail)"

BEHAVIOR_EVENT_COUNT="$(summary_value behavior_event_count)"
PEOPLE_TRACK_COUNT="$(summary_value people_track_count)"
FACE_OBSERVATION_COUNT="$(summary_value face_observation_count)"
GALLERY_HIT_COUNT="$(summary_value gallery_hit_count)"
WATCHLIST_HIT_COUNT="$(summary_value watchlist_hit_count)"
LIVE_SEARCH_HIT_COUNT="$(summary_value live_search_hit_count)"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  fatal "d1_detection_report_validation_failed"
fi

echo ""
echo "Result=PASS"
echo "docker_access=${DOCKER_ACCESS}"
echo "docker_command_prefix=${DOCKER}"
echo "compose_command_prefix=${COMPOSE}"
echo "sudo_used=${SUDO_USED}"
echo "build_used=${BUILD_USED}"
echo "pull_used=${PULL_USED}"
echo "services_started=${SERVICES_STARTED}"
echo "actual_containers_started=${ACTUAL_CONTAINERS_STARTED}"
echo "source_adapter_stopped_after_run=${SOURCE_ADAPTER_STOPPED_AFTER_RUN}"
echo "clip_worker_started=no"
echo "media_worker_started=no"
echo "video_file_sink_started=no"
echo "input_type=rtsp"
echo "input_uri=${INPUT_URI}"
echo "duration_seconds=${DURATION_SECONDS}"
echo "source_id=${SOURCE_ID}"
echo "camera_id=${CAMERA_ID}"
echo "local_file_used=no"
echo "test_video_used=no"
echo "second_rtsp_pull=no"
echo "source_extraction_fallback=no"
echo "recording_enabled=false"
echo "clip_generated=false"
echo "evidence_bundle_generated=false"
echo "behavior_event_count=${BEHAVIOR_EVENT_COUNT}"
echo "people_track_count=${PEOPLE_TRACK_COUNT}"
echo "face_observation_count=${FACE_OBSERVATION_COUNT}"
echo "gallery_hit_count=${GALLERY_HIT_COUNT}"
echo "watchlist_hit_count=${WATCHLIST_HIT_COUNT}"
echo "live_search_hit_count=${LIVE_SEARCH_HIT_COUNT}"
echo "report.md=${OUTPUT_DIR}/report.md"
echo "summary.json=${OUTPUT_DIR}/summary.json"
echo "people_tracks.csv=${OUTPUT_DIR}/people_tracks.csv"
echo "people_tracks.json=${OUTPUT_DIR}/people_tracks.json"
echo "face_observations.csv=${OUTPUT_DIR}/face_observations.csv"
echo "face_observations.json=${OUTPUT_DIR}/face_observations.json"
if [[ -f "$OUTPUT_DIR/gallery_hits.csv" ]]; then
  echo "gallery_hits.csv=${OUTPUT_DIR}/gallery_hits.csv"
  echo "gallery_hits.json=${OUTPUT_DIR}/gallery_hits.json"
else
  echo "gallery_hits.csv=not_generated_no_hits"
  echo "gallery_hits.json=not_generated_no_hits"
fi
echo "pass_count=${PASS_COUNT}"
echo "fail_count=${FAIL_COUNT}"
