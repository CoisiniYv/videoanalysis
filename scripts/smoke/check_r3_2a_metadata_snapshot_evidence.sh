#!/usr/bin/env bash
set -euo pipefail

RTSP_URL="${R3_2A_RTSP_URL:-rtsp://10.37.57.112:8554/live/1080movie}"
EVENT_TYPE="${R3_2A_EVENT_TYPE:-both}"
MATCH_THRESHOLD="${R3_2A_MATCH_THRESHOLD:-0.35}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
API_BASE="${API_BASE:-http://localhost:8004}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"

echo "=== R3.2A Metadata + Snapshot Evidence Smoke ==="
echo "rtsp_url=${RTSP_URL}"
echo "event_type=${EVENT_TYPE}"
echo "snapshot_backend=opencv"
echo "ffprobe_usage=preflight only"

psql_tsv() {
  local sql="$1"
  if command -v psql >/dev/null 2>&1; then
    psql "${DATABASE_URL}" -t -A -F $'\t' -v ON_ERROR_STOP=1 -c "${sql}"
  else
    docker exec "${PG_CONTAINER}" psql -U video -d video_analytics \
      -t -A -F $'\t' -v ON_ERROR_STOP=1 -c "${sql}"
  fi
}

if command -v ffprobe >/dev/null 2>&1; then
  if ! timeout 20 ffprobe -rtsp_transport tcp "${RTSP_URL}" >/tmp/r3_2a_ffprobe.log 2>&1; then
    echo "SKIP: RTSP unavailable; ffprobe preflight failed. See /tmp/r3_2a_ffprobe.log"
    exit 0
  fi
else
  echo "warning=ffprobe_not_found; skipping smoke preflight only"
fi

EVENT_IDS=()

if [[ "${EVENT_TYPE}" == "intrusion" || "${EVENT_TYPE}" == "both" ]]; then
  echo "Generating intrusion event via R3.1A smoke..."
  INTRUSION_SOURCE_ID="r3_2a_intrusion_$(date +%s)"
  R3_1A_RTSP_URL="${RTSP_URL}" \
  R3_1A_SOURCE_ID="${INTRUSION_SOURCE_ID}" \
  R3_1A_WAIT_SECONDS="${R3_2A_WAIT_SECONDS:-180}" \
  bash scripts/smoke/check_r3_1a_behavior_event_evidence.sh

  INTRUSION_ID="$(psql_tsv "
SELECT id
FROM events
WHERE source_id = '${INTRUSION_SOURCE_ID}'
  AND event_type = 'intrusion'
ORDER BY created_at DESC
LIMIT 1;
")"
  if [[ -n "${INTRUSION_ID}" ]]; then
    EVENT_IDS+=("${INTRUSION_ID}")
  fi
fi

if [[ "${EVENT_TYPE}" == "watchlist_hit" || "${EVENT_TYPE}" == "both" ]]; then
  echo "Generating watchlist_hit event via R3.1B smoke..."
  WATCHLIST_SOURCE_ID="r3_2a_watchlist_$(date +%s)"
  R3_1B_RTSP_URL="${RTSP_URL}" \
  R3_1B_SOURCE_ID="${WATCHLIST_SOURCE_ID}" \
  R3_1B_MATCH_THRESHOLD="${MATCH_THRESHOLD}" \
  bash scripts/smoke/check_r3_1b_face_match_evidence.sh || true

  WATCHLIST_ID="$(psql_tsv "
SELECT id
FROM events
WHERE source_id = '${WATCHLIST_SOURCE_ID}'
  AND event_type = 'watchlist_hit'
ORDER BY created_at DESC
LIMIT 1;
")"
  if [[ -n "${WATCHLIST_ID}" ]]; then
    EVENT_IDS+=("${WATCHLIST_ID}")
  else
    echo "NO_WATCHLIST_HIT_ABOVE_THRESHOLD"
  fi
fi

if [[ "${#EVENT_IDS[@]}" -eq 0 ]]; then
  echo "FAIL: no event available for R3.2A evidence processing"
  exit 1
fi

for EVENT_ID in "${EVENT_IDS[@]}"; do
  echo "Processing evidence event_id=${EVENT_ID}"
  DATABASE_URL="${DATABASE_URL}" \
  python3 services/clip-worker/process_evidence_task.py \
    --event-id "${EVENT_ID}" \
    --rtsp-url "${RTSP_URL}" \
    --capture-backend opencv \
    --media-root /data/video-analytics/media \
    | python3 -m json.tool

  META_ROW="$(psql_tsv "
SELECT e.media_status,
       e.payload->'media'->>'snapshot_status',
       e.payload->'media'->>'metadata_status',
       e.payload->'media'->>'clip_status',
       e.snapshot_path,
       e.payload->'media'->>'metadata_path'
FROM events e
WHERE e.id = '${EVENT_ID}'::uuid;
")"
  echo "media_row=${META_ROW}"

  MEDIA_STATUS="$(printf '%s' "${META_ROW}" | cut -f1)"
  SNAPSHOT_STATUS="$(printf '%s' "${META_ROW}" | cut -f2)"
  METADATA_STATUS="$(printf '%s' "${META_ROW}" | cut -f3)"
  CLIP_STATUS="$(printf '%s' "${META_ROW}" | cut -f4)"
  SNAPSHOT_PATH="$(printf '%s' "${META_ROW}" | cut -f5)"
  METADATA_PATH="$(printf '%s' "${META_ROW}" | cut -f6)"

  [[ "${MEDIA_STATUS}" == "ready" || "${MEDIA_STATUS}" == "partial" ]]
  [[ "${METADATA_STATUS}" == "ready" ]]
  [[ "${CLIP_STATUS}" == "not_implemented" ]]
  [[ -f "${METADATA_PATH}" ]]
  if [[ "${SNAPSHOT_STATUS}" == "ready" ]]; then
    [[ -f "${SNAPSHOT_PATH}" ]]
  else
    echo "snapshot_not_ready_status=${SNAPSHOT_STATUS}"
  fi
  [[ ! -f "$(dirname "${METADATA_PATH}")/raw_clip.mp4" ]]
  [[ ! -f "$(dirname "${METADATA_PATH}")/annotated_clip.mp4" ]]

  curl --noproxy '*' -fsS "${API_BASE}/api/v1/events/${EVENT_ID}/evidence" \
    | python3 -m json.tool
done

echo "PASS: R3.2A metadata + snapshot evidence smoke"
echo "raw_clip.mp4 generated=NO"
echo "annotated_clip.mp4 generated=NO"
