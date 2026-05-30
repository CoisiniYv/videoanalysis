#!/usr/bin/env bash
set -euo pipefail

RTSP_URL="${R3_3A0_RTSP_URL:-rtsp://10.37.57.112:8554/live/1080movie}"
SOURCE_ID="${R3_3A0_SOURCE_ID:-r3_3a0_rtsp_probe_$(date +%s)}"
PROBE_MAX_FRAMES="${R3_3A0_PROBE_MAX_FRAMES:-20}"
PROBE_ROOT="${R3_3A0_PROBE_OUTPUT_ROOT:-/data/video-analytics/media/debug/r3_3a0_frame_uuid_probe}"
COMPOSE_FILE="${COMPOSE_FILE:-infra/docker-compose.c1-official-adapter.yml}"

echo "=== R3.3A0 Savant Frame UUID / Replay Anchor Runtime Probe ==="
echo "rtsp_url=${RTSP_URL}"
echo "source_id=${SOURCE_ID}"
echo "probe_root=${PROBE_ROOT}"
echo "probe_max_frames=${PROBE_MAX_FRAMES}"
echo "scope=runtime diagnostic only"
echo "no_replay_clip=YES"
echo "no_exact_snapshot=YES"
echo "no_segment_recording=YES"
echo "no_performance_test=YES"

if command -v ffprobe >/dev/null 2>&1; then
  if ! timeout 20 ffprobe -rtsp_transport tcp "${RTSP_URL}" >/tmp/r3_3a0_ffprobe.log 2>&1; then
    echo "SKIP: RTSP unavailable; ffprobe preflight failed. See /tmp/r3_3a0_ffprobe.log"
    exit 0
  fi
else
  echo "warning=ffprobe_not_found; continuing without RTSP preflight"
fi

rm -rf "${PROBE_ROOT:?}/${SOURCE_ID}" 2>/dev/null || true

echo "Running R2.5 RTSP smoke with runtime probe enabled..."
R3_3A0_FRAME_UUID_PROBE_ENABLED=true \
R3_3A0_PROBE_MAX_FRAMES="${PROBE_MAX_FRAMES}" \
R3_3A0_PROBE_OUTPUT_ROOT="${PROBE_ROOT}" \
R2_5_RTSP_URL="${RTSP_URL}" \
R2_5_SOURCE_ID="${SOURCE_ID}" \
R2_5_WAIT_SECONDS="${R3_3A0_WAIT_SECONDS:-90}" \
bash scripts/smoke/check_r2_5_single_rtsp_camera_inference.sh

PROBE_DIR="${PROBE_ROOT}/${SOURCE_ID}"
if [[ ! -d "${PROBE_DIR}" ]]; then
  echo "FAIL: probe directory not found: ${PROBE_DIR}"
  echo "Recent Savant probe logs:"
  docker logs c1-official-savant --tail 200 2>&1 | grep 'r3_3a0_frame_uuid_probe' || true
  exit 1
fi

COUNT="$(find "${PROBE_DIR}" -maxdepth 1 -type f -name '*.json' | wc -l | tr -d '[:space:]')"
echo "probe_json_count=${COUNT}"
if [[ "${COUNT}" -le 0 ]]; then
  echo "FAIL: no probe JSON files produced in ${PROBE_DIR}"
  exit 1
fi

FIRST_JSON="$(find "${PROBE_DIR}" -maxdepth 1 -type f -name '*.json' | sort | head -n 1)"
echo "first_probe_json=${FIRST_JSON}"
python3 -m json.tool "${FIRST_JSON}"

python3 - "${FIRST_JSON}" <<'PY'
import json
import sys

sample = json.load(open(sys.argv[1], encoding="utf-8"))
for key in (
    "frame_object_type",
    "available_attrs",
    "uuid",
    "previous_keyframe_uuid",
    "keyframe_uuid",
    "video_frame_object_type",
    "nested_objects",
    "pts",
    "frame_num",
    "timestamp_ms_used_by_event",
):
    if key not in sample:
        raise SystemExit(f"missing probe key: {key}")
print("runtime_object_type=" + str(sample.get("frame_object_type")))
print("uuid_found=" + str(sample.get("uuid") is not None))
print("previous_keyframe_uuid_found=" + str(sample.get("previous_keyframe_uuid") is not None))
print("keyframe_uuid_found=" + str(sample.get("keyframe_uuid") is not None))
print("video_frame_object_type=" + str(sample.get("video_frame_object_type")))
print("video_frame_uuid_found=" + str(sample.get("video_frame_uuid") is not None))
print("pts_found=" + str(sample.get("pts") is not None))
print("frame_num_found=" + str(sample.get("frame_num") is not None))
PY

echo "Checking metadata-sink sample for UUID fields..."
META_FILE="$(find /data/video-analytics/media/c1-official-metadata -maxdepth 1 -type f -name "${SOURCE_ID}*metadata.ndjson" | sort | tail -n 1 || true)"
if [[ -n "${META_FILE}" && -f "${META_FILE}" ]]; then
  echo "metadata_sink_file=${META_FILE}"
  head -n 1 "${META_FILE}" | python3 -m json.tool
  if head -n 5 "${META_FILE}" | grep -E '"(uuid|frame_uuid|keyframe_uuid|previous_keyframe_uuid)"' >/dev/null; then
    echo "metadata_sink_uuid_fields=present"
  else
    echo "metadata_sink_uuid_fields=not_found_in_first_5_rows"
  fi
else
  echo "metadata_sink_file=not_found"
fi

echo "Recent probe log lines:"
docker logs c1-official-savant --tail 200 2>&1 | grep 'r3_3a0_frame_uuid_probe' || true

echo "PASS: R3.3A0 frame UUID runtime probe completed"
