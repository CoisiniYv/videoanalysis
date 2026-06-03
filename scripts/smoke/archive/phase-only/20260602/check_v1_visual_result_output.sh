#!/usr/bin/env bash
set -euo pipefail

echo "=== V1.4 Actual Frame Image Source Proof Smoke ==="
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
EXPLICIT_INDEX_ROOT="${V1_EXPLICIT_FRAME_UUID_INDEX_ROOT:-/data/video-analytics/media/debug/explicit_frame_uuid_index}"
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
failures = []
if diag.get("frame_alignment_status") == "matched":
    if diag.get("image_frame_uuid_proof_source") not in {
        "runtime_frame_dump_sidecar",
        "frame_uuid_trace_exact_hit",
        "explicit_frame_uuid_index",
    }:
        failures.append(f"untrusted proof_source={diag.get('image_frame_uuid_proof_source')}")
    source_extraction = diag.get("source_extraction") or {}
    if source_extraction and source_extraction.get("actual_frame_index") in (None, 0):
        failures.append("source extraction first frame or unknown")
    if ann.get("person_bbox_status") != "generated":
        failures.append("person bbox missing")
    if ann.get("roi_status") != "generated":
        failures.append("ROI missing")
else:
    if diag.get("bbox_drawn") is True:
        failures.append("bbox drawn while frame proof blocked")
if failures:
    raise SystemExit("FAIL: " + "; ".join(failures))
print("V1.4 BEHAVIOR_FRAME_PROOF_PASS" if diag.get("frame_alignment_status") == "matched" else "V1.4 FRAME_PROOF_BLOCKED")
print(f"behavior_record_frame_uuid={diag.get('record_frame_uuid')}")
print(f"behavior_image_frame_uuid={diag.get('image_frame_uuid')}")
print(f"behavior_image_proof_source={diag.get('image_frame_uuid_proof_source')}")
print(f"behavior_image_sha256={diag.get('image_sha256')}")
print(f"behavior_is_first_frame={diag.get('is_first_frame')}")
print(f"behavior_bbox_drawn={diag.get('bbox_drawn')}")
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
if diag.get("frame_alignment_status") == "matched":
    failures = []
    if diag.get("image_frame_uuid_proof_source") not in {
        "runtime_frame_dump_sidecar",
        "frame_uuid_trace_exact_hit",
        "explicit_frame_uuid_index",
    }:
        failures.append(f"untrusted proof_source={diag.get('image_frame_uuid_proof_source')}")
    source_extraction = diag.get("source_extraction") or {}
    if source_extraction and source_extraction.get("actual_frame_index") in (None, 0):
        failures.append("source extraction first frame or unknown")
    if ann.get("face_bbox_status") != "generated":
        failures.append("face bbox missing")
    if failures:
        raise SystemExit("FAIL: " + "; ".join(failures))
    print("V1.4 FACE_FRAME_PROOF_PASS")
    print(f"face_record_frame_uuid={diag.get('record_frame_uuid')}")
    print(f"face_image_frame_uuid={diag.get('image_frame_uuid')}")
    print(f"face_image_proof_source={diag.get('image_frame_uuid_proof_source')}")
    print(f"face_image_sha256={diag.get('image_sha256')}")
    print(f"face_is_first_frame={diag.get('is_first_frame')}")
    print(f"face_bbox_drawn={diag.get('bbox_drawn')}")
else:
    if diag.get("bbox_drawn") is True:
        raise SystemExit("FAIL: face bbox drawn while frame proof blocked")
    print("V1.4 FRAME_PROOF_BLOCKED")
    print(f"face_record_frame_uuid={diag.get('record_frame_uuid')}")
    print(f"face_blocking_reason={diag.get('blocking_reason')}")
PY
}

validate_cross_result_reuse() {
  local behavior_diag="$1"
  local face_diag="$2"
  python3 - "$behavior_diag" "$face_diag" <<'PY'
import json
import sys
from pathlib import Path

behavior = json.loads(Path(sys.argv[1]).read_text()) if Path(sys.argv[1]).exists() else {}
face = json.loads(Path(sys.argv[2]).read_text()) if Path(sys.argv[2]).exists() else {}
same_sha = bool(behavior.get("image_sha256") and behavior.get("image_sha256") == face.get("image_sha256"))
different_frames = bool(
    behavior.get("record_frame_uuid")
    and face.get("record_frame_uuid")
    and behavior.get("record_frame_uuid") != face.get("record_frame_uuid")
)
if same_sha and different_frames:
    print("V1.4 IMAGE_REUSE_FAILED")
    print("reason=different_records_reused_same_image")
    raise SystemExit(1)
print(f"same_image_reused={str(same_sha).lower()}")
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
record is available for V1.4. A plain `face_observation` is not gallery
recognition and must not be presented as recognition output.
EOF
}

write_top_index() {
  local root="$1"
  local behavior_status face_status gallery_available
  local behavior_proof face_proof behavior_sha face_sha behavior_first face_first
  behavior_status="$(metadata_field "${root}/behavior_intrusion/metadata.json" diagnosis.frame_alignment_status 2>/dev/null || true)"
  face_status="$(metadata_field "${root}/face_observation/metadata.json" diagnosis.frame_alignment_status 2>/dev/null || true)"
  behavior_proof="$(metadata_field "${root}/behavior_intrusion/metadata.json" diagnosis.image_frame_uuid_proof_source 2>/dev/null || true)"
  face_proof="$(metadata_field "${root}/face_observation/metadata.json" diagnosis.image_frame_uuid_proof_source 2>/dev/null || true)"
  behavior_sha="$(metadata_field "${root}/behavior_intrusion/metadata.json" diagnosis.image_sha256 2>/dev/null || true)"
  face_sha="$(metadata_field "${root}/face_observation/metadata.json" diagnosis.image_sha256 2>/dev/null || true)"
  behavior_first="$(metadata_field "${root}/behavior_intrusion/metadata.json" diagnosis.is_first_frame 2>/dev/null || true)"
  face_first="$(metadata_field "${root}/face_observation/metadata.json" diagnosis.is_first_frame 2>/dev/null || true)"
  gallery_available="false"
  cat >"${root}/README.md" <<EOF
# V1.4 Visual Result Latest

This is debug/MVP visual output, not production evidence. V1.4 only accepts
boxes when the image has independent proof that it is the record frame UUID.
If proof is blocked, no boxes are accepted.

- Behavior frame proof: ${behavior_status}
- Behavior image proof source: ${behavior_proof}
- Behavior image sha256: ${behavior_sha}
- Behavior is first frame: ${behavior_first}
- Face frame proof: ${face_status}
- Face image proof source: ${face_proof}
- Face image sha256: ${face_sha}
- Face is first frame: ${face_first}
- Gallery recognition available: ${gallery_available}
EOF
  cat >"${root}/index.html" <<EOF
<!doctype html>
<html>
<head><meta charset="utf-8"><title>V1.4 Visual Result Latest</title></head>
<body>
<h1>V1.4 Visual Result Latest</h1>
<p>Debug/MVP visual output, not production evidence.</p>
<p>Blocked frame proof means boxes are not accepted.</p>
<ul>
  <li>Behavior frame proof: <strong>${behavior_status}</strong></li>
  <li>Behavior image proof source: <strong>${behavior_proof}</strong></li>
  <li>Behavior image sha256: <code>${behavior_sha}</code></li>
  <li>Behavior is first frame: <strong>${behavior_first}</strong></li>
  <li>Face frame proof: <strong>${face_status}</strong></li>
  <li>Face image proof source: <strong>${face_proof}</strong></li>
  <li>Face image sha256: <code>${face_sha}</code></li>
  <li>Face is first frame: <strong>${face_first}</strong></li>
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
  --explicit-frame-uuid-index-root "${EXPLICIT_INDEX_ROOT}" \
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
{
  "bbox_drawn": false,
  "blocking_reason": "no_face_observation_with_face_bbox",
  "frame_alignment_status": "blocked",
  "frame_proof_status": "blocked",
  "image_frame_uuid_proof_source": "unknown",
  "image_sha256": null,
  "is_first_frame": "unknown",
  "recognition_semantics_status": "face_observation_only_not_gallery_match",
  "visual_correctness_status": "blocked"
}
JSON
  cat >"${OUTPUT_ROOT}/face_observation/metadata.json" <<'JSON'
{
  "annotations": {
    "face_bbox_status": "missing_required",
    "frame_alignment_status": "blocked"
  },
  "diagnosis": {
    "bbox_drawn": false,
    "blocking_reason": "no_face_observation_with_face_bbox",
    "frame_alignment_status": "blocked",
    "frame_proof_status": "blocked",
    "image_frame_uuid_proof_source": "unknown",
    "image_sha256": null,
    "is_first_frame": "unknown",
    "recognition_semantics_status": "face_observation_only_not_gallery_match",
    "visual_correctness_status": "blocked"
  },
  "media": {
    "annotated_snapshot_path": null,
    "snapshot_annotation_status": "blocked"
  }
}
JSON
  echo "V1.4 FRAME_PROOF_BLOCKED"
else
  printf "%s" "${FACE_ROW}" >"${FACE_JSON}"
  python3 scripts/debug/generate_visual_result.py \
    --event-json "${FACE_JSON}" \
    --source-mp4 "${SOURCE_MP4}" \
    --frame-trace-root "${TRACE_ROOT}" \
    --explicit-frame-uuid-index-root "${EXPLICIT_INDEX_ROOT}" \
    --output-root "${OUTPUT_ROOT}" \
    --run-id face_observation \
    --result-type face_observation \
    --face-bbox-format cxcywh \
    | tee "${WORK_DIR}/face_generate.log"
  FACE_METADATA="${OUTPUT_ROOT}/face_observation/metadata.json"
  validate_face_or_blocked "${FACE_METADATA}"
fi

write_gallery_unavailable "${OUTPUT_ROOT}"
validate_cross_result_reuse \
  "${OUTPUT_ROOT}/behavior_intrusion/diagnosis.json" \
  "${OUTPUT_ROOT}/face_observation/diagnosis.json"
write_top_index "${OUTPUT_ROOT}"

echo "GALLERY_RECOGNITION_UNAVAILABLE"
echo "gallery_reason=no_frame_anchored_watchlist_hit_or_gallery_match"
echo "manual_index=${OUTPUT_ROOT}/index.html"
echo "manual_readme=${OUTPUT_ROOT}/README.md"

BEHAVIOR_STATUS="$(metadata_field "${OUTPUT_ROOT}/behavior_intrusion/metadata.json" diagnosis.frame_alignment_status 2>/dev/null || echo blocked)"
FACE_STATUS="$(metadata_field "${OUTPUT_ROOT}/face_observation/metadata.json" diagnosis.frame_alignment_status 2>/dev/null || echo blocked)"
if [[ "${BEHAVIOR_STATUS}" == "matched" && "${FACE_STATUS}" == "matched" ]]; then
  echo "V1.4 BEHAVIOR_FRAME_PROOF_PASS"
  echo "V1.4 FACE_FRAME_PROOF_PASS"
else
  echo "V1.4 FRAME_PROOF_BLOCKED"
fi
