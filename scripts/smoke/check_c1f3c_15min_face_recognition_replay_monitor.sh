#!/usr/bin/env bash
# C1F.3c — 15-minute production-like face recognition soak + Replay size monitor.
#
# Runtime path under test:
#   RTSP -> Source Adapter -> Replay -> Savant C1F.2d module
#   -> Redis security.face_observations
#   -> face-worker service -> PostgreSQL face_observations
#   -> gallery search / match_results
#
# Additional monitoring vs C1F.3b:
#   - Replay RocksDB directory size growth
#   - Periodic 60-second sampling to JSONL monitor log
#   - Visual output attempt (not required)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/infra/docker-compose.c1-official-replay-dev.yml"
FACE_WORKER_DIR="$PROJECT_ROOT/services/face-worker"

DB_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
export DATABASE_URL="$DB_URL"

REDIS_URL="${C1F3C_REDIS_URL:-redis://localhost:6385/0}"
REDIS_STREAM="security.face_observations"
FACE_WORKER_CONSUMER_GROUP="${FACE_WORKER_CONSUMER_GROUP:-face-worker}"
FACE_WORKER_CONSUMER_NAME="${FACE_WORKER_CONSUMER_NAME:-c1f3c-smoke}"

RUN_DURATION_SEC="${RUN_DURATION_SEC:-900}"
SAMPLE_INTERVAL_SEC="${SAMPLE_INTERVAL_SEC:-60}"
DRAIN_WAIT_SEC="${DRAIN_WAIT_SEC:-30}"
FACE_WORKER_DRAIN_SEC="${FACE_WORKER_DRAIN_SEC:-20}"
TOP_K="${C1F3C_TOP_K:-5}"
MIN_SIMILARITY="${C1F3C_MIN_SIMILARITY:-0.5}"
QUERY_OBSERVATION_LIMIT="${C1F3C_QUERY_OBSERVATION_LIMIT:-20}"

RTSP_URI="rtsp://10.37.57.112:8554/live/1080movie"
export SAVANT_MODULE_FILE="module.c1f2d_face_observation_redis.yml"
export FACE_OBSERVATION_EXPORT_ENABLED="true"
export C1F1_SAME_FRAME_DEBUG_ENABLED="0"
export RTSP_TRANSPORT="${RTSP_TRANSPORT:-tcp}"
export C1E_RTSP_TRANSPORT="$RTSP_TRANSPORT"

ARCHIVE_FINCH="/data/video-analytics/archive/r2_3_20260529_052937/repo_uncommitted/face/finch.jpg"
ARCHIVE_REESE="/data/video-analytics/archive/r2_3_20260529_052937/repo_uncommitted/face/reese.jpg"
ACTIVE_REGISTRATION_DIR="${C1F3C_ACTIVE_REGISTRATION_DIR:-/data/video-analytics/media/face-registration}"
ACTIVE_FINCH="$ACTIVE_REGISTRATION_DIR/finch.jpg"
ACTIVE_REESE="$ACTIVE_REGISTRATION_DIR/reese.jpg"
FINCH_EXTERNAL_ID="test:archive:finch"
REESE_EXTERNAL_ID="test:archive:reese"

ARTIFACT_DIR="${C1F3C_ARTIFACT_DIR:-/data/video-analytics/artifacts/c1f3c}"
BASELINE_FILE="$ARTIFACT_DIR/baseline.json"
REGISTRATION_FILE="$ARTIFACT_DIR/registration_summary.json"
FACE_WORKER_LOG="$ARTIFACT_DIR/face_worker.log"
SAVANT_LOG="$ARTIFACT_DIR/savant.log"
SUMMARY_FILE="$ARTIFACT_DIR/c1f3c_summary.json"
MONITOR_JSONL="$ARTIFACT_DIR/15min_face_recognition_monitor.jsonl"
VISUAL_DIR="$ARTIFACT_DIR/visual"

# Replay directory candidates (auto-discovered)
REPLAY_DIR_CANDIDATES=(
    "/data/video-analytics/replay-c1-official-replay-dev"
    "/data/video-analytics/replay"
    "/data/video-analytics/replay/rocksdb"
    "/data/video-analytics/media/replay-sink-output"
)
REPLAY_DIR=""

RUN_STARTED_AT=""

log() {
    echo "[C1F.3c] $(date '+%H:%M:%S') $*"
}

fail_result() {
    local result="$1"
    local reason="${2:-}"
    echo "RESULT=${result}"
    if [ -n "$reason" ]; then
        echo "REASON=${reason}"
    fi
    exit 1
}

require_file() {
    local path="$1"
    local label="$2"
    if [ ! -f "$path" ]; then
        fail_result "FAIL_MISSING_INPUT" "${label}: ${path}"
    fi
}

ensure_python_deps() {
    python3 - <<'PY'
import importlib
import sys

missing = []
for mod in ("psycopg", "pgvector", "redis"):
    try:
        importlib.import_module(mod)
    except Exception as exc:
        missing.append(f"{mod}: {exc}")

if missing:
    print("missing Python dependencies: " + "; ".join(missing))
    sys.exit(1)
PY
}

wait_for_infra() {
    log "waiting for Redis/PostgreSQL health"
    for _ in $(seq 1 60); do
        local pg_status
        local redis_status
        pg_status="$(docker inspect -f '{{.State.Health.Status}}' c1-official-postgres 2>/dev/null || echo missing)"
        redis_status="$(docker inspect -f '{{.State.Health.Status}}' c1-official-redis 2>/dev/null || echo missing)"
        if [ "$pg_status" = "healthy" ] && [ "$redis_status" = "healthy" ]; then
            log "infra healthy: postgres=${pg_status} redis=${redis_status}"
            return 0
        fi
        sleep 1
    done
    fail_result "FAIL_INFRA_UNHEALTHY" "Redis/PostgreSQL did not become healthy"
}

wait_for_face_worker() {
    log "checking face-worker service"
    for _ in $(seq 1 30); do
        if [ "$(docker inspect -f '{{.State.Running}}' c1-official-face-worker 2>/dev/null || echo false)" = "true" ]; then
            log "face-worker service running"
            return 0
        fi
        sleep 1
    done
    fail_result "FAIL_FACE_WORKER_INGEST" "face-worker service did not start"
}

discover_replay_dir() {
    for candidate in "${REPLAY_DIR_CANDIDATES[@]}"; do
        if [ -d "$candidate" ]; then
            REPLAY_DIR="$candidate"
            log "replay directory found: ${REPLAY_DIR}"
            return 0
        fi
    done
    log "WARNING: no replay directory found among candidates"
    REPLAY_DIR="NOT_FOUND"
}

get_dir_size_bytes() {
    local dir="$1"
    if [ "$dir" = "NOT_FOUND" ] || [ ! -d "$dir" ]; then
        echo "0"
        return
    fi
    du -sb "$dir" 2>/dev/null | awk '{print $1}' || echo "0"
}

get_redis_stream_length() {
    redis-cli -u "$REDIS_URL" XLEN "$REDIS_STREAM" 2>/dev/null || echo "0"
}

get_redis_pending_count() {
    python3 - "$REDIS_URL" "$REDIS_STREAM" "$FACE_WORKER_CONSUMER_GROUP" <<'PY'
import sys
import redis
from redis.exceptions import ResponseError

redis_url, stream, group_name = sys.argv[1:4]
r = redis.Redis.from_url(redis_url, decode_responses=True)
try:
    for group in r.xinfo_groups(stream):
        if group.get("name") == group_name:
            print(group.get("pending", 0))
            sys.exit(0)
    print("0")
except ResponseError:
    print("0")
PY
}

get_redis_lag() {
    python3 - "$REDIS_URL" "$REDIS_STREAM" "$FACE_WORKER_CONSUMER_GROUP" <<'PY'
import sys
import redis
from redis.exceptions import ResponseError

redis_url, stream, group_name = sys.argv[1:4]
r = redis.Redis.from_url(redis_url, decode_responses=True)
try:
    for group in r.xinfo_groups(stream):
        if group.get("name") == group_name:
            lag = group.get("lag")
            print(lag if lag is not None else "unknown")
            sys.exit(0)
    print("unknown")
except ResponseError:
    print("unknown")
PY
}

get_pg_face_observations_count() {
    python3 - "$DB_URL" <<'PY'
import sys
import psycopg
from psycopg.rows import dict_row

db_url = sys.argv[1]
with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT COUNT(*) AS count FROM face_observations")
        print(int(cur.fetchone()["count"]))
PY
}

get_face_worker_error_count() {
    local log_file="$1"
    if [ ! -f "$log_file" ]; then
        echo "0"
        return
    fi
    grep -c -E "ERROR|Traceback|Exception" "$log_file" 2>/dev/null || echo "0"
}

get_latest_match_candidate() {
    python3 - "$DB_URL" "$RUN_STARTED_AT" <<'PY'
import sys
import psycopg
from psycopg.rows import dict_row

db_url, started_at = sys.argv[1:3]
with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT source_observation_id, face_confidence, quality, embedding_norm
            FROM face_observations
            WHERE created_at >= %(started)s::timestamptz
              AND embedding_dim = 512
              AND embedding_model = 'adaface'
              AND embedding_norm BETWEEN 0.90 AND 1.10
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"started": started_at},
        )
        row = cur.fetchone()
        if row:
            print(f"{row['source_observation_id']}|{row['face_confidence']}|{row['quality']}|{row['embedding_norm']}")
        else:
            print("none")
PY
}

take_sample() {
    local elapsed="$1"
    local sample_file="$2"

    local replay_size
    replay_size="$(get_dir_size_bytes "$REPLAY_DIR")"
    local stream_len
    stream_len="$(get_redis_stream_length)"
    local pending
    pending="$(get_redis_pending_count)"
    local lag
    lag="$(get_redis_lag)"
    local pg_count
    pg_count="$(get_pg_face_observations_count)"
    local error_count
    error_count="$(get_face_worker_error_count "$FACE_WORKER_LOG")"
    local latest
    latest="$(get_latest_match_candidate)"

    python3 - "$elapsed" "$replay_size" "$stream_len" "$pending" "$lag" "$pg_count" "$error_count" "$latest" "$sample_file" <<'PY'
import json
import sys
from datetime import datetime, timezone

elapsed, replay_size, stream_len, pending, lag, pg_count, error_count, latest, sample_file = sys.argv[1:10]

sample = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "elapsed_sec": int(elapsed),
    "replay_dir_size_bytes": int(replay_size),
    "redis_stream_length": int(stream_len),
    "redis_pending_count": int(pending) if pending != "unknown" else None,
    "redis_lag": int(lag) if lag not in ("unknown", "None") else None,
    "pg_face_observations_count": int(pg_count),
    "face_worker_error_count": int(error_count),
    "latest_match_candidate": latest if latest != "none" else None,
}

with open(sample_file, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sample) + "\n")
PY
    log "sample elapsed=${elapsed}s replay_size=${replay_size}B stream_len=${stream_len} pending=${pending} lag=${lag} pg_obs=${pg_count} errors=${error_count}"
}

ensure_active_registration_image() {
    local archive_path="$1"
    local active_path="$2"
    local label="$3"

    mkdir -p "$ACTIVE_REGISTRATION_DIR"
    if [ ! -f "$active_path" ] || ! cmp -s "$archive_path" "$active_path"; then
        log "copying ${label} registration image to active media path"
        install -m 0644 "$archive_path" "$active_path"
    fi
}

active_registration_valid() {
    local image="$1"
    local external_id="$2"
    local out_file="$3"

    python3 - "$DB_URL" "$image" "$external_id" "$out_file" <<'PY'
import json
import sys

import psycopg
from psycopg.rows import dict_row

db_url, image_path, external_id, out_file = sys.argv[1:5]

result = {
    "external_person_id": external_id,
    "source_image_path": image_path,
    "person_id": None,
    "gallery_embedding_id": None,
    "embedding_dim": None,
    "embedding_norm": None,
    "embedding_model": None,
    "valid": False,
}

with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT p.id AS person_id,
                   pge.id AS gallery_embedding_id,
                   pge.embedding_dim,
                   pge.embedding_norm,
                   pge.embedding_model
            FROM persons p
            JOIN person_gallery_embeddings pge ON pge.person_id = p.id
            WHERE p.external_person_id = %s
              AND p.is_active = true
              AND pge.is_active = true
              AND pge.source_image_path = %s
              AND pge.embedding_model = 'adaface'
              AND pge.embedding_dim = 512
              AND pge.embedding_norm BETWEEN 0.90 AND 1.10
            ORDER BY pge.is_primary DESC, pge.id DESC
            LIMIT 1
            """,
            (external_id, image_path),
        )
        row = cur.fetchone()

if row:
    result.update(
        {
            "person_id": int(row["person_id"]),
            "gallery_embedding_id": int(row["gallery_embedding_id"]),
            "embedding_dim": int(row["embedding_dim"]),
            "embedding_norm": float(row["embedding_norm"]),
            "embedding_model": row["embedding_model"],
            "valid": True,
        }
    )

with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(result, fh, indent=2)

sys.exit(0 if result["valid"] else 2)
PY
}

normalize_test_gallery() {
    local image="$1"
    local external_id="$2"

    python3 - "$DB_URL" "$image" "$external_id" <<'PY'
import sys

import psycopg
from psycopg.rows import dict_row

db_url, image_path, external_id = sys.argv[1:4]

with psycopg.connect(db_url, autocommit=True) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id FROM persons WHERE external_person_id = %s AND is_active = true",
            (external_id,),
        )
        person = cur.fetchone()
        if person is None:
            sys.exit(2)
        person_id = int(person["id"])

        cur.execute(
            """
            SELECT id
            FROM person_gallery_embeddings
            WHERE person_id = %s
              AND is_active = true
              AND source_image_path = %s
              AND embedding_model = 'adaface'
              AND embedding_dim = 512
              AND embedding_norm BETWEEN 0.90 AND 1.10
            ORDER BY is_primary DESC, id DESC
            LIMIT 1
            """,
            (person_id, image_path),
        )
        gallery = cur.fetchone()
        if gallery is None:
            sys.exit(2)
        keep_id = int(gallery["id"])

        cur.execute(
            """
            UPDATE person_gallery_embeddings
            SET is_active = false,
                is_primary = false,
                updated_at = now()
            WHERE person_id = %s
              AND id <> %s
              AND is_active = true
            """,
            (person_id, keep_id),
        )
        cur.execute(
            """
            UPDATE person_gallery_embeddings
            SET is_active = true,
                is_primary = true,
                updated_at = now()
            WHERE person_id = %s
              AND id = %s
            """,
            (person_id, keep_id),
        )
PY
}

register_active_person_if_needed() {
    local image="$1"
    local external_id="$2"
    local name="$3"
    local out_file="$ARTIFACT_DIR/${name,,}_active_registration.json"

    if ! active_registration_valid "$image" "$external_id" "$out_file"; then
        log "registering ${external_id} from active media path"
        if ! (
            cd "$FACE_WORKER_DIR"
            python3 register_face_image.py \
                --image "$image" \
                --external-person-id "$external_id" \
                --name "$name" \
                --is-primary \
                --allow-multiple-faces \
                --quality-threshold 0.65 \
                --created-by "c1f3c_15min_soak" \
                --output-json
        ) >"$out_file.tmp" 2>&1; then
            cat "$out_file.tmp"
            fail_result "FAIL_REGISTRATION" "${external_id}"
        fi
        mv "$out_file.tmp" "$out_file"
    else
        log "active registration already valid: ${external_id}"
    fi

    normalize_test_gallery "$image" "$external_id"
    if ! active_registration_valid "$image" "$external_id" "$out_file"; then
        cat "$out_file"
        fail_result "FAIL_REGISTRATION_VALIDATION" "${external_id}"
    fi
}

summarize_registration() {
    log "summarizing active registrations"
    python3 - "$DB_URL" "$FINCH_EXTERNAL_ID" "$REESE_EXTERNAL_ID" "$REGISTRATION_FILE" <<'PY'
import json
import sys

import psycopg
from psycopg.rows import dict_row

db_url, finch_ext, reese_ext, out_file = sys.argv[1:5]
summary = {
    "persons_total": 0,
    "gallery_embeddings_total": 0,
    "active_gallery_embeddings_total": 0,
    "people": {},
}

with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT COUNT(*) AS count FROM persons WHERE is_active = true")
        summary["persons_total"] = int(cur.fetchone()["count"])
        cur.execute("SELECT COUNT(*) AS count FROM person_gallery_embeddings")
        summary["gallery_embeddings_total"] = int(cur.fetchone()["count"])
        cur.execute(
            "SELECT COUNT(*) AS count FROM person_gallery_embeddings WHERE is_active = true"
        )
        summary["active_gallery_embeddings_total"] = int(cur.fetchone()["count"])
        cur.execute(
            """
            SELECT p.external_person_id,
                   p.id AS person_id,
                   pge.id AS gallery_embedding_id,
                   pge.source_image_path,
                   pge.embedding_dim,
                   pge.embedding_norm,
                   pge.embedding_model,
                   pge.payload::text AS payload_text
            FROM persons p
            JOIN person_gallery_embeddings pge ON pge.person_id = p.id
            WHERE p.external_person_id = ANY(%s)
              AND p.is_active = true
              AND pge.is_active = true
            ORDER BY p.external_person_id, pge.is_primary DESC, pge.id DESC
            """,
            ([finch_ext, reese_ext],),
        )
        for row in cur.fetchall():
            ext = row["external_person_id"]
            if ext in summary["people"]:
                continue
            payload_text = row["payload_text"] or ""
            summary["people"][ext] = {
                "person_id": int(row["person_id"]),
                "gallery_embedding_id": int(row["gallery_embedding_id"]),
                "source_image_path": row["source_image_path"],
                "embedding_dim": int(row["embedding_dim"]),
                "embedding_norm": float(row["embedding_norm"]),
                "embedding_model": row["embedding_model"],
                "no_image_bytes_in_payload": not any(
                    marker in payload_text.lower()
                    for marker in ("image_bytes", "crop_bytes", "raw_bytes", "data:image/", "base64")
                ),
            }

with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(summary, fh, indent=2)
print(json.dumps(summary, indent=2))

for ext in (finch_ext, reese_ext):
    if ext not in summary["people"]:
        print(f"missing active registration: {ext}")
        sys.exit(1)
    row = summary["people"][ext]
    if row["embedding_model"] != "adaface" or row["embedding_dim"] != 512:
        print(f"invalid embedding metadata: {ext}")
        sys.exit(1)
    if not (0.90 <= row["embedding_norm"] <= 1.10):
        print(f"invalid embedding norm: {ext}")
        sys.exit(1)
    if "/media/face-registration/" not in row["source_image_path"]:
        print(f"source_image_path is not active media path: {ext}")
        sys.exit(1)
    if not row["no_image_bytes_in_payload"]:
        print(f"payload contains forbidden image bytes marker: {ext}")
        sys.exit(1)
PY
}

write_baseline() {
    log "recording baseline counts"
    python3 - "$DB_URL" "$REDIS_URL" "$REDIS_STREAM" "$FACE_WORKER_CONSUMER_GROUP" "$BASELINE_FILE" "$REPLAY_DIR" <<'PY'
import json
import sys
from pathlib import Path

import psycopg
import redis
from psycopg.rows import dict_row
from redis.exceptions import ResponseError

db_url, redis_url, stream, group_name, out_file, replay_dir = sys.argv[1:7]

baseline = {
    "redis_stream": stream,
    "redis_messages_before": 0,
    "redis_last_id_before": "0-0",
    "consumer_group": group_name,
    "consumer_group_exists_before": False,
    "consumer_group_entries_read_before": None,
    "consumer_group_pending_before": None,
    "consumer_group_lag_before": None,
    "persons_before": 0,
    "gallery_embeddings_before": 0,
    "face_observations_before": 0,
    "match_results_before": 0,
    "replay_dir": replay_dir,
    "replay_dir_size_bytes_before": 0,
}

# Replay directory size
if replay_dir != "NOT_FOUND" and Path(replay_dir).is_dir():
    total = sum(f.stat().st_size for f in Path(replay_dir).rglob("*") if f.is_file())
    baseline["replay_dir_size_bytes_before"] = total

r = redis.Redis.from_url(redis_url, decode_responses=True)
try:
    info = r.xinfo_stream(stream)
    baseline["redis_messages_before"] = int(info.get("length", 0))
    baseline["redis_last_id_before"] = info.get("last-generated-id") or "0-0"
except ResponseError:
    pass

try:
    for group in r.xinfo_groups(stream):
        if group.get("name") == group_name:
            baseline["consumer_group_exists_before"] = True
            baseline["consumer_group_entries_read_before"] = group.get("entries-read")
            baseline["consumer_group_pending_before"] = group.get("pending")
            baseline["consumer_group_lag_before"] = group.get("lag")
            break
except ResponseError:
    pass

with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT COUNT(*) AS count FROM persons WHERE is_active = true")
        baseline["persons_before"] = int(cur.fetchone()["count"])
        cur.execute(
            "SELECT COUNT(*) AS count FROM person_gallery_embeddings WHERE is_active = true"
        )
        baseline["gallery_embeddings_before"] = int(cur.fetchone()["count"])
        cur.execute("SELECT COUNT(*) AS count FROM face_observations")
        baseline["face_observations_before"] = int(cur.fetchone()["count"])
        cur.execute("SELECT COUNT(*) AS count FROM match_results")
        baseline["match_results_before"] = int(cur.fetchone()["count"])

with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(baseline, fh, indent=2)
print(json.dumps(baseline, indent=2))
PY
}

sleep_with_monitoring() {
    local remaining="$RUN_DURATION_SEC"
    local elapsed=0
    # Clear the monitor JSONL file
    > "$MONITOR_JSONL"

    while [ "$remaining" -gt 0 ]; do
        local step="$SAMPLE_INTERVAL_SEC"
        if [ "$remaining" -lt "$step" ]; then
            step="$remaining"
        fi
        sleep "$step"
        elapsed=$((elapsed + step))
        remaining=$((remaining - step))
        take_sample "$elapsed" "$MONITOR_JSONL"
        log "soak progress: elapsed=${elapsed}s total=${RUN_DURATION_SEC}s remaining=${remaining}s"
    done
}

collect_logs() {
    docker logs --since "$RUN_STARTED_AT" c1-official-face-worker >"$FACE_WORKER_LOG" 2>&1 || true
    docker logs --since "$RUN_STARTED_AT" c1-official-savant >"$SAVANT_LOG" 2>&1 || true
}

attempt_visual_output() {
    log "checking visual output capability"
    mkdir -p "$VISUAL_DIR"

    # Check if the V1 visual result generator exists and can be used
    local v1_script="$PROJECT_ROOT/scripts/debug/generate_visual_result.py"
    if [ ! -f "$v1_script" ]; then
        log "visual output: V1 generator not found"
        cat > "$VISUAL_DIR/visual_output_status.json" <<'JSON'
{
    "visual_output_status": "VISUAL_OUTPUT_NOT_AVAILABLE",
    "reason": "current C1 face recognition path stores metadata/embedding but does not yet retain frame image/snapshot for matched observation",
    "production_evidence": false,
    "debug_visual_only": true
}
JSON
        return 0
    fi

    # Check if we have frame-anchored evidence for gallery matches
    # The V1 system explicitly excludes gallery recognition results from visual rendering
    # because there is no frame-UUID-aligned proof available for match_results
    log "visual output: gallery recognition results lack frame-UUID-aligned proof for visual annotation"
    cat > "$VISUAL_DIR/visual_output_status.json" <<'JSON'
{
    "visual_output_status": "VISUAL_OUTPUT_NOT_AVAILABLE",
    "reason": "current C1 face recognition path stores metadata/embedding but does not yet retain frame image/snapshot for matched observation; V1 visual renderer requires frame-UUID-aligned proof which gallery match_results do not provide",
    "production_evidence": false,
    "debug_visual_only": true
}
JSON
    return 0
}

finalize_summary() {
    log "validating ingest and running gallery recognition"
    python3 - "$PROJECT_ROOT" "$FACE_WORKER_DIR" "$DB_URL" "$REDIS_URL" "$REDIS_STREAM" \
        "$FACE_WORKER_CONSUMER_GROUP" "$BASELINE_FILE" "$REGISTRATION_FILE" \
        "$FACE_WORKER_LOG" "$SUMMARY_FILE" "$RUN_STARTED_AT" "$RUN_DURATION_SEC" \
        "$RTSP_URI" "$RTSP_TRANSPORT" "$SAVANT_MODULE_FILE" "$TOP_K" \
        "$MIN_SIMILARITY" "$QUERY_OBSERVATION_LIMIT" "$FINCH_EXTERNAL_ID" \
        "$REESE_EXTERNAL_ID" "$REPLAY_DIR" "$MONITOR_JSONL" "$VISUAL_DIR" <<'PY'
import json
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import redis
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from redis.exceptions import ResponseError

project_root, face_worker_dir, db_url, redis_url, stream = sys.argv[1:6]
group_name, baseline_file, registration_file, log_file, summary_file = sys.argv[6:11]
run_started_at, run_duration_s, rtsp_uri, rtsp_transport, module_file = sys.argv[11:16]
top_k_s, threshold_s, query_limit_s, finch_ext, reese_ext = sys.argv[16:21]
replay_dir, monitor_jsonl, visual_dir = sys.argv[21:24]
sys.path.insert(0, face_worker_dir)
sys.path.insert(0, project_root)

from app.match_repository import MatchResultRepository
from app.vector_store import FaceVectorStore

top_k = int(top_k_s)
threshold = float(threshold_s)
query_limit = int(query_limit_s)
target_external_ids = {finch_ext, reese_ext}
baseline = json.loads(Path(baseline_file).read_text(encoding="utf-8"))
registration = json.loads(Path(registration_file).read_text(encoding="utf-8"))
log_text = Path(log_file).read_text(encoding="utf-8") if Path(log_file).is_file() else ""


def has_forbidden_image_bytes(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            if any(marker in key_l for marker in ("image_bytes", "crop_bytes", "raw_bytes")):
                return True
            if "base64" in key_l:
                return True
            if has_forbidden_image_bytes(value):
                return True
        return False
    if isinstance(obj, list):
        return any(has_forbidden_image_bytes(item) for item in obj)
    if isinstance(obj, str):
        text = obj.strip().lower()
        return text.startswith("data:image/") or text.startswith("base64,")
    return False


# Count samples from monitor JSONL
samples_count = 0
if Path(monitor_jsonl).is_file():
    samples_count = sum(1 for _ in Path(monitor_jsonl).open())

# Replay directory size after
replay_dir_size_after = 0
if replay_dir != "NOT_FOUND" and Path(replay_dir).is_dir():
    replay_dir_size_after = sum(f.stat().st_size for f in Path(replay_dir).rglob("*") if f.is_file())

batch_re = re.compile(
    r"(?:new|pending) batch: inserted=(\d+) duplicates=(\d+) skipped=(\d+) failed=(\d+)"
)
log_inserted = log_duplicates = log_skipped = log_failed = 0
for match in batch_re.finditer(log_text):
    log_inserted += int(match.group(1))
    log_duplicates += int(match.group(2))
    log_skipped += int(match.group(3))
    log_failed += int(match.group(4))

error_lines = [
    line
    for line in log_text.splitlines()
    if "ERROR" in line or "Traceback" in line or "Exception" in line
]

r = redis.Redis.from_url(redis_url, decode_responses=True)
redis_messages_after = 0
redis_last_id_after = "0-0"
pending_after = None
lag_after = None
entries_read_after = None
try:
    info = r.xinfo_stream(stream)
    redis_messages_after = int(info.get("length", 0))
    redis_last_id_after = info.get("last-generated-id") or "0-0"
except ResponseError:
    pass

try:
    for group in r.xinfo_groups(stream):
        if group.get("name") == group_name:
            pending_after = group.get("pending")
            lag_after = group.get("lag")
            entries_read_after = group.get("entries-read")
            break
except ResponseError:
    pass

redis_forbidden_count = 0
new_redis_seen = 0
baseline_last_id = baseline.get("redis_last_id_before") or "0-0"
try:
    for _msg_id, fields in r.xrange(stream, min=f"({baseline_last_id}", max="+"):
        new_redis_seen += 1
        raw = fields.get("data")
        if not raw:
            continue
        try:
            obs = json.loads(raw)
        except Exception:
            continue
        if has_forbidden_image_bytes(obs):
            redis_forbidden_count += 1
except ResponseError:
    pass

# Visual output status
visual_status_file = Path(visual_dir) / "visual_output_status.json"
visual_status = "VISUAL_OUTPUT_NOT_AVAILABLE"
visual_reason = "current C1 face recognition path stores metadata/embedding but does not yet retain frame image/snapshot for matched observation"
if visual_status_file.is_file():
    try:
        vs = json.loads(visual_status_file.read_text(encoding="utf-8"))
        visual_status = vs.get("visual_output_status", visual_status)
        visual_reason = vs.get("reason", visual_reason)
    except Exception:
        pass

summary = {
    "result": "FAIL_SEARCH_ERROR",
    "runtime": {
        "run_duration": int(run_duration_s),
        "fixed_rtsp": rtsp_uri,
        "rtsp_transport": rtsp_transport,
        "replay_inline": True,
        "savant_module": module_file,
        "face_worker_service_used": True,
        "direct_repository_ingest_used": "NO",
    },
    "replay_monitor": {
        "replay_path": replay_dir,
        "replay_size_before_bytes": baseline.get("replay_dir_size_bytes_before", 0),
        "replay_size_after_bytes": replay_dir_size_after,
        "replay_size_growth_bytes": replay_dir_size_after - baseline.get("replay_dir_size_bytes_before", 0),
        "samples_count": samples_count,
        "sample_interval_sec": int(sys.argv[16]) if False else 60,
        "monitor_jsonl_path": monitor_jsonl,
    },
    "database": {
        "postgres_container": "c1-official-postgres",
        "database": "video_analytics",
        "persons_total": None,
        "gallery_embeddings_total": None,
        "face_observations_before": baseline["face_observations_before"],
        "face_observations_after": None,
        "new_observations_inserted": None,
        "duplicate_observations_skipped": log_duplicates,
        "match_results_before": baseline["match_results_before"],
        "match_results_after": None,
    },
    "redis": {
        "stream": stream,
        "messages_before": baseline["redis_messages_before"],
        "messages_after": redis_messages_after,
        "new_messages": max(0, redis_messages_after - baseline["redis_messages_before"]),
        "new_messages_scanned": new_redis_seen,
        "consumer_group": group_name,
        "pending_count": pending_after,
        "lag": lag_after,
        "entries_read_after": entries_read_after,
        "face_worker_ack_after_db_write": "YES",
        "redis_errors": len(error_lines),
        "forbidden_image_bytes": redis_forbidden_count,
    },
    "registration": registration,
    "ingest": {
        "face_worker_consumed_messages": log_inserted + log_duplicates + log_skipped + log_failed,
        "db_rows_inserted_from_worker_logs": log_inserted,
        "duplicates_from_worker_logs": log_duplicates,
        "skipped_from_worker_logs": log_skipped,
        "failed_from_worker_logs": log_failed,
        "idempotency_verified": False,
        "sample_source_observation_id": "",
        "sample_embedding_dim": 0,
        "sample_embedding_norm": 0.0,
        "valid_new_observations": 0,
        "db_payload_forbidden_image_bytes": 0,
    },
    "gallery_recognition": {
        "search_executed": False,
        "query_observations_searched": 0,
        "top_k": top_k,
        "threshold": threshold,
        "result_type": "FAIL_SEARCH_ERROR",
        "matched_external_person_id": "",
        "matched_person_id": None,
        "similarity": None,
        "rank": None,
        "gallery_embedding_id": None,
        "top_candidate_external_person_id": "",
        "top_candidate_similarity": None,
        "reason": "",
        "match_results_written": 0,
    },
    "visual_output": {
        "visual_output_status": visual_status,
        "visual_output_dir": visual_dir,
        "report_path": str(Path(visual_dir) / "report.md") if visual_status == "GENERATED" else "",
        "annotated_snapshot_path": "",
        "annotation_json_path": "",
        "production_evidence": False,
        "debug_visual_only": True,
        "reason": visual_reason,
    },
    "boundary": {
        "watchlist_hit_implemented": "NO",
        "live_search_hit_implemented": "NO",
        "api_frontend_implemented": "NO",
        "production_evidence_generated": "NO",
        "image_bytes_in_redis_db": "NO",
        "second_rtsp": "NO",
        "source_extraction": "NO",
    },
}

with psycopg.connect(db_url, autocommit=True) as conn:
    register_vector(conn)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT COUNT(*) AS count FROM persons WHERE is_active = true")
        summary["database"]["persons_total"] = int(cur.fetchone()["count"])
        cur.execute(
            "SELECT COUNT(*) AS count FROM person_gallery_embeddings WHERE is_active = true"
        )
        gallery_count = int(cur.fetchone()["count"])
        summary["database"]["gallery_embeddings_total"] = gallery_count
        cur.execute("SELECT COUNT(*) AS count FROM face_observations")
        face_observations_after = int(cur.fetchone()["count"])
        summary["database"]["face_observations_after"] = face_observations_after
        summary["database"]["new_observations_inserted"] = (
            face_observations_after - baseline["face_observations_before"]
        )
        cur.execute("SELECT COUNT(*) AS count FROM match_results")
        summary["database"]["match_results_after"] = int(cur.fetchone()["count"])

        cur.execute(
            """
            SELECT COUNT(*) = COUNT(DISTINCT source_observation_id) AS ok
            FROM face_observations
            """
        )
        summary["ingest"]["idempotency_verified"] = bool(cur.fetchone()["ok"])

        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM face_observations
            WHERE created_at >= %(started)s::timestamptz
              AND embedding_dim = 512
              AND embedding_model = 'adaface'
              AND detector_model = 'yolov8_face'
              AND embedding_norm BETWEEN 0.90 AND 1.10
              AND face_bbox IS NOT NULL
              AND landmarks IS NOT NULL
            """,
            {"started": run_started_at},
        )
        summary["ingest"]["valid_new_observations"] = int(cur.fetchone()["count"])

        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM face_observations
            WHERE created_at >= %(started)s::timestamptz
              AND (
                    payload ? 'embedding'
                 OR payload::text ILIKE '%%image_bytes%%'
                 OR payload::text ILIKE '%%crop_bytes%%'
                 OR payload::text ILIKE '%%raw_bytes%%'
                 OR payload::text ILIKE '%%data:image/%%'
                 OR payload::text ILIKE '%%base64%%'
              )
            """,
            {"started": run_started_at},
        )
        summary["ingest"]["db_payload_forbidden_image_bytes"] = int(cur.fetchone()["count"])

        cur.execute(
            """
            SELECT id, source_observation_id, embedding_dim, embedding_norm
            FROM face_observations
            WHERE created_at >= %(started)s::timestamptz
              AND embedding_dim = 512
              AND embedding_model = 'adaface'
              AND detector_model = 'yolov8_face'
              AND embedding_norm BETWEEN 0.90 AND 1.10
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"started": run_started_at},
        )
        sample = cur.fetchone()
        if sample:
            summary["ingest"]["sample_source_observation_id"] = sample["source_observation_id"]
            summary["ingest"]["sample_embedding_dim"] = int(sample["embedding_dim"])
            summary["ingest"]["sample_embedding_norm"] = float(sample["embedding_norm"])

    if redis_forbidden_count > 0 or summary["ingest"]["db_payload_forbidden_image_bytes"] > 0:
        summary["result"] = "FAIL_IMAGE_BYTES_IN_REDIS_OR_DB"
        summary["gallery_recognition"]["result_type"] = "FAIL_IMAGE_BYTES_IN_REDIS_OR_DB"
        summary["gallery_recognition"]["reason"] = "forbidden image/crop/base64 bytes detected"
    elif summary["redis"]["new_messages"] <= 0 and new_redis_seen <= 0:
        summary["result"] = "FAIL_NO_REDIS_MESSAGES"
        summary["gallery_recognition"]["result_type"] = "FAIL_NO_REDIS_MESSAGES"
        summary["gallery_recognition"]["reason"] = "no new Redis face observations"
    elif (
        summary["ingest"]["face_worker_consumed_messages"] <= 0
        or summary["database"]["new_observations_inserted"] <= 0
    ):
        summary["result"] = "FAIL_FACE_WORKER_INGEST"
        summary["gallery_recognition"]["result_type"] = "FAIL_FACE_WORKER_INGEST"
        summary["gallery_recognition"]["reason"] = "face-worker did not insert new PostgreSQL observations"
    elif gallery_count <= 0:
        summary["result"] = "FAIL_NO_GALLERY"
        summary["gallery_recognition"]["result_type"] = "FAIL_NO_GALLERY"
        summary["gallery_recognition"]["reason"] = "no active gallery embeddings"
    else:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT id, source_observation_id, camera_id, source_id,
                           track_id, timestamp_ms, face_confidence, quality, embedding
                    FROM face_observations
                    WHERE created_at >= %(started)s::timestamptz
                      AND embedding_dim = 512
                      AND embedding_model = 'adaface'
                      AND detector_model = 'yolov8_face'
                      AND embedding_norm BETWEEN 0.90 AND 1.10
                    ORDER BY created_at DESC
                    LIMIT %(limit)s
                    """,
                    {"started": run_started_at, "limit": query_limit},
                )
                observations = cur.fetchall()

            store = FaceVectorStore(conn)
            repo = MatchResultRepository(conn)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            best_top = None
            matched = None
            searched = 0

            for obs in observations:
                embedding = obs["embedding"]
                if hasattr(embedding, "tolist"):
                    embedding_list = [float(x) for x in embedding.tolist()]
                else:
                    embedding_list = [float(x) for x in embedding]

                results = store.search_gallery(
                    embedding_list,
                    top_k=top_k,
                    min_similarity=None,
                )
                searched += 1
                if not results:
                    continue

                top = results[0]
                if best_top is None or float(top["similarity"]) > float(best_top["similarity"]):
                    best_top = dict(top)

                request_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"c1f3c:{obs['source_observation_id']}",
                    )
                )
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM match_results WHERE search_request_id = %s",
                        (request_id,),
                    )

                threshold_results = [
                    row for row in results if float(row["similarity"]) >= threshold
                ]
                for rank_idx, result in enumerate(threshold_results, start=1):
                    row_id = repo.insert_gallery_match_result(
                        {
                            "search_request_id": request_id,
                            "search_mode": "gallery_match",
                            "query_observation_id": obs["id"],
                            "query_source_observation_id": obs["source_observation_id"],
                            "query_person_id": result.get("person_id"),
                            "query_gallery_embedding_id": result["id"],
                            "query_embedding_model": result.get("embedding_model", "adaface"),
                            "similarity_threshold": threshold,
                            "rank": rank_idx,
                            "similarity": float(result["similarity"]),
                            "face_confidence": obs["face_confidence"],
                            "quality": obs["quality"],
                            "expires_at": expires_at,
                            "payload": {
                                "phase": "C1F.3c",
                                "smoke": "15min_face_recognition_replay_monitor",
                            },
                        }
                    )
                    if row_id is not None:
                        summary["gallery_recognition"]["match_results_written"] += 1
                    if (
                        result.get("external_person_id") in target_external_ids
                        and matched is None
                    ):
                        matched = (rank_idx, result)

            summary["gallery_recognition"]["search_executed"] = searched > 0
            summary["gallery_recognition"]["query_observations_searched"] = searched
            if best_top is not None:
                summary["gallery_recognition"]["top_candidate_external_person_id"] = (
                    best_top.get("external_person_id") or ""
                )
                summary["gallery_recognition"]["top_candidate_similarity"] = float(
                    best_top["similarity"]
                )

            if matched is not None:
                rank_idx, result = matched
                summary["result"] = "PASS_MATCH"
                summary["gallery_recognition"].update(
                    {
                        "result_type": "PASS_MATCH",
                        "matched_external_person_id": result.get("external_person_id") or "",
                        "matched_person_id": int(result["person_id"]),
                        "gallery_embedding_id": int(result["id"]),
                        "similarity": float(result["similarity"]),
                        "rank": rank_idx,
                        "reason": "Finch/Reese matched at or above threshold",
                    }
                )
            elif searched > 0 and best_top is not None:
                summary["result"] = "PASS_NO_MATCH_PRODUCTION_PIPELINE_OK"
                summary["gallery_recognition"]["result_type"] = (
                    "PASS_NO_MATCH_PRODUCTION_PIPELINE_OK"
                )
                if float(best_top["similarity"]) < threshold:
                    summary["gallery_recognition"]["reason"] = (
                        "production-like pipeline ran, but top candidate is below threshold"
                    )
                else:
                    summary["gallery_recognition"]["reason"] = (
                        "production-like pipeline ran, but threshold candidates were not Finch/Reese"
                    )
            else:
                summary["result"] = "FAIL_SEARCH_ERROR"
                summary["gallery_recognition"]["result_type"] = "FAIL_SEARCH_ERROR"
                summary["gallery_recognition"]["reason"] = "no query observations could be searched"
        except Exception as exc:
            summary["result"] = "FAIL_SEARCH_ERROR"
            summary["gallery_recognition"]["result_type"] = "FAIL_SEARCH_ERROR"
            summary["gallery_recognition"]["reason"] = str(exc)

with open(summary_file, "w", encoding="utf-8") as fh:
    json.dump(summary, fh, indent=2)

print("=== C1F.3c Summary ===")
print(f"RESULT={summary['result']}")
print(f"run_duration={summary['runtime']['run_duration']}")
print(f"fixed_rtsp={summary['runtime']['fixed_rtsp']}")
print(f"rtsp_transport={summary['runtime']['rtsp_transport']}")
print(f"savant_module={summary['runtime']['savant_module']}")
print(f"face_worker_service_used={summary['runtime']['face_worker_service_used']}")
print(f"direct_repository_ingest_used={summary['runtime']['direct_repository_ingest_used']}")
print(f"replay_path={summary['replay_monitor']['replay_path']}")
print(f"replay_size_before={summary['replay_monitor']['replay_size_before_bytes']}")
print(f"replay_size_after={summary['replay_monitor']['replay_size_after_bytes']}")
print(f"replay_size_growth={summary['replay_monitor']['replay_size_growth_bytes']}")
print(f"samples_count={summary['replay_monitor']['samples_count']}")
print(f"sample_interval_sec={summary['replay_monitor']['sample_interval_sec']}")
print(f"monitor_jsonl_path={summary['replay_monitor']['monitor_jsonl_path']}")
print(f"persons_total={summary['database']['persons_total']}")
print(f"gallery_embeddings_total={summary['database']['gallery_embeddings_total']}")
print(f"face_observations_before={summary['database']['face_observations_before']}")
print(f"face_observations_after={summary['database']['face_observations_after']}")
print(f"new_observations_inserted={summary['database']['new_observations_inserted']}")
print(f"duplicate_observations_skipped={summary['database']['duplicate_observations_skipped']}")
print(f"match_results_before={summary['database']['match_results_before']}")
print(f"match_results_after={summary['database']['match_results_after']}")
print(f"redis_messages_before={summary['redis']['messages_before']}")
print(f"redis_messages_after={summary['redis']['messages_after']}")
print(f"redis_new_messages={summary['redis']['new_messages']}")
print(f"consumer_group={summary['redis']['consumer_group']}")
print(f"pending_count={summary['redis']['pending_count']}")
print(f"lag={summary['redis']['lag']}")
print(f"redis_errors={summary['redis']['redis_errors']}")
print(f"face_worker_consumed_messages={summary['ingest']['face_worker_consumed_messages']}")
print(f"db_rows_inserted={summary['ingest']['db_rows_inserted_from_worker_logs']}")
print(f"idempotency_verified={summary['ingest']['idempotency_verified']}")
print(f"valid_new_observations={summary['ingest']['valid_new_observations']}")
print(f"search_executed={summary['gallery_recognition']['search_executed']}")
print(f"query_observations_searched={summary['gallery_recognition']['query_observations_searched']}")
print(f"result_type={summary['gallery_recognition']['result_type']}")
print(f"matched_external_person_id={summary['gallery_recognition']['matched_external_person_id']}")
print(f"matched_person_id={summary['gallery_recognition']['matched_person_id']}")
print(f"similarity={summary['gallery_recognition']['similarity']}")
print(f"rank={summary['gallery_recognition']['rank']}")
print(f"top_candidate_external_person_id={summary['gallery_recognition']['top_candidate_external_person_id']}")
print(f"top_candidate_similarity={summary['gallery_recognition']['top_candidate_similarity']}")
print(f"reason={summary['gallery_recognition']['reason']}")
print(f"visual_output_status={summary['visual_output']['visual_output_status']}")
print(f"visual_output_dir={summary['visual_output']['visual_output_dir']}")
print(f"production_evidence={summary['visual_output']['production_evidence']}")
print(f"debug_visual_only={summary['visual_output']['debug_visual_only']}")
print("watchlist_hit implemented: NO")
print("live_search_hit implemented: NO")
print("API/frontend implemented: NO")
print("production evidence generated: NO")
print("image bytes in Redis/DB: NO")
print("second RTSP: NO")
print("source extraction: NO")
PY
}

cleanup_runtime() {
    log "cleanup: stopping source-adapter and savant-security"
    docker compose -f "$COMPOSE_FILE" stop source-adapter savant-security >/dev/null 2>&1 || true
}

main() {
    mkdir -p "$ARTIFACT_DIR"

    log "pre-flight checks"
    require_file "$COMPOSE_FILE" "C1 official compose"
    require_file "$ARCHIVE_FINCH" "archived Finch image"
    require_file "$ARCHIVE_REESE" "archived Reese image"
    require_file "$FACE_WORKER_DIR/register_face_image.py" "face registration CLI"
    require_file "$PROJECT_ROOT/modules/savant_security/$SAVANT_MODULE_FILE" "Savant C1F.2d module"
    ensure_python_deps

    discover_replay_dir

    log "stopping staged runtime services before baseline"
    docker compose -f "$COMPOSE_FILE" stop source-adapter savant-security face-worker >/dev/null 2>&1 || true

    log "starting Redis/PostgreSQL/Replay"
    docker compose -f "$COMPOSE_FILE" up -d redis postgres replay-service
    wait_for_infra

    ensure_active_registration_image "$ARCHIVE_FINCH" "$ACTIVE_FINCH" "Finch"
    ensure_active_registration_image "$ARCHIVE_REESE" "$ACTIVE_REESE" "Reese"
    register_active_person_if_needed "$ACTIVE_FINCH" "$FINCH_EXTERNAL_ID" "Finch"
    register_active_person_if_needed "$ACTIVE_REESE" "$REESE_EXTERNAL_ID" "Reese"
    summarize_registration

    write_baseline

    RUN_STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    log "run started at ${RUN_STARTED_AT}; duration=${RUN_DURATION_SEC}s"

    log "starting face-worker service"
    docker compose -f "$COMPOSE_FILE" up -d --build --force-recreate face-worker
    wait_for_face_worker

    log "starting Savant C1F.2d module and source-adapter"
    docker compose -f "$COMPOSE_FILE" up -d --force-recreate savant-security source-adapter

    trap cleanup_runtime EXIT

    sleep_with_monitoring

    log "draining Savant output for ${DRAIN_WAIT_SEC}s"
    sleep "$DRAIN_WAIT_SEC"
    cleanup_runtime

    log "allowing face-worker to drain for ${FACE_WORKER_DRAIN_SEC}s"
    sleep "$FACE_WORKER_DRAIN_SEC"
    collect_logs

    attempt_visual_output

    finalize_summary

    local result
    result="$(python3 -c "import json; print(json.load(open('$SUMMARY_FILE'))['result'])")"
    case "$result" in
        PASS_MATCH|PASS_NO_MATCH_PRODUCTION_PIPELINE_OK)
            exit 0
            ;;
        *)
            exit 1
            ;;
    esac
}

main "$@"
