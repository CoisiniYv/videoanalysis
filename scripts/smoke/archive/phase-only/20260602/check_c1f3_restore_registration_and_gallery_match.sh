#!/usr/bin/env bash
# C1F.3 — Restore archived face registration + one gallery recognition smoke.
#
# Scope:
#   archived finch/reese images -> persons/person_gallery_embeddings
#   C1 runtime Redis face observations -> PostgreSQL face_observations
#   face_observations -> gallery top-k search
#
# This is a gallery plumbing smoke only. It does not implement watchlist_hit,
# live_search_hit, API/frontend flows, production evidence, second RTSP intake,
# or source extraction.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/infra/docker-compose.c1-official-replay-dev.yml"
FACE_WORKER_DIR="$PROJECT_ROOT/services/face-worker"
C1F2D_SMOKE="$PROJECT_ROOT/scripts/smoke/check_c1f2d_face_observation_redis_smoke.sh"

DB_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
export DATABASE_URL="$DB_URL"

REDIS_URL="${C1F3_REDIS_URL:-redis://localhost:6385/0}"
REDIS_STREAM="security.face_observations"

FINCH_IMAGE="/data/video-analytics/archive/r2_3_20260529_052937/repo_uncommitted/face/finch.jpg"
REESE_IMAGE="/data/video-analytics/archive/r2_3_20260529_052937/repo_uncommitted/face/reese.jpg"
FINCH_EXTERNAL_ID="test:archive:finch"
REESE_EXTERNAL_ID="test:archive:reese"

TOP_K="${C1F3_TOP_K:-5}"
MIN_SIMILARITY="${C1F3_MIN_SIMILARITY:-0.5}"
REGISTRATION_QUALITY_THRESHOLD="${C1F3_REGISTRATION_QUALITY_THRESHOLD:-0.65}"
C1F3_SEARCH_REQUEST_ID="c1f30000-0000-4000-8000-000000000001"

ARTIFACT_DIR="${C1F3_ARTIFACT_DIR:-/data/video-analytics/artifacts/c1f3}"
SCHEMA_SUMMARY_FILE="$ARTIFACT_DIR/schema_summary.json"
REGISTRATION_SUMMARY_FILE="$ARTIFACT_DIR/registration_summary.json"
INGEST_SUMMARY_FILE="$ARTIFACT_DIR/ingest_summary.json"
OBSERVATION_SUMMARY_FILE="$ARTIFACT_DIR/face_observation_summary.json"
MATCH_SUMMARY_FILE="$ARTIFACT_DIR/gallery_match_summary.json"
SUMMARY_FILE="$ARTIFACT_DIR/c1f3_summary.json"

log() {
    echo "[C1F.3] $(date '+%H:%M:%S') $*"
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
    log "waiting for current C1 official Redis/PostgreSQL"
    for _ in $(seq 1 30); do
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
    fail_result "FAIL_INFRA_UNHEALTHY" "c1-official Redis/PostgreSQL did not become healthy"
}

check_schema() {
    log "checking current C1 official PostgreSQL schema"
    python3 - "$DB_URL" "$SCHEMA_SUMMARY_FILE" <<'PY'
import json
import sys

import psycopg
from psycopg.rows import dict_row

db_url = sys.argv[1]
out_path = sys.argv[2]

required_tables = [
    "persons",
    "person_gallery_embeddings",
    "face_observations",
    "match_results",
]

summary = {
    "postgres_container": "c1-official-postgres",
    "database": "video_analytics",
    "applied_migrations": [
        "001_init.sql",
        "005_phase_f3_1_face_observations.sql",
        "006_phase_f3_4_gallery_schema.sql",
        "007_phase_f3_5_match_results_gallery_semantics.sql",
    ],
    "persons_table": False,
    "person_gallery_embeddings_table": False,
    "face_observations_table": False,
    "match_results_table": False,
    "vector_extension": False,
}

with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT current_database() AS db")
        summary["database"] = cur.fetchone()["db"]

        cur.execute(
            """
            SELECT extname FROM pg_extension
            WHERE extname IN ('vector', 'pg_trgm', 'pgcrypto')
            """
        )
        extensions = {row["extname"] for row in cur.fetchall()}
        summary["vector_extension"] = "vector" in extensions
        summary["extensions"] = sorted(extensions)

        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
            """,
            (required_tables,),
        )
        tables = {row["table_name"] for row in cur.fetchall()}

        summary["persons_table"] = "persons" in tables
        summary["person_gallery_embeddings_table"] = (
            "person_gallery_embeddings" in tables
        )
        summary["face_observations_table"] = "face_observations" in tables
        summary["match_results_table"] = "match_results" in tables

        cur.execute(
            """
            SELECT c.relname AS table_name,
                   a.attname AS column_name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname IN ('face_observations', 'person_gallery_embeddings')
              AND a.attname = 'embedding'
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY c.relname
            """
        )
        summary["embedding_columns"] = [dict(row) for row in cur.fetchall()]

missing = [
    name
    for name in required_tables
    if not summary[f"{name}_table"] if name != "person_gallery_embeddings"
]
if not summary["person_gallery_embeddings_table"]:
    missing.append("person_gallery_embeddings")

summary["ok"] = summary["vector_extension"] and not missing
summary["missing_tables"] = missing

with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(summary, fh, indent=2)

print(json.dumps(summary, indent=2))
if not summary["ok"]:
    sys.exit(1)
PY
}

registration_valid() {
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
    "active_gallery_count": 0,
    "valid": False,
}

with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, name, external_person_id
            FROM persons
            WHERE external_person_id = %s
              AND is_active = true
            """,
            (external_id,),
        )
        person = cur.fetchone()
        if person is None:
            with open(out_file, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2)
            sys.exit(2)

        result["person_id"] = int(person["id"])
        cur.execute(
            """
            SELECT id, embedding_dim, embedding_norm, embedding_model,
                   source_image_path, is_primary
            FROM person_gallery_embeddings
            WHERE person_id = %s
              AND is_active = true
            ORDER BY is_primary DESC, id DESC
            """,
            (person["id"],),
        )
        galleries = cur.fetchall()
        result["active_gallery_count"] = len(galleries)

        for row in galleries:
            norm = float(row["embedding_norm"])
            if (
                row["source_image_path"] == image_path
                and row["embedding_model"] == "adaface"
                and int(row["embedding_dim"]) == 512
                and 0.90 <= norm <= 1.10
            ):
                result.update(
                    {
                        "gallery_embedding_id": int(row["id"]),
                        "embedding_dim": int(row["embedding_dim"]),
                        "embedding_norm": norm,
                        "embedding_model": row["embedding_model"],
                        "valid": True,
                    }
                )
                break

with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(result, fh, indent=2)

sys.exit(0 if result["valid"] else 2)
PY
}

register_image_if_needed() {
    local image="$1"
    local external_id="$2"
    local name="$3"
    local person_out="$ARTIFACT_DIR/${name,,}_registration.json"

    if registration_valid "$image" "$external_id" "$person_out"; then
        log "registration already valid: ${external_id}"
        return 0
    fi

    log "registering archived face image: ${external_id}"
    if ! (
        cd "$FACE_WORKER_DIR"
        python3 register_face_image.py \
            --image "$image" \
            --external-person-id "$external_id" \
            --name "$name" \
            --is-primary \
            --allow-multiple-faces \
            --quality-threshold "$REGISTRATION_QUALITY_THRESHOLD" \
            --created-by "c1f3_restore_registration_smoke" \
            --output-json
    ) >"$person_out.tmp" 2>&1; then
        cat "$person_out.tmp"
        if grep -q "REAL_IMAGE_EMBEDDING_UNAVAILABLE" "$person_out.tmp"; then
            fail_result "BLOCKED_NO_IMAGE_REGISTRATION_TOOL" "offline external image registration is not available"
        fi
        fail_result "FAIL_REGISTRATION" "${external_id}"
    fi
    mv "$person_out.tmp" "$person_out"
    cat "$person_out"

    if ! registration_valid "$image" "$external_id" "$person_out"; then
        cat "$person_out"
        fail_result "FAIL_REGISTRATION_VALIDATION" "${external_id}"
    fi
}

summarize_registration() {
    log "summarizing restored registrations"
    python3 - "$DB_URL" "$FINCH_EXTERNAL_ID" "$REESE_EXTERNAL_ID" "$REGISTRATION_SUMMARY_FILE" <<'PY'
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
            """
            SELECT COUNT(*) AS count
            FROM person_gallery_embeddings
            WHERE is_active = true
            """
        )
        summary["active_gallery_embeddings_total"] = int(cur.fetchone()["count"])

        cur.execute(
            """
            SELECT p.external_person_id,
                   p.id AS person_id,
                   p.name,
                   pge.id AS gallery_embedding_id,
                   pge.embedding_dim,
                   pge.embedding_norm,
                   pge.embedding_model,
                   pge.source_image_path,
                   pge.is_primary,
                   pge.is_active,
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
                "name": row["name"],
                "gallery_embedding_id": int(row["gallery_embedding_id"]),
                "embedding_dim": int(row["embedding_dim"]),
                "embedding_norm": float(row["embedding_norm"]),
                "embedding_model": row["embedding_model"],
                "source_image_path": row["source_image_path"],
                "is_primary": bool(row["is_primary"]),
                "no_image_bytes_in_payload": not any(
                    marker in payload_text.lower()
                    for marker in (
                        "image_bytes",
                        "crop_bytes",
                        "raw_bytes",
                        "data:image/",
                    )
                ),
            }

with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(summary, fh, indent=2)

print(json.dumps(summary, indent=2))

missing = [ext for ext in (finch_ext, reese_ext) if ext not in summary["people"]]
if missing:
    print("missing registrations: " + ", ".join(missing))
    sys.exit(1)

for ext, row in summary["people"].items():
    if row["embedding_model"] != "adaface":
        print(f"{ext}: embedding_model is not adaface")
        sys.exit(1)
    if row["embedding_dim"] != 512:
        print(f"{ext}: embedding_dim is not 512")
        sys.exit(1)
    if not (0.90 <= row["embedding_norm"] <= 1.10):
        print(f"{ext}: embedding_norm out of range")
        sys.exit(1)
    if not row["no_image_bytes_in_payload"]:
        print(f"{ext}: payload contains forbidden image bytes marker")
        sys.exit(1)
PY
}

count_valid_db_observations() {
    python3 - "$DB_URL" <<'PY'
import psycopg

with psycopg.connect(__import__("sys").argv[1]) as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM face_observations
            WHERE embedding_dim = 512
              AND embedding_model = 'adaface'
              AND detector_model = 'yolov8_face'
              AND embedding_norm BETWEEN 0.90 AND 1.10
              AND face_bbox IS NOT NULL
              AND landmarks IS NOT NULL
            """
        )
        print(cur.fetchone()[0])
PY
}

count_valid_redis_observations() {
    python3 - "$REDIS_URL" "$REDIS_STREAM" <<'PY'
import json
import sys

import redis

redis_url, stream = sys.argv[1:3]
r = redis.Redis.from_url(redis_url, decode_responses=True)
count = 0
for _msg_id, fields in r.xrevrange(stream, "+", "-", count=500):
    raw = fields.get("data")
    if not raw:
        continue
    try:
        obs = json.loads(raw)
    except Exception:
        continue
    embedding = obs.get("embedding")
    if (
        isinstance(embedding, list)
        and len(embedding) == 512
        and obs.get("embedding_dim") == 512
        and 0.90 <= float(obs.get("embedding_norm", 0.0)) <= 1.10
        and obs.get("embedding_model") == "adaface"
        and obs.get("detector_model") == "yolov8_face"
    ):
        count += 1
print(count)
PY
}

generate_redis_observations_if_needed() {
    local valid_db_count
    valid_db_count="$(count_valid_db_observations)"
    if [ "$valid_db_count" -gt 0 ]; then
        log "PostgreSQL already has valid face_observations: ${valid_db_count}"
        return 0
    fi

    local valid_redis_count
    valid_redis_count="$(count_valid_redis_observations || echo 0)"
    if [ "$valid_redis_count" -gt 0 ]; then
        log "Redis already has valid ${REDIS_STREAM} messages: ${valid_redis_count}"
        return 0
    fi

    if [ "${C1F3_RUN_C1F2D_IF_NEEDED:-1}" != "1" ]; then
        fail_result "FAIL_NO_OBSERVATIONS" "no DB observations and C1F2D generation disabled"
    fi

    require_file "$C1F2D_SMOKE" "C1F.2d smoke"
    log "generating Redis face observations via C1F.2d smoke"
    if ! C1F2D_ARTIFACT_DIR="$ARTIFACT_DIR/c1f2d" \
        C1F2D_SMOKE_DURATION="${C1F3_C1F2D_SMOKE_DURATION:-45}" \
        C1F2D_DRAIN_WAIT="${C1F3_C1F2D_DRAIN_WAIT:-10}" \
        "$C1F2D_SMOKE"; then
        log "C1F.2d generation failed; C1F.3 will still check Redis for reusable observations"
    fi
}

ingest_redis_observations() {
    log "ingesting Redis face observations through face-worker repository path"
    python3 - "$PROJECT_ROOT" "$FACE_WORKER_DIR" "$DB_URL" "$REDIS_URL" "$REDIS_STREAM" "$INGEST_SUMMARY_FILE" <<'PY'
import json
import sys
from pathlib import Path

import psycopg
import redis

project_root, face_worker_dir, db_url, redis_url, stream, out_file = sys.argv[1:7]
sys.path.insert(0, face_worker_dir)
sys.path.insert(0, project_root)

from app.repository import FaceObservationRepository
from app.worker import _parse_observation, _validate_embedding


def has_forbidden_image_bytes(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            if "bytes" in key_l or "base64" in key_l or key_l in {
                "image_b64",
                "crop_b64",
                "raw_image",
            }:
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


summary = {
    "redis_stream": stream,
    "redis_messages_seen": 0,
    "redis_valid_observations": 0,
    "inserted": 0,
    "duplicates": 0,
    "skipped": 0,
    "failed": 0,
    "forbidden_image_bytes": 0,
    "sample_source_observation_id": "",
    "sample_embedding_dim": 0,
    "sample_embedding_norm": 0.0,
    "face_worker_ingest_used": "compatibility_repository_path",
}

r = redis.Redis.from_url(redis_url, decode_responses=False)
entries = r.xrevrange(stream, b"+", b"-", count=500)
summary["redis_messages_seen"] = len(entries)

with psycopg.connect(db_url, autocommit=True) as conn:
    repo = FaceObservationRepository(conn)
    for msg_id, fields in entries:
        msg_id_s = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        obs = _parse_observation(fields)
        if obs is None:
            summary["skipped"] += 1
            continue
        if has_forbidden_image_bytes(obs):
            summary["forbidden_image_bytes"] += 1
            summary["skipped"] += 1
            continue
        embed_err = _validate_embedding(obs, msg_id_s)
        if embed_err:
            summary["skipped"] += 1
            continue
        try:
            row_id = repo.insert_observation(obs)
        except Exception as exc:
            summary["failed"] += 1
            summary.setdefault("errors", []).append(str(exc))
            continue
        summary["redis_valid_observations"] += 1
        if row_id is None:
            summary["duplicates"] += 1
        else:
            summary["inserted"] += 1
        if not summary["sample_source_observation_id"]:
            summary["sample_source_observation_id"] = obs.get(
                "source_observation_id", ""
            )
            summary["sample_embedding_dim"] = int(obs.get("embedding_dim", 0))
            summary["sample_embedding_norm"] = float(
                obs.get("embedding_norm", 0.0)
            )

Path(out_file).write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary, indent=2))

if summary["forbidden_image_bytes"] > 0:
    sys.exit(2)
if summary["failed"] > 0:
    sys.exit(1)
PY
}

summarize_face_observations() {
    log "validating PostgreSQL face_observations"
    python3 - "$DB_URL" "$OBSERVATION_SUMMARY_FILE" <<'PY'
import json
import sys

import psycopg
from psycopg.rows import dict_row

db_url, out_file = sys.argv[1:3]

summary = {
    "face_observations_total": 0,
    "valid_observations_total": 0,
    "source_observation_id_unique": True,
    "payload_without_embedding": True,
    "payload_without_image_bytes": True,
    "sample_observation_id": "",
    "sample_source_observation_id": "",
    "sample_embedding_dim": 0,
    "sample_embedding_norm": 0.0,
    "sample_detector_model": "",
    "sample_embedding_model": "",
}

with psycopg.connect(db_url) as conn:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT COUNT(*) AS count FROM face_observations")
        summary["face_observations_total"] = int(cur.fetchone()["count"])

        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM face_observations
            WHERE embedding_dim = 512
              AND embedding_model = 'adaface'
              AND detector_model = 'yolov8_face'
              AND embedding_norm BETWEEN 0.90 AND 1.10
              AND camera_id IS NOT NULL
              AND source_id IS NOT NULL
              AND track_id IS NOT NULL
              AND timestamp_ms IS NOT NULL
              AND face_bbox IS NOT NULL
              AND landmarks IS NOT NULL
            """
        )
        summary["valid_observations_total"] = int(cur.fetchone()["count"])

        cur.execute(
            """
            SELECT COUNT(*) = COUNT(DISTINCT source_observation_id) AS is_unique
            FROM face_observations
            """
        )
        summary["source_observation_id_unique"] = bool(cur.fetchone()["is_unique"])

        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM face_observations
            WHERE payload ? 'embedding'
               OR payload::text ILIKE '%image_bytes%'
               OR payload::text ILIKE '%crop_bytes%'
               OR payload::text ILIKE '%raw_bytes%'
               OR payload::text ILIKE '%data:image/%'
            """
        )
        bad_payload_count = int(cur.fetchone()["count"])
        summary["payload_without_embedding"] = bad_payload_count == 0
        summary["payload_without_image_bytes"] = bad_payload_count == 0

        cur.execute(
            """
            SELECT id, source_observation_id, embedding_dim, embedding_norm,
                   detector_model, embedding_model
            FROM face_observations
            WHERE embedding_dim = 512
              AND embedding_model = 'adaface'
              AND detector_model = 'yolov8_face'
              AND embedding_norm BETWEEN 0.90 AND 1.10
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row:
            summary.update(
                {
                    "sample_observation_id": str(row["id"]),
                    "sample_source_observation_id": row["source_observation_id"],
                    "sample_embedding_dim": int(row["embedding_dim"]),
                    "sample_embedding_norm": float(row["embedding_norm"]),
                    "sample_detector_model": row["detector_model"],
                    "sample_embedding_model": row["embedding_model"],
                }
            )

with open(out_file, "w", encoding="utf-8") as fh:
    json.dump(summary, fh, indent=2)

print(json.dumps(summary, indent=2))

if summary["valid_observations_total"] < 1:
    sys.exit(2)
if not summary["source_observation_id_unique"]:
    sys.exit(1)
if not summary["payload_without_embedding"] or not summary["payload_without_image_bytes"]:
    sys.exit(1)
PY
}

run_gallery_match() {
    log "running gallery top-k match smoke"
    python3 - "$PROJECT_ROOT" "$FACE_WORKER_DIR" "$DB_URL" \
        "$C1F3_SEARCH_REQUEST_ID" "$TOP_K" "$MIN_SIMILARITY" \
        "$FINCH_EXTERNAL_ID" "$REESE_EXTERNAL_ID" "$MATCH_SUMMARY_FILE" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

project_root, face_worker_dir, db_url = sys.argv[1:4]
search_request_id, top_k_s, threshold_s = sys.argv[4:7]
finch_ext, reese_ext, out_file = sys.argv[7:10]
sys.path.insert(0, face_worker_dir)
sys.path.insert(0, project_root)

from app.match_repository import MatchResultRepository
from app.vector_store import FaceVectorStore

top_k = int(top_k_s)
threshold = float(threshold_s)
target_external_ids = {finch_ext, reese_ext}

summary = {
    "search_executed": False,
    "search_request_id": search_request_id,
    "query_observation_id": "",
    "query_source_observation_id": "",
    "top_k": top_k,
    "threshold": threshold,
    "result_type": "FAIL_SEARCH_ERROR",
    "matched_external_person_id": "",
    "matched_person_id": None,
    "matched_gallery_embedding_id": None,
    "similarity": None,
    "rank": None,
    "top_candidate_external_person_id": "",
    "top_candidate_person_id": None,
    "top_candidate_similarity": None,
    "reason": "",
    "match_results_written": 0,
}

with psycopg.connect(db_url, autocommit=True) as conn:
    register_vector(conn)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM person_gallery_embeddings pge
            JOIN persons p ON p.id = pge.person_id
            WHERE p.is_active = true
              AND pge.is_active = true
              AND pge.embedding_dim = 512
              AND pge.embedding_model = 'adaface'
              AND pge.embedding_norm BETWEEN 0.90 AND 1.10
            """
        )
        gallery_count = int(cur.fetchone()["count"])
        if gallery_count < 1:
            summary["result_type"] = "FAIL_NO_GALLERY"
            summary["reason"] = "no active valid gallery embeddings"
            Path(out_file).write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(json.dumps(summary, indent=2))
            sys.exit(0)

        cur.execute(
            """
            SELECT id, source_observation_id, camera_id, source_id, track_id,
                   timestamp_ms, face_confidence, quality, embedding
            FROM face_observations
            WHERE embedding_dim = 512
              AND embedding_model = 'adaface'
              AND detector_model = 'yolov8_face'
              AND embedding_norm BETWEEN 0.90 AND 1.10
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        obs = cur.fetchone()
        if obs is None:
            summary["result_type"] = "FAIL_NO_OBSERVATIONS"
            summary["reason"] = "no valid PostgreSQL face_observations"
            Path(out_file).write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(json.dumps(summary, indent=2))
            sys.exit(0)

        cur.execute(
            "DELETE FROM match_results WHERE search_request_id = %s",
            (search_request_id,),
        )

    embedding = obs["embedding"]
    if hasattr(embedding, "tolist"):
        embedding_list = [float(x) for x in embedding.tolist()]
    else:
        embedding_list = [float(x) for x in embedding]

    summary["query_observation_id"] = str(obs["id"])
    summary["query_source_observation_id"] = obs["source_observation_id"]

    store = FaceVectorStore(conn)
    all_results = store.search_gallery(
        embedding_list,
        top_k=top_k,
        min_similarity=None,
    )
    summary["search_executed"] = True

    if not all_results:
        summary["result_type"] = "FAIL_SEARCH_ERROR"
        summary["reason"] = "gallery search returned no candidates"
        Path(out_file).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
        sys.exit(0)

    top = all_results[0]
    summary["top_candidate_external_person_id"] = top.get("external_person_id") or ""
    summary["top_candidate_person_id"] = int(top["person_id"])
    summary["top_candidate_similarity"] = float(top["similarity"])

    threshold_results = [
        row for row in all_results if float(row["similarity"]) >= threshold
    ]

    repo = MatchResultRepository(conn)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    for rank_idx, result in enumerate(threshold_results, start=1):
        row_id = repo.insert_gallery_match_result(
            {
                "search_request_id": search_request_id,
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
                "payload": {"phase": "C1F.3", "smoke": "restore_registration_gallery_match"},
            }
        )
        if row_id is not None:
            summary["match_results_written"] += 1

    matched = None
    for rank_idx, result in enumerate(threshold_results, start=1):
        if result.get("external_person_id") in target_external_ids:
            matched = (rank_idx, result)
            break

    if matched is not None:
        rank_idx, result = matched
        summary.update(
            {
                "result_type": "PASS_MATCH",
                "matched_external_person_id": result.get("external_person_id") or "",
                "matched_person_id": int(result["person_id"]),
                "matched_gallery_embedding_id": int(result["id"]),
                "similarity": float(result["similarity"]),
                "rank": rank_idx,
                "reason": "registered archived person matched above threshold",
            }
        )
    else:
        summary["result_type"] = "PASS_NO_MATCH_PIPELINE_OK"
        if threshold_results:
            summary["reason"] = (
                "gallery search ran, but top threshold candidate is not Finch/Reese"
            )
        else:
            summary["reason"] = (
                "gallery search ran, but all candidates were below threshold; "
                "current RTSP person may not be Finch/Reese"
            )

Path(out_file).write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary, indent=2))
PY
}

write_combined_summary() {
    python3 - "$SUMMARY_FILE" "$SCHEMA_SUMMARY_FILE" "$REGISTRATION_SUMMARY_FILE" \
        "$INGEST_SUMMARY_FILE" "$OBSERVATION_SUMMARY_FILE" "$MATCH_SUMMARY_FILE" \
        "$FINCH_IMAGE" "$REESE_IMAGE" <<'PY'
import json
import os
import sys

summary_file, schema_file, reg_file, ingest_file, obs_file, match_file = sys.argv[1:7]
finch_image, reese_image = sys.argv[7:9]


def load(path):
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


schema = load(schema_file)
registration = load(reg_file)
ingest = load(ingest_file)
observations = load(obs_file)
match = load(match_file)

combined = {
    "result": match.get("result_type", "FAIL_SEARCH_ERROR"),
    "schema": schema,
    "registration": registration,
    "ingest": ingest,
    "face_observations": observations,
    "gallery_recognition": match,
    "boundary": {
        "watchlist_hit_implemented": "NO",
        "live_search_hit_implemented": "NO",
        "api_frontend_implemented": "NO",
        "production_evidence_generated": "NO",
        "image_bytes_in_redis_db": "NO",
        "second_rtsp": "NO",
        "source_extraction": "NO",
    },
    "archived_images": {
        "finch": {"path": finch_image, "exists": os.path.isfile(finch_image)},
        "reese": {"path": reese_image, "exists": os.path.isfile(reese_image)},
    },
}

with open(summary_file, "w", encoding="utf-8") as fh:
    json.dump(combined, fh, indent=2)

print("=== C1F.3 Summary ===")
print(f"RESULT={combined['result']}")
print(f"postgres_container={schema.get('postgres_container')}")
print(f"database={schema.get('database')}")
print(f"vector_extension={schema.get('vector_extension')}")
print(f"persons_total={registration.get('persons_total')}")
print(f"gallery_embeddings_total={registration.get('gallery_embeddings_total')}")
print(f"redis_valid_observations={ingest.get('redis_valid_observations', 0)}")
print(f"face_worker_ingest_used={ingest.get('face_worker_ingest_used', '')}")
print(f"face_observations_total={observations.get('face_observations_total')}")
print(f"new_observations_inserted={ingest.get('inserted', 0)}")
print(f"query_observation_id={match.get('query_observation_id', '')}")
print(f"top_k={match.get('top_k')}")
print(f"threshold={match.get('threshold')}")
print(f"matched_external_person_id={match.get('matched_external_person_id', '')}")
print(f"matched_person_id={match.get('matched_person_id')}")
print(f"similarity={match.get('similarity')}")
print(f"rank={match.get('rank')}")
print(f"top_candidate_external_person_id={match.get('top_candidate_external_person_id', '')}")
print(f"top_candidate_similarity={match.get('top_candidate_similarity')}")
print(f"reason={match.get('reason', '')}")
print("watchlist_hit implemented: NO")
print("live_search_hit implemented: NO")
print("API/frontend implemented: NO")
print("production evidence generated: NO")
print("image bytes in Redis/DB: NO")
print("second RTSP: NO")
print("source extraction: NO")
PY
}

main() {
    mkdir -p "$ARTIFACT_DIR"

    log "pre-flight checks"
    require_file "$COMPOSE_FILE" "C1 official compose"
    require_file "$FINCH_IMAGE" "archived Finch image"
    require_file "$REESE_IMAGE" "archived Reese image"
    require_file "$FACE_WORKER_DIR/register_face_image.py" "external image registration CLI"
    ensure_python_deps

    log "starting current C1 official Redis/PostgreSQL only"
    docker compose -f "$COMPOSE_FILE" up -d redis postgres
    wait_for_infra

    check_schema || fail_result "FAIL_SCHEMA" "current C1 official schema check failed"

    register_image_if_needed "$FINCH_IMAGE" "$FINCH_EXTERNAL_ID" "Finch"
    register_image_if_needed "$REESE_IMAGE" "$REESE_EXTERNAL_ID" "Reese"
    summarize_registration || fail_result "FAIL_REGISTRATION_VALIDATION" "registration acceptance failed"

    generate_redis_observations_if_needed
    ingest_redis_observations || fail_result "FAIL_INGEST" "Redis to PostgreSQL compatibility ingest failed"
    summarize_face_observations || fail_result "FAIL_NO_OBSERVATIONS" "no valid PostgreSQL face_observations"

    run_gallery_match
    write_combined_summary

    local result
    result="$(python3 -c "import json; print(json.load(open('$MATCH_SUMMARY_FILE')).get('result_type', 'FAIL_SEARCH_ERROR'))")"
    case "$result" in
        PASS_MATCH|PASS_NO_MATCH_PIPELINE_OK)
            exit 0
            ;;
        FAIL_NO_GALLERY|FAIL_NO_OBSERVATIONS|FAIL_SEARCH_ERROR|*)
            exit 1
            ;;
    esac
}

main "$@"
