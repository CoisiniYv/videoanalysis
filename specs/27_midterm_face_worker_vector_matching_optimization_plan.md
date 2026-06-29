# 27_midterm_face_worker_vector_matching_optimization_plan.md

Date: 2026-06-29

## 1. Purpose

This spec designs the optimization and acceptance plan for the current
`face-worker` bottleneck.

2026-06-29 decision update:

- The selected online gallery-search direction is now Qdrant, not a pgvector ANN
  index first.
- `specs/28_midterm_qdrant_face_gallery_migration_plan.md` is the detailed
  replacement plan for the gallery vector serving layer and has reached
  authoritative runtime acceptance for the current 5000-person scale gate.
- This spec remains valid for the surrounding work: baseline measurement,
  face-worker observability, persistence/matching decoupling, match-worker
  scaling, and the native-rewrite decision gate.

Current runtime relationship:

```text
Savant AdaFace / face_observation_exporter
  -> Redis Stream security.face_observations
  -> face-worker
       -> validate embedding
       -> persist face_observations
       -> search Qdrant face_gallery_current
       -> exact rerank from PostgreSQL person_gallery_embeddings
       -> publish watchlist_hit to Redis Stream security.events
  -> event-worker
       -> persist events
       -> create evidence_tasks
       -> publish record requests
```

So `face-worker` is a Savant-outside sidecar service, not part of the Savant
GPU inference hot path. It does not call `event-worker` directly; it publishes
unified security events back to Redis and `event-worker` consumes them.

## 2. Current Finding

The current bottleneck is not proven to be Python execution speed. The current
code path is synchronous and DB/query heavy:

- `services/face-worker/app/worker.py` reads
  `security.face_observations` through a Redis consumer group.
- `_process_batch()` validates each embedding, inserts it into PostgreSQL, and
  then synchronously calls `WatchlistMatchEmitter.emit_for_observation()` for
  newly inserted rows.
- `WatchlistMatchEmitter` resolves camera/env watchlist targets, calls
  `GallerySearchBackend.search_gallery()`, and publishes `watchlist_hit` events
  to `security.events`.
- The current authoritative online gallery path is Qdrant candidate search plus
  PostgreSQL exact rerank. `FaceVectorStore.search_gallery()` remains as the
  pgvector rollback/exact baseline path.
- `db/migrations/005_face_observations.sql` and
  `db/migrations/006_gallery_schema.sql` still define `vector(512)` columns.
  Historical `face_observations` similarity search is not migrated to Qdrant in
  this cutover.

Important nuance:

- Current watchlist matching usually resolves a small `person_ids` target set
  from env or camera rules. If the target list is only a few people, exact
  search over the filtered gallery rows may be cheaper and more correct than
  ANN.
- Current 20,000-vector Qdrant benchmark covers the near-term “数千人员、每人几张图”
  registered-gallery workload. If active embeddings grow to 50k/100k, rerun the
  Qdrant scale benchmark before assuming the same latency.

## 3. Decision: Do Not Rewrite To C++ First

Do not rewrite `face-worker` to C++ as the first optimization.

Rationale:

- The expensive work is likely PostgreSQL/pgvector lookup, synchronous coupling,
  Redis/DB I/O, and single-consumer structure.
- Rewriting the same synchronous DB/query path in C++ would keep most of the
  bottleneck and increase deployment complexity.
- Moving matching into Savant would put gallery lookup and event emission near
  the inference hot path, making GPU pipeline latency and restart behavior
  worse.
- The current sidecar design is operationally useful: gallery matching can be
  scaled, restarted, and profiled independently of Savant.

C++ or Rust becomes reasonable only after the database/index/decoupling work is
done and profiling proves Python CPU time is the remaining dominant bottleneck.
See Spec 5 for the native-rewrite decision gate.

## 4. Optimization Strategy

Optimize in this order after the Qdrant decision:

1. Measure the real bottleneck.
2. Add stable observability.
3. Keep Qdrant as the derived gallery vector-serving layer while PostgreSQL
   remains source of truth.
4. Decouple observation persistence from watchlist matching if ACK/pending
   still needs it.
5. Scale matching workers.
6. Consider native implementation only if profiling still requires it.

2026-06-29 continuous optimization rule:

- Qdrant gallery search is now complete for the current 5000-person scale gate.
  The next implementation stream should not keep widening vector-store scope.
- The `face-worker` split is the next candidate only if current Qdrant search
  leaves `security.face_observations` ACK latency or pending dominated by the
  synchronous chain: PostgreSQL insert, rule resolution, exact rerank, event
  publish, or per-message blocking.
- Multiple `face-match-worker` consumers are allowed only after the split creates
  a separate `security.face_match_requests` queue and idempotency proof.
- A Qdrant historical `face_observations` index is a separate product/search
  workstream. It is not part of watchlist gallery cutover and should be opened
  only when live-search or forensic historical face search becomes the target
  workload.

Stop/go metrics for each step:

- Qdrant gallery cutover gate: complete for current scale. Final 60-route
  8 FPS rerun had Qdrant p95/p99 4ms/5ms, exact rerank p95/p99 2ms/3ms,
  fallback count 0; 20,000-vector gRPC benchmark all-search p95/p99 was
  4.275ms/6.801ms with top1 self/person hit rate 1.0.
- Worker split gate: observation ACK p95/p99 or Redis pending remains high after
  Qdrant, while Qdrant query latency is already within SLA.
- Multi-matcher gate: `security.face_match_requests` pending grows or drains too
  slowly with one matcher, while duplicate-event idempotency is already proven.
- Historical observation-index gate: a measured API/operator workflow requires
  broad similarity search over `face_observations`, not just registered gallery
  matching.

## 5. Execution Plan

### Spec 0 - Baseline And Workload Definition

Problem:

The current report says `face-worker` is a risk, but does not yet rank its
sub-costs: Redis read, JSON parse, embedding validation, DB insert, rule
resolution, Qdrant query, exact rerank, event publish, or ACK behavior.

Required baseline artifact:

- enabled source count;
- configured FPS, face intervals, and `FACE_REID_MIN_INTERVAL_MS`;
- `security.face_observations` XLEN and XPENDING summary;
- face-worker CPU/memory;
- observations consumed per second;
- insert p50/p95/p99;
- gallery query p50/p95/p99;
- watchlist events emitted per second;
- active persons count;
- active gallery embeddings count;
- watchlist target count per rule;
- representative `EXPLAIN (ANALYZE, BUFFERS)` for:
  - target-filtered watchlist query;
  - broad gallery search query;
  - face observation similarity search if used by search APIs.

Acceptance:

```text
PASS_FACE_WORKER_SPEC0_BASELINE_CAPTURED
```

Pass criteria:

- A baseline report exists under `docs/`.
- It includes query plans and cardinality numbers, not just CPU guesses.
- It states whether the current profile is target-filtered or broad search.
- It states whether observed lag is Redis pending, DB insert, gallery query, or
  event publish dominated.

### Spec 1 - Face Worker Observability Contract

Problem:

Current code logs `gallery_query_duration_ms`, but there is no stable pressure
schema that reports face-worker lag and p95/p99 across runs.

Modification direction:

- Extend pressure artifacts to record:
  - `security.face_observations` XLEN;
  - XPENDING for `face-worker-midterm`;
  - face-worker consumed/inserted/duplicate/invalid/failed counts;
  - watchlist query p50/p95/p99;
  - watchlist events emitted;
  - target person count;
  - gallery embedding count;
  - PostgreSQL query-plan snapshot or `pg_stat_statements` deltas when
    available.
- Start with structured logs or a pressure parser if that is fastest.
- Add a metrics endpoint only if logs are not reliable enough for p95/p99.

Target effect:

- Every later optimization can prove it improved the intended metric.
- A passing inference/evidence pressure run cannot hide a face-worker backlog.

Acceptance:

```text
PASS_FACE_WORKER_SPEC1_OBSERVABILITY
```

Pass criteria:

- Targeted tests prove the report parser/schema recognizes face-worker fields.
- A pressure report includes face-worker p95/p99 and pending/lag.
- Missing metrics are reported as `not_available`, not silently omitted.

### Spec 2 - Query Plan And Index Closure

Problem:

`person_gallery_embeddings` and `face_observations` contain `vector(512)`
columns. The registered-gallery online path now uses Qdrant, while historical
`face_observations` similarity search still remains in PostgreSQL/pgvector.

2026-06-29 replacement direction:

- For online registered-gallery watchlist matching, the Qdrant migration in
  `specs/28_midterm_qdrant_face_gallery_migration_plan.md` is complete for the
  current scale gate.
- Keep PostgreSQL/pgvector as the exact baseline, rollback path, and optional
  exact-rerank source for Qdrant candidates.
- Do not add pgvector ANN indexes for the registered-gallery hot path unless
  Qdrant is explicitly rolled back or rejected.
- Historical `face_observations` similarity search can still use pgvector until
  a separate Qdrant observation-index design exists.

Legacy pgvector direction if Qdrant is explicitly rejected later:

- Keep the exact target-filtered watchlist query path when target cardinality is
  small and measured plans show it is faster.
- Add ANN indexes only where plans prove they help. Candidate migration:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS
    idx_person_gallery_embedding_hnsw_cosine_active
ON person_gallery_embeddings
USING hnsw (embedding vector_cosine_ops)
WHERE is_active = true;
```

- For broad face observation search, consider:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS
    idx_face_observations_embedding_hnsw_cosine
ON face_observations
USING hnsw (embedding vector_cosine_ops);
```

- If ANN is used before thresholding, keep threshold semantics explicit:
  - candidate retrieval may be approximate;
  - final event emission should exact-rerank candidate rows before publishing
    `watchlist_hit`, unless recall loss is explicitly accepted.
- Apply vector index migrations outside active pressure windows because
  `CREATE INDEX CONCURRENTLY` is still resource-heavy.

Target effect:

- Gallery size growth does not linearly increase watchlist latency for the
  selected production gallery size.
- Threshold semantics remain stable.

Current acceptance evidence:

- 60-route 8 FPS Qdrant authoritative pressure passed with fallback count 0 and
  retained evidence 50/50.
- Qdrant query p95/p99 was 4ms/5ms; exact rerank p95/p99 was 2ms/3ms.
- 5000 persons x 4 images, or 20,000 active vectors, gRPC benchmark all-search
  p95/p99 was 4.275ms/6.801ms with top1 self/person hit rate 1.0.

Acceptance:

```text
PASS_FACE_WORKER_SPEC2_VECTOR_QUERY_SCALE
```

Pass criteria:

- Qdrant bootstrap/reconcile proves active Qdrant points match active
  PostgreSQL gallery rows.
- Qdrant shadow mode compares against the current exact pgvector baseline for
  representative gallery sizes.
- Static tests verify Qdrant is additive and the default backend remains
  rollback-safe until cutover.
- If Qdrant is explicitly rejected and pgvector ANN is revived, migration/static
  tests must verify additive concurrent indexes, no destructive DDL, and the
  expected pgvector operator class.
- For broad or high-cardinality target search, the selected backend improves
  gallery query p95 by at least 50% or meets the selected SLA.
- Watchlist hit event count and matched person identities match the exact
  baseline on the acceptance dataset.

Default SLA for the midterm pressure profile:

- target-filtered Qdrant gallery query p95 <= 50 ms;
- target-filtered Qdrant gallery query p99 <= 150 ms;
- exact-rerank total p95 <= 100 ms when enabled;
- zero false `watchlist_hit` events compared with exact rerank baseline;
- no duplicate `source_event_id` rows in `events`.

### Spec 3 - Decouple Persistence From Matching

Problem:

One face observation currently pays the full cost of DB insert plus watchlist
matching before the batch loop moves on. A slow gallery query can delay Redis
ACK/recovery for unrelated observations.

Modification direction:

- Split `face-worker` into two logical roles. They may initially share the same
  Python image:
  - `face-observation-worker`: consume `security.face_observations`, validate,
    insert `face_observations`, enqueue a match request, then ACK.
  - `face-match-worker`: consume match requests, resolve watchlist rules, query
    gallery, publish `watchlist_hit` to `security.events`.
- Prefer an idempotent request stream:

```text
security.face_match_requests
```

- Use an idempotency key:

```text
source_observation_id
```

- Request payload should include enough routing information to avoid rereading
  large JSON unnecessarily, but the DB row remains the durable truth.
- On duplicate observation insert, do not emit duplicate match requests unless
  the previous request is known missing or failed.
- Keep event idempotency:

```text
watchlist_hit:{source_observation_id}:{person_id}
```

The `events.source_event_id` unique constraint and event-worker
`ON CONFLICT (source_event_id) DO NOTHING` provide a second protection layer.

Target effect:

- Observation ingestion stays bounded even when gallery matching is slow.
- Matching can be scaled independently.
- Redis pending for `security.face_observations` no longer grows because of
  gallery query latency.

Acceptance:

```text
PASS_FACE_WORKER_SPEC3_MATCHING_DECOUPLED
```

Pass criteria:

- Unit tests cover:
  - insert succeeds + match request published + original message ACKed;
  - insert fails + original message not ACKed;
  - match request publish fails + original message not ACKed or is safely
    retried;
  - duplicate observation does not create duplicate match events;
  - match worker publish failure leaves match request pending.
- Integration or mocked Redis tests prove consumer groups are separate:
  - `face-worker-midterm` for observations;
  - `face-match-workers-midterm` for match requests.
- Under a synthetic slow-gallery-query test, observation insert lag remains
  bounded while match queue backlog grows visibly.
- Existing `watchlist_hit` payload schema and evidence policy remain unchanged.

### Spec 4 - Match Worker Scaling And Backpressure

Problem:

After decoupling, the match side can still fall behind if every observation
requires synchronous gallery search.

Modification direction:

- Allow multiple `face-match-worker` consumers.
- Use unique consumer names per container.
- Cache watchlist rule and target person resolution for the configured refresh
  interval.
- Add per-source and per-rule queue/lag metrics.
- Preserve correctness before adding lossy throttling.
- Only if pressure requires it, add policy controls:
  - minimum face quality;
  - per-track match cooldown;
  - max match backlog before low-quality observations are marked skipped;
  - priority for explicit watchlist targets over broad live search.

Target effect:

- Matching throughput scales horizontally without duplicate events.
- Backpressure is explicit and observable.

Acceptance:

```text
PASS_FACE_WORKER_SPEC4_MATCH_WORKER_SCALE
```

Pass criteria:

- Two or more match workers can run concurrently without duplicate
  `watchlist_hit` source events.
- `security.face_match_requests` pending drains to zero after the pressure
  window.
- Match worker p95 stays within SLA for the selected gallery size.
- If low-quality skipping is enabled, skipped counts and reasons appear in the
  pressure report.

Default end-to-end SLA:

- `security.face_observations` pending returns to 0 after drain;
- `security.face_match_requests` pending returns to 0 after drain;
- observation ingest p95 <= 50 ms excluding Redis blocking wait;
- gallery query p95 <= 100 ms for the accepted gallery size;
- watchlist event publish p95 <= 50 ms;
- no duplicate `watchlist_hit` for the same
  `(source_observation_id, person_id, rule_id)`;
- retained watchlist evidence remains queryable through 8090.

### Spec 5 - Native Rewrite Decision Gate

Problem:

C++/Rust may be useful eventually, but rewriting too early can hide the actual
DB/query bottleneck and increase operational risk.

Do not start a native rewrite unless all are true:

- Spec 1 through Spec 4 are complete or explicitly ruled out.
- The selected vector backend, now Qdrant unless explicitly reversed, is already
  within SLA.
- Redis pending still grows under the target pressure profile.
- Profiling shows Python CPU/serialization/validation/event construction is
  the dominant remaining cost.
- The new implementation can keep the same Redis, PostgreSQL, and event payload
  contracts.

Possible native paths:

1. Keep PostgreSQL as vector store, rewrite only the consumer/matcher runtime.
   - Lower risk, but limited benefit if DB dominates.
2. Add an in-memory ANN gallery cache with C++/Rust/FAISS/HNSWlib.
   - Higher performance potential, but requires gallery cache invalidation,
     exact rerank, reload semantics, and consistency tests.

Non-goal:

- Do not move watchlist DB lookup directly into the Savant pipeline unless a
  separate design proves it will not harm inference latency or model stability.

Acceptance:

```text
PASS_FACE_WORKER_SPEC5_NATIVE_DECISION
```

Pass criteria:

- A profiling report proves Python runtime is the bottleneck after DB/index and
  decoupling work.
- A compatibility test suite proves byte-for-byte or schema-equivalent
  `watchlist_hit` payloads.
- Rollback path keeps the Python worker available.

## 6. Final Acceptance

The face-worker optimization is complete only when the selected production
profile satisfies all of the following:

- `security.face_observations` pending does not grow without bound.
- Face observation insert p95 and p99 are reported.
- Gallery query p95 and p99 are reported.
- Active gallery/person cardinality is reported.
- Watchlist target cardinality is reported.
- Query plans for target-filtered and broad search are captured.
- `watchlist_hit` payload semantics are unchanged.
- `events.source_event_id` uniqueness prevents duplicate event rows.
- 8090 evidence list/detail can query retained watchlist evidence after drain.
- The pressure report includes all face-worker metrics or marks missing fields
  explicitly.

Final token:

```text
PASS_FACE_WORKER_VECTOR_MATCHING_SCALE
```

## 7. Recommended First Implementation Slice

The first Qdrant implementation slice is complete. The next slice should be
strictly about cost attribution and, only if needed, persistence/matching
decoupling:

0. Under true RTSP or the next pressure run, capture DB insert latency,
   rule-resolution latency, exact-rerank latency, event-publish latency, Redis
   pending, and end-to-end ACK latency.
1. If Qdrant query remains within SLA but ACK/pending is high, add
   `security.face_match_requests` and split observation persistence from
   matching.
2. Preserve `watchlist_hit` payload, `events.source_event_id`, evidence policy,
   and 8090 evidence semantics.
3. Add multiple matcher consumers only after the split and idempotency proof.
