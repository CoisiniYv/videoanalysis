#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

C2_12D_EVIDENCE_ROOT="${C2_12D_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
C2_12D_RUN_ID="${C2_12D_RUN_ID:-c2_12d_finch_join_gap_$(date +%Y%m%dT%H%M%S)}"
C2_12D_OUTPUT_DIR="${C2_12D_OUTPUT_DIR:-$C2_12D_EVIDENCE_ROOT/$C2_12D_RUN_ID}"
C2_12C_OUTPUT_DIR="${C2_12C_OUTPUT_DIR:-/data/video-analytics/media/evidence/c2_12c_runtime_capture_search_20260608T011352}"
C2_12D_SINK_ROOT="${C2_12D_SINK_ROOT:-/data/video-analytics/media/c2-post-savant-replay-fps-probe}"
C2_12D_THRESHOLD="${C2_12D_THRESHOLD:-0.65}"

python "$ROOT_DIR/scripts/tools/build_c2_12d_runtime_match_visual_evidence.py" \
  --c2-12c-output-dir "$C2_12C_OUTPUT_DIR" \
  --sink-root "$C2_12D_SINK_ROOT" \
  --output-dir "$C2_12D_OUTPUT_DIR" \
  --threshold "$C2_12D_THRESHOLD" \
  --overwrite

python - "$C2_12D_OUTPUT_DIR" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
summary_path = output_dir / "summary.json"
summary = json.loads(summary_path.read_text(encoding="utf-8"))
marker = summary.get("result_marker")
accepted = {
    "PASS_C2_12D_FINCH_RUNTIME_VISUAL_EVIDENCE_READY",
    "PARTIAL_C2_12D_RUNTIME_MATCH_NO_VIDEO_SINK_COVERAGE",
    "PARTIAL_C2_12D_MATCH_FOUND_SIDECAR_JOIN_GAP",
    "PARTIAL_C2_12D_MATCH_GEOMETRY_MISSING",
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
if summary.get("fallback_used") is not False:
    errors.append("fallback_used")
if summary.get("legacy_used_for_visual_binding") is not False:
    errors.append("legacy_used_for_visual_binding")
if summary.get("db_window_fallback_used") is not False:
    errors.append("db_window_fallback_used")

result = {
    "result_marker": marker if not errors else "FAIL_C2_12D_RUNTIME_VISUAL_EVIDENCE_BLOCKED",
    "errors": errors,
    "output_dir": str(output_dir),
    "summary": str(summary_path),
    "matched_finch": summary.get("matched_finch") or {
        "external_person_id": summary.get("external_person_id"),
        "source_observation_id": summary.get("source_observation_id"),
        "similarity": summary.get("similarity"),
    },
    "sink_outputs_inspected": summary.get("sink_outputs_inspected"),
    "sink_outputs_covering_match": summary.get("sink_outputs_covering_match"),
    "missing_link": summary.get("missing_link"),
    "decision_reason": summary.get("decision_reason"),
}
print(json.dumps(result, indent=2, sort_keys=True, default=str))
if errors:
    raise SystemExit(2)
PY
