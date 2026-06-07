#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

C2_12C_EVIDENCE_ROOT="${C2_12C_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
C2_12C_RUN_ID="${C2_12C_RUN_ID:-c2_12c_runtime_capture_search_$(date +%Y%m%dT%H%M%S)}"
C2_12C_OUTPUT_DIR="${C2_12C_OUTPUT_DIR:-$C2_12C_EVIDENCE_ROOT/$C2_12C_RUN_ID}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
C2_12C_REDIS_URL="${C2_12C_REDIS_URL:-redis://127.0.0.1:6395/0}"
C2_12C_TIMEOUT_SECONDS="${C2_12C_TIMEOUT_SECONDS:-60}"
C2_12C_MAX_REDIS_MESSAGES="${C2_12C_MAX_REDIS_MESSAGES:-100}"
C2_12C_BACKLOG_MESSAGES="${C2_12C_BACKLOG_MESSAGES:-200}"
C2_12C_THRESHOLD="${C2_12C_THRESHOLD:-0.65}"
C2_12C_TOP_K="${C2_12C_TOP_K:-20}"

python "$ROOT_DIR/scripts/tools/probe_c2_12c_runtime_reese_finch_capture.py" \
  --output-dir "$C2_12C_OUTPUT_DIR" \
  --database-url "$DATABASE_URL" \
  --redis-url "$C2_12C_REDIS_URL" \
  --timeout-seconds "$C2_12C_TIMEOUT_SECONDS" \
  --max-redis-messages "$C2_12C_MAX_REDIS_MESSAGES" \
  --backlog-messages "$C2_12C_BACKLOG_MESSAGES" \
  --threshold "$C2_12C_THRESHOLD" \
  --top-k "$C2_12C_TOP_K" \
  --overwrite

python - "$C2_12C_OUTPUT_DIR" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
summary_path = output_dir / "summary.json"
summary = json.loads(summary_path.read_text(encoding="utf-8"))
marker = summary.get("result_marker")
accepted = {
    "PASS_C2_12C_RUNTIME_REESE_FINCH_WATCHLIST_READY",
    "PARTIAL_C2_12C_RUNTIME_CAPTURE_NO_MATCH_IN_WINDOW",
    "PARTIAL_C2_12C_RUNTIME_OBSERVATION_CAPTURE_GAP",
    "PARTIAL_C2_12C_FACE_OBSERVATION_STREAM_EMPTY",
    "PARTIAL_C2_12C_FACE_WORKER_NOT_CONSUMING",
    "PARTIAL_C2_12C_MATCH_FOUND_EVIDENCE_JOIN_GAP",
}
errors = []
if marker not in accepted:
    errors.append(f"marker:{marker}")
if summary.get("payload_has_embedding") is not False:
    errors.append("payload_has_embedding")
if summary.get("payload_has_image_bytes") is not False:
    errors.append("payload_has_image_bytes")
if summary.get("fake_match_used") is not False:
    errors.append("fake_match_used")
if summary.get("gallery_self_match_used") is not False:
    errors.append("gallery_self_match_used")
if summary.get("event_style_replay_job_passed") is not False:
    errors.append("event_style_replay_job_passed")
if summary.get("event_style_replay_claimed") is not False:
    errors.append("event_style_replay_claimed")

result = {
    "result_marker": marker if not errors else "FAIL_C2_12C_RUNTIME_CAPTURE_BLOCKED",
    "errors": errors,
    "output_dir": str(output_dir),
    "summary": str(summary_path),
    "source_id": summary.get("runtime_source_status", {}).get("source_id"),
    "db_before_count": summary.get("runtime_source_status", {}).get("db_before_count"),
    "db_after_count": summary.get("runtime_source_status", {}).get("db_after_count"),
    "new_observations": summary.get("captured_observations_summary", {}).get("db_rows_available"),
    "threshold": summary.get("threshold"),
    "decision": summary.get("decision"),
    "payload_has_embedding": summary.get("payload_has_embedding"),
    "payload_has_image_bytes": summary.get("payload_has_image_bytes"),
}
print(json.dumps(result, indent=2, default=str, sort_keys=True))
if errors:
    raise SystemExit(2)
PY
