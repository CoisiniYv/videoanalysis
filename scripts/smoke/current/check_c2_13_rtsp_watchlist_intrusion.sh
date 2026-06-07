#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

C2_13_EVIDENCE_ROOT="${C2_13_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
C2_13_RUN_ID="${C2_13_RUN_ID:-c2_13_rtsp_watchlist_intrusion_$(date +%Y%m%dT%H%M%S)}"
C2_13_OUTPUT_DIR="${C2_13_OUTPUT_DIR:-$C2_13_EVIDENCE_ROOT/$C2_13_RUN_ID}"
C2_13_RUNTIME_SECONDS="${C2_13_RUNTIME_SECONDS:-180}"
C2_13_MAX_MESSAGES="${C2_13_MAX_MESSAGES:-200}"
C2_13_WATCHLIST_THRESHOLD="${C2_13_WATCHLIST_THRESHOLD:-0.65}"
C2_13_TOP_K="${C2_13_TOP_K:-20}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
C2_13_REDIS_URL="${C2_13_REDIS_URL:-redis://127.0.0.1:6395/0}"

python "$ROOT_DIR/scripts/tools/run_c2_13_rtsp_watchlist_intrusion_probe.py" \
  --output-dir "$C2_13_OUTPUT_DIR" \
  --database-url "$DATABASE_URL" \
  --redis-url "$C2_13_REDIS_URL" \
  --runtime-seconds "$C2_13_RUNTIME_SECONDS" \
  --max-messages "$C2_13_MAX_MESSAGES" \
  --threshold "$C2_13_WATCHLIST_THRESHOLD" \
  --top-k "$C2_13_TOP_K" \
  --overwrite

python - "$C2_13_OUTPUT_DIR" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
summary_path = output_dir / "decision_summary.json"
summary = json.loads(summary_path.read_text(encoding="utf-8"))
marker = summary.get("result_marker")
accepted = {
    "PASS_C2_13_RTSP_WATCHLIST_INTRUSION_READY",
    "PARTIAL_C2_13_RTSP_WATCHLIST_READY_INTRUSION_NO_TRIGGER",
    "PARTIAL_C2_13_RTSP_INTRUSION_READY_WATCHLIST_NO_MATCH",
    "PARTIAL_C2_13_RTSP_ALGORITHMS_NO_TRIGGER_OR_MATCH",
    "PARTIAL_C2_13_NOT_REAL_RTSP_INPUT",
    "PARTIAL_C2_13_RTSP_CONFIG_MISSING",
}
errors = []
if marker not in accepted:
    errors.append(f"marker:{marker}")
if summary.get("payload_has_embedding") is not False:
    errors.append("payload_has_embedding")
if summary.get("payload_has_image_bytes") is not False:
    errors.append("payload_has_image_bytes")
if summary.get("event_style_replay_job_passed") is not False:
    errors.append("event_style_replay_job_passed")
if summary.get("event_style_replay_claimed") is not False:
    errors.append("event_style_replay_claimed")

result = {
    "result_marker": marker if not errors else "FAIL_C2_13_RTSP_DUAL_ALGORITHM_BLOCKED",
    "errors": errors,
    "output_dir": str(output_dir),
    "summary": str(summary_path),
    "input_type": summary.get("input_type"),
    "source_id": summary.get("source_id"),
    "camera_id": summary.get("camera_id"),
    "rtsp_url_redacted": summary.get("rtsp_url_redacted"),
    "watchlist_result": summary.get("watchlist_result"),
    "intrusion_result": summary.get("intrusion_result"),
    "watchlist_hit_count": summary.get("watchlist_hit_count"),
    "intrusion_event_count": summary.get("intrusion_event_count"),
    "visual_evidence_status": summary.get("visual_evidence_status"),
}
print(json.dumps(result, indent=2, sort_keys=True, default=str))
if errors:
    raise SystemExit(2)
PY
