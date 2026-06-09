#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

C2_13V_EVIDENCE_ROOT="${C2_13V_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
C2_13V_RUN_ID="${C2_13V_RUN_ID:-c2_13v_rtsp_video_retention_audit_$(date +%Y%m%dT%H%M%S)}"
C2_13V_OUTPUT_DIR="${C2_13V_OUTPUT_DIR:-$C2_13V_EVIDENCE_ROOT/$C2_13V_RUN_ID}"
C2_13V_DISK_SAMPLE_SECONDS="${C2_13V_DISK_SAMPLE_SECONDS:-300}"
C2_13V_DISK_SAMPLE_INTERVAL_SECONDS="${C2_13V_DISK_SAMPLE_INTERVAL_SECONDS:-30}"
C2_13V_EVENT_CAPTURE_SECONDS="${C2_13V_EVENT_CAPTURE_SECONDS:-120}"
C2_13V_MAX_MESSAGES="${C2_13V_MAX_MESSAGES:-500}"
C2_13V_REPLAY_WAIT_SECONDS="${C2_13V_REPLAY_WAIT_SECONDS:-75}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
C2_13V_REDIS_URL="${C2_13V_REDIS_URL:-redis://127.0.0.1:6395/0}"

python "$ROOT_DIR/scripts/tools/run_c2_13v_rtsp_video_retention_audit.py" \
  --output-dir "$C2_13V_OUTPUT_DIR" \
  --database-url "$DATABASE_URL" \
  --redis-url "$C2_13V_REDIS_URL" \
  --disk-sample-seconds "$C2_13V_DISK_SAMPLE_SECONDS" \
  --disk-sample-interval-seconds "$C2_13V_DISK_SAMPLE_INTERVAL_SECONDS" \
  --event-capture-seconds "$C2_13V_EVENT_CAPTURE_SECONDS" \
  --max-messages "$C2_13V_MAX_MESSAGES" \
  --replay-wait-seconds "$C2_13V_REPLAY_WAIT_SECONDS" \
  --overwrite

python - "$C2_13V_OUTPUT_DIR" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
summary_path = output_dir / "decision_summary.json"
summary = json.loads(summary_path.read_text(encoding="utf-8"))
marker = summary.get("result_marker")
accepted = {
    "PASS_C2_13V_RTSP_VIDEO_EVIDENCE_RETENTION_READY",
    "PARTIAL_C2_13V_EVENTS_READY_VIDEO_EVIDENCE_GAP",
    "PARTIAL_C2_13V_RETENTION_POLICY_UNKNOWN",
    "PARTIAL_C2_13V_NO_EVENTS_IN_WINDOW",
}
errors = []
if marker not in accepted:
    errors.append(f"marker:{marker}")
if summary.get("input_type") != "rtsp":
    errors.append("input_type_not_rtsp")
if summary.get("payload_has_embedding") is not False:
    errors.append("payload_has_embedding")
if summary.get("payload_has_image_bytes") is not False:
    errors.append("payload_has_image_bytes")

result = {
    "result_marker": marker if not errors else "FAIL_C2_13V_RTSP_VIDEO_RETENTION_BLOCKED",
    "errors": errors,
    "input_type": summary.get("input_type"),
    "source_id": summary.get("source_id"),
    "retention_policy_status": summary.get("retention_policy_status"),
    "disk_growth_delta": summary.get("disk_growth_delta"),
    "events_captured": summary.get("events_captured"),
    "evidence_generated_count": summary.get("evidence_generated_count"),
    "replay_video_status": summary.get("replay_video_status"),
    "overall_marker": marker,
    "output_dir": str(output_dir),
}
print(json.dumps(result, indent=2, sort_keys=True))
if errors:
    raise SystemExit(2)
PY
