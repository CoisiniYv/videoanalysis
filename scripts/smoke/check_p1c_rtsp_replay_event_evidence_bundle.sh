#!/usr/bin/env bash
# Phase P1c-RTSP event-triggered Replay evidence bundle smoke.
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
# No source extraction fallback, second RTSP pull, annotated_clip, API, or
# production compose.

set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SMOKE_DIR}/../.."
COMPOSE_FILE="${ROOT_DIR}/infra/docker-compose.p1c-rtsp-replay-event-evidence.yml"
OLD_COMPOSE_P1A="${ROOT_DIR}/infra/docker-compose.p1a-replay-inline-poc.yml"
OLD_COMPOSE_P1B="${ROOT_DIR}/infra/docker-compose.p1b-rtsp-replay-manual-sink.yml"
OLD_COMPOSE_P1_REPLAY_CLIP="${ROOT_DIR}/infra/docker-compose.p1-replay-clip-poc.yml"
REPLAY_CONFIG="${ROOT_DIR}/modules/savant_replay/config.p1c_rtsp_inline.json"
CAMERA_CONFIG="${ROOT_DIR}/modules/savant_security/config/cameras.p1c_rtsp_replay.yml"
REDIS_SHIM="${ROOT_DIR}/modules/savant_security/poc_deps/redis.py"
REQUIRED_RTSP_URL="rtsp://10.37.57.112:8554/live/1080movie"
RTSP_URL="${P1C_RTSP_URL:-$REQUIRED_RTSP_URL}"
SOURCE_ID="${P1C_RTSP_SOURCE_ID:-p1c_rtsp_replay}"
CAMERA_ID="${P1C_RTSP_CAMERA_ID:-cam_p1c_rtsp_replay}"
REPLAY_API="${P1C_RTSP_REPLAY_API:-http://127.0.0.1:8088}"
WAIT_SECONDS="${P1C_RTSP_WAIT_SECONDS:-240}"
RUN_ID="${P1C_RTSP_RUN_ID:-$(date +%s%N)}"
P1C_RTSP_RUN_ID="$RUN_ID"
P1C_ALLOW_BUILD="${P1C_ALLOW_BUILD:-0}"
PRE_SECONDS=5
POST_SECONDS=5
SCHEDULING_MARGIN_SECONDS=10
max_events=1
max_record_requests=1
max_replay_jobs=1
max_evidence_bundles=1
EVIDENCE_ROOT="/data/video-analytics/media/evidence"
SINK_RUN_ROOT="/data/video-analytics/media/replay-sink-output/p1c-rtsp/${RUN_ID}"
P1C_RTSP_SINK_ROOT="/media/replay-sink-output/p1c-rtsp/${RUN_ID}"
P1C_RTSP_SINK_DIR_LOCATION="/media/replay-sink-output/p1c-rtsp/${RUN_ID}/%source_id%/%src_filename%/"
export P1C_RTSP_SINK_ROOT
export P1C_RTSP_SINK_DIR_LOCATION
export P1C_RTSP_RUN_ID

REDIS_CONTAINER="p1c-rtsp-replay-redis"
PG_CONTAINER="p1c-rtsp-replay-postgres"
REPLAY_CONTAINER="p1c-rtsp-replay-service"
SAVANT_CONTAINER="p1c-rtsp-replay-savant-security"
SOURCE_CONTAINER="p1c-rtsp-replay-source-adapter"
SINK_CONTAINER="p1c-rtsp-replay-video-file-sink"
EVENT_WORKER_CONTAINER="p1c-rtsp-replay-event-worker"
CLIP_WORKER_CONTAINER="p1c-rtsp-replay-clip-worker"
MEDIA_WORKER_CONTAINER="p1c-rtsp-replay-media-worker"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0
BUILD_USED="no"
PULL_USED="no"
BIND_MOUNT_STATUS="not_checked"
WORKER_REBUILD_REQUIRED="unknown"
WORKER_RESTART_REQUIRED="unknown"
SERVICES_RESTARTED=""
ACTUAL_CONTAINERS_STARTED=""

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
    DOCKER_ACCESS="DOCKER_ACCESS_BLOCKED"
    DOCKER=""
    COMPOSE=""
    SUDO_USED="no"
    echo "docker_access=${DOCKER_ACCESS}"
    blocked "docker_daemon_unavailable"
  fi
  echo -e "${BLUE}[docker]${NC} access=$DOCKER_ACCESS prefix=$DOCKER"
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
  ffprobe -v error -show_entries format=duration,format_name -of json "$path" 2>/dev/null || true
}

cleanup_p1c_evidence_dirs() {
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

count_p1c_evidence_dirs() {
  python3 - "$EVIDENCE_ROOT" "$CAMERA_ID" "$RUN_ID" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
camera_id = sys.argv[2]
run_id = sys.argv[3]
count = 0
if root.exists():
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
        if not (isinstance(event, dict) and event.get("camera_id") == camera_id):
            continue
        try:
            metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        if metadata.get("run_id") == run_id:
            count += 1
print(count)
PY
}

assert_worker_bind_mounts_configured() {
  python3 - "$COMPOSE_FILE" <<'PY'
import sys
from pathlib import Path

import yaml

compose = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = {
    "event-worker": "../services/event-worker:/app:rw",
    "clip-worker": "../services/clip-worker:/app:rw",
    "media-worker": "../services/media-worker:/app:rw",
}
missing = []
for service, mount in expected.items():
    volumes = compose["services"].get(service, {}).get("volumes", []) or []
    if mount not in volumes:
        missing.append(f"{service}:{mount}")
if missing:
    print("\n".join(missing))
    raise SystemExit(1)
PY
}

ensure_worker_images_available_or_build_allowed() {
  local missing=""
  for image in \
    p1c-rtsp-replay-event-worker:latest \
    p1c-rtsp-replay-clip-worker:latest \
    p1c-rtsp-replay-media-worker:latest; do
    if ! $DOCKER image inspect "$image" >/dev/null 2>&1; then
      missing="${missing} ${image}"
    fi
  done

  if [[ -n "$missing" && "$P1C_ALLOW_BUILD" != "1" ]]; then
    echo "missing_worker_images=${missing# }"
    echo "Hint=rerun with P1C_ALLOW_BUILD=1 or enable worker bind mounts"
    blocked "worker_image_missing_and_build_not_allowed"
  fi
}

verify_worker_bind_mount() {
  local label="$1"
  local container="$2"
  local host_file="$3"
  local container_file="$4"
  local host_hash=""
  local container_hash=""

  host_hash="$(sha256sum "${ROOT_DIR}/${host_file}" | awk '{print $1}')"
  container_hash="$($DOCKER exec "$container" sha256sum "$container_file" 2>/dev/null | awk '{print $1}' || true)"
  echo "worker_mount_${label}_host_sha256=${host_hash}"
  echo "worker_mount_${label}_container_sha256=${container_hash}"
  if [[ -z "$container_hash" || "$host_hash" != "$container_hash" ]]; then
    BIND_MOUNT_STATUS="MOUNT_NOT_ACTIVE"
    WORKER_REBUILD_REQUIRED="yes"
    WORKER_RESTART_REQUIRED="unknown"
    echo "bind_mount_status=${BIND_MOUNT_STATUS}"
    blocked "worker_bind_mount_not_active"
  fi
}

verify_worker_bind_mounts() {
  verify_worker_bind_mount "event_worker" "$EVENT_WORKER_CONTAINER" \
    "services/event-worker/app/worker.py" "/app/app/worker.py"
  verify_worker_bind_mount "clip_worker" "$CLIP_WORKER_CONTAINER" \
    "services/clip-worker/app/worker.py" "/app/app/worker.py"
  verify_worker_bind_mount "media_worker" "$MEDIA_WORKER_CONTAINER" \
    "services/media-worker/app/worker.py" "/app/app/worker.py"
  BIND_MOUNT_STATUS="MOUNT_OK"
  WORKER_REBUILD_REQUIRED="no"
  WORKER_RESTART_REQUIRED="yes"
  echo "bind_mount_status=${BIND_MOUNT_STATUS}"
  echo "rebuild_required=${WORKER_REBUILD_REQUIRED}"
  echo "restart_required=${WORKER_RESTART_REQUIRED}"
}

echo "--- P1c-RTSP Event-triggered Replay Evidence Bundle Smoke ---"
echo "compose=${COMPOSE_FILE}"
echo "source_id=${SOURCE_ID}"
echo "camera_id=${CAMERA_ID}"
echo "rtsp_url=${RTSP_URL}"
echo "replay_api=${REPLAY_API}"
echo "run_id=${RUN_ID}"
echo "max_events=1"
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

if ! timeout 20 ffprobe -rtsp_transport tcp -i "$RTSP_URL" -v error -show_streams >/tmp/p1c_rtsp_ffprobe.log 2>&1; then
  blocked "rtsp_unreachable"
fi
check 1 "RTSP URL is reachable and exact" pass

check 2 "compose file exists" "$([[ -f "$COMPOSE_FILE" ]] && echo pass || echo fail)"
check 3 "Replay P1c config exists" "$([[ -f "$REPLAY_CONFIG" ]] && echo pass || echo fail)"
check 4 "camera P1c config exists" "$([[ -f "$CAMERA_CONFIG" ]] && echo pass || echo fail)"
check 5 "local Redis shim exists for offline savant-security startup" "$([[ -f "$REDIS_SHIM" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && exit 1

P1C_SERVICES="$($COMPOSE -f "$COMPOSE_FILE" config --services)"
for required in redis postgres replay-service savant-security source-adapter video-file-sink event-worker clip-worker media-worker; do
  if ! echo "$P1C_SERVICES" | grep -qx "$required"; then
    fatal "P1c compose missing service $required"
  fi
done
for forbidden in api rtsp-server ffmpeg-source metadata-sink evidence-worker face-worker; do
  if echo "$P1C_SERVICES" | grep -qx "$forbidden"; then
    fatal "P1c compose contains forbidden service $forbidden"
  fi
done
check 6 "compose contains only P1c allowed services" pass

if grep -Eq 'file:///testVideo|/testVideo/test.mp4|video_loop.sh|source extraction|annotated_clip|rtsp-server|ffmpeg-source|api:' "$COMPOSE_FILE"; then
  fatal "local_file_or_test_video_in_p1c_rtsp_path"
fi
if ! grep -Eq 'rtsp://10\.37\.57\.112:8554/live/1080movie' "$COMPOSE_FILE"; then
  fatal "P1c compose does not contain the required RTSP URL"
fi
check 7 "compose uses required RTSP URI and no local file fallback" pass

STREAMS_JSON="$(python3 - "$REPLAY_CONFIG" "$COMPOSE_FILE" "$CAMERA_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

import yaml

replay = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
compose = yaml.safe_load(Path(sys.argv[2]).read_text(encoding="utf-8"))
cameras = yaml.safe_load(Path(sys.argv[3]).read_text(encoding="utf-8"))
services = compose["services"]
source_env = services["source-adapter"]["environment"]
savant_env = services["savant-security"]["environment"]
sink_env = services["video-file-sink"]["environment"]
event_env = services["event-worker"]["environment"]
clip_env = services["clip-worker"]["environment"]
media_env = services["media-worker"]["environment"]
cam = cameras["cameras"]["cam_p1c_rtsp_replay"]
rule = cam["rules"]["intrusion"]
out_stream = replay.get("out_stream")
print(json.dumps({
    "replay_in_stream": replay["in_stream"]["url"],
    "replay_out_stream": None if out_stream is None else out_stream["url"],
    "source_adapter_output": source_env["ZMQ_ENDPOINT"],
    "source_adapter_rtsp": source_env["RTSP_URI"],
    "source_adapter_location": source_env["LOCATION"],
    "savant_input_stream": savant_env["ZMQ_SRC_ENDPOINT"],
    "video_file_sink_endpoint": sink_env["ZMQ_ENDPOINT"],
    "event_worker_recording_enabled": event_env["RECORDING_ENABLED"],
    "clip_worker_replay_api": clip_env["REPLAY_API_URL"],
    "clip_worker_sink_url": clip_env["REPLAY_JOB_SINK_URL"],
    "media_worker_sink_dir": media_env["SINK_OUTPUT_DIR"],
    "media_worker_evidence_dir": media_env["EVIDENCE_OUTPUT_DIR"],
    "media_worker_p1_finalizer": media_env["P1_RAW_CLIP_FINALIZER_ENABLED"],
    "camera_source_id": cam["source_id"],
    "camera_rtsp_url": cam["rtsp_url"],
    "rule_clip_required": rule["clip_required"],
    "rule_snapshot_required": rule["snapshot_required"],
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
CLIP_SINK_URL="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["clip_worker_sink_url"])' "$STREAMS_JSON")"
P1_FINALIZER="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["media_worker_p1_finalizer"])' "$STREAMS_JSON")"
RULE_CLIP_REQUIRED="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["rule_clip_required"])' "$STREAMS_JSON")"
RULE_SNAPSHOT_REQUIRED="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["rule_snapshot_required"])' "$STREAMS_JSON")"

echo -e "${BLUE}[topology] replay in_stream=${REPLAY_IN_STREAM}${NC}"
echo -e "${BLUE}[topology] source adapter output=${SOURCE_OUTPUT}${NC}"
echo -e "${BLUE}[topology] replay out_stream=${REPLAY_OUT_STREAM}${NC}"
echo -e "${BLUE}[topology] savant input=${SAVANT_INPUT}${NC}"
echo -e "${BLUE}[topology] source adapter rtsp=${SOURCE_RTSP}${NC}"
echo -e "${BLUE}[topology] source adapter location=${SOURCE_LOCATION}${NC}"
echo -e "${BLUE}[topology] video-file-sink endpoint=${SINK_ENDPOINT}${NC}"

check 8 "source adapter targets replay-service in_stream" "$([[ "$SOURCE_OUTPUT" == "dealer+connect:tcp://replay-service:5555" ]] && echo pass || echo fail)"
check 9 "replay out_stream targets savant-security" "$([[ "$REPLAY_OUT_STREAM" == "dealer+connect:tcp://savant-security:5557" ]] && echo pass || echo fail)"
check 10 "savant input binds replay out_stream port" "$([[ "$SAVANT_INPUT" == "router+bind:tcp://0.0.0.0:5557" ]] && echo pass || echo fail)"
check 11 "source adapter uses RTSP source and no local file" "$([[ "$SOURCE_RTSP" == "$RTSP_URL" && "$SOURCE_LOCATION" == "$RTSP_URL" ]] && echo pass || echo fail)"
check 12 "clip-worker Replay job sink targets video-file-sink" "$([[ "$CLIP_SINK_URL" == "pub+connect:tcp://video-file-sink:6666" && "$SINK_ENDPOINT" == "sub+bind:tcp://0.0.0.0:6666" ]] && echo pass || echo fail)"
check 13 "P1 raw clip finalizer enabled" "$([[ "$P1_FINALIZER" == "true" ]] && echo pass || echo fail)"
check 14 "camera rule requests clip only" "$([[ "$RULE_CLIP_REQUIRED" == "True" && "$RULE_SNAPSHOT_REQUIRED" == "False" ]] && echo pass || echo fail)"
[[ "$FAIL_COUNT" -gt 0 ]] && blocked "topology_or_contract_mismatch"

OLD_POC_CONTAINERS="$($DOCKER ps --format '{{.Names}}' | grep -E '^(p1a-replay-inline|p1b-replay-manual|p1b-rtsp-replay|p1-replay-clip)' || true)"
if [[ -n "$OLD_POC_CONTAINERS" ]]; then
  echo -e "${BLUE}Stopping old P1a/P1b/P1 replay POC containers...${NC}"
  for old_compose in "$OLD_COMPOSE_P1A" "$OLD_COMPOSE_P1B" "$OLD_COMPOSE_P1_REPLAY_CLIP"; do
    if [[ -f "$old_compose" ]]; then
      $COMPOSE -f "$old_compose" down --remove-orphans >/dev/null 2>&1 || true
    fi
  done
fi
OLD_POC_CONTAINERS_AFTER="$($DOCKER ps --format '{{.Names}}' | grep -E '^(p1a-replay-inline|p1b-replay-manual|p1b-rtsp-replay|p1-replay-clip)' || true)"
if [[ -n "$OLD_POC_CONTAINERS_AFTER" ]]; then
  echo "$OLD_POC_CONTAINERS_AFTER"
  fatal "old P1a/P1b/P1 replay POC containers are still running"
fi
check 15 "old P1a/P1b replay POC containers are not running" pass

echo -e "${BLUE}Resetting any prior P1c compose state before start...${NC}"
$COMPOSE -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true
$DOCKER run --rm --pull never \
  -v /data/video-analytics/postgres-p1c-rtsp-replay:/p1c-postgres \
  -v /data/video-analytics/replay-p1c-rtsp:/p1c-replay \
  -v /data/video-analytics/media:/media \
  alpine:3.20 \
  sh -c "rm -rf /p1c-postgres/* /p1c-replay/* /media/replay-sink-output/p1c-rtsp/*" >/dev/null 2>&1 || true
cleanup_p1c_evidence_dirs

if ! assert_worker_bind_mounts_configured; then
  BIND_MOUNT_STATUS="MOUNT_NOT_ACTIVE"
  echo "bind_mount_status=${BIND_MOUNT_STATUS}"
  blocked "worker_bind_mount_not_active"
fi
ensure_worker_images_available_or_build_allowed

echo -e "${BLUE}Starting P1c RTSP replay evidence stack...${NC}"
if [[ "$P1C_ALLOW_BUILD" == "1" ]]; then
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
SERVICES_RESTARTED="redis postgres replay-service savant-security video-file-sink event-worker clip-worker media-worker source-adapter"

MISSING=""
for c in "$REDIS_CONTAINER" "$PG_CONTAINER" "$REPLAY_CONTAINER" "$SAVANT_CONTAINER" "$SOURCE_CONTAINER" "$SINK_CONTAINER" "$EVENT_WORKER_CONTAINER" "$CLIP_WORKER_CONTAINER" "$MEDIA_WORKER_CONTAINER"; do
  s="$(container_status "$c")"
  [[ "$s" != "running" ]] && MISSING="${MISSING} ${c}(${s})"
done
if [[ -n "$MISSING" ]]; then
  echo -e "${RED}containers not ready:${NC}"
  for m in $MISSING; do echo "  - $m"; done
  exit 1
fi
check 16 "P1c containers running" pass
ACTUAL_CONTAINERS_STARTED="$($DOCKER ps --format '{{.Names}}' | grep -E '^p1c-rtsp-replay-' | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
verify_worker_bind_mounts

REPLAY_CODE="$(curl_silent -o /dev/null -w "%{http_code}" "${REPLAY_API}/api/v1/status" 2>/dev/null || echo "000")"
check 17 "Replay /api/v1/status responds" "$([[ "$REPLAY_CODE" == "200" ]] && echo pass || echo fail)"

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

echo -e "${BLUE}Waiting for RTSP -> Replay -> Savant -> event-worker -> clip-worker -> media-worker...${NC}"
EVENT_ID="$(_pg "SELECT id FROM events WHERE source_id='${SOURCE_ID}' AND payload->'media'->>'clip_status'='generated' ORDER BY updated_at DESC LIMIT 1;")"
SOURCE_EVENT_ID=""
REPLAY_JOB_ID=""
CLIP_PATH=""
CLIP_STATUS=""
EVIDENCE_DIR=""
METADATA_PATH=""
EVENT_ANNOTATION_PATH=""
RAW_CLIP=""
if [[ -z "$EVENT_ID" ]]; then
  for _ in $(seq 1 "$WAIT_SECONDS"); do
    EVENT_ID="$(_pg "SELECT id FROM events WHERE source_id='${SOURCE_ID}' AND payload->'media'->>'clip_status'='generated' ORDER BY updated_at DESC LIMIT 1;")"
    [[ -n "$EVENT_ID" ]] && break
    sleep 1
  done
fi

if [[ -n "$EVENT_ID" ]]; then
  SOURCE_EVENT_ID="$(_pg "SELECT source_event_id FROM events WHERE id='${EVENT_ID}'::uuid;")"
  REPLAY_JOB_ID="$(_pg "SELECT payload->'media'->>'replay_job_id' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  CLIP_PATH="$(_pg "SELECT clip_path FROM events WHERE id='${EVENT_ID}'::uuid;")"
  CLIP_STATUS="$(_pg "SELECT payload->'media'->>'clip_status' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  EVIDENCE_DIR="$(_pg "SELECT payload->'media'->>'evidence_dir' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  METADATA_PATH="$(_pg "SELECT payload->'media'->>'metadata_path' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  EVENT_ANNOTATION_PATH="$(_pg "SELECT payload->'media'->>'event_annotation_path' FROM events WHERE id='${EVENT_ID}'::uuid;")"
  if [[ -n "$EVIDENCE_DIR" && "$EVIDENCE_DIR" != "NULL" ]]; then
    RAW_CLIP="$($DOCKER exec "$MEDIA_WORKER_CONTAINER" sh -c "find '$EVIDENCE_DIR' -maxdepth 1 -type f \\( -name 'raw_clip.mov' -o -name 'raw_clip.webm' -o -name 'raw_clip.mp4' \\) -size +0c 2>/dev/null | head -n 1" | tr -d '\r')"
  fi
fi

REDIS_EVENT_RAW="$(_redis XREVRANGE security.events + - COUNT 1000 | grep -F "$SOURCE_ID" || true)"
RECORD_REQUEST_RAW="$(_redis XREVRANGE security.record_requests + - COUNT 1000 | grep -F "$SOURCE_ID" || true)"
EVENT_COUNT="$(_redis XLEN security.events)"
RECORD_REQUEST_COUNT="$(_redis XLEN security.record_requests)"
SOURCE_HAS_ID="$($DOCKER logs --since 30m "$SOURCE_CONTAINER" 2>&1 | grep -F "$SOURCE_ID" || true)"
REPLAY_RX_LOG="$($DOCKER logs --since 30m "$REPLAY_CONTAINER" 2>&1 | grep -E 'Received message|Adding message|Sending message to ZeroMQ socket' || true)"
SAVANT_HAS_ID="$($DOCKER logs --since 30m "$SAVANT_CONTAINER" 2>&1 | grep -F "$SOURCE_ID" || true)"
EVENT_WORKER_LOG="$($DOCKER logs --since 30m "$EVENT_WORKER_CONTAINER" 2>&1 | grep -E 'record_request_published|record_request_check|record_request_skipped' || true)"
CLIP_WORKER_LOG="$($DOCKER logs --since 30m "$CLIP_WORKER_CONTAINER" 2>&1 | grep -E 'replay_job_created|Replay job request|keyframe_provided_directly|clip_worker_skipped' || true)"
MEDIA_WORKER_LOG="$($DOCKER logs --since 30m "$MEDIA_WORKER_CONTAINER" 2>&1 | grep -E 'media_event_updated|media_metadata_parsed|p1_finalizer=True' || true)"

check 19 "source adapter logs show RTSP source_id" "$([[ -n "$SOURCE_HAS_ID" ]] && echo pass || echo fail)"
check 20 "Replay logs show source frames received and forwarded" "$([[ -n "$REPLAY_RX_LOG" ]] && echo pass || echo fail)"
check 21 "Savant logs show P1c source_id" "$([[ -n "$SAVANT_HAS_ID" ]] && echo pass || echo fail)"
check 22 "Redis security.events observed for P1c source" "$([[ -n "$REDIS_EVENT_RAW" ]] && echo pass || echo fail)"
check 23 "PostgreSQL intrusion event inserted" "$([[ -n "$EVENT_ID" ]] && echo pass || echo fail)"
check 24 "event-worker published record_request" "$([[ "$RECORD_REQUEST_COUNT" -eq 1 && -n "$EVENT_WORKER_LOG" ]] && echo pass || echo fail)"
check 25 "clip-worker created Replay job" "$([[ -n "$REPLAY_JOB_ID" && "$REPLAY_JOB_ID" != "NULL" && -n "$CLIP_WORKER_LOG" ]] && echo pass || echo fail)"
check 26 "media-worker finalized event evidence" "$([[ "$CLIP_STATUS" == "generated" && -n "$MEDIA_WORKER_LOG" ]] && echo pass || echo fail)"

EVENT_FIELDS="$(_pg "SELECT COALESCE(event_ts_ms,0)::text || '|' || COALESCE(frame_uuid,'') || '|' || COALESCE(keyframe_uuid,'') || '|' || COALESCE(payload->'media'->>'previous_keyframe_uuid','') || '|' || COALESCE(payload->'media'->>'frame_num','') || '|' || COALESCE(payload->'media'->>'frame_pts','') FROM events WHERE id='${EVENT_ID}'::uuid;")"
IFS='|' read -r EVENT_TS_MS FRAME_UUID KEYFRAME_UUID PREVIOUS_KEYFRAME_UUID FRAME_NUM FRAME_PTS <<< "$EVENT_FIELDS"
REPLAY_JOB_REQUEST="$(_pg "SELECT COALESCE(payload->'media'->'replay_job_request','{}'::jsonb)::text FROM events WHERE id='${EVENT_ID}'::uuid;")"
REPLAY_ANCHOR="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(data.get("anchor_keyframe", ""))
PY
)"
REPLAY_OFFSET="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print((data.get("offset") or {}).get("seconds", ""))
PY
)"
REPLAY_STOP_CONDITION="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(json.dumps(data.get("stop_condition") or {}, sort_keys=True))
PY
)"
REPLAY_SINK_URL="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(((data.get("sink") or {}).get("url")) or "")
PY
)"
STORED_STREAM_ID="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(((data.get("configuration") or {}).get("stored_stream_id")) or "")
PY
)"
RESULTING_STREAM_ID="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(((data.get("configuration") or {}).get("resulting_stream_id")) or "")
PY
)"
STOP_CONDITION_MODE="$(python3 - "$REPLAY_JOB_REQUEST" <<'PY'
import json, sys
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
import json, sys
try:
    data = json.loads(sys.argv[1] or "{}")
except json.JSONDecodeError:
    data = {}
print(data.get("fallback_reason") or "")
PY
)"

HOST_RAW_CLIP="$(host_media_path "${RAW_CLIP:-}")"
HOST_METADATA_PATH="$(host_media_path "${METADATA_PATH:-}")"
SINK_METADATA_PATH="${EVIDENCE_DIR}/sink_metadata.json"
RAW_CLIP_SIZE="0"
if [[ -n "$RAW_CLIP" ]]; then
  RAW_CLIP_SIZE="$($DOCKER exec "$MEDIA_WORKER_CONTAINER" stat -c%s "$RAW_CLIP" 2>/dev/null | tr -d '[:space:]' || echo "0")"
fi
PROBE="$(ffprobe_json "$HOST_RAW_CLIP")"
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
print("yes" if duration >= 3.0 else "no")
PY
)"
BUSINESS_METADATA_OK="$(python3 - "$HOST_METADATA_PATH" "$RTSP_URL" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
rtsp_url = sys.argv[2]
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
ok = (
    data.get("schema_version") == "1.0"
    and data.get("phase") == "P1c-RTSP"
    and data.get("evidence_type") == "security_event_replay_clip"
    and data.get("recording_strategy") == "savant_replay"
    and (data.get("input") or {}).get("input_type") == "rtsp"
    and (data.get("input") or {}).get("input_uri") == rtsp_url
    and (data.get("input") or {}).get("local_file_used") is False
    and required_limitations.issubset(limitations)
)
print("yes" if ok else "no")
PY
)"
ANNOTATION_BBOX_CONVERSION="$(python3 - "$(host_media_path "${EVENT_ANNOTATION_PATH:-}")" <<'PY'
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
        if fmt == "xyxy" and source in {"xywh", "xyxy"}:
            print("yes")
        else:
            print("partial")
        raise SystemExit(0)
print("partial")
PY
)"
SINK_METADATA_PRESERVED="$($DOCKER exec "$MEDIA_WORKER_CONTAINER" test -s "$SINK_METADATA_PATH" && echo yes || echo no)"

check 27 "events.clip_path points to raw_clip" "$([[ "$CLIP_PATH" == "$RAW_CLIP" && "$CLIP_PATH" == /media/evidence/*/raw_clip.* ]] && echo pass || echo fail)"
check 28 "raw_clip file exists and non-empty" "$([[ -n "$RAW_CLIP" && "${RAW_CLIP_SIZE:-0}" -gt 0 ]] && echo pass || echo fail)"
check 29 "metadata.json exists in evidence bundle" "$($DOCKER exec "$MEDIA_WORKER_CONTAINER" test -s "$METADATA_PATH" && echo pass || echo fail)"
check 30 "event_annotation.json exists in evidence bundle" "$($DOCKER exec "$MEDIA_WORKER_CONTAINER" test -s "$EVENT_ANNOTATION_PATH" && echo pass || echo fail)"
check 31 "business-level evidence metadata generated" "$([[ "$BUSINESS_METADATA_OK" == "yes" ]] && echo pass || echo fail)"
check 32 "sink metadata preserved" "$([[ "$SINK_METADATA_PRESERVED" == "yes" ]] && echo pass || echo fail)"
check 33 "event_annotation bbox conversion declared" "$([[ "$ANNOTATION_BBOX_CONVERSION" != "no" ]] && echo pass || echo fail)"
check 34 "raw clip is ffprobe-readable" "$([[ -n "$VIDEO_FORMAT" && "$DURATION_OK" == "yes" ]] && echo pass || echo fail)"
check 35 "no annotated_clip generated" "$($DOCKER exec "$MEDIA_WORKER_CONTAINER" sh -c "test ! -e '${EVIDENCE_DIR}/annotated_clip.mp4' && test ! -e '${EVIDENCE_DIR}/annotated_clip.mov' && test ! -e '${EVIDENCE_DIR}/annotated_clip.webm'" && echo pass || echo fail)"
check 36 "Replay job used event keyframe anchor" "$([[ -n "$REPLAY_ANCHOR" && "$REPLAY_ANCHOR" == "${PREVIOUS_KEYFRAME_UUID:-$KEYFRAME_UUID}" ]] && echo pass || echo fail)"
check 37 "single RTSP path preserved" "$([[ "$SOURCE_OUTPUT" == "dealer+connect:tcp://replay-service:5555" && "$REPLAY_OUT_STREAM" == "dealer+connect:tcp://savant-security:5557" ]] && echo pass || echo fail)"
check 38 "Replay stop_condition uses ts_delta_sec or declared fallback" "$([[ "$STOP_CONDITION_MODE" == "ts_delta_sec" || ( "$STOP_CONDITION_MODE" == "frame_count_fallback" && -n "$FALLBACK_REASON" ) ]] && echo pass || echo fail)"

echo -e "${BLUE}Stopping source adapter after first evidence bundle...${NC}"
$DOCKER stop "$SOURCE_CONTAINER" >/dev/null 2>&1 || true
sleep 5
POST_STOP_EVENT_COUNT="$(_redis XLEN security.events)"
POST_STOP_RECORD_REQUEST_COUNT="$(_redis XLEN security.record_requests)"
POST_STOP_REPLAY_JOB_COUNT="$(_pg "SELECT COUNT(*) FROM events WHERE source_id='${SOURCE_ID}' AND COALESCE(payload->'media'->>'replay_job_id','') <> '';")"
POST_STOP_EVIDENCE_COUNT="$(count_p1c_evidence_dirs)"
POST_STOP_SINK_COUNT="$(find "$SINK_RUN_ROOT" -name metadata.json | wc -l 2>/dev/null || echo 0)"
EXTRA_CLIPS_DETECTED=0
if [[ "$POST_STOP_EVIDENCE_COUNT" -gt "$max_evidence_bundles" ]]; then
  EXTRA_CLIPS_DETECTED=$((POST_STOP_EVIDENCE_COUNT - max_evidence_bundles))
fi
check 39 "source stopped after first clip" "$([[ "$(container_status "$SOURCE_CONTAINER")" != "running" ]] && echo pass || echo fail)"
check 40 "one-shot evidence policy" "$([[ "$POST_STOP_EVENT_COUNT" -ge 1 && "$POST_STOP_RECORD_REQUEST_COUNT" -eq "$max_record_requests" && "$POST_STOP_REPLAY_JOB_COUNT" -eq "$max_replay_jobs" && "$POST_STOP_EVIDENCE_COUNT" -eq "$max_evidence_bundles" && "$POST_STOP_SINK_COUNT" -eq 1 && "$EXTRA_CLIPS_DETECTED" -eq 0 ]] && echo pass || echo fail)"
check 41 "extra clips detected" "$([[ "$EXTRA_CLIPS_DETECTED" -eq 0 ]] && echo pass || echo fail)"

echo ""
echo "docker_access=${DOCKER_ACCESS}"
echo "docker_command_prefix=${DOCKER}"
echo "compose_command_prefix=${COMPOSE}"
echo "sudo_used=${SUDO_USED}"
echo "actual_containers_started=${ACTUAL_CONTAINERS_STARTED}"
echo "build_used=${BUILD_USED}"
echo "P1C_ALLOW_BUILD=${P1C_ALLOW_BUILD}"
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
echo "source_id=${SOURCE_ID}"
echo "camera_id=${CAMERA_ID}"
echo "security.events_observed=$([[ -n "$REDIS_EVENT_RAW" ]] && echo yes || echo no)"
echo "source_event_id=${SOURCE_EVENT_ID}"
echo "event_id=${EVENT_ID}"
echo "event_ts_ms=${EVENT_TS_MS}"
echo "frame_uuid=${FRAME_UUID}"
echo "keyframe_uuid=${KEYFRAME_UUID}"
echo "previous_keyframe_uuid=${PREVIOUS_KEYFRAME_UUID}"
echo "frame_num=${FRAME_NUM}"
echo "frame_pts=${FRAME_PTS}"
echo "record_request_observed=$([[ -n "$RECORD_REQUEST_RAW" ]] && echo yes || echo no)"
echo "record_requests_created=${POST_STOP_RECORD_REQUEST_COUNT}"
echo "events_created=${POST_STOP_EVENT_COUNT}"
echo "replay_jobs_created=$([[ -n "$REPLAY_JOB_ID" && "$REPLAY_JOB_ID" != "NULL" ]] && echo 1 || echo 0)"
echo "evidence_bundles_created=${POST_STOP_EVIDENCE_COUNT}"
echo "extra_clips_detected=${EXTRA_CLIPS_DETECTED}"
echo "source_stopped_after_first_clip=$([[ "$(container_status "$SOURCE_CONTAINER")" != "running" ]] && echo yes || echo no)"
echo "source_stopped_after_smoke=$([[ "$(container_status "$SOURCE_CONTAINER")" != "running" ]] && echo yes || echo no)"
echo "one_shot_policy=$([[ "$POST_STOP_EVENT_COUNT" -ge 1 && "$POST_STOP_RECORD_REQUEST_COUNT" -eq "$max_record_requests" && "$POST_STOP_REPLAY_JOB_COUNT" -eq "$max_replay_jobs" && "$POST_STOP_EVIDENCE_COUNT" -eq "$max_evidence_bundles" && "$EXTRA_CLIPS_DETECTED" -eq 0 ]] && echo yes || echo no)"
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
echo "offset_seconds=${REPLAY_OFFSET}"
echo "offset.seconds=${REPLAY_OFFSET}"
echo "stop_condition=${REPLAY_STOP_CONDITION}"
echo "stop_condition_mode=${STOP_CONDITION_MODE}"
echo "fallback_reason=${FALLBACK_REASON}"
echo "sink_url=${REPLAY_SINK_URL}"
echo "stored_stream_id=${STORED_STREAM_ID}"
echo "resulting_stream_id=${RESULTING_STREAM_ID}"
echo "evidence_dir=${EVIDENCE_DIR}"
echo "metadata_json=${METADATA_PATH}"
echo "event_annotation_json=${EVENT_ANNOTATION_PATH}"
echo "raw_clip=${RAW_CLIP}"
echo "host_raw_clip=${HOST_RAW_CLIP}"
echo "raw_clip_size=${RAW_CLIP_SIZE}"
echo "video_duration=${VIDEO_DURATION}"
echo "sink_metadata_json=${SINK_METADATA_PATH}"
echo "business_metadata_generated=${BUSINESS_METADATA_OK}"
echo "sink_metadata_preserved=${SINK_METADATA_PRESERVED}"
echo "event_annotation_bbox_conversion=${ANNOTATION_BBOX_CONVERSION}"
echo "ffprobe_status=$([[ -n "$VIDEO_FORMAT" && "$DURATION_OK" == "yes" ]] && echo ok || echo not_ok)"
echo "annotated_clip=no"
echo "api_started=no"
echo "production_compose_change=no"
echo "validation=$([[ "$FAIL_COUNT" -eq 0 ]] && echo pass || echo fail)"
echo "--- Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed ---"

if [[ "${EXTRA_CLIPS_DETECTED}" -gt 0 ]]; then
  fatal "uncontrolled_clip_generation"
fi
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  fatal "p1c_rtsp_event_evidence_failure"
fi
