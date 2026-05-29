#!/usr/bin/env bash
# R2.5 Single RTSP Camera Inference Smoke
#
# Verifies one RTSP stream through:
#   RTSP -> official source adapter -> savant-security -> YOLO26-pose
#   -> nvtracker -> ROI intrusion rule setup -> YOLOv8-Face full-frame primary
#   -> face-person association -> AdaFace -> Redis -> workers -> PostgreSQL.
#
# This is a single-camera smoke. It is not a performance test.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR"

API_BASE="${API_BASE:-http://localhost:8004}"
COMPOSE_FILE="${COMPOSE_FILE:-infra/docker-compose.c1-official-adapter.yml}"
NETWORK="${NETWORK:-c1-official-adapter_default}"
REDIS_CONTAINER="${REDIS_CONTAINER:-c1-official-redis}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"
CAMERA_CLI="scripts/camera_config_cli.py"
CONTROLLER="scripts/runtime/camera_source_controller.py"
MODULE_CONFIG="modules/savant_security/config/cameras.generated.yml"
SOURCES_CONFIG="infra/generated/sources.generated.yml"

R2_5_RTSP_URL="${R2_5_RTSP_URL:-}"
R2_5_CAMERA_ID="${R2_5_CAMERA_ID:-cam_r2_5_rtsp}"
R2_5_SOURCE_ID="${R2_5_SOURCE_ID:-r2_5_rtsp_1080movie}"
R2_5_CAMERA_NAME="${R2_5_CAMERA_NAME:-R2.5 RTSP Movie Test}"
R2_5_WAIT_SECONDS="${R2_5_WAIT_SECONDS:-180}"
R2_5_MIN_FACE_OBSERVATIONS="${R2_5_MIN_FACE_OBSERVATIONS:-1}"
R2_5_MIN_EVENTS="${R2_5_MIN_EVENTS:-0}"
R2_5_ROI_POLYGON="${R2_5_ROI_POLYGON:-[[0.05,0.05],[0.95,0.05],[0.95,0.95],[0.05,0.95]]}"
R2_5_FRAME_WIDTH="${R2_5_FRAME_WIDTH:-1920}"
R2_5_FRAME_HEIGHT="${R2_5_FRAME_HEIGHT:-1080}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
export DATABASE_URL
NO_PROXY="localhost,127.0.0.1,::1,${NO_PROXY:-}"
no_proxy="$NO_PROXY"
export NO_PROXY no_proxy

ZONE_NAME="${ZONE_NAME:-r2_5_full_frame}"
RULE_MIN_INSIDE_MS="${RULE_MIN_INSIDE_MS:-1}"
RULE_COOLDOWN_S="${RULE_COOLDOWN_S:-5}"
CONTAINER_NAME="video-analytics-source-${R2_5_SOURCE_ID}"

PASS=0
FAIL=0
STARTED=0

ok() { PASS=$((PASS + 1)); echo "OK   [$PASS] $1"; }
fail() { FAIL=$((FAIL + 1)); echo "FAIL [$FAIL] $1"; }
note() { echo "..   $1"; }
warn() { echo "WARN $1"; }

cleanup() {
  if [ "$STARTED" -eq 1 ]; then
    note "stopping source adapter ${R2_5_SOURCE_ID}"
    python3 "$CONTROLLER" stop --source-id "$R2_5_SOURCE_ID" >/tmp/r2_5_stop.log 2>&1 || true
  fi
}
trap cleanup EXIT

is_uint() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

mask_rtsp() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit

url = sys.argv[1]
try:
    p = urlsplit(url)
    host = p.hostname or ""
    if host:
        parts = host.split(".")
        if len(parts) == 4 and all(part.isdigit() for part in parts):
            host = ".".join(parts[:2] + ["x", "x"])
        elif "." in host:
            host = host.split(".", 1)[0] + ".<redacted>"
    netloc = host
    if p.port:
        netloc += f":{p.port}"
    print(urlunsplit((p.scheme, netloc, p.path, "", "")))
except Exception:
    print("<rtsp-url-redacted>")
PY
}

sql_quote() {
  printf "%s" "$1" | sed "s/'/''/g"
}

cleanup_existing_camera_config() {
  local camera_id="$1"
  local source_id="$2"
  local q_camera_id q_source_id deleted
  q_camera_id="$(sql_quote "$camera_id")"
  q_source_id="$(sql_quote "$source_id")"
  deleted=$(psql_scalar "WITH deleted AS (
    DELETE FROM cameras
    WHERE id = '$q_camera_id' OR source_id = '$q_source_id'
    RETURNING id
  )
  SELECT COUNT(*) FROM deleted;" 2>/tmp/r2_5_camera_cleanup.log || echo "ERROR")
  if [ "$deleted" = "ERROR" ]; then
    fail "camera config cleanup failed; see /tmp/r2_5_camera_cleanup.log"
    return 1
  fi
  ok "old camera config cleared (deleted_cameras=${deleted:-0})"
  return 0
}

psql_scalar() {
  local sql="$1"
  if command -v psql >/dev/null 2>&1; then
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -tAc "$sql"
  else
    docker exec "$PG_CONTAINER" psql -U video -d video_analytics -v ON_ERROR_STOP=1 -tAc "$sql"
  fi
}

psql_table() {
  local sql="$1"
  if command -v psql >/dev/null 2>&1; then
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -P pager=off -c "$sql"
  else
    docker exec "$PG_CONTAINER" psql -U video -d video_analytics -v ON_ERROR_STOP=1 -P pager=off -c "$sql"
  fi
}

post_with_cli() {
  local label="$1"
  shift
  local log="/tmp/r2_5_${label}.log"
  if python3 "$CAMERA_CLI" "$@" >"$log" 2>&1; then
    ok "$label configured"
    return 0
  fi
  local rc=$?
  if [ "$rc" -eq 3 ]; then
    ok "$label already exists"
    return 0
  fi
  fail "$label failed; see $log"
  return "$rc"
}

polygon_to_cli_points() {
  python3 - "$R2_5_ROI_POLYGON" "$R2_5_FRAME_WIDTH" "$R2_5_FRAME_HEIGHT" <<'PY'
import ast
import json
import sys

raw, width_s, height_s = sys.argv[1], sys.argv[2], sys.argv[3]
width = float(width_s)
height = float(height_s)

try:
    if raw.strip().startswith("["):
        pts = json.loads(raw)
    else:
        pts = [
            [float(x), float(y)]
            for x, y in (pair.split(",", 1) for pair in raw.split(";") if pair.strip())
        ]
except Exception:
    pts = ast.literal_eval(raw)

if not isinstance(pts, list) or not (3 <= len(pts) <= 10):
    raise SystemExit("ROI polygon must contain 3 to 10 points")

clean = []
for idx, pt in enumerate(pts):
    if not isinstance(pt, (list, tuple)) or len(pt) != 2:
        raise SystemExit(f"ROI point #{idx + 1} must be [x, y]")
    x = float(pt[0])
    y = float(pt[1])
    clean.append([x, y])

normalized = all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in clean)
if normalized:
    clean = [[x * width, y * height] for x, y in clean]
    mode = f"normalized_scaled_to_pixel:{int(width)}x{int(height)}"
else:
    mode = "pixel"

print(";".join(f"{x:.3f},{y:.3f}" for x, y in clean))
print(mode, file=sys.stderr)
PY
}

verify_camera_config() {
  python3 - "$API_BASE" "$R2_5_CAMERA_ID" "$R2_5_SOURCE_ID" "$R2_5_RTSP_URL" <<'PY'
import json
import sys
import urllib.request

api_base, camera_id, expected_source_id, expected_uri = sys.argv[1:5]
url = api_base.rstrip("/") + f"/api/v1/cameras/{camera_id}"
with urllib.request.urlopen(url, timeout=30) as resp:
    payload = json.loads(resp.read().decode("utf-8"))
cam = payload.get("data") or {}
errors = []
if cam.get("source_id") != expected_source_id:
    errors.append(f"source_id mismatch: expected {expected_source_id}, got {cam.get('source_id')}")
if cam.get("rtsp_url") != expected_uri:
    errors.append("rtsp_url mismatch for existing camera_id")
if not cam.get("enabled", False):
    errors.append("camera is not enabled")
if errors:
    print("FAIL: " + "; ".join(errors))
    raise SystemExit(1)
print(f"camera verified: camera_id={camera_id} source_id={expected_source_id} enabled=true")
PY
}

verify_runtime_config() {
  python3 - "$MODULE_CONFIG" "$SOURCES_CONFIG" "$R2_5_CAMERA_ID" "$R2_5_SOURCE_ID" "$ZONE_NAME" <<'PY'
import sys
import yaml

module_path, sources_path, camera_id, source_id, zone_name = sys.argv[1:6]
module_doc = yaml.safe_load(open(module_path, encoding="utf-8")) or {}
sources_doc = yaml.safe_load(open(sources_path, encoding="utf-8")) or {}
cam = (module_doc.get("cameras") or {}).get(camera_id) or {}
src = (sources_doc.get("sources") or {}).get(camera_id) or {}
zone = (cam.get("zones") or {}).get(zone_name) or {}
rule = (cam.get("rules") or {}).get("intrusion") or {}
missing = []
if cam.get("source_id") != source_id:
    missing.append("camera source_id")
if src.get("source_id") != source_id:
    missing.append("source manifest source_id")
if src.get("adapter_type") != "gstreamer":
    missing.append("source adapter_type")
if not str(src.get("uri", "")).startswith(("rtsp://", "rtsps://")):
    missing.append("RTSP uri")
if zone.get("type") != "polygon" or not zone.get("points"):
    missing.append("ROI polygon")
if rule.get("zone") != zone_name:
    missing.append("intrusion rule binding")
if src.get("zmq_endpoint") != "dealer+connect:tcp://savant-security:5555":
    missing.append("ZMQ endpoint")
if missing:
    print("FAIL: runtime config missing " + ", ".join(missing))
    raise SystemExit(1)
print(f"runtime config verified: camera_id={camera_id} source_id={source_id}")
print(f"roi polygon points={zone.get('points')}")
print("intrusion rule verified: rule_type=intrusion zone=" + zone_name)
print("source adapter verified: adapter_type=gstreamer zmq_endpoint=" + src.get("zmq_endpoint", ""))
PY
}

print_adapter_env() {
  echo "--- Adapter env ---"
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER_NAME" 2>/dev/null \
    | while IFS= read -r line; do
        case "$line" in
          SOURCE_ID=*|ZMQ_ENDPOINT=*) echo "$line" ;;
          LOCATION=*) echo "LOCATION=$(mask_rtsp "${line#LOCATION=}")" ;;
          RTSP_URI=*) echo "RTSP_URI=$(mask_rtsp "${line#RTSP_URI=}")" ;;
        esac
      done
}

echo "=== R2.5 Single RTSP Camera Inference Smoke ==="
echo "  API_BASE=$API_BASE"
echo "  COMPOSE_FILE=$COMPOSE_FILE"
echo "  NETWORK=$NETWORK"
echo "  CAMERA_ID=$R2_5_CAMERA_ID"
echo "  SOURCE_ID=$R2_5_SOURCE_ID"
echo "  RTSP_URL=$(mask_rtsp "$R2_5_RTSP_URL")"
echo "  WAIT_SECONDS=$R2_5_WAIT_SECONDS"
echo "  MIN_FACE_OBSERVATIONS=$R2_5_MIN_FACE_OBSERVATIONS"
echo "  MIN_EVENTS=$R2_5_MIN_EVENTS"
echo ""

if [ -z "$R2_5_RTSP_URL" ]; then
  fail "R2_5_RTSP_URL is required; refusing to run without a real RTSP input"
  exit 1
fi
case "$R2_5_RTSP_URL" in
  rtsp://*|rtsps://*) ok "R2_5_RTSP_URL uses RTSP scheme" ;;
  *) fail "R2_5_RTSP_URL must start with rtsp:// or rtsps://"; exit 1 ;;
esac
case "$R2_5_RTSP_URL" in
  *"<LAN_STREAM_HOST>"*) fail "R2_5_RTSP_URL still contains placeholder host"; exit 1 ;;
esac

for value_name in R2_5_WAIT_SECONDS R2_5_MIN_FACE_OBSERVATIONS R2_5_MIN_EVENTS; do
  value="${!value_name}"
  if is_uint "$value"; then
    ok "$value_name is numeric"
  else
    fail "$value_name must be a non-negative integer"
    exit 1
  fi
done

command -v docker >/dev/null 2>&1 || { fail "docker CLI not found"; exit 1; }
docker info >/dev/null 2>&1 || { fail "docker daemon not reachable"; exit 1; }
command -v curl >/dev/null 2>&1 || { fail "curl not found"; exit 1; }
command -v python3 >/dev/null 2>&1 || { fail "python3 not found"; exit 1; }
ok "required local CLIs are available"

if command -v ffprobe >/dev/null 2>&1; then
  note "probing RTSP URL with ffprobe"
  if command -v timeout >/dev/null 2>&1; then
    timeout 20 ffprobe -v error -rtsp_transport tcp "$R2_5_RTSP_URL" >/tmp/r2_5_ffprobe.log 2>&1
  else
    ffprobe -v error -rtsp_transport tcp "$R2_5_RTSP_URL" >/tmp/r2_5_ffprobe.log 2>&1
  fi
  if [ "$?" -eq 0 ]; then
    ok "ffprobe reached RTSP URL"
  else
    fail "ffprobe could not read RTSP URL; see /tmp/r2_5_ffprobe.log"
    exit 1
  fi
else
  warn "ffprobe not found; continuing without RTSP preflight"
fi

note "starting c1-official stack"
if docker compose -f "$COMPOSE_FILE" up -d \
     redis postgres api savant-security event-worker face-worker metadata-sink video-file-sink >/tmp/r2_5_compose_up.log 2>&1; then
  ok "compose stack up"
else
  fail "compose up failed; see /tmp/r2_5_compose_up.log"
  exit 1
fi

note "waiting for PostgreSQL"
for _ in $(seq 1 30); do
  if docker exec "$PG_CONTAINER" pg_isready -U video -d video_analytics >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$PG_CONTAINER" pg_isready -U video -d video_analytics >/dev/null 2>&1 \
  && ok "PostgreSQL healthy" \
  || { fail "PostgreSQL unhealthy"; exit 1; }

note "waiting for API"
for _ in $(seq 1 30); do
  if curl --noproxy '*' -fsS "$API_BASE/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl --noproxy '*' -fsS "$API_BASE/health" >/dev/null 2>&1 \
  && ok "API healthy" \
  || { fail "API not responding at $API_BASE"; exit 1; }

ROI_PARSE_LOG="/tmp/r2_5_roi_parse.log"
if ROI_POINTS=$(polygon_to_cli_points 2>"$ROI_PARSE_LOG"); then
  ok "ROI polygon parsed"
else
  fail "ROI polygon parse failed; see $ROI_PARSE_LOG"
  exit 1
fi
ROI_MODE=$(cat "$ROI_PARSE_LOG" 2>/dev/null || true)
note "ROI polygon mode=${ROI_MODE:-unknown} points=$ROI_POINTS"

cleanup_existing_camera_config "$R2_5_CAMERA_ID" "$R2_5_SOURCE_ID" || exit 1

post_with_cli add_camera add-camera \
  --api-base-url "$API_BASE" \
  --camera-id "$R2_5_CAMERA_ID" \
  --source-id "$R2_5_SOURCE_ID" \
  --name "$R2_5_CAMERA_NAME" \
  --uri "$R2_5_RTSP_URL" \
  --enabled true || exit 1

verify_camera_config && ok "camera config matches this run" || exit 1

post_with_cli add_zone add-zone \
  --api-base-url "$API_BASE" \
  --camera-id "$R2_5_CAMERA_ID" \
  --zone-name "$ZONE_NAME" \
  --polygon "$ROI_POINTS" || exit 1

post_with_cli add_intrusion_rule add-intrusion-rule \
  --api-base-url "$API_BASE" \
  --camera-id "$R2_5_CAMERA_ID" \
  --zone "$ZONE_NAME" \
  --min-inside-ms "$RULE_MIN_INSIDE_MS" \
  --cooldown-s "$RULE_COOLDOWN_S" \
  --severity medium \
  --snapshot-required false \
  --clip-required false || exit 1

note "exporting runtime configs"
if python3 "$CAMERA_CLI" export-runtime \
     --api-base-url "$API_BASE" \
     --module-config-output "$MODULE_CONFIG" \
     --sources-output "$SOURCES_CONFIG" >/tmp/r2_5_export.log 2>&1; then
  ok "runtime configs exported"
else
  fail "runtime config export failed; see /tmp/r2_5_export.log"
  exit 1
fi
verify_runtime_config && ok "ROI/rule/source runtime config verified" || exit 1

note "restarting savant-security to load generated camera config"
if docker compose -f "$COMPOSE_FILE" restart savant-security >/tmp/r2_5_savant_restart.log 2>&1; then
  ok "savant-security restarted"
else
  fail "savant-security restart failed; see /tmp/r2_5_savant_restart.log"
  exit 1
fi
sleep 8

note "removing any stale adapter container for this source_id"
python3 "$CONTROLLER" stop --source-id "$R2_5_SOURCE_ID" >/tmp/r2_5_pre_stop.log 2>&1 || true

note "starting RTSP source adapter via camera_source_controller.py"
if python3 "$CONTROLLER" start \
     --sources "$SOURCES_CONFIG" \
     --source-id "$R2_5_SOURCE_ID" \
     --network "$NETWORK" >/tmp/r2_5_start.log 2>&1; then
  STARTED=1
  ok "source adapter started"
else
  fail "source adapter start failed; see /tmp/r2_5_start.log"
  exit 1
fi

sleep 5
python3 "$CONTROLLER" status --source-id "$R2_5_SOURCE_ID" >/tmp/r2_5_status.log 2>&1
grep -q "Up " /tmp/r2_5_status.log \
  && ok "source adapter container is Up" \
  || { fail "source adapter container is not Up; see /tmp/r2_5_status.log"; exit 1; }
print_adapter_env

note "waiting up to ${R2_5_WAIT_SECONDS}s for face observations"
Q_SOURCE_ID="$(sql_quote "$R2_5_SOURCE_ID")"
FACE_COUNT=0
FACE_VALID_COUNT=0
EVENT_COUNT=0
for _ in $(seq 1 "$R2_5_WAIT_SECONDS"); do
  FACE_COUNT=$(psql_scalar "SELECT COUNT(*) FROM face_observations WHERE source_id = '$Q_SOURCE_ID';" 2>/dev/null || echo 0)
  FACE_VALID_COUNT=$(psql_scalar "SELECT COUNT(*) FROM face_observations WHERE source_id = '$Q_SOURCE_ID' AND embedding_model = 'adaface' AND embedding_dim = 512 AND embedding_norm BETWEEN 0.90 AND 1.10;" 2>/dev/null || echo 0)
  EVENT_COUNT=$(psql_scalar "SELECT COUNT(*) FROM events WHERE source_id = '$Q_SOURCE_ID';" 2>/dev/null || echo 0)
  if [ "${FACE_VALID_COUNT:-0}" -ge "$R2_5_MIN_FACE_OBSERVATIONS" ] 2>/dev/null; then
    if [ "$R2_5_MIN_EVENTS" -eq 0 ] || [ "${EVENT_COUNT:-0}" -ge "$R2_5_MIN_EVENTS" ] 2>/dev/null; then
      break
    fi
  fi
  sleep 1
done

REDIS_FACE_XLEN=$(docker exec "$REDIS_CONTAINER" redis-cli XLEN security.face_observations 2>/dev/null || echo 0)
REDIS_FACE_SOURCE_COUNT=$(docker exec "$REDIS_CONTAINER" redis-cli --raw XREVRANGE security.face_observations + - COUNT 500 2>/dev/null \
  | awk -v sid="$R2_5_SOURCE_ID" '$0 == sid {c++} END {print c+0}')
REDIS_EVENTS_XLEN=$(docker exec "$REDIS_CONTAINER" redis-cli XLEN security.events 2>/dev/null || echo 0)
REDIS_EVENTS_SOURCE_COUNT=$(docker exec "$REDIS_CONTAINER" redis-cli --raw XREVRANGE security.events + - COUNT 500 2>/dev/null \
  | awk -v sid="$R2_5_SOURCE_ID" '$0 == sid {c++} END {print c+0}')

echo "--- Redis results ---"
echo "security.face_observations XLEN=$REDIS_FACE_XLEN source_id_matches_last_500=$REDIS_FACE_SOURCE_COUNT"
docker exec "$REDIS_CONTAINER" redis-cli XREVRANGE security.face_observations + - COUNT 5 || true
echo "security.events XLEN=$REDIS_EVENTS_XLEN source_id_matches_last_500=$REDIS_EVENTS_SOURCE_COUNT"
docker exec "$REDIS_CONTAINER" redis-cli XREVRANGE security.events + - COUNT 5 || true

echo "--- PostgreSQL face_observations ---"
echo "face_observations source_id_count=${FACE_COUNT:-0} valid_adaface_512_norm_count=${FACE_VALID_COUNT:-0}"
psql_table "SELECT source_observation_id, camera_id, source_id, track_id,
       timestamp_ms, quality, embedding_model, embedding_dim,
       ROUND(embedding_norm::numeric, 6) AS embedding_norm, created_at
FROM face_observations
WHERE source_id = '$Q_SOURCE_ID'
ORDER BY created_at DESC
LIMIT 10;" || true

if [ "${REDIS_FACE_SOURCE_COUNT:-0}" -ge "$R2_5_MIN_FACE_OBSERVATIONS" ] 2>/dev/null; then
  ok "Redis security.face_observations contains this source_id"
else
  fail "Redis security.face_observations has fewer than $R2_5_MIN_FACE_OBSERVATIONS matches for this source_id"
fi

if [ "${FACE_COUNT:-0}" -ge "$R2_5_MIN_FACE_OBSERVATIONS" ] 2>/dev/null; then
  ok "PostgreSQL face_observations contains this source_id"
else
  fail "PostgreSQL face_observations has fewer than $R2_5_MIN_FACE_OBSERVATIONS rows for this source_id"
fi

if [ "${FACE_VALID_COUNT:-0}" -ge "$R2_5_MIN_FACE_OBSERVATIONS" ] 2>/dev/null; then
  ok "face_observations embedding_model=adaface embedding_dim=512 norm in [0.90,1.10]"
else
  fail "face_observations did not satisfy AdaFace 512-d norm validation"
fi

echo "--- PostgreSQL events ---"
echo "events source_id_count=${EVENT_COUNT:-0}"
psql_table "SELECT id, source_event_id, event_type, camera_id, source_id,
       track_id, confidence, created_at
FROM events
WHERE source_id = '$Q_SOURCE_ID'
ORDER BY created_at DESC
LIMIT 10;" || true

if [ "$R2_5_MIN_EVENTS" -gt 0 ]; then
  if [ "${EVENT_COUNT:-0}" -ge "$R2_5_MIN_EVENTS" ] 2>/dev/null; then
    ok "PostgreSQL events contains required source_id rows"
  else
    fail "PostgreSQL events has fewer than $R2_5_MIN_EVENTS rows for this source_id"
  fi
else
  ok "events are optional for this run because R2_5_MIN_EVENTS=0"
fi

echo ""
echo "=== R2.5 Result: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
  echo "R2.5 single RTSP camera inference: FAIL"
  exit 1
fi
echo "R2.5 single RTSP camera inference: PASS"
exit 0
