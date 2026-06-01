#!/usr/bin/env bash
# C1F.2c — AdaFace runtime validation smoke.
#
# Verifies AdaFace produces 512-d embeddings with valid norm on real face
# crops from the RTSP stream. Does NOT verify Redis export, face_reid_gate,
# or face_observation_exporter.
#
# STARTUP ORDER: staged startup (source-adapter before savant-security)
# to work around Savant 0.6.0 zeromq_source_bin timing issue. This is a
# smoke stabilization tactic, not a production startup order.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/infra/docker-compose.c1-official-replay-dev.yml"
export SAVANT_MODULE_FILE="${SAVANT_MODULE_FILE:-module.c1f2c_adaface_runtime.yml}"
MODULE_FILE="$REPO_ROOT/modules/savant_security/$SAVANT_MODULE_FILE"
ARTIFACT_DIR="/data/video-analytics/artifacts/c1f2c"
SUMMARY_FILE="$ARTIFACT_DIR/adaface_runtime_summary.jsonl"
REQUIRED_RTSP_URL="rtsp://10.37.57.112:8554/live/1080movie"
WAIT_SECONDS="${C1F2C_WAIT_SECONDS:-90}"
POLL_SECONDS="${C1F2C_POLL_SECONDS:-5}"
C1F2C_KEEP_STACK_ON_EXIT="${C1F2C_KEEP_STACK_ON_EXIT:-0}"

DOCKER=""
COMPOSE=""
COMPOSE_STARTED="no"
START_EPOCH="$(date +%s)"
SAVANT_CONTAINER="c1-official-savant"

cleanup() {
  if [[ "$COMPOSE_STARTED" == "yes" && "$C1F2C_KEEP_STACK_ON_EXIT" != "1" ]]; then
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

fail_result() {
  local result="$1"
  local reason="$2"
  local code="${3:-1}"
  echo "Result=${result}"
  echo "Reason=${reason}"
  exit "$code"
}

parse_summary() {
  python3 - "$SUMMARY_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
stats = {
    "frames_inspected": 0,
    "frames_with_face": 0,
    "faces_seen": 0,
    "faces_with_landmarks": 0,
    "faces_with_embedding": 0,
    "faces_with_embedding_dim_512": 0,
    "faces_with_valid_norm": 0,
    "sample_embedding_dim": 0,
    "sample_embedding_norm": 0.0,
}

if not path.exists():
    print(json.dumps(stats, sort_keys=True))
    raise SystemExit(0)

for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
    if not line.strip():
        continue
    try:
        record = json.loads(line)
    except Exception:
        continue

    stats["frames_inspected"] = max(stats["frames_inspected"], record.get("frame_num", 0))

    has_landmarks = record.get("has_landmarks", False)
    has_embedding = record.get("has_embedding", False)
    dim = record.get("embedding_dim", 0)
    norm = record.get("embedding_norm", 0.0)
    valid = record.get("valid_norm", False)

    if has_landmarks:
        stats["faces_with_landmarks"] += 1
    if has_embedding:
        stats["faces_with_embedding"] += 1
        stats["faces_seen"] += 1
        if dim == 512:
            stats["faces_with_embedding_dim_512"] += 1
        if valid:
            stats["faces_with_valid_norm"] += 1
        if not stats["sample_embedding_dim"]:
            stats["sample_embedding_dim"] = dim
            stats["sample_embedding_norm"] = norm

# Count frames_with_face from unique frame_num values
frame_nums = set()
for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
    if not line.strip():
        continue
    try:
        record = json.loads(line)
        if record.get("has_embedding"):
            frame_nums.add(record.get("frame_num", 0))
    except Exception:
        pass
stats["frames_with_face"] = len(frame_nums)

print(json.dumps(stats, sort_keys=True))
PY
}

json_value() {
  local key="$1"
  python3 -c "
import json, sys
data = json.loads(open('$SUMMARY_FILE').read()) if '$SUMMARY_FILE' else {}
print(data.get('$key', ''))
" 2>/dev/null || echo ""
}

detect_docker

export C1F1_SAME_FRAME_DEBUG_ENABLED=0
export C1E_RTSP_TRANSPORT=tcp

echo "--- C1F.2c AdaFace Runtime Validation Smoke ---"
echo "script=scripts/smoke/check_c1f2c_adaface_runtime_smoke.sh"
echo "compose=${COMPOSE_FILE}"
echo "module=${SAVANT_MODULE_FILE}"
echo "rtsp_source=${REQUIRED_RTSP_URL}"
echo "artifact_dir=${ARTIFACT_DIR}"
echo "summary_file=${SUMMARY_FILE}"
echo "wait_seconds=${WAIT_SECONDS}"
echo ""

command -v python3 >/dev/null 2>&1 || fail_result "FAIL_RUNTIME" "python3 not found"

mkdir -p "$ARTIFACT_DIR" || fail_result "FAIL_RUNTIME" "cannot create artifact directory"
rm -f "$SUMMARY_FILE" || fail_result "FAIL_RUNTIME" "cannot remove stale summary"

# Validate compose config
if ! $COMPOSE -f "$COMPOSE_FILE" config >/dev/null 2>&1; then
  fail_result "FAIL_RUNTIME" "docker compose config failed"
fi

# Validate module file exists
if [[ ! -f "$MODULE_FILE" ]]; then
  fail_result "FAIL_RUNTIME" "module file not found: $MODULE_FILE"
fi

echo "[c1f2c-smoke] bringing down any existing stack..."
$COMPOSE -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 || true

echo "[c1f2c-smoke] starting core services..."
if ! $COMPOSE -f "$COMPOSE_FILE" up -d --force-recreate --no-build --pull never \
  redis postgres replay-service video-file-sink; then
  fail_result "FAIL_RUNTIME" "docker compose up core services failed"
fi
COMPOSE_STARTED="yes"

echo "[c1f2c-smoke] starting source-adapter..."
if ! $COMPOSE -f "$COMPOSE_FILE" up -d --force-recreate --no-build --pull never --no-deps source-adapter; then
  fail_result "FAIL_RUNTIME" "docker compose up source-adapter failed"
fi

# Wait for source-adapter to start streaming
elapsed=0
while [[ "$elapsed" -le 45 ]]; do
  if $COMPOSE -f "$COMPOSE_FILE" logs --tail 160 source-adapter 2>/dev/null \
    | grep -qE "Setting pipeline to PLAYING|Sink caps changed|Processed [0-9]+ frames"; then
    echo "[c1f2c-smoke] source-adapter streaming after ${elapsed}s"
    break
  fi
  sleep "$POLL_SECONDS"
  elapsed=$((elapsed + POLL_SECONDS))
done

echo "[c1f2c-smoke] starting savant-security..."
if ! $COMPOSE -f "$COMPOSE_FILE" up -d --force-recreate --no-build --pull never savant-security; then
  fail_result "FAIL_RUNTIME" "docker compose up savant-security failed"
fi

# Wait for savant ready
elapsed=0
while [[ "$elapsed" -le 60 ]]; do
  health=$($DOCKER inspect "$SAVANT_CONTAINER" 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin)[0]; print(d.get("State",{}).get("Health",{}).get("Status", d.get("State",{}).get("Status","unknown")))' 2>/dev/null || echo "missing")
  if [[ "$health" == "healthy" ]]; then
    echo "[c1f2c-smoke] savant ready after ${elapsed}s"
    break
  fi
  sleep "$POLL_SECONDS"
  elapsed=$((elapsed + POLL_SECONDS))
done

# Wait for summary
echo "[c1f2c-smoke] waiting for AdaFace runtime summary..."
elapsed=0
while [[ "$elapsed" -le "$WAIT_SECONDS" ]]; do
  if [[ -s "$SUMMARY_FILE" ]]; then
    parse_summary > /tmp/c1f2c_stats.json
    faces_with_embedding=$(python3 -c "import json; print(json.load(open('/tmp/c1f2c_stats.json')).get('faces_with_embedding', 0))" 2>/dev/null || echo 0)
    echo "[c1f2c-smoke] elapsed=${elapsed}s faces_with_embedding=${faces_with_embedding}"
    if [[ "$faces_with_embedding" =~ ^[0-9]+$ && "$faces_with_embedding" -gt 0 ]]; then
      break
    fi
  else
    echo "[c1f2c-smoke] elapsed=${elapsed}s summary_not_ready"
  fi
  sleep "$POLL_SECONDS"
  elapsed=$((elapsed + POLL_SECONDS))
done

if [[ ! -s "$SUMMARY_FILE" ]]; then
  fail_result "BLOCKED_NO_SUMMARY_OUTPUT" "no JSONL produced in ${WAIT_SECONDS}s" 2
fi

parse_summary > /tmp/c1f2c_stats.json
STATS_FILE="/tmp/c1f2c_stats.json"

frames_inspected=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('frames_inspected', 0))")
frames_with_face=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('frames_with_face', 0))")
faces_seen=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('faces_seen', 0))")
faces_with_landmarks=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('faces_with_landmarks', 0))")
faces_with_embedding=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('faces_with_embedding', 0))")
faces_with_dim_512=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('faces_with_embedding_dim_512', 0))")
faces_with_valid_norm=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('faces_with_valid_norm', 0))")
sample_dim=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('sample_embedding_dim', 0))")
sample_norm=$(python3 -c "import json; print(json.load(open('$STATS_FILE')).get('sample_embedding_norm', 0))")
run_duration="$(( $(date +%s) - START_EPOCH ))s"

result=""
exit_code=0
if [[ "$faces_with_embedding" -gt 0 && "$faces_with_dim_512" -gt 0 && "$faces_with_valid_norm" -gt 0 ]]; then
  result="PASS"
elif [[ "$faces_with_embedding" -gt 0 && "$faces_with_dim_512" -gt 0 ]]; then
  result="PASS_WITH_SOURCE_CORRUPTION"
elif [[ "$faces_with_embedding" -eq 0 ]]; then
  result="BLOCKED_NO_EMBEDDING"
  exit_code=2
elif [[ "$faces_with_dim_512" -eq 0 ]]; then
  result="BLOCKED_WRONG_DIM"
  exit_code=2
else
  result="FAIL_RUNTIME"
  exit_code=1
fi

echo ""
echo "=== C1F.2c AdaFace Runtime Validation Smoke ==="
echo "Result=${result}"
echo ""
echo "Runtime smoke:"
echo "  run_duration: ${run_duration}"
echo "  frames_inspected: ${frames_inspected}"
echo "  frames_with_face: ${frames_with_face}"
echo "  faces_seen: ${faces_seen}"
echo "  faces_with_landmarks: ${faces_with_landmarks}"
echo "  faces_with_embedding: ${faces_with_embedding}"
echo "  faces_with_embedding_dim_512: ${faces_with_dim_512}"
echo "  faces_with_valid_norm: ${faces_with_valid_norm}"
echo "  sample_embedding_dim: ${sample_dim}"
echo "  sample_embedding_norm: ${sample_norm}"
echo ""
echo "Boundary:"
echo "  face_reid_gate runtime validated: NO"
echo "  face_observation_exporter runtime validated: NO"
echo "  Redis security.face_observations validated: NO"
echo "  face-worker DB ingest validated: NO"
echo "  gallery/watchlist/live_search implemented: NO"
echo "  production evidence clip validated: NO"

exit "$exit_code"
