#!/usr/bin/env bash
set -euo pipefail

RTSP_URL="${R3_2B_RTSP_URL:-rtsp://10.37.57.112:8554/live/1080movie}"
EVENT_TYPE="${R3_2B_EVENT_TYPE:-intrusion}"
RAW_MP4="${R3_2B_RAW_MP4:-}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
API_BASE="${API_BASE:-http://localhost:8004}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"

echo "=== R3.2B Raw Clip Evidence Smoke ==="
echo "rtsp_url=${RTSP_URL}"
echo "event_type=${EVENT_TYPE}"
echo "mode=$([[ -n "${RAW_MP4}" ]] && echo raw_mp4_fallback || echo no_reliable_clip_source)"
echo "ffprobe_usage=preflight only"
echo "live_rtsp_current_clip=disabled"

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
  if ! timeout 20 ffprobe -rtsp_transport tcp "${RTSP_URL}" >/tmp/r3_2b_ffprobe.log 2>&1; then
    echo "SKIP: RTSP unavailable; ffprobe preflight failed. See /tmp/r3_2b_ffprobe.log"
    exit 0
  fi
else
  echo "warning=ffprobe_not_found; skipping smoke preflight only"
fi

EVENT_ID="${R3_2B_EVENT_ID:-}"
if [[ -z "${EVENT_ID}" ]]; then
  if [[ "${EVENT_TYPE}" != "intrusion" ]]; then
    echo "FAIL: smoke currently generates intrusion only unless R3_2B_EVENT_ID is provided"
    exit 1
  fi

  SOURCE_ID="r3_2b_intrusion_$(date +%s)"
  R3_1A_RTSP_URL="${RTSP_URL}" \
  R3_1A_SOURCE_ID="${SOURCE_ID}" \
  R3_1A_WAIT_SECONDS="${R3_2B_WAIT_SECONDS:-180}" \
  bash scripts/smoke/check_r3_1a_behavior_event_evidence.sh

  EVENT_ID="$(psql_tsv "
SELECT id
FROM events
WHERE source_id = '${SOURCE_ID}'
  AND event_type = 'intrusion'
ORDER BY created_at DESC
LIMIT 1;
")"
fi

if [[ -z "${EVENT_ID}" ]]; then
  echo "FAIL: no event available for R3.2B evidence processing"
  exit 1
fi

echo "Processing evidence event_id=${EVENT_ID}"
PROCESS_ARGS=(
  services/clip-worker/process_evidence_task.py
  --event-id "${EVENT_ID}"
  --rtsp-url "${RTSP_URL}"
  --capture-backend opencv
  --media-root /data/video-analytics/media
)
if [[ -n "${RAW_MP4}" ]]; then
  PROCESS_ARGS+=(--raw-mp4 "${RAW_MP4}")
fi

DATABASE_URL="${DATABASE_URL}" python3 "${PROCESS_ARGS[@]}" | python3 -m json.tool

META_ROW="$(psql_tsv "
SELECT e.media_status,
       e.payload->'media'->>'snapshot_status',
       e.payload->'media'->>'metadata_status',
       e.payload->'media'->>'clip_status',
       e.snapshot_path,
       e.payload->'media'->>'metadata_path',
       e.clip_path,
       e.payload->'media'->>'raw_clip_path',
       e.payload->'media'->>'clip_error_message'
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
CLIP_PATH="$(printf '%s' "${META_ROW}" | cut -f7)"
RAW_CLIP_PATH="$(printf '%s' "${META_ROW}" | cut -f8)"
CLIP_ERROR_MESSAGE="$(printf '%s' "${META_ROW}" | cut -f9)"

[[ "${METADATA_STATUS}" == "ready" ]]
[[ -f "${METADATA_PATH}" ]]
if [[ "${SNAPSHOT_STATUS}" == "ready" ]]; then
  [[ -f "${SNAPSHOT_PATH}" ]]
else
  echo "snapshot_not_ready_status=${SNAPSHOT_STATUS}"
fi

OUTPUT_DIR="$(dirname "${METADATA_PATH}")"
[[ ! -f "${OUTPUT_DIR}/annotated_clip.mp4" ]]

if [[ -n "${RAW_MP4}" ]]; then
  [[ "${MEDIA_STATUS}" == "ready" ]]
  [[ "${CLIP_STATUS}" == "ready" ]]
  [[ "${CLIP_PATH}" == "${OUTPUT_DIR}/raw_clip.mp4" ]]
  [[ "${RAW_CLIP_PATH}" == "${OUTPUT_DIR}/raw_clip.mp4" ]]
  [[ -f "${RAW_CLIP_PATH}" ]]
  echo "raw_clip_path=${RAW_CLIP_PATH}"
else
  [[ "${MEDIA_STATUS}" == "partial" ]]
  [[ "${CLIP_STATUS}" == "not_implemented" ]]
  [[ -z "${CLIP_PATH}" ]]
  [[ -z "${RAW_CLIP_PATH}" ]]
  [[ ! -f "${OUTPUT_DIR}/raw_clip.mp4" ]]
  echo "clip_error_message=${CLIP_ERROR_MESSAGE}"
fi

curl --noproxy '*' -fsS "${API_BASE}/api/v1/events/${EVENT_ID}/evidence" \
  | python3 -m json.tool

echo "PASS: R3.2B raw clip evidence smoke"
echo "event_id=${EVENT_ID}"
echo "media_status=${MEDIA_STATUS}"
echo "clip_status=${CLIP_STATUS}"
echo "raw_clip.mp4 generated=$([[ -n "${RAW_MP4}" ]] && echo YES || echo NO)"
echo "annotated_clip.mp4 generated=NO"
