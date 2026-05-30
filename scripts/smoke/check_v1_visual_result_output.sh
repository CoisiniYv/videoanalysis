#!/usr/bin/env bash
set -euo pipefail

echo "=== V1 Visual Result Output Smoke ==="
echo "scope=debug_mvp_visual_output"
echo "no_production_clip_worker=YES"
echo "no_production_media_worker=YES"
echo "no_replay_cache_sink_deployment=YES"
echo "no_db_migration=YES"
echo "no_performance_test=YES"

OUTPUT_ROOT="${V1_OUTPUT_ROOT:-/data/video-analytics/media/debug/visual_results}"
A2A_ROOT="${V1_A2A_ROOT:-/data/video-analytics/media/debug/r3_3a2a_frame_uuid_identity}"
RUN_ID="${V1_RUN_ID:-v1_smoke_$(date +%s)}"
WORK_DIR="${V1_WORK_DIR:-tmp/v1_visual_result_smoke}"
SUMMARY_JSON="${V1_A2A_SUMMARY_JSON:-}"
SOURCE_FRAME="${V1_SOURCE_FRAME:-}"
SOURCE_CLIP="${V1_SOURCE_CLIP:-}"
EVENT_JSON=""
MOCK_INPUT="false"

if [[ -z "${SUMMARY_JSON}" && -d "${A2A_ROOT}" ]]; then
  SUMMARY_JSON="$(find "${A2A_ROOT}" -maxdepth 2 -name identity_summary.json -type f | sort | tail -1 || true)"
fi

if [[ -n "${SUMMARY_JSON}" && -f "${SUMMARY_JSON}" ]]; then
  echo "input=real_a2a_summary"
  echo "summary_json=${SUMMARY_JSON}"
  if [[ -z "${SOURCE_FRAME}" ]]; then
    SOURCE_FRAME="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("inspection_material_path") or "")' "${SUMMARY_JSON}")"
  fi
  if [[ -z "${SOURCE_CLIP}" ]]; then
    SOURCE_CLIP="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("source_aligned_clip_path") or "")' "${SUMMARY_JSON}")"
  fi
else
  echo "input=mock_event_json"
  MOCK_INPUT="true"
  mkdir -p "${WORK_DIR}"
  EVENT_JSON="${WORK_DIR}/event.json"
  SOURCE_FRAME="${WORK_DIR}/frame.jpg"
  SOURCE_CLIP="${WORK_DIR}/clip.mp4"
  export EVENT_JSON SOURCE_FRAME SOURCE_CLIP
  python3 - <<'PY'
import json
import os
from pathlib import Path

import cv2
import numpy as np

event_path = Path(os.environ["EVENT_JSON"])
frame_path = Path(os.environ["SOURCE_FRAME"])
clip_path = Path(os.environ["SOURCE_CLIP"])

image = np.zeros((240, 320, 3), dtype=np.uint8)
image[:] = (28, 40, 52)
cv2.rectangle(image, (120, 35), (215, 220), (70, 160, 240), -1)
cv2.imwrite(str(frame_path), image)

writer = cv2.VideoWriter(str(clip_path), cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (320, 240))
if not writer.isOpened():
    raise SystemExit("failed to create mock clip")
for _ in range(12):
    writer.write(image)
writer.release()

event = {
    "event_id": "v1-smoke-event",
    "source_event_id": "v1:smoke:intrusion:123",
    "event_type": "intrusion",
    "camera_id": "cam_v1_smoke",
    "source_id": "source_v1_smoke",
    "track_id": "42",
    "event_ts_ms": 123456789,
    "frame_uuid": "v1-smoke-frame-uuid",
    "keyframe_uuid": None,
    "previous_keyframe_uuid": None,
    "payload": {
        "bbox": {"x": 120, "y": 35, "width": 95, "height": 185},
        "roi_polygon": [[10, 10], [310, 10], [310, 230], [10, 230]],
        "media": {
            "frame_uuid": "v1-smoke-frame-uuid",
            "event_ts_ms": 123456789
        }
    }
}
event_path.write_text(json.dumps(event), encoding="utf-8")
PY
fi

if [[ ! -f "${SOURCE_FRAME}" ]]; then
  echo "FAIL: source frame missing: ${SOURCE_FRAME}"
  exit 1
fi

CMD=(
  python3 scripts/debug/generate_visual_result.py
  --source-frame "${SOURCE_FRAME}"
  --source-clip "${SOURCE_CLIP}"
  --output-root "${OUTPUT_ROOT}"
  --run-id "${RUN_ID}"
)

if [[ -n "${SUMMARY_JSON}" && -f "${SUMMARY_JSON}" ]]; then
  CMD+=(--a2a-summary-json "${SUMMARY_JSON}")
else
  CMD+=(--event-json "${EVENT_JSON}")
fi

LOG_PATH="${WORK_DIR}/generate_visual_result.log"
mkdir -p "${WORK_DIR}"
"${CMD[@]}" | tee "${LOG_PATH}"

METADATA_JSON="$(python3 -c 'import pathlib,sys
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    if line.startswith("metadata_json="):
        print(line.split("=", 1)[1])
        break
' "${LOG_PATH}")"

if [[ -z "${METADATA_JSON}" || ! -f "${METADATA_JSON}" ]]; then
  echo "FAIL: metadata.json not found"
  exit 1
fi

export METADATA_JSON MOCK_INPUT
python3 - <<'PY'
import json
import os
from pathlib import Path

metadata_path = Path(os.environ["METADATA_JSON"])
metadata = json.loads(metadata_path.read_text())
media = metadata["media"]
limitations = "\n".join(metadata.get("limitations", []))

required = [
    media.get("report_path"),
    media.get("raw_snapshot_path"),
    media.get("annotated_snapshot_path"),
]
for path in required:
    if not path or not Path(path).exists() or Path(path).stat().st_size <= 0:
        raise SystemExit(f"FAIL: expected non-empty output missing: {path}")

clip_path = media.get("annotated_clip_path")
frames_dir = media.get("annotated_frames_dir")
if clip_path:
    if not Path(clip_path).exists() or Path(clip_path).stat().st_size <= 0:
        raise SystemExit(f"FAIL: annotated clip missing/non-empty check failed: {clip_path}")
elif frames_dir:
    frames = sorted(Path(frames_dir).glob("*.jpg"))
    if not frames:
        raise SystemExit(f"FAIL: annotated_frames empty: {frames_dir}")
else:
    raise SystemExit("FAIL: neither annotated_clip.mp4 nor annotated_frames was generated")

if "not production evidence" not in limitations:
    raise SystemExit("FAIL: not production evidence limitation missing")
if "source extraction only; not Replay evidence" not in limitations:
    raise SystemExit("FAIL: source extraction / not Replay limitation missing")
if metadata.get("visual_result_type") == "production_evidence":
    raise SystemExit("FAIL: metadata claims production evidence")

print(f"metadata_json={metadata_path}")
print(f"report_md={media.get('report_path')}")
print(f"raw_snapshot={media.get('raw_snapshot_path')}")
print(f"annotated_snapshot={media.get('annotated_snapshot_path')}")
print(f"raw_clip={media.get('raw_clip_path')}")
print(f"annotated_clip={clip_path or ''}")
print(f"annotated_frames={frames_dir or ''}")
print(f"mock_input={os.environ['MOCK_INPUT']}")
PY

echo "PASS: V1 visual result output generated"
