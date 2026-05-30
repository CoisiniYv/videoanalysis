#!/usr/bin/env bash
set -euo pipefail

echo "=== V1.3 Frame-Aligned Visual Rendering Smoke ==="
echo "scope=debug_mvp_frame_aligned_visual_output"
echo "no_production_clip_worker=YES"
echo "no_production_media_worker=YES"
echo "no_replay_cache_sink_deployment=YES"
echo "no_db_migration=YES"
echo "no_performance_test=YES"

OUTPUT_ROOT="${V1_OUTPUT_ROOT:-manual-inspection/v1_visual_result_latest}"
A2A_ROOT="${V1_A2A_ROOT:-/data/video-analytics/media/debug/r3_3a2a_frame_uuid_identity}"
TRACE_ROOT="${V1_TRACE_ROOT:-/data/video-analytics/media/debug/r3_3a2a_frame_anchor_trace}"
WORK_DIR="${V1_WORK_DIR:-tmp/v1_visual_result_smoke}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"
SOURCE_MP4="${V1_SOURCE_MP4:-/home/user/video-analytics/testVideo/1080movie.mp4}"
CAMERA_CONFIG="${V1_CAMERA_CONFIG:-modules/savant_security/config/cameras.generated.yml}"
mkdir -p "${WORK_DIR}" "${OUTPUT_ROOT}"

psql_value() {
  local sql="$1"
  if command -v psql >/dev/null 2>&1; then
    psql "${DATABASE_URL}" -t -A -v ON_ERROR_STOP=1 -c "${sql}"
  else
    docker exec "${PG_CONTAINER}" psql -U video -d video_analytics \
      -t -A -v ON_ERROR_STOP=1 -c "${sql}"
  fi
}

latest_a2a_summary() {
  find "${A2A_ROOT}" -maxdepth 2 -name identity_summary.json -type f 2>/dev/null | sort | tail -1 || true
}

metadata_field() {
  local metadata_path="$1"
  local field_path="$2"
  python3 - "$metadata_path" "$field_path" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
cur = data
for part in sys.argv[2].split("."):
    cur = cur.get(part) if isinstance(cur, dict) else None
print("" if cur is None else cur)
PY
}

validate_behavior() {
  local metadata_path="$1"
  python3 - "$metadata_path" <<'PY'
import json
import sys
from pathlib import Path

metadata = json.loads(Path(sys.argv[1]).read_text())
ann = metadata["annotations"]
diag = metadata["diagnosis"]
media = metadata["media"]
failures = []
if diag.get("frame_alignment_status") != "matched":
    failures.append(f"frame_alignment_status={diag.get('frame_alignment_status')}")
if ann.get("person_bbox_status") != "generated":
    failures.append("person bbox missing")
if ann.get("roi_status") != "generated":
    failures.append("ROI missing")
if not media.get("annotated_snapshot_path") or not Path(media["annotated_snapshot_path"]).exists():
    failures.append("annotated snapshot missing")
if failures:
    raise SystemExit("FAIL: " + "; ".join(failures))
print("BEHAVIOR_VISUAL_PASS")
print(f"behavior_record_frame_uuid={diag.get('record_frame_uuid')}")
print(f"behavior_image_frame_uuid={diag.get('image_frame_uuid')}")
print(f"behavior_person_bbox_xyxy={diag.get('person_bbox_xyxy')}")
print(f"behavior_roi_status={ann.get('roi_status')}")
PY
}

validate_face_or_blocked() {
  local metadata_path="$1"
  python3 - "$metadata_path" <<'PY'
import json
import sys
from pathlib import Path

metadata = json.loads(Path(sys.argv[1]).read_text())
ann = metadata["annotations"]
diag = metadata["diagnosis"]
media = metadata["media"]
if diag.get("frame_alignment_status") == "matched":
    failures = []
    if ann.get("face_bbox_status") != "generated":
        failures.append("face bbox missing")
    if not media.get("annotated_snapshot_path") or not Path(media["annotated_snapshot_path"]).exists():
        failures.append("annotated snapshot missing")
    if failures:
        raise SystemExit("FAIL: " + "; ".join(failures))
    print("FACE_VISUAL_PASS")
    print(f"face_record_frame_uuid={diag.get('record_frame_uuid')}")
    print(f"face_image_frame_uuid={diag.get('image_frame_uuid')}")
    print(f"face_bbox_xyxy={diag.get('face_bbox_xyxy')}")
else:
    print("FACE_VISUAL_BLOCKED")
    print(f"face_record_frame_uuid={diag.get('record_frame_uuid')}")
    print(f"face_blocking_reason={diag.get('blocking_reason')}")
PY
}

write_gallery_unavailable() {
  local root="$1"
  mkdir -p "${root}/gallery_recognition"
  cat >"${root}/gallery_recognition/diagnosis.json" <<'JSON'
{
  "gallery_recognition_visual_sample": {
    "available": false,
    "reason": "no_frame_anchored_watchlist_hit_or_gallery_match"
  },
  "recognition_semantics_status": "unavailable_not_face_observation",
  "visual_correctness_status": "blocked"
}
JSON
  cat >"${root}/gallery_recognition/report.md" <<'EOF'
# Gallery Recognition Visual Sample

Status: unavailable.

No frame-anchored `watchlist_hit`, `live_search_hit`, or `gallery_match`
record is available for V1.3. A plain `face_observation` is not gallery
recognition and must not be presented as recognition output.
EOF
}

write_top_index() {
  local root="$1"
  local behavior_status face_status gallery_available
  behavior_status="$(metadata_field "${root}/behavior_intrusion/metadata.json" diagnosis.frame_alignment_status 2>/dev/null || true)"
  face_status="$(metadata_field "${root}/face_observation/metadata.json" diagnosis.frame_alignment_status 2>/dev/null || true)"
  gallery_available="false"
  cat >"${root}/README.md" <<'EOF'
# V1.3 Visual Result Latest

This is debug/MVP visual output, not production evidence. V1.3 only draws boxes
when the image is frame_uuid-aligned with the record. Manual review is required
before accepting visual correctness.
EOF
  cat >"${root}/index.html" <<EOF
<!doctype html>
<html>
<head><meta charset="utf-8"><title>V1.3 Visual Result Latest</title></head>
<body>
<h1>V1.3 Visual Result Latest</h1>
<p>Debug/MVP visual output, not production evidence.</p>
<p>Manual review required before accepting visual correctness.</p>
<ul>
  <li>behavior_intrusion frame_alignment_status: <strong>${behavior_status}</strong></li>
  <li>face_observation frame_alignment_status: <strong>${face_status}</strong></li>
  <li>gallery recognition available: <strong>${gallery_available}</strong></li>
</ul>
<h2>Behavior Intrusion</h2>
<ul>
  <li><a href="behavior_intrusion/diagnosis.json">diagnosis</a></li>
  <li><a href="behavior_intrusion/annotated_snapshot.jpg">annotated snapshot</a></li>
  <li><a href="behavior_intrusion/report.md">report</a></li>
</ul>
<h2>Face Observation</h2>
<ul>
  <li><a href="face_observation/diagnosis.json">diagnosis</a></li>
  <li><a href="face_observation/annotated_snapshot.jpg">annotated snapshot</a></li>
  <li><a href="face_observation/report.md">report</a></li>
</ul>
<h2>Gallery Recognition</h2>
<ul>
  <li><a href="gallery_recognition/diagnosis.json">diagnosis</a></li>
  <li><a href="gallery_recognition/report.md">report</a></li>
</ul>
</body>
</html>
EOF
}

SUMMARY_JSON="$(latest_a2a_summary)"
if [[ -z "${SUMMARY_JSON}" || ! -f "${SUMMARY_JSON}" ]]; then
  echo "FAIL: no A2a identity_summary.json found under ${A2A_ROOT}; behavior requires frame_uuid-aligned material"
  exit 1
fi
echo "behavior_summary_json=${SUMMARY_JSON}"

BEHAVIOR_FRAME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("inspection_material_path") or "")' "${SUMMARY_JSON}")"
BEHAVIOR_CLIP="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("source_aligned_clip_path") or "")' "${SUMMARY_JSON}")"
if [[ ! -f "${BEHAVIOR_FRAME}" ]]; then
  echo "FAIL: behavior A2a matched frame missing: ${BEHAVIOR_FRAME}"
  exit 1
fi

python3 scripts/debug/generate_visual_result.py \
  --a2a-summary-json "${SUMMARY_JSON}" \
  --source-frame "${BEHAVIOR_FRAME}" \
  --source-clip "${BEHAVIOR_CLIP}" \
  --output-root "${OUTPUT_ROOT}" \
  --run-id behavior_intrusion \
  --result-type behavior_intrusion \
  --camera-config "${CAMERA_CONFIG}" \
  --person-bbox-format xywh \
  --source-mp4 "${SOURCE_MP4}" \
  --frame-trace-root "${TRACE_ROOT}" \
  | tee "${WORK_DIR}/behavior_generate.log"

BEHAVIOR_METADATA="${OUTPUT_ROOT}/behavior_intrusion/metadata.json"
validate_behavior "${BEHAVIOR_METADATA}"

FACE_JSON="${WORK_DIR}/face_observation.json"
FACE_ROW="$(psql_value "
SELECT json_build_object(
  'source_observation_id', source_observation_id,
  'message_type', 'face_observation',
  'camera_id', camera_id,
  'source_id', source_id,
  'track_id', track_id,
  'timestamp_ms', timestamp_ms,
  'frame_num', frame_num,
  'face_bbox', face_bbox,
  'person_bbox', person_bbox,
  'landmarks', landmarks,
  'quality', quality,
  'payload', payload
)::text
FROM face_observations
WHERE face_bbox IS NOT NULL
ORDER BY created_at DESC
LIMIT 1;
")"
if [[ -z "${FACE_ROW}" ]]; then
  mkdir -p "${OUTPUT_ROOT}/face_observation"
  cat >"${OUTPUT_ROOT}/face_observation/diagnosis.json" <<'JSON'
{"frame_alignment_status":"blocked","blocking_reason":"no_face_observation_with_face_bbox"}
JSON
  echo "FACE_VISUAL_BLOCKED"
else
  printf "%s" "${FACE_ROW}" >"${FACE_JSON}"
  python3 scripts/debug/generate_visual_result.py \
    --event-json "${FACE_JSON}" \
    --source-mp4 "${SOURCE_MP4}" \
    --frame-trace-root "${TRACE_ROOT}" \
    --output-root "${OUTPUT_ROOT}" \
    --run-id face_observation \
    --result-type face_observation \
    --face-bbox-format cxcywh \
    | tee "${WORK_DIR}/face_generate.log"
  FACE_METADATA="${OUTPUT_ROOT}/face_observation/metadata.json"
  validate_face_or_blocked "${FACE_METADATA}"
fi

write_gallery_unavailable "${OUTPUT_ROOT}"
write_top_index "${OUTPUT_ROOT}"

echo "GALLERY_RECOGNITION_UNAVAILABLE"
echo "gallery_reason=no_frame_anchored_watchlist_hit_or_gallery_match"
echo "manual_index=${OUTPUT_ROOT}/index.html"
echo "manual_readme=${OUTPUT_ROOT}/README.md"

FACE_STATUS="$(metadata_field "${OUTPUT_ROOT}/face_observation/metadata.json" diagnosis.frame_alignment_status 2>/dev/null || echo blocked)"
if [[ "${FACE_STATUS}" == "matched" ]]; then
  echo "V1.3 PASS: behavior visual pass; face visual pass; gallery recognition unavailable"
else
  echo "V1.3 PARTIAL PASS: behavior visual pass; face visual blocked due to no frame-aligned image; gallery recognition unavailable"
fi
