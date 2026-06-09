#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

C2_13P_EVIDENCE_ROOT="${C2_13P_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
C2_13P_RUN_ID="${C2_13P_RUN_ID:-c2_13p_rtsp_pose_intrusion_$(date +%Y%m%dT%H%M%S)}"
C2_13P_OUTPUT_DIR="${C2_13P_OUTPUT_DIR:-$C2_13P_EVIDENCE_ROOT/$C2_13P_RUN_ID}"
C2_13P_RUNTIME_SECONDS="${C2_13P_RUNTIME_SECONDS:-120}"
C2_13P_MAX_MESSAGES="${C2_13P_MAX_MESSAGES:-300}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
C2_13P_REDIS_URL="${C2_13P_REDIS_URL:-redis://127.0.0.1:6395/0}"

python "$ROOT_DIR/scripts/tools/run_c2_13p_rtsp_pose_intrusion_probe.py" \
  --output-dir "$C2_13P_OUTPUT_DIR" \
  --database-url "$DATABASE_URL" \
  --redis-url "$C2_13P_REDIS_URL" \
  --runtime-seconds "$C2_13P_RUNTIME_SECONDS" \
  --max-messages "$C2_13P_MAX_MESSAGES" \
  --overwrite

python - "$C2_13P_OUTPUT_DIR" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
summary_path = output_dir / "decision_summary.json"
summary = json.loads(summary_path.read_text(encoding="utf-8"))
marker = summary.get("result_marker")
accepted = {
    "PASS_C2_13P_RTSP_INTRUSION_READY",
    "PARTIAL_C2_13P_POSE_METADATA_PRESENT_EXPORT_GAP",
    "PARTIAL_C2_13P_POSE_MODEL_NOT_ACTIVE",
    "PARTIAL_C2_13P_NO_PERSON_IN_SCENE",
    "PARTIAL_C2_13P_EVENT_WORKER_INTRUSION_PERSISTENCE_GAP",
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
if summary.get("event_style_replay_job_passed") is not False:
    errors.append("event_style_replay_job_passed")
if summary.get("event_style_replay_claimed") is not False:
    errors.append("event_style_replay_claimed")

result = {
    "result_marker": marker if not errors else "FAIL_C2_13P_RTSP_INTRUSION_BLOCKED",
    "errors": errors,
    "output_dir": str(output_dir),
    "summary": str(summary_path),
    "input_type": summary.get("input_type"),
    "source_id": summary.get("source_id"),
    "pose_model_present": summary.get("pose_model_present"),
    "person_objects_in_metadata": summary.get("person_objects_in_metadata"),
    "security.person_observations_before": summary.get("security_person_observations_before", {}).get("length"),
    "security.person_observations_after": summary.get("security_person_observations_after", {}).get("length"),
    "intrusion_event_count": summary.get("intrusion_event_count"),
    "overall_marker": marker,
}
print(json.dumps(result, indent=2, sort_keys=True, default=str))
if errors:
    raise SystemExit(2)
PY
