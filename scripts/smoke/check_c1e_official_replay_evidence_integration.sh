#!/usr/bin/env bash
# Phase C1E official dev Replay evidence integration smoke.
#
# Verifies the single path:
#   rtsp://10.37.57.112:8554/live/1080movie
#     -> source-adapter
#     -> replay-service
#     -> savant-security
#     -> Redis security.events
#     -> event-worker -> PostgreSQL events -> security.record_requests
#     -> clip-worker -> Replay job
#     -> video-file-sink
#     -> media-worker P1 raw finalizer
#     -> /media/evidence/{event_id}/raw_clip.*
#
# Dev-only. No production compose, source extraction fallback, second RTSP
# pull, local file source, or annotated_clip generation.

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SMOKE_DIR}/../.."
COMPOSE_FILE="${ROOT_DIR}/infra/docker-compose.c1-official-replay-dev.yml"
OLD_C1_COMPOSE="${ROOT_DIR}/infra/docker-compose.c1-official-adapter.yml"
P1C_COMPOSE="${ROOT_DIR}/infra/docker-compose.p1c-rtsp-replay-event-evidence.yml"
D1_COMPOSE="${ROOT_DIR}/infra/docker-compose.d1-rtsp-15min-detection.yml"
REPLAY_CONFIG="${ROOT_DIR}/modules/savant_replay/config.p1c_rtsp_inline.json"
CAMERA_CONFIG="${ROOT_DIR}/modules/savant_security/config/cameras.c1e_replay.yml"
REQUIRED_RTSP_URL="rtsp://10.37.57.112:8554/live/1080movie"
RTSP_URL="${C1E_RTSP_URL:-$REQUIRED_RTSP_URL}"
SOURCE_ID="${C1E_SOURCE_ID:-c1e_rtsp_replay}"
CAMERA_ID="${C1E_CAMERA_ID:-cam_c1e_rtsp_replay}"
REPLAY_API="${C1E_REPLAY_API:-http://127.0.0.1:8088}"
WAIT_SECONDS="${C1E_WAIT_SECONDS:-240}"
RUN_ID="${C1E_RUN_ID:-$(date +%s%N)}"
C1E_RUN_ID="$RUN_ID"
C1E_ALLOW_BUILD="${C1E_ALLOW_BUILD:-0}"
PRE_SECONDS=5
POST_SECONDS=5
SCHEDULING_MARGIN_SECONDS=10
max_record_requests=1
max_replay_jobs=1
max_evidence_bundles=1
EVIDENCE_ROOT="/data/video-analytics/media/evidence"
SINK_RUN_ROOT="/data/video-analytics/media/replay-sink-output/c1e/${RUN_ID}"
C1E_SINK_ROOT="/media/replay-sink-output/c1e/${RUN_ID}"
C1E_SINK_DIR_LOCATION="/media/replay-sink-output/c1e/${RUN_ID}/%source_id%/%src_filename%/"
export C1E_RUN_ID
export C1E_SINK_ROOT
export C1E_SINK_DIR_LOCATION

REDIS_CONTAINER="c1-official-redis"
PG_CONTAINER="c1-official-postgres"
REPLAY_CONTAINER="c1-official-replay-service"
SAVANT_CONTAINER="c1-official-savant"
SOURCE_CONTAINER="c1-official-source-adapter"
SINK_CONTAINER="c1-official-video-file-sink"
EVENT_WORKER_CONTAINER="c1-official-event-worker"
CLIP_WORKER_CONTAINER="c1-official-clip-worker"
MEDIA_WORKER_CONTAINER="c1-official-media-worker"

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
BIND_MOUNT_STATUS="not_checked"
WORKER_REBUILD_REQUIRED="unknown"
WORKER_RESTART_REQUIRED="unknown"
SERVICES_RESTARTED=""
ACTUAL_CONTAINERS_STARTED=""
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
  [[ -n "${ACTUAL_CONTAINERS_STARTED:-}" ]] && echo "actual_containers_started=${ACTUAL_CONTAINERS_STARTED}"
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

_pg() {
  $DOCKER exec "$PG_CONTAINER" psql -q -U video -d video_analytics -t -A -c "$1" 2>/dev/null \
    | head -n1 \
    | sed 's/[[:space:]]*$//'
}

_redis() {
  $DOCKER exec "$REDIS_CONTAINER" redis-cli "$@" 2>/dev/null || true
}

curl_silent() {
  curl --noproxy '*' -s --connect-timeout 5 "$@"
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
  if [[ -z "$path" || ! -f "$path" ]]; then
    printf '{}'
    return 0
  fi
  ffprobe -v error -show_entries format=duration,format_name -of json "$path" 2>/dev/null || true
}

cleanup_c1e_evidence_dirs() {
  python3 - "$EVIDENCE_ROOT" "$CAMERA_ID" <<'PY'
import json
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
camera_id = sys.argv[2]
if not root.exists():
    raise SystemExit(0)
for path in root.iterdir():
    if not path.is_dir():
        continue
    annotation = path / "event_annotation.json"
    if not annotation.exists():
        continue
    try:
        data = json.loads(annotation.read_text(encoding="utf-8"))
    except Exception:
        continue
    event = data.get("event", {}) if isinstance(data, dict) else {}
    if isinstance(event, dict) and event.get("camera_id") == camera_id:
        shutil.rmtree(path, ignore_errors=True)
PY
}

count_c1e_evidence_dirs() {
  python3 - "$EVIDENCE_ROOT" "$RUN_ID" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
run_id = sys.argv[2]
count = 0
if root.exists():
    for path in root.iterdir():
        if not path.is_dir():
            continue
        metadata = path / "metadata.json"
        if not metadata.exists():
            continue
        try:
            data = json.loads(metadata.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("run_id") == run_id:
            count += 1
print(count)
PY
}

assert_bind_mounts_configured() {
  python3 - "$COMPOSE_FILE" "$OLD_C1_COMPOSE" <<'PY'
import sys
from pathlib import Path

import yaml

compose = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
old = yaml.safe_load(Path(sys.argv[2]).read_text(encoding="utf-8"))
expected = {
    "savant-security": "../modules/savant_security:/opt/savant/src/module:rw",
    "event-worker": "../services/event-worker:/app:rw",
    "clip-worker": "../services/clip-worker:/app:rw",
    "media-worker": "../services/media-worker:/app:rw",
}
missing = []
for service, mount in expected.items():
    volumes = compose["services"].get(service, {}).get("volumes", []) or []
    if mount not in volumes:
        missing.append(f"c1e:{service}:{mount}")
adapter_expected = {
    "savant-security": "../modules/savant_security:/opt/savant/src/module:rw",
    "event-worker": "../services/event-worker:/app:rw",
    "face-worker": "../services/face-worker:/app:rw",
    "api": "../services/api:/app:rw",
}
for service, mount in adapter_expected.items():
    volumes = old["services"].get(service, {}).get("volumes", []) or []
    if mount not in volumes:
        missing.append(f"adapter:{service}:{mount}")
if missing:
    print("\n".join(missing))
    raise SystemExit(1)
PY
}

ensure_worker_images_available_or_build_allowed() {
  local missing=""
  for image in \
    c1-official-event-worker:latest \
    p1c-rtsp-replay-clip-worker:latest \
    p1c-rtsp-replay-media-worker:latest; do
    if ! $DOCKER image inspect "$image" >/dev/null 2>&1; then
      missing="${missing} ${image}"
    fi
  done

  if [[ -n "$missing" && "$C1E_ALLOW_BUILD" != "1" ]]; then
    echo "missing_worker_images=${missing# }"
    echo "Hint=rerun with C1E_ALLOW_BUILD=1 or enable worker bind mounts"
    blocked "worker_image_missing_and_build_not_allowed"
  fi
}

verify_bind_mount_hash() {
  local label="$1"
  local container="$2"
  local host_file="$3"
  local container_file="$4"
  local host_hash=""
  local container_hash=""

  host_hash="$(sha256sum "${ROOT_DIR}/${host_file}" | awk '{print $1}')"
  container_hash="$($DOCKER exec "$container" sha256sum "$container_file" 2>/dev/null | awk '{print $1}' || true)"
  echo "bind_mount_${label}_host_sha256=${host_hash}"
  echo "bind_mount_${label}_container_sha256=${container_hash}"
  if [[ -z "$container_hash" || "$host_hash" != "$container_hash" ]]; then
    BIND_MOUNT_STATUS="MOUNT_NOT_ACTIVE"
    WORKER_REBUILD_REQUIRED="yes"
    WORKER_RESTART_REQUIRED="unknown"
    echo "bind_mount_status=${BIND_MOUNT_STATUS}"
    blocked "worker_bind_mount_not_active"
  fi
}

verify_bind_mounts() {
  verify_bind_mount_hash "savant_security" "$SAVANT_CONTAINER" \
    "modules/savant_security/custom/pyfuncs/behavior_rules.py" \
    "/opt/savant/src/module/custom/pyfuncs/behavior_rules.py"
  verify_bind_mount_hash "event_worker" "$EVENT_WORKER_CONTAINER" \
    "services/event-worker/app/worker.py" "/app/app/worker.py"
  verify_bind_mount_hash "clip_worker" "$CLIP_WORKER_CONTAINER" \
    "services/clip-worker/app/worker.py" "/app/app/worker.py"
  verify_bind_mount_hash "media_worker" "$MEDIA_WORKER_CONTAINER" \
    "services/media-worker/app/worker.py" "/app/app/worker.py"
  BIND_MOUNT_STATUS="MOUNT_OK"
  WORKER_REBUILD_REQUIRED="no"
  WORKER_RESTART_REQUIRED="yes"
  echo "bind_mount_status=${BIND_MOUNT_STATUS}"
  echo "worker_rebuild_required=${WORKER_REBUILD_REQUIRED}"
  echo "worker_restart_required=${WORKER_RESTART_REQUIRED}"
}

record_request_data_json() {
  $DOCKER exec "$REDIS_CONTAINER" redis-cli --raw XREVRANGE security.record_requests + - COUNT 10 2>/dev/null \
    | python3 -c '
import sys
lines = [line.rstrip("\n") for line in sys.stdin]
for index, line in enumerate(lines):
    if line == "data" and index + 1 < len(lines):
        print(lines[index + 1])
        raise SystemExit(0)
print("")
'
}

echo "--- C1E Official Replay Evidence Integration Smoke ---"
echo "compose=${COMPOSE_FILE}"
echo "source_id=${SOURCE_ID}"
echo "camera_id=${CAMERA_ID}"
echo "rtsp_url=${RTSP_URL}"
echo "run_id=${RUN_ID}"
echo "max_record_requests=1"
echo "max_replay_jobs=1"
echo "max_evidence_bundles=1"
echo ""

command -v python3 >/dev/null 2>&1 || fatal "python3 not found"
command -v curl >/dev/null 2>&1 || fatal "curl not found"
command -v ffprobe >/dev/null 2>&1 || fatal "ffprobe not found; cannot prove RTSP/video output"

if [[ "$RTSP_URL" != "$REQUIRED_RTSP_URL" ]]; then
  fatal "fixed_rtsp_uri_mismatch"
fi

if ! timeout 20 ffprobe -rtsp_transport tcp -i "$RTSP_URL" -v error -show_streams >/tmp/c1e_rtsp_ffprobe.log 2>&1; then
  blocked "rtsp_unreachable"
fi
check 1 "fixed RTSP URL is reachable" pass

check 2 "C1E replay dev compose exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"
check 3 "C1 official adapter compose exists" "$([[ -f "$OLD_C1_COMPOSE" ]] && echo pass || echo fail)"
check 4 "Replay config exists" "$([[ -f "$REPLAY_CONFIG" ]] && echo pass || echo fail)"
check 5 "C1E camera config exists" "$([[ -f "$CAMERA_CONFIG" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && exit 1

C1E_SERVICES="$($COMPOSE -f "$COMPOSE_FILE" config --services)"
for required in redis postgres replay-service savant-security source-adapter video-file-sink event-worker clip-worker media-worker; do
  if ! echo "$C1E_SERVICES" | grep -qx "$required"; then
    fatal "c1e_compose_missing_service_${required}"
  fi
done
for forbidden in rtsp-server ffmpeg-source metadata-sink evidence-worker; do
  if echo "$C1E_SERVICES" | grep -qx "$forbidden"; then
    fatal "c1e_compose_contains_forbidden_service_${forbidden}"
  fi
done
check 6 "C1E compose defines replay evidence services only" pass

if grep -Eiq '\.mp4|file://|testVideo|test source|looping file source|video_path|video_loop\.sh|ffmpeg-source|rtsp-server' "$COMPOSE_FILE" "$CAMERA_CONFIG"; then
  fatal "local_file_or_test_video_in_c1e_rtsp_path"
fi
if ! grep -Fq "$REQUIRED_RTSP_URL" "$COMPOSE_FILE"; then
  fatal "c1e_compose_missing_fixed_rtsp_uri"
fi
if ! grep -Fq "$REQUIRED_RTSP_URL" "$CAMERA_CONFIG"; then
  fatal "c1e_camera_config_missing_fixed_rtsp_uri"
fi
check 7 "fixed RTSP source and no local fallback" pass

if ! assert_bind_mounts_configured; then
  BIND_MOUNT_STATUS="MOUNT_NOT_ACTIVE"
  echo "bind_mount_status=${BIND_MOUNT_STATUS}"
  blocked "worker_bind_mount_not_active"
fi
check 8 "C1 official dev bind mounts are configured" pass

RUNTIME_JSON="$($COMPOSE -f "$COMPOSE_FILE" config | python3 -c '
import json
import sys
import yaml

doc = yaml.safe_load(sys.stdin.read())
services = doc.get("services", {})
source = services["source-adapter"].get("environment", {})
savant = services["savant-security"].get("environment", {})
sink = services["video-file-sink"].get("environment", {})
event = services["event-worker"].get("environment", {})
clip = services["clip-worker"].get("environment", {})
media = services["media-worker"].get("environment", {})
print(json.dumps({
    "source_id": source.get("SOURCE_ID"),
    "rtsp_uri": source.get("RTSP_URI"),
    "location": source.get("LOCATION"),
    "source_output": source.get("ZMQ_ENDPOINT"),
    "savant_input": savant.get("ZMQ_SRC_ENDPOINT"),
    "savant_sink": savant.get("ZMQ_SINK_ENDPOINT"),
    "sink_endpoint": sink.get("ZMQ_ENDPOINT"),
    "recording_enabled": event.get("RECORDING_ENABLED"),
    "recording_event_types": event.get("RECORDING_EVENT_TYPES"),
    "recording_source_id": event.get("RECORDING_SOURCE_ID"),
    "recording_max_requests": event.get("RECORDING_MAX_REQUESTS_PER_RUN"),
    "recording_cooldown": event.get("RECORDING_COOLDOWN_SECONDS"),
    "recording_pre": event.get("RECORDING_PRE_SECONDS"),
    "recording_post": event.get("RECORDING_POST_SECONDS"),
    "clip_replay_api": clip.get("REPLAY_API_URL"),
    "clip_sink_url": clip.get("REPLAY_JOB_SINK_URL"),
    "clip_stop_condition_mode": clip.get("REPLAY_STOP_CONDITION_MODE"),
    "media_evidence_dir": media.get("EVIDENCE_OUTPUT_DIR"),
    "media_phase": media.get("EVIDENCE_PHASE"),
    "media_input_uri": media.get("EVIDENCE_INPUT_URI"),
    "media_finalizer": media.get("P1_RAW_CLIP_FINALIZER_ENABLED"),
}, sort_keys=True))
')"
SOURCE_OUTPUT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_output"])' "$RUNTIME_JSON")"
SAVANT_INPUT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["savant_input"])' "$RUNTIME_JSON")"
SINK_ENDPOINT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["sink_endpoint"])' "$RUNTIME_JSON")"
CLIP_SINK_URL="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["clip_sink_url"])' "$RUNTIME_JSON")"
RECORDING_ENABLED="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["recording_enabled"])' "$RUNTIME_JSON")"
RECORDING_EVENT_TYPES="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["recording_event_types"])' "$RUNTIME_JSON")"
RECORDING_SOURCE_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["recording_source_id"])' "$RUNTIME_JSON")"
RECORDING_MAX_REQUESTS="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["recording_max_requests"])' "$RUNTIME_JSON")"
RECORDING_COOLDOWN="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["recording_cooldown"])' "$RUNTIME_JSON")"
RECORDING_PRE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["recording_pre"])' "$RUNTIME_JSON")"
RECORDING_POST="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["recording_post"])' "$RUNTIME_JSON")"
CLIP_STOP_MODE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["clip_stop_condition_mode"])' "$RUNTIME_JSON")"
MEDIA_PHASE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["media_phase"])' "$RUNTIME_JSON")"
MEDIA_FINALIZER="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["media_finalizer"])' "$RUNTIME_JSON")"
MEDIA_INPUT_URI="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["media_input_uri"])' "$RUNTIME_JSON")"

REPLAY_TO_SAVANT="$(python3 - "$REPLAY_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
out_stream = data.get("out_stream") or {}
print(out_stream.get("url", ""))
PY
)"

CAMERA_JSON="$(python3 - "$CAMERA_CONFIG" "$CAMERA_ID" <<'PY'
import json
import sys
from pathlib import Path

import yaml

doc = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
camera = doc["cameras"][sys.argv[2]]
rule = camera["rules"]["intrusion"]
print(json.dumps({
    "source_id": camera["source_id"],
    "rtsp_url": camera["rtsp_url"],
    "clip_required": rule["clip_required"],
    "snapshot_required": rule["snapshot_required"],
}, sort_keys=True))
PY
)"
CAMERA_SOURCE_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["source_id"])' "$CAMERA_JSON")"
CAMERA_RTSP_URL="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["rtsp_url"])' "$CAMERA_JSON")"
RULE_CLIP_REQUIRED="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["clip_required"])' "$CAMERA_JSON")"
RULE_SNAPSHOT_REQUIRED="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["snapshot_required"])' "$CAMERA_JSON")"

check 9 "source adapter targets replay-service only" "$([[ "$SOURCE_OUTPUT" == "dealer+connect:tcp://replay-service:5555" ]] && echo pass || echo fail)"
check 10 "Replay out_stream targets savant-security" "$([[ "$REPLAY_TO_SAVANT" == "dealer+connect:tcp://savant-security:5557" ]] && echo pass || echo fail)"
check 11 "savant input binds Replay output port" "$([[ "$SAVANT_INPUT" == "router+bind:tcp://0.0.0.0:5557" ]] && echo pass || echo fail)"
check 12 "clip-worker job sink targets video-file-sink" "$([[ "$CLIP_SINK_URL" == "pub+connect:tcp://video-file-sink:6666" && "$SINK_ENDPOINT" == "sub+bind:tcp://0.0.0.0:6666" ]] && echo pass || echo fail)"
check 13 "recording gate is configured for one C1E intrusion request" "$([[ "$RECORDING_ENABLED" == "true" && "$RECORDING_EVENT_TYPES" == "intrusion" && "$RECORDING_SOURCE_ID" == "$SOURCE_ID" && "$RECORDING_MAX_REQUESTS" == "1" && "$RECORDING_COOLDOWN" == "30" && "$RECORDING_PRE" == "5" && "$RECORDING_POST" == "5" ]] && echo pass || echo fail)"
check 14 "clip-worker prefers ts_delta_sec" "$([[ "$CLIP_STOP_MODE" == "ts_delta_sec" ]] && echo pass || echo fail)"
check 15 "media-worker C1E raw finalizer configured" "$([[ "$MEDIA_PHASE" == "C1E-RTSP" && "$MEDIA_FINALIZER" == "true" && "$MEDIA_INPUT_URI" == "$RTSP_URL" ]] && echo pass || echo fail)"
check 16 "camera rule requests clip only" "$([[ "$CAMERA_SOURCE_ID" == "$SOURCE_ID" && "$CAMERA_RTSP_URL" == "$RTSP_URL" && "$RULE_CLIP_REQUIRED" == "True" && "$RULE_SNAPSHOT_REQUIRED" == "False" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && fatal "c1e_static_contract_mismatch"

REPLAY_TTL_JSON="$(python3 - "$REPLAY_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    data = {}
ttl = data.get("storage", {}).get("rocksdb", {}).get("data_expiration_ttl", {})
rocksdb = data.get("storage", {}).get("rocksdb", {})
print(json.dumps({
    "field": "storage.rocksdb.data_expiration_ttl" if ttl else "",
    "seconds": ttl.get("secs", ""),
    "rocksdb_path": rocksdb.get("path", ""),
}))
PY
)"
REPLAY_TTL_FIELD="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["field"])' "$REPLAY_TTL_JSON")"
REPLAY_TTL_SECONDS="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["seconds"])' "$REPLAY_TTL_JSON")"
REPLAY_ROCKSDB_PATH="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["rocksdb_path"])' "$REPLAY_TTL_JSON")"
TTL_REQUIREMENT_SECONDS=$((PRE_SECONDS + POST_SECONDS + SCHEDULING_MARGIN_SECONDS))
REPLAY_TTL_OK="no"
if [[ "$REPLAY_TTL_SECONDS" =~ ^[0-9]+$ ]] && [[ "$REPLAY_TTL_SECONDS" -ge "$TTL_REQUIREMENT_SECONDS" ]]; then
  REPLAY_TTL_OK="yes"
fi
echo "replay_ttl_field=${REPLAY_TTL_FIELD}"
echo "replay_ttl_seconds=${REPLAY_TTL_SECONDS}"
echo "ttl_requirement_seconds=${TTL_REQUIREMENT_SECONDS}"
echo "replay_ttl_ok=${REPLAY_TTL_OK}"
echo "replay_rocksdb_path=${REPLAY_ROCKSDB_PATH}"
if [[ -z "$REPLAY_TTL_FIELD" || ! "$REPLAY_TTL_SECONDS" =~ ^[0-9]+$ ]]; then
  blocked "replay_ttl_not_configured"
elif [[ "$REPLAY_TTL_OK" != "yes" ]]; then
  blocked "replay_ttl_too_short"
fi
check 17 "Replay TTL is configured and long enough" pass

ensure_worker_images_available_or_build_allowed

echo -e "${BLUE}Stopping isolated POC/official containers before C1E run...${NC}"
$COMPOSE -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true
$COMPOSE -f "$OLD_C1_COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
if [[ -f "$P1C_COMPOSE" ]]; then
  $COMPOSE -f "$P1C_COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
fi
if [[ -f "$D1_COMPOSE" ]]; then
  $COMPOSE -f "$D1_COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
fi

$DOCKER run --rm --pull never \
  -v /data/video-analytics/postgres-c1-official-replay-dev:/c1e-postgres \
  -v /data/video-analytics/replay-c1-official-replay-dev:/c1e-replay \
  -v /data/video-analytics/media:/media \
  alpine:3.20 \
  sh -c "rm -rf /c1e-postgres/* /c1e-replay/* /media/replay-sink-output/c1e/*" >/dev/null 2>&1 || true
cleanup_c1e_evidence_dirs

echo -e "${BLUE}Starting C1 official Replay evidence dev stack...${NC}"
if [[ "$C1E_ALLOW_BUILD" == "1" ]]; then
  BUILD_USED="yes"
  $COMPOSE -f "$COMPOSE_FILE" up -d --build --force-recreate --pull never \
    redis postgres replay-service savant-security video-file-sink event-worker clip-worker media-worker
  $COMPOSE -f "$COMPOSE_FILE" up -d --build --force-recreate --pull never source-adapter
else
  BUILD_USED="no"
  $COMPOSE -f "$COMPOSE_FILE" up -d --no-build --force-recreate --pull never \
    redis postgres replay-service savant-security video-file-sink event-worker clip-worker media-worker
  $COMPOSE -f "$COMPOSE_FILE" up -d --no-build --force-recreate --pull never source-adapter
fi
COMPOSE_STARTED="yes"
SERVICES_RESTARTED="redis postgres replay-service savant-security video-file-sink event-worker clip-worker media-worker source-adapter"

MISSING=""
for c in "$REDIS_CONTAINER" "$PG_CONTAINER" "$REPLAY_CONTAINER" "$SAVANT_CONTAINER" "$SOURCE_CONTAINER" "$SINK_CONTAINER" "$EVENT_WORKER_CONTAINER" "$CLIP_WORKER_CONTAINER" "$MEDIA_WORKER_CONTAINER"; do
  s="$(container_status "$c")"
  [[ "$s" != "running" ]] && MISSING="${MISSING} ${c}(${s})"
done
if [[ -n "$MISSING" ]]; then
  echo -e "${RED}containers not ready:${NC}"
  for m in $MISSING; do echo "  - $m"; done
  fatal "c1e_containers_not_running"
fi
check 18 "C1E containers running" pass
ACTUAL_CONTAINERS_STARTED="$($DOCKER ps --format '{{.Names}}' | grep -E '^c1-official-(redis|postgres|replay-service|savant|source-adapter|video-file-sink|event-worker|clip-worker|media-worker)$' | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
verify_bind_mounts

REPLAY_CODE="$(curl_silent -o /dev/null -w "%{http_code}" "${REPLAY_API}/api/v1/status" 2>/dev/null || echo "000")"
check 19 "Replay /api/v1/status responds" "$([[ "$REPLAY_CODE" == "200" ]] && echo pass || echo fail)"

echo -e "${BLUE}Waiting for C1E event -> record_request -> Replay job -> evidence bundle...${NC}"
EVENT_ID="$(_pg "SELECT id FROM events WHERE source_id='${SOURCE_ID}' AND payload->'media'->>'clip_status'='generated' ORDER BY updated_at DESC LIMIT 1;")"
if [[ -z "$EVENT_ID" ]]; then
  for _ in $(seq 1 "$WAIT_SECONDS"); do
    EVENT_ID="$(_pg "SELECT id FROM events WHERE source_id='${SOURCE_ID}' AND payload->'media'->>'clip_status'='generated' ORDER BY updated_at DESC LIMIT 1;")"
    [[ -n "$EVENT_ID" ]] && break
    sleep 1
  done
fi

SOURCE_EVENT_ID=""
REPLAY_JOB_ID=""
CLIP_PATH=""
CLIP_STATUS=""
RECORDING_STRATEGY=""
EVIDENCE_DIR=""
METADATA_PATH=""
EVENT_ANNOTATION_PATH=""
RAW_CLIP=""
EVENT_TS_MS=""
FRAME_UUID=""
KEYFRAME_UUID=""
PREVIOUS_KEYFRAME_UUID=""
FRAME_NUM=""
FRAME_PTS=""
REPLAY_JOB_REQUEST="{}"
if [[ -n "$EVENT_ID" ]]; then
  SOURCE_EVENT_ID="$(_pg "SELECT source_event_id FROM events WHERE id='${EVENT_ID}'::uuid;")"
  REPLAY_JOB_ID="$(_pg "SELECT payload->'media'->>'replay_job_id' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  CLIP_PATH="$(_pg "SELECT clip_path FROM events WHERE id='${EVENT_ID}'::uuid;")"
  CLIP_STATUS="$(_pg "SELECT payload->'media'->>'clip_status' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  RECORDING_STRATEGY="$(_pg "SELECT payload->'media'->>'recording_strategy' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  EVIDENCE_DIR="$(_pg "SELECT payload->'media'->>'evidence_dir' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  METADATA_PATH="$(_pg "SELECT payload->'media'->>'metadata_path' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  EVENT_ANNOTATION_PATH="$(_pg "SELECT payload->'media'->>'event_annotation_path' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  EVENT_FIELDS="$(_pg "SELECT COALESCE(event_ts_ms,0)::text || '|' || COALESCE(frame_uuid,'') || '|' || COALESCE(keyframe_uuid,'') || '|' || COALESCE(payload->'media'->>'previous_keyframe_uuid','') || '|' || COALESCE(payload->'media'->>'frame_num','') || '|' || COALESCE(payload->'media'->>'frame_pts','') FROM events WHERE id='${EVENT_ID}'::uuid;")"
  IFS='|' read -r EVENT_TS_MS FRAME_UUID KEYFRAME_UUID PREVIOUS_KEYFRAME_UUID FRAME_NUM FRAME_PTS <<< "$EVENT_FIELDS"
  REPLAY_JOB_REQUEST="$(_pg "SELECT COALESCE(payload->'media'->'replay_job_request','{}'::jsonb)::text FROM events WHERE id='${EVENT_ID}'::uuid;")"
  if [[ -n "$EVIDENCE_DIR" && "$EVIDENCE_DIR" != "NULL" ]]; then
    RAW_CLIP="$($DOCKER exec "$MEDIA_WORKER_CONTAINER" sh -c "find '$EVIDENCE_DIR' -maxdepth 1 -type f \\( -name 'raw_clip.mov' -o -name 'raw_clip.webm' -o -name 'raw_clip.mp4' \\) -size +0c 2>/dev/null | head -n 1" | tr -d '\r')"
  fi
fi

REDIS_EVENT_RAW="$(_redis XREVRANGE security.events + - COUNT 1000 | grep -F "$SOURCE_ID" || true)"
RECORD_REQUEST_RAW="$(_redis XREVRANGE security.record_requests + - COUNT 1000 | grep -F "$SOURCE_ID" || true)"
RECORD_REQUEST_COUNT="$(_redis XLEN security.record_requests)"
EVENTS_CREATED="$(_pg "SELECT COUNT(*) FROM events WHERE source_id='${SOURCE_ID}';")"
REPLAY_JOBS_CREATED="$(_pg "SELECT COUNT(*) FROM events WHERE source_id='${SOURCE_ID}' AND COALESCE(payload->'media'->>'replay_job_id','') <> '';")"
SOURCE_HAS_ID="$($DOCKER logs --since 30m "$SOURCE_CONTAINER" 2>&1 | grep -F "$SOURCE_ID" || true)"
REPLAY_RX_LOG="$($DOCKER logs --since 30m "$REPLAY_CONTAINER" 2>&1 | grep -E 'Received message|Adding message|Sending message to ZeroMQ socket' || true)"
SAVANT_HAS_ID="$($DOCKER logs --since 30m "$SAVANT_CONTAINER" 2>&1 | grep -F "$SOURCE_ID" || true)"
EVENT_WORKER_LOG="$($DOCKER logs --since 30m "$EVENT_WORKER_CONTAINER" 2>&1 | grep -E 'record_request_published|record_request_check|record_request_skipped' || true)"
CLIP_WORKER_LOG="$($DOCKER logs --since 30m "$CLIP_WORKER_CONTAINER" 2>&1 | grep -E 'replay_job_created|Replay job request|keyframe_provided_directly|clip_worker_skipped' || true)"
MEDIA_WORKER_LOG="$($DOCKER logs --since 30m "$MEDIA_WORKER_CONTAINER" 2>&1 | grep -E 'media_event_updated|media_metadata_parsed|p1_finalizer=True' || true)"

check 20 "source adapter logs show C1E source_id" "$([[ -n "$SOURCE_HAS_ID" ]] && echo pass || echo fail)"
check 21 "Replay logs show frames received and forwarded" "$([[ -n "$REPLAY_RX_LOG" ]] && echo pass || echo fail)"
check 22 "Savant logs show C1E source_id" "$([[ -n "$SAVANT_HAS_ID" ]] && echo pass || echo fail)"
check 23 "Redis security.events observed for C1E source" "$([[ -n "$REDIS_EVENT_RAW" ]] && echo pass || echo fail)"
check 24 "PostgreSQL event row inserted" "$([[ -n "$EVENT_ID" ]] && echo pass || echo fail)"
check 25 "event-worker published one record_request" "$([[ "${RECORD_REQUEST_COUNT:-0}" -eq 1 && -n "$EVENT_WORKER_LOG" ]] && echo pass || echo fail)"
check 26 "clip-worker created one Replay job" "$([[ "${REPLAY_JOBS_CREATED:-0}" -eq 1 && -n "$REPLAY_JOB_ID" && "$REPLAY_JOB_ID" != "NULL" && -n "$CLIP_WORKER_LOG" ]] && echo pass || echo fail)"
check 27 "media-worker finalized event evidence" "$([[ "$CLIP_STATUS" == "generated" && -n "$MEDIA_WORKER_LOG" ]] && echo pass || echo fail)"

REPLAY_ANCHOR="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(data.get("anchor_keyframe", ""))
PY
)"
REPLAY_OFFSET="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print((data.get("offset") or {}).get("seconds", ""))
PY
)"
REPLAY_STOP_CONDITION="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(json.dumps(data.get("stop_condition") or {}, sort_keys=True))
PY
)"
REPLAY_SINK_URL="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(((data.get("sink") or {}).get("url")) or "")
PY
)"
STORED_STREAM_ID="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(((data.get("configuration") or {}).get("stored_stream_id")) or "")
PY
)"
RESULTING_STREAM_ID="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(((data.get("configuration") or {}).get("resulting_stream_id")) or "")
PY
)"
STOP_CONDITION_MODE="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
stop = data.get("stop_condition") or {}
if "ts_delta_sec" in stop:
    print("ts_delta_sec")
elif "frame_count" in stop:
    print("frame_count_fallback")
else:
    print("unknown")
PY
)"
FALLBACK_REASON="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(data.get("fallback_reason") or "")
PY
)"

RECORD_REQUEST_DATA="$(record_request_data_json)"
REQUEST_ID="$(python3 - "$RECORD_REQUEST_DATA" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(data.get("request_id", ""))
PY
)"
REQUEST_STATUS="$(python3 - "$RECORD_REQUEST_DATA" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(data.get("status", ""))
PY
)"
REQUEST_PRE_SECONDS="$(python3 - "$RECORD_REQUEST_DATA" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(data.get("pre_seconds", ""))
PY
)"
REQUEST_POST_SECONDS="$(python3 - "$RECORD_REQUEST_DATA" <<'PY'
import json
import sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(data.get("post_seconds", ""))
PY
)"

HOST_RAW_CLIP="$(host_media_path "${RAW_CLIP:-}")"
HOST_METADATA_PATH="$(host_media_path "${METADATA_PATH:-}")"
HOST_EVENT_ANNOTATION_PATH="$(host_media_path "${EVENT_ANNOTATION_PATH:-}")"
SINK_METADATA_PATH="${EVIDENCE_DIR}/sink_metadata.json"
RAW_CLIP_SIZE="0"
if [[ -n "$RAW_CLIP" ]]; then
  RAW_CLIP_SIZE="$($DOCKER exec "$MEDIA_WORKER_CONTAINER" stat -c%s "$RAW_CLIP" 2>/dev/null | tr -d '[:space:]' || echo "0")"
fi
PROBE="$(ffprobe_json "$HOST_RAW_CLIP")"
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
print((data.get("format") or {}).get("duration") or "")
PY
)"
DURATION_OK="$(python3 - "$VIDEO_DURATION" <<'PY'
import sys
try:
    duration = float(sys.argv[1])
except (TypeError, ValueError):
    duration = 0.0
print("yes" if duration >= 3.0 else "no")
PY
)"
BUSINESS_METADATA_OK="$(python3 - "$HOST_METADATA_PATH" "$RTSP_URL" "$RUN_ID" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
rtsp_url = sys.argv[2]
run_id = sys.argv[3]
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("no")
    raise SystemExit(0)
limitations = set(data.get("limitations") or [])
required_limitations = {
    "single-event evidence POC",
    "not incident coalescing",
    "not continuous recording",
    "no annotated_clip generated",
}
media = data.get("media") or {}
event = data.get("event") or {}
replay = data.get("replay") or {}
ok = (
    data.get("schema_version") == "1.0"
    and data.get("phase") == "C1E-RTSP"
    and data.get("run_id") == run_id
    and data.get("evidence_type") == "security_event_replay_clip"
    and data.get("recording_strategy") == "savant_replay"
    and (data.get("input") or {}).get("input_type") == "rtsp"
    and (data.get("input") or {}).get("input_uri") == rtsp_url
    and (data.get("input") or {}).get("local_file_used") is False
    and (data.get("input") or {}).get("source_extraction_fallback") is False
    and (data.get("input") or {}).get("second_rtsp_pull") is False
    and event.get("event_id")
    and replay.get("replay_job_id")
    and media.get("raw_clip_path")
    and required_limitations.issubset(limitations)
)
print("yes" if ok else "no")
PY
)"
ANNOTATION_BBOX_CONVERSION="$(python3 - "$HOST_EVENT_ANNOTATION_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("no")
    raise SystemExit(0)
for overlay in data.get("overlays") or []:
    if overlay.get("type") == "person_bbox":
        source = overlay.get("bbox_source_format", "")
        fmt = overlay.get("bbox_format", "")
        bbox = overlay.get("bbox")
        raw = overlay.get("bbox_raw")
        if fmt == "xyxy" and source == "xywh" and isinstance(bbox, list) and len(bbox) == 4 and isinstance(raw, dict):
            x = float(raw["x"])
            y = float(raw["y"])
            w = float(raw["width"])
            h = float(raw["height"])
            expected = [x, y, x + w, y + h]
            print("yes" if [float(v) for v in bbox] == expected else "no")
        elif fmt == "xyxy" and source == "xyxy":
            print("yes")
        else:
            print("no")
        raise SystemExit(0)
print("no")
PY
)"
SINK_METADATA_PRESERVED="$($DOCKER exec "$MEDIA_WORKER_CONTAINER" test -s "$SINK_METADATA_PATH" && echo yes || echo no)"

check 28 "events.clip_path points to raw_clip" "$([[ "$CLIP_PATH" == "$RAW_CLIP" && "$CLIP_PATH" == /media/evidence/*/raw_clip.* ]] && echo pass || echo fail)"
check 29 "raw_clip file exists and non-empty" "$([[ -n "$RAW_CLIP" && "${RAW_CLIP_SIZE:-0}" -gt 0 ]] && echo pass || echo fail)"
check 30 "raw clip is ffprobe-readable" "$([[ -n "$VIDEO_FORMAT" && "$DURATION_OK" == "yes" ]] && echo pass || echo fail)"
check 31 "metadata.json contains business metadata" "$([[ "$BUSINESS_METADATA_OK" == "yes" ]] && echo pass || echo fail)"
check 32 "sink_metadata.json is preserved" "$([[ "$SINK_METADATA_PRESERVED" == "yes" ]] && echo pass || echo fail)"
check 33 "event_annotation.json exists and bbox conversion is correct" "$([[ "$ANNOTATION_BBOX_CONVERSION" == "yes" ]] && echo pass || echo fail)"
check 34 "no annotated_clip generated" "$($DOCKER exec "$MEDIA_WORKER_CONTAINER" sh -c "test ! -e '${EVIDENCE_DIR}/annotated_clip.mp4' && test ! -e '${EVIDENCE_DIR}/annotated_clip.mov' && test ! -e '${EVIDENCE_DIR}/annotated_clip.webm'" && echo pass || echo fail)"
check 35 "Replay job uses previous keyframe before current keyframe" "$([[ -n "$REPLAY_ANCHOR" && "$REPLAY_ANCHOR" == "${PREVIOUS_KEYFRAME_UUID:-$KEYFRAME_UUID}" ]] && echo pass || echo fail)"
check 36 "Replay stop_condition uses ts_delta_sec or declared fallback" "$([[ "$STOP_CONDITION_MODE" == "ts_delta_sec" || ( "$STOP_CONDITION_MODE" == "frame_count_fallback" && -n "$FALLBACK_REASON" ) ]] && echo pass || echo fail)"
check 37 "Replay stored stream id matches source id" "$([[ "$STORED_STREAM_ID" == "$SOURCE_ID" ]] && echo pass || echo fail)"
check 38 "Replay resulting stream id contains event id" "$([[ -n "$RESULTING_STREAM_ID" && -n "$EVENT_ID" && "$RESULTING_STREAM_ID" == *"$EVENT_ID"* ]] && echo pass || echo fail)"
check 39 "Replay sink url points to video-file-sink" "$([[ "$REPLAY_SINK_URL" == "pub+connect:tcp://video-file-sink:6666" ]] && echo pass || echo fail)"

echo -e "${BLUE}Stopping source adapter after first evidence bundle...${NC}"
$DOCKER stop "$SOURCE_CONTAINER" >/dev/null 2>&1 || true
sleep 5
SOURCE_STOPPED_AFTER_SMOKE="$([[ "$(container_status "$SOURCE_CONTAINER")" != "running" ]] && echo yes || echo no)"
POST_STOP_RECORD_REQUEST_COUNT="$(_redis XLEN security.record_requests)"
POST_STOP_REPLAY_JOB_COUNT="$(_pg "SELECT COUNT(*) FROM events WHERE source_id='${SOURCE_ID}' AND COALESCE(payload->'media'->>'replay_job_id','') <> '';")"
POST_STOP_EVIDENCE_COUNT="$(count_c1e_evidence_dirs)"
if [[ -d "$SINK_RUN_ROOT" ]]; then
  POST_STOP_SINK_COUNT="$(find "$SINK_RUN_ROOT" -name metadata.json | wc -l | tr -d '[:space:]')"
else
  POST_STOP_SINK_COUNT="0"
fi
EXTRA_CLIPS_DETECTED=0
if [[ "$POST_STOP_EVIDENCE_COUNT" -gt "$max_evidence_bundles" ]]; then
  EXTRA_CLIPS_DETECTED=$((POST_STOP_EVIDENCE_COUNT - max_evidence_bundles))
fi
if [[ "$POST_STOP_SINK_COUNT" -gt "$max_evidence_bundles" && "$POST_STOP_SINK_COUNT" -gt "$POST_STOP_EVIDENCE_COUNT" ]]; then
  EXTRA_CLIPS_DETECTED=$((POST_STOP_SINK_COUNT - max_evidence_bundles))
fi
check 40 "source adapter stopped after smoke" "$([[ "$SOURCE_STOPPED_AFTER_SMOKE" == "yes" ]] && echo pass || echo fail)"
check 41 "single-event evidence counts are controlled" "$([[ "$POST_STOP_RECORD_REQUEST_COUNT" -eq "$max_record_requests" && "$POST_STOP_REPLAY_JOB_COUNT" -eq "$max_replay_jobs" && "$POST_STOP_EVIDENCE_COUNT" -eq "$max_evidence_bundles" && "$POST_STOP_SINK_COUNT" -eq 1 && "$EXTRA_CLIPS_DETECTED" -eq 0 ]] && echo pass || echo fail)"
check 42 "extra clips detected is zero" "$([[ "$EXTRA_CLIPS_DETECTED" -eq 0 ]] && echo pass || echo fail)"

echo ""
echo "docker_access=${DOCKER_ACCESS}"
echo "docker_command_prefix=${DOCKER}"
echo "compose_command_prefix=${COMPOSE}"
echo "sudo_used=${SUDO_USED}"
echo "actual_containers_started=${ACTUAL_CONTAINERS_STARTED}"
echo "build_used=${BUILD_USED}"
echo "C1E_ALLOW_BUILD=${C1E_ALLOW_BUILD}"
echo "pull_used=${PULL_USED}"
echo "bind_mount_status=${BIND_MOUNT_STATUS}"
echo "services_restarted=${SERVICES_RESTARTED}"
echo "worker_rebuild_required=${WORKER_REBUILD_REQUIRED}"
echo "worker_restart_required=${WORKER_RESTART_REQUIRED}"
echo "input_type=rtsp"
echo "input_uri=${RTSP_URL}"
echo "local_file_used=false"
echo "test_video_used=false"
echo "source_extraction_fallback=false"
echo "second_rtsp_pull=false"
echo "second_rtsp_pull_used=no"
echo "source_to_replay_to_savant_single_path=yes"
echo "replay_service=$(container_status "$REPLAY_CONTAINER")"
echo "savant_security=$(container_status "$SAVANT_CONTAINER")"
echo "source_adapter=$(container_status "$SOURCE_CONTAINER")"
echo "video_file_sink=$(container_status "$SINK_CONTAINER")"
echo "event_worker=$(container_status "$EVENT_WORKER_CONTAINER")"
echo "clip_worker=$(container_status "$CLIP_WORKER_CONTAINER")"
echo "media_worker=$(container_status "$MEDIA_WORKER_CONTAINER")"
echo "api=not_started_optional"
echo "source_id=${SOURCE_ID}"
echo "camera_id=${CAMERA_ID}"
echo "security.events_observed=$([[ -n "$REDIS_EVENT_RAW" ]] && echo yes || echo no)"
echo "event_id=${EVENT_ID}"
echo "source_event_id=${SOURCE_EVENT_ID}"
echo "event_ts_ms=${EVENT_TS_MS}"
echo "frame_uuid=${FRAME_UUID}"
echo "keyframe_uuid=${KEYFRAME_UUID}"
echo "previous_keyframe_uuid=${PREVIOUS_KEYFRAME_UUID}"
echo "frame_num=${FRAME_NUM}"
echo "frame_pts=${FRAME_PTS}"
echo "record_request_observed=$([[ -n "$RECORD_REQUEST_RAW" ]] && echo yes || echo no)"
echo "record_request_id=${REQUEST_ID}"
echo "record_request_status=${REQUEST_STATUS}"
echo "record_request_pre_seconds=${REQUEST_PRE_SECONDS}"
echo "record_request_post_seconds=${REQUEST_POST_SECONDS}"
echo "events_created=${EVENTS_CREATED}"
echo "record_requests_created=${POST_STOP_RECORD_REQUEST_COUNT}"
echo "replay_jobs_created=${POST_STOP_REPLAY_JOB_COUNT}"
echo "evidence_bundles_created=${POST_STOP_EVIDENCE_COUNT}"
echo "extra_clips_detected=${EXTRA_CLIPS_DETECTED}"
echo "source_stopped_after_smoke=${SOURCE_STOPPED_AFTER_SMOKE}"
echo "pre_seconds=${PRE_SECONDS}"
echo "post_seconds=${POST_SECONDS}"
echo "scheduling_margin_seconds=${SCHEDULING_MARGIN_SECONDS}"
echo "replay_ttl_field=${REPLAY_TTL_FIELD}"
echo "replay_ttl_seconds=${REPLAY_TTL_SECONDS}"
echo "ttl_requirement_seconds=${TTL_REQUIREMENT_SECONDS}"
echo "replay_ttl_ok=${REPLAY_TTL_OK}"
echo "replay_rocksdb_path=${REPLAY_ROCKSDB_PATH}"
echo "replay_api_url=${REPLAY_API}"
echo "replay_job_id=${REPLAY_JOB_ID}"
echo "anchor_keyframe_uuid=${REPLAY_ANCHOR}"
echo "offset.seconds=${REPLAY_OFFSET}"
echo "stop_condition=${REPLAY_STOP_CONDITION}"
echo "stop_condition_mode=${STOP_CONDITION_MODE}"
echo "fallback_reason=${FALLBACK_REASON}"
echo "sink_url=${REPLAY_SINK_URL}"
echo "stored_stream_id=${STORED_STREAM_ID}"
echo "resulting_stream_id=${RESULTING_STREAM_ID}"
echo "events.clip_path=${CLIP_PATH}"
echo "payload.media.clip_status=${CLIP_STATUS}"
echo "payload.media.recording_strategy=${RECORDING_STRATEGY}"
echo "payload.media.evidence_dir=${EVIDENCE_DIR}"
echo "evidence_dir=${EVIDENCE_DIR}"
echo "metadata_json=${METADATA_PATH}"
echo "sink_metadata_json=${SINK_METADATA_PATH}"
echo "event_annotation_json=${EVENT_ANNOTATION_PATH}"
echo "raw_clip=${RAW_CLIP}"
echo "host_raw_clip=${HOST_RAW_CLIP}"
echo "raw_clip_size=${RAW_CLIP_SIZE}"
echo "raw_clip_duration=${VIDEO_DURATION}"
echo "business_metadata_generated=${BUSINESS_METADATA_OK}"
echo "sink_metadata_preserved=${SINK_METADATA_PRESERVED}"
echo "event_annotation_bbox_conversion=${ANNOTATION_BBOX_CONVERSION}"
echo "ffprobe_status=$([[ -n "$VIDEO_FORMAT" && "$DURATION_OK" == "yes" ]] && echo ok || echo not_ok)"
echo "annotated_clip=no"
echo "production_compose_change=no"
echo "validation=$([[ "$FAIL_COUNT" -eq 0 ]] && echo pass || echo fail)"
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "${EXTRA_CLIPS_DETECTED}" -gt 0 ]]; then
  fatal "uncontrolled_clip_generation"
fi
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  fatal "c1e_official_replay_evidence_failure"
fi
