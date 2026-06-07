#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose.c2-post-savant-replay-poc.yml"
ENV_FILE="$ROOT_DIR/infra/env/c2-post-savant-replay-poc.env"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

REPLAY_API_URL="${C2_POC_REPLAY_API_URL:-http://127.0.0.1:8098}"
SOURCE_ID="${C2_POC_SOURCE_ID:-c2_post_savant_replay_poc}"
SINK_URL="${C2_POC_SINK_URL:-dealer+connect:tcp://video-file-sink:6666}"
SINK_ROOT="${C2_POC_SINK_ROOT:-/data/video-analytics/media/c2-post-savant-replay-poc}"
JOB_SECONDS="${C2_POC_JOB_SECONDS:-5}"
C2_POC_REPLAY_FPS="${C2_POC_REPLAY_FPS:-24}"
KEYFRAME_WAIT_SECONDS="${C2_POC_KEYFRAME_WAIT_SECONDS:-90}"
RUN_ID="c2-poc-$(date +%Y%m%dT%H%M%S)"
REPORT_PATH="/tmp/${RUN_ID}-metadata-retention.json"

compose() {
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

wait_for_replay() {
  local deadline=$((SECONDS + 60))
  until curl --noproxy '*' -fsS "$REPLAY_API_URL/api/v1/status" >/tmp/c2-poc-replay-status.json; do
    if (( SECONDS >= deadline )); then
      echo "BLOCKED_C2_POC_STACK_FAILED replay status unavailable" >&2
      return 1
    fi
    sleep 2
  done
}

wait_for_keyframe() {
  local deadline=$((SECONDS + KEYFRAME_WAIT_SECONDS))
  local response
  while (( SECONDS < deadline )); do
    response="$(curl --noproxy '*' -fsS \
      -H 'content-type: application/json' \
      -d "{\"source_id\":\"${SOURCE_ID}\",\"limit\":1}" \
      "$REPLAY_API_URL/api/v1/keyframes/find" 2>/dev/null || true)"
    if [[ -n "$response" ]]; then
      python - "$response" <<'PY'
import json
import sys

try:
    data = json.loads(sys.argv[1])
except Exception:
    sys.exit(1)
kfs = data.get("keyframes")
uuid = ""
if isinstance(kfs, list) and len(kfs) > 1 and isinstance(kfs[1], list) and kfs[1]:
    uuid = str(kfs[1][0])
elif isinstance(kfs, list) and kfs and isinstance(kfs[0], str):
    uuid = str(kfs[0])
uuid = data.get("keyframe_uuid") or data.get("uuid") or uuid
if uuid:
    print(uuid)
else:
    sys.exit(1)
PY
      return 0
    fi
    sleep 3
  done
  echo "BLOCKED_C2_SAVANT_TO_REPLAY_ZMQ_LINK_FAILED no keyframe for source_id=${SOURCE_ID}" >&2
  return 1
}

create_replay_job() {
  local keyframe_uuid="$1"
  local payload_file="/tmp/${RUN_ID}-job.json"
  python - "$SOURCE_ID" "$keyframe_uuid" "$SINK_URL" "$RUN_ID" "$JOB_SECONDS" "$C2_POC_REPLAY_FPS" >"$payload_file" <<'PY'
import json
import sys

source_id, keyframe_uuid, sink_url, run_id, seconds, fps = sys.argv[1:7]
seconds_f = float(seconds)
fps_i = max(int(float(fps)), 1)
frame_duration = {"secs": 0, "nanos": int(round(1_000_000_000 / fps_i))}
payload = {
    "sink": {
        "url": sink_url,
        "options": {
            "send_timeout": {"secs": 5, "nanos": 0},
            "send_retries": 5,
            "receive_timeout": {"secs": 5, "nanos": 0},
            "receive_retries": 5,
            "send_hwm": 10000,
            "receive_hwm": 10000,
            "inflight_ops": 100,
        },
    },
    "configuration": {
        "ts_sync": True,
        "skip_intermediary_eos": False,
        "send_eos": True,
        "stop_on_incorrect_ts": False,
        "stored_stream_id": source_id,
        "resulting_stream_id": run_id,
        "routing_labels": "bypass",
        "max_idle_duration": {"secs": 10, "nanos": 0},
        "max_delivery_duration": {"secs": 30, "nanos": 0},
        "send_metadata_only": False,
        "ts_discrepancy_fix_duration": frame_duration,
        "min_duration": frame_duration,
        "max_duration": frame_duration,
        "labels": {"run_id": run_id, "phase": "c2.0"},
    },
    "stop_condition": {"frame_count": int(round(seconds_f * fps_i))},
    "anchor_keyframe": keyframe_uuid,
    "anchor_wait_duration": {"secs": 1, "nanos": 0},
    "offset": {"seconds": 0.0},
    "attributes": [],
}
print(json.dumps(payload, separators=(",", ":")))
PY
  curl --noproxy '*' -fsS \
    -X PUT \
    -H 'content-type: application/json' \
    --data-binary "@${payload_file}" \
    "$REPLAY_API_URL/api/v1/job" >/tmp/"${RUN_ID}-job-response.json"
  echo "$payload_file"
}

find_metadata() {
  local deadline=$((SECONDS + 90))
  while (( SECONDS < deadline )); do
    local found
    found="$(find "$SINK_ROOT" -path "*${RUN_ID}*" -name metadata.json -type f -print 2>/dev/null | head -1 || true)"
    if [[ -n "$found" ]]; then
      echo "$found"
      return 0
    fi
    sleep 2
  done
  return 1
}

inspect_metadata() {
  local metadata_path="$1"
  local job_payload_path="$2"
  python - "$metadata_path" "$REPORT_PATH" "$SOURCE_ID" "$RUN_ID" "$job_payload_path" <<'PY'
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

metadata_path = Path(sys.argv[1])
report_path = Path(sys.argv[2])
source_id = sys.argv[3]
run_id = sys.argv[4]
job_payload_path = Path(sys.argv[5])


def load_json_or_jsonl(path: Path) -> Any:
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def object_label(obj: dict[str, Any]) -> str:
    for key in ("label", "class_label", "element_name", "name"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    namespace = obj.get("namespace")
    label = obj.get("label")
    if namespace or label:
        return f"{namespace}.{label}"
    return ""


def looks_like_object(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if any(key in value for key in ("bbox", "box", "detection_box", "confidence", "track_id")):
        return True
    if "label" in value and any(key in value for key in ("attributes", "children", "parent")):
        return True
    return False


def flatten_objects(value: Any) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if looks_like_object(value):
            objects.append(value)
        direct = value.get("objects")
        if isinstance(direct, list):
            objects.extend([item for item in direct if isinstance(item, dict)])
        for item in value.values():
            if isinstance(item, (dict, list)):
                objects.extend(flatten_objects(item))
    elif isinstance(value, list):
        for item in value:
            if looks_like_object(item):
                objects.append(item)
            if isinstance(item, (dict, list)):
                objects.extend(flatten_objects(item))
    return objects


def flatten_frames(data: Any) -> list[Any]:
    if isinstance(data, dict):
        for key in ("frames", "metadata", "records"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    if isinstance(data, list):
        return data
    return []


def has_any_key(value: Any, names: set[str]) -> bool:
    return any(isinstance(item, dict) and any(name in item for name in names) for item in walk(value))


def has_attribute(value: Any, name: str) -> bool:
    for item in walk(value):
        if isinstance(item, dict) and item.get("name") == name and ("value" in item or "values" in item):
            return True
    return False


def first_object_sample(objects: list[dict[str, Any]]) -> dict[str, Any]:
    if not objects:
        return {}
    obj = dict(objects[0])
    for key in list(obj.keys()):
        if key.lower() in {"embedding", "feature", "vector"}:
            obj[key] = "<omitted>"
    return obj


data = load_json_or_jsonl(metadata_path)
frames = flatten_frames(data)
frame_objects: list[list[dict[str, Any]]] = [flatten_objects(frame) for frame in frames]
objects = [obj for objs in frame_objects for obj in objs]
labels = Counter(object_label(obj) for obj in objects)
person_objects = [
    obj for obj in objects
    if "person" in object_label(obj).lower() or obj.get("class_id") == 0 and "landmarks" not in json.dumps(obj).lower()
]
face_objects = [obj for obj in objects if "face" in object_label(obj).lower()]
person_bbox_exists = has_any_key(person_objects, {"bbox", "box", "detection_box"})
person_keypoints_exists = has_attribute(person_objects, "keypoints") or has_any_key(person_objects, {"keypoints"})
literal_track_id_exists = has_any_key(objects, {"track_id"})
object_id_identity_exists = has_any_key(person_objects, {"object_id"})
person_track_id_attr_exists = has_attribute(face_objects, "person_track_id")
track_identity_available = literal_track_id_exists or object_id_identity_exists or person_track_id_attr_exists

video_files = sorted(
    str(path)
    for path in metadata_path.parent.iterdir()
    if path.is_file() and path.name != "metadata.json"
)
summary = {
    "status": "PASS_C2_POST_SAVANT_REPLAY_METADATA_RETAINED"
    if person_objects and person_bbox_exists and person_keypoints_exists and track_identity_available
    else "FAIL_C2_POST_SAVANT_REPLAY_METADATA_DROPPED",
    "metadata_path": str(metadata_path),
    "output_dir": str(metadata_path.parent),
    "video_files": video_files,
    "source_id_expected": source_id,
    "run_id": run_id,
    "job_payload_path": str(job_payload_path),
    "job_payload": json.loads(job_payload_path.read_text()),
    "frames_count": len(frames),
    "frames_with_objects_count": sum(1 for objs in frame_objects if objs),
    "total_objects_count": len(objects),
    "person_objects_count": len(person_objects),
    "face_objects_count": len(face_objects),
    "labels": dict(labels),
    "bbox_exists": has_any_key(objects, {"bbox", "box", "detection_box"}),
    "person_bbox_exists": person_bbox_exists,
    "track_id_exists": literal_track_id_exists,
    "object_id_identity_exists": object_id_identity_exists,
    "person_track_id_attribute_exists": person_track_id_attr_exists,
    "track_identity_available": track_identity_available,
    "track_identity_note": (
        "legacy video-file-sink metadata uses object_id/person_track_id attributes; "
        "literal track_id is not emitted"
    ) if track_identity_available and not literal_track_id_exists else "",
    "keypoints_exists": person_keypoints_exists,
    "landmarks_exists": has_attribute(face_objects, "landmarks") or has_any_key(face_objects, {"landmarks"}),
    "source_observation_id_exists": has_any_key(objects, {"source_observation_id"}),
    "frame_uuid_exists": has_any_key(frames, {"frame_uuid", "keyframe_uuid", "previous_keyframe_uuid"}),
    "frame_pts_exists": has_any_key(frames, {"pts", "pts_ms", "frame_pts", "frame_pts_ms"}),
    "object_sample": first_object_sample(objects),
}
report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
print(json.dumps(summary, indent=2, ensure_ascii=False))
PY
}

main() {
  mkdir -p "$SINK_ROOT"
  echo "compose_file=$COMPOSE_FILE"
  echo "env_file=$ENV_FILE"
  echo "replay_api_url=$REPLAY_API_URL"
  echo "source_id=$SOURCE_ID"
  compose ps
  wait_for_replay
  keyframe_uuid="$(wait_for_keyframe)"
  echo "keyframe_uuid=$keyframe_uuid"
  job_payload_path="$(create_replay_job "$keyframe_uuid")"
  echo "job_payload_path=$job_payload_path"
  metadata_path="$(find_metadata)" || {
    echo "BLOCKED_C2_REPLAY_JOB_TO_SINK_FAILED no metadata.json under $SINK_ROOT for run_id=$RUN_ID" >&2
    exit 1
  }
  inspect_metadata "$metadata_path" "$job_payload_path"
  echo "report_path=$REPORT_PATH"
}

main "$@"
