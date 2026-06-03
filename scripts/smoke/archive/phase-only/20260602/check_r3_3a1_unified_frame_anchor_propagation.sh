#!/usr/bin/env bash
set -euo pipefail

RTSP_URL="${R3_3A1_RTSP_URL:-rtsp://10.37.57.112:8554/live/1080movie}"
SOURCE_ID="${R3_3A1_SOURCE_ID:-r3_3a1_rtsp_anchor_$(date +%s)}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"

echo "=== R3.3A1 Unified Frame Anchor Propagation Smoke ==="
echo "rtsp_url=${RTSP_URL}"
echo "source_id=${SOURCE_ID}"
echo "scope=metadata propagation only"
echo "no_replay_clip=YES"
echo "no_exact_snapshot=YES"
echo "no_video_file_sink_change=YES"
echo "no_performance_test=YES"

if command -v ffprobe >/dev/null 2>&1; then
  if ! timeout 20 ffprobe -rtsp_transport tcp "${RTSP_URL}" >/tmp/r3_3a1_ffprobe.log 2>&1; then
    echo "SKIP: RTSP unavailable; ffprobe preflight failed. See /tmp/r3_3a1_ffprobe.log"
    exit 0
  fi
else
  echo "warning=ffprobe_not_found; continuing without RTSP preflight"
fi

psql_tsv() {
  local sql="$1"
  if command -v psql >/dev/null 2>&1; then
    psql "${DATABASE_URL}" -t -A -F $'\t' -v ON_ERROR_STOP=1 -c "${sql}"
  else
    docker exec "${PG_CONTAINER}" psql -U video -d video_analytics \
      -t -A -F $'\t' -v ON_ERROR_STOP=1 -c "${sql}"
  fi
}

R2_5_RTSP_URL="${RTSP_URL}" \
R2_5_SOURCE_ID="${SOURCE_ID}" \
R2_5_WAIT_SECONDS="${R3_3A1_WAIT_SECONDS:-90}" \
bash scripts/smoke/check_r2_5_single_rtsp_camera_inference.sh

EVENT_ROW="$(psql_tsv "
SELECT id, frame_uuid, keyframe_uuid, payload->'media'->>'frame_uuid'
FROM events
WHERE source_id = '${SOURCE_ID}'
ORDER BY created_at DESC
LIMIT 1;
")"
echo "event_anchor_row=${EVENT_ROW}"

EVENT_ID="$(printf '%s' "${EVENT_ROW}" | cut -f1)"
EVENT_FRAME_UUID="$(printf '%s' "${EVENT_ROW}" | cut -f2)"
EVENT_MEDIA_FRAME_UUID="$(printf '%s' "${EVENT_ROW}" | cut -f4)"

if [[ -z "${EVENT_ID}" ]]; then
  echo "FAIL: no behavior event found for source_id=${SOURCE_ID}"
  exit 1
fi
if [[ -z "${EVENT_FRAME_UUID}" ]]; then
  echo "FAIL: events.frame_uuid is empty for event_id=${EVENT_ID}"
  exit 1
fi
if [[ "${EVENT_FRAME_UUID}" != "${EVENT_MEDIA_FRAME_UUID}" ]]; then
  echo "FAIL: event top-level frame_uuid does not match payload.media.frame_uuid"
  exit 1
fi

FACE_ROW="$(psql_tsv "
SELECT source_observation_id, payload->'media'->>'frame_uuid'
FROM face_observations
WHERE source_id = '${SOURCE_ID}'
ORDER BY created_at DESC
LIMIT 1;
")"
echo "face_anchor_row=${FACE_ROW}"

FACE_ID="$(printf '%s' "${FACE_ROW}" | cut -f1)"
FACE_FRAME_UUID="$(printf '%s' "${FACE_ROW}" | cut -f2)"
if [[ -n "${FACE_ID}" ]]; then
  if [[ -z "${FACE_FRAME_UUID}" ]]; then
    echo "FAIL: face_observations.payload.media.frame_uuid is empty for ${FACE_ID}"
    exit 1
  fi
  echo "face_observation_frame_uuid=${FACE_FRAME_UUID}"
else
  echo "face_observation_anchor=SKIPPED no face observation produced in this run"
fi

echo "PASS: R3.3A1 unified frame anchor propagation smoke"
