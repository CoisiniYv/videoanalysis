#!/usr/bin/env bash
# R3.1A behavior event evidence smoke.
#
# Runs one MediaMTX RTSP source through the existing R2.5 smoke path, then
# verifies intrusion event -> evidence_task -> API evidence response.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR"

COMPOSE_FILE="${COMPOSE_FILE:-infra/docker-compose.c1-official-adapter.yml}"
API_BASE="${API_BASE:-http://localhost:8004}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"

R3_1A_RTSP_URL="${R3_1A_RTSP_URL:-}"
R3_1A_CAMERA_ID="${R3_1A_CAMERA_ID:-cam_r3_1a_rtsp_movie}"
R3_1A_SOURCE_ID="${R3_1A_SOURCE_ID:-r3_1a_rtsp_movie_$(date +%s)}"
R3_1A_CAMERA_NAME="${R3_1A_CAMERA_NAME:-R3.1A RTSP Movie Camera}"
R3_1A_WAIT_SECONDS="${R3_1A_WAIT_SECONDS:-180}"
R3_1A_REBUILD_SERVICES="${R3_1A_REBUILD_SERVICES:-1}"

PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); echo "OK   [$PASS] $1"; }
fail() { FAIL=$((FAIL + 1)); echo "FAIL [$FAIL] $1"; }
note() { echo "..   $1"; }

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

if [ -z "$R3_1A_RTSP_URL" ]; then
  fail "R3_1A_RTSP_URL is required"
  echo "PASS=$PASS FAIL=$FAIL"
  exit 1
fi

if command -v ffprobe >/dev/null 2>&1; then
  note "probing RTSP URL before rebuilding services"
  if timeout 20 ffprobe -rtsp_transport tcp "$R3_1A_RTSP_URL" >/tmp/r3_1a_ffprobe.log 2>&1; then
    ok "RTSP URL is reachable"
  else
    fail "RTSP URL is not reachable; see /tmp/r3_1a_ffprobe.log"
    echo "PASS=$PASS FAIL=$FAIL"
    exit 1
  fi
else
  note "ffprobe not found; skipping RTSP preflight"
fi

if [ "$R3_1A_REBUILD_SERVICES" = "1" ]; then
  note "rebuilding api/event-worker so smoke uses current R3.1A code"
  if docker compose -f "$COMPOSE_FILE" build api event-worker; then
    ok "api/event-worker rebuilt"
  else
    fail "failed to rebuild api/event-worker"
    echo "PASS=$PASS FAIL=$FAIL"
    exit 1
  fi
fi

note "starting c1-official stack"
if docker compose -f "$COMPOSE_FILE" up -d \
  redis postgres api savant-security event-worker face-worker metadata-sink video-file-sink; then
  ok "c1-official stack started"
else
  fail "failed to start c1-official stack"
  echo "PASS=$PASS FAIL=$FAIL"
  exit 1
fi

note "applying R3/R3.1A migrations"
if command -v psql >/dev/null 2>&1; then
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/008_r3_unified_event_evidence_algorithm_rules.sql >/tmp/r3_1a_migration.log 2>&1
  MIGRATION_STATUS=$?
  if [ "$MIGRATION_STATUS" -eq 0 ]; then
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/009_r3_1a_evidence_lifecycle.sql >>/tmp/r3_1a_migration.log 2>&1
    MIGRATION_STATUS=$?
  fi
else
  docker exec -i "$PG_CONTAINER" psql -U video -d video_analytics -v ON_ERROR_STOP=1 \
    < db/migrations/008_r3_unified_event_evidence_algorithm_rules.sql >/tmp/r3_1a_migration.log 2>&1
  MIGRATION_STATUS=$?
  if [ "$MIGRATION_STATUS" -eq 0 ]; then
    docker exec -i "$PG_CONTAINER" psql -U video -d video_analytics -v ON_ERROR_STOP=1 \
      < db/migrations/009_r3_1a_evidence_lifecycle.sql >>/tmp/r3_1a_migration.log 2>&1
    MIGRATION_STATUS=$?
  fi
fi
if [ "$MIGRATION_STATUS" -eq 0 ]; then
  ok "R3/R3.1A migrations applied"
else
  fail "R3/R3.1A migration failed; see /tmp/r3_1a_migration.log"
  echo "PASS=$PASS FAIL=$FAIL"
  exit 1
fi

note "running single RTSP inference smoke for source_id=$R3_1A_SOURCE_ID"
if R2_5_RTSP_URL="$R3_1A_RTSP_URL" \
   R2_5_CAMERA_ID="$R3_1A_CAMERA_ID" \
   R2_5_SOURCE_ID="$R3_1A_SOURCE_ID" \
   R2_5_CAMERA_NAME="$R3_1A_CAMERA_NAME" \
   R2_5_WAIT_SECONDS="$R3_1A_WAIT_SECONDS" \
   R2_5_MIN_FACE_OBSERVATIONS=0 \
   R2_5_MIN_EVENTS=1 \
   bash scripts/smoke/check_r2_5_single_rtsp_camera_inference.sh; then
  ok "RTSP smoke produced at least one intrusion event"
else
  fail "RTSP smoke failed to produce required intrusion event"
  echo "PASS=$PASS FAIL=$FAIL"
  exit 1
fi

q_source_id="$(printf "%s" "$R3_1A_SOURCE_ID" | sed "s/'/''/g")"
EVENT_ID="$(psql_scalar "
SELECT id
FROM events
WHERE source_id = '$q_source_id'
  AND event_type = 'intrusion'
ORDER BY created_at DESC
LIMIT 1;
")"

if [ -n "$EVENT_ID" ]; then
  ok "intrusion event found event_id=$EVENT_ID"
else
  fail "no intrusion event found for source_id=$R3_1A_SOURCE_ID"
  echo "PASS=$PASS FAIL=$FAIL"
  exit 1
fi

TASK_ROW="$(psql_scalar "
SELECT task_id || '|' || status || '|' || COALESCE(error_message, '')
FROM evidence_tasks
WHERE event_id = '$EVENT_ID'::uuid
ORDER BY created_at DESC
LIMIT 1;
")"

if [ -n "$TASK_ROW" ]; then
  ok "evidence_task found: $TASK_ROW"
else
  fail "no evidence_task found for event_id=$EVENT_ID"
fi

MEDIA_STATUS="$(psql_scalar "SELECT media_status FROM events WHERE id = '$EVENT_ID'::uuid;")"
case "$MEDIA_STATUS" in
  pending|processing|ready|failed|not_implemented)
    ok "events.media_status=$MEDIA_STATUS"
    ;;
  *)
    fail "unexpected events.media_status=$MEDIA_STATUS"
    ;;
esac

note "event evidence DB rows"
psql_table "
SELECT e.id, e.source_event_id, e.event_type, e.source_id, e.media_status,
       e.snapshot_path, e.clip_path,
       et.task_id, et.status AS task_status, et.metadata_path, et.error_message
FROM events e
LEFT JOIN evidence_tasks et ON et.event_id = e.id
WHERE e.id = '$EVENT_ID'::uuid
ORDER BY et.created_at DESC
LIMIT 5;
"

note "calling API evidence endpoint"
if python3 - "$API_BASE" "$EVENT_ID" <<'PY'
import json
import sys
import urllib.request

api_base, event_id = sys.argv[1:3]
url = api_base.rstrip("/") + f"/api/v1/events/{event_id}/evidence"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(url, timeout=30) as resp:
    body = json.loads(resp.read().decode("utf-8"))
data = body.get("data") or {}
if body.get("error"):
    raise SystemExit(f"api error: {body['error']}")
required = ["event_id", "source_event_id", "event_type", "media_status", "snapshot_path", "clip_path", "metadata_path"]
missing = [key for key in required if key not in data]
if missing:
    raise SystemExit(f"missing fields: {missing}")
if data["media_status"] not in {"pending", "processing", "ready", "failed", "not_implemented"}:
    raise SystemExit(f"unexpected media_status={data['media_status']}")
print(json.dumps({
    "event_id": data["event_id"],
    "media_status": data["media_status"],
    "snapshot_path": data.get("snapshot_path"),
    "clip_path": data.get("clip_path"),
    "metadata_path": data.get("metadata_path"),
    "error_message": data.get("error_message"),
}, indent=2))
PY
then
  ok "API evidence endpoint returned stable evidence status"
else
  fail "API evidence endpoint check failed"
fi

if [ "$FAIL" -eq 0 ]; then
  echo "PASS R3.1A behavior event evidence smoke source_id=$R3_1A_SOURCE_ID event_id=$EVENT_ID media_status=$MEDIA_STATUS"
  exit 0
fi

echo "FAIL R3.1A behavior event evidence smoke PASS=$PASS FAIL=$FAIL"
exit 1
