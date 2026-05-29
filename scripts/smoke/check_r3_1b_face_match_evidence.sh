#!/usr/bin/env bash
set -euo pipefail

RTSP_URL="${R3_1B_RTSP_URL:-rtsp://10.37.57.112:8554/live/1080movie}"
SOURCE_ID="${R3_1B_SOURCE_ID:-r3_1b_rtsp_movie_$(date +%s)}"
CAMERA_ID="${R3_1B_CAMERA_ID:-cam_r3_1b_rtsp_movie}"
CAMERA_NAME="${R3_1B_CAMERA_NAME:-R3.1B Face Match RTSP Camera}"
WAIT_SECONDS="${R3_1B_WAIT_SECONDS:-180}"
MATCH_THRESHOLD="${R3_1B_MATCH_THRESHOLD:-0.35}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
REDIS_URL="${REDIS_URL:-redis://localhost:6385/0}"
API_BASE="${API_BASE:-http://localhost:8004}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"

echo "=== R3.1B Face Match Evidence Smoke ==="
echo "rtsp_url=${RTSP_URL}"
echo "source_id=${SOURCE_ID}"
echo "threshold=${MATCH_THRESHOLD}"
echo "note=threshold may be smoke calibration threshold; production default FACE_MATCH_THRESHOLD=0.50"
echo "policy=do not generate raw_clip or annotated_clip in R3.1B"

psql_cmd() {
  local sql="$1"
  if command -v psql >/dev/null 2>&1; then
    psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 -P pager=off -c "${sql}"
  else
    docker exec "${PG_CONTAINER}" psql -U video -d video_analytics \
      -v ON_ERROR_STOP=1 -P pager=off -c "${sql}"
  fi
}

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
  ffprobe -rtsp_transport tcp "${RTSP_URL}" >/dev/null
else
  echo "warning=ffprobe_not_found"
fi

echo "Checking Finch/Reese active gallery..."
psql_cmd "
SELECT p.external_person_id, p.name, pge.id AS gallery_embedding_id,
       pge.embedding_model, pge.embedding_dim, pge.is_active
FROM persons p
JOIN person_gallery_embeddings pge ON pge.person_id = p.id
WHERE p.external_person_id IN ('demo:f4_3:finch', 'demo:f4_3:reese')
  AND pge.is_active = true
ORDER BY p.external_person_id, pge.id DESC;
"

echo "Running R2.5 RTSP smoke to produce face_observations..."
R2_5_RTSP_URL="${RTSP_URL}" \
R2_5_CAMERA_ID="${CAMERA_ID}" \
R2_5_SOURCE_ID="${SOURCE_ID}" \
R2_5_CAMERA_NAME="${CAMERA_NAME}" \
R2_5_WAIT_SECONDS="${WAIT_SECONDS}" \
R2_5_MIN_FACE_OBSERVATIONS=1 \
R2_5_MIN_EVENTS=0 \
bash scripts/smoke/check_r2_5_single_rtsp_camera_inference.sh

echo "Emitting watchlist_hit events from stored face_observations..."
EMIT_JSON="$(DATABASE_URL="${DATABASE_URL}" REDIS_URL="${REDIS_URL}" \
  python3 services/face-worker/emit_face_match_events.py \
    --source-id "${SOURCE_ID}" \
    --external-person-id demo:f4_3:finch \
    --external-person-id demo:f4_3:reese \
    --threshold "${MATCH_THRESHOLD}" \
    --top-k 10 \
    --observation-limit 200 \
    --output-json)"
echo "${EMIT_JSON}" | python3 -m json.tool

EVENTS_EMITTED="$(printf '%s' "${EMIT_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["events_emitted"])')"
if [[ "${EVENTS_EMITTED}" == "0" ]]; then
  echo "NO_HIT_ABOVE_THRESHOLD"
  echo "No fabricated watchlist_hit was created."
  echo "Suggestion: extend R3_1B_WAIT_SECONDS or lower R3_1B_MATCH_THRESHOLD for calibration only."
  exit 0
fi

echo "Waiting for event-worker to consume security.events..."
sleep 8

SOURCE_EVENT_ID="$(printf '%s' "${EMIT_JSON}" | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data["emitted_events"][0]["source_event_id"])')"
EVENT_ROW="$(psql_tsv "
SELECT id, source_event_id, event_type, media_status
FROM events
WHERE source_event_id = '${SOURCE_EVENT_ID}'
LIMIT 1;
")"

if [[ -z "${EVENT_ROW}" ]]; then
  echo "FAIL: watchlist_hit event not found in events for source_event_id=${SOURCE_EVENT_ID}"
  exit 1
fi

EVENT_ID="$(printf '%s' "${EVENT_ROW}" | cut -f1)"
echo "event_row=${EVENT_ROW}"

TASK_ROW="$(psql_tsv "
SELECT task_id, status
FROM evidence_tasks
WHERE event_id = '${EVENT_ID}'::uuid
ORDER BY created_at DESC
LIMIT 1;
")"
if [[ -z "${TASK_ROW}" ]]; then
  echo "FAIL: evidence_task not found for event_id=${EVENT_ID}"
  exit 1
fi
echo "evidence_task=${TASK_ROW}"

echo "Checking evidence API..."
curl --noproxy '*' -fsS "${API_BASE}/api/v1/events/${EVENT_ID}/evidence" \
  | python3 -m json.tool

echo "PASS: R3.1B watchlist_hit evidence MVP verified"
echo "raw_clip_generated=NO"
echo "annotated_clip_generated=NO"
