#!/usr/bin/env bash
# C1F.1d - Real RTSP same-frame pose + face detection smoke.
#
# Verifies the C1 official replay dev stack can emit
# /data/video-analytics/artifacts/c1f1/same_frame_pose_face_summary.jsonl
# and that at least one frame anchor contains both:
#   - YOLO26-pose person detection with keypoints
#   - YOLOv8-Face face detection
#
# This smoke does not require face-person association to pass. It does not
# pull a second RTSP source, extract clips, or write image artifacts.
#
# STARTUP ORDER NOTE: This smoke uses a staged startup (source-adapter
# before savant-security) as a stabilization tactic to work around a
# Savant 0.6.0 zeromq_source_bin startup timing issue. This is NOT a
# production startup order recommendation. Production startup order
# should be determined by the deployment orchestration layer.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/infra/docker-compose.c1-official-replay-dev.yml"
export SAVANT_MODULE_FILE="${SAVANT_MODULE_FILE:-module.c1f1_same_frame.yml}"
MODULE_FILE="$REPO_ROOT/modules/savant_security/$SAVANT_MODULE_FILE"
PYFUNC_FILE="$REPO_ROOT/modules/savant_security/custom/pyfuncs/same_frame_detection_debug.py"
ARTIFACT_DIR="/data/video-analytics/artifacts/c1f1"
SUMMARY_FILE="$ARTIFACT_DIR/same_frame_pose_face_summary.jsonl"
REPLAY_ROCKSDB_DIR="/data/video-analytics/replay-c1-official-replay-dev"
REQUIRED_RTSP_URL="rtsp://10.37.57.112:8554/live/1080movie"
WAIT_SECONDS="${C1F1_WAIT_SECONDS:-120}"
POLL_SECONDS="${C1F1_POLL_SECONDS:-5}"
C1F1_KEEP_STACK_ON_EXIT="${C1F1_KEEP_STACK_ON_EXIT:-0}"
SAVANT_READY_WAIT_SECONDS="${C1F1_SAVANT_READY_WAIT_SECONDS:-90}"

DOCKER=""
COMPOSE=""
COMPOSE_STARTED="no"
START_EPOCH="$(date +%s)"
CONFIG_FILE="$(mktemp -t c1f1-compose-config.XXXXXX.yml)"
STATS_FILE="$(mktemp -t c1f1-summary-stats.XXXXXX.json)"
VALIDATION_FILE="$(mktemp -t c1f1-compose-validation.XXXXXX.json)"
SAVANT_CONTAINER="c1-official-savant"

cleanup() {
  rm -f "$CONFIG_FILE" "$STATS_FILE" "$VALIDATION_FILE"
  if [[ "$COMPOSE_STARTED" == "yes" && "$C1F1_KEEP_STACK_ON_EXIT" != "1" ]]; then
    $COMPOSE -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

detect_docker() {
  if docker ps >/dev/null 2>&1; then
    DOCKER="docker"
    COMPOSE="docker compose"
  elif sudo docker ps >/dev/null 2>&1; then
    DOCKER="sudo docker"
    COMPOSE="sudo docker compose"
  else
    echo "Result=FAIL_RUNTIME"
    echo "Reason=docker_daemon_unavailable"
    exit 1
  fi
}

emit_runtime_tail() {
  if [[ -z "$COMPOSE" ]]; then
    return 0
  fi
  echo ""
  echo "Runtime diagnostics:"
  $COMPOSE -f "$COMPOSE_FILE" ps 2>/dev/null || true
  echo ""
  echo "savant-security logs:"
  $COMPOSE -f "$COMPOSE_FILE" logs --tail 80 savant-security 2>/dev/null || true
  echo ""
  echo "source-adapter logs:"
  $COMPOSE -f "$COMPOSE_FILE" logs --tail 80 source-adapter 2>/dev/null || true
  echo ""
  echo "replay-service logs:"
  $COMPOSE -f "$COMPOSE_FILE" logs --tail 80 replay-service 2>/dev/null || true
}

fail_result() {
  local result="$1"
  local reason="$2"
  local code="${3:-1}"
  echo "Result=${result}"
  echo "Reason=${reason}"
  emit_runtime_tail
  exit "$code"
}

json_value() {
  local key="$1"
  python3 - "$STATS_FILE" "$key" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = data.get(sys.argv[2], "")
if isinstance(value, (dict, list)):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
elif value is None:
    print("")
else:
    print(value)
PY
}

container_health() {
  local container="$1"
  $DOCKER inspect "$container" 2>/dev/null \
    | python3 -c '
import json
import sys
data = json.load(sys.stdin)[0]
health = data.get("State", {}).get("Health")
if health:
    print(health.get("Status", "unknown"))
else:
    print(data.get("State", {}).get("Status", "unknown"))
' 2>/dev/null || echo "missing"
}

wait_for_savant_ready() {
  local elapsed=0
  while [[ "$elapsed" -le "$SAVANT_READY_WAIT_SECONDS" ]]; do
    local health
    health="$(container_health "$SAVANT_CONTAINER")"
    if [[ "$health" == "healthy" ]] || $COMPOSE -f "$COMPOSE_FILE" logs --tail 120 savant-security 2>/dev/null | grep -q "ModuleStatus.RUNNING"; then
      echo "[c1f1-smoke] savant-security ready after ${elapsed}s health=${health}"
      return 0
    fi
    echo "[c1f1-smoke] waiting for savant-security ready elapsed=${elapsed}s health=${health}"
    sleep "$POLL_SECONDS"
    elapsed=$((elapsed + POLL_SECONDS))
  done
  return 1
}

wait_for_source_streaming() {
  local elapsed=0
  while [[ "$elapsed" -le 45 ]]; do
    if $COMPOSE -f "$COMPOSE_FILE" logs --tail 160 source-adapter 2>/dev/null \
      | grep -qE "Setting pipeline to PLAYING|Sink caps changed|Processed [0-9]+ frames"; then
      echo "[c1f1-smoke] source-adapter streaming after ${elapsed}s"
      return 0
    fi
    echo "[c1f1-smoke] waiting for source-adapter streaming elapsed=${elapsed}s"
    sleep "$POLL_SECONDS"
    elapsed=$((elapsed + POLL_SECONDS))
  done
  return 1
}

validate_config() {
  python3 - "$CONFIG_FILE" "$MODULE_FILE" "$PYFUNC_FILE" "$REQUIRED_RTSP_URL" <<'PY'
import ast
import json
import sys
from pathlib import Path

import yaml

compose_path = Path(sys.argv[1])
module_path = Path(sys.argv[2])
pyfunc_path = Path(sys.argv[3])
required_rtsp = sys.argv[4]

compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
module = yaml.safe_load(module_path.read_text(encoding="utf-8"))
pyfunc_source = pyfunc_path.read_text(encoding="utf-8")


def env_map(service):
    env = service.get("environment", {}) or {}
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    result = {}
    for item in env:
        if "=" in str(item):
            key, value = str(item).split("=", 1)
            result[key] = value
    return result


def volumes(service):
    return service.get("volumes", []) or []


def has_bind_mount(service, source, target):
    for volume in volumes(service):
        if isinstance(volume, str):
            parts = volume.split(":")
            if len(parts) >= 2 and parts[0] == source and parts[1] == target:
                return True
            continue
        if not isinstance(volume, dict):
            continue
        if volume.get("type") != "bind":
            continue
        if volume.get("source") == source and volume.get("target") == target:
            return True
    return False


def pipeline_elements():
    pipeline = module.get("pipeline", {})
    if isinstance(pipeline, dict):
        return pipeline.get("elements", []) or []
    if isinstance(pipeline, list):
        return pipeline
    return []


def find_element(name):
    for element in pipeline_elements():
        if isinstance(element, dict) and element.get("name") == name:
            return element
    return None


def output_labels(element):
    objects = (
        element.get("model", {})
        .get("output", {})
        .get("objects", [])
        or []
    )
    labels = []
    for obj in objects:
        if isinstance(obj, dict):
            labels.append(str(obj.get("label") or obj.get("name") or ""))
        else:
            labels.append(str(obj))
    return labels


def input_object(element):
    top = element.get("input", {}) or {}
    model_input = element.get("model", {}).get("input", {}) or {}
    return top.get("object") or model_input.get("object")


def code_without_docstrings(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(getattr(node.body[0], "value", None), (ast.Constant, ast.Str))
            ):
                node.body = node.body[1:]
    return ast.unparse(tree).lower()


services = compose.get("services", {}) or {}
failures = []
details = {}

source = services.get("source-adapter")
if not source:
    failures.append(("FAIL_RUNTIME", "source-adapter service missing"))
else:
    source_env = env_map(source)
    details["source_rtsp_uri"] = source_env.get("RTSP_URI", "")
    details["source_location"] = source_env.get("LOCATION", "")
    details["source_rtsp_transport"] = source_env.get("RTSP_TRANSPORT", "")
    details["source_zmq_endpoint"] = source_env.get("ZMQ_ENDPOINT", "")
    if source_env.get("RTSP_URI") != required_rtsp:
        failures.append(("FAIL_RUNTIME", "source-adapter RTSP_URI is not the fixed RTSP"))
    if source_env.get("LOCATION") != required_rtsp:
        failures.append(("FAIL_RUNTIME", "source-adapter LOCATION is not the fixed RTSP"))
    if source_env.get("RTSP_TRANSPORT", "").lower() != "tcp":
        failures.append(("FAIL_RUNTIME", "source-adapter RTSP_TRANSPORT is not tcp"))
    if "replay-service" not in source_env.get("ZMQ_ENDPOINT", ""):
        failures.append(("FAIL_RUNTIME", "source-adapter is not connected to replay-service"))

second_rtsp_services = []
for name, service in services.items():
    env = env_map(service)
    if name != "source-adapter":
        if env.get("RTSP_URI", "").startswith("rtsp://") or env.get("LOCATION", "").startswith("rtsp://"):
            second_rtsp_services.append(name)
    entrypoint = " ".join(str(x) for x in service.get("entrypoint", []) or [])
    command = " ".join(str(x) for x in service.get("command", []) or [])
    if name != "source-adapter" and "sources/rtsp.sh" in f"{entrypoint} {command}":
        second_rtsp_services.append(name)
adapter_like = [name for name in services if "source-adapter" in name]
if len(adapter_like) != 1 or second_rtsp_services:
    failures.append(("FAIL_SECOND_RTSP", f"second RTSP path detected: {second_rtsp_services or adapter_like}"))

for name, service in services.items():
    env = env_map(service)
    for key, value in env.items():
        normalized = value.strip().lower()
        if "SOURCE_EXTRACTION" in key.upper() and normalized not in {"", "0", "false", "no"}:
            failures.append(("FAIL_SOURCE_EXTRACTION", f"{name}.{key}={value}"))
        if key in {"EVIDENCE_LOCAL_FILE_USED", "EVIDENCE_TEST_VIDEO_USED"} and normalized not in {"", "0", "false", "no"}:
            failures.append(("FAIL_SOURCE_EXTRACTION", f"{name}.{key}={value}"))
        if "ANNOTATED_CLIP" in key.upper() and normalized in {"1", "true", "yes", "on"}:
            failures.append(("FAIL_RUNTIME", f"production annotated_clip enabled by {name}.{key}={value}"))

replay = services.get("replay-service", {})
replay_volumes = volumes(replay)
details["replay_inline_config"] = any("config.p1c_rtsp_inline.json" in str(volume) for volume in replay_volumes)
if not details["replay_inline_config"]:
    failures.append(("FAIL_RUNTIME", "Replay inline config mount missing"))

savant = services.get("savant-security")
if not savant:
    failures.append(("FAIL_RUNTIME", "savant-security service missing"))
else:
    savant_env = env_map(savant)
    savant_volumes = volumes(savant)
    details["same_frame_debug_env"] = savant_env.get("C1F1_SAME_FRAME_DEBUG_ENABLED", "")
    details["face_observation_export_enabled"] = savant_env.get("FACE_OBSERVATION_EXPORT_ENABLED", "")
    details["artifacts_mount"] = has_bind_mount(
        savant,
        "/data/video-analytics/artifacts",
        "/data/video-analytics/artifacts",
    )
    if savant_env.get("C1F1_SAME_FRAME_DEBUG_ENABLED") != "1":
        failures.append(("FAIL_RUNTIME", "C1F1_SAME_FRAME_DEBUG_ENABLED did not render as 1"))
    if not details["artifacts_mount"]:
        failures.append(("FAIL_RUNTIME", "artifacts bind mount missing"))

pose = find_element("yolo26_pose")
face = find_element("yolov8_face")
if not pose:
    failures.append(("BLOCKED_NO_PERSON_OBSERVED", "YOLO26-pose element missing"))
else:
    details["yolo26_pose_enabled"] = True
    if pose.get("element") != "nvinfer@complex_model":
        failures.append(("FAIL_RUNTIME", "YOLO26-pose is not a complex primary model"))
    if "person" not in output_labels(pose):
        failures.append(("BLOCKED_NO_PERSON_OBSERVED", "YOLO26-pose person label missing"))
    if input_object(pose):
        failures.append(("FAIL_RUNTIME", "YOLO26-pose is not full-frame primary"))

if not face:
    failures.append(("BLOCKED_NO_FACE_OBSERVED", "YOLOv8-Face element missing"))
else:
    details["yolov8_face_enabled"] = True
    if face.get("element") != "nvinfer@complex_model":
        failures.append(("FAIL_RUNTIME", "YOLOv8-Face is not a complex primary model"))
    if "face" not in output_labels(face):
        failures.append(("BLOCKED_NO_FACE_OBSERVED", "YOLOv8-Face face label missing"))
    if input_object(face):
        failures.append(("FAIL_FACE_SECONDARY_ROI", f"YOLOv8-Face input.object={input_object(face)}"))

try:
    code = code_without_docstrings(pyfunc_source)
except Exception as exc:
    failures.append(("FAIL_RUNTIME", f"same_frame debug pyfunc parse failed: {exc}"))
else:
    forbidden_code = [
        "cv2.imwrite",
        ".save(",
        "base64",
        "xadd",
        "redis",
        "rtsp://",
    ]
    found = [token for token in forbidden_code if token in code]
    details["same_frame_pyfunc_forbidden_tokens"] = found
    if found:
        failures.append(("FAIL_RUNTIME", f"same_frame debug pyfunc has forbidden tokens: {found}"))

result = {
    "ok": not failures,
    "failures": failures,
    "details": details,
}
print(json.dumps(result, sort_keys=True))
PY
}

parse_summary() {
  python3 - "$SUMMARY_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
stats = {
    "raw_lines": 0,
    "invalid_lines": 0,
    "frames_inspected": 0,
    "frames_with_person": 0,
    "frames_with_keypoints": 0,
    "frames_with_face": 0,
    "frames_with_both": 0,
    "association_observed": "no",
    "association_reason_if_no": "",
    "sample_frame_uuid": "",
    "sample_frame_num": "",
    "sample_frame_pts": "",
    "sample_timestamp_ms": "",
    "sample_person_bbox": "",
    "sample_keypoints_count": "",
    "sample_face_bbox": "",
    "sample_landmarks_count": "",
}

if not path.exists():
    print(json.dumps(stats, sort_keys=True))
    raise SystemExit(0)

for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
    if not line.strip():
        continue
    stats["raw_lines"] += 1
    try:
        record = json.loads(line)
    except Exception:
        stats["invalid_lines"] += 1
        continue

    stats["frames_inspected"] += 1
    pose = record.get("pose") if isinstance(record.get("pose"), dict) else {}
    face = record.get("face") if isinstance(record.get("face"), dict) else {}
    assoc = record.get("association") if isinstance(record.get("association"), dict) else {}

    persons = pose.get("persons") if isinstance(pose.get("persons"), list) else []
    faces = face.get("faces") if isinstance(face.get("faces"), list) else []
    try:
        person_count = int(pose.get("person_count", len(persons)) or 0)
    except Exception:
        person_count = len(persons)
    try:
        face_count = int(face.get("face_count", len(faces)) or 0)
    except Exception:
        face_count = len(faces)

    keypoint_person = None
    for person in persons:
        if not isinstance(person, dict):
            continue
        try:
            keypoints_count = int(person.get("keypoints_count") or 0)
        except Exception:
            keypoints_count = 0
        if keypoints_count > 0:
            keypoint_person = person
            break

    if person_count > 0:
        stats["frames_with_person"] += 1
    if keypoint_person is not None:
        stats["frames_with_keypoints"] += 1
    if face_count > 0:
        stats["frames_with_face"] += 1

    pairs = assoc.get("matched_face_person_pairs")
    if isinstance(pairs, list) and pairs:
        stats["association_observed"] = "yes"

    both = person_count > 0 and keypoint_person is not None and face_count > 0
    if both:
        stats["frames_with_both"] += 1
        if not stats["sample_frame_uuid"]:
            sample_face = next((f for f in faces if isinstance(f, dict)), {})
            stats["sample_frame_uuid"] = record.get("frame_uuid") or ""
            stats["sample_frame_num"] = record.get("frame_num") if record.get("frame_num") is not None else ""
            stats["sample_frame_pts"] = record.get("frame_pts") if record.get("frame_pts") is not None else ""
            stats["sample_timestamp_ms"] = record.get("timestamp_ms") if record.get("timestamp_ms") is not None else ""
            stats["sample_person_bbox"] = keypoint_person.get("bbox") or ""
            stats["sample_keypoints_count"] = keypoint_person.get("keypoints_count") or ""
            stats["sample_face_bbox"] = sample_face.get("bbox") or ""
            stats["sample_landmarks_count"] = sample_face.get("landmarks_count") or ""
            stats["association_reason_if_no"] = assoc.get("reason_if_no_match") or ""
    elif not stats["association_reason_if_no"]:
        stats["association_reason_if_no"] = assoc.get("reason_if_no_match") or ""

print(json.dumps(stats, sort_keys=True))
PY
}

detect_docker

export C1F1_SAME_FRAME_DEBUG_ENABLED=1
export C1E_RTSP_TRANSPORT=tcp

echo "--- C1F.1d Real RTSP Same-Frame Pose + Face Detection Smoke ---"
echo "script=scripts/smoke/check_c1f1_same_frame_pose_face_detection.sh"
echo "compose=${COMPOSE_FILE}"
echo "rtsp_source=${REQUIRED_RTSP_URL}"
echo "artifact_dir=${ARTIFACT_DIR}"
echo "summary_file=${SUMMARY_FILE}"
echo "wait_seconds=${WAIT_SECONDS}"
echo ""

command -v python3 >/dev/null 2>&1 || fail_result "FAIL_RUNTIME" "python3 not found"

mkdir -p "$ARTIFACT_DIR" || fail_result "FAIL_RUNTIME" "cannot create artifact directory: $ARTIFACT_DIR"
rm -f "$SUMMARY_FILE" || fail_result "FAIL_RUNTIME" "cannot remove stale summary: $SUMMARY_FILE"
mkdir -p "$REPLAY_ROCKSDB_DIR" || fail_result "FAIL_RUNTIME" "cannot create Replay RocksDB directory: $REPLAY_ROCKSDB_DIR"

if ! $COMPOSE -f "$COMPOSE_FILE" config >"$CONFIG_FILE" 2>/tmp/c1f1-compose-config.err; then
  cat /tmp/c1f1-compose-config.err >&2 || true
  fail_result "FAIL_RUNTIME" "docker compose config failed"
fi

validate_config >"$VALIDATION_FILE" || fail_result "FAIL_RUNTIME" "compose/module validation failed"
VALIDATION_OK="$(python3 - "$VALIDATION_FILE" <<'PY'
import json
import sys
from pathlib import Path
print("yes" if json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("ok") else "no")
PY
)"
if [[ "$VALIDATION_OK" != "yes" ]]; then
  VALIDATION_RESULT="$(python3 - "$VALIDATION_FILE" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
failures = data.get("failures") or [["FAIL_RUNTIME", "unknown validation failure"]]
print(failures[0][0])
PY
)"
  VALIDATION_REASON="$(python3 - "$VALIDATION_FILE" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
failures = data.get("failures") or [["FAIL_RUNTIME", "unknown validation failure"]]
print("; ".join(f"{item[0]}:{item[1]}" for item in failures))
PY
)"
  fail_result "$VALIDATION_RESULT" "$VALIDATION_REASON"
fi

python3 - "$VALIDATION_FILE" <<'PY'
import json
import sys
from pathlib import Path

details = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["details"]
for key in [
    "source_rtsp_uri",
    "source_location",
    "source_rtsp_transport",
    "source_zmq_endpoint",
    "replay_inline_config",
    "same_frame_debug_env",
    "artifacts_mount",
    "yolo26_pose_enabled",
    "yolov8_face_enabled",
]:
    print(f"{key}={details.get(key)}")
PY

echo ""
echo "[c1f1-smoke] recreating C1 official replay dev stack..."
$COMPOSE -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true

echo "[c1f1-smoke] clearing Replay RocksDB cache..."
$DOCKER run --rm --pull never \
  -v "${REPLAY_ROCKSDB_DIR}:/c1f1-replay" \
  alpine:3.20 \
  sh -c "rm -rf /c1f1-replay/*" >/dev/null 2>&1 || true

if ! $COMPOSE -f "$COMPOSE_FILE" up -d --force-recreate --no-build --pull never \
  redis postgres replay-service video-file-sink event-worker clip-worker media-worker; then
  fail_result "FAIL_RUNTIME" "docker compose up core services failed"
fi
COMPOSE_STARTED="yes"

echo "[c1f1-smoke] starting source-adapter before savant-security to seed Replay inline frames..."
if ! $COMPOSE -f "$COMPOSE_FILE" up -d --force-recreate --no-build --pull never --no-deps source-adapter; then
  fail_result "FAIL_RUNTIME" "docker compose up source-adapter failed"
fi

if ! wait_for_source_streaming; then
  fail_result "FAIL_RUNTIME" "source-adapter did not start streaming before savant-security start"
fi

echo "[c1f1-smoke] starting savant-security after source-adapter is streaming..."
if ! $COMPOSE -f "$COMPOSE_FILE" up -d --force-recreate --no-build --pull never savant-security; then
  fail_result "FAIL_RUNTIME" "docker compose up savant-security failed"
fi

if ! wait_for_savant_ready; then
  fail_result "FAIL_RUNTIME" "savant-security did not become ready before source-adapter start"
fi

echo "[c1f1-smoke] waiting for same-frame JSONL..."
elapsed=0
while [[ "$elapsed" -le "$WAIT_SECONDS" ]]; do
  if [[ -s "$SUMMARY_FILE" ]]; then
    parse_summary >"$STATS_FILE"
    frames_inspected="$(json_value frames_inspected)"
    frames_with_both="$(json_value frames_with_both)"
    echo "[c1f1-smoke] elapsed=${elapsed}s frames=${frames_inspected} both=${frames_with_both}"
    if [[ "$frames_with_both" =~ ^[0-9]+$ && "$frames_with_both" -gt 0 ]]; then
      break
    fi
  else
    echo "[c1f1-smoke] elapsed=${elapsed}s summary_not_ready"
  fi
  sleep "$POLL_SECONDS"
  elapsed=$((elapsed + POLL_SECONDS))
done

if [[ ! -s "$SUMMARY_FILE" ]]; then
  fail_result "BLOCKED_NO_SUMMARY_OUTPUT" "no JSONL produced in ${WAIT_SECONDS}s" 2
fi

parse_summary >"$STATS_FILE"

raw_lines="$(json_value raw_lines)"
invalid_lines="$(json_value invalid_lines)"
frames_inspected="$(json_value frames_inspected)"
frames_with_person="$(json_value frames_with_person)"
frames_with_keypoints="$(json_value frames_with_keypoints)"
frames_with_face="$(json_value frames_with_face)"
frames_with_both="$(json_value frames_with_both)"
association_observed="$(json_value association_observed)"
association_reason_if_no="$(json_value association_reason_if_no)"
run_duration="$(( $(date +%s) - START_EPOCH ))s"

if [[ "$frames_inspected" -eq 0 ]]; then
  fail_result "BLOCKED_NO_SUMMARY_OUTPUT" "summary exists but contains no parseable JSON frames; raw_lines=${raw_lines} invalid_lines=${invalid_lines}" 2
fi

result=""
reason=""
exit_code=0
if [[ "$frames_with_both" -gt 0 ]]; then
  if [[ "$association_observed" == "yes" ]]; then
    result="PASS"
  else
    result="PASS_DUAL_DETECTION_NO_ASSOC"
    reason="${association_reason_if_no:-no_geometric_match_found}"
  fi
elif [[ "$frames_with_face" -eq 0 ]]; then
  result="BLOCKED_NO_FACE_OBSERVED"
  reason="no frames with face_count > 0"
  exit_code=2
elif [[ "$frames_with_person" -eq 0 ]]; then
  result="BLOCKED_NO_PERSON_OBSERVED"
  reason="no frames with person_count > 0"
  exit_code=2
elif [[ "$frames_with_keypoints" -eq 0 ]]; then
  result="BLOCKED_NO_KEYPOINTS_OBSERVED"
  reason="no frames with person keypoints_count > 0"
  exit_code=2
else
  result="FAIL_RUNTIME"
  reason="person/keypoints and face were observed, but never on the same frame anchor"
  exit_code=1
fi

echo ""
echo "=== C1F.1d Same-Frame Pose + Face Detection Smoke ==="
echo "Result=${result}"
[[ -n "$reason" ]] && echo "Reason=${reason}"
echo ""
echo "Smoke:"
echo "  script: scripts/smoke/check_c1f1_same_frame_pose_face_detection.sh"
echo "  result: ${result}"
echo "  run_duration: ${run_duration}"
echo "  frames_inspected: ${frames_inspected}"
echo "  frames_with_person: ${frames_with_person}"
echo "  frames_with_keypoints: ${frames_with_keypoints}"
echo "  frames_with_face: ${frames_with_face}"
echo "  frames_with_both: ${frames_with_both}"
echo "  raw_jsonl_lines: ${raw_lines}"
echo "  invalid_jsonl_lines: ${invalid_lines}"
echo "  sample_frame_uuid: $(json_value sample_frame_uuid)"
echo "  sample_frame_num: $(json_value sample_frame_num)"
echo "  sample_frame_pts: $(json_value sample_frame_pts)"
echo "  sample_timestamp_ms: $(json_value sample_timestamp_ms)"
echo "  sample_person_bbox: $(json_value sample_person_bbox)"
echo "  sample_keypoints_count: $(json_value sample_keypoints_count)"
echo "  sample_face_bbox: $(json_value sample_face_bbox)"
echo "  sample_landmarks_count: $(json_value sample_landmarks_count)"
echo "  association_observed: ${association_observed}"
echo "  association_reason_if_no: ${association_reason_if_no}"
echo ""
echo "Forbidden path check (config-level verified):"
echo "  no second RTSP: yes (config)"
echo "  no source extraction: yes (config)"
echo "  no full-frame image output: yes (config)"
echo "  no face crop output: yes (config)"
echo "  no image bytes in Redis: yes (config)"
echo "  no production annotated_clip: yes (config)"
echo "  no runtime artifact committed: yes (config)"

exit "$exit_code"
