#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

INPUT_BUNDLE="${C2_4_INPUT_BUNDLE:-/data/video-analytics/media/evidence/c2_3b_r2_20260607T201306_stable_sink_crop}"
EVIDENCE_ROOT="${C2_4_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
AUDIT_ROOT="${C2_4_AUDIT_ROOT:-/data/video-analytics/media/evidence_audit}"
RUN_ID="${C2_4_RUN_ID:-c2_4_identity_binding_$(date +%Y%m%dT%H%M%S)}"
OUTPUT_BUNDLE="${C2_4_OUTPUT_BUNDLE:-$EVIDENCE_ROOT/$RUN_ID}"
AUDIT_DIR="${C2_4_AUDIT_DIR:-$AUDIT_ROOT/$RUN_ID}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
REDIS_URL="${C2_4_REDIS_URL:-redis://127.0.0.1:6395/0}"
REDIS_SOURCE_ID="${C2_4_REDIS_SOURCE_ID:-c2_post_savant_fps_probe}"
EXTERNAL_PERSON_ID="${C2_4_EXTERNAL_PERSON_ID:-test:c2_4:person}"
PERSON_NAME="${C2_4_PERSON_NAME:-C2.4 Test Person}"
SEARCH_REQUEST_ID="${C2_4_SEARCH_REQUEST_ID:-c2400000-0000-4000-8000-000000000001}"
REPORT_PATH="${C2_4_REPORT_PATH:-/tmp/${RUN_ID}-report.json}"

fail() {
  local marker="$1"
  local reason="$2"
  python - "$marker" "$reason" "$REPORT_PATH" "$INPUT_BUNDLE" "$OUTPUT_BUNDLE" "$AUDIT_DIR" <<'PY'
import json
import sys
from pathlib import Path

marker, reason, report_path, input_bundle, output_bundle, audit_dir = sys.argv[1:7]
payload = {
    "result_marker": marker,
    "reason": reason,
    "input_bundle": input_bundle,
    "output_bundle": output_bundle,
    "c2_3q_audit_output_dir": audit_dir,
}
Path(report_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
PY
  exit 2
}

if [[ ! -d "$INPUT_BUNDLE" ]]; then
  fail "FAIL_C2_4_IDENTITY_BINDING_BLOCKED" "input_bundle_missing"
fi

BUILDER_OUTPUT="$(python "$ROOT_DIR/scripts/tools/build_c2_identity_patched_evidence_bundle.py" \
  --input-bundle "$INPUT_BUNDLE" \
  --output-dir "$OUTPUT_BUNDLE" \
  --database-url "$DATABASE_URL" \
  --redis-url "$REDIS_URL" \
  --redis-source-id "$REDIS_SOURCE_ID" \
  --external-person-id "$EXTERNAL_PERSON_ID" \
  --person-name "$PERSON_NAME" \
  --search-request-id "$SEARCH_REQUEST_ID" \
  --similarity-threshold 0.99 \
  --join-iou-threshold 0.995 \
  --join-pts-tolerance-ns 2000000 \
  --prepare-db-schema \
  --reset-test-identity \
  --overwrite 2>&1)" || {
    echo "$BUILDER_OUTPUT" >&2
    if grep -q "PARTIAL_C2_4_IDENTITY_JOIN_KEY_MISSING" <<<"$BUILDER_OUTPUT"; then
      fail "PARTIAL_C2_4_IDENTITY_JOIN_KEY_MISSING" "no_reliable_sidecar_to_face_observation_join"
    fi
    fail "FAIL_C2_4_IDENTITY_BINDING_BLOCKED" "identity_patch_builder_failed"
  }
echo "$BUILDER_OUTPUT"

SELECTED_FRAME="$(python - "$OUTPUT_BUNDLE" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads((Path(sys.argv[1]) / "c2_4_identity_binding_summary.json").read_text(encoding="utf-8"))
print(summary["selected_frame"])
PY
)"

SAMPLE_FRAMES="$(python - "$OUTPUT_BUNDLE" "$SELECTED_FRAME" <<'PY'
import json
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
selected = int(sys.argv[2])
summary = json.loads((bundle / "summary.json").read_text(encoding="utf-8"))
count = int(summary.get("sidecar_frame_count") or summary.get("frame_count") or 0)
frames = {0, selected, max(0, selected - 2), min(max(0, count - 1), selected + 2), count - 1}
print(",".join(str(frame) for frame in sorted(frames) if 0 <= frame < count))
PY
)"

python "$ROOT_DIR/scripts/tools/audit_c2_evidence_output_correctness.py" \
  --bundle-dir "$OUTPUT_BUNDLE" \
  --output-dir "$AUDIT_DIR" \
  --sample-frames "$SAMPLE_FRAMES" >/tmp/"${RUN_ID}-audit.log" 2>&1 || {
    cat /tmp/"${RUN_ID}-audit.log" >&2
    fail "FAIL_C2_4_IDENTITY_BINDING_BLOCKED" "c2_3q_audit_failed"
  }

python - "$OUTPUT_BUNDLE" "$AUDIT_DIR" "$REPORT_PATH" "$INPUT_BUNDLE" "$DATABASE_URL" <<'PY'
import json
import sys
from pathlib import Path

bundle_dir, audit_dir, report_path, input_bundle = map(Path, sys.argv[1:5])
database_url = sys.argv[5]
summary = json.loads((bundle_dir / "summary.json").read_text(encoding="utf-8"))
c24 = json.loads((bundle_dir / "c2_4_identity_binding_summary.json").read_text(encoding="utf-8"))
audit = json.loads((audit_dir / "audit_summary.json").read_text(encoding="utf-8"))
counts = summary.get("object_counts") or {}
video_integrity = summary.get("video_integrity") or {}

errors = []
if int(counts.get("known_face") or 0) <= 0:
    errors.append("known_face_count_not_positive")
if summary.get("identity_binding_connected") is not True:
    errors.append("identity_binding_not_connected")
if summary.get("identity_patch_source") != "match_results":
    errors.append("identity_patch_source_not_match_results")
if summary.get("fallback_used") is not False:
    errors.append("fallback_used_true")
if summary.get("legacy_used_for_visual_binding") is not False:
    errors.append("legacy_used_for_visual_binding_true")
if summary.get("allow_db_annotation_fallback") is not False:
    errors.append("allow_db_annotation_fallback_not_false")
if summary.get("allow_legacy_annotation_fallback") is not False:
    errors.append("allow_legacy_annotation_fallback_not_false")
if summary.get("production_ready") is not True:
    errors.append("production_ready_not_true")
if video_integrity.get("production_gate_passed") is not True:
    errors.append("video_integrity_gate_not_passed")
if summary.get("evidence_capture_mode") != "stable_post_savant_sink_time_crop":
    errors.append("capture_mode_not_stable_sink_crop")
if summary.get("workaround_used") is not True:
    errors.append("workaround_used_not_true")
if summary.get("event_style_replay_job_passed") is not False:
    errors.append("event_style_replay_job_passed_not_false")
if audit.get("result_marker") == "FAIL_C2_3Q_EVIDENCE_OUTPUT_AUDIT_BLOCKED":
    errors.append("c2_3q_audit_failed")
if c24.get("geometry_modified") is not False:
    errors.append("identity_patch_modified_geometry")
for key in ("source_observation_id", "person_id", "gallery_embedding_id", "match_result_id", "similarity"):
    if c24.get(key) in (None, ""):
        errors.append(f"missing_{key}")

marker = "PASS_C2_4_IDENTITY_BINDING_EVIDENCE_PATCH_READY"
if errors:
    marker = "FAIL_C2_4_IDENTITY_BINDING_BLOCKED"

report = {
    "result_marker": marker,
    "errors": errors,
    "input_bundle": str(input_bundle),
    "output_bundle": str(bundle_dir),
    "c2_3q_audit_output_dir": str(audit_dir),
    "audit_result_marker": audit.get("result_marker"),
    "index_html": str(audit_dir / "index.html"),
    "contact_sheet": str(audit_dir / "contact_sheet.jpg"),
    "database_url": database_url,
    "selected_frame": c24.get("selected_frame"),
    "selected_track_id": c24.get("selected_track_id"),
    "source_observation_id": c24.get("source_observation_id"),
    "face_bbox": c24.get("face_bbox"),
    "person_id": c24.get("person_id"),
    "external_person_id": c24.get("external_person_id"),
    "gallery_embedding_id": c24.get("gallery_embedding_id"),
    "match_result_id": c24.get("match_result_id"),
    "similarity": c24.get("similarity"),
    "threshold": c24.get("threshold"),
    "join_method": c24.get("join_method"),
    "decoded_video_frame_count": summary.get("decoded_video_frame_count"),
    "metadata_frame_count": summary.get("original_metadata_frame_count"),
    "sidecar_frame_count": summary.get("sidecar_frame_count"),
    "known_face_count": counts.get("known_face"),
    "unknown_face_count": summary.get("unknown_face_count"),
    "person_count": counts.get("person"),
    "face_count": counts.get("face"),
    "production_ready": summary.get("production_ready"),
    "video_integrity_status": video_integrity.get("integrity_status"),
    "video_integrity_gate_passed": video_integrity.get("production_gate_passed"),
    "fallback_used": summary.get("fallback_used"),
    "legacy_used_for_visual_binding": summary.get("legacy_used_for_visual_binding"),
    "allow_db_annotation_fallback": summary.get("allow_db_annotation_fallback"),
    "allow_legacy_annotation_fallback": summary.get("allow_legacy_annotation_fallback"),
    "evidence_capture_mode": summary.get("evidence_capture_mode"),
    "workaround_used": summary.get("workaround_used"),
    "event_style_replay_job_passed": summary.get("event_style_replay_job_passed"),
}
Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2, sort_keys=True))
raise SystemExit(2 if errors else 0)
PY
