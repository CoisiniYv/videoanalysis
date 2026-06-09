#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SOURCE_ID="${C2_14_SOURCE_ID:-c2_post_savant_fps_probe}"
RING_ROOT="${C2_14_RING_ROOT:-/data/video-analytics/media/rtsp-ring}"
SOURCE_DIR="${C2_14_SOURCE_DIR:-/data/video-analytics/media/c2-post-savant-replay-fps-probe}"
TTL_SECONDS="${MEDIA_RING_TTL_SECONDS:-600}"
MAX_BYTES="${MEDIA_RING_MAX_BYTES_PER_SOURCE:-1073741824}"
MIN_KEEP_SECONDS="${MEDIA_RING_MIN_KEEP_SECONDS:-120}"
STAMP="$(date -u +%Y%m%dT%H%M%S)"
OUT_DIR="${C2_14A_OUTPUT_DIR:-/data/video-analytics/media/evidence/c2_14a_rtsp_segment_ring_${STAMP}}"

mkdir -p "$OUT_DIR"

INSPECT_JSON="$OUT_DIR/runtime_ring_inspect.json"
INDEX_JSON="$OUT_DIR/segment_index_report.json"
RETENTION_JSON="$OUT_DIR/retention_dry_run_report.json"
SUMMARY_JSON="$OUT_DIR/decision_summary.json"

python "$ROOT/scripts/tools/manage_c2_14_rtsp_segment_ring.py" inspect \
  --ring-root "$RING_ROOT" \
  --source-id "$SOURCE_ID" \
  --json-output "$INSPECT_JSON" >/dev/null

python "$ROOT/scripts/tools/manage_c2_14_rtsp_segment_ring.py" index-existing \
  --source-dir "$SOURCE_DIR" \
  --ring-root "$RING_ROOT" \
  --source-id "$SOURCE_ID" \
  --ttl-seconds "$TTL_SECONDS" \
  --json-output "$INDEX_JSON" >/dev/null

python "$ROOT/scripts/tools/manage_c2_14_rtsp_segment_ring.py" retention-dry-run \
  --ring-root "$RING_ROOT" \
  --source-id "$SOURCE_ID" \
  --ttl-seconds "$TTL_SECONDS" \
  --max-bytes "$MAX_BYTES" \
  --min-keep-seconds "$MIN_KEEP_SECONDS" \
  --json-output "$RETENTION_JSON" >/dev/null

python - "$INSPECT_JSON" "$INDEX_JSON" "$RETENTION_JSON" "$SUMMARY_JSON" <<'PY'
import json
import sys
from pathlib import Path

inspect_path, index_path, retention_path, summary_path = [Path(arg) for arg in sys.argv[1:]]
inspect = json.loads(inspect_path.read_text())
index = json.loads(index_path.read_text())
retention = json.loads(retention_path.read_text())

failures = []
if inspect.get("unsafe_payload_scan", {}).get("passed") is False:
    failures.append("inspect_unsafe_payload")
if index.get("unsafe_payload_scan", {}).get("passed") is False:
    failures.append("index_unsafe_payload")
if retention.get("unsafe_payload_scan", {}).get("passed") is False:
    failures.append("retention_unsafe_payload")
if retention.get("unsafe_deletion_target_count") not in (0, None):
    failures.append("unsafe_deletion_target")
if retention.get("deleted_count") not in (0, None):
    failures.append("dry_run_deleted_files")

ring_root = Path(inspect["ring_root"]).resolve(strict=False)
source_id = inspect["source_id"]
ring_source_root = (ring_root / source_id).resolve(strict=False)
for candidate in retention.get("delete_candidates") or []:
    segment_dir = Path(candidate.get("segment_dir") or "").resolve(strict=False)
    try:
        segment_dir.relative_to(ring_source_root / "segments")
    except ValueError:
        failures.append("delete_candidate_outside_ring")

compatible = int(index.get("compatible_segments_found") or 0)
marker = (
    "FAIL_C2_14_RTSP_SEGMENT_RING_BLOCKED"
    if failures
    else "PASS_C2_14A_RTSP_SEGMENT_RING_READY"
    if compatible > 0
    else "PARTIAL_C2_14A_NO_COMPATIBLE_SEGMENTS"
)
summary = {
    "schema_version": "1.0-c2.14a-smoke",
    "result_marker": marker,
    "input_type": inspect.get("input_type"),
    "source_id": source_id,
    "ring_root": str(ring_root),
    "segment_index_path": index.get("index_path"),
    "segment_index_status": "present" if Path(index.get("index_path") or "").exists() else "missing",
    "segment_index_row_count": index.get("index_row_count"),
    "compatible_segments_found": compatible,
    "retention_policy": {
        "ttl_seconds": retention.get("ttl_seconds"),
        "max_bytes": retention.get("max_bytes"),
        "min_keep_seconds": retention.get("min_keep_seconds"),
        "dry_run": retention.get("dry_run"),
        "delete_candidate_count": retention.get("delete_candidate_count"),
        "deleted_count": retention.get("deleted_count"),
        "unsafe_deletion_target_count": retention.get("unsafe_deletion_target_count"),
    },
    "cleanup_dry_run_result": "safe" if not failures else "unsafe",
    "event_clip_builder_status": "design_contract_ready_runtime_segments_required",
    "failures": failures,
    "reports": {
        "inspect": str(inspect_path),
        "index": str(index_path),
        "retention": str(retention_path),
    },
}
summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

print(f"input_type={summary['input_type']}")
print(f"source_id={summary['source_id']}")
print(f"ring_root={summary['ring_root']}")
print(f"segment_index_status={summary['segment_index_status']}")
print(f"segment_index_row_count={summary['segment_index_row_count']}")
print(f"compatible_segments_found={summary['compatible_segments_found']}")
print(f"retention_policy_status=bounded_config_found")
print(f"retention_ttl_seconds={summary['retention_policy']['ttl_seconds']}")
print(f"retention_max_bytes={summary['retention_policy']['max_bytes']}")
print(f"cleanup_dry_run_result={summary['cleanup_dry_run_result']}")
print(f"delete_candidate_count={summary['retention_policy']['delete_candidate_count']}")
print(f"deleted_count={summary['retention_policy']['deleted_count']}")
print(f"event_clip_builder_status={summary['event_clip_builder_status']}")
print(f"result_marker={marker}")
print(f"output_dir={summary_path.parent}")

if marker.startswith("FAIL_"):
    sys.exit(2)
PY
