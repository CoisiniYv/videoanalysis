#!/usr/bin/env bash
# C1F.4a — Face match event evidence bundle + continuous overlay JSON.
#
# Main path under test:
#   face_observations + gallery
#   -> services/face-worker/emit_face_match_events.py
#   -> Redis security.events
#   -> event-worker -> security.record_requests
#   -> clip-worker -> Replay job -> video-file-sink
#   -> media-worker evidence bundle with raw_clip + annotations.jsonl

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/infra/docker-compose.c1-official-replay-dev.yml"
FACE_WORKER_DIR="$PROJECT_ROOT/services/face-worker"

DB_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
export DATABASE_URL="$DB_URL"

REDIS_URL="${C1F4A_REDIS_URL:-redis://localhost:6385/0}"
EVENT_STREAM="security.events"
RECORD_REQUEST_STREAM="security.record_requests"
SOURCE_ID="${C1F4A_SOURCE_ID:-c1e_rtsp_replay}"
FIXED_RTSP="rtsp://10.37.57.112:8554/live/1080movie"

MATCH_THRESHOLD="${C1F4A_MATCH_THRESHOLD:-0.5}"
OBSERVATION_LIMIT="${C1F4A_OBSERVATION_LIMIT:-200}"
TOP_K="${C1F4A_TOP_K:-1}"
EVENT_WAIT_SEC="${C1F4A_EVENT_WAIT_SEC:-90}"
RECORD_WAIT_SEC="${C1F4A_RECORD_WAIT_SEC:-90}"
REPLAY_WAIT_SEC="${C1F4A_REPLAY_WAIT_SEC:-120}"
EVIDENCE_WAIT_SEC="${C1F4A_EVIDENCE_WAIT_SEC:-240}"
OBSERVATION_BOOTSTRAP_SEC="${C1F4A_OBSERVATION_BOOTSTRAP_SEC:-90}"

export SAVANT_MODULE_FILE="module.c1f2d_face_observation_redis.yml"
export FACE_OBSERVATION_EXPORT_ENABLED="true"
export C1F1_SAME_FRAME_DEBUG_ENABLED="0"
export RTSP_TRANSPORT="${RTSP_TRANSPORT:-tcp}"
export C1E_RTSP_TRANSPORT="$RTSP_TRANSPORT"

ARCHIVE_FINCH="/data/video-analytics/archive/r2_3_20260529_052937/repo_uncommitted/face/finch.jpg"
ARCHIVE_REESE="/data/video-analytics/archive/r2_3_20260529_052937/repo_uncommitted/face/reese.jpg"
ACTIVE_REGISTRATION_DIR="${C1F4A_ACTIVE_REGISTRATION_DIR:-/data/video-analytics/media/face-registration}"
ACTIVE_FINCH="$ACTIVE_REGISTRATION_DIR/finch.jpg"
ACTIVE_REESE="$ACTIVE_REGISTRATION_DIR/reese.jpg"
FINCH_EXTERNAL_ID="test:archive:finch"
REESE_EXTERNAL_ID="test:archive:reese"

ARTIFACT_DIR="${C1F4A_ARTIFACT_DIR:-/data/video-analytics/artifacts/c1f4a}"
EMIT_FILE="$ARTIFACT_DIR/emit_face_match_events.json"
EVENT_FILE="$ARTIFACT_DIR/event_row.json"
RECORD_FILE="$ARTIFACT_DIR/record_request.json"
REPLAY_FILE="$ARTIFACT_DIR/replay_job.json"
VERIFY_FILE="$ARTIFACT_DIR/evidence_verify.json"
SUMMARY_FILE="$ARTIFACT_DIR/c1f4a_summary.json"

THRESHOLD_CALIBRATION="false"
if [ "$MATCH_THRESHOLD" != "0.5" ] && [ "$MATCH_THRESHOLD" != "0.50" ]; then
    THRESHOLD_CALIBRATION="true"
fi

log() {
    echo "[C1F.4a] $(date '+%H:%M:%S') $*"
}

fail_result() {
    local result="$1"
    local reason="${2:-}"
    write_final_summary "$result" "$reason" || true
    echo "RESULT=${result}"
    if [ -n "$reason" ]; then
        echo "REASON=${reason}"
    fi
    exit 1
}

host_media_path() {
    local path="$1"
    if [[ "$path" == /media/* ]]; then
        echo "/data/video-analytics/media/${path#/media/}"
    else
        echo "$path"
    fi
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
            return 0
        fi
        sleep 1
    done
    fail_result "FAIL_INFRA_UNHEALTHY" "Redis/PostgreSQL did not become healthy"
}

wait_for_container_running() {
    local container="$1"
    local label="$2"
    for _ in $(seq 1 45); do
        if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || echo false)" = "true" ]; then
            return 0
        fi
        sleep 1
    done
    fail_result "FAIL_SERVICE_NOT_RUNNING" "${label} did not start"
}

start_core_services() {
    log "starting current C1 official services for event evidence path"
    docker compose -f "$COMPOSE_FILE" up -d --build \
        redis postgres replay-service video-file-sink
    wait_for_infra
    docker compose -f "$COMPOSE_FILE" up -d --build --force-recreate \
        face-worker event-worker clip-worker media-worker
    wait_for_container_running c1-official-face-worker "face-worker"
    wait_for_container_running c1-official-event-worker "event-worker"
    wait_for_container_running c1-official-clip-worker "clip-worker"
    wait_for_container_running c1-official-media-worker "media-worker"
    wait_for_container_running c1-official-video-file-sink "video-file-sink"
    wait_for_container_running c1-official-replay-service "replay-service"
}

ensure_active_registration_image() {
    local archive_path="$1"
    local active_path="$2"
    local label="$3"

    require_file "$archive_path" "$label archived image"
    mkdir -p "$ACTIVE_REGISTRATION_DIR"
    if [ ! -f "$active_path" ] || ! cmp -s "$archive_path" "$active_path"; then
        log "copying ${label} image to active registration media"
        install -m 0644 "$archive_path" "$active_path"
    fi
}

active_registration_valid() {
    local image="$1"
    local external_id="$2"
    python3 - "$DB_URL" "$image" "$external_id" <<'PY'
import sys

import psycopg
from psycopg.rows import dict_row

db_url, image_path, external_id = sys.argv[1:4]
with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT p.id AS person_id, pge.id AS gallery_embedding_id
            FROM persons p
            JOIN person_gallery_embeddings pge ON pge.person_id = p.id
            WHERE p.external_person_id = %s
              AND p.is_active = true
              AND pge.is_active = true
              AND pge.source_image_path = %s
              AND pge.embedding_model = 'adaface'
              AND pge.embedding_dim = 512
              AND pge.embedding_norm BETWEEN 0.90 AND 1.10
            LIMIT 1
            """,
            (external_id, image_path),
        )
        sys.exit(0 if cur.fetchone() else 2)
PY
}

register_active_person_if_needed() {
    local image="$1"
    local external_id="$2"
    local name="$3"
    local out_file="$ARTIFACT_DIR/register_${name,,}.json"

    if active_registration_valid "$image" "$external_id"; then
        log "active registration valid: ${external_id}"
        return 0
    fi

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
            --created-by "c1f4a_evidence_overlay_smoke" \
            --output-json
    ) >"$out_file.tmp" 2>&1; then
        cat "$out_file.tmp"
        fail_result "FAIL_NO_GALLERY" "registration failed for ${external_id}"
    fi
    mv "$out_file.tmp" "$out_file"
}

ensure_gallery() {
    ensure_active_registration_image "$ARCHIVE_FINCH" "$ACTIVE_FINCH" "finch"
    ensure_active_registration_image "$ARCHIVE_REESE" "$ACTIVE_REESE" "reese"
    register_active_person_if_needed "$ACTIVE_FINCH" "$FINCH_EXTERNAL_ID" "Finch"
    register_active_person_if_needed "$ACTIVE_REESE" "$REESE_EXTERNAL_ID" "Reese"

    python3 - "$DB_URL" "$FINCH_EXTERNAL_ID" "$REESE_EXTERNAL_ID" <<'PY'
import sys

import psycopg

db_url, finch, reese = sys.argv[1:4]
with psycopg.connect(db_url) as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM persons p
            JOIN person_gallery_embeddings pge ON pge.person_id = p.id
            WHERE p.external_person_id = ANY(%s)
              AND p.is_active = true
              AND pge.is_active = true
              AND pge.embedding_dim = 512
              AND pge.embedding_norm BETWEEN 0.90 AND 1.10
            """,
            ([finch, reese],),
        )
        count = cur.fetchone()[0]
if count < 2:
    sys.exit(2)
PY
}

anchored_observation_count() {
    python3 - "$DB_URL" "$SOURCE_ID" <<'PY'
import sys

import psycopg

db_url, source_id = sys.argv[1:3]
with psycopg.connect(db_url) as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM face_observations
            WHERE source_id = %s
              AND embedding_dim = 512
              AND embedding_norm BETWEEN 0.90 AND 1.10
              AND payload->'media'->>'frame_uuid' IS NOT NULL
              AND (
                    payload->'media'->>'previous_keyframe_uuid' IS NOT NULL
                 OR payload->'media'->>'keyframe_uuid' IS NOT NULL
              )
            """,
            (source_id,),
        )
        print(cur.fetchone()[0])
PY
}

bootstrap_observations_if_needed() {
    local count
    count="$(anchored_observation_count | tr -d '[:space:]')"
    if [ "${count:-0}" -gt 0 ]; then
        log "anchored face observations available: ${count}"
        return 0
    fi

    log "no anchored observations; running short production-like observation generation"
    docker compose -f "$COMPOSE_FILE" up -d --build savant-security source-adapter
    sleep "$OBSERVATION_BOOTSTRAP_SEC"
    count="$(anchored_observation_count | tr -d '[:space:]')"
    if [ "${count:-0}" -le 0 ]; then
        fail_result "FAIL_NO_OBSERVATIONS" "face-worker did not ingest anchored observations"
    fi
    log "anchored face observations after bootstrap: ${count}"
}

emit_face_match_event() {
    log "emitting face match event through existing face_match_event_service"
    if ! (
        cd "$FACE_WORKER_DIR"
        REDIS_URL="$REDIS_URL" DATABASE_URL="$DB_URL" \
            python3 emit_face_match_events.py \
                --source-id "$SOURCE_ID" \
                --external-person-id "$FINCH_EXTERNAL_ID" \
                --external-person-id "$REESE_EXTERNAL_ID" \
                --threshold "$MATCH_THRESHOLD" \
                --top-k "$TOP_K" \
                --observation-limit "$OBSERVATION_LIMIT" \
                --event-stream "$EVENT_STREAM" \
                --output-json
    ) >"$EMIT_FILE.tmp"; then
        cat "$EMIT_FILE.tmp" || true
        fail_result "FAIL_SEARCH_ERROR" "emit_face_match_events.py failed"
    fi
    mv "$EMIT_FILE.tmp" "$EMIT_FILE"
}

extract_emit_field() {
    local field="$1"
    python3 - "$EMIT_FILE" "$field" <<'PY'
import json
import sys

path, field = sys.argv[1:3]
data = json.loads(open(path, encoding="utf-8").read())
candidate = (data.get("top_candidates") or [{}])[0]
event = (data.get("emitted_events") or [{}])[0]
payload = event.get("payload") or {}
event_match = payload.get("match") or {}
event_person = payload.get("matched_person") or {}
values = {
    "events_emitted": data.get("events_emitted", 0),
    "source_event_id": event.get("source_event_id") or candidate.get("source_event_id", ""),
    "source_observation_id": event_match.get("source_observation_id") or candidate.get("source_observation_id", ""),
    "matched_external_person_id": event_person.get("external_person_id") or candidate.get("external_person_id", ""),
    "matched_person_id": event_person.get("person_id") or candidate.get("person_id", ""),
    "similarity": event_match.get("similarity") or candidate.get("similarity", ""),
    "threshold": event_match.get("threshold") or data.get("threshold", ""),
}
print(values.get(field, ""))
PY
}

wait_for_event_ingested() {
    local source_event_id="$1"
    log "waiting for event-worker DB ingest: ${source_event_id}"
    for _ in $(seq 1 "$EVENT_WAIT_SEC"); do
        if python3 - "$DB_URL" "$source_event_id" "$EVENT_FILE" <<'PY'
import json
import sys

import psycopg
from psycopg.rows import dict_row

db_url, source_event_id, out_file = sys.argv[1:4]
with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, source_event_id, event_type, camera_id, source_id,
                   track_id, event_ts_ms, frame_uuid, keyframe_uuid, payload
            FROM events
            WHERE source_event_id = %s
            """,
            (source_event_id,),
        )
        row = cur.fetchone()
if not row:
    sys.exit(2)
data = dict(row)
data["id"] = str(data["id"])
with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, default=str)
PY
        then
            return 0
        fi
        sleep 1
    done
    fail_result "FAIL_EVENT_NOT_INGESTED" "$source_event_id"
}

wait_for_record_request() {
    local source_event_id="$1"
    log "waiting for event-worker record_request"
    for _ in $(seq 1 "$RECORD_WAIT_SEC"); do
        if python3 - "$REDIS_URL" "$RECORD_REQUEST_STREAM" "$source_event_id" "$RECORD_FILE" <<'PY'
import json
import sys

from redis import Redis

redis_url, stream, source_event_id, out_file = sys.argv[1:5]
r = Redis.from_url(redis_url, decode_responses=True)
for _msg_id, fields in r.xrange(stream, "-", "+"):
    raw = fields.get("data")
    if not raw:
        continue
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        continue
    if data.get("source_event_id") == source_event_id:
        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        sys.exit(0)
sys.exit(2)
PY
        then
            return 0
        fi
        sleep 1
    done
    fail_result "FAIL_RECORD_REQUEST_NOT_CREATED" "$source_event_id"
}

wait_for_replay_job() {
    local event_id="$1"
    log "waiting for clip-worker Replay job"
    for _ in $(seq 1 "$REPLAY_WAIT_SEC"); do
        if python3 - "$DB_URL" "$event_id" "$REPLAY_FILE" <<'PY'
import json
import sys

import psycopg
from psycopg.rows import dict_row

db_url, event_id, out_file = sys.argv[1:4]
with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT payload->'media'->>'replay_job_id' AS replay_job_id,
                   payload->'media'->'replay_job_request' AS replay_job_request,
                   payload->'media'->>'clip_status' AS clip_status
            FROM events
            WHERE id = %s::uuid
            """,
            (event_id,),
        )
        row = cur.fetchone()
if not row or not row["replay_job_id"]:
    sys.exit(2)
data = dict(row)
with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, default=str)
PY
        then
            return 0
        fi
        sleep 1
    done
    fail_result "FAIL_REPLAY_JOB_NOT_CREATED" "$event_id"
}

wait_for_evidence_bundle() {
    local event_id="$1"
    log "waiting for media-worker evidence finalization"
    for _ in $(seq 1 "$EVIDENCE_WAIT_SEC"); do
        if python3 - "$DB_URL" "$event_id" "$VERIFY_FILE.db" <<'PY'
import json
import sys

import psycopg
from psycopg.rows import dict_row

db_url, event_id, out_file = sys.argv[1:4]
with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT clip_path,
                   payload->'media'->>'evidence_dir' AS evidence_dir,
                   payload->'media'->>'metadata_path' AS metadata_path,
                   payload->'media'->>'sink_metadata_path' AS sink_metadata_path,
                   payload->'media'->>'event_annotation_path' AS event_annotation_path,
                   payload->'media'->>'annotations_jsonl_path' AS annotations_jsonl_path,
                   payload->'media'->>'summary_json_path' AS summary_json_path,
                   payload->'media'->>'annotated_clip_status' AS annotated_clip_status,
                   payload->'media'->>'clip_status' AS clip_status
            FROM events
            WHERE id = %s::uuid
            """,
            (event_id,),
        )
        row = cur.fetchone()
if not row or not row["evidence_dir"] or not row["annotations_jsonl_path"]:
    sys.exit(2)
with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(dict(row), fh, indent=2, default=str)
PY
        then
            if ! verify_evidence_bundle "$VERIFY_FILE.db"; then
                if grep -q "raw_clip" "$VERIFY_FILE" 2>/dev/null; then
                    fail_result "FAIL_RAW_CLIP_NOT_GENERATED" "raw_clip missing_or_empty"
                fi
                if grep -q "embedding leak" "$VERIFY_FILE" 2>/dev/null; then
                    fail_result "FAIL_EMBEDDING_LEAK" "embedding leak detected"
                fi
                if grep -q "image/crop/base64 leak" "$VERIFY_FILE" 2>/dev/null; then
                    fail_result "FAIL_IMAGE_BYTES_LEAK" "image/crop/base64 leak detected"
                fi
                fail_result "FAIL_ANNOTATIONS_NOT_GENERATED" "evidence bundle verification failed"
            fi
            return 0
        fi
        sleep 1
    done
    fail_result "FAIL_ANNOTATIONS_NOT_GENERATED" "$event_id"
}

verify_evidence_bundle() {
    local db_file="$1"
    python3 - "$db_file" "$VERIFY_FILE" <<'PY'
import glob
import json
import os
import sys

db_file, out_file = sys.argv[1:3]
db = json.loads(open(db_file, encoding="utf-8").read())

def host_path(path: str) -> str:
    if path.startswith("/media/"):
        return "/data/video-analytics/media/" + path[len("/media/"):]
    return path

bundle_dir = host_path(db["evidence_dir"])
raw_clip_path = host_path(db["clip_path"] or "")
metadata_path = host_path(db["metadata_path"] or "")
sink_metadata_path = host_path(db["sink_metadata_path"] or "")
event_annotation_path = host_path(db["event_annotation_path"] or "")
annotations_path = host_path(db["annotations_jsonl_path"] or "")
summary_path = host_path(db["summary_json_path"] or "")

failures = []
for label, path in (
    ("bundle_dir", bundle_dir),
    ("raw_clip", raw_clip_path),
    ("metadata_json", metadata_path),
    ("sink_metadata_json", sink_metadata_path),
    ("event_annotation_json", event_annotation_path),
    ("annotations_jsonl", annotations_path),
    ("summary_json", summary_path),
):
    if label == "bundle_dir":
        if not os.path.isdir(path):
            failures.append(f"{label} missing: {path}")
    elif not os.path.isfile(path) or os.path.getsize(path) <= 0:
        failures.append(f"{label} missing_or_empty: {path}")

metadata = {}
summary = {}
lines = []
if os.path.isfile(metadata_path):
    metadata = json.loads(open(metadata_path, encoding="utf-8").read())
if os.path.isfile(summary_path):
    summary = json.loads(open(summary_path, encoding="utf-8").read())
if os.path.isfile(annotations_path):
    with open(annotations_path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))

if not lines:
    failures.append("annotations_jsonl has no lines")

has_bbox = any(obj.get("bbox") for line in lines for obj in line.get("objects", []))
has_style = any(
    obj.get("style", {}).get("bbox_color")
    for line in lines
    for obj in line.get("objects", [])
)
has_pose = all(
    "pose" in obj for line in lines for obj in line.get("objects", [])
)
has_action = all(
    "action" in obj for line in lines for obj in line.get("objects", [])
)
has_frame_anchor = any(
    line.get("frame_uuid") and line.get("keyframe_uuid") and line.get("frame_pts") is not None
    for line in lines
)
if not has_bbox:
    failures.append("annotations missing face bbox")
if not has_style:
    failures.append("annotations missing style.bbox_color")
if not has_pose:
    failures.append("annotations missing pose reserved field")
if not has_action:
    failures.append("annotations missing action reserved field")
if not has_frame_anchor:
    failures.append("annotations missing frame_uuid/keyframe_uuid/frame_pts")

joined = "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)
embedding_leaked = '"embedding"' in joined or bool(summary.get("embedding_leaked"))
image_bytes_leaked = any(
    token in joined.lower()
    for token in ("image_bytes", "crop_bytes", "raw_bytes", "base64", "data:image/")
) or bool(summary.get("image_bytes_leaked"))
if embedding_leaked:
    failures.append("embedding leak detected")
if image_bytes_leaked:
    failures.append("image/crop/base64 leak detected")

annotated = []
for pattern in ("annotated_clip.mp4", "annotated_clip.mov", "annotated_clip.webm"):
    annotated.extend(glob.glob(os.path.join(bundle_dir, pattern)))
if annotated:
    failures.append("annotated_clip generated")

annotations_meta = metadata.get("annotations", {})
media_meta = metadata.get("media", {})
if annotations_meta.get("annotation_mode") != "continuous_jsonl":
    failures.append("metadata annotation_mode is not continuous_jsonl")
if annotations_meta.get("frontend_overlay_required") is not True:
    failures.append("metadata frontend_overlay_required is not true")
if media_meta.get("annotated_clip_status") != "not_generated":
    failures.append("metadata annotated_clip_status is not_generated missing")

result = {
    "ok": not failures,
    "failures": failures,
    "bundle_dir": bundle_dir,
    "raw_clip_path": raw_clip_path,
    "raw_clip_size": os.path.getsize(raw_clip_path) if os.path.isfile(raw_clip_path) else 0,
    "metadata_json": metadata_path,
    "sink_metadata_json": sink_metadata_path,
    "event_annotation_json": event_annotation_path,
    "annotations_jsonl": annotations_path,
    "summary_json": summary_path,
    "annotation_lines": len(lines),
    "face_objects": summary.get("face_objects", 0),
    "matched_objects": summary.get("matched_objects", 0),
    "low_similarity_objects": summary.get("low_similarity_objects", 0),
    "unknown_objects": summary.get("unknown_objects", 0),
    "pose_status": "unavailable" if summary.get("pose_unavailable_objects", 0) else "",
    "action_status": "none" if summary.get("action_none_objects", 0) else "",
    "colors_used": summary.get("colors_used", []),
    "embedding_leaked": embedding_leaked,
    "image_bytes_leaked": image_bytes_leaked,
    "annotated_clip_generated": bool(annotated),
}
with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(result, fh, indent=2)
if failures:
    print("; ".join(failures))
    sys.exit(2)
PY
}

json_field() {
    local file="$1"
    local expr="$2"
    python3 - "$file" "$expr" <<'PY'
import json
import sys

data = json.loads(open(sys.argv[1], encoding="utf-8").read())
value = data
for part in sys.argv[2].split("."):
    if not part:
        continue
    if isinstance(value, dict):
        value = value.get(part, "")
    else:
        value = ""
        break
if isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False))
elif value is None:
    print("")
else:
    print(value)
PY
}

write_final_summary() {
    local result="${1:-UNKNOWN}"
    local reason="${2:-}"
    python3 - "$SUMMARY_FILE" "$result" "$reason" "$EMIT_FILE" "$EVENT_FILE" "$RECORD_FILE" "$REPLAY_FILE" "$VERIFY_FILE" "$MATCH_THRESHOLD" "$THRESHOLD_CALIBRATION" "$FIXED_RTSP" "$RTSP_TRANSPORT" <<'PY'
import json
import os
import sys

(
    out_file,
    result,
    reason,
    emit_file,
    event_file,
    record_file,
    replay_file,
    verify_file,
    threshold,
    threshold_calibration,
    fixed_rtsp,
    rtsp_transport,
) = sys.argv[1:13]

def load(path):
    if os.path.isfile(path):
        return json.loads(open(path, encoding="utf-8").read())
    return {}

summary = {
    "result": result,
    "reason": reason,
    "threshold": float(threshold),
    "threshold_calibration": threshold_calibration == "true",
    "fixed_rtsp": fixed_rtsp,
    "rtsp_transport": rtsp_transport,
    "savant_module": "module.c1f2d_face_observation_redis.yml",
    "event_stream": "security.events",
    "record_request_stream": "security.record_requests",
    "emit": load(emit_file),
    "event": load(event_file),
    "record_request": load(record_file),
    "replay": load(replay_file),
    "evidence": load(verify_file),
    "boundary": {
        "second_rtsp": False,
        "source_extraction": False,
        "ffmpeg_rtsp_clipping": False,
        "direct_replay_bypass": False,
        "direct_repository_event_insert": False,
        "watchlist_live_search_api": False,
        "frontend": False,
    },
}
os.makedirs(os.path.dirname(out_file), exist_ok=True)
with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(summary, fh, indent=2, default=str)
PY
}

main() {
    mkdir -p "$ARTIFACT_DIR"
    ensure_python_deps
    start_core_services
    ensure_gallery
    bootstrap_observations_if_needed
    emit_face_match_event

    local emitted
    emitted="$(extract_emit_field events_emitted | tr -d '[:space:]')"
    if [ "${emitted:-0}" -le 0 ]; then
        write_final_summary "PASS_NO_MATCH_EVENT_NOT_EMITTED" "no above-threshold Finch/Reese face match event"
        echo "RESULT=PASS_NO_MATCH_EVENT_NOT_EMITTED"
        echo "threshold=${MATCH_THRESHOLD}"
        echo "threshold_calibration=${THRESHOLD_CALIBRATION}"
        exit 0
    fi

    local source_event_id
    source_event_id="$(extract_emit_field source_event_id)"
    wait_for_event_ingested "$source_event_id"
    local event_id
    event_id="$(json_field "$EVENT_FILE" id)"
    wait_for_record_request "$source_event_id"
    wait_for_replay_job "$event_id"
    wait_for_evidence_bundle "$event_id"

    local verify_ok
    verify_ok="$(json_field "$VERIFY_FILE" ok)"
    if [ "$verify_ok" != "True" ] && [ "$verify_ok" != "true" ]; then
        fail_result "FAIL_ANNOTATIONS_NOT_GENERATED" "evidence verification failed"
    fi

    write_final_summary "PASS_FACE_MATCH_EVIDENCE_BUNDLE_READY" ""
    echo "RESULT=PASS_FACE_MATCH_EVIDENCE_BUNDLE_READY"
    echo "source_event_id=${source_event_id}"
    echo "event_id=${event_id}"
    echo "source_observation_id=$(extract_emit_field source_observation_id)"
    echo "matched_external_person_id=$(extract_emit_field matched_external_person_id)"
    echo "similarity=$(extract_emit_field similarity)"
    echo "threshold=${MATCH_THRESHOLD}"
    echo "threshold_calibration=${THRESHOLD_CALIBRATION}"
    echo "bundle_dir=$(json_field "$VERIFY_FILE" bundle_dir)"
    echo "raw_clip_path=$(json_field "$VERIFY_FILE" raw_clip_path)"
    echo "raw_clip_size=$(json_field "$VERIFY_FILE" raw_clip_size)"
    echo "annotations_jsonl=$(json_field "$VERIFY_FILE" annotations_jsonl)"
    echo "summary_json=$(json_field "$VERIFY_FILE" summary_json)"
    echo "annotation_lines=$(json_field "$VERIFY_FILE" annotation_lines)"
    echo "face_objects=$(json_field "$VERIFY_FILE" face_objects)"
    echo "matched_objects=$(json_field "$VERIFY_FILE" matched_objects)"
    echo "embedding_leaked=NO"
    echo "image_bytes_leaked=NO"
    echo "annotated_clip_generated=NO"
    echo "second_rtsp=NO"
    echo "source_extraction=NO"
    echo "ffmpeg_rtsp_clipping=NO"
    echo "direct_replay_bypass=NO"
    echo "direct_repository_event_insert=NO"
}

main "$@"
