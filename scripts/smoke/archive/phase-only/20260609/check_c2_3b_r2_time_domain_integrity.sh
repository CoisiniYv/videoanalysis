#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

REPLAY_API_URL="${C2_3B_REPLAY_API_URL:-${C2_POC_REPLAY_API_URL:-http://127.0.0.1:8098}}"
POST_SAVANT_SOURCE_ID="${C2_3B_POST_SAVANT_SOURCE_ID:-c2_post_savant_fps_probe}"
SINK_URL="${C2_3B_SINK_URL:-${C2_POC_SINK_URL:-dealer+connect:tcp://video-file-sink:6666}}"
SINK_ROOT="${C2_3B_SINK_ROOT:-/data/video-analytics/media/c2-post-savant-replay-fps-probe}"
STABLE_SINK_OUTPUT="${C2_3B_R2_STABLE_SINK_OUTPUT:-/data/video-analytics/media/c2-post-savant-replay-fps-probe/c2-fps-probe-20260607T140420%/unknown%}"
EVIDENCE_ROOT="${C2_3B_EVIDENCE_ROOT:-/data/video-analytics/media/evidence}"
AUDIT_ROOT="${C2_3B_AUDIT_ROOT:-/data/video-analytics/media/evidence_audit}"
PRE_SECONDS="${C2_3B_PRE_SECONDS:-5}"
POST_SECONDS="${C2_3B_POST_SECONDS:-5}"
REPLAY_FPS="${C2_3B_REPLAY_FPS:-24}"
KEYFRAME_WAIT_SECONDS="${C2_3B_KEYFRAME_WAIT_SECONDS:-30}"
SINK_WAIT_SECONDS="${C2_3B_SINK_WAIT_SECONDS:-90}"
RUN_ID="${C2_3B_RUN_ID:-c2_3b_r2_$(date +%Y%m%dT%H%M%S)}"
EVENT_ID="${C2_3B_EVENT_ID:-66666666-6666-4666-8666-$(date +%012s)}"
REQUEST_ID="c2_3b_r2:req:${RUN_ID}"
SOURCE_EVENT_ID="c2_3b_r2:synthetic:${RUN_ID}"
REPORT_PATH="${C2_3B_R2_REPORT_PATH:-/tmp/${RUN_ID}-report.json}"
EVENT_FRAME_NUM="${C2_3B_EVENT_FRAME_NUM:-125}"
EVENT_FRAME_PTS="${C2_3B_EVENT_FRAME_PTS:-}"
REQUESTED_START_PTS="${C2_3B_REQUESTED_START_PTS:-}"
REQUESTED_END_PTS="${C2_3B_REQUESTED_END_PTS:-}"
ANCHOR_KEYFRAME_UUID="${C2_3B_ANCHOR_KEYFRAME_UUID:-}"
ANCHOR_PTS="${C2_3B_ANCHOR_PTS:-}"

fail() {
  local marker="$1"
  local reason="$2"
  python - "$marker" "$reason" "$REPORT_PATH" <<'PY'
import json
import sys
from pathlib import Path

marker, reason, report_path = sys.argv[1:4]
payload = {"result_marker": marker, "reason": reason}
Path(report_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
PY
  exit 2
}

derive_window_from_stable_sink() {
  PY_ROOT_DIR="$ROOT_DIR" python - "$STABLE_SINK_OUTPUT" "$EVENT_FRAME_NUM" "$PRE_SECONDS" "$POST_SECONDS" <<'PY'
import json
import sys
from pathlib import Path

sink = Path(sys.argv[1])
event_frame_num = int(sys.argv[2])
pre = float(sys.argv[3])
post = float(sys.argv[4])
metadata = sink / "metadata.json"
if not metadata.is_file():
    raise SystemExit("stable sink metadata missing")
rows = []
try:
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else [payload]
except json.JSONDecodeError:
    rows = [json.loads(line) for line in metadata.read_text(encoding="utf-8").splitlines() if line.strip()]
frames = [row for row in rows if isinstance(row, dict) and row.get("pts") is not None]
if event_frame_num < 0 or event_frame_num >= len(frames):
    raise SystemExit("event frame outside stable sink metadata")
event = frames[event_frame_num]
event_pts = int(event["pts"])
start_pts = event_pts - int(pre * 1_000_000_000)
end_pts = event_pts + int(post * 1_000_000_000)
anchor = None
for frame in frames:
    pts = int(frame["pts"])
    if pts <= start_pts and frame.get("keyframe") is True:
        anchor = frame
if anchor is None:
    for frame in frames:
        if frame.get("keyframe") is True:
            anchor = frame
            break
if anchor is None:
    raise SystemExit("no stable sink keyframe found")
print(json.dumps({
    "event_frame_num": event_frame_num,
    "event_frame_pts": event_pts,
    "requested_start_pts": start_pts,
    "requested_end_pts": end_pts,
    "anchor_keyframe_uuid": anchor.get("uuid") or anchor.get("frame_uuid"),
    "anchor_pts": int(anchor["pts"]),
}, sort_keys=True))
PY
}

init_window() {
  local window_json
  window_json="$(derive_window_from_stable_sink)"
  EVENT_FRAME_PTS="${EVENT_FRAME_PTS:-$(python -c 'import json,sys; print(json.loads(sys.argv[1])["event_frame_pts"])' "$window_json")}"
  REQUESTED_START_PTS="${REQUESTED_START_PTS:-$(python -c 'import json,sys; print(json.loads(sys.argv[1])["requested_start_pts"])' "$window_json")}"
  REQUESTED_END_PTS="${REQUESTED_END_PTS:-$(python -c 'import json,sys; print(json.loads(sys.argv[1])["requested_end_pts"])' "$window_json")}"
  ANCHOR_KEYFRAME_UUID="${ANCHOR_KEYFRAME_UUID:-$(python -c 'import json,sys; print(json.loads(sys.argv[1])["anchor_keyframe_uuid"])' "$window_json")}"
  ANCHOR_PTS="${ANCHOR_PTS:-$(python -c 'import json,sys; print(json.loads(sys.argv[1])["anchor_pts"])' "$window_json")}"
}

wait_for_replay() {
  local deadline=$((SECONDS + 30))
  until curl --noproxy '*' -fsS "$REPLAY_API_URL/api/v1/status" >/tmp/"${RUN_ID}-replay-status.json"; do
    if (( SECONDS >= deadline )); then
      return 1
    fi
    sleep 2
  done
  return 0
}

create_replay_payload() {
  local payload_path="/tmp/${RUN_ID}-replay-job.json"
  local request_path="/tmp/${RUN_ID}-record-request.json"
  PY_ROOT_DIR="$ROOT_DIR" python - "$request_path" "$payload_path" "$POST_SAVANT_SOURCE_ID" "$EVENT_ID" "$REQUEST_ID" "$SOURCE_EVENT_ID" "$ANCHOR_KEYFRAME_UUID" "$SINK_URL" "$PRE_SECONDS" "$POST_SECONDS" "$REPLAY_FPS" "$EVENT_FRAME_PTS" "$EVENT_FRAME_NUM" "$REQUESTED_START_PTS" "$REQUESTED_END_PTS" "$ANCHOR_PTS" <<'PY'
import importlib
import json
import os
import sys
from pathlib import Path

request_path = Path(sys.argv[1])
payload_path = Path(sys.argv[2])
source_id, event_id, request_id, source_event_id, keyframe_uuid, sink_url = sys.argv[3:9]
pre_seconds, post_seconds, replay_fps = sys.argv[9:12]
frame_pts, frame_num, start_pts, end_pts, anchor_pts = sys.argv[12:17]
root = Path(os.environ["PY_ROOT_DIR"])

def activate(service, module):
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    sys.path.insert(0, str(root / "services" / service))
    try:
        return importlib.import_module(module)
    finally:
        try:
            sys.path.remove(str(root / "services" / service))
        except ValueError:
            pass

record_request = activate("event-worker", "app.record_request")
event = {
    "schema_version": "1.0",
    "source_event_id": source_event_id,
    "event_type": "c2_3b_r2_synthetic_event",
    "camera_id": "cam-c2-post-savant",
    "source_id": source_id,
    "event_ts_ms": 1780000000000,
    "frame_pts": int(frame_pts),
    "frame_num": int(frame_num),
    "keyframe_uuid": keyframe_uuid,
    "payload": {"media": {"source_id": source_id}},
    "evidence_policy": {
        "pre_seconds": int(float(pre_seconds)),
        "post_seconds": int(float(post_seconds)),
        "replay_source_kind": "post_savant",
        "evidence_topology": "post_savant",
        "annotation_source_policy": "post_savant_sink_metadata_only",
        "allow_db_annotation_fallback": False,
        "allow_legacy_annotation_fallback": False,
        "frame_pts": int(frame_pts),
        "frame_num": int(frame_num),
        "requested_start_pts": int(start_pts),
        "requested_end_pts": int(end_pts),
        "event_frame_pts": int(frame_pts),
        "replay_anchor_pts": int(anchor_pts),
        "replay_anchor_keyframe": keyframe_uuid,
        "replay_stop_strategy": "anchor_start_offset_zero",
    },
}
request = record_request.build_record_request(event, event_id, request_id=request_id)
request_path.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
worker = activate("clip-worker", "app.worker")
replay_client = importlib.import_module("app.replay_client")
labels = worker._replay_job_labels(event_id, request)
payload = replay_client.build_job_payload(
    source_id=request["source_id"],
    keyframe_uuid=keyframe_uuid,
    pre_seconds=float(pre_seconds),
    post_seconds=float(post_seconds),
    sink_endpoint=sink_url,
    labels=labels,
    stop_condition_mode="ts_delta_sec",
    fps=int(float(replay_fps)),
    force_constant_cadence=True,
    offset_seconds_override=0.0,
)
payload_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
assert payload["offset"]["seconds"] == 0.0
assert "ts_delta_sec" in payload["stop_condition"]
PY
  echo "$payload_path"
}

submit_replay_job() {
  local payload_path="$1"
  curl --noproxy '*' -fsS \
    -X PUT \
    -H 'content-type: application/json' \
    --data-binary "@${payload_path}" \
    "$REPLAY_API_URL/api/v1/job" >/tmp/"${RUN_ID}-replay-job-response.json"
}

wait_for_sink_output() {
  local deadline=$((SECONDS + SINK_WAIT_SECONDS))
  local best_dir=""
  while (( SECONDS < deadline )); do
    local metadata
    metadata="$(find "$SINK_ROOT" -path "*replay-event-${EVENT_ID}%*" -name metadata.json -type f -print 2>/dev/null | head -1 || true)"
    if [[ -n "$metadata" && -s "$metadata" ]]; then
      local dir
      dir="$(dirname "$metadata")"
      local video
      video="$(find "$dir" \( -name video.mov -o -name raw_clip.mov -o -name '*.mp4' -o -name '*.mkv' \) -type f -size +0c -print -quit)"
      if [[ -n "$video" ]]; then
        best_dir="$dir"
        sleep 4
        echo "$best_dir"
        return 0
      fi
    fi
    sleep 2
  done
  return 1
}

build_bundle() {
  local input_dir="$1"
  local bundle_dir="$2"
  local mode="$3"
  local extra_args=()
  if [[ "$mode" == "workaround" ]]; then
    extra_args+=(
      --time-domain-crop-applied
      --crop-video-to-time-window
      --evidence-capture-mode stable_post_savant_sink_time_crop
      --event-style-replay-job-failed
      --replay-event-flow-status blocked_by_video_integrity
      --workaround-used
      --workaround-reason event_style_replay_resulting_stream_video_integrity_failed
      --replay-stop-strategy stable_sink_pts_crop
    )
  else
    extra_args+=(
      --evidence-capture-mode keyframe_safe_event_style_replay
      --event-style-replay-job-passed
      --replay-event-flow-status attempted
      --workaround-not-used
      --replay-stop-strategy anchor_start_offset_zero
    )
  fi
  python "$ROOT_DIR/scripts/tools/build_c2_post_savant_evidence_bundle.py" \
    --input-dir "$input_dir" \
    --output-dir "$bundle_dir" \
    --copy-video \
    --trim-sidecar-to-video \
    --overwrite \
    --max-fps "${MAX_FPS:-8/1}" \
    --min-fps "${MIN_FPS:-2/1}" \
    --fps-gating-applied \
    --requested-start-pts "$REQUESTED_START_PTS" \
    --requested-end-pts "$REQUESTED_END_PTS" \
    --event-frame-pts "$EVENT_FRAME_PTS" \
    --video-integrity-required \
    "${extra_args[@]}"
}

run_audit() {
  local bundle_dir="$1"
  local audit_dir="$2"
  local sample_frames
  sample_frames="$(python - "$bundle_dir" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads((Path(sys.argv[1]) / "summary.json").read_text(encoding="utf-8"))
count = int(summary.get("sidecar_frame_count") or summary.get("frame_count") or 0)
if count <= 1:
    print("0")
else:
    frames = sorted({0, count // 8, count // 4, count // 2, (count * 3) // 4, count - 1})
    print(",".join(str(frame) for frame in frames if 0 <= frame < count))
PY
)"
  python "$ROOT_DIR/scripts/tools/audit_c2_evidence_output_correctness.py" \
    --bundle-dir "$bundle_dir" \
    --output-dir "$audit_dir" \
    --sample-frames "$sample_frames" >/tmp/"${RUN_ID}-audit.log" 2>&1 || true
}

summarize_result() {
  local marker="$1"
  local mode="$2"
  local sink_output_dir="$3"
  local bundle_dir="$4"
  local audit_dir="$5"
  python - "$marker" "$mode" "$sink_output_dir" "$bundle_dir" "$audit_dir" "$REPORT_PATH" "$REQUEST_ID" "$SOURCE_EVENT_ID" "$POST_SAVANT_SOURCE_ID" "$EVENT_ID" "$REQUESTED_START_PTS" "$REQUESTED_END_PTS" <<'PY'
import json
import sys
from pathlib import Path

marker, mode = sys.argv[1:3]
sink_output_dir, bundle_dir, audit_dir, report_path = map(Path, sys.argv[3:7])
request_id, source_event_id, source_id, event_id = sys.argv[7:11]
requested_start_pts, requested_end_pts = sys.argv[11:13]
summary = json.loads((bundle_dir / "summary.json").read_text(encoding="utf-8"))
audit = {}
audit_path = audit_dir / "audit_summary.json"
if audit_path.is_file():
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
counts = summary.get("object_counts") or {}
video_integrity = summary.get("video_integrity") or {}
report = {
    "result_marker": marker,
    "mode": mode,
    "record_request_id": request_id,
    "source_event_id": source_event_id,
    "post_savant_replay_source_id": source_id,
    "event_id": event_id,
    "video_file_sink_output_dir": str(sink_output_dir),
    "final_evidence_bundle_dir": str(bundle_dir),
    "c2_3q_audit_output_dir": str(audit_dir),
    "audit_result_marker": audit.get("result_marker"),
    "requested_start_pts": requested_start_pts,
    "requested_end_pts": requested_end_pts,
    "actual_start_pts": (summary.get("time_window") or {}).get("actual_start_pts"),
    "actual_end_pts": (summary.get("time_window") or {}).get("actual_end_pts"),
    "duration_s": video_integrity.get("duration_s"),
    "decoded_video_frame_count": summary.get("decoded_video_frame_count"),
    "metadata_frame_count": summary.get("original_metadata_frame_count"),
    "sidecar_frame_count": summary.get("sidecar_frame_count"),
    "trim_occurred": summary.get("trim_occurred"),
    "time_domain_crop_applied": summary.get("time_domain_crop_applied"),
    "production_ready": summary.get("production_ready"),
    "timeline_reconciliation_status": summary.get("timeline_reconciliation_status"),
    "first_frame_keyframe": video_integrity.get("first_frame_keyframe"),
    "first_keyframe_pts_time": video_integrity.get("first_keyframe_pts_time"),
    "keyframe_count": video_integrity.get("keyframe_count"),
    "decode_error_count": video_integrity.get("decode_error_count"),
    "pts_large_jump_detected": video_integrity.get("pts_large_jump_detected"),
    "max_packet_duration_s": video_integrity.get("max_packet_duration_s"),
    "integrity_status": video_integrity.get("integrity_status"),
    "production_gate_passed": video_integrity.get("production_gate_passed"),
    "person_count": counts.get("person"),
    "face_count": counts.get("face"),
    "known_face_count": counts.get("known_face"),
    "keypoints_count": summary.get("keypoints_count"),
    "face_landmarks_count": summary.get("face_landmarks_count"),
    "fallback_used": summary.get("fallback_used", False),
    "legacy_used_for_visual_binding": summary.get("legacy_used_for_visual_binding"),
    "allow_db_annotation_fallback": summary.get("allow_db_annotation_fallback"),
    "allow_legacy_annotation_fallback": summary.get("allow_legacy_annotation_fallback"),
    "video_integrity_decode_log": str(bundle_dir / "video_integrity_decode_errors.log"),
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2, sort_keys=True))
PY
}

bundle_passed() {
  local bundle_dir="$1"
  local audit_dir="$2"
  python - "$bundle_dir" "$audit_dir" <<'PY'
import json
import sys
from pathlib import Path

bundle_dir, audit_dir = map(Path, sys.argv[1:3])
summary = json.loads((bundle_dir / "summary.json").read_text(encoding="utf-8"))
audit = json.loads((audit_dir / "audit_summary.json").read_text(encoding="utf-8"))
video_integrity = summary.get("video_integrity") or {}
fail = (
    not summary.get("production_ready")
    or not video_integrity.get("production_gate_passed")
    or bool(audit.get("failures"))
    or audit.get("result_marker") == "FAIL_C2_3Q_EVIDENCE_OUTPUT_AUDIT_BLOCKED"
)
raise SystemExit(1 if fail else 0)
PY
}

init_window

SCHEME_A_SINK=""
SCHEME_A_BUNDLE="$EVIDENCE_ROOT/${RUN_ID}_event_replay"
SCHEME_A_AUDIT="$AUDIT_ROOT/${RUN_ID}_event_replay"
if wait_for_replay; then
  PAYLOAD_PATH="$(create_replay_payload)"
  if submit_replay_job "$PAYLOAD_PATH"; then
    SCHEME_A_SINK="$(wait_for_sink_output || true)"
    if [[ -n "$SCHEME_A_SINK" ]]; then
      build_bundle "$SCHEME_A_SINK" "$SCHEME_A_BUNDLE" "event"
      run_audit "$SCHEME_A_BUNDLE" "$SCHEME_A_AUDIT"
      if bundle_passed "$SCHEME_A_BUNDLE" "$SCHEME_A_AUDIT"; then
        summarize_result "PASS_C2_3B_R2_KEYFRAME_SAFE_TIME_DOMAIN_REPLAY_READY" "event_style_replay" "$SCHEME_A_SINK" "$SCHEME_A_BUNDLE" "$SCHEME_A_AUDIT"
        exit 0
      fi
    fi
  fi
fi

if [[ ! -d "$STABLE_SINK_OUTPUT" ]]; then
  fail "FAIL_C2_3B_R2_TIME_DOMAIN_REPAIR_BLOCKED" "stable_sink_output_missing"
fi
WORKAROUND_BUNDLE="$EVIDENCE_ROOT/${RUN_ID}_stable_sink_crop"
WORKAROUND_AUDIT="$AUDIT_ROOT/${RUN_ID}_stable_sink_crop"
build_bundle "$STABLE_SINK_OUTPUT" "$WORKAROUND_BUNDLE" "workaround"
run_audit "$WORKAROUND_BUNDLE" "$WORKAROUND_AUDIT"
if bundle_passed "$WORKAROUND_BUNDLE" "$WORKAROUND_AUDIT"; then
  summarize_result "PARTIAL_C2_3B_R2_STABLE_SINK_WORKAROUND_READY" "stable_sink_time_crop" "$STABLE_SINK_OUTPUT" "$WORKAROUND_BUNDLE" "$WORKAROUND_AUDIT"
  exit 0
fi

summarize_result "FAIL_C2_3B_R2_TIME_DOMAIN_REPAIR_BLOCKED" "stable_sink_time_crop" "$STABLE_SINK_OUTPUT" "$WORKAROUND_BUNDLE" "$WORKAROUND_AUDIT"
exit 2
