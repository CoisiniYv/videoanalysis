# Phase F3.4 — Gallery Schema and Enrollment Harness

Date: 2026-05-27
Status: complete

## 1. Summary

Created the gallery data model and enrollment harness for the face intelligence pipeline.
This phase establishes the foundation for F4 (watchlist / 重点人员布控) and F5 (one-click search / 一键找人).

## 2. Deliverables

### 2.1 Database Migration — `006_phase_f3_4_gallery_schema.sql`

Creates three tables with non-destructive, idempotent migration logic:

| Table | Purpose |
|---|---|
| `persons` | Registered person master data (BIGSERIAL PK) |
| `person_gallery_embeddings` | Long-term gallery vectors (BIGSERIAL PK, FK to persons) |
| `match_results` | Short-lived derived search results (BIGSERIAL PK) |

Migration behavior on existing `persons` table:

- If `persons` does not exist: creates target schema directly.
- If `persons.id` is already BIGINT/BIGSERIAL: no-op, `CREATE TABLE IF NOT EXISTS` skips.
- If old UUID-PK `persons` from migration 001 exists with 0 rows: auto-drops and recreates.
- If old UUID-PK `persons` exists with data: raises `EXCEPTION` requiring manual cutover.

Key design decisions (from F3.3):

- `persons` only represents registered people — temporary upload queries do not create a person.
- `person_gallery_embeddings` stores long-term registered gallery embeddings, not temporary query embeddings.
- `match_results` stores derived search results, not query embeddings or uploaded images.
- `person_gallery_embeddings` has a partial unique index ensuring at most one primary embedding per active person.
- `match_results` has a unique constraint on `(search_request_id, matched_observation_id)`.

### 2.2 Repository Classes

| File | Class | Purpose |
|---|---|---|
| `services/face-worker/app/person_repository.py` | `PersonRepository` | CRUD for `persons` table |
| `services/face-worker/app/gallery_repository.py` | `GalleryRepository` | CRUD for `person_gallery_embeddings` table |

PersonRepository methods:
- `create_person(name, **kwargs) -> int` — returns BIGINT id
- `get_by_id(person_id) -> dict | None`
- `list_active() -> list[dict]`
- `deactivate(person_id) -> bool` — soft-delete

GalleryRepository methods:
- `add_embedding(person_id, embedding, **kwargs) -> int` — validates embedding, returns BIGINT id
- `get_by_id(gallery_id) -> dict | None`
- `list_by_person(person_id) -> list[dict]` — active embeddings only, primary first
- `deactivate(gallery_id) -> bool` — soft-delete

### 2.3 FaceVectorStore Extension

Added `search_gallery()` method to existing `FaceVectorStore`:

```python
def search_gallery(
    self,
    embedding: list[float],
    *,
    top_k: int = 10,
    min_similarity: float | None = None,
    person_ids: list[int] | None = None,
    include_embedding: bool = False,
) -> list[dict[str, Any]]:
```

Searches `person_gallery_embeddings` joined with `persons` using cosine distance.
Returns results ordered by descending similarity with `person_name` included.

### 2.4 Enrollment CLI — `services/face-worker/enroll_gallery.py`

Enrolls a gallery embedding from an existing `face_observation`:

```bash
# Create new person and enroll
python enroll_gallery.py --observation-id face:cam1:42:1000 --person-name "John Doe"

# Add to existing person
python enroll_gallery.py --observation-id face:cam1:42:1000 --person-id 5 --set-primary
```

Reads the observation's embedding from PostgreSQL, creates or resolves the person,
and inserts into `person_gallery_embeddings`.

## 3. Tests

Test file: `harness/tests/test_gallery_enrollment.py`

| Class | Type | Count | Coverage |
|---|---|---|---|
| `TestGalleryEmbeddingValidation` | unit | 13 | Embedding validation (length, norm, types) |
| `TestEnrollCliArgs` | unit | 6 | CLI argparse mutually exclusive selector |
| `TestPersonRepositoryUnit` | unit (mocked) | 9 | PersonRepository CRUD (incl. external_person_id) |
| `TestGalleryRepositoryUnit` | unit (mocked) | 8 | GalleryRepository CRUD |
| `TestSearchGalleryParams` | unit (mocked) | 8 | search_gallery parameter handling + active filter |
| `TestSearchGalleryResult` | unit (mocked) | 4 | search_gallery result shape |
| `TestGalleryIntegration` | integration | 18 | Real PostgreSQL end-to-end (incl. provenance, deactivation) |

Total: **66 tests** (48 unit + 18 integration)

```bash
# Without DATABASE_URL: 48 passed, 18 integration tests skipped
pytest -q harness/tests/test_gallery_enrollment.py

# With DATABASE_URL: 66 passed
DATABASE_URL="postgresql://video:video@localhost:5438/video_analytics" \
  pytest -q harness/tests/test_gallery_enrollment.py
```

### Known Notes

- F3.4's own integration tests (18 tests in `TestGalleryIntegration`) pass and leave zero test rows in the database.
- The broader cross-suite run (gallery + face_vector_store + face_worker: 164 total) depends on DATABASE_URL connectivity. In environments where face_vector_store integration tests cannot connect, they are skipped; this does not affect F3.4 correctness.
- F3.4 does not depend on face_vector_store integration tests passing in every external harness environment.

## 4. What F3.4 Does NOT Implement

Per scope boundary:

- No `watchlist_hit` event (F4)
- No `live_search_hit` event (F5)
- No `security.events` writes
- No retention deletion worker
- No temporary upload search
- No Savant pipeline changes
- No face observation producer changes
- No API endpoints
- No `face_observations` nullable migration (deferred to retention phase)

## 5. Migration Notes

Migration 006 is non-destructive and idempotent. A `DO` block detects the state of the existing `persons` table:

- **No `persons` table**: proceeds to `CREATE TABLE IF NOT EXISTS`.
- **`persons.id` is already BIGINT**: no-op (already migrated).
- **Old UUID-PK `persons` with 0 rows**: auto-drops and recreates. This covers the migration-001 placeholder that was never wired to production (`events.person_id` was redefined as INTEGER without FK in migration 002; no other table has a FK to the old `persons`).
- **Old UUID-PK `persons` with data**: raises `EXCEPTION` with manual cutover instructions. This prevents silent data loss.

All `CREATE TABLE` and `CREATE INDEX` statements use `IF NOT EXISTS`, so re-running after a successful first run is a no-op.
