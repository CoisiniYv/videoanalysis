#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
EXPECTED_ROOT="/home/user/video-analytics"
STAMP="$(date -u +%Y%m%dT%H%M%S)"
OUTPUT_DIR="${C2_14D_OUTPUT_DIR:-/data/video-analytics/media/evidence/c2_14d_rtsp_watchlist_known_face_clip_${STAMP}}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@127.0.0.1:5432/video_analytics}"
REDIS_URL="${C2_14D_REDIS_URL:-redis://127.0.0.1:6395/0}"

if [[ "$ROOT" != "$EXPECTED_ROOT" ]]; then
  echo "repo_root=$ROOT"
  echo "expected_root=$EXPECTED_ROOT"
  echo "overall_marker=FAIL_C2_14D_WRONG_WORKTREE"
  exit 2
fi

cd "$ROOT"

python -m pytest harness/tests/test_c2_14d_rtsp_watchlist_known_face_evidence.py -q
python -m py_compile scripts/tools/build_c2_14d_watchlist_clip_from_ring.py
python -m py_compile scripts/tools/audit_c2_14d_watchlist_evidence_bundle.py

python scripts/tools/build_c2_14d_watchlist_clip_from_ring.py \
  --output-dir "$OUTPUT_DIR" \
  --database-url "$DATABASE_URL" \
  --redis-url "$REDIS_URL" \
  >"$OUTPUT_DIR.tool_output.json" || true

SUMMARY_JSON="$OUTPUT_DIR/summary.json"
if [[ ! -f "$SUMMARY_JSON" ]]; then
  echo "bundle_path=$OUTPUT_DIR"
  echo "overall_marker=PARTIAL_C2_14D_NO_WATCHLIST_HIT"
  exit 1
fi

AUDIT_JSON="$(mktemp /tmp/c2_14d_watchlist_audit_XXXXXX.json)"
trap 'rm -f "$AUDIT_JSON" "$OUTPUT_DIR.tool_output.json"' EXIT

python scripts/tools/audit_c2_14d_watchlist_evidence_bundle.py \
  --bundle "$OUTPUT_DIR" \
  --json-output "$AUDIT_JSON" >/dev/null || true

python - "$SUMMARY_JSON" "$AUDIT_JSON" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
audit = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
marker = audit.get("result_marker") or summary.get("result_marker")

print(f"bundle_path={summary.get('bundle_path') or Path(sys.argv[1]).parent}")
print(f"event_type={summary.get('event_type')}")
print(f"source_id={summary.get('source_id')}")
print(f"person_id={summary.get('person_id')}")
print(f"external_person_id={summary.get('external_person_id')}")
print(f"similarity={summary.get('similarity')}")
print(f"threshold={summary.get('threshold')}")
print(f"source_observation_id={summary.get('source_observation_id')}")
print(f"frame_pts={summary.get('frame_pts')}")
print(f"event_ts_ms={summary.get('event_ts_ms')}")
print(f"raw_clip={summary.get('raw_clip')}")
print(f"decoded_frame_count={audit.get('decoded_frame_count') or summary.get('decoded_video_frame_count')}")
print(f"sidecar_frame_count={audit.get('sidecar_frame_count') or summary.get('sidecar_frame_count')}")
print(f"identity_binding_method={summary.get('identity_binding_method')}")
print(f"unsafe_payload_scan_passed={(audit.get('unsafe_payload_scan') or {}).get('passed')}")
print(f"overall_marker={marker}")

marker = str(marker or "")
if marker.startswith("FAIL_"):
    sys.exit(2)
if marker.startswith("PARTIAL_"):
    sys.exit(1)
PY
