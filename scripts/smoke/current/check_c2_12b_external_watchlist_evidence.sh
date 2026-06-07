#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

C2_12B_EVIDENCE_ROOT="${C2_12B_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
C2_12B_RUN_ID="${C2_12B_RUN_ID:-c2_12b_external_watchlist_search_$(date +%Y%m%dT%H%M%S)}"
C2_12B_OUTPUT_DIR="${C2_12B_OUTPUT_DIR:-$C2_12B_EVIDENCE_ROOT/$C2_12B_RUN_ID}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
C2_12B_STABLE_BUNDLE="${C2_12B_STABLE_BUNDLE:-/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035}"
C2_12B_THRESHOLD="${C2_12B_THRESHOLD:-0.65}"
C2_12B_TOP_K="${C2_12B_TOP_K:-10}"

python "$ROOT_DIR/scripts/tools/build_c2_12b_external_watchlist_evidence.py" \
  --output-dir "$C2_12B_OUTPUT_DIR" \
  --database-url "$DATABASE_URL" \
  --stable-bundle "$C2_12B_STABLE_BUNDLE" \
  --threshold "$C2_12B_THRESHOLD" \
  --top-k "$C2_12B_TOP_K" \
  --overwrite

python - "$C2_12B_OUTPUT_DIR" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
summary_candidates = [
    output_dir / "c2_12b_external_watchlist_summary.json",
    output_dir / "c2_12b_search_summary.json",
]
summary_path = next((path for path in summary_candidates if path.is_file()), None)
if summary_path is None:
    print(json.dumps({"result_marker": "FAIL_C2_12B_EXTERNAL_WATCHLIST_BLOCKED", "errors": ["summary_missing"]}, indent=2))
    raise SystemExit(2)

summary = json.loads(summary_path.read_text(encoding="utf-8"))
marker = summary.get("result_marker")
accepted = {
    "PASS_C2_12B_EXTERNAL_PERSON_WATCHLIST_EVIDENCE_READY",
    "PARTIAL_C2_12B_MATCH_FOUND_EVIDENCE_JOIN_GAP",
    "PARTIAL_C2_12B_NO_VIDEO_MATCH_FOUND",
    "PARTIAL_C2_12B_NO_VIDEO_OBSERVATIONS_AVAILABLE",
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
if summary.get("gallery_self_match_used_for_pass") is not False:
    errors.append("gallery_self_match_used_for_pass")
if summary.get("test_c2_4_person_used") is not False:
    errors.append("test_c2_4_person_used")
if summary.get("event_style_replay_job_passed") is not False:
    errors.append("event_style_replay_job_passed")

result = {
    "result_marker": marker if not errors else "FAIL_C2_12B_EXTERNAL_WATCHLIST_BLOCKED",
    "errors": errors,
    "output_dir": str(output_dir),
    "summary": str(summary_path),
    "threshold": summary.get("threshold"),
    "decision_reason": summary.get("decision_reason"),
    "best_match": summary.get("best_match"),
    "payload_has_embedding": summary.get("payload_has_embedding"),
    "payload_has_image_bytes": summary.get("payload_has_image_bytes"),
}
print(json.dumps(result, indent=2, default=str, sort_keys=True))
if errors:
    raise SystemExit(2)
PY
