#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

SOURCE_EVENT_ID="${C2_8_SOURCE_EVENT_ID:-c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:c2_7_event_worker_persistence_20260607T223424}"
EVIDENCE_ROOT="${C2_8_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
RUN_ID="${C2_8_RUN_ID:-c2_8_api_evidence_detail_$(date +%Y%m%dT%H%M%S)}"
OUTPUT_DIR="${C2_8_OUTPUT_DIR:-$EVIDENCE_ROOT/$RUN_ID}"
REPORT_PATH="${C2_8_REPORT_PATH:-/tmp/${RUN_ID}-report.json}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"

fail() {
  local marker="$1"
  local reason="$2"
  python - "$marker" "$reason" "$REPORT_PATH" "$SOURCE_EVENT_ID" "$OUTPUT_DIR" <<'PY'
import json
import sys
from pathlib import Path

marker, reason, report_path, source_event_id, output_dir = sys.argv[1:6]
payload = {
    "result_marker": marker,
    "reason": reason,
    "source_event_id": source_event_id,
    "output_dir": output_dir,
}
Path(report_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
PY
  exit 2
}

set +e
BUILDER_OUTPUT="$(python "$ROOT_DIR/scripts/tools/build_c2_8_api_evidence_detail.py" \
  --source-event-id "$SOURCE_EVENT_ID" \
  --output-dir "$OUTPUT_DIR" \
  --database-url "$DATABASE_URL" \
  --overwrite 2>&1)"
BUILDER_STATUS=$?
set -e
if [[ "$BUILDER_STATUS" -ne 0 ]]; then
  echo "$BUILDER_OUTPUT" >&2
  fail "FAIL_C2_8_API_EVIDENCE_DETAIL_BLOCKED" "api_evidence_detail_builder_failed"
fi
echo "$BUILDER_OUTPUT"

python - "$OUTPUT_DIR" "$REPORT_PATH" "$SOURCE_EVENT_ID" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
report_path = Path(sys.argv[2])
source_event_id = sys.argv[3]

summary_path = output_dir / "c2_8_api_evidence_detail_summary.json"
api_response_path = output_dir / "api_evidence_detail_response.json"
repository_response_path = output_dir / "repository_evidence_detail_response.json"
unsafe_scan_path = output_dir / "unsafe_payload_scan.json"

summary = json.loads(summary_path.read_text(encoding="utf-8"))
api_response = json.loads(api_response_path.read_text(encoding="utf-8"))
repository_response = json.loads(repository_response_path.read_text(encoding="utf-8"))
unsafe_scan = json.loads(unsafe_scan_path.read_text(encoding="utf-8"))
detail = (((api_response.get("json") or {}).get("data") or {}).get("evidence_detail") or {})
repo_detail = ((repository_response.get("data") or {}).get("evidence_detail") or {})
evidence = detail.get("evidence") if isinstance(detail.get("evidence"), dict) else {}
person = detail.get("person") if isinstance(detail.get("person"), dict) else {}
watchlist = detail.get("watchlist") if isinstance(detail.get("watchlist"), dict) else {}

errors = []
if summary.get("result_marker") != "PASS_C2_8_API_EVIDENCE_DETAIL_READY":
    errors.append("unexpected_result_marker")
if summary.get("source_event_id") != source_event_id:
    errors.append("source_event_id_mismatch")
if summary.get("api_route_verified") is not True or summary.get("testclient_verified") is not True:
    errors.append("api_testclient_not_verified")
if summary.get("repository_verified") is not True:
    errors.append("repository_not_verified")
if summary.get("live_http_verified") is not False:
    errors.append("live_http_should_not_be_claimed")
if detail.get("event_type") != "watchlist_hit":
    errors.append("detail_event_type_not_watchlist_hit")
if detail.get("source_observation_id") != "face:c2_post_savant_fps_probe:4:17854:1":
    errors.append("source_observation_id_missing")
if person.get("person_id") != 4 or person.get("external_person_id") != "test:c2_4:person":
    errors.append("person_fields_invalid")
if watchlist.get("watchlist_rule_id") != "c2_6r_test_watchlist_rule":
    errors.append("watchlist_rule_id_invalid")
if watchlist.get("gallery_embedding_id") != 3 or watchlist.get("match_result_id") != 6:
    errors.append("match_linkage_invalid")
if float(watchlist.get("similarity") or -1) < 0.99:
    errors.append("similarity_invalid")
if float(watchlist.get("threshold") or -1) != 0.99:
    errors.append("threshold_invalid")
if not evidence.get("bundle_path") or not Path(evidence["bundle_path"]).is_dir():
    errors.append("evidence_bundle_missing")
for key in ("raw_clip_path", "summary_path", "sidecar_path", "watchlist_event_path"):
    value = evidence.get(key)
    if not value or not Path(value).is_file():
        errors.append(f"evidence_file_missing_{key}")
if evidence.get("video_integrity_status") != "pass":
    errors.append("video_integrity_not_pass")
if evidence.get("production_ready") is not True:
    errors.append("production_ready_not_true")
if int(evidence.get("known_face_count") or 0) <= 0:
    errors.append("known_face_count_missing")
if int(evidence.get("watchlist_hit_count") or 0) != 1:
    errors.append("watchlist_hit_count_not_1")
if evidence.get("capture_mode") != "stable_post_savant_sink_time_crop":
    errors.append("capture_mode_invalid")
if evidence.get("workaround_used") is not True:
    errors.append("workaround_used_not_true")
if evidence.get("event_style_replay_job_passed") is not False:
    errors.append("event_style_replay_job_passed_not_false")
if unsafe_scan.get("payload_has_embedding") is not False:
    errors.append("unsafe_embedding_present")
if unsafe_scan.get("payload_has_image_bytes") is not False:
    errors.append("unsafe_image_bytes_present")
if unsafe_scan.get("forbidden_key_paths"):
    errors.append("unsafe_forbidden_key_paths_present")
if detail.get("unsafe_payload_scan") != unsafe_scan:
    errors.append("detail_unsafe_scan_mismatch")
if repo_detail.get("source_event_id") != detail.get("source_event_id"):
    errors.append("repository_detail_mismatch")

marker = "PASS_C2_8_API_EVIDENCE_DETAIL_READY"
if errors:
    marker = "FAIL_C2_8_API_EVIDENCE_DETAIL_BLOCKED"

report = {
    "result_marker": marker,
    "errors": errors,
    "output_dir": str(output_dir),
    "api_evidence_detail_response_json": str(api_response_path),
    "repository_evidence_detail_response_json": str(repository_response_path),
    "unsafe_payload_scan_json": str(unsafe_scan_path),
    "c2_8_api_evidence_detail_summary_json": str(summary_path),
    "db_event_id": summary.get("db_event_id"),
    "source_event_id": summary.get("source_event_id"),
    "event_type": summary.get("event_type"),
    "source_observation_id": summary.get("source_observation_id"),
    "person_id": summary.get("person_id"),
    "external_person_id": summary.get("external_person_id"),
    "watchlist_rule_id": summary.get("watchlist_rule_id"),
    "gallery_embedding_id": summary.get("gallery_embedding_id"),
    "match_result_id": summary.get("match_result_id"),
    "similarity": summary.get("similarity"),
    "threshold": summary.get("threshold"),
    "evidence_bundle_path": summary.get("evidence_bundle_path"),
    "raw_clip_path": summary.get("raw_clip_path"),
    "summary_path": summary.get("summary_path"),
    "sidecar_path": summary.get("sidecar_path"),
    "watchlist_event_path": summary.get("watchlist_event_path"),
    "production_ready": summary.get("production_ready"),
    "video_integrity_status": summary.get("video_integrity_status"),
    "api_route_verified": summary.get("api_route_verified"),
    "testclient_verified": summary.get("testclient_verified"),
    "repository_verified": summary.get("repository_verified"),
    "live_http_verified": summary.get("live_http_verified"),
    "payload_has_embedding": summary.get("payload_has_embedding"),
    "payload_has_image_bytes": summary.get("payload_has_image_bytes"),
    "evidence_capture_mode": summary.get("evidence_capture_mode"),
    "workaround_used": summary.get("workaround_used"),
    "event_style_replay_job_passed": summary.get("event_style_replay_job_passed"),
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2, sort_keys=True))
if errors:
    raise SystemExit(2)
PY
