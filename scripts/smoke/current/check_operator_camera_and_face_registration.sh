#!/usr/bin/env bash
set -euo pipefail

API_BASE_URL="${API_BASE_URL:-http://0.0.0.0:8090}"
DATABASE_URL="${DATABASE_URL:-${VIDEO_ANALYTICS_DATABASE_URL:-postgresql://video:video@0.0.0.0:5432/video_analytics}}"
CAMERA_ID="${OPERATOR_SMOKE_CAMERA_ID:-00000000-0000-4000-8000-000000000901}"
CAMERA_ENABLED="${OPERATOR_SMOKE_CAMERA_ENABLED:-false}"
FACE_IMAGE="${FACE_REGISTRATION_TEST_IMAGE:-${F4_2_TEST_IMAGE:-}}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CAMERA_COMPAT_MIGRATION="${REPO_ROOT}/db/migrations/012_operator_camera_schema_compat.sql"
CAMERA_RULE_ZONE_COMPAT_MIGRATION="${REPO_ROOT}/db/migrations/016_camera_rule_zone_id_text_compat.sql"

case "${CAMERA_ENABLED}" in
  true|false) ;;
  *)
    echo "OPERATOR_SMOKE_CAMERA_ENABLED must be true or false"
    exit 1
    ;;
esac

curl_api() {
  curl --noproxy '*' -fsS "$@"
}

cleanup_smoke_camera() {
  if [[ "${OPERATOR_SMOKE_KEEP_CAMERA:-false}" == "true" ]]; then
    return
  fi
  DATABASE_URL="${DATABASE_URL}" CAMERA_ID="${CAMERA_ID}" python3 - <<'PY' || true
import os

import psycopg

database_url = os.environ["DATABASE_URL"]
camera_id = os.environ["CAMERA_ID"]
with psycopg.connect(database_url, autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM camera_rules WHERE camera_id = %s::uuid", (camera_id,))
        cur.execute("DELETE FROM camera_zones WHERE camera_id = %s::uuid", (camera_id,))
        cur.execute(
            """
            DELETE FROM cameras
            WHERE id = %s::uuid
              AND enabled IS FALSE
              AND (
                name = 'Operator Smoke Camera'
                OR rtsp_url = 'rtsp://example.local/operator-smoke'
              )
            """,
            (camera_id,),
        )
PY
}

trap cleanup_smoke_camera EXIT

echo "[operator-smoke] checking operator portal health at ${API_BASE_URL}"
curl_api "${API_BASE_URL}/health" | grep -q '"status":"ok"'

echo "[operator-smoke] checking operator portal page"
curl_api "${API_BASE_URL}/" | grep -q 'camera-form'

echo "[operator-smoke] ensuring camera operator schema"
DATABASE_URL="${DATABASE_URL}" \
CAMERA_COMPAT_MIGRATION="${CAMERA_COMPAT_MIGRATION}" \
CAMERA_RULE_ZONE_COMPAT_MIGRATION="${CAMERA_RULE_ZONE_COMPAT_MIGRATION}" \
python3 - <<'PY'
import os
from pathlib import Path

import psycopg

database_url = os.environ["DATABASE_URL"]
migration = Path(os.environ["CAMERA_COMPAT_MIGRATION"])
zone_migration = Path(os.environ["CAMERA_RULE_ZONE_COMPAT_MIGRATION"])
required = {"source_id", "rtsp_url", "input_type", "rtsp_transport", "fps_policy", "alert_policy"}
with psycopg.connect(database_url, autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'cameras'
            """
        )
        columns = {row[0] for row in cur.fetchall()}
        if not required.issubset(columns):
            cur.execute(migration.read_text(encoding="utf-8"))
        cur.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'camera_rules'
              AND column_name = 'zone_id'
            """
        )
        zone_type = cur.fetchone()
        if not zone_type or zone_type[0] != "text":
            cur.execute(zone_migration.read_text(encoding="utf-8"))
PY

echo "[operator-smoke] creating/updating deterministic camera ${CAMERA_ID}"
camera_payload="$(mktemp)"
cat >"${camera_payload}" <<JSON
{
  "id": "${CAMERA_ID}",
  "name": "Operator Smoke Camera",
  "source_id": "${CAMERA_ID}_source",
  "rtsp_url": "rtsp://example.local/operator-smoke",
  "site_id": "operator_smoke",
  "location": "operator_smoke",
  "gpu_id": 0,
  "enabled": ${CAMERA_ENABLED},
  "input_type": "rtsp",
  "rtsp_transport": "tcp",
  "fps_policy": {"max_fps": "8/1", "min_fps": "2/1"},
  "alert_policy": {
    "global_alert_cooldown_s": 30,
    "store_suppressed_events": true,
    "suppress_record_request": true,
    "critical_bypass": false
  }
}
JSON

create_response="$(curl --noproxy '*' -sS -w '\n%{http_code}' \
  -H 'Content-Type: application/json' \
  -d @"${camera_payload}" \
  "${API_BASE_URL}/api/v1/cameras")"
create_status="$(printf '%s' "${create_response}" | tail -n1)"
if [[ "${create_status}" == "409" ]]; then
  curl_api -X PUT \
    -H 'Content-Type: application/json' \
    -d @"${camera_payload}" \
    "${API_BASE_URL}/api/v1/cameras/${CAMERA_ID}" >/dev/null
elif [[ "${create_status}" != "200" ]]; then
  printf '%s\n' "${create_response}"
  echo "FAIL_OPERATOR_CAMERA_REGISTRATION_API"
  exit 1
fi

curl_api "${API_BASE_URL}/api/v1/cameras/${CAMERA_ID}/config" | grep -q "${CAMERA_ID}"

if [[ -n "${FACE_IMAGE}" && -f "${FACE_IMAGE}" && -n "${YOLOV8_FACE_ONNX:-}" && -f "${YOLOV8_FACE_ONNX:-}" && -n "${ADAFACE_ONNX:-}" && -f "${ADAFACE_ONNX:-}" ]]; then
  echo "[operator-smoke] registering face image ${FACE_IMAGE}"
  face_response="$(curl_api -X POST \
    -F "image=@${FACE_IMAGE}" \
    -F "external_person_id=operator:smoke:face" \
    -F "name=Operator Smoke Face" \
    -F "is_primary=true" \
    -F "keep_crop=true" \
    "${API_BASE_URL}/api/v1/people/register-face")"
  gallery_id="$(printf '%s' "${face_response}" | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data["data"]["gallery_embedding_id"])')"
  DATABASE_URL="${DATABASE_URL}" GALLERY_ID="${gallery_id}" python3 - <<'PY'
import os
import psycopg

database_url = os.environ["DATABASE_URL"]
gallery_id = int(os.environ["GALLERY_ID"])
with psycopg.connect(database_url) as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_type, embedding_model, embedding_dim, is_active, payload
            FROM person_gallery_embeddings
            WHERE id = %s
            """,
            (gallery_id,),
        )
        row = cur.fetchone()
if not row:
    raise SystemExit("gallery row not found")
source_type, embedding_model, embedding_dim, is_active, payload = row
if source_type != "manual_upload" or embedding_model != "adaface" or embedding_dim != 512 or not is_active:
    raise SystemExit(f"unexpected gallery row: {row!r}")
if payload and payload.get("dev_mock_used") is True:
    raise SystemExit("dev mock payload not allowed")
PY
  echo "PASS_OPERATOR_CAMERA_AND_FACE_REGISTRATION_READY"
else
  echo "[operator-smoke] face registration skipped; set FACE_REGISTRATION_TEST_IMAGE, YOLOV8_FACE_ONNX, and ADAFACE_ONNX for full smoke"
  echo "PASS_OPERATOR_CAMERA_REGISTRATION_READY_FACE_REGISTRATION_SKIPPED"
fi
