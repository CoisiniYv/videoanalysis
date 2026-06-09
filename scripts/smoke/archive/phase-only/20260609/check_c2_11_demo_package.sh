#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

C2_11_EVIDENCE_ROOT="${C2_11_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
C2_11_RUN_ID="${C2_11_RUN_ID:-c2_11_demo_package_$(date +%Y%m%dT%H%M%S)}"
C2_11_OUTPUT_DIR="${C2_11_OUTPUT_DIR:-$C2_11_EVIDENCE_ROOT/$C2_11_RUN_ID}"
C2_10_OUTPUT_DIR="${C2_10_OUTPUT_DIR:-/data/video-analytics/media/evidence/c2_10_runtime_viewer_20260607T230426}"
C2_6R_BUNDLE="${C2_6R_BUNDLE:-/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035}"

python "$ROOT_DIR/scripts/tools/build_c2_11_demo_package.py" \
  --output-dir "$C2_11_OUTPUT_DIR" \
  --c2-10-output-dir "$C2_10_OUTPUT_DIR" \
  --c2-6r-bundle "$C2_6R_BUNDLE" \
  --overwrite

python - "$C2_11_OUTPUT_DIR" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
manifest_path = output_dir / "demo_manifest.json"
index_path = output_dir / "index.html"
unsafe_scan_path = output_dir / "unsafe_payload_scan.json"
checklist_path = output_dir / "demo_checklist.md"

errors = []
for path in (manifest_path, index_path, unsafe_scan_path, checklist_path):
    if not path.is_file():
        errors.append(f"missing:{path}")

manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
unsafe_scan = json.loads(unsafe_scan_path.read_text(encoding="utf-8")) if unsafe_scan_path.is_file() else {}
index_html = index_path.read_text(encoding="utf-8") if index_path.is_file() else ""

event = manifest.get("event") or {}
evidence = manifest.get("evidence") or {}
safety = manifest.get("safety") or {}
limitations = manifest.get("limitations") or []

if manifest.get("package_result_marker") != "PASS_C2_11_DEMO_PACKAGE_READY":
    errors.append("package_result_marker")
if manifest.get("result_marker") != "PASS_C2_REBASELINE_DEMO_READY":
    errors.append("result_marker")
if event.get("event_type") != "watchlist_hit":
    errors.append("event_type")
if event.get("person_id") != 4:
    errors.append("person_id")
if event.get("external_person_id") != "test:c2_4:person":
    errors.append("external_person_id")
if event.get("source_observation_id") != "face:c2_post_savant_fps_probe:4:17854:1":
    errors.append("source_observation_id")
if float(event.get("similarity") or -1) != 1.0:
    errors.append("similarity")
if float(event.get("threshold") or -1) != 0.99:
    errors.append("threshold")
if event.get("watchlist_rule_id") != "c2_6r_test_watchlist_rule":
    errors.append("watchlist_rule_id")
if not evidence.get("bundle_path") or not Path(evidence["bundle_path"]).is_dir():
    errors.append("evidence_bundle_path")
if evidence.get("capture_mode") != "stable_post_savant_sink_time_crop":
    errors.append("capture_mode")
if evidence.get("workaround_used") is not True:
    errors.append("workaround_used")
if evidence.get("event_style_replay_job_passed") is not False:
    errors.append("event_style_replay_job_passed")
if safety.get("embedding_present") is not False:
    errors.append("embedding_present")
if safety.get("image_base64_crop_present") is not False:
    errors.append("image_base64_crop_present")
if safety.get("fallback_used") is not False:
    errors.append("fallback_used")
if safety.get("legacy_used_for_visual_binding") is not False:
    errors.append("legacy_used_for_visual_binding")
for required in (
    "event_style_replay_not_passed",
    "stable_post_savant_sink_time_crop_workaround",
    "deterministic_fixed_sample",
    "no_broad_accuracy_proof",
    "no_long_running_soak",
):
    if required not in limitations:
        errors.append(f"missing_limitation:{required}")
for token in (
    "C2 Watchlist Evidence Demo",
    "watchlist_hit",
    "test:c2_4:person",
    "stable_post_savant_sink_time_crop",
    "Event-style Replay is not passed",
    "deterministic fixed sample",
):
    if token not in index_html:
        errors.append(f"index_missing:{token}")
if unsafe_scan.get("passed") is not True:
    errors.append("unsafe_scan_failed")
if unsafe_scan.get("payload_has_embedding") is not False:
    errors.append("unsafe_embedding_present")
if unsafe_scan.get("payload_has_image_bytes") is not False:
    errors.append("unsafe_image_present")
if unsafe_scan.get("has_suspicious_numeric_vectors") is not False:
    errors.append("unsafe_vector_present")

result = {
    "result_marker": "PASS_C2_11_DEMO_PACKAGE_READY" if not errors else "FAIL_C2_11_DEMO_PACKAGE_BLOCKED",
    "errors": errors,
    "output_dir": str(output_dir),
    "index_html": str(index_path),
    "demo_manifest": str(manifest_path),
    "unsafe_payload_scan": str(unsafe_scan_path),
    "unsafe_payload_scan_passed": unsafe_scan.get("passed"),
    "payload_has_embedding": unsafe_scan.get("payload_has_embedding"),
    "payload_has_image_bytes": unsafe_scan.get("payload_has_image_bytes"),
    "has_suspicious_numeric_vectors": unsafe_scan.get("has_suspicious_numeric_vectors"),
}
print(json.dumps(result, indent=2, sort_keys=True))
if errors:
    raise SystemExit(2)
PY
