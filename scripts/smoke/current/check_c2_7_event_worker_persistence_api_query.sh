#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

INPUT_EVENT="${C2_7_INPUT_EVENT:-/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035/redis_watchlist_event.json}"
INPUT_BUNDLE="${C2_7_INPUT_BUNDLE:-/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035}"
AUDIT_PATH="${C2_7_AUDIT_PATH:-/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035}"
EVIDENCE_ROOT="${C2_7_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
RUN_ID="${C2_7_RUN_ID:-c2_7_event_worker_persistence_$(date +%Y%m%dT%H%M%S)}"
OUTPUT_DIR="${C2_7_OUTPUT_DIR:-$EVIDENCE_ROOT/$RUN_ID}"
REPORT_PATH="${C2_7_REPORT_PATH:-/tmp/${RUN_ID}-report.json}"

DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
REDIS_URL="${C2_7_REDIS_URL:-redis://127.0.0.1:6395/0}"
INPUT_STREAM="${C2_7_INPUT_STREAM:-c2_7.security.events.test}"
CONSUMER_GROUP="${C2_7_CONSUMER_GROUP:-c2_7_event_worker_test}"
CONSUMER_NAME="${C2_7_CONSUMER_NAME:-c2_7-one-message}"

fail() {
  local marker="$1"
  local reason="$2"
  python - "$marker" "$reason" "$REPORT_PATH" "$INPUT_EVENT" "$INPUT_BUNDLE" "$OUTPUT_DIR" "$INPUT_STREAM" <<'PY'
import json
import sys
from pathlib import Path

marker, reason, report_path, input_event, input_bundle, output_dir, input_stream = sys.argv[1:8]
payload = {
    "result_marker": marker,
    "reason": reason,
    "input_event": input_event,
    "input_bundle": input_bundle,
    "output_dir": output_dir,
    "input_stream": input_stream,
}
Path(report_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
PY
  exit 2
}

if [[ ! -f "$INPUT_EVENT" ]]; then
  fail "FAIL_C2_7_EVENT_PERSISTENCE_BLOCKED" "input_event_missing"
fi
if [[ ! -d "$INPUT_BUNDLE" ]]; then
  fail "FAIL_C2_7_EVENT_PERSISTENCE_BLOCKED" "input_bundle_missing"
fi

set +e
BUILDER_OUTPUT="$(python "$ROOT_DIR/scripts/tools/build_c2_7_persist_watchlist_event.py" \
  --input-event "$INPUT_EVENT" \
  --input-bundle "$INPUT_BUNDLE" \
  --audit-path "$AUDIT_PATH" \
  --output-dir "$OUTPUT_DIR" \
  --database-url "$DATABASE_URL" \
  --redis-url "$REDIS_URL" \
  --input-stream "$INPUT_STREAM" \
  --consumer-group "$CONSUMER_GROUP" \
  --consumer-name "$CONSUMER_NAME" \
  --ensure-event-schema \
  --overwrite 2>&1)"
BUILDER_STATUS=$?
set -e
if [[ "$BUILDER_STATUS" -ne 0 ]]; then
  echo "$BUILDER_OUTPUT" >&2
  if grep -q "PARTIAL_C2_7_EVENT_WORKER_ENTRYPOINT_ONLY" <<<"$BUILDER_OUTPUT"; then
    fail "PARTIAL_C2_7_EVENT_WORKER_ENTRYPOINT_ONLY" "redis_event_stream_unavailable"
  fi
  fail "FAIL_C2_7_EVENT_PERSISTENCE_BLOCKED" "event_worker_persistence_builder_failed"
fi
echo "$BUILDER_OUTPUT"

python - "$OUTPUT_DIR" "$REPORT_PATH" "$INPUT_EVENT" "$INPUT_BUNDLE" <<'PY'
import json
import sys
from pathlib import Path

output_dir, report_path, input_event, input_bundle = map(Path, sys.argv[1:5])
summary = json.loads((output_dir / "c2_7_event_worker_persistence_summary.json").read_text(encoding="utf-8"))
event = json.loads((output_dir / "persisted_watchlist_event.json").read_text(encoding="utf-8"))
db_row = json.loads((output_dir / "db_event_row.json").read_text(encoding="utf-8"))
repo_response = json.loads((output_dir / "repository_response.json").read_text(encoding="utf-8"))
api_response_path = output_dir / "api_response.json"
api_response = json.loads(api_response_path.read_text(encoding="utf-8")) if api_response_path.is_file() else {}

forbidden = {
    "embedding",
    "embeddings",
    "embedding_vector",
    "embedding_values",
    "embedding_list",
    "image_bytes",
    "crop_bytes",
    "frame_bytes",
    "raw_frame",
    "base64",
    "base64_image",
    "image_base64",
}

def find_forbidden(value, path=""):
    hits = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in forbidden:
                hits.append(path + str(key))
            hits.extend(find_forbidden(nested, path + str(key) + "."))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            hits.extend(find_forbidden(nested, f"{path}{index}."))
    elif isinstance(value, str) and (";base64," in value.lower() or value.lower().startswith("data:image")):
        hits.append(path.rstrip(".") or "value")
    return hits

errors = []
if summary.get("result_marker") != "PASS_C2_7_EVENT_WORKER_PERSISTENCE_API_QUERY_READY":
    errors.append("unexpected_result_marker")
if summary.get("execution_mode") != "Redis stream event-worker one-message consumer":
    errors.append("execution_mode_invalid")
if summary.get("redis_event_stream_verified") is not True:
    errors.append("redis_event_stream_not_verified")
if summary.get("event_worker_persistence_verified") is not True:
    errors.append("event_worker_persistence_not_verified")
if summary.get("repository_query_verified") is not True:
    errors.append("repository_query_not_verified")
if summary.get("api_query_verified") is not True:
    errors.append("api_query_not_verified")
if summary.get("idempotency_verified") is not True:
    errors.append("idempotency_not_verified")
if int(summary.get("duplicate_count_after_replay") or 0) != 1:
    errors.append("duplicate_count_not_1")
for key in ("source_event_id", "source_observation_id", "person_id", "gallery_embedding_id", "match_result_id", "watchlist_rule_id"):
    if event.get(key) in (None, ""):
        errors.append(f"event_missing_{key}")
if event.get("event_type") != "watchlist_hit":
    errors.append("event_type_not_watchlist_hit")
payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
if payload.get("evidence_capture_mode") != "stable_post_savant_sink_time_crop":
    errors.append("capture_mode_missing")
if payload.get("workaround_used") is not True:
    errors.append("workaround_not_true")
if payload.get("event_style_replay_job_passed") is not False:
    errors.append("replay_status_not_false")
if payload.get("primary_identity_join_key") != "source_observation_id":
    errors.append("primary_join_key_invalid")
if payload.get("track_id_join_warning") is not True:
    errors.append("track_id_join_warning_missing")
if not (payload.get("evidence") or {}).get("bundle_path"):
    errors.append("evidence_bundle_path_missing")
if find_forbidden(payload) or find_forbidden(db_row.get("payload") or {}):
    errors.append("payload_contains_embedding_or_image_bytes")
if db_row.get("source_event_id") != event.get("source_event_id"):
    errors.append("db_source_event_id_mismatch")
if db_row.get("event_type") != "watchlist_hit":
    errors.append("db_event_type_mismatch")
if (repo_response.get("row") or {}).get("source_event_id") != event.get("source_event_id"):
    errors.append("repository_response_mismatch")
if api_response and (api_response.get("json") or {}).get("data", {}).get("source_event_id") != event.get("source_event_id"):
    errors.append("api_response_mismatch")

marker = "PASS_C2_7_EVENT_WORKER_PERSISTENCE_API_QUERY_READY"
if errors:
    marker = "FAIL_C2_7_EVENT_PERSISTENCE_BLOCKED"

report = {
    "result_marker": marker,
    "errors": errors,
    "input_event": str(input_event),
    "input_bundle": str(input_bundle),
    "output_dir": str(output_dir),
    "persisted_watchlist_event_json": str(output_dir / "persisted_watchlist_event.json"),
    "queried_event_json": str(output_dir / "queried_event.json"),
    "db_event_row_json": str(output_dir / "db_event_row.json"),
    "repository_response_json": str(output_dir / "repository_response.json"),
    "api_response_json": str(api_response_path) if api_response_path.is_file() else "",
    "c2_7_event_worker_persistence_summary_json": str(output_dir / "c2_7_event_worker_persistence_summary.json"),
    "source_event_id": event.get("source_event_id"),
    "event_id": summary.get("event_id"),
    "event_type": event.get("event_type"),
    "status": db_row.get("status"),
    "camera_id": event.get("camera_id"),
    "source_id": event.get("source_id"),
    "source_observation_id": event.get("source_observation_id"),
    "person_id": event.get("person_id"),
    "external_person_id": event.get("external_person_id"),
    "gallery_embedding_id": event.get("gallery_embedding_id"),
    "match_result_id": event.get("match_result_id"),
    "similarity": event.get("similarity"),
    "threshold": event.get("threshold"),
    "watchlist_rule_id": event.get("watchlist_rule_id"),
    "repository_query_verified": summary.get("repository_query_verified"),
    "api_query_verified": summary.get("api_query_verified"),
    "live_api_runtime_verified": summary.get("live_api_runtime_verified"),
    "idempotency_verified": summary.get("idempotency_verified"),
    "duplicate_count_after_replay": summary.get("duplicate_count_after_replay"),
    "payload_has_embedding": summary.get("payload_has_embedding"),
    "payload_has_image_bytes": summary.get("payload_has_image_bytes"),
    "evidence_bundle_path": summary.get("evidence_bundle_path"),
    "evidence_capture_mode": summary.get("evidence_capture_mode"),
    "workaround_used": summary.get("workaround_used"),
    "event_style_replay_job_passed": summary.get("event_style_replay_job_passed"),
    "redis_input_stream": summary.get("redis_input_stream"),
    "redis_input_message_id": summary.get("redis_input_message_id"),
    "redis_duplicate_message_id": summary.get("redis_duplicate_message_id"),
    "redis_cleanup_status": summary.get("redis_cleanup_status"),
}
Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2, sort_keys=True))
if errors:
    raise SystemExit(2)
PY
