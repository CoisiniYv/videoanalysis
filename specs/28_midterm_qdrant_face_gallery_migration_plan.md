# 28_midterm_qdrant_face_gallery_migration_plan.md

Date: 2026-06-29

## 1. Purpose

This spec replaces the current face gallery vector search path from
PostgreSQL/pgvector to Qdrant while keeping the existing midterm architecture
stable.

Scope:

- registered-person gallery matching for `watchlist_hit`;
- `person_gallery_embeddings` as the PostgreSQL source of truth;
- Qdrant as the derived high-performance vector serving layer;
- no change to Savant, YOLOv8-Face, AdaFace, event-worker, clip-worker,
  media-worker, or 8090 payload semantics.
- no change to the current evidence storage model: PostgreSQL keeps evidence
  metadata and the filesystem keeps playable video artifacts.

Non-scope:

- replacing AdaFace embeddings;
- switching to ArcFace SDK for feature extraction;
- moving watchlist matching into Savant;
- deleting pgvector columns from PostgreSQL;
- migrating historical `face_observations` similarity search to Qdrant in the
  first cutover.
- solving every face-worker throughput issue in this same cut. If baseline
  proves the dominant cost is Redis/DB I/O, rule resolution, synchronous
  coupling, or Python CPU rather than vector retrieval, Spec 27 still governs
  the next optimization.

## 2. Decision

Use Qdrant for the online gallery nearest-neighbor lookup.

Keep PostgreSQL as the canonical database:

```text
PostgreSQL
  persons
  person_gallery_embeddings
  face_observations
  events
  evidence_tasks
```

Add Qdrant as a rebuildable search index:

```text
Qdrant
  collection: face_gallery_adaface_512_v1
  alias:      face_gallery_current
  point_id:   person_gallery_embeddings.id
```

The current hot path becomes:

```text
Savant AdaFace / face_observation_exporter
  -> Redis Stream security.face_observations
  -> face-worker
       -> validate embedding
       -> persist face_observations in PostgreSQL
       -> resolve watchlist rules and target persons from PostgreSQL
       -> search Qdrant face_gallery_current
       -> optional exact rerank of Qdrant candidates
       -> publish watchlist_hit to Redis Stream security.events
  -> event-worker
       -> persist events
       -> create evidence_tasks
       -> publish record requests
```

This keeps Qdrant outside the Savant GPU inference path. Qdrant failure must not
restart Savant or change frame ingestion behavior.

The migration must be baseline-first. Do not make Qdrant authoritative until the
current pgvector behavior, target-list cardinality, threshold behavior, and
face-worker p95/p99 have been measured. Qdrant is a performance serving layer,
not a new source of truth.

## 3. Why Qdrant Fits This Codebase

Current code facts:

- `face-worker` already receives 512-d AdaFace embeddings from Redis. It does
  not need a face SDK for extraction.
- `services/face-worker/app/vector_store.py` currently performs exact pgvector
  gallery search with `ORDER BY pge.embedding <=> %(query_embedding)s`.
- `db/migrations/006_gallery_schema.sql` keeps gallery vectors in
  `person_gallery_embeddings.embedding vector(512)`.
- `face-worker` already returns a small gallery-match dict that is consumed by
  `build_watchlist_hit_event()`.

Qdrant is a better replacement target than ArcFace SDK for this bottleneck
because the remaining problem is vector retrieval and operational scaling, not
embedding extraction.

Important compatibility nuance:

- If a rule targets only a few people, an exact PostgreSQL path over those
  specific gallery rows may still be cheaper and simpler than a Qdrant ANN
  query plus exact rerank.
- Qdrant provides the biggest benefit for broad search or high-cardinality
  target lists.
- The implementation must therefore support a measured hybrid policy rather
  than assuming every query should go to Qdrant.

Compatibility goal:

- `WatchlistMatchEmitter` should receive search results with the same logical
  fields as today:
  - `id`;
  - `person_id`;
  - `person_name`;
  - `external_person_id`;
  - `source_type`;
  - `embedding_model`;
  - `is_primary`;
  - `quality`;
  - `similarity`;
  - `distance`;
  - `created_at`.
- `build_watchlist_hit_event()` and `events.source_event_id` generation remain
  unchanged.
- Rollback is an environment change back to the pgvector adapter, not a DB
  rollback.

## 4. Qdrant Data Model

### 4.1 Collection

Collection name:

```text
face_gallery_adaface_512_v1
```

Alias used by runtime:

```text
face_gallery_current
```

Vector config:

```text
size: 512
distance: Cosine
```

Point id:

```text
point_id = person_gallery_embeddings.id
```

`BIGSERIAL` gallery ids are positive integers and map cleanly to Qdrant point
ids. A future multi-tenant or multi-site deployment can switch to UUID point ids
without changing the event payload, because the payload still carries
`gallery_embedding_id`.

### 4.2 Payload

Each active gallery row is indexed as one Qdrant point:

```json
{
  "gallery_embedding_id": 123,
  "person_id": 45,
  "person_name": "Reese",
  "external_person_id": "demo:midterm:reese",
  "source_type": "manual_upload",
  "embedding_model": "adaface",
  "model_version": null,
  "is_primary": true,
  "is_active": true,
  "quality": 0.91,
  "created_at": "2026-06-29T00:00:00Z",
  "updated_at": "2026-06-29T00:00:00Z"
}
```

Required payload indexes:

```text
person_id: integer
external_person_id: keyword
embedding_model: keyword
is_active: bool
is_primary: bool
```

Optional payload indexes:

```text
quality: float
updated_at: datetime or keyword
```

Search filter:

```text
must:
  is_active == true
  embedding_model == "adaface"
  person_id in resolved_target_person_ids
```

The `person_id` filter preserves the current watchlist semantics. Broad gallery
search is allowed only when the rule explicitly targets all active persons or
when a live-search feature asks for broad search.

The Qdrant filter builder must have unit tests for:

- one target person;
- many target persons;
- explicit all-active-person search;
- empty target list, which must return no result and must not silently broaden
  to all persons;
- inactive person/gallery rows.

### 4.3 Deletes And Deactivation

PostgreSQL remains source of truth.

When a gallery row becomes inactive:

1. mark `person_gallery_embeddings.is_active = false` in PostgreSQL;
2. enqueue a Qdrant delete operation for that `gallery_embedding_id`;
3. Qdrant runtime search still filters `is_active == true` as a guard.

Deleting the Qdrant point is preferred over leaving inactive points in the
collection because stale watchlist matches are worse than a small sync delay.

### 4.4 Score And Threshold Contract

The current pgvector path uses:

```text
distance   = embedding <=> query_embedding
similarity = 1 - distance
```

Qdrant cosine search returns a higher-is-better score for cosine similarity.
The adapter must expose the same logical fields as the pgvector path:

```text
similarity = qdrant_score
distance   = 1 - qdrant_score
```

During rollout, `QDRANT_EXACT_RERANK_ENABLED=true` is the authority for final
watchlist decisions. Unit tests must prove the score mapping with deterministic
vectors and must prove threshold equality around boundary values such as
`threshold - 0.001`, `threshold`, and `threshold + 0.001`.

## 5. Code Architecture

### 5.1 Search Adapter Boundary

Add a small adapter interface instead of letting `WatchlistMatchEmitter` know
which vector backend is active.

Suggested module:

```text
services/face-worker/app/gallery_search.py
```

Logical interface:

```python
class GallerySearchBackend:
    def search_gallery(
        self,
        embedding: list[float],
        *,
        top_k: int,
        min_similarity: float | None,
        person_ids: list[int] | None,
        include_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        ...
```

Implementations:

- `PgvectorGallerySearchBackend`
  - wraps the current `FaceVectorStore.search_gallery()`;
  - remains the rollback path;
  - remains the exact baseline for parity tests.
- `QdrantGallerySearchBackend`
  - calls Qdrant `face_gallery_current`;
  - maps Qdrant score/payload into the current gallery-match dict;
  - applies the same `top_k`, threshold, and target-person contract.
- `ShadowGallerySearchBackend`
  - emits decisions from pgvector;
  - runs Qdrant in parallel or immediately after;
  - logs mismatch metrics without changing event output.
- `HybridGallerySearchBackend`
  - uses the exact pgvector path for very small target sets;
  - uses Qdrant for broad or high-cardinality target sets;
  - logs the selected route and target cardinality for every query.

Runtime selector:

```text
FACE_VECTOR_BACKEND=pgvector|qdrant|shadow|hybrid
```

Default during rollout:

```text
FACE_VECTOR_BACKEND=pgvector
```

Hybrid routing defaults:

```text
FACE_VECTOR_SMALL_TARGET_THRESHOLD=5
FACE_VECTOR_HYBRID_BROAD_USES_QDRANT=true
```

The threshold must be chosen from Stage 0 measurements. If small target exact
search is already below SLA, keep it on pgvector/exact and avoid unnecessary
network round trips to Qdrant.

### 5.2 Exact Rerank Policy

Qdrant is the candidate generator. For watchlist event emission, start with
exact rerank enabled:

```text
QDRANT_EXACT_RERANK_ENABLED=true
QDRANT_CANDIDATE_MULTIPLIER=3
QDRANT_MIN_CANDIDATES=20
```

Flow:

1. Query Qdrant with `top_k * QDRANT_CANDIDATE_MULTIPLIER`.
2. Fetch candidate gallery rows by primary key from PostgreSQL.
3. Recompute cosine similarity in Python against those candidate vectors.
4. Apply the same threshold.
5. Deduplicate by `person_id`.
6. Emit the best match per person/rule.

This avoids a PostgreSQL vector scan while keeping watchlist decisions anchored
to the same exact cosine math during migration. After parity and pressure runs
are stable, exact rerank can be disabled only with a documented acceptance run.

Exact rerank requirements:

- candidate fetch must use primary keys from Qdrant point ids;
- candidate rows must be fetched from PostgreSQL source-of-truth tables;
- inactive rows must be filtered again after fetch;
- missing candidate rows must increment a mismatch/degraded counter;
- final event emission must use the reranked result, not the raw Qdrant score,
  while exact rerank is enabled.

### 5.3 Qdrant Client Configuration

New environment variables:

```text
FACE_VECTOR_BACKEND=pgvector
FACE_VECTOR_SMALL_TARGET_THRESHOLD=5
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=face_gallery_current
QDRANT_TIMEOUT_SECONDS=2.0
QDRANT_SEARCH_EF=128
QDRANT_CANDIDATE_MULTIPLIER=3
QDRANT_MIN_CANDIDATES=20
QDRANT_EXACT_RERANK_ENABLED=true
QDRANT_FALLBACK_TO_PGVECTOR=true
QDRANT_WRITE_WAIT=true
```

Fallback behavior:

- In `shadow` mode, pgvector is authoritative.
- In `qdrant` mode with `QDRANT_FALLBACK_TO_PGVECTOR=true`, Qdrant errors fall
  back to pgvector and emit a structured degraded log.
- In `qdrant` mode with fallback disabled, search failures must not emit a
  guessed `watchlist_hit`. They should be observable as failed or pending match
  work.

Fallback safety:

- fallback must increment a counter and appear in pressure reports;
- fallback is allowed during canary but must be zero in the final authoritative
  acceptance run;
- fallback must not change the `watchlist_hit` payload schema;
- fallback must not ACK a match request as successful unless an event decision
  was actually made or an explicit no-match result was produced.

### 5.4 Gallery Sync Boundary

Do not make Qdrant the writer of gallery state.

Add a PostgreSQL outbox table in migration 021:

```text
gallery_vector_sync_outbox
```

Minimum columns:

```text
id BIGSERIAL PRIMARY KEY
gallery_embedding_id BIGINT NOT NULL
operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete'))
status TEXT NOT NULL DEFAULT 'pending'
attempts INTEGER NOT NULL DEFAULT 0
last_error TEXT
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
processed_at TIMESTAMPTZ
claimed_at TIMESTAMPTZ
claimed_by TEXT
next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()
```

Indexes:

```text
(status, created_at)
(status, next_attempt_at, created_at)
(gallery_embedding_id, created_at DESC)
```

Writers:

- `libs/face_registration/gallery_repository.py`
- `services/face-worker/app/gallery_repository.py`
- primary-promotion updates in `image_face_registration.py`
- `services/api/app/repositories/maintenance.py` soft-delete paths

Sync worker:

```text
services/face-worker/sync_qdrant_gallery.py
```

Modes:

```text
--mode bootstrap
--mode drain-outbox
--mode reconcile
--mode rebuild
```

The first implementation can run `drain-outbox` in the `face-worker` image as a
separate process. It should not be embedded into Savant.

Outbox transaction and ordering rules:

- PostgreSQL gallery row change and outbox insert must commit in the same
  transaction.
- Sync workers must claim rows with `FOR UPDATE SKIP LOCKED`.
- A worker may process multiple outbox rows concurrently only when it preserves
  per-`gallery_embedding_id` order.
- If several pending rows exist for the same `gallery_embedding_id`, the sync
  worker may coalesce them only when the final operation is equivalent to the
  latest PostgreSQL source-of-truth state.
- Failed rows must use retry/backoff through `next_attempt_at`; after a bounded
  number of attempts they move to `failed` or `poisoned` and become visible in
  8090/runtime diagnostics.
- `reconcile` must be able to repair missed outbox rows by comparing
  PostgreSQL active gallery rows with Qdrant points.

## 6. Compose And Deployment Plan

Add a Qdrant service to the midterm compose file.

Recommended shape:

```yaml
qdrant:
  image: qdrant/qdrant:v1.18.2
  container_name: video-analytics-midterm-qdrant
  profiles: ["qdrant"]
  expose:
    - "6333"
  volumes:
    - /data/video-analytics/qdrant-midterm:/qdrant/storage
  restart: unless-stopped
```

Production deployments that need Qdrant API-key enforcement should add the
auth env only when the key is non-empty, preferably through a production
override:

```yaml
environment:
  QDRANT__SERVICE__API_KEY: "${QDRANT_API_KEY}"
```

Notes:

- `v1.18.2` was the latest stable Qdrant release found during this 2026-06-29
  planning pass. Keep the image pinned, and bump it only through a dedicated
  validation run. Do not deploy `latest`.
- Keep Qdrant on the Docker network. Do not publish host port `6333` in the
  production compose path unless explicitly needed for local diagnostics.
- Store Qdrant data under `/data/video-analytics/qdrant-midterm`.
- Treat `/data/video-analytics/qdrant-midterm` as rebuildable derived state.
  Clean-machine migration should not require carrying it.
- Offline packaging with `--include-images` must include the pinned Qdrant
  image in `images.tar` and `image_list.txt`.
- Qdrant exposes `/healthz`, `/livez`, and `/readyz`. The official container may
  not include `curl` or `wget`, so do not add a brittle in-container
  healthcheck unless the image contents are verified. The sync worker and
  pressure harness should poll `http://qdrant:6333/readyz` before use.
- `face-worker` itself should still be able to start in `pgvector` mode without
  Qdrant.
- Adding `qdrant-client` changes the face-worker dependency layer and therefore
  requires rebuilding/recreating the face-worker image only. It must not trigger
  Savant, Replay, or media-worker rebuilds.
- If `QDRANT_API_KEY` is empty for local dev, the compose rendering and Qdrant
  startup smoke must prove the service behaves as unauthenticated rather than
  enabling auth with an empty key. If that is ambiguous, use a separate override
  file for authenticated production deployments.

## 7. Migration Stages

### Stage 0 - Contract And Baseline Lock

Goal:

- freeze the exact behavior that Qdrant must preserve.
- prove whether vector retrieval is actually the dominant face-worker cost.

Actions:

- capture current `person_gallery_embeddings` active/total counts;
- capture current watchlist target count per active rule;
- capture pgvector exact topK for a deterministic query set;
- capture current `watchlist_hit` payload examples;
- capture face-worker gallery query p50/p95/p99 and Redis pending;
- record the current threshold and topK values.
- capture current small-target exact search p95/p99 for target cardinalities
  1, 2, 5, 10, all-active;
- capture DB insert p50/p95/p99, rule-resolution p50/p95/p99, event-publish
  p50/p95/p99, and end-to-end face-worker ACK latency;
- decide the initial `FACE_VECTOR_SMALL_TARGET_THRESHOLD`.

Acceptance:

```text
PASS_QDRANT_SPEC0_CONTRACT_LOCKED
```

Pass criteria:

- A baseline report exists under `docs/`.
- It includes pgvector exact results for at least:
  - current real gallery rows;
  - a synthetic 10k gallery;
  - a synthetic 100k gallery or the selected production gallery size,
    whichever is larger.
- The report states whether exact rerank is required for cutover.
- The report states whether Qdrant, hybrid routing, or persistence/matching
  decoupling should be the next dominant optimization.

2026-06-29 baseline checkpoint:

- Baseline report:
  `docs/midterm_qdrant_face_gallery_baseline_2026-06-29.md`.
- Current live database had 3 active persons, 5 total persons, 3 active gallery
  embeddings, and 5 total gallery embeddings.
- Current active watchlist rules were target-filtered:
  `primary_rtsp` targeted Reese/Finch and `lab` targeted zr.
- Current `security.face_observations` and `security.events` consumer groups
  had pending count 0 during the sample.
- Current target-filtered pgvector exact gallery query over the real gallery was
  p50 0.167ms, p95 0.198ms, p99 0.301ms across 200 samples.
- Conclusion: the current small live gallery is not the bottleneck. Initial
  hybrid threshold remains `FACE_VECTOR_SMALL_TARGET_THRESHOLD=5`; small target
  lists should stay pgvector/exact, while Qdrant is justified for production
  high-cardinality or all-active watchlist search.
- Synthetic 10k/100k gallery benchmarks remain pending and are required before
  claiming production gallery-size performance.

### Stage 1 - Infrastructure Added But Inert

Goal:

- add Qdrant without changing matching behavior.

Actions:

- add `qdrant-client` to `services/face-worker/requirements.txt`;
- add Qdrant env variables to `Config`;
- add Qdrant compose service and persistent volume;
- add a healthcheck command or script;
- add `FACE_VECTOR_BACKEND=pgvector` as the default;
- add static tests that ensure default runtime remains pgvector.
- add offline packaging support for the pinned Qdrant image when
  `--include-images` is used.

2026-06-29 implementation checkpoint:

- Added `qdrant-client` to `services/face-worker/requirements.txt`.
- Added `FACE_VECTOR_BACKEND=pgvector|shadow|qdrant|hybrid` config fields with
  pgvector as the default.
- Added the Qdrant compose service behind the explicit `qdrant` profile and no
  published host port.
- Added packaging/deployment support for `/data/video-analytics/qdrant-midterm`
  and the pinned Qdrant image in offline image bundles.
- Added static tests for default pgvector behavior, compose profile placement,
  Qdrant result-shape compatibility, and pressure-report schema fields.
- Runtime Qdrant startup was completed after proxy recovery:
  `video-analytics-midterm-qdrant` runs `qdrant/qdrant:v1.18.2` on the compose
  network without publishing host port `6333`.
- Qdrant runtime search now defaults to gRPC through `QDRANT_PREFER_GRPC=true`
  to avoid HTTP/JSON overhead for 512-d vector queries.

Acceptance:

```text
PASS_QDRANT_SPEC1_SERVICE_BOOTSTRAPPED
```

Pass criteria:

- `face-worker` starts and matches with pgvector when Qdrant is absent.
- Qdrant starts under its profile and reports healthy.
- No `watchlist_hit` payload changes are observed.
- `docker compose ... config` shows Qdrant only in the intended profile or
  documented runtime path.
- Offline image packaging includes Qdrant only when requested.
- `git diff --check` and targeted static tests pass.

### Stage 2 - Collection Bootstrap And Sync

Goal:

- make Qdrant contain the same active gallery as PostgreSQL.

Actions:

- add migration `021_qdrant_gallery_sync_outbox.sql`;
- implement collection creation and payload-index creation;
- implement bulk bootstrap from PostgreSQL to Qdrant;
- implement outbox drain for upsert/delete;
- implement transactional outbox writes in every gallery writer path;
- implement `FOR UPDATE SKIP LOCKED` claim, retry/backoff, and failed/poisoned
  visibility;
- implement reconcile:
  - Qdrant point count equals active PostgreSQL gallery count;
  - all active `gallery_embedding_id` rows exist in Qdrant;
  - no inactive gallery point is searchable with `is_active == true`;
  - payload `person_id` and `external_person_id` match PostgreSQL.

2026-06-29 implementation checkpoint:

- Added migration `021_qdrant_gallery_sync_outbox.sql`.
- Added `gallery_vector_sync_outbox` with pending/retry/processing/completed/
  poisoned states.
- Added PostgreSQL triggers on `person_gallery_embeddings` and `persons` so
  insert/update/delete/deactivation/name/external-id changes enqueue sync work
  in the same transaction.
- Added `gallery_sync_outbox.py` with `FOR UPDATE SKIP LOCKED` claim,
  retry/backoff, completion, and status summary helpers.
- Added `sync_qdrant_gallery.py` modes:
  `bootstrap`, `drain-outbox`, `reconcile`, `rebuild`, and `status`.
- Applied migration 021 successfully on the current live `phase0-postgres`
  database.
- Runtime bootstrap/reconcile/drain have been executed. Current live status is
  PostgreSQL active gallery count 3, Qdrant point count 3, outbox active 0.
- Collection creation/update now enforces production-oriented query tuning:
  payload indexes, `on_disk_payload=true`, HNSW `m=16`, `ef_construct=100`,
  `full_scan_threshold=1000`, optimizer `indexing_threshold=1000`, and
  `default_segment_number=2`.

Acceptance:

```text
PASS_QDRANT_SPEC2_GALLERY_SYNC_READY
```

Pass criteria:

- Bootstrap is idempotent.
- Re-running bootstrap does not duplicate points.
- Deactivating a gallery row removes or disables the Qdrant point.
- Newly registered faces become searchable within the selected sync SLA.
- Default sync SLA: p95 outbox-to-Qdrant lag <= 5 seconds on the midterm host.
- Reordering tests prove deactivate/delete cannot be overwritten by an older
  pending upsert for the same gallery id.
- Reconcile can repair a deliberately deleted Qdrant point from PostgreSQL.

### Stage 3 - Shadow Read Parity

Goal:

- prove Qdrant can replace pgvector for gallery search before it emits events.

Actions:

- implement `ShadowGallerySearchBackend`;
- in `shadow` mode, pgvector remains authoritative;
- implement score/distance mapping tests before live shadow;
- implement hybrid route logging and target-cardinality metrics;
- run Qdrant search for the same query and log:
  - qdrant latency;
  - pgvector latency;
  - top1 person parity;
  - topK overlap;
  - threshold crossing mismatch;
  - missing target ids;
  - score delta.
  - selected route: pgvector_exact, qdrant, qdrant_exact_rerank, or fallback.

Acceptance:

```text
PASS_QDRANT_SPEC3_SHADOW_PARITY
```

Pass criteria:

- On the deterministic acceptance dataset:
  - no false positive `watchlist_hit` decisions compared with pgvector exact;
  - no false negative above the accepted threshold;
  - top1 person parity is 100 percent for thresholded matches.
- On the live shadow pressure run:
  - mismatch count is 0, or every mismatch is explained by approximate recall
    and removed by exact rerank;
  - Qdrant p95 is lower than pgvector p95 at representative gallery size;
  - Redis `security.face_observations` pending returns to 0 after drain.
- Hybrid mode routes small target lists according to the Stage 0 decision and
  does not broaden empty target lists.

2026-06-29 implementation checkpoint:

- Added `PgvectorGallerySearchBackend`, `QdrantGallerySearchBackend`,
  `ShadowGallerySearchBackend`, and `HybridGallerySearchBackend`.
- Added Qdrant payload filter tests for target person ids and empty target
  safety.
- Added score mapping tests for `similarity = qdrant_score` and
  `distance = 1 - qdrant_score`.
- Added exact rerank tests proving PostgreSQL candidate vectors, not raw Qdrant
  ordering, decide thresholded output when exact rerank is enabled.
- Added fallback tests for Qdrant errors.
- Live shadow-only pressure was not retained as a separate acceptance artifact.
  The cutover instead kept exact rerank enabled and verified Qdrant
  authoritative pressure with fallback disabled, no watchlist emit failures, and
  retained 8090 evidence. If approximate-only Qdrant decisions are later
  desired, rerun Stage 3 with exact rerank disabled only after a dedicated
  parity acceptance run.

### Stage 4 - Canary Cutover

Goal:

- emit production events from Qdrant with fast rollback.

Actions:

- set `FACE_VECTOR_BACKEND=qdrant` for a canary camera, rule, or short pressure
  window;
- keep `QDRANT_EXACT_RERANK_ENABLED=true`;
- keep `QDRANT_FALLBACK_TO_PGVECTOR=true`;
- compare emitted `watchlist_hit` event count and identities to shadow baseline;
- verify evidence generation through 8090.

Canary note:

- The initial selector is process-wide through env. A camera/rule-scoped canary
  is allowed only if an explicit per-rule backend override is added. Without
  that override, canary means a short controlled service-level window.

Acceptance:

```text
PASS_QDRANT_SPEC4_MATCHING_CUTOVER
```

Pass criteria:

- No duplicate `events.source_event_id` rows.
- No unexpected `watchlist_hit` payload schema differences.
- Qdrant search p95 and p99 are in the pressure report.
- Retained watchlist evidence remains queryable through 8090.
- Rollback to `FACE_VECTOR_BACKEND=pgvector` works by service restart only.

2026-06-29 cutover checkpoint:

- Runtime was switched to `FACE_VECTOR_BACKEND=qdrant`,
  `QDRANT_FALLBACK_TO_PGVECTOR=false`, `QDRANT_PREFER_GRPC=true`.
- `face-worker` restarted cleanly without rebuilding Savant, Replay,
  clip-worker, or media-worker.
- A 60-route authoritative pressure run completed with fallback count 0 and
  unchanged `watchlist_hit` evidence semantics.
- Final rerun `pressure60_qdrant_final_8fps_20260629T150103Z` kept the same
  contract after old pressure residue cleanup: retained evidence 50/50,
  8090 evidence 50/50 OK, fallback 0, shadow mismatch 0, and Qdrant outbox
  active 0.

### Stage 5 - Operational Hardening

Goal:

- make Qdrant rebuildable, observable, and safe to operate.

Actions:

- add pressure report fields:
  - Qdrant health;
  - collection name and vector count;
  - outbox pending/failed counts;
  - outbox lag p95/p99;
  - Qdrant query p50/p95/p99;
  - fallback count;
  - shadow mismatch count.
- add backup/runbook:
  - Qdrant snapshots for fast restore;
  - PostgreSQL rebuild command as the authoritative disaster recovery path;
  - collection alias swap procedure for blue/green rebuilds.
- add alert thresholds:
  - Qdrant unhealthy;
  - outbox pending exceeds SLA;
  - fallback count > 0 during cutover window;
  - shadow mismatch > 0;
  - Qdrant query p99 above SLA.

Acceptance:

```text
PASS_QDRANT_SPEC5_OPERABILITY_BACKUP
```

Pass criteria:

- Qdrant can be rebuilt from PostgreSQL without losing canonical data.
- Snapshot creation and restore are documented and tested once.
- Pressure report includes Qdrant metrics or explicit `not_available` fields.
- The operator can tell whether the active backend is pgvector, shadow, or
  Qdrant.

2026-06-29 operational checkpoint:

- Pressure reports include Qdrant query p50/p95/p99, exact rerank p50/p95/p99,
  fallback count, shadow mismatch count, and outbox active/age summary.
- `sync_qdrant_gallery.py --mode status` reports collection status, PostgreSQL
  active gallery count, Qdrant points, update queue, payload indexes, and outbox
  counts.
- `qdrant-sync-worker` now runs the sync loop independently from `face-worker`.
  The sync loop reclaims stale `processing` outbox rows after timeout and
  verifies that alias `face_gallery_current` points to
  `face_gallery_adaface_512_v1`.
- `QDRANT_BATCH_QUERY_ENABLED=false` keeps batch query disabled by default.
  The adapter-level `search_gallery_batch()` scaffold exists for the later
  `_process_batch()` optimization without changing current runtime semantics.
- Snapshot create/restore is still a later hardening task; PostgreSQL rebuild
  remains the authoritative recovery path for this cutover.

### Stage 6 - Full Pressure Acceptance

Goal:

- prove the Qdrant path under the selected midterm production profile.

Actions:

- run the selected pressure profile with Qdrant authoritative;
- run at least one drain period after input stops;
- retain watchlist evidence samples;
- capture Qdrant, Redis, PostgreSQL, face-worker, event-worker, clip-worker,
  media-worker, and 8090 proof.

Acceptance:

```text
PASS_QDRANT_SPEC6_PRESSURE_ACCEPTED
```

Default SLA:

- `security.face_observations` pending returns to 0 after drain;
- Qdrant query p95 <= 50 ms and p99 <= 150 ms for the selected gallery size;
- exact rerank total p95 <= 100 ms and p99 <= 250 ms when enabled;
- outbox p95 sync lag <= 5 seconds;
- fallback-to-pgvector count is 0 in the final authoritative run;
- no duplicate `watchlist_hit` rows for the same
  `(source_observation_id, person_id, rule_id)`;
- retained watchlist evidence is visible in 8090 list/detail.

2026-06-29 pressure acceptance:

- Report:
  `/data/video-analytics/artifacts/pressure60_qdrant_final_8fps_20260629T150103Z/report.json`
- 60 routes, 8 FPS, single-GPU dual shard, batch size 4, 300s run plus 600s
  drain.
- Status `passed`; failure reasons `[]`; warning only
  `validate_seq_iq_expected_sampling_gap`.
- Qdrant query count 3283, p50 2ms, p95 4ms, p99 5ms, max 9ms.
- Exact rerank count 3283, p50 1ms, p95 2ms, p99 3ms, max 8ms.
- Face-worker gallery query p50 3ms, p95 6ms, p99 8ms, max 12ms.
- Fallback count 0; shadow mismatch 0; watchlist hits emitted 630; watchlist
  emit failed 0.
- `security.face_observations` pending returned to 0.
- 8090 retained evidence proof was 50/50 with database index source.
- Cleanup retained 50 playable bundles: 30 `watchlist_hit`, 20 `intrusion`.
- Evidence generation latency was explicitly measured: media finalization
  p95/p99 8.141s/8.635s, lifecycle p95/p99 196.192s/221.247s. The remaining
  evidence long tail is before finalizer start, not Qdrant lookup.

2026-06-29 gallery-scale acceptance:

- Report:
  `/data/video-analytics/artifacts/qdrant_scale/qdrant_gallery_scale_5000x4_rerun_20260629T150018Z.json`
- Synthetic gallery: 5000 persons x 4 images = 20,000 active vectors.
- Temporary collection used gRPC, HNSW indexed all 20,000 vectors, and was
  deleted after the benchmark.
- All-search latency: p50 1.981ms, p95 4.275ms, p99 6.801ms, max 7.833ms.
- Target-filtered latency remained under p95 1.757ms for target sizes 2, 20,
  and 200.
- Correctness acceptance passed: top1 self hit rate 1.0, top1 person hit rate
  1.0, missing self count 0 for every target size.
- Benchmark acceptance `max_p95_ms=50`, `max_p99_ms=150` passed.

## 8. Rollback Plan

Fast rollback:

```text
FACE_VECTOR_BACKEND=pgvector
docker compose ... restart face-worker
```

No PostgreSQL rollback is required because:

- gallery vectors remain in `person_gallery_embeddings`;
- pgvector search code remains available;
- Qdrant is a derived index;
- Qdrant outbox rows can remain pending until the Qdrant service is restored.

If Qdrant contains bad data:

1. switch runtime back to pgvector;
2. delete or recreate `face_gallery_adaface_512_v1`;
3. rebuild from PostgreSQL;
4. run shadow parity again;
5. switch alias back only after parity passes.

## 9. Test Plan

Unit tests:

- Qdrant payload filter generation for target `person_id` lists;
- Qdrant score to `similarity` and `distance` mapping;
- threshold boundary equality between pgvector exact and Qdrant exact-reranked
  results;
- gallery result dict compatibility with `build_watchlist_hit_event()`;
- exact rerank threshold behavior;
- hybrid routing for small target, broad target, and empty target lists;
- fallback-to-pgvector on Qdrant timeout;
- no fallback when fallback is disabled;
- outbox upsert/delete row creation.
- outbox ordering for upsert/delete/upsert sequences on the same gallery id.

Static tests:

- compose contains Qdrant only behind an explicit profile or documented runtime
  path;
- `FACE_VECTOR_BACKEND` default remains pgvector until cutover;
- no `latest` Qdrant image in production compose;
- empty `QDRANT_API_KEY` behavior is covered by compose rendering or an explicit
  unauthenticated dev override;
- offline packaging includes Qdrant only when `--include-images` is requested;
- no destructive DDL in migration 021;
- `events.source_event_id` uniqueness remains unchanged.

Integration tests:

- bootstrap real PostgreSQL gallery rows into a local Qdrant service;
- compare Qdrant topK against pgvector exact for deterministic vectors;
- deactivate a gallery row and verify it is no longer returned;
- delete a Qdrant point and verify reconcile restores it from PostgreSQL;
- register a new face and verify sync lag is within SLA;
- run face-worker in shadow mode and verify event output still comes from
  pgvector.

Pressure tests:

- current small-gallery smoke;
- synthetic representative gallery pressure;
- live selected production pressure with retained watchlist evidence.

## 10. Implementation File List

Likely files to add or modify:

```text
db/migrations/021_qdrant_gallery_sync_outbox.sql
infra/docker-compose.midterm.yml
infra/env/midterm.env
scripts/midterm_package_clean.sh
scripts/midterm_deploy_clean.sh
services/face-worker/requirements.txt
services/face-worker/app/config.py
services/face-worker/app/gallery_search.py
services/face-worker/app/qdrant_gallery_store.py
services/face-worker/app/gallery_sync_outbox.py
services/face-worker/app/worker.py
services/face-worker/sync_qdrant_gallery.py
services/face-worker/benchmark_qdrant_gallery_scale.py
scripts/runtime/run_midterm_pressure60.py
libs/face_registration/gallery_repository.py
libs/face_registration/image_face_registration.py
services/api/app/repositories/maintenance.py
harness/tests/test_qdrant_gallery_store.py
harness/tests/test_qdrant_gallery_sync_outbox.py
harness/tests/test_face_worker_qdrant_backend_static.py
harness/tests/test_midterm_deployment_contract.py
```

`services/face-worker/app/vector_store.py` should stay in place as the pgvector
adapter and rollback path.

## 11. Final Acceptance

The Qdrant migration is complete only when all of these are true:

- Qdrant is the authoritative online gallery search backend.
- PostgreSQL remains the source of truth for people and gallery rows.
- Qdrant can be rebuilt from PostgreSQL.
- Shadow parity has no unexplained watchlist decision mismatch.
- The final pressure run has Qdrant p95/p99, outbox lag, fallback count, and
  Redis pending metrics.
- Final authoritative run has fallback count 0.
- `watchlist_hit` payload semantics are unchanged.
- 8090 evidence list/detail can query retained watchlist evidence.
- Rollback to pgvector has been tested once.

Final token:

```text
PASS_FACE_GALLERY_QDRANT_CUTOVER
```

2026-06-29 status:

- Qdrant authoritative runtime and 60-route pressure acceptance are complete,
  including final rerun `pressure60_qdrant_final_8fps_20260629T150103Z`.
- PostgreSQL remains source of truth and Qdrant rebuild from PostgreSQL is
  proven through bootstrap/reconcile/status.
- Outbox insert/update/delete smoke is proven: a temporary gallery row synced to
  Qdrant, Qdrant+exact top1 matched pgvector exact top1, update drained, delete
  drained, and the Qdrant point count after delete was 0.
- Live rollback was tested by restarting only `face-worker` with
  `FACE_VECTOR_BACKEND=pgvector`, observing `backend=pgvector`, then restarting
  only `face-worker` back to `FACE_VECTOR_BACKEND=qdrant` with fallback
  disabled and observing `backend=qdrant`. No rebuild, Savant, Replay,
  clip-worker, or media-worker restart was needed.
- Snapshot create/restore and 50k/100k gallery benchmarks remain later
  hardening, not blockers for the current 5000-person cutover.

## 12. Recommended First Slice

Implement this in the smallest safe slice:

0. Capture the Stage 0 baseline first and decide the initial hybrid routing
   threshold.
1. Add Qdrant service, config, and `qdrant-client`, but keep
   `FACE_VECTOR_BACKEND=pgvector`.
2. Add collection bootstrap, transactional outbox, and reconcile script.
3. Add Qdrant adapter, exact rerank, hybrid routing, and shadow mode.
4. Run parity against pgvector exact and verify threshold/score mapping.
5. Only then make Qdrant authoritative for a canary run.
6. Keep Spec 27 persistence/matching decoupling queued if the baseline or
   pressure run proves Qdrant search is not the dominant remaining cost.

This gives the performance benefit without mixing vector-store replacement with
Savant, event-worker, clip-worker, media-worker, or C++ rewrites.

## 12.1 Relationship To Later Optimizations

This spec is the first face-worker scale step, not the whole face-worker
optimization program.

Required ordering:

1. Complete Qdrant registered-gallery cutover for `person_gallery_embeddings`.
2. Re-run pressure with Qdrant authoritative and exact rerank/fallback metrics.
3. Decide whether the remaining bottleneck is still vector lookup or the
   synchronous face-worker chain.
4. Only then start Spec 27 persistence/matching decoupling.
5. Only after decoupling, add multiple matcher workers if the match queue needs
   horizontal scale.
6. Keep historical `face_observations` Qdrant indexing out of this cutover.

Meaning of deferred items:

- `face-worker` split means separating observation persistence from matching so
  a slow gallery query cannot delay `security.face_observations` ACK.
- Multiple matcher workers means scaling the post-split match consumers on
  `security.face_match_requests`; it is not useful before that queue exists.
- Historical observation indexing means indexing `face_observations` for
  live-search or forensic history search. It is much larger than the registered
  gallery and requires its own retention, delete, camera/time filter, and
  consistency design.

Do not implement these deferred items inside the first Qdrant PR unless Stage 0
baseline proves Qdrant gallery lookup is not a meaningful cost and the user
explicitly retargets the work.

## 13. Source Notes

This plan used current Qdrant primary documentation for:

- Docker quickstart and default ports:
  `https://qdrant.tech/documentation/quickstart/`
- collections, vector size, and cosine distance:
  `https://qdrant.tech/documentation/concepts/collections/`
- points, upsert, and point ids:
  `https://qdrant.tech/documentation/concepts/points/`
- filtering and `match any` target filters:
  `https://qdrant.tech/documentation/concepts/filtering/`
- payload indexes and filterable HNSW:
  `https://qdrant.tech/documentation/concepts/indexing/`
- query API, `hnsw_ef`, batch query, and `score_threshold`:
  `https://qdrant.tech/documentation/concepts/search/`
- monitoring, `/metrics`, `/healthz`, `/livez`, and `/readyz`:
  `https://qdrant.tech/documentation/guides/monitoring/`
- security and `QDRANT__SERVICE__API_KEY`:
  `https://qdrant.tech/documentation/guides/security/`
- snapshots and restore:
  `https://qdrant.tech/documentation/concepts/snapshots/`
- current pinned release:
  `https://github.com/qdrant/qdrant/releases/tag/v1.18.2`
