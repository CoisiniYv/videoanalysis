#!/usr/bin/env bash
# Phase E1 — Alert Evidence MVP Smoke Test
#
# Prerequisites:
#   1. Phase 3H compose has been started, intrusion events generated.
#   2. source-adapter, savant-zmq, metadata-sink, video-file-sink stopped.
#   3. evidence-worker has been run (docker compose up evidence-worker).
#   4. postgres and api containers are running.
#
# Requires: curl, python3 (Pillow)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-infra/docker-compose.phase3h-zmq.yml}"
API_BASE="${API_BASE:-http://localhost:8001}"
PG_CONTAINER="${PG_CONTAINER:-phase3h-zmq-postgres}"
export PG_CONTAINER

PASS=0
FAIL=0

ok() { echo "OK  [$((++PASS))] $1"; }
fail() { echo "FAIL [$((++FAIL))] $1"; }

# Convert container path (/media/...) to host path (/data/video-analytics/media/...)
MEDIA_HOST_ROOT="${MEDIA_ROOT:-/data/video-analytics/media}"
container_to_host() {
  local path="$1"
  echo "${MEDIA_HOST_ROOT}${path#/media}"  # replace /media prefix with host root
}

echo "=== Phase E1 Alert Evidence MVP Smoke Test ==="
echo ""

# 1. Compose file includes evidence-worker
if grep -q "evidence-worker:" "$PROJECT_DIR/$COMPOSE_FILE" 2>/dev/null; then
  ok "compose file includes evidence-worker service"
else
  fail "compose file missing evidence-worker service"
fi

# ── Database checks ───────────────────────────────────────────────────

# 2. At least 1 intrusion event exists
INTRUSION_COUNT=$(newgrp docker <<DOCKER_EOF
docker exec "$PG_CONTAINER" psql -U video -d video_analytics -tAc \
  "SELECT COUNT(*) FROM events WHERE event_type = 'intrusion'" 2>/dev/null || echo 0
DOCKER_EOF
)
if [ "$INTRUSION_COUNT" -gt 0 ] 2>/dev/null; then
  ok "at least 1 intrusion event exists (count=$INTRUSION_COUNT)"
else
  fail "no intrusion events found in PostgreSQL"
fi

# Get the most recent intrusion event that has evidence
EVENT_JSON=$(newgrp docker <<DOCKER_EOF
docker exec "$PG_CONTAINER" psql -U video -d video_analytics -tAc \
  "SELECT row_to_json(e) FROM (
     SELECT id, source_event_id, event_type, track_id, source_id,
            snapshot_path, clip_path,
            payload->'media'->>'annotated_snapshot_path' as annotated_snapshot_path,
            payload->'media'->>'evidence_frame_num' as evidence_frame_num,
            payload->'media'->>'snapshot_status' as snapshot_status,
            payload->'media'->>'clip_status' as clip_status,
            payload->'media'->>'annotated_snapshot_status' as annotated_snapshot_status
     FROM events
     WHERE event_type = 'intrusion'
       AND snapshot_path IS NOT NULL
     ORDER BY created_at DESC
     LIMIT 1
   ) e" 2>/dev/null || echo "{}"
DOCKER_EOF
)

EVENT_ID=$(echo "$EVENT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")
TRACK_ID=$(echo "$EVENT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('track_id',''))" 2>/dev/null || echo "")
FRAME_NUM=$(echo "$EVENT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('evidence_frame_num',''))" 2>/dev/null || echo "")
SNAPSHOT_PATH_DB=$(echo "$EVENT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('snapshot_path',''))" 2>/dev/null || echo "")
CLIP_PATH_DB=$(echo "$EVENT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('clip_path',''))" 2>/dev/null || echo "")
ANNOTATED_PATH_DB=$(echo "$EVENT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('annotated_snapshot_path',''))" 2>/dev/null || echo "")

# Convert to host paths (container /media → host /data/video-analytics/media)
SNAPSHOT_HOST="$(container_to_host "$SNAPSHOT_PATH_DB")"
CLIP_HOST="$(container_to_host "$CLIP_PATH_DB")"
ANNOTATED_HOST="$(container_to_host "$ANNOTATED_PATH_DB")"

# 3. Event has non-null snapshot_path
if [ -n "$SNAPSHOT_PATH_DB" ]; then
  ok "event has non-null snapshot_path ($SNAPSHOT_PATH_DB)"
else
  fail "event snapshot_path is null — evidence-worker may not have run"
fi

# 4. Snapshot file exists on host disk
if [ -f "$SNAPSHOT_HOST" ]; then
  ok "snapshot file exists on disk ($SNAPSHOT_HOST)"
else
  fail "snapshot file not found: $SNAPSHOT_HOST"
fi

# 5. Annotated snapshot file exists on host disk
if [ -n "$ANNOTATED_HOST" ] && [ -f "$ANNOTATED_HOST" ]; then
  ok "annotated snapshot file exists on disk ($ANNOTATED_HOST)"
else
  fail "annotated snapshot file not found: $ANNOTATED_HOST"
fi

# 6. Clip file exists on host disk
if [ -n "$CLIP_HOST" ] && [ -f "$CLIP_HOST" ]; then
  ok "clip file exists on disk ($CLIP_HOST)"
else
  fail "clip file not found: $CLIP_HOST"
fi

# ── API checks ────────────────────────────────────────────────────────

API_EVENT=$(curl -s "$API_BASE/api/v1/events/$EVENT_ID" 2>/dev/null || echo "{}")
API_DATA=$(echo "$API_EVENT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
data = d.get('data', d)
print(json.dumps({
    'snapshot_url': data.get('snapshot_url', ''),
    'annotated_snapshot_url': data.get('annotated_snapshot_url', ''),
    'clip_url': data.get('clip_url', ''),
}))
" 2>/dev/null || echo '{"snapshot_url":"","annotated_snapshot_url":"","clip_url":""}')

SNAPSHOT_URL=$(echo "$API_DATA" | python3 -c "import sys,json; print(json.load(sys.stdin).get('snapshot_url',''))" 2>/dev/null || echo "")
ANNOTATED_URL=$(echo "$API_DATA" | python3 -c "import sys,json; print(json.load(sys.stdin).get('annotated_snapshot_url',''))" 2>/dev/null || echo "")
CLIP_URL=$(echo "$API_DATA" | python3 -c "import sys,json; print(json.load(sys.stdin).get('clip_url',''))" 2>/dev/null || echo "")

# 7. API returns snapshot_url
if [ -n "$SNAPSHOT_URL" ]; then
  ok "API returns snapshot_url ($SNAPSHOT_URL)"
else
  fail "API missing snapshot_url"
fi

# 8. API returns annotated_snapshot_url
if [ -n "$ANNOTATED_URL" ]; then
  ok "API returns annotated_snapshot_url ($ANNOTATED_URL)"
else
  fail "API missing annotated_snapshot_url"
fi

# 9. API returns clip_url
if [ -n "$CLIP_URL" ]; then
  ok "API returns clip_url ($CLIP_URL)"
else
  fail "API missing clip_url"
fi

# 10,11,12: URL HTTP checks
FULL_SNAPSHOT_URL="${API_BASE}${SNAPSHOT_URL}"
FULL_ANNOTATED_URL="${API_BASE}${ANNOTATED_URL}"
FULL_CLIP_URL="${API_BASE}${CLIP_URL}"

HTTP_SNAPSHOT=$(curl -s -o /dev/null -w "%{http_code}" "$FULL_SNAPSHOT_URL" 2>/dev/null || echo "000")
if [ "$HTTP_SNAPSHOT" = "200" ]; then
  ok "snapshot_url HTTP 200 ($FULL_SNAPSHOT_URL)"
else
  fail "snapshot_url HTTP $HTTP_SNAPSHOT ($FULL_SNAPSHOT_URL)"
fi

HTTP_ANNOTATED=$(curl -s -o /dev/null -w "%{http_code}" "$FULL_ANNOTATED_URL" 2>/dev/null || echo "000")
if [ "$HTTP_ANNOTATED" = "200" ]; then
  ok "annotated_snapshot_url HTTP 200 ($FULL_ANNOTATED_URL)"
else
  fail "annotated_snapshot_url HTTP $HTTP_ANNOTATED ($FULL_ANNOTATED_URL)"
fi

HTTP_CLIP=$(curl -s -o /dev/null -w "%{http_code}" "$FULL_CLIP_URL" 2>/dev/null || echo "000")
if [ "$HTTP_CLIP" = "200" ]; then
  ok "clip_url HTTP 200 ($FULL_CLIP_URL)"
else
  fail "clip_url HTTP $HTTP_CLIP ($FULL_CLIP_URL)"
fi

# 13. Annotated snapshot has at least 1 bbox red pixel
if [ -f "$ANNOTATED_HOST" ]; then
  RED_PIXELS=$(python3 -c "
from PIL import Image
img = Image.open('$ANNOTATED_HOST').convert('RGB')
pixels = img.load()
w, h = img.size
count = 0
for y in range(h):
    for x in range(w):
        r, g, b = pixels[x, y]
        if r > 200 and g < 50 and b < 50:
            count += 1
print(count)
" 2>/dev/null || echo "0")
  if [ "$RED_PIXELS" -gt 0 ] 2>/dev/null; then
    ok "annotated snapshot has bbox red pixels (count=$RED_PIXELS)"
  else
    fail "annotated snapshot has NO red bbox pixels"
  fi
else
  fail "annotated snapshot missing, cannot check bbox pixels"
fi

# ── Annotated clip checks (E1.1b) ────────────────────────────────

ANNOTATED_CLIP_URL=""
ANNOTATED_CLIP_PATH_DB=""
ANNOTATED_CLIP_HOST=""

# Fetch annotated_clip_url from API
ANNOTATED_CLIP_URL=$(echo "$API_EVENT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
data = d.get('data', d)
print(data.get('annotated_clip_url', ''))
" 2>/dev/null || echo "")

# Get annotated_clip_path from DB
export EVENT_ID
ANNOTATED_CLIP_PATH_DB=$(newgrp docker <<DOCKER_EOF
docker exec "$PG_CONTAINER" psql -U video -d video_analytics -tAc \
  "SELECT payload->'media'->>'annotated_clip_path' FROM events WHERE id = '$EVENT_ID'" 2>/dev/null || echo ""
DOCKER_EOF
)

ANNOTATED_CLIP_HOST="$(container_to_host "$ANNOTATED_CLIP_PATH_DB")"

# 14. API returns annotated_clip_url
if [ -n "$ANNOTATED_CLIP_URL" ]; then
  ok "API returns annotated_clip_url ($ANNOTATED_CLIP_URL)"
else
  fail "API missing annotated_clip_url"
fi

# 15. annotated_clip_url HTTP 200
FULL_ANNOTATED_CLIP_URL="${API_BASE}${ANNOTATED_CLIP_URL}"
HTTP_ANNOTATED_CLIP=$(curl -s -o /dev/null -w "%{http_code}" "$FULL_ANNOTATED_CLIP_URL" 2>/dev/null || echo "000")
if [ "$HTTP_ANNOTATED_CLIP" = "200" ]; then
  ok "annotated_clip_url HTTP 200 ($FULL_ANNOTATED_CLIP_URL)"
else
  fail "annotated_clip_url HTTP $HTTP_ANNOTATED_CLIP ($FULL_ANNOTATED_CLIP_URL)"
fi

# 16. annotated clip file exists on disk
if [ -n "$ANNOTATED_CLIP_HOST" ] && [ -f "$ANNOTATED_CLIP_HOST" ]; then
  ok "annotated clip file exists on disk ($ANNOTATED_CLIP_HOST)"
else
  fail "annotated clip file not found: $ANNOTATED_CLIP_HOST"
fi

# 17. annotated clip size > 100KB
if [ -f "$ANNOTATED_CLIP_HOST" ]; then
  CLIP_SIZE=$(stat -c%s "$ANNOTATED_CLIP_HOST" 2>/dev/null || echo "0")
  if [ "$CLIP_SIZE" -gt 102400 ] 2>/dev/null; then
    ok "annotated clip size > 100KB (size=$CLIP_SIZE bytes)"
  else
    fail "annotated clip size <= 100KB (size=$CLIP_SIZE bytes)"
  fi
fi

# 18. annotated clip frames have red bbox pixels
if [ -f "$ANNOTATED_CLIP_HOST" ]; then
  # Extract 3 frames from the clip and check for red pixels
  RED_FRAMES=0
  for seek_s in 0.5 2.0 4.0; do
    TMP_FRAME="/tmp/e1_smoke_annotated_clip_frame_$$.jpg"
    ffmpeg -y -nostdin -loglevel error -ss "$seek_s" -i "$ANNOTATED_CLIP_HOST" -vframes 1 "$TMP_FRAME" 2>/dev/null
    if [ -f "$TMP_FRAME" ]; then
      RED=$(python3 -c "
from PIL import Image
img = Image.open('$TMP_FRAME').convert('RGB')
pixels = img.load()
w, h = img.size
count = 0
for y in range(0, h, 3):
    for x in range(0, w, 3):
        r, g, b = pixels[x, y]
        if r > 200 and g < 50 and b < 50:
            count += 1
print(count)
" 2>/dev/null || echo "0")
      if [ "$RED" -gt 0 ] 2>/dev/null; then
        RED_FRAMES=$((RED_FRAMES + 1))
      fi
      rm -f "$TMP_FRAME"
    fi
  done
  if [ "$RED_FRAMES" -gt 0 ] 2>/dev/null; then
    ok "annotated clip has red bbox pixels in $RED_FRAMES/3 sampled frames"
  else
    fail "annotated clip has NO red bbox pixels in sampled frames"
  fi
fi

# ── Summary output ────────────────────────────────────────────────────

echo ""
echo "=== Phase E1 Evidence Generation Result ==="
echo "  event_id:            $EVENT_ID"
echo "  track_id:            $TRACK_ID"
echo "  selected frame_num:  $FRAME_NUM"
echo "  snapshot_url:        ${API_BASE}${SNAPSHOT_URL}"
echo "  annotated_snapshot_url: ${API_BASE}${ANNOTATED_URL}"
echo "  clip_url:            ${API_BASE}${CLIP_URL}"
echo "  annotated_clip_url:  ${API_BASE}${ANNOTATED_CLIP_URL}"
echo "=========================================="
echo "MANUAL CHECK: Open the URLs above. Verify:"
echo "  - snapshot shows the scene"
echo "  - annotated snapshot has red bbox rectangles on people"
echo "  - clip plays correctly for ~6 seconds"
echo "  - annotated clip has red bbox overlay on every frame"
echo ""

echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
