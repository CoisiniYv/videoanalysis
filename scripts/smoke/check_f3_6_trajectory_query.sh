#!/usr/bin/env bash
# F3.6 registered person trajectory query smoke — deterministic seed + cleanup
#
# NOT read-only: this smoke seeds deterministic test data and cleans it up.
#
# Seeds:
#   1. A test person (with external_person_id)
#   2. A gallery embedding
#   3. Three face_observations (different cameras/timestamps)
#   4. Three match_results with search_mode='registered_person_history'
#   5. One match_results with search_mode='gallery_match' (interference)
#
# Verifies:
#   - --person-id returns 3 registered_person_history rows
#   - --external-person-id returns 3 registered_person_history rows
#   - gallery_match is excluded
#   - timestamp DESC ordering
#   - camera filter works
#   - min_similarity filter works
#   - --json output parses
#
# Cleanup: trap-based, removes all test:f3_6:smoke: prefixed data.
# Verifies zero leftovers.

set -euo pipefail

DB_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
FACE_WORKER_DIR="$(cd "$(dirname "$0")/../../services/face-worker" && pwd)"
PREFIX="test:f3_6:smoke:"
PERSON_ID_FILE="/tmp/f3_6_smoke_person_id.txt"
EXT_ID="${PREFIX}ext001"

cleanup() {
    echo ""
    echo "--- Cleanup ---"
    python3 - "$DB_URL" "$PREFIX" << 'PYEOF'
import os, sys

DB_URL = sys.argv[1]
PREFIX = sys.argv[2]

import psycopg

conn = psycopg.connect(DB_URL)
with conn.cursor() as cur:
    # match_results: check both source_observation_id columns
    cur.execute(
        "DELETE FROM match_results "
        "WHERE matched_source_observation_id LIKE %s "
        "OR query_source_observation_id LIKE %s",
        (PREFIX + "%", PREFIX + "%"),
    )
    mr_deleted = cur.rowcount
    cur.execute(
        "DELETE FROM person_gallery_embeddings WHERE person_id IN "
        "(SELECT id FROM persons WHERE name LIKE %s)",
        (PREFIX + "%",),
    )
    pge_deleted = cur.rowcount
    cur.execute("DELETE FROM persons WHERE name LIKE %s", (PREFIX + "%",))
    p_deleted = cur.rowcount
    cur.execute(
        "DELETE FROM face_observations WHERE source_observation_id LIKE %s",
        (PREFIX + "%",),
    )
    fo_deleted = cur.rowcount
    conn.commit()
print(f"Cleanup: match_results={mr_deleted}, gallery={pge_deleted}, persons={p_deleted}, observations={fo_deleted}")

# Verify zero leftovers
with conn.cursor() as cur:
    checks = [
        ("match_results", """
            SELECT COUNT(*) FROM match_results
            WHERE search_request_id::text LIKE %s
               OR matched_source_observation_id LIKE %s"""),
        ("persons", "SELECT COUNT(*) FROM persons WHERE name LIKE %s OR external_person_id LIKE %s"),
        ("gallery", """
            SELECT COUNT(*) FROM person_gallery_embeddings pge
            JOIN persons p ON p.id = pge.person_id
            WHERE p.name LIKE %s"""),
        ("observations", "SELECT COUNT(*) FROM face_observations WHERE source_observation_id LIKE %s"),
    ]
    all_zero = True
    for label, sql in checks:
        cur.execute(sql, (PREFIX + "%",) * (sql.count("%s")))
        count = cur.fetchone()[0]
        if count > 0:
            print(f"  LEFTOVER: {label} = {count}")
            all_zero = False
    if all_zero:
        print("  All leftovers: 0")

conn.close()
PYEOF
    rm -f "$PERSON_ID_FILE"
}

trap cleanup EXIT

echo "=== F3.6 Trajectory Query Smoke ==="
echo "DB: ${DB_URL}"
echo "Prefix: ${PREFIX}"
echo "NOT read-only: seeds and cleans up test data"
echo ""

# ── 1. Seed test data ─────────────────────────────────────────────────────

python3 - "$DB_URL" "$PREFIX" "$EXT_ID" "$PERSON_ID_FILE" << 'PYEOF'
import os, sys, math, json

DB_URL = sys.argv[1]
PREFIX = sys.argv[2]
EXT_ID = sys.argv[3]
PERSON_ID_FILE = sys.argv[4]

import psycopg
from pgvector.psycopg import register_vector, Vector

conn = psycopg.connect(DB_URL)
register_vector(conn)

DIM = 512
val = 1.0 / math.sqrt(DIM)
emb = [val] * DIM
norm = math.sqrt(sum(x * x for x in emb))

# 1. Create person with external_person_id
with conn.cursor() as cur:
    cur.execute(
        "INSERT INTO persons (name, external_person_id, created_by) "
        "VALUES (%s, %s, 'smoke') RETURNING id",
        (PREFIX + "person", EXT_ID),
    )
    person_id = cur.fetchone()[0]
    conn.commit()
print(f"Seeded person_id={person_id}, external_person_id={EXT_ID}")

# 2. Create gallery embedding
with conn.cursor() as cur:
    cur.execute(
        """
        INSERT INTO person_gallery_embeddings (
            person_id, embedding, embedding_dim, embedding_norm,
            source_type, is_active
        ) VALUES (%s, %s, 512, %s, 'smoke', true)
        RETURNING id
        """,
        (person_id, Vector(emb), norm),
    )
    gallery_id = cur.fetchone()[0]
    conn.commit()
print(f"Seeded gallery_embedding_id={gallery_id}")

# 3. Create three face_observations (different cameras, ascending timestamps)
obs_data = [
    ("obs1", "cam-lobby",   "t1", 1717000100000),
    ("obs2", "cam-parking", "t2", 1717000200000),
    ("obs3", "cam-lobby",   "t3", 1717000300000),
]
obs_ids = []
for suffix, cam, tid, ts in obs_data:
    sid = PREFIX + suffix
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO face_observations (
                source_observation_id, camera_id, source_id, track_id,
                timestamp_ms, face_bbox, landmarks,
                face_confidence, quality, embedding_dim,
                embedding, embedding_norm, reid_throttle_key
            ) VALUES (
                %s, %s, 'src1', %s,
                %s,
                '[320,240,60,60]'::jsonb,
                '[100,200,150,200,125,230,110,240,140,240]'::jsonb,
                0.90, 0.88, 512,
                %s, 1.0, %s
            )
            RETURNING id
            """,
            (sid, cam, tid, ts, Vector(emb), tid),
        )
        obs_id = cur.fetchone()[0]
        conn.commit()
    obs_ids.append((obs_id, sid, cam, tid, ts))
    print(f"Seeded observation {sid} -> uuid={obs_id}")

# 4. Create match_results (registered_person_history) — 3 rows
import uuid as _uuid
from datetime import datetime, timedelta, timezone

similarities = [0.92, 0.85, 0.78]
for i, ((obs_id, sid, cam, tid, ts), sim) in enumerate(zip(obs_ids, similarities)):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO match_results (
                search_request_id, search_mode,
                query_person_id, query_gallery_embedding_id,
                matched_observation_id, matched_source_observation_id,
                matched_camera_id, matched_source_id, matched_track_id,
                matched_timestamp_ms, rank, similarity, expires_at
            ) VALUES (
                %s, 'registered_person_history',
                %s, %s,
                %s, %s,
                %s, 'src1', %s,
                %s, %s, %s,
                %s
            )
            RETURNING id
            """,
            (
                str(_uuid.uuid4()), person_id, gallery_id,
                obs_id, sid,
                cam, tid,
                ts, i + 1, sim,
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
        mr_id = cur.fetchone()[0]
        conn.commit()
    print(f"Seeded registered_person_history id={mr_id} for {sid} sim={sim}")

# 5. Create one gallery_match interference row (matched_observation_id=NULL)
with conn.cursor() as cur:
    cur.execute(
        """
        INSERT INTO match_results (
            search_request_id, search_mode,
            query_person_id, query_gallery_embedding_id,
            matched_observation_id,
            rank, similarity, expires_at
        ) VALUES (
            %s, 'gallery_match',
            %s, %s,
            NULL,
            1, 0.95,
            %s
        )
        RETURNING id
        """,
        (
            str(_uuid.uuid4()), person_id, gallery_id,
            datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )
    gm_id = cur.fetchone()[0]
    conn.commit()
print(f"Seeded gallery_match interference id={gm_id}")

# Write person_id for the shell script
with open(PERSON_ID_FILE, "w") as f:
    f.write(str(person_id))

print("Seed complete.")
conn.close()
PYEOF

PERSON_ID=$(cat "$PERSON_ID_FILE")

echo ""
echo "--- Running query_trajectory.py ---"
echo ""

# ── 2. Run trajectory queries ──────────────────────────────────────────────

cd "${FACE_WORKER_DIR}"

# Basic query: --person-id
echo "=== --person-id query ==="
OUTPUT_PERSON=$(python3 query_trajectory.py --person-id "${PERSON_ID}" --json)
echo "${OUTPUT_PERSON}" | python3 -m json.tool
ROW_COUNT=$(echo "${OUTPUT_PERSON}" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "${ROW_COUNT}" != "3" ]; then
    echo "FAIL: expected 3 rows from --person-id, got ${ROW_COUNT}"
    exit 1
fi
echo "  ✓ 3 registered_person_history rows returned"
echo ""

# --external-person-id query
echo "=== --external-person-id query ==="
OUTPUT_EXT=$(python3 query_trajectory.py --external-person-id "${EXT_ID}" --json)
echo "${OUTPUT_EXT}" | python3 -m json.tool
EXT_COUNT=$(echo "${OUTPUT_EXT}" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "${EXT_COUNT}" != "3" ]; then
    echo "FAIL: expected 3 rows from --external-person-id, got ${EXT_COUNT}"
    exit 1
fi
echo "  ✓ 3 registered_person_history rows via external_person_id"
echo ""

# Verify ordering: timestamps should be DESC (300000, 200000, 100000)
echo "=== Ordering check ==="
TS0=$(echo "${OUTPUT_PERSON}" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['matched_timestamp_ms'])")
TS1=$(echo "${OUTPUT_PERSON}" | python3 -c "import sys,json; print(json.load(sys.stdin)[1]['matched_timestamp_ms'])")
TS2=$(echo "${OUTPUT_PERSON}" | python3 -c "import sys,json; print(json.load(sys.stdin)[2]['matched_timestamp_ms'])")
if [ "${TS0}" -le "${TS1}" ] || [ "${TS1}" -le "${TS2}" ]; then
    echo "FAIL: timestamps not in DESC order: ${TS0}, ${TS1}, ${TS2}"
    exit 1
fi
echo "  ✓ Timestamp DESC order: ${TS0} > ${TS1} > ${TS2}"

# Verify gallery_match excluded
TOTAL=$(echo "${OUTPUT_PERSON}" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "${TOTAL}" != "3" ]; then
    echo "FAIL: expected exactly 3 rows (gallery_match excluded), got ${TOTAL}"
    exit 1
fi
echo "  ✓ gallery_match excluded (total=3)"
echo ""

# Camera filter
echo "=== Camera filter (cam-lobby) ==="
CAM_OUTPUT=$(python3 query_trajectory.py --person-id "${PERSON_ID}" --camera-id cam-lobby --json)
CAM_COUNT=$(echo "${CAM_OUTPUT}" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "${CAM_COUNT}" != "2" ]; then
    echo "FAIL: expected 2 cam-lobby rows, got ${CAM_COUNT}"
    exit 1
fi
echo "  ✓ camera filter: 2 cam-lobby rows"

# min_similarity filter
echo ""
echo "=== min_similarity filter (0.80) ==="
SIM_OUTPUT=$(python3 query_trajectory.py --person-id "${PERSON_ID}" --min-similarity 0.80 --json)
SIM_COUNT=$(echo "${SIM_OUTPUT}" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "${SIM_COUNT}" != "2" ]; then
    echo "FAIL: expected 2 rows with sim>=0.80, got ${SIM_COUNT}"
    exit 1
fi
echo "  ✓ min_similarity filter: 2 rows with sim>=0.80"

# JSON output validation
echo ""
echo "=== JSON output validation ==="
echo "${OUTPUT_PERSON}" | python3 -m json.tool > /dev/null
echo "  ✓ Valid JSON"
echo "${OUTPUT_PERSON}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for row in data:
    assert 'embedding' not in row, 'embedding leaked into JSON output'
print('  ✓ No embedding in JSON output')
"

echo ""
echo "=== F3.6 Smoke PASS ==="
