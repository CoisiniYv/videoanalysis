#!/usr/bin/env bash
# Phase C1.2 — Official adapter runtime smoke test
#
# Verifies the C1.2 closed loop: configure a camera through the API,
# export both runtime config files, bring up the
# c1-official-adapter compose, attach a single source via the
# camera_source_controller, wait for the evidence files, then detach
# the source cleanly.
#
# This script is GPU + Docker dependent. Honest exit codes:
#   0  ok — every step passed
#   1  hard failure (env / API / DB / pipeline)
#   77 SKIPPED — environment not provisioned (per autotools convention)
#
# Required inputs (env vars):
#   C1_TEST_SOURCE_URI   rtsp:// or file:// the controller should attach.
#                        Mandatory. No default — we will NOT silently
#                        pick a stale local file.
#
# Optional:
#   API_BASE             default http://localhost:8004 (c1-official-api port)
#   COMPOSE_FILE         default infra/docker-compose.c1-official-adapter.yml
#   WAIT_SECONDS         default 90
#   PG_CONTAINER         default c1-official-postgres
#   CAMERA_ID            default cam_c1_2
#   SOURCE_ID            default c1_2_test

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR"

API_BASE="${API_BASE:-http://localhost:8004}"
COMPOSE_FILE="${COMPOSE_FILE:-infra/docker-compose.c1-official-adapter.yml}"
WAIT_SECONDS="${WAIT_SECONDS:-90}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"
CAMERA_ID="${CAMERA_ID:-cam_c1_2}"
SOURCE_ID="${SOURCE_ID:-c1_2_test}"
CONTROLLER="scripts/runtime/camera_source_controller.py"
EXPORTER="scripts/config/export_runtime_configs.py"
MODULE_CONFIG="modules/savant_security/config/cameras.generated.yml"
SOURCES_CONFIG="infra/generated/sources.generated.yml"
NETWORK="c1-official-adapter_default"

PASS=0
FAIL=0
ok()   { echo "OK   [$((++PASS))] $1"; }
fail() { echo "FAIL [$((++FAIL))] $1"; }
note() { echo "..   $1"; }
skip() { echo "SKIP $1"; exit 77; }

# Never print rtsp_url with credentials.
safe_scheme() {
  local uri="${1:-}"
  if [ -z "$uri" ]; then echo ""; return; fi
  echo "${uri%%://*}"
}

echo "=== Phase C1.2 Official Adapter Runtime Smoke ==="
echo "  COMPOSE_FILE=$COMPOSE_FILE"
echo "  API_BASE=$API_BASE"
echo "  CAMERA_ID=$CAMERA_ID  SOURCE_ID=$SOURCE_ID"
echo "  C1_TEST_SOURCE_URI scheme=$(safe_scheme "${C1_TEST_SOURCE_URI:-}")"
echo ""

# ── Environment preflight ──────────────────────────────────────────────────

if [ -z "${C1_TEST_SOURCE_URI:-}" ]; then
  skip "C1_TEST_SOURCE_URI is unset — cannot attach a real source."
fi
case "$C1_TEST_SOURCE_URI" in
  rtsp://*|rtsps://*|file://*) ;;
  *) skip "C1_TEST_SOURCE_URI must use rtsp://, rtsps://, or file:// scheme.";;
esac

command -v docker >/dev/null 2>&1 || skip "docker CLI not on PATH."
docker info >/dev/null 2>&1 || skip "docker daemon not reachable."
command -v curl   >/dev/null 2>&1 || skip "curl not on PATH."
command -v python3 >/dev/null 2>&1 || skip "python3 not on PATH."

if ! command -v nvidia-smi >/dev/null 2>&1; then
  skip "nvidia-smi not on PATH — Savant module requires NVIDIA GPU."
fi

# ── Bring up compose ───────────────────────────────────────────────────────

note "starting compose stack (postgres, redis, api, savant-security, sinks, event-worker)"
if ! docker compose -f "$COMPOSE_FILE" up -d \
       redis postgres api savant-security event-worker \
       metadata-sink video-file-sink >/dev/null; then
  fail "compose up failed"
  exit 1
fi
ok "compose stack up"

note "waiting for postgres to be ready"
for _ in $(seq 1 30); do
  if docker exec "$PG_CONTAINER" pg_isready -U video -d video_analytics >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$PG_CONTAINER" pg_isready -U video -d video_analytics >/dev/null 2>&1 \
  && ok "postgres healthy" \
  || { fail "postgres unhealthy"; exit 1; }

note "waiting for FastAPI /health"
for _ in $(seq 1 30); do
  if curl -fsS "$API_BASE/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "$API_BASE/health" >/dev/null && ok "API responds" \
  || { fail "API not responding at $API_BASE"; exit 1; }

# ── Configure the camera via API ───────────────────────────────────────────

note "creating camera ${CAMERA_ID} (source_id=${SOURCE_ID}) via API"
CAM_HTTP=$(curl -s -o /tmp/c1_2_cam.json -w "%{http_code}" \
  -X POST "$API_BASE/api/v1/cameras" \
  -H "Content-Type: application/json" \
  -d "$(cat <<JSON
{
  "id": "$CAMERA_ID",
  "source_id": "$SOURCE_ID",
  "name": "C1.2 Smoke Camera",
  "rtsp_url": "$C1_TEST_SOURCE_URI",
  "gpu_id": 0,
  "enabled": true
}
JSON
)")
if [ "$CAM_HTTP" = "200" ] || [ "$CAM_HTTP" = "409" ]; then
  ok "camera POST → $CAM_HTTP (200 = created, 409 = already exists)"
else
  fail "camera POST returned $CAM_HTTP"
  exit 1
fi

note "creating perimeter polygon zone"
ZONE_HTTP=$(curl -s -o /tmp/c1_2_zone.json -w "%{http_code}" \
  -X POST "$API_BASE/api/v1/cameras/${CAMERA_ID}/zones" \
  -H "Content-Type: application/json" \
  -d '{"zone_name":"perimeter","zone_type":"polygon","points":[[100,100],[1820,100],[1820,980],[100,980]]}')
if [ "$ZONE_HTTP" = "200" ] || [ "$ZONE_HTTP" = "409" ]; then
  ok "zone POST → $ZONE_HTTP"
else
  fail "zone POST returned $ZONE_HTTP"
  exit 1
fi

note "creating intrusion rule"
RULE_HTTP=$(curl -s -o /tmp/c1_2_rule.json -w "%{http_code}" \
  -X POST "$API_BASE/api/v1/cameras/${CAMERA_ID}/rules" \
  -H "Content-Type: application/json" \
  -d '{"rule_type":"intrusion","enabled":true,"config":{"zone":"perimeter","min_inside_ms":1,"cooldown_s":5,"severity":"medium","snapshot_required":true,"clip_required":true}}')
if [ "$RULE_HTTP" = "200" ] || [ "$RULE_HTTP" = "409" ]; then
  ok "intrusion rule POST → $RULE_HTTP"
else
  fail "rule POST returned $RULE_HTTP"
  exit 1
fi

# ── Export the runtime configs ─────────────────────────────────────────────

note "running export_runtime_configs.py"
if python3 "$EXPORTER" \
     --api-base-url "$API_BASE" \
     --module-config-output "$MODULE_CONFIG" \
     --sources-output "$SOURCES_CONFIG" >/tmp/c1_2_export.log 2>&1; then
  ok "configs exported"
else
  fail "exporter failed; see /tmp/c1_2_export.log"
  exit 1
fi
[ -s "$MODULE_CONFIG" ]  && ok "module config exists"  || { fail "module config missing"; exit 1; }
[ -s "$SOURCES_CONFIG" ] && ok "sources config exists" || { fail "sources config missing"; exit 1; }

# Restart savant-security so it picks up the new cameras.generated.yml.
note "restarting savant-security to load the fresh cameras.generated.yml"
docker compose -f "$COMPOSE_FILE" restart savant-security >/dev/null \
  && ok "savant-security restarted" \
  || { fail "savant-security restart failed"; exit 1; }
sleep 8

# ── Attach the source via the controller ───────────────────────────────────

note "attaching source ${SOURCE_ID} via camera_source_controller"
TESTVIDEO_MOUNT=""
if [ -d "$PROJECT_DIR/testVideo" ]; then
  TESTVIDEO_MOUNT="${PROJECT_DIR}/testVideo:/testVideo:ro"
fi

if python3 "$CONTROLLER" start \
     --sources "$SOURCES_CONFIG" \
     --source-id "$SOURCE_ID" \
     --network "$NETWORK" \
     ${TESTVIDEO_MOUNT:+--testvideo-mount "$TESTVIDEO_MOUNT"} \
     >/tmp/c1_2_start.log 2>&1; then
  ok "controller start succeeded"
else
  fail "controller start failed; see /tmp/c1_2_start.log"
  exit 1
fi
sleep 5
python3 "$CONTROLLER" status --source-id "$SOURCE_ID" >/tmp/c1_2_status.log 2>&1
grep -q "Up " /tmp/c1_2_status.log && ok "adapter container reports Up" \
  || fail "adapter container not Up (see /tmp/c1_2_status.log)"

# ── Wait for intrusion events ──────────────────────────────────────────────

note "waiting up to ${WAIT_SECONDS}s for intrusion events"
COUNT=0
for _ in $(seq 1 "$WAIT_SECONDS"); do
  COUNT=$(docker exec "$PG_CONTAINER" psql -U video -d video_analytics -tAc \
    "SELECT COUNT(*) FROM events WHERE event_type='intrusion' AND camera_id='$CAMERA_ID'" 2>/dev/null || echo 0)
  if [ "${COUNT:-0}" -gt 0 ] 2>/dev/null; then
    break
  fi
  sleep 1
done
if [ "${COUNT:-0}" -gt 0 ] 2>/dev/null; then
  ok "intrusion events generated (count=$COUNT)"
else
  fail "no intrusion events after ${WAIT_SECONDS}s"
fi

# ── Stop the source-side traffic before running evidence-worker ────────────

note "stopping source / metadata-sink / video-file-sink to freeze the snapshot input"
python3 "$CONTROLLER" stop --source-id "$SOURCE_ID" >/dev/null 2>&1 \
  && ok "controller stop succeeded" \
  || fail "controller stop failed"
docker compose -f "$COMPOSE_FILE" stop metadata-sink video-file-sink >/dev/null 2>&1

# ── Generate evidence ─────────────────────────────────────────────────────

note "running evidence-worker one-shot"
docker compose -f "$COMPOSE_FILE" run --rm evidence-worker >/tmp/c1_2_ev.log 2>&1 \
  && ok "evidence-worker completed" \
  || fail "evidence-worker failed; see /tmp/c1_2_ev.log"

# ── Verify evidence on disk + over HTTP ────────────────────────────────────

EVENT_JSON=$(docker exec "$PG_CONTAINER" psql -U video -d video_analytics -tAc "
  SELECT row_to_json(e) FROM (
    SELECT id, snapshot_path, clip_path,
           payload->'media'->>'annotated_snapshot_path' AS annotated_snapshot_path,
           payload->'media'->>'annotated_clip_path'     AS annotated_clip_path
    FROM events
    WHERE event_type='intrusion' AND camera_id='$CAMERA_ID' AND snapshot_path IS NOT NULL
    ORDER BY created_at DESC LIMIT 1
  ) e" 2>/dev/null || echo "{}")
EVENT_ID=$(echo "$EVENT_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
if [ -n "$EVENT_ID" ]; then
  ok "evidence row exists ($EVENT_ID)"
else
  fail "no evidence row with snapshot_path found"
fi

container_to_host() {
  echo "/data/video-analytics/media${1#/media}"
}
SNAP=$(echo "$EVENT_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('snapshot_path',''))" 2>/dev/null)
ANN_SNAP=$(echo "$EVENT_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('annotated_snapshot_path',''))" 2>/dev/null)
CLIP=$(echo "$EVENT_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('clip_path',''))" 2>/dev/null)
ANN_CLIP=$(echo "$EVENT_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('annotated_clip_path',''))" 2>/dev/null)

for label_path in "snapshot:$SNAP" "annotated_snapshot:$ANN_SNAP" "clip:$CLIP" "annotated_clip:$ANN_CLIP"; do
  label="${label_path%%:*}"
  path="${label_path#*:}"
  [ -z "$path" ] && { fail "$label path is empty in DB"; continue; }
  host_path="$(container_to_host "$path")"
  [ -f "$host_path" ] && ok "$label exists on disk ($host_path)" \
                      || fail "$label missing on disk ($host_path)"
done

note "checking media URLs over the API"
for media in snapshot annotated_snapshot clip annotated_clip; do
  url=$(curl -fsS "$API_BASE/api/v1/events/$EVENT_ID" | \
    python3 -c "import sys,json; d=json.load(sys.stdin).get('data',{}); print(d.get('${media}_url') or '')" 2>/dev/null)
  if [ -z "$url" ]; then
    fail "API missing ${media}_url"
    continue
  fi
  http=$(curl -s -o /dev/null -w "%{http_code}" "$API_BASE$url")
  [ "$http" = "200" ] && ok "${media}_url HTTP 200" || fail "${media}_url HTTP $http"
done

# ── Verify module survived the detach (still up) ───────────────────────────

if docker compose -f "$COMPOSE_FILE" ps savant-security --format '{{.State}}' | grep -qi running; then
  ok "savant-security still running after source detach"
else
  fail "savant-security stopped — detach broke the module"
fi

# ── Result ─────────────────────────────────────────────────────────────────

echo ""
echo "=== Result: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
