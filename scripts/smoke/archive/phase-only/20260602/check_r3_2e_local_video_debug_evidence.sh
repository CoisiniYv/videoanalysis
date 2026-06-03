#!/usr/bin/env bash
set -euo pipefail

SOURCE_MP4="${R3_2E_SOURCE_MP4:-/home/user/video-analytics/testVideo/1080movie.mp4}"
TARGET_EXTERNAL_IDS="${R3_2E_TARGET_EXTERNAL_IDS:-demo:f4_3:finch,demo:f4_3:reese}"
MATCH_THRESHOLD="${R3_2E_MATCH_THRESHOLD:-0.35}"
SOURCE_ID="${R3_2E_SOURCE_ID:-r3_2e_local_video_$(date +%s)}"
CAMERA_ID="${R3_2E_CAMERA_ID:-cam_r3_2e_local_video}"
CAMERA_NAME="${R3_2E_CAMERA_NAME:-R3.2E Local Video Debug Camera}"
WAIT_SECONDS="${R3_2E_WAIT_SECONDS:-180}"
MIN_FACE_OBSERVATIONS="${R3_2E_MIN_FACE_OBSERVATIONS:-5}"
OUTPUT_ROOT="${R3_2E_OUTPUT_ROOT:-/data/video-analytics/media/debug/local_video_evidence}"
COMPOSE_FILE="${COMPOSE_FILE:-infra/docker-compose.c1-official-adapter.yml}"
API_BASE="${API_BASE:-http://localhost:8004}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"
NETWORK="${NETWORK:-c1-official-adapter_default}"
CONTROLLER="${CONTROLLER:-scripts/runtime/camera_source_controller.py}"
MODULE_CONFIG="${MODULE_CONFIG:-modules/savant_security/config/cameras.generated.yml}"
SOURCES_CONFIG="${SOURCES_CONFIG:-infra/generated/sources.generated.yml}"

echo "=== R3.2E Local Video Debug Evidence Smoke ==="
echo "source_mp4=${SOURCE_MP4}"
echo "source_id=${SOURCE_ID}"
echo "target_external_ids=${TARGET_EXTERNAL_IDS}"
echo "threshold=${MATCH_THRESHOLD}"
echo "min_face_observations=${MIN_FACE_OBSERVATIONS}"
echo "mode=local_video_debug"
echo "policy=not_production_rtsp_evidence"
echo "RTSP production evidence path is not used"

if [[ ! -f "${SOURCE_MP4}" ]]; then
  echo "SKIP: local source MP4 not found: ${SOURCE_MP4}"
  exit 0
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

cleanup() {
  python3 "${CONTROLLER}" stop --source-id "${SOURCE_ID}" >/tmp/r3_2e_stop.log 2>&1 || true
}
trap cleanup EXIT

echo "Starting c1-official stack..."
docker compose -f "${COMPOSE_FILE}" up -d \
  redis postgres api savant-security event-worker face-worker metadata-sink video-file-sink \
  >/tmp/r3_2e_compose_up.log 2>&1

for _ in $(seq 1 30); do
  docker exec "${PG_CONTAINER}" pg_isready -U video -d video_analytics >/dev/null 2>&1 && break
  sleep 1
done
docker exec "${PG_CONTAINER}" pg_isready -U video -d video_analytics >/dev/null 2>&1

for _ in $(seq 1 30); do
  curl --noproxy '*' -fsS "${API_BASE}/health" >/dev/null 2>&1 && break
  sleep 1
done
curl --noproxy '*' -fsS "${API_BASE}/health" >/dev/null 2>&1

echo "Checking active Finch/Reese gallery..."
psql_tsv "
SELECT p.external_person_id, p.name, pge.id
FROM persons p
JOIN person_gallery_embeddings pge ON pge.person_id = p.id
WHERE p.external_person_id IN ('demo:f4_3:finch', 'demo:f4_3:reese')
  AND pge.is_active = true
ORDER BY p.external_person_id, pge.id DESC;
" || true

echo "Creating local video camera config..."
psql_tsv "
DELETE FROM cameras
WHERE id = '${CAMERA_ID}' OR source_id = '${SOURCE_ID}';
" >/tmp/r3_2e_delete_camera.log

CAMERA_PAYLOAD="$(python3 - "$CAMERA_ID" "$SOURCE_ID" "$CAMERA_NAME" "file:///testVideo/$(basename "${SOURCE_MP4}")" <<'PY'
import json
import sys

camera_id, source_id, name, uri = sys.argv[1:5]
print(json.dumps({
    "id": camera_id,
    "source_id": source_id,
    "name": name,
    "rtsp_url": uri,
    "enabled": True,
}))
PY
)"
curl --noproxy '*' -fsS \
  -H 'Content-Type: application/json' \
  -d "${CAMERA_PAYLOAD}" \
  "${API_BASE}/api/v1/cameras" >/tmp/r3_2e_add_camera.log

mkdir -p "$(dirname "${MODULE_CONFIG}")" "$(dirname "${SOURCES_CONFIG}")"
curl --noproxy '*' -fsS \
  "${API_BASE}/api/v1/cameras/config/export" \
  >"${MODULE_CONFIG}"
python3 - "${MODULE_CONFIG}" "${SOURCES_CONFIG}" <<'PY'
import sys
import yaml

module_config, sources_config = sys.argv[1:3]
doc = yaml.safe_load(open(module_config, encoding="utf-8")) or {}
sources = {}
for camera_id, camera in (doc.get("cameras") or {}).items():
    sources[camera_id] = {
        "camera_id": camera_id,
        "source_id": str(camera.get("source_id", "")),
        "uri": str(camera.get("rtsp_url", "")),
        "enabled": bool(camera.get("enabled", True)),
        "adapter_type": "gstreamer",
        "zmq_endpoint": "dealer+connect:tcp://savant-security:5555",
    }
with open(sources_config, "w", encoding="utf-8") as f:
    yaml.safe_dump({"sources": sources}, f, sort_keys=False, allow_unicode=True)
PY

echo "Restarting savant-security for local video config..."
docker compose -f "${COMPOSE_FILE}" restart savant-security >/tmp/r3_2e_savant_restart.log 2>&1
sleep 8

echo "Starting local video source adapter..."
python3 "${CONTROLLER}" stop --source-id "${SOURCE_ID}" >/tmp/r3_2e_pre_stop.log 2>&1 || true
python3 "${CONTROLLER}" start \
  --sources "${SOURCES_CONFIG}" \
  --source-id "${SOURCE_ID}" \
  --network "${NETWORK}" \
  --testvideo-mount "$(dirname "${SOURCE_MP4}"):/testVideo:ro" \
  >/tmp/r3_2e_start_source.log 2>&1

echo "Waiting for face_observations..."
OBS_COUNT=0
HIT_COUNT=0
for _ in $(seq 1 "${WAIT_SECONDS}"); do
  OBS_COUNT="$(psql_tsv "SELECT COUNT(*) FROM face_observations WHERE source_id = '${SOURCE_ID}';" | tr -d '[:space:]')"
  HIT_COUNT="$(psql_tsv "
SELECT COUNT(*)
FROM (
  SELECT DISTINCT ON (fo.source_observation_id)
    fo.source_observation_id,
    1 - (fo.embedding <=> pge.embedding) AS similarity
  FROM face_observations fo
  JOIN person_gallery_embeddings pge ON pge.is_active = true
  JOIN persons p ON p.id = pge.person_id
  WHERE fo.source_id = '${SOURCE_ID}'
    AND p.external_person_id IN ('demo:f4_3:finch', 'demo:f4_3:reese')
  ORDER BY fo.source_observation_id, similarity DESC
) ranked
WHERE similarity >= ${MATCH_THRESHOLD};
" | tr -d '[:space:]')"
  if [[ "${HIT_COUNT:-0}" -gt 0 && "${OBS_COUNT:-0}" -ge "${MIN_FACE_OBSERVATIONS}" ]]; then
    break
  fi
  if (( _ % 10 == 0 )); then
    echo "wait_progress seconds=${_} observations=${OBS_COUNT:-0} hits_above_threshold=${HIT_COUNT:-0}"
  fi
  sleep 1
done
echo "observations_count=${OBS_COUNT:-0}"
echo "hits_above_threshold=${HIT_COUNT:-0}"
if [[ "${OBS_COUNT:-0}" -le 0 ]]; then
  echo "FAIL: no face_observations for local video source_id=${SOURCE_ID}"
  exit 1
fi

echo "Exporting local-video debug evidence..."
EXPORT_JSON="/tmp/r3_2e_export.json"
set +e
DATABASE_URL="${DATABASE_URL}" python3 services/clip-worker/export_local_video_debug_evidence.py \
  --source-mp4 "${SOURCE_MP4}" \
  --source-id "${SOURCE_ID}" \
  --external-person-ids "${TARGET_EXTERNAL_IDS}" \
  --threshold "${MATCH_THRESHOLD}" \
  --output-root "${OUTPUT_ROOT}" \
  --limit 50 \
  --pre-ms 5000 \
  --post-ms 5000 \
  --window-ms 600 \
  --output-json >"${EXPORT_JSON}" 2>/tmp/r3_2e_export.err
EXPORT_RC=$?
set -e
cat "${EXPORT_JSON}" | python3 -m json.tool
cat /tmp/r3_2e_export.err >&2 || true

HIT_COUNT="$(python3 - "${EXPORT_JSON}" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("hit_count", 0))
PY
)"
SNAPSHOT_PATH="$(python3 - "${EXPORT_JSON}" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("debug_overlay_snapshot_path") or "")
PY
)"
CLIP_PATH="$(python3 - "${EXPORT_JSON}" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("debug_annotated_clip_path") or "")
PY
)"
METADATA_PATH="$(python3 - "${EXPORT_JSON}" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("debug_visual_metadata_path") or "")
PY
)"
NOT_PROD="$(python3 - "${EXPORT_JSON}" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("not_production_rtsp_evidence"))
PY
)"

if [[ "${HIT_COUNT}" == "0" ]]; then
  echo "NO_HIT_ABOVE_THRESHOLD"
  echo "No fabricated local-video debug evidence was created."
  echo "Suggestion: extend R3_2E_WAIT_SECONDS or lower R3_2E_MATCH_THRESHOLD for calibration only."
  exit 0
fi

[[ "${EXPORT_RC}" == "0" ]]
[[ -f "${SNAPSHOT_PATH}" ]]
[[ -f "${CLIP_PATH}" ]]
[[ -f "${METADATA_PATH}" ]]
[[ "${NOT_PROD}" == "True" || "${NOT_PROD}" == "true" ]]

echo "PASS: R3.2E local video debug evidence smoke"
echo "source_id=${SOURCE_ID}"
echo "source_mp4_path=${SOURCE_MP4}"
echo "observations_count=${OBS_COUNT}"
echo "debug_overlay_snapshot.jpg=${SNAPSHOT_PATH}"
echo "debug_annotated_clip.mp4=${CLIP_PATH}"
echo "debug_visual_metadata.json=${METADATA_PATH}"
echo "canonical_production_evidence_written=NO"
echo "savant_pipeline_changed=NO"
