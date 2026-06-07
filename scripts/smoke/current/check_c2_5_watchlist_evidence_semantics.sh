#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

INPUT_BUNDLE="${C2_5_INPUT_BUNDLE:-/data/video-analytics/media/evidence/c2_4_identity_binding_20260607T211846}"
EVIDENCE_ROOT="${C2_5_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
AUDIT_ROOT="${C2_5_AUDIT_ROOT:-/data/video-analytics/media/evidence_audit}"
RUN_ID="${C2_5_RUN_ID:-c2_5_watchlist_hit_$(date +%Y%m%dT%H%M%S)}"
OUTPUT_BUNDLE="${C2_5_OUTPUT_BUNDLE:-$EVIDENCE_ROOT/$RUN_ID}"
AUDIT_DIR="${C2_5_AUDIT_DIR:-$AUDIT_ROOT/$RUN_ID}"
REPORT_PATH="${C2_5_REPORT_PATH:-/tmp/${RUN_ID}-report.json}"

PERSON_ID="${C2_5_PERSON_ID:-4}"
EXTERNAL_PERSON_ID="${C2_5_EXTERNAL_PERSON_ID:-test:c2_4:person}"
SOURCE_OBSERVATION_ID="${C2_5_SOURCE_OBSERVATION_ID:-face:c2_post_savant_fps_probe:4:17854:1}"
WATCHLIST_RULE_ID="${C2_5_WATCHLIST_RULE_ID:-c2_5_test_watchlist_rule}"
WATCHLIST_RULE_NAME="${C2_5_WATCHLIST_RULE_NAME:-C2.5 Test Watchlist Rule}"
PERSON_NAME="${C2_5_PERSON_NAME:-C2.5 Test Watchlist Person}"
THRESHOLD="${C2_5_THRESHOLD:-0.99}"

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
  fail "FAIL_C2_5_WATCHLIST_EVENT_SEMANTICS_BLOCKED" "input_bundle_missing"
fi

BUILDER_OUTPUT="$(python "$ROOT_DIR/scripts/tools/build_c2_watchlist_evidence_bundle.py" \
  --input-bundle "$INPUT_BUNDLE" \
  --output-dir "$OUTPUT_BUNDLE" \
  --person-id "$PERSON_ID" \
  --external-person-id "$EXTERNAL_PERSON_ID" \
  --source-observation-id "$SOURCE_OBSERVATION_ID" \
  --watchlist-rule-id "$WATCHLIST_RULE_ID" \
  --watchlist-rule-name "$WATCHLIST_RULE_NAME" \
  --person-name "$PERSON_NAME" \
  --threshold "$THRESHOLD" \
  --overwrite 2>&1)" || {
    echo "$BUILDER_OUTPUT" >&2
    fail "FAIL_C2_5_WATCHLIST_EVENT_SEMANTICS_BLOCKED" "watchlist_builder_failed"
  }
echo "$BUILDER_OUTPUT"

SELECTED_FRAME="$(python - "$OUTPUT_BUNDLE" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads((Path(sys.argv[1]) / "watchlist_evidence_summary.json").read_text(encoding="utf-8"))
print(summary["frame_num"])
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
    fail "FAIL_C2_5_WATCHLIST_EVENT_SEMANTICS_BLOCKED" "c2_3q_audit_failed"
  }

python - "$OUTPUT_BUNDLE" "$AUDIT_DIR" "$REPORT_PATH" "$INPUT_BUNDLE" <<'PY'
import json
import sys
from pathlib import Path

bundle_dir, audit_dir, report_path, input_bundle = map(Path, sys.argv[1:5])
summary = json.loads((bundle_dir / "summary.json").read_text(encoding="utf-8"))
event = json.loads((bundle_dir / "watchlist_event.json").read_text(encoding="utf-8"))
watchlist = json.loads((bundle_dir / "watchlist_evidence_summary.json").read_text(encoding="utf-8"))
audit = json.loads((audit_dir / "audit_summary.json").read_text(encoding="utf-8"))
rows = [
    json.loads(line)
    for line in (bundle_dir / "annotations.frame_cache.identity.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]

forbidden_keys = {
    "embedding",
    "embeddings",
    "embedding_vector",
    "image_bytes",
    "crop_bytes",
    "frame_bytes",
    "base64",
    "base64_image",
}

def find_forbidden(value, path=""):
    hits = []
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            if lowered in forbidden_keys:
                hits.append(path + str(key))
            hits.extend(find_forbidden(nested, path + str(key) + "."))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            hits.extend(find_forbidden(nested, f"{path}{index}."))
    return hits

target = None
for row in rows:
    for obj in row.get("objects") or []:
        identity = obj.get("identity") if isinstance(obj.get("identity"), dict) else {}
        if identity.get("source_observation_id") == event.get("source_observation_id"):
            target = obj
            break
    if target:
        break

errors = []
if event.get("event_type") != "watchlist_hit":
    errors.append("event_type_not_watchlist_hit")
for key in ("source_observation_id", "person_id", "gallery_embedding_id", "match_result_id", "similarity", "threshold", "watchlist_rule_id"):
    if event.get(key) in (None, ""):
        errors.append(f"event_missing_{key}")
if find_forbidden(event):
    errors.append("event_payload_contains_embedding_or_image_bytes")
if target is None:
    errors.append("sidecar_watchlist_target_missing")
else:
    identity = target.get("identity") if isinstance(target.get("identity"), dict) else {}
    if target.get("object_type") != "known_face":
        errors.append("target_not_known_face")
    if identity.get("event_type") != "watchlist_hit":
        errors.append("sidecar_identity_event_type_missing")
    if identity.get("watchlist_rule_id") != event.get("watchlist_rule_id"):
        errors.append("sidecar_watchlist_rule_mismatch")
    if identity.get("identity_source") != "match_results":
        errors.append("sidecar_identity_source_not_match_results")
if int(summary.get("watchlist_hit_count") or 0) != 1:
    errors.append("watchlist_hit_count_not_1")
if int(summary.get("known_face_count") or 0) <= 0:
    errors.append("known_face_count_not_positive")
if summary.get("production_ready") is not True:
    errors.append("production_ready_not_true")
if (summary.get("video_integrity") or {}).get("production_gate_passed") is not True:
    errors.append("video_integrity_gate_not_passed")
if summary.get("fallback_used") is not False:
    errors.append("fallback_used_true")
if summary.get("legacy_used_for_visual_binding") is not False:
    errors.append("legacy_used_for_visual_binding_true")
if summary.get("evidence_capture_mode") != "stable_post_savant_sink_time_crop":
    errors.append("capture_mode_not_stable_sink_crop")
if summary.get("workaround_used") is not True:
    errors.append("workaround_used_not_true")
if summary.get("event_style_replay_job_passed") is not False:
    errors.append("event_style_replay_job_passed_not_false")
if audit.get("result_marker") == "FAIL_C2_3Q_EVIDENCE_OUTPUT_AUDIT_BLOCKED":
    errors.append("c2_3q_audit_failed")

marker = "PASS_C2_5_WATCHLIST_EVENT_EVIDENCE_SEMANTICS_READY"
if errors:
    marker = "FAIL_C2_5_WATCHLIST_EVENT_SEMANTICS_BLOCKED"

report = {
    "result_marker": marker,
    "errors": errors,
    "input_bundle": str(input_bundle),
    "output_bundle": str(bundle_dir),
    "c2_3q_audit_output_dir": str(audit_dir),
    "audit_result_marker": audit.get("result_marker"),
    "audit_index_html": str(audit_dir / "index.html"),
    "audit_contact_sheet": str(audit_dir / "contact_sheet.jpg"),
    "watchlist_event_json": str(bundle_dir / "watchlist_event.json"),
    "watchlist_evidence_summary_json": str(bundle_dir / "watchlist_evidence_summary.json"),
    "watchlist_evidence_report_html": str(bundle_dir / "watchlist_evidence_report.html"),
    "source_event_id": event.get("source_event_id"),
    "watchlist_rule_id": event.get("watchlist_rule_id"),
    "person_id": event.get("person_id"),
    "external_person_id": event.get("external_person_id"),
    "source_observation_id": event.get("source_observation_id"),
    "gallery_embedding_id": event.get("gallery_embedding_id"),
    "match_result_id": event.get("match_result_id"),
    "similarity": event.get("similarity"),
    "threshold": event.get("threshold"),
    "frame_num": event.get("frame_num"),
    "track_id": event.get("track_id"),
    "join_method": watchlist.get("join_method"),
    "decoded_video_frame_count": summary.get("decoded_video_frame_count"),
    "metadata_frame_count": summary.get("original_metadata_frame_count"),
    "sidecar_frame_count": summary.get("sidecar_frame_count"),
    "known_face_count": summary.get("known_face_count"),
    "watchlist_hit_count": summary.get("watchlist_hit_count"),
    "unknown_face_count": summary.get("unknown_face_count"),
    "production_ready": summary.get("production_ready"),
    "video_integrity_status": (summary.get("video_integrity") or {}).get("integrity_status"),
    "video_integrity_gate_passed": (summary.get("video_integrity") or {}).get("production_gate_passed"),
    "fallback_used": summary.get("fallback_used"),
    "legacy_used_for_visual_binding": summary.get("legacy_used_for_visual_binding"),
    "evidence_capture_mode": summary.get("evidence_capture_mode"),
    "workaround_used": summary.get("workaround_used"),
    "event_style_replay_job_passed": summary.get("event_style_replay_job_passed"),
    "viewer_verified": False,
    "viewer_note": "Live evidence-viewer HTTP is not required for C2.5 file semantics.",
}
Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2, sort_keys=True))
raise SystemExit(2 if errors else 0)
PY
