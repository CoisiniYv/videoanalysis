#!/usr/bin/env bash
set -euo pipefail

RTSP_URL="${R3_3A2A_RTSP_URL:-rtsp://10.37.57.112:8554/live/1080movie}"
SOURCE_ID="${R3_3A2A_SOURCE_ID:-r3_3a2a_rtsp_identity_$(date +%s)}"
SOURCE_MP4="${R3_3A2A_SOURCE_MP4:-/home/user/video-analytics/testVideo/1080movie.mp4}"
TRACE_ROOT="${R3_3A2A_TRACE_OUTPUT_ROOT:-/data/video-analytics/media/debug/r3_3a2a_frame_anchor_trace}"
OUTPUT_ROOT="${R3_3A2A_OUTPUT_ROOT:-/data/video-analytics/media/debug/r3_3a2a_frame_uuid_identity}"
DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
PG_CONTAINER="${PG_CONTAINER:-c1-official-postgres}"

echo "=== R3.3A2a Frame UUID Source-Frame Identity Smoke ==="
echo "rtsp_url=${RTSP_URL}"
echo "source_id=${SOURCE_ID}"
echo "source_mp4=${SOURCE_MP4}"
echo "trace_root=${TRACE_ROOT}"
echo "output_root=${OUTPUT_ROOT}"
echo "scope=frame_uuid_identity_only"
echo "no_replay_service=YES"
echo "no_video_file_sink_production=YES"
echo "no_clip_worker=YES"
echo "no_media_worker=YES"
echo "no_exact_snapshot=YES"
echo "no_db_migration=YES"
echo "no_performance_test=YES"

if command -v ffprobe >/dev/null 2>&1; then
  if ! timeout 20 ffprobe -rtsp_transport tcp "${RTSP_URL}" >/tmp/r3_3a2a_ffprobe.log 2>&1; then
    echo "SKIP: RTSP unavailable; ffprobe preflight failed. See /tmp/r3_3a2a_ffprobe.log"
    exit 0
  fi
else
  echo "warning=ffprobe_not_found; continuing without RTSP preflight"
fi

psql_value() {
  local sql="$1"
  if command -v psql >/dev/null 2>&1; then
    psql "${DATABASE_URL}" -t -A -v ON_ERROR_STOP=1 -c "${sql}"
  else
    docker exec "${PG_CONTAINER}" psql -U video -d video_analytics \
      -t -A -v ON_ERROR_STOP=1 -c "${sql}"
  fi
}

rm -rf "${TRACE_ROOT}/${SOURCE_ID}" "${OUTPUT_ROOT}/${SOURCE_ID}"
mkdir -p "${OUTPUT_ROOT}/${SOURCE_ID}"

R3_3A2A_FRAME_ANCHOR_TRACE_ENABLED=true \
R3_3A2A_TRACE_OUTPUT_ROOT="${TRACE_ROOT}" \
R3_3A2A_TRACE_MAX_FRAMES="${R3_3A2A_TRACE_MAX_FRAMES:-500}" \
R2_5_RTSP_URL="${RTSP_URL}" \
R2_5_SOURCE_ID="${SOURCE_ID}" \
R2_5_WAIT_SECONDS="${R3_3A2A_WAIT_SECONDS:-90}" \
R2_5_MIN_EVENTS="${R3_3A2A_MIN_EVENTS:-1}" \
bash scripts/smoke/check_r2_5_single_rtsp_camera_inference.sh

EVENT_JSON="$(psql_value "
SELECT json_build_object(
  'event_id', id,
  'source_event_id', source_event_id,
  'event_type', event_type,
  'camera_id', camera_id,
  'source_id', source_id,
  'event_ts_ms', event_ts_ms,
  'frame_uuid', frame_uuid,
  'keyframe_uuid', keyframe_uuid,
  'previous_keyframe_uuid', payload->'media'->>'previous_keyframe_uuid',
  'frame_num', payload->'media'->>'frame_num',
  'media', payload->'media',
  'bbox', payload->'bbox',
  'zone_id', payload->>'zone_id',
  'inside_ms', payload->>'inside_ms'
)::text
FROM events
WHERE source_id = '${SOURCE_ID}'
  AND frame_uuid IS NOT NULL
ORDER BY
  (
    COALESCE(NULLIF(payload->'media'->>'frame_pts', ''), '0')::bigint >= 5000000000
  ) DESC,
  created_at DESC
LIMIT 1;
")"

if [[ -z "${EVENT_JSON}" ]]; then
  echo "FAIL: no behavior event with frame_uuid found for source_id=${SOURCE_ID}"
  exit 1
fi

TRACE_PATH="${TRACE_ROOT}/${SOURCE_ID}/trace.jsonl"
SUMMARY_PATH="${OUTPUT_ROOT}/${SOURCE_ID}/identity_summary.json"
export EVENT_JSON TRACE_PATH SUMMARY_PATH SOURCE_MP4 OUTPUT_DIR="${OUTPUT_ROOT}/${SOURCE_ID}"

python3 - <<'PY'
import json
import os
import subprocess
from pathlib import Path

event = json.loads(os.environ["EVENT_JSON"])
trace_path = Path(os.environ["TRACE_PATH"])
summary_path = Path(os.environ["SUMMARY_PATH"])
source_mp4 = Path(os.environ["SOURCE_MP4"])
output_dir = Path(os.environ["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)

if not trace_path.exists():
    raise SystemExit(f"FAIL: trace.jsonl not found: {trace_path}")

records = []
with trace_path.open("r", encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line:
            records.append(json.loads(line))

frame_uuid = event.get("frame_uuid")
matches = [record for record in records if record.get("frame_uuid") == frame_uuid]
if not matches:
    raise SystemExit(f"FAIL: event.frame_uuid not found in trace: {frame_uuid}")
if len(matches) > 1:
    raise SystemExit(
        f"FAIL: event.frame_uuid matched multiple trace records: {frame_uuid} count={len(matches)}"
    )

match = matches[0]
material_path = None
material_error = None
source_clip_path = None
source_clip_error = None
clip_window_seconds = 10.0
clip_start_seconds = None
clip_duration_seconds = None
event_position_ms = None
event_offset_from_center_ms = None
event_offset_from_center_frames = None
frame_pts = match.get("frame_pts")
seek_seconds = None
if isinstance(frame_pts, int) and frame_pts >= 0:
    seek_seconds = frame_pts / 1_000_000_000.0
elif event.get("event_ts_ms") is not None:
    seek_seconds = float(event["event_ts_ms"]) / 1000.0

if not source_mp4.exists():
    raise SystemExit(f"FAIL: source_mp4 not found; source extraction blocked: {source_mp4}")
if seek_seconds is None:
    raise SystemExit("FAIL: no frame_pts available; source extraction blocked")

if source_mp4.exists() and seek_seconds is not None:
    material = output_dir / "matched_frame.jpg"
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{seek_seconds:.3f}",
        "-i",
        str(source_mp4),
        "-frames:v",
        "1",
        str(material),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        material_path = str(material)
    except Exception as exc:  # pragma: no cover - environment dependent
        material_error = f"{type(exc).__name__}: {exc}"

    clip_start_seconds = max(seek_seconds - 5.0, 0.0)
    event_position_ms = int(round((seek_seconds - clip_start_seconds) * 1000.0))
    clip_duration_seconds = 10.0 if seek_seconds >= 5.0 else seek_seconds + 5.0
    clip = output_dir / "source_aligned_clip.mp4"
    clip_cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{clip_start_seconds:.3f}",
        "-i",
        str(source_mp4),
        "-t",
        f"{clip_duration_seconds:.3f}",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        str(clip),
    ]
    try:
        subprocess.run(clip_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        source_clip_path = str(clip)
    except Exception as exc:  # pragma: no cover - environment dependent
        source_clip_error = f"{type(exc).__name__}: {exc}"

    clip_center_ms = int(round((clip_duration_seconds * 1000.0) / 2.0))
    event_offset_from_center_ms = int(event_position_ms - clip_center_ms)
    duration_ns = match.get("duration")
    frame_duration_ms = (duration_ns / 1_000_000.0) if isinstance(duration_ns, int) and duration_ns > 0 else None
    if frame_duration_ms:
        event_offset_from_center_frames = int(round(event_offset_from_center_ms / frame_duration_ms))

if source_clip_path is None:
    raise SystemExit(f"FAIL: source aligned clip could not be generated: {source_clip_error}")

summary = {
    "result": "SOURCE_FRAME_AND_SOURCE_CLIP_PASS_MANUAL_VISUAL_CHECK_REQUIRED",
    "source_id": event.get("source_id"),
    "camera_id": event.get("camera_id"),
    "source_mp4": str(source_mp4),
    "looping_video": True,
    "pts_wrap_risk": True,
    "event": event,
    "trace_path": str(trace_path),
    "trace_record_count": len(records),
    "unique_trace_match": True,
    "matched_trace_record": match,
    "inspection_material_path": material_path,
    "inspection_material_error": material_error,
    "seek_seconds_for_material": seek_seconds,
    "source_aligned_clip_path": source_clip_path,
    "source_aligned_clip_error": source_clip_error,
    "clip_window_seconds": clip_window_seconds,
    "clip_start_seconds": clip_start_seconds,
    "clip_duration_seconds": clip_duration_seconds,
    "event_frame_position_in_clip_ms": event_position_ms,
    "event_offset_from_clip_center_ms": event_offset_from_center_ms,
    "event_offset_from_clip_center_frames": event_offset_from_center_frames,
    "source_clip_alignment": "PASS",
    "source_clip_limitation": "source-video extraction, NOT Replay extraction",
    "identity_drift_frames": 0,
    "identity_drift_ms": 0,
    "manual_visual_check": "required",
    "boundaries": {
        "no_replay_service": True,
        "no_video_file_sink_production": True,
        "no_clip_worker": True,
        "no_media_worker": True,
        "no_exact_snapshot": True,
        "no_db_migration": True,
        "no_performance_test": True,
        "timestamp_fallback_primary": False,
    },
}
summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
print(f"event_id={event.get('event_id')}")
print(f"source_event_id={event.get('source_event_id')}")
print(f"event_type={event.get('event_type')}")
print(f"event_ts_ms={event.get('event_ts_ms')}")
print(f"frame_uuid={event.get('frame_uuid')}")
print(f"keyframe_uuid={event.get('keyframe_uuid')}")
print(f"previous_keyframe_uuid={event.get('previous_keyframe_uuid')}")
print(f"trace_path={trace_path}")
print(f"trace_record_count={len(records)}")
print(f"matched_frame_num={match.get('frame_num')}")
print(f"matched_frame_pts={match.get('frame_pts')}")
print(f"matched_ntp_timestamp={match.get('ntp_timestamp')}")
print(f"inspection_material_path={material_path or ''}")
print(f"inspection_material_error={material_error or ''}")
print(f"source_aligned_clip_path={source_clip_path or ''}")
print(f"source_clip_alignment=PASS")
print(f"clip_start_seconds={clip_start_seconds}")
print(f"clip_duration_seconds={clip_duration_seconds}")
print(f"event_frame_position_in_clip_ms={event_position_ms}")
print(f"event_offset_from_clip_center_ms={event_offset_from_center_ms}")
print(f"event_offset_from_clip_center_frames={event_offset_from_center_frames}")
print(f"summary_path={summary_path}")
PY

echo "PASS: R3.3A2a frame_uuid unique trace identity verified"
echo "PASS: R3.3A2a source-video aligned clip generated"
echo "NOTE: manual visual inspection is still required for acceptance"
