#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
EXPECTED_ROOT="/home/user/video-analytics"
BUNDLE="${C2_14C_BUNDLE_PATH:-/data/video-analytics/media/evidence/c2_14b_rtsp_event_clip_20260608T041137}"

if [[ "$ROOT" != "$EXPECTED_ROOT" ]]; then
  echo "repo_root=$ROOT"
  echo "expected_root=$EXPECTED_ROOT"
  echo "overall_marker=FAIL_C2_14C_WRONG_WORKTREE"
  exit 2
fi

cd "$ROOT"

python -m pytest harness/tests/test_c2_14c_rtsp_evidence_acceptance.py -q
python -m py_compile scripts/tools/audit_c2_14c_rtsp_evidence_bundle.py

AUDIT_JSON="$(mktemp /tmp/c2_14c_rtsp_evidence_audit_XXXXXX.json)"
trap 'rm -f "$AUDIT_JSON"' EXIT

python scripts/tools/audit_c2_14c_rtsp_evidence_bundle.py \
  --bundle "$BUNDLE" \
  --json-output "$AUDIT_JSON" >/dev/null

python - "$AUDIT_JSON" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
result = json.loads(path.read_text(encoding="utf-8"))
retention = result.get("retention_fixture") or {}
operator = result.get("operator_reports") or {}
unsafe = result.get("unsafe_payload_scan") or {}
coverage = result.get("event_coverage") or {}

print(f"bundle_path={result.get('bundle_path')}")
print(f"event_type={result.get('event_type')}")
print(f"source_id={result.get('source_id')}")
print(f"source_event_id={result.get('source_event_id')}")
print(f"decoded_frame_count={result.get('decoded_frame_count')}")
print(f"sidecar_frame_count={result.get('sidecar_frame_count')}")
print(f"duration_s={result.get('duration_s')}")
print(f"event_frame_pts={result.get('event_frame_pts')}")
print(f"event_ts_ms={result.get('event_ts_ms')}")
print(f"event_coverage_passed={coverage.get('passed')}")
print(f"unsafe_payload_scan_passed={unsafe.get('passed')}")
print(f"retention_fixture_marker={retention.get('result_marker')}")
print(f"retention_deleted_count={retention.get('deleted_count')}")
print(f"operator_summary_references_all={operator.get('summary_report_references_all')}")
print(f"operator_html_reference_gap={operator.get('operator_html_reference_gap')}")
print(f"live_evidence_viewer={operator.get('live_evidence_viewer')}")
print(f"overall_marker={result.get('result_marker')}")

marker = str(result.get("result_marker") or "")
if marker.startswith("FAIL_"):
    sys.exit(2)
if marker.startswith("PARTIAL_"):
    sys.exit(1)
PY
