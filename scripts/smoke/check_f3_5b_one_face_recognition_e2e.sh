#!/usr/bin/env bash
# F3.5b One Face Recognition End-to-End Smoke
#
# Proves one registered-person recognition loop works:
#   face_observation -> enroll_gallery -> match_gallery -> match_results -> top1 recognized person
#
# Uses a REAL existing face_observation from PostgreSQL.
# Does NOT create fake embeddings or synthetic data.
#
# Cleanup: trap-based. Removes test:f3_5b person, gallery, match_results.
# Does NOT delete the original face_observation.

set -euo pipefail

DB_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
export DATABASE_URL="$DB_URL"
FACE_WORKER_DIR="$(cd "$(dirname "$0")/../../services/face-worker" && pwd)"
EXT_ID="test:f3_5b:person"
OBS_UUID_FILE="/tmp/f3_5b_obs_uuid.txt"
OBS_SOURCE_FILE="/tmp/f3_5b_obs_source.txt"

# Deterministic search_request_id — never random
F3_5B_SEARCH_REQUEST_ID="f35b0000-0000-4000-8000-000000000001"

cleanup() {
    echo ""
    echo "--- Cleanup ---"
    python3 - "$DB_URL" "$EXT_ID" "$F3_5B_SEARCH_REQUEST_ID" << 'PYEOF'
import sys

DB_URL = sys.argv[1]
EXT_ID = sys.argv[2]
SEARCH_REQ_ID = sys.argv[3]

import psycopg

conn = psycopg.connect(DB_URL)

with conn.cursor() as cur:
    # match_results by deterministic search_request_id
    cur.execute(
        "DELETE FROM match_results WHERE search_request_id = %s",
        (SEARCH_REQ_ID,),
    )
    mr = cur.rowcount

    # person_gallery_embeddings for test person
    cur.execute(
        "DELETE FROM person_gallery_embeddings WHERE person_id IN "
        "(SELECT id FROM persons WHERE external_person_id = %s)",
        (EXT_ID,),
    )
    pge = cur.rowcount

    # person
    cur.execute(
        "DELETE FROM persons WHERE external_person_id = %s",
        (EXT_ID,),
    )
    p = cur.rowcount

    conn.commit()
print(f"Cleanup: match_results={mr}, gallery={pge}, persons={p}")

# Verify cleanup
with conn.cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM persons WHERE external_person_id = %s", (EXT_ID,))
    assert cur.fetchone()[0] == 0, "person not cleaned up"
    cur.execute(
        "SELECT COUNT(*) FROM person_gallery_embeddings pge "
        "JOIN persons p ON p.id = pge.person_id WHERE p.external_person_id = %s",
        (EXT_ID,),
    )
    assert cur.fetchone()[0] == 0, "gallery not cleaned up"
    cur.execute(
        "SELECT COUNT(*) FROM match_results WHERE search_request_id = %s",
        (SEARCH_REQ_ID,),
    )
    assert cur.fetchone()[0] == 0, "match_results not cleaned up"
print("Cleanup verified: all test:f3_5b data removed")
conn.close()
PYEOF
    rm -f "$OBS_UUID_FILE" "$OBS_SOURCE_FILE"
}

trap cleanup EXIT

echo "=== F3.5b One Face Recognition End-to-End Smoke ==="
echo "DB: ${DB_URL}"
echo "search_request_id: ${F3_5B_SEARCH_REQUEST_ID}"
echo ""

# ── 1. Select one existing face_observation ────────────────────────────────

echo "--- Step 1: Select existing face_observation ---"

python3 - "$DB_URL" "$OBS_UUID_FILE" "$OBS_SOURCE_FILE" << 'PYEOF'
import sys

DB_URL = sys.argv[1]
OBS_UUID_FILE = sys.argv[2]
OBS_SOURCE_FILE = sys.argv[3]

import psycopg
from psycopg.rows import dict_row

conn = psycopg.connect(DB_URL)
with conn.cursor(row_factory=dict_row) as cur:
    cur.execute("""
        SELECT id, source_observation_id, camera_id, source_id, track_id,
               timestamp_ms, face_confidence, quality
        FROM face_observations
        WHERE embedding IS NOT NULL
          AND vector_dims(embedding) = 512
          AND source_observation_id IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1
    """)
    obs = cur.fetchone()

if obs is None:
    print("FAIL: no usable face_observations found")
    sys.exit(1)

print(f"Selected observation:")
print(f"  UUID (id):               {obs['id']}")
print(f"  source_observation_id:   {obs['source_observation_id']}")
print(f"  camera_id:               {obs['camera_id']}")
print(f"  source_id:               {obs['source_id']}")
print(f"  track_id:                {obs['track_id']}")
print(f"  timestamp_ms:            {obs['timestamp_ms']}")
print(f"  face_confidence:         {obs['face_confidence']}")
print(f"  quality:                 {obs['quality']}")

with open(OBS_UUID_FILE, "w") as f:
    f.write(str(obs["id"]))

with open(OBS_SOURCE_FILE, "w") as f:
    f.write(obs["source_observation_id"])

conn.close()
PYEOF

SELECTED_OBS_UUID=$(cat "$OBS_UUID_FILE")
SELECTED_SOURCE_OBS_ID=$(cat "$OBS_SOURCE_FILE")
echo ""
echo "Selected observation UUID:        ${SELECTED_OBS_UUID}"
echo "Selected source_observation_id:   ${SELECTED_SOURCE_OBS_ID}"

# ── 2. Enroll as test person ───────────────────────────────────────────────

echo ""
echo "--- Step 2: Enroll as test person ---"

cd "${FACE_WORKER_DIR}"

ENROLL_OUTPUT=$(python3 enroll_gallery.py \
    --observation-id "${SELECTED_SOURCE_OBS_ID}" \
    --external-person-id "${EXT_ID}" \
    --set-primary \
    --source-type "test_e2e" \
    --created-by "f3_5b_smoke" 2>&1)

echo "${ENROLL_OUTPUT}"

# Extract person_id and gallery_embedding_id from output
PERSON_ID=$(echo "${ENROLL_OUTPUT}" | grep -oP '(?:Created person|Reusing existing person): id=\K[0-9]+' || true)
if [ -z "${PERSON_ID}" ]; then
    echo "FAIL: could not extract person_id from enroll output"
    exit 1
fi

GALLERY_ID=$(echo "${ENROLL_OUTPUT}" | grep -oP 'Enrolled gallery embedding: id=\K[0-9]+' || true)
if [ -z "${GALLERY_ID}" ]; then
    echo "FAIL: could not extract gallery_embedding_id from enroll output"
    exit 1
fi

echo ""
echo "  person_id=${PERSON_ID}"
echo "  gallery_embedding_id=${GALLERY_ID}"

# ── 3. Match observation against gallery ────────────────────────────────────

echo ""
echo "--- Step 3: Match observation against gallery ---"

MATCH_OUTPUT=$(python3 match_gallery.py \
    --observation-id "${SELECTED_SOURCE_OBS_ID}" \
    --top-k 5 \
    --min-similarity 0.5 \
    --search-request-id "${F3_5B_SEARCH_REQUEST_ID}" 2>&1)

echo "${MATCH_OUTPUT}"

# ── 4. Verify match_results ─────────────────────────────────────────────────

echo ""
echo "--- Step 4: Verify match_results ---"

python3 - "$DB_URL" "$F3_5B_SEARCH_REQUEST_ID" "$PERSON_ID" "$GALLERY_ID" "$EXT_ID" \
           "$SELECTED_OBS_UUID" "$SELECTED_SOURCE_OBS_ID" << 'PYEOF'
import sys

DB_URL = sys.argv[1]
SEARCH_REQ_ID = sys.argv[2]
PERSON_ID = int(sys.argv[3])
GALLERY_ID = int(sys.argv[4])
EXT_ID = sys.argv[5]
SELECTED_OBS_UUID = sys.argv[6]
SELECTED_SOURCE_OBS_ID = sys.argv[7]

import psycopg
from psycopg.rows import dict_row

conn = psycopg.connect(DB_URL)
with conn.cursor(row_factory=dict_row) as cur:
    cur.execute("""
        SELECT
            mr.search_request_id,
            mr.search_mode,
            mr.query_person_id,
            mr.query_gallery_embedding_id,
            mr.query_observation_id,
            mr.query_source_observation_id,
            mr.matched_observation_id,
            mr.rank,
            mr.similarity,
            p.name AS person_name,
            p.external_person_id
        FROM match_results mr
        JOIN persons p ON p.id = mr.query_person_id
        WHERE mr.search_request_id = %(req)s
        ORDER BY mr.rank ASC
    """, {"req": SEARCH_REQ_ID})
    rows = cur.fetchall()

if not rows:
    print("FAIL: no match_results found for search_request_id")
    sys.exit(1)

print(f"Found {len(rows)} match_result(s)")

# Verify rank 1
rank1 = None
for row in rows:
    if row["rank"] == 1:
        rank1 = row
        break

if rank1 is None:
    print("FAIL: no rank=1 row found")
    sys.exit(1)

errors = []

# Check top1 person
if rank1["query_person_id"] != PERSON_ID:
    errors.append(f"person_id mismatch: expected {PERSON_ID}, got {rank1['query_person_id']}")

if rank1["query_gallery_embedding_id"] != GALLERY_ID:
    errors.append(f"gallery_embedding_id mismatch: expected {GALLERY_ID}, got {rank1['query_gallery_embedding_id']}")

if rank1["external_person_id"] != EXT_ID:
    errors.append(f"external_person_id mismatch: expected {EXT_ID}, got {rank1['external_person_id']}")

# gallery_match semantics: matched_observation_id is NULL
if rank1["matched_observation_id"] is not None:
    errors.append(f"matched_observation_id should be NULL for gallery_match, got {rank1['matched_observation_id']}")

# query_observation_id must equal selected observation UUID
if str(rank1["query_observation_id"]) != SELECTED_OBS_UUID:
    errors.append(
        f"query_observation_id mismatch: expected {SELECTED_OBS_UUID}, "
        f"got {rank1['query_observation_id']}"
    )

# query_source_observation_id must equal selected source_observation_id
if rank1["query_source_observation_id"] != SELECTED_SOURCE_OBS_ID:
    errors.append(
        f"query_source_observation_id mismatch: expected {SELECTED_SOURCE_OBS_ID}, "
        f"got {rank1['query_source_observation_id']}"
    )

sim = float(rank1["similarity"])

# Enforce self-match threshold
if sim < 0.99:
    errors.append(f"similarity {sim:.6f} < 0.99: self-match threshold not met")

# Report
print()
print("=== Recognition Result ===")
print(f"Recognized person:         {rank1['external_person_id']}")
print(f"search_request_id:         {SEARCH_REQ_ID}")
print(f"query_observation_id:      {rank1['query_observation_id']}")
print(f"query_source_observation_id: {rank1['query_source_observation_id']}")
print(f"person_id:                 {rank1['query_person_id']}")
print(f"gallery_embedding_id:      {rank1['query_gallery_embedding_id']}")
print(f"rank:                      {rank1['rank']}")
print(f"similarity:                {sim:.6f}")
print(f"search_mode:               {rank1['search_mode']}")
print(f"matched_observation_id:    {rank1['matched_observation_id']} (NULL = gallery_match)")
print()

if errors:
    for e in errors:
        print(f"FAIL: {e}")
    sys.exit(1)

print("PASS: similarity >= 0.99 self-match confirmed")
print("PASS: all assertions passed")
print("=== F3.5b Recognition E2E PASS ===")
conn.close()
PYEOF

# ── 5. Print camera context and final summary ──────────────────────────────

echo ""
echo "--- Step 5: Camera context ---"

python3 - "$DB_URL" "$SELECTED_SOURCE_OBS_ID" << 'PYEOF'
import sys

DB_URL = sys.argv[1]
SOURCE_OBS_ID = sys.argv[2]

import psycopg
from psycopg.rows import dict_row

conn = psycopg.connect(DB_URL)
with conn.cursor(row_factory=dict_row) as cur:
    cur.execute("""
        SELECT camera_id, source_id, track_id, timestamp_ms
        FROM face_observations
        WHERE source_observation_id = %s
    """, (SOURCE_OBS_ID,))
    row = cur.fetchone()

if row is None:
    print("FAIL: original face_observation was deleted!")
    sys.exit(1)

print(f"camera_id:    {row['camera_id']}")
print(f"source_id:    {row['source_id']}")
print(f"track_id:     {row['track_id']}")
print(f"timestamp_ms: {row['timestamp_ms']}")
print()
print(f"Original observation preserved: {SOURCE_OBS_ID}")
conn.close()
PYEOF
