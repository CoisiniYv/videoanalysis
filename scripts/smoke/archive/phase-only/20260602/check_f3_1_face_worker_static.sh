#!/usr/bin/env bash
# F3.1 Static Smoke — face-worker files and contract checks
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PASS=0
FAIL=0

check() {
    local desc="$1" condition="$2"
    if eval "$condition"; then
        echo "  $(printf '\033[32m') PASS$(printf '\033[0m')  $desc"
        PASS=$((PASS + 1))
    else
        echo "  $(printf '\033[31m') FAIL$(printf '\033[0m')  $desc"
        FAIL=$((FAIL + 1))
    fi
}

echo "--- F3.1 Static: DB migration ---"
MIG="$REPO_ROOT/db/migrations/005_phase_f3_1_face_observations.sql"
check "migration file exists" "[ -f '$MIG' ]"
check "CREATE TABLE face_observations" "grep -q 'CREATE TABLE.*face_observations' '$MIG'"
check "vector extension guarded" "grep -q 'CREATE EXTENSION IF NOT EXISTS vector' '$MIG'"
check "pgcrypto extension guarded" "grep -q 'CREATE EXTENSION IF NOT EXISTS pgcrypto' '$MIG'"
check "source_observation_id UNIQUE" "grep -q 'source_observation_id.*UNIQUE' '$MIG'"
check "embedding vector(512) NOT NULL" "grep -q 'vector(512) NOT NULL' '$MIG'"
check "embedding_dim CHECK = 512" "grep -q 'CHECK.*embedding_dim = 512' '$MIG'"
check "btree indexes present" "grep -q 'CREATE INDEX.*face_observations' '$MIG'"
# Exclude SQL comment lines before checking for HNSW/IVFFlat
check "no HNSW in migration" "grep -v '^[[:space:]]*--' '$MIG' | grep -qiv 'hnsw\|ivfflat' || ! grep -v '^[[:space:]]*--' '$MIG' | grep -qi 'hnsw\|ivfflat'"
check "person_bbox column exists" "grep -q 'person_bbox' '$MIG'"
check "snapshot_path column exists" "grep -q 'snapshot_path' '$MIG'"
check "crop_path column exists" "grep -q 'crop_path' '$MIG'"
check "model_version column exists" "grep -q 'model_version' '$MIG'"
check "captured_at column exists" "grep -q 'captured_at' '$MIG'"
check "face_bbox JSONB" "grep -q 'face_bbox.*JSONB' '$MIG'"
check "landmarks JSONB" "grep -q 'landmarks.*JSONB' '$MIG'"

echo ""
echo "--- F3.1 Static: face-worker files ---"
FW="$REPO_ROOT/services/face-worker"
check "main.py exists" "[ -f '$FW/main.py' ]"
check "app/__init__.py exists" "[ -f '$FW/app/__init__.py' ]"
check "app/config.py exists" "[ -f '$FW/app/config.py' ]"
check "app/redis_consumer.py exists" "[ -f '$FW/app/redis_consumer.py' ]"
check "app/repository.py exists" "[ -f '$FW/app/repository.py' ]"
check "app/worker.py exists" "[ -f '$FW/app/worker.py' ]"
check "requirements.txt exists" "[ -f '$FW/requirements.txt' ]"
check "Dockerfile exists" "[ -f '$FW/Dockerfile' ]"

echo ""
echo "--- F3.1 Static: requirements ---"
check "redis in requirements" "grep -q 'redis' '$FW/requirements.txt'"
check "psycopg in requirements" "grep -q 'psycopg' '$FW/requirements.txt'"
check "pgvector in requirements" "grep -q 'pgvector' '$FW/requirements.txt'"

echo ""
echo "--- F3.1 Static: config has consumer_start_id ---"
check "consumer_start_id in Config dataclass" "grep -q 'consumer_start_id' '$FW/app/config.py'"
check "FACE_OBSERVATION_CONSUMER_START_ID env var" "grep -q 'FACE_OBSERVATION_CONSUMER_START_ID' '$FW/app/config.py'"
check "default start_id is 0" "grep -q 'FACE_OBSERVATION_CONSUMER_START_ID.*\"0\"' '$FW/app/config.py'"

echo ""
echo "--- F3.1 Static: redis_consumer accepts start_id ---"
check "RedisStreamConsumer init has start_id param" "grep -q 'start_id' '$FW/app/redis_consumer.py'"
check "ensure_group uses self._start_id" "grep -q 'self._start_id' '$FW/app/redis_consumer.py'"

echo ""
echo "--- F3.1 Static: worker validates embedding ---"
check "_validate_embedding function exists" "grep -q '_validate_embedding' '$FW/app/worker.py'"
check "embedding length check" "grep -q 'len(embedding).*_EMBEDDING_DIM' '$FW/app/worker.py'"
check "embedding_dim check" "grep -q 'embedding_dim.*_EMBEDDING_DIM\|_EMBEDDING_DIM.*embedding_dim' '$FW/app/worker.py'"
check "embedding_norm range check" "grep -q '_MIN_EMBEDDING_NORM\|_MAX_EMBEDDING_NORM' '$FW/app/worker.py'"
check "skipped counter" "grep -q 'skipped' '$FW/app/worker.py'"
check "invalid embedding not acked" "grep -q 'left pending' '$FW/app/worker.py'"

echo ""
echo "--- F3.1 Static: compose has face-worker ---"
COMP="$REPO_ROOT/infra/docker-compose.c1-official-adapter.yml"
check "face-worker service exists" "grep -q 'face-worker:' '$COMP'"
check "FACE_OBSERVATION_STREAM env" "grep -q 'FACE_OBSERVATION_STREAM' '$COMP'"
check "FACE_OBSERVATION_CONSUMER_START_ID env" "grep -q 'FACE_OBSERVATION_CONSUMER_START_ID' '$COMP'"
check "CONSUMER_GROUP in face-worker" "grep -A25 'face-worker:' '$COMP' | grep -q 'CONSUMER_GROUP'"

echo ""
echo "--- F3.1 Static: forbidden elements ---"
check "no HNSWLIB in face-worker" "! grep -rqi 'hnswlib' '$FW/' 2>/dev/null"
check "no Qdrant in face-worker" "! grep -rqi 'qdrant' '$FW/' 2>/dev/null"
check "no watchlist_hit in face-worker" "! grep -rqi 'watchlist_hit' '$FW/' 2>/dev/null"
check "no live_search in face-worker" "! grep -rqi 'live_search_hit' '$FW/' 2>/dev/null"
check "no security.events in face-worker" "! grep -rq 'security.events' '$FW/' 2>/dev/null"
check "no similarity search in face-worker" "! grep -rqi '<=>\|cosine_distance\|l2_distance\|inner_product' '$FW/' 2>/dev/null"

echo ""
echo "--- F3.1 Static: GPU pipeline untouched ---"
MOD="$REPO_ROOT/modules/savant_security/module.yml"
check "module.yml exists" "[ -f '$MOD' ]"
check "no face-worker reference in module.yml" "! grep -qi 'face.worker' '$MOD' 2>/dev/null"
# Exclude YAML comment lines — "No pgvector/watchlist" comment is allowed
check "no pgvector in module.yml (non-comment)" "grep -v '^[[:space:]]*#' '$MOD' | grep -qiv 'pgvector' || ! grep -v '^[[:space:]]*#' '$MOD' | grep -qi 'pgvector'"

echo ""
echo "--- F3.1 Static: test file ---"
check "test_face_worker.py exists" "[ -f '$REPO_ROOT/harness/tests/test_face_worker.py' ]"

echo ""
echo "--- F3.1 Static smoke summary ---"
echo ""
echo "  Static checks: $PASS passed, $FAIL failed"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "  F3.1 static smoke: FAIL"
    exit 1
else
    echo ""
    echo "  F3.1 static smoke: PASS"
    exit 0
fi
