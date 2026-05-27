# Phase F3.2 — pgvector Similarity Search Harness

Date: 2026-05-27
Status: complete
Parent: F3.1 (face observations persisted to PostgreSQL with pgvector)

## Purpose

Add a read-path similarity search harness over the `face_observations` table using pgvector's exact cosine distance operator (`<=>`).

This is a **pure harness** — no watchlist_hit, no live_search_hit, no security.events, no person gallery.

## Scope

- `FaceVectorStore.search_similar_faces()` — given a 512-d query embedding, return topK similar observations
- Exact search only (no ANN/HNSW/IVFFlat index — deferred to F3.3)
- Metadata-only results by default (512-d embedding not returned unless `include_embedding=True`)
- Strict query embedding validation (same policy as F3.1)
- Dynamic WHERE clause for optional `min_similarity` threshold and `camera_scope` filtering
- Parameterized SQL — no string interpolation

## Out of scope

- watchlist_hit / live_search_hit events
- Person gallery / `person_gallery_embeddings` table
- HNSW / IVFFlat / ANN index
- `FaceVectorStore.add_gallery_embedding()` / `search_gallery()` / `search_live_target()`
- `security.events` output
- REST API endpoint

## Files changed

| File | Change |
|---|---|
| `services/face-worker/app/vector_store.py` | New: `FaceVectorStore` class + `_validate_query_embedding()` |
| `harness/tests/test_face_vector_store.py` | New: 31 unit tests + 8 integration tests |
| `scripts/smoke/check_f3_2_pgvector_similarity_search.sh` | New: read-only smoke against live `face_observations` |
| `docs/phase_f3_2_pgvector_similarity_harness.md` | New: this file |
| `specs/04_face_intelligence.md` | Updated: record F3.2 harness under §12 |
| `specs/05_database_schema.md` | Updated: note search harness over `face_observations` |

## Similarity metric

- **Operator**: pgvector `<=>` (cosine distance)
- **similarity = 1 - cosine_distance**
- Expected useful range near [0, 1] for L2-normalized AdaFace embeddings (norm ≈ 1.0)
- This is NOT a calibrated identity probability
- Values may slightly exceed [0, 1] due to floating-point error

## Smoke result (2026-05-27)

- DB: `video_analytics` @ localhost:5438
- `face_observations` rows: 20,729
- Query: most recent observation (latest `created_at`)
- vector_dims: 512 confirmed
- Top1 similarity: 1.0 (distance 0.0)
- Multiple rows with identical embeddings due to deterministic AdaFace on looped test video (expected)
- Camera scope filter: verified working
- Read-only: confirmed no INSERT/UPDATE/DELETE

## Verification

- 31 unit tests pass (mocked, no DB)
- 8 integration tests pass (real PostgreSQL + pgvector; test rows use `test:f3_2:` prefix, cleaned up in `finally`; verification confirms zero `test:f3_2:%` rows remain)
- Smoke script passes (read-only, live data)
- No watchlist_hit / live_search_hit / security.events code introduced
- No HNSW/IVFFlat/ANN index created

## Next: F3.3/F4

- F3.3: Evaluate ANN index (HNSW/IVFFlat) for production scale
- F4: Watchlist — `person_gallery_embeddings`, `watchlist_rules`, `watchlist_hit` events
