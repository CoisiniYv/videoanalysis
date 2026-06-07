#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

INPUT_BUNDLE="${C2_6R_INPUT_BUNDLE:-/data/video-analytics/media/evidence/c2_6_live_watchlist_20260607T215648}"
EVIDENCE_ROOT="${C2_6R_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
AUDIT_ROOT="${C2_6R_AUDIT_ROOT:-/data/video-analytics/media/evidence_audit}"
RUN_ID="${C2_6R_RUN_ID:-c2_6r_redis_watchlist_$(date +%Y%m%dT%H%M%S)}"
OUTPUT_BUNDLE="${C2_6R_OUTPUT_BUNDLE:-$EVIDENCE_ROOT/$RUN_ID}"
AUDIT_DIR="${C2_6R_AUDIT_DIR:-$AUDIT_ROOT/$RUN_ID}"
REPORT_PATH="${C2_6R_REPORT_PATH:-/tmp/${RUN_ID}-report.json}"

DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
REDIS_URL="${C2_6R_REDIS_URL:-redis://127.0.0.1:6395/0}"
SOURCE_OBSERVATION_ID="${C2_6R_SOURCE_OBSERVATION_ID:-face:c2_post_savant_fps_probe:4:17854:1}"
EXTERNAL_PERSON_ID="${C2_6R_EXTERNAL_PERSON_ID:-test:c2_4:person}"
WATCHLIST_RULE_ID="${C2_6R_WATCHLIST_RULE_ID:-c2_6r_test_watchlist_rule}"
WATCHLIST_RULE_NAME="${C2_6R_WATCHLIST_RULE_NAME:-C2.6R Test Watchlist Rule}"
THRESHOLD="${C2_6R_THRESHOLD:-0.99}"
SEARCH_REQUEST_ID="${C2_6R_SEARCH_REQUEST_ID:-c2600000-0000-4000-8000-0000000000a1}"
INPUT_STREAM="${C2_6R_INPUT_STREAM:-c2_6r.face_observations.test}"
OUTPUT_STREAM="${C2_6R_OUTPUT_STREAM:-c2_6r.security.events.test}"

fail() {
  local marker="$1"
  local reason="$2"
  python - "$marker" "$reason" "$REPORT_PATH" "$INPUT_BUNDLE" "$OUTPUT_BUNDLE" "$AUDIT_DIR" "$INPUT_STREAM" "$OUTPUT_STREAM" <<'PY'
import json
import sys
from pathlib import Path

marker, reason, report_path, input_bundle, output_bundle, audit_dir, input_stream, output_stream = sys.argv[1:9]
payload = {
    "result_marker": marker,
    "reason": reason,
    "input_bundle": input_bundle,
    "output_bundle": output_bundle,
    "c2_3q_audit_output_dir": audit_dir,
    "input_stream": input_stream,
    "output_stream": output_stream,
}
Path(report_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
PY
  exit 2
}

if [[ ! -d "$INPUT_BUNDLE" ]]; then
  fail "FAIL_C2_6R_WATCHLIST_CONSUMER_BLOCKED" "input_bundle_missing"
fi

set +e
BUILDER_OUTPUT="$(python "$ROOT_DIR/scripts/tools/build_c2_6r_redis_watchlist_evidence_bundle.py" \
  --input-bundle "$INPUT_BUNDLE" \
  --output-dir "$OUTPUT_BUNDLE" \
  --database-url "$DATABASE_URL" \
  --redis-url "$REDIS_URL" \
  --source-observation-id "$SOURCE_OBSERVATION_ID" \
  --external-person-id "$EXTERNAL_PERSON_ID" \
  --watchlist-rule-id "$WATCHLIST_RULE_ID" \
  --watchlist-rule-name "$WATCHLIST_RULE_NAME" \
  --threshold "$THRESHOLD" \
  --search-request-id "$SEARCH_REQUEST_ID" \
  --input-stream "$INPUT_STREAM" \
  --output-stream "$OUTPUT_STREAM" \
  --overwrite 2>&1)"
BUILDER_STATUS=$?
set -e
if [[ "$BUILDER_STATUS" -ne 0 ]]; then
  echo "$BUILDER_OUTPUT" >&2
  if grep -q "PARTIAL_C2_6R_REDIS_INFRA_GAP" <<<"$BUILDER_OUTPUT"; then
    fail "PARTIAL_C2_6R_REDIS_INFRA_GAP" "redis_infra_gap"
  fi
  fail "FAIL_C2_6R_WATCHLIST_CONSUMER_BLOCKED" "redis_watchlist_builder_failed"
fi
echo "$BUILDER_OUTPUT"

SELECTED_FRAME="$(python - "$OUTPUT_BUNDLE" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads((Path(sys.argv[1]) / "c2_6r_redis_watchlist_summary.json").read_text(encoding="utf-8"))
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
    fail "FAIL_C2_6R_WATCHLIST_CONSUMER_BLOCKED" "c2_3q_audit_failed"
  }

python - "$OUTPUT_BUNDLE" "$AUDIT_DIR" "$REPORT_PATH" "$INPUT_BUNDLE" <<'PY'
import json
import sys
from pathlib import Path

bundle_dir, audit_dir, report_path, input_bundle = map(Path, sys.argv[1:5])
summary = json.loads((bundle_dir / "summary.json").read_text(encoding="utf-8"))
event = json.loads((bundle_dir / "redis_watchlist_event.json").read_text(encoding="utf-8"))
c26r = json.loads((bundle_dir / "c2_6r_redis_watchlist_summary.json").read_text(encoding="utf-8"))
audit = json.loads((audit_dir / "audit_summary.json").read_text(encoding="utf-8"))
rows = [
    json.loads(line)
    for line in (bundle_dir / "annotations.frame_cache.identity.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]

forbidden = {
    "embedding",
    "embeddings",
    "embedding_vector",
    "embedding_list",
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
            if str(key).lower() in forbidden:
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
if c26r.get("result_marker") != "PASS_C2_6R_REDIS_CONSUMER_WATCHLIST_EVENT_READY":
    errors.append("unexpected_c2_6r_marker")
if c26r.get("execution_mode") != "Mode A Redis stream one-message consumer":
    errors.append("execution_mode_not_mode_a")
if event.get("producer") != "face-worker-one-message-consumer":
    errors.append("producer_not_face_worker_one_message_consumer")
for key in ("source_observation_id", "person_id", "gallery_embedding_id", "match_result_id", "similarity", "threshold", "watchlist_rule_id"):
    if event.get(key) in (None, ""):
        errors.append(f"event_missing_{key}")
if find_forbidden(event):
    errors.append("event_payload_contains_embedding_or_image_bytes")
payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
if payload.get("primary_identity_join_key") != "source_observation_id":
    errors.append("primary_identity_join_key_not_source_observation_id")
if payload.get("track_id_join_warning") is not True:
    errors.append("track_id_join_warning_not_true")
if payload.get("identity_source") != "face_worker_pgvector_match":
    errors.append("identity_source_not_face_worker_pgvector_match")
if payload.get("watchlist_match_source") != "face_worker_consumer":
    errors.append("watchlist_match_source_not_consumer")
if target is None:
    errors.append("sidecar_target_missing")
else:
    identity = target.get("identity") if isinstance(target.get("identity"), dict) else {}
    if identity.get("primary_identity_join_key") != "source_observation_id":
        errors.append("sidecar_primary_identity_join_key_missing")
    if identity.get("track_id_join_warning") is not True:
        errors.append("sidecar_track_id_join_warning_missing")
if int(summary.get("watchlist_hit_count") or 0) != 1:
    errors.append("watchlist_hit_count_not_1")
if int(summary.get("known_face_count") or 0) <= 0:
    errors.append("known_face_count_not_positive")
if summary.get("redis_consumer_loop_verified") is not True:
    errors.append("redis_consumer_loop_not_verified")
if summary.get("face_worker_one_message_entrypoint_verified") is not True:
    errors.append("one_message_entrypoint_not_verified")
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

marker = "PASS_C2_6R_REDIS_CONSUMER_WATCHLIST_EVENT_READY"
if errors:
    marker = "FAIL_C2_6R_WATCHLIST_CONSUMER_BLOCKED"

report = {
    "result_marker": marker,
    "errors": errors,
    "input_bundle": str(input_bundle),
    "output_bundle": str(bundle_dir),
    "c2_3q_audit_output_dir": str(audit_dir),
    "audit_result_marker": audit.get("result_marker"),
    "audit_index_html": str(audit_dir / "index.html"),
    "audit_contact_sheet": str(audit_dir / "contact_sheet.jpg"),
    "redis_watchlist_event_json": str(bundle_dir / "redis_watchlist_event.json"),
    "live_watchlist_event_json": str(bundle_dir / "live_watchlist_event.json"),
    "c2_6r_redis_watchlist_summary_json": str(bundle_dir / "c2_6r_redis_watchlist_summary.json"),
    "execution_mode": c26r.get("execution_mode"),
    "input_stream": c26r.get("redis_input_stream"),
    "output_stream": c26r.get("redis_output_stream"),
    "input_message_id": c26r.get("redis_input_message_id"),
    "output_message_id": c26r.get("redis_output_message_id"),
    "cleanup_status": c26r.get("redis_cleanup_status"),
    "source_event_id": event.get("source_event_id"),
    "producer": event.get("producer"),
    "source_observation_id": event.get("source_observation_id"),
    "person_id": event.get("person_id"),
    "external_person_id": event.get("external_person_id"),
    "gallery_embedding_id": event.get("gallery_embedding_id"),
    "match_result_id": event.get("match_result_id"),
    "similarity": event.get("similarity"),
    "threshold": event.get("threshold"),
    "watchlist_rule_id": event.get("watchlist_rule_id"),
    "track_id": event.get("track_id"),
    "track_id_join_warning": summary.get("track_id_join_warning"),
    "db_observation_track_id": summary.get("db_observation_track_id"),
    "evidence_sidecar_track_id": summary.get("evidence_sidecar_track_id"),
    "known_face_count": summary.get("known_face_count"),
    "watchlist_hit_count": summary.get("watchlist_hit_count"),
    "redis_consumer_loop_verified": summary.get("redis_consumer_loop_verified"),
    "one_message_entrypoint_verified": summary.get("face_worker_one_message_entrypoint_verified"),
    "production_ready": summary.get("production_ready"),
    "video_integrity_status": (summary.get("video_integrity") or {}).get("integrity_status"),
    "video_integrity_gate_passed": (summary.get("video_integrity") or {}).get("production_gate_passed"),
    "evidence_capture_mode": summary.get("evidence_capture_mode"),
    "workaround_used": summary.get("workaround_used"),
    "event_style_replay_job_passed": summary.get("event_style_replay_job_passed"),
    "fallback_used": summary.get("fallback_used"),
    "legacy_used_for_visual_binding": summary.get("legacy_used_for_visual_binding"),
}
Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2, sort_keys=True))
raise SystemExit(2 if errors else 0)
PY
