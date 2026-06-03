#!/usr/bin/env bash
set -euo pipefail

SUMMARY_PATH="${R3_3A2B_A2A_SUMMARY_PATH:-}"
OUTPUT_ROOT="${R3_3A2B_OUTPUT_ROOT:-/data/video-analytics/media/debug/r3_3a2b_replay_visual}"

echo "=== R3.3A2b Replay Keyframe Visual Clip POC Smoke ==="
echo "summary_path=${SUMMARY_PATH}"
echo "output_root=${OUTPUT_ROOT}"
echo "scope=pending_same_stream_replay_cache_poc"
echo "replay_api_calls=NO"
echo "no_production_clip_worker=YES"
echo "no_production_media_worker=YES"
echo "no_db_migration=YES"
echo "no_production_video_file_sink=YES"
echo "no_performance_test=YES"

if [[ -z "${SUMMARY_PATH}" || ! -f "${SUMMARY_PATH}" ]]; then
  echo "FAIL: R3_3A2B_A2A_SUMMARY_PATH must point to an A2a identity_summary.json"
  exit 1
fi

mkdir -p "${OUTPUT_ROOT}"
REPORT_JSON="${OUTPUT_ROOT}/replay_visual_summary.json"
export SUMMARY_PATH OUTPUT_ROOT REPORT_JSON

python3 - <<'PY'
import json
import os
from pathlib import Path

summary_path = Path(os.environ["SUMMARY_PATH"])
output_root = Path(os.environ["OUTPUT_ROOT"])
report_json = Path(os.environ["REPORT_JSON"])

a2a = json.loads(summary_path.read_text(encoding="utf-8"))
event = a2a.get("event") or {}
media = event.get("media") if isinstance(event.get("media"), dict) else {}
previous_keyframe_uuid = event.get("previous_keyframe_uuid") or media.get(
    "previous_keyframe_uuid"
)
keyframe_uuid = event.get("keyframe_uuid") or media.get("keyframe_uuid")
anchor_uuid = previous_keyframe_uuid or keyframe_uuid
anchor_source = "previous_keyframe_uuid" if previous_keyframe_uuid else "keyframe_uuid"

result = {
    "result": "UNVERIFIED",
    "blocked": True,
    "reason": (
        "same-stream Replay/cache POC not deployed; verification could not be executed"
    ),
    "temporary_harness": "none",
    "event_id": event.get("event_id"),
    "source_event_id": event.get("source_event_id"),
    "event_type": event.get("event_type"),
    "source_id": event.get("source_id"),
    "camera_id": event.get("camera_id"),
    "event_ts_ms": event.get("event_ts_ms"),
    "frame_uuid": event.get("frame_uuid"),
    "keyframe_uuid": keyframe_uuid,
    "previous_keyframe_uuid": previous_keyframe_uuid,
    "replay_anchor_used": anchor_uuid,
    "replay_anchor_source": anchor_source if anchor_uuid else None,
    "replay_api_calls": False,
    "job_request": None,
    "job_response": None,
    "clip_path": None,
    "metadata_path": None,
    "playable": None,
    "visual_alignment_result": "UNVERIFIED",
    "boundaries": {
        "no_production_clip_worker": True,
        "no_production_media_worker": True,
        "no_db_migration": True,
        "no_production_video_file_sink": True,
        "no_performance_test": True,
    },
}

if not anchor_uuid:
    result["reason"] = (
        "no keyframe uuid available; current frame_uuid was not used as Replay anchor"
    )
    report_json.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    raise SystemExit(0)

report_json.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
print(f"event_id={result['event_id']}")
print(f"frame_uuid={result['frame_uuid']}")
print(f"replay_anchor_used={anchor_uuid}")
print(f"replay_anchor_source={result['replay_anchor_source']}")
print(f"visual_alignment_result={result['visual_alignment_result']}")
print(f"reason={result['reason']}")
print(f"report_json={report_json}")
PY
