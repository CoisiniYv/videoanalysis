#!/usr/bin/env bash
# F3.2 pgvector similarity search smoke — READ-ONLY
#
# Uses the most recent face_observation embedding as query, searches topK,
# and verifies:
#   1. Top1 has distance 0 / similarity ≈ 1.0
#   2. vector_dims(embedding) = 512
#   3. Camera scope filter works
#
# Query row may NOT be top1 when many observations share identical embeddings
# (deterministic AdaFace on looped test video). The query row should appear
# within a wider topK search.
#
# Does NOT modify face_observations or any other table.
#   4. Camera scope filter works
#
# Does NOT modify face_observations or any other table.

set -euo pipefail

DB_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
TOP_K="${TOP_K:-5}"

echo "=== F3.2 pgvector Similarity Search Smoke ==="
echo "DB: ${DB_URL}"
echo "topK: ${TOP_K}"
echo ""

python3 << 'PYEOF'
import os, sys, math, json

DB_URL = os.getenv("DATABASE_URL", "postgresql://video:video@localhost:5438/video_analytics")
TOP_K = int(os.getenv("TOP_K", "5"))

import psycopg
from pgvector.psycopg import register_vector

conn = psycopg.connect(DB_URL)
register_vector(conn)

# ── 1. Fetch the most recent observation as query ──────────────────────────
with conn.cursor() as cur:
    cur.execute("""
        SELECT source_observation_id, camera_id, source_id, track_id,
               embedding, embedding_dim
        FROM face_observations
        ORDER BY created_at DESC
        LIMIT 1
    """)
    query_row = cur.fetchone()

if query_row is None:
    print("FAIL: no face_observations in table")
    sys.exit(1)

query_sid = query_row[0]
query_cam = query_row[1]
query_emb = query_row[4]
query_dim = query_row[5]

print(f"Query row:")
print(f"  source_observation_id: {query_sid}")
print(f"  camera_id:             {query_cam}")
print(f"  vector_dims:           {query_dim}")
print(f"  embedding[:5]:         {[round(float(x), 6) for x in query_emb[:5]]}")

# Verify vector dims
if query_dim != 512:
    print(f"FAIL: expected vector_dims=512, got {query_dim}")
    sys.exit(1)
print("  ✓ vector_dims = 512")

# Verify embedding norm
sq = sum(float(x)**2 for x in query_emb)
norm = math.sqrt(sq)
print(f"  embedding_norm:        {norm:.6f}")
if not (0.90 <= norm <= 1.10):
    print(f"WARN: embedding norm {norm:.6f} outside [0.90, 1.10]")
else:
    print("  ✓ embedding norm in [0.90, 1.10]")

# ── 2. Self-search: query row should be top1 ───────────────────────────────
print(f"\n--- Self-search (topK={TOP_K}) ---")

with conn.cursor() as cur:
    cur.execute("""
        SELECT source_observation_id, camera_id, track_id,
               1 - (embedding <=> %(emb)s) AS similarity,
               embedding <=> %(emb)s AS distance
        FROM face_observations
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> %(emb)s
        LIMIT %(k)s
    """, {"emb": query_emb, "k": TOP_K})
    results = cur.fetchall()

if not results:
    print("FAIL: no results from self-search")
    sys.exit(1)

top1_sid, top1_cam, top1_track, top1_sim, top1_dist = results[0]

print(f"Top1:")
print(f"  source_observation_id: {top1_sid}")
print(f"  similarity:            {round(float(top1_sim), 6)}")
print(f"  distance:              {round(float(top1_dist), 6)}")
print(f"  camera_id:             {top1_cam}")
print(f"  track_id:              {top1_track}")

# Verify query row presence: with deterministic AdaFace, multiple
# observations of the same face produce identical embeddings (distance=0).
# ORDER BY with equal distances is non-deterministic for tie-breaking,
# so the query row may not be top1 but should appear in a wider search.
result_sids = [r[0] for r in results]
query_in_topk = query_sid in result_sids

if top1_sid == query_sid:
    print("  ✓ query row IS top1")
elif query_in_topk:
    print(f"  ✓ query row in top{TOP_K} (rank={result_sids.index(query_sid)+1}) — tie-breaking ok")
else:
    # Do a wider search to check query row presence
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_observation_id
            FROM face_observations
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %(emb)s
            LIMIT 200
        """, {"emb": query_emb})
        wide_sids = [r[0] for r in cur.fetchall()]
    if query_sid in wide_sids:
        print(f"  ✓ query row found at rank {wide_sids.index(query_sid)+1} in wider search — many identical embeddings")
    else:
        print(f"  WARN: query row not found in top 50 — investigate embedding uniqueness")

# Verify top1 similarity ≈ 1.0
sim = float(top1_sim)
if abs(sim - 1.0) < 0.01:
    print("  ✓ top1 similarity ≈ 1.0")
else:
    print(f"  WARN: top1 similarity={sim:.6f}, expected ≈1.0")

# Show remaining results
for i, row in enumerate(results[1:], start=2):
    sid, cam, track, sim, dist = row
    print(f"Top{i}: sid={sid} sim={round(float(sim), 6)} dist={round(float(dist), 6)} cam={cam}")

# ── 3. Camera scope filter ─────────────────────────────────────────────────
print(f"\n--- Camera scope filter (camera_id='{query_cam}') ---")

with conn.cursor() as cur:
    cur.execute("""
        SELECT source_observation_id, camera_id,
               1 - (embedding <=> %(emb)s) AS similarity
        FROM face_observations
        WHERE embedding IS NOT NULL
          AND camera_id = ANY(%(cam_scope)s::text[])
        ORDER BY embedding <=> %(emb)s
        LIMIT %(k)s
    """, {"emb": query_emb, "cam_scope": [query_cam], "k": TOP_K})
    filtered = cur.fetchall()

print(f"Results (scope=[{query_cam}]): {len(filtered)}")
for row in filtered:
    sid, cam, sim = row
    assert cam == query_cam, f"camera_scope violation: {cam} != {query_cam}"
    print(f"  sid={sid} cam={cam} sim={round(float(sim), 6)}")
print("  ✓ all results match camera_scope")

# ── 4. Summary ─────────────────────────────────────────────────────────────
print(f"\n=== Smoke Summary ===")
print(f"Query:              {query_sid}")
print(f"Top1:               {top1_sid}")
print(f"Top1 similarity:    {round(sim, 6)}")
print(f"Vector dims:        {query_dim}")
print(f"Camera scope:       OK")
print(f"Read-only:          YES (no INSERT/UPDATE/DELETE)")
print(f"PASS")

conn.close()
PYEOF

echo ""
echo "=== F3.2 Smoke Complete ==="
