#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

EVENT_ID="${C2_10_EVENT_ID:-b4cf4b6b-9d90-4282-9b7f-5e0c56e81a32}"
SOURCE_EVENT_ID="${C2_10_SOURCE_EVENT_ID:-c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:c2_7_event_worker_persistence_20260607T223424}"
C2_8_RESPONSE="${C2_10_C2_8_RESPONSE:-/data/video-analytics/media/evidence/c2_8_api_evidence_detail_20260607T225039/api_evidence_detail_response.json}"
C2_6R_BUNDLE="${C2_10_C2_6R_BUNDLE:-/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035}"
C2_6R_AUDIT="${C2_10_C2_6R_AUDIT:-/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035}"
EVIDENCE_ROOT="${C2_10_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
RUN_ID="${C2_10_RUN_ID:-c2_10_runtime_viewer_$(date +%Y%m%dT%H%M%S)}"
OUTPUT_DIR="${C2_10_OUTPUT_DIR:-$EVIDENCE_ROOT/$RUN_ID}"
REPORT_PATH="${C2_10_REPORT_PATH:-/tmp/${RUN_ID}-report.json}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
API_BASE_URL="${C2_10_API_BASE_URL:-}"
VIEWER_BASE_URL="${C2_10_VIEWER_BASE_URL:-http://127.0.0.1:8090}"
START_TEMP_API="${C2_10_START_TEMP_API:-true}"

fail() {
  local marker="$1"
  local reason="$2"
  python - "$marker" "$reason" "$REPORT_PATH" "$OUTPUT_DIR" "$EVENT_ID" "$SOURCE_EVENT_ID" <<'PY'
import json
import sys
from pathlib import Path

marker, reason, report_path, output_dir, event_id, source_event_id = sys.argv[1:7]
payload = {
    "result_marker": marker,
    "reason": reason,
    "output_dir": output_dir,
    "event_id": event_id,
    "source_event_id": source_event_id,
}
Path(report_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
PY
  exit 2
}

if [[ ! -f "$C2_8_RESPONSE" ]]; then
  fail "FAIL_C2_10_RUNTIME_VIEWER_BLOCKED" "c2_8_response_missing"
fi
if [[ ! -d "$C2_6R_BUNDLE" ]]; then
  fail "FAIL_C2_10_RUNTIME_VIEWER_BLOCKED" "c2_6r_bundle_missing"
fi
if [[ ! -d "$C2_6R_AUDIT" ]]; then
  fail "FAIL_C2_10_RUNTIME_VIEWER_BLOCKED" "c2_6r_audit_missing"
fi

TEMP_API_FLAG="--start-temp-api"
if [[ "$START_TEMP_API" != "true" && "$START_TEMP_API" != "1" ]]; then
  TEMP_API_FLAG="--no-start-temp-api"
fi

set +e
BUILDER_OUTPUT="$(python "$ROOT_DIR/scripts/tools/build_c2_10_runtime_api_viewer_report.py" \
  --output-dir "$OUTPUT_DIR" \
  --event-id "$EVENT_ID" \
  --source-event-id "$SOURCE_EVENT_ID" \
  --c2-8-response "$C2_8_RESPONSE" \
  --c2-6r-bundle "$C2_6R_BUNDLE" \
  --c2-6r-audit "$C2_6R_AUDIT" \
  --database-url "$DATABASE_URL" \
  --api-base-url "$API_BASE_URL" \
  --viewer-base-url "$VIEWER_BASE_URL" \
  "$TEMP_API_FLAG" \
  --overwrite 2>&1)"
BUILDER_STATUS=$?
set -e
if [[ "$BUILDER_STATUS" -ne 0 ]]; then
  echo "$BUILDER_OUTPUT" >&2
  fail "FAIL_C2_10_RUNTIME_VIEWER_BLOCKED" "runtime_viewer_builder_failed"
fi
echo "$BUILDER_OUTPUT"

python - "$OUTPUT_DIR" "$REPORT_PATH" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
report_path = Path(sys.argv[2])
summary_path = output_dir / "c2_10_runtime_viewer_summary.json"
unsafe_scan_path = output_dir / "unsafe_payload_scan.json"
report_html_path = output_dir / "operator_watchlist_evidence.html"

summary = json.loads(summary_path.read_text(encoding="utf-8"))
unsafe_scan = json.loads(unsafe_scan_path.read_text(encoding="utf-8"))
report_html = report_html_path.read_text(encoding="utf-8")

errors = []
if summary.get("result_marker") not in {
    "PASS_C2_10_RUNTIME_API_VIEWER_READY",
    "PARTIAL_C2_10_LIVE_API_READY_VIEWER_STATIC_READY",
    "PARTIAL_C2_10_STATIC_REPORT_ONLY_READY",
}:
    errors.append("unexpected_result_marker")
for expected in (
    "Watchlist Hit",
    "known_face",
    "test:c2_4:person",
    "face:c2_post_savant_fps_probe:4:17854:1",
    "stable_post_savant_sink_time_crop",
    "Event-style Replay is not passed",
):
    if expected not in report_html:
        errors.append(f"report_missing_{expected}")
if summary.get("event_type") != "watchlist_hit":
    errors.append("event_type_not_watchlist_hit")
if summary.get("person_id") != 4:
    errors.append("person_id_invalid")
if summary.get("external_person_id") != "test:c2_4:person":
    errors.append("external_person_id_invalid")
if summary.get("source_observation_id") != "face:c2_post_savant_fps_probe:4:17854:1":
    errors.append("source_observation_id_invalid")
if summary.get("watchlist_rule_id") != "c2_6r_test_watchlist_rule":
    errors.append("watchlist_rule_id_invalid")
if float(summary.get("similarity") or -1) < 0.99:
    errors.append("similarity_invalid")
if float(summary.get("threshold") or -1) != 0.99:
    errors.append("threshold_invalid")
if not summary.get("evidence_bundle_path") or not Path(summary["evidence_bundle_path"]).is_dir():
    errors.append("evidence_bundle_missing")
for key in ("raw_clip_path", "sidecar_path", "watchlist_event_path", "summary_path"):
    value = summary.get(key)
    if not value or not Path(value).is_file():
        errors.append(f"file_missing_{key}")
if summary.get("production_ready") is not True:
    errors.append("production_ready_not_true")
if summary.get("video_integrity_status") != "pass":
    errors.append("video_integrity_not_pass")
if summary.get("evidence_capture_mode") != "stable_post_savant_sink_time_crop":
    errors.append("capture_mode_invalid")
if summary.get("workaround_used") is not True:
    errors.append("workaround_not_true")
if summary.get("event_style_replay_job_passed") is not False:
    errors.append("replay_status_not_false")
if unsafe_scan.get("payload_has_embedding") is not False or summary.get("payload_has_embedding") is not False:
    errors.append("unsafe_embedding_present")
if unsafe_scan.get("payload_has_image_bytes") is not False or summary.get("payload_has_image_bytes") is not False:
    errors.append("unsafe_image_bytes_present")
if unsafe_scan.get("forbidden_key_paths") or summary.get("forbidden_key_paths"):
    errors.append("unsafe_forbidden_key_paths_present")
if summary.get("containers_restarted") is not False:
    errors.append("containers_restarted_should_be_false")
if summary.get("event_style_replay_claimed_passed") is not False:
    errors.append("event_style_replay_claimed_passed")

marker = summary.get("result_marker")
if errors:
    marker = "FAIL_C2_10_RUNTIME_VIEWER_BLOCKED"

report = {
    "result_marker": marker,
    "errors": errors,
    "output_dir": str(output_dir),
    "operator_report_path": str(report_html_path),
    "c2_10_runtime_viewer_summary_json": str(summary_path),
    "unsafe_payload_scan_json": str(unsafe_scan_path),
    "live_api_http_verified": summary.get("live_api_http_verified"),
    "live_api_base_url": summary.get("live_api_base_url"),
    "live_api_status_code_by_event_id": summary.get("live_api_status_code_by_event_id"),
    "live_api_status_code_by_source_event_id": summary.get("live_api_status_code_by_source_event_id"),
    "live_api_response_by_event_id_json": summary.get("live_api_response_by_event_id_json"),
    "live_api_response_by_source_event_id_json": summary.get("live_api_response_by_source_event_id_json"),
    "evidence_viewer_verified": summary.get("evidence_viewer_verified"),
    "viewer_base_url": summary.get("viewer_base_url"),
    "viewer_health_status_code": summary.get("viewer_health_status_code"),
    "viewer_manifest_status_code": summary.get("viewer_manifest_status_code"),
    "viewer_annotations_status_code": summary.get("viewer_annotations_status_code"),
    "viewer_manifest_url": summary.get("viewer_manifest_url"),
    "viewer_annotations_url": summary.get("viewer_annotations_url"),
    "event_type": summary.get("event_type"),
    "source_observation_id": summary.get("source_observation_id"),
    "person_id": summary.get("person_id"),
    "external_person_id": summary.get("external_person_id"),
    "watchlist_rule_id": summary.get("watchlist_rule_id"),
    "similarity": summary.get("similarity"),
    "threshold": summary.get("threshold"),
    "evidence_bundle_path": summary.get("evidence_bundle_path"),
    "payload_has_embedding": summary.get("payload_has_embedding"),
    "payload_has_image_bytes": summary.get("payload_has_image_bytes"),
    "evidence_capture_mode": summary.get("evidence_capture_mode"),
    "workaround_used": summary.get("workaround_used"),
    "event_style_replay_job_passed": summary.get("event_style_replay_job_passed"),
    "containers_restarted": summary.get("containers_restarted"),
    "temporary_api_started": summary.get("temporary_api_started"),
    "temporary_api_stopped": summary.get("temporary_api_stopped"),
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2, sort_keys=True))
if errors:
    raise SystemExit(2)
PY
