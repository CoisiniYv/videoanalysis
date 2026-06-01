#!/usr/bin/env bash
# C1F.2d — Face Observation Redis Export Smoke
#
# Validates the full face observation pipeline:
#   RTSP -> Source Adapter -> Replay -> Savant
#   -> YOLO26-pose -> nvtracker -> YOLOv8-Face -> face-person association
#   -> AdaFace -> face_reid_gate -> face_observation_exporter
#   -> Redis security.face_observations
#
# Uses:
#   - Fixed RTSP: rtsp://10.37.57.112:8554/live/1080movie
#   - Inline Replay (single-ingestion, no second RTSP)
#   - module.c1f2d_face_observation_redis.yml (trimmed module)
#   - FACE_OBSERVATION_EXPORT_ENABLED=true (explicit)
#
# NOT validated:
#   - face-worker DB ingest
#   - PostgreSQL face_observations
#   - gallery match / watchlist / live_search
#   - API / frontend
#   - production evidence clip
#
# Startup order (staged):
#   1. redis, postgres, replay-service (compose default)
#   2. source-adapter (starts RTSP -> Replay)
#   3. savant-security (starts after source-adapter)
#   This is NOT a production startup order recommendation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/infra/docker-compose.c1-official-replay-dev.yml"

# ---- Fixed RTSP source ----
RTSP_URI="${C1E_RTSP_URI:-rtsp://10.37.57.112:8554/live/1080movie}"

# ---- Module selection ----
export SAVANT_MODULE_FILE="${SAVANT_MODULE_FILE:-module.c1f2d_face_observation_redis.yml}"
MODULE_FILE="$PROJECT_ROOT/modules/savant_security/$SAVANT_MODULE_FILE"

# ---- Timing ----
SMOKE_DURATION="${C1F2D_SMOKE_DURATION:-45}"
DRAIN_WAIT="${C1F2D_DRAIN_WAIT:-10}"

# ---- Redis ----
REDIS_PORT="${C1F2D_REDIS_PORT:-6385}"
REDIS_STREAM="security.face_observations"

# ---- Artifacts ----
ARTIFACT_DIR="${C1F2D_ARTIFACT_DIR:-/data/video-analytics/artifacts/c1f2d}"
SUMMARY_FILE="$ARTIFACT_DIR/face_observation_redis_summary.json"

# ---- Env for compose ----
export FACE_OBSERVATION_EXPORT_ENABLED="true"
export C1F1_SAME_FRAME_DEBUG_ENABLED="0"

log() {
    echo "[C1F.2d] $(date '+%H:%M:%S') $*"
}

cleanup() {
    log "cleanup: stopping staged containers"
    cd "$PROJECT_ROOT"
    docker compose -f "$COMPOSE_FILE" stop savant-security source-adapter 2>/dev/null || true
}
trap cleanup EXIT

# ---- Pre-flight ----
log "pre-flight: checking Docker daemon"
if ! docker ps >/dev/null 2>&1; then
    if ! sudo docker ps >/dev/null 2>&1; then
        echo "RESULT=BLOCKED"
        echo "REASON=docker_daemon_inaccessible"
        exit 1
    fi
    log "warning: using sudo docker"
    DOCKER_CMD="sudo docker"
    COMPOSE_CMD="sudo docker compose"
else
    DOCKER_CMD="docker"
    COMPOSE_CMD="docker compose"
fi

log "pre-flight: checking module file"
if [ ! -f "$MODULE_FILE" ]; then
    echo "RESULT=BLOCKED"
    echo "REASON=module_file_missing: $SAVANT_MODULE_FILE"
    exit 1
fi

log "pre-flight: checking redis-py"
if ! python3 -c "import redis" 2>/dev/null; then
    log "installing redis-py"
    pip install redis -q 2>/dev/null || {
        echo "RESULT=BLOCKED"
        echo "REASON=redis_py_not_available"
        exit 1
    }
fi

# ---- Start infrastructure ----
log "starting infrastructure: redis, postgres, replay-service"
cd "$PROJECT_ROOT"
$COMPOSE_CMD -f "$COMPOSE_FILE" up -d redis postgres replay-service
sleep 3

log "waiting for Redis healthy"
for i in $(seq 1 15); do
    if $DOCKER_CMD exec c1-official-redis redis-cli ping 2>/dev/null | grep -q PONG; then
        log "Redis healthy"
        break
    fi
    sleep 1
done

# ---- Record baseline ----
mkdir -p "$ARTIFACT_DIR"
redis_messages_before=$(python3 -c "
import redis
r = redis.Redis(host='localhost', port=$REDIS_PORT)
print(r.xlen('$REDIS_STREAM'))
" 2>/dev/null || echo "0")
log "redis baseline: XLEN $REDIS_STREAM = $redis_messages_before"

# ---- Start source-adapter ----
log "starting source-adapter (RTSP -> Replay)"
$COMPOSE_CMD -f "$COMPOSE_FILE" up -d source-adapter
sleep 3

# ---- Start savant-security ----
log "starting savant-security ($SAVANT_MODULE_FILE, FACE_OBSERVATION_EXPORT_ENABLED=true)"
$COMPOSE_CMD -f "$COMPOSE_FILE" up -d savant-security

# ---- Capture logs ----
SAVANT_LOGFILE="$ARTIFACT_DIR/savant_c1f2d.log"
$DOCKER_CMD logs -f c1-official-savant >"$SAVANT_LOGFILE" 2>&1 &
LOG_PID=$!

# ---- Wait for pipeline ----
log "waiting ${SMOKE_DURATION}s for pipeline to process frames"
sleep "$SMOKE_DURATION"

# ---- Drain ----
log "draining for ${DRAIN_WAIT}s"
sleep "$DRAIN_WAIT"

# ---- Stop ----
log "stopping savant-security and source-adapter"
$COMPOSE_CMD -f "$COMPOSE_FILE" stop savant-security source-adapter 2>/dev/null || true
sleep 2
kill "$LOG_PID" 2>/dev/null || true
wait "$LOG_PID" 2>/dev/null || true

# ---- Validate via python (redis-py) ----
log "reading and validating Redis messages"

export _REDIS_BEFORE="$redis_messages_before"
export C1F2D_REDIS_PORT="$REDIS_PORT"
export C1F2D_ARTIFACT_DIR="$ARTIFACT_DIR"
export C1F2D_SMOKE_DURATION="$SMOKE_DURATION"
export C1F2D_DRAIN_WAIT="$DRAIN_WAIT"

python3 -c "
import json
import math
import os
import redis

REDIS_PORT = int(os.environ.get('C1F2D_REDIS_PORT', '6385'))
REDIS_STREAM = 'security.face_observations'
ARTIFACT_DIR = os.environ.get('C1F2D_ARTIFACT_DIR', '/data/video-analytics/artifacts/c1f2d')
BEFORE = int(os.environ.get('_REDIS_BEFORE', '0'))
SMOKE_DURATION = int(os.environ.get('C1F2D_SMOKE_DURATION', '45'))
DRAIN_WAIT = int(os.environ.get('C1F2D_DRAIN_WAIT', '10'))
SUMMARY_FILE = os.path.join(ARTIFACT_DIR, 'face_observation_redis_summary.json')
VALID_FILE = os.path.join(ARTIFACT_DIR, 'valid_observations.jsonl')

r = redis.Redis(host='localhost', port=REDIS_PORT, decode_responses=True)
after = r.xlen(REDIS_STREAM)
new_count = max(0, after - BEFORE)

raw = r.xrevrange(REDIS_STREAM, '+', '-', count=500)

stats = {
    'result': 'FAIL_NO_REDIS_MESSAGES',
    'run_duration_s': SMOKE_DURATION + DRAIN_WAIT,
    'redis_stream': REDIS_STREAM,
    'redis_messages_before': BEFORE,
    'redis_messages_after': after,
    'new_messages_observed': new_count,
    'valid_face_observations': 0,
    'observations_with_track_id': 0,
    'observations_with_embedding': 0,
    'observations_with_embedding_dim_512': 0,
    'observations_with_valid_norm': 0,
    'observations_with_gate_allowed': 0,
    'observations_without_image_bytes': 0,
    'sample_source_observation_id': '',
    'sample_embedding_dim': 0,
    'sample_embedding_norm': '0.0',
    'source_corruption_observed': False,
    'module': os.environ.get('SAVANT_MODULE_FILE', ''),
    'rtsp_source': os.environ.get('C1E_RTSP_URI', 'rtsp://10.37.57.112:8554/live/1080movie'),
}

if new_count <= 0:
    logfile = os.path.join(ARTIFACT_DIR, 'savant_c1f2d.log')
    if os.path.isfile(logfile):
        log_text = open(logfile).read()
        # Check for real failures, not normal EOS shutdown
        if 'error' in log_text.lower() and 'pipeline' in log_text.lower():
            stats['result'] = 'FAIL_PIPELINE_STOPPED'
        elif 'traceback' in log_text.lower():
            stats['result'] = 'FAIL_PIPELINE_STOPPED'
    json.dump(stats, open(SUMMARY_FILE, 'w'), indent=2)
    print(json.dumps(stats, indent=2))
    raise SystemExit(0)

valid_obs = []
with open(VALID_FILE, 'w') as vf:
    for msg_id, fields in raw:
        data_json = fields.get('data', '')
        if not data_json:
            continue
        try:
            obs = json.loads(data_json)
        except Exception:
            continue

        # Check for image/crop/base64 bytes in any field value
        has_image_bytes = False
        image_byte_keywords = ['jpg', 'jpeg', 'png', 'raw', 'base64', 'crop_bytes', 'image_bytes']
        obs_str = json.dumps(obs).lower()
        for kw in image_byte_keywords:
            if kw in obs_str:
                for k, v in obs.items():
                    if isinstance(v, str) and len(v) > 200 and not v.startswith('{'):
                        has_image_bytes = True
                        break
                if has_image_bytes:
                    break

        checks = {
            'source_observation_id': bool(obs.get('source_observation_id')),
            'camera_id': bool(obs.get('camera_id')),
            'source_id': bool(obs.get('source_id')),
            'timestamp_ms': bool(obs.get('timestamp_ms')),
            'track_id': bool(obs.get('track_id')),
            'face_bbox': obs.get('face_bbox') is not None,
            'landmarks': obs.get('landmarks') is not None,
            'face_confidence': obs.get('face_confidence') is not None,
            'quality': obs.get('quality') is not None,
        }

        emb = obs.get('embedding')
        has_embedding = isinstance(emb, list) and len(emb) > 0
        emb_dim = obs.get('embedding_dim', 0)
        emb_norm = float(obs.get('embedding_norm', 0.0))
        emb_model = obs.get('embedding_model', '')
        det_model = obs.get('detector_model', '')
        reid_allowed = obs.get('reid_allowed', False)

        is_valid = all(checks.values()) and all([
            has_embedding,
            emb_dim == 512,
            0.90 <= emb_norm <= 1.10,
            emb_model == 'adaface',
            det_model == 'yolov8_face',
            reid_allowed is True or str(reid_allowed).lower() == 'true',
            not has_image_bytes,
        ])

        if is_valid:
            stats['valid_face_observations'] += 1
            vf.write(json.dumps(obs, separators=(',', ':')) + '\n')
            if not valid_obs:
                valid_obs.append(obs)

        if checks.get('track_id'):
            stats['observations_with_track_id'] += 1
        if has_embedding:
            stats['observations_with_embedding'] += 1
        if emb_dim == 512:
            stats['observations_with_embedding_dim_512'] += 1
        if emb_norm and 0.90 <= emb_norm <= 1.10:
            stats['observations_with_valid_norm'] += 1
        if reid_allowed is True or str(reid_allowed).lower() == 'true':
            stats['observations_with_gate_allowed'] += 1
        if not has_image_bytes:
            stats['observations_without_image_bytes'] += 1

if stats['valid_face_observations'] > 0:
    s = valid_obs[0]
    stats['sample_source_observation_id'] = s.get('source_observation_id', '')
    stats['sample_embedding_dim'] = s.get('embedding_dim', 0)
    stats['sample_embedding_norm'] = str(s.get('embedding_norm', 0.0))
    stats['result'] = 'PASS'
else:
    stats['result'] = 'FAIL_NO_VALID_OBSERVATION'

logfile = os.path.join(ARTIFACT_DIR, 'savant_c1f2d.log')
if os.path.isfile(logfile):
    log_text = open(logfile).read()
    if any(kw in log_text.lower() for kw in ['generated_corrupt', 'decode error', 'reference frame']):
        stats['source_corruption_observed'] = True
        if stats['result'] == 'PASS':
            stats['result'] = 'PASS_WITH_SOURCE_CORRUPTION'

json.dump(stats, open(SUMMARY_FILE, 'w'), indent=2)
print(json.dumps(stats, indent=2))
"

# ---- Extract result ----
result=$(python3 -c "import json; print(json.load(open('$SUMMARY_FILE')).get('result', 'UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")

# ---- Output summary ----
log "========================================="
log "C1F.2d Face Observation Redis Export Smoke"
log "========================================="
python3 -c "
import json
d = json.load(open('$SUMMARY_FILE'))
for k, v in d.items():
    print(f'{k}={v}')
"

# ---- Exit code ----
case "$result" in
    PASS|PASS_WITH_SOURCE_CORRUPTION)
        log "SMOKE PASSED: $result"
        exit 0
        ;;
    *)
        log "SMOKE FAILED: $result"
        exit 1
        ;;
esac
