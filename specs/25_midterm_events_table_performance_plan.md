# 25_midterm_events_table_performance_plan.md

Date: 2026-06-28

## 0. Implementation Result - 2026-06-28

Implemented and locally applied:

- `db/migrations/018_events_table_performance_indexes.sql` for the P0 `events`
  query shapes in this plan;
- `db/migrations/019_media_worker_events_queue_indexes.sql` for the
  media-worker evidence queue scans discovered during post-P0 sampling.

Read-only validation after applying 018 showed the four P0 query shapes changed
from `Seq Scan` / `Parallel Seq Scan` to `Index Scan` / `Index Only Scan`.
The real 8090 `events` API queries kept the same response shape; the remaining
camera-name `OR` join no longer forced an `events` table scan.

The first 5-minute `pg_stat_user_tables.events` sample still showed background
scan growth:

```text
seq_scan_delta: 159
seq_tup_read_delta: 10,567,458
```

Follow-up EXPLAIN checks traced that residual scan load to media-worker periodic
queue queries over `events`. Migration 019 adds narrower partial indexes for
those queue predicates. After applying 019, a second 5-minute sample showed:

```text
seq_scan_delta: 0
seq_tup_read_delta: 0
idx_scan_delta: 174
idx_tup_fetch_delta: 223,996
```

Validation commands run:

```text
pytest -q harness/tests/test_events_table_performance_indexes_static.py \
  harness/tests/test_midterm_worker_indexes_static.py \
  harness/tests/test_event_repository.py \
  harness/tests/test_alert_policy_scoped_cooldown.py \
  harness/tests/test_evidence_viewer_database_index.py
PYTHONPYCACHEPREFIX=/tmp/video-analytics-pycache python -m py_compile \
  services/api/app/repositories/events.py \
  services/event-worker/app/repository.py \
  services/api/app/services/runtime_overview.py \
  services/media-worker/app/worker.py
git diff --check
```

## 1. Purpose

This plan fixes the confirmed `events` table performance bottleneck without
expanding into unrelated pipeline work.

The next implementation goal should only cover:

- `events` table query shapes;
- indexes required by those query shapes;
- `event-worker` / API reads that hit `events`;
- read-only planner/statistics validation.

This is intentionally narrow. It is not a face-recognition, pgvector, evidence
materialization, Savant batch, runtime topology, or 60-stream pressure-test plan.

## 2. Current Finding

The 2026-06-28 read-only PostgreSQL inspection found:

```text
events live rows: about 68,000
events total size: about 326 MB
events seq_scan: about 190,000
events seq_tup_read: about 11.2 billion
```

The table is not large enough to explain the pressure by size alone. The issue
is repeated inefficient query plans.

The same inspection found no current connection pile-up:

```text
active DB sessions: low
deadlocks: 0
5-second events seq_scan delta during the check: 0
```

So the problem is a confirmed latent performance risk, not a currently observed
runaway lock or blocking incident.

## 3. Confirmed Bad Query Shapes

### 3.1 Recent events list

Current API path:

```text
GET /api/v1/events/recent
```

Repository path:

```text
services/api/app/repositories/events.py
EventRepository.list_recent()
```

Query shape:

```sql
SELECT e.*, c.name AS camera_name
FROM events e
LEFT JOIN cameras c
  ON c.id::text = e.camera_id
  OR c.source_id = e.source_id
ORDER BY e.created_at DESC
LIMIT $1;
```

Observed planner issue:

```text
Parallel Seq Scan on events
Sort by e.created_at DESC
```

Primary cause:

- no `events(created_at DESC)` index;
- camera join uses an `OR` predicate.

### 3.2 Event type filtered list

Current API path:

```text
GET /api/v1/events?event_type=...
```

Repository path:

```text
services/api/app/repositories/events.py
EventRepository.list_events()
```

Query shape:

```sql
WHERE e.event_type = $1
ORDER BY e.created_at DESC
LIMIT $2 OFFSET $3;
```

Observed planner issue:

```text
Parallel Seq Scan on events
Sort by e.created_at DESC
```

Primary cause:

- existing `idx_events_event_type` is a single-column index;
- it does not satisfy `event_type + created_at DESC` pagination.

### 3.3 Source/type recent existence check

Repository path:

```text
services/event-worker/app/repository.py
has_event_type_since_ts_ms()
```

Query shape:

```sql
SELECT 1
FROM events
WHERE source_id = $1
  AND event_type = $2
  AND created_at >= to_timestamp($3 / 1000.0)
  AND COALESCE(status, 'new') <> 'suppressed'
LIMIT 1;
```

Observed planner issue:

```text
Seq Scan on events
```

Primary cause:

- no composite index for `source_id + event_type + created_at`;
- the unsuppressed predicate is repeated but not indexed.

### 3.4 Cooldown latest-event lookup

Repository path:

```text
services/event-worker/app/repository.py
latest non-suppressed event lookup for cooldown
```

Query shape:

```sql
WHERE camera_id = $1
  AND source_event_id <> $2
  AND COALESCE(status, 'new') <> 'suppressed'
  AND event_ts_ms <= $3
  AND event_type = $4
ORDER BY event_ts_ms DESC
LIMIT 1;
```

Current planner can use `camera_id`, but a dedicated composite partial index is
still the correct production shape for high event volume.

### 3.5 Runtime overview evidence state summary

Current service path:

```text
services/api/app/services/runtime_overview.py
```

Query shape:

```sql
WHERE created_at >= now() - interval '3 hours'
  AND (
    clip_required = true
    OR snapshot_required = true
    OR payload->'media'->>'clip_required' = 'true'
  )
```

Observed planner issue:

```text
Seq Scan on events
```

Primary cause:

- no index that matches recent evidence-required events;
- JSONB predicate makes the planner fall back to scanning.

## 4. Non-Goals

Do not include these in this plan:

- changing YOLOv8-Face, AdaFace, face-worker matching, or pgvector search;
- adding HNSW / IVFFlat vector indexes;
- changing evidence materialization policy or clip/media workers;
- changing Redis stream writer behavior;
- changing compose topology, runtime apply/restart, FPS, batch, or model paths;
- running 60-stream pressure tests;
- deleting or rewriting historical event rows;
- making 8090 UI changes beyond what is required to avoid pathological event
  queries.

## 5. Phase P0 - Add Targeted Events Indexes

Create a new migration for these indexes.

Important execution rule:

- Use `CREATE INDEX CONCURRENTLY` in live environments.
- `CREATE INDEX CONCURRENTLY` cannot run inside a normal transaction block.
- If the migration runner wraps every migration in a transaction, make this a
  documented operator SQL step or use the repo's non-transaction migration path.
- Do not apply this during an active pressure-test window.

Recommended indexes:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_created_at_desc
ON events (created_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_event_type_created_at_desc
ON events (event_type, created_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_source_type_created_at_desc_unsuppressed
ON events (source_id, event_type, created_at DESC)
WHERE COALESCE(status, 'new') <> 'suppressed';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_camera_type_ts_desc_unsuppressed
ON events (camera_id, event_type, event_ts_ms DESC)
WHERE COALESCE(status, 'new') <> 'suppressed';
```

Expected coverage:

| Index | Covers |
| --- | --- |
| `idx_events_created_at_desc` | recent events list, default event pagination |
| `idx_events_event_type_created_at_desc` | event-type filtered list |
| `idx_events_source_type_created_at_desc_unsuppressed` | `has_event_type_since_ts_ms()` |
| `idx_events_camera_type_ts_desc_unsuppressed` | cooldown latest-event lookup |

## 6. Phase P1 - Keep API Query Changes Minimal

Only adjust `services/api/app/repositories/events.py` if P0 indexes alone do not
produce acceptable plans.

Allowed changes:

- keep `list_recent()` ordered by `e.created_at DESC`;
- keep `list_events()` semantics unchanged;
- avoid widening the selected event columns unless needed by response schemas;
- replace the `EVENTS_WITH_CAMERA_SQL` `OR` join only if planner still chooses a
  poor plan after indexes.

Preferred camera-name approach if the `OR` join remains costly:

1. fetch the event page first using the new `events` index;
2. resolve camera names for the small page by joining or mapping against
   `cameras` after pagination.

This avoids multiplying the `events` scan by a broad `OR` join.

Do not change event response semantics in this phase.

## 7. Phase P2 - Runtime Overview Query Fix

First run `EXPLAIN` after P0. If runtime overview still scans `events`, add a
targeted partial index:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_events_evidence_required_created_at_desc
ON events (created_at DESC)
WHERE clip_required = true
   OR snapshot_required = true
   OR payload->'media'->>'clip_required' = 'true';
```

If the JSONB predicate still prevents reliable planner behavior, stop and record
the finding. The next narrow option is to move the runtime overview evidence
summary to `evidence_bundles` / `evidence_tasks`, but that is a separate
implementation step and should not be mixed into P0.

## 8. Validation Plan

Validation must be read-only unless explicitly applying the index migration in a
maintenance window.

Before applying indexes:

```sql
SELECT relname, n_live_tup, n_dead_tup, seq_scan, seq_tup_read, idx_scan
FROM pg_stat_user_tables
WHERE relname = 'events';
```

Planner checks:

```sql
EXPLAIN
SELECT e.*
FROM events e
ORDER BY e.created_at DESC
LIMIT 50;

EXPLAIN
SELECT e.*
FROM events e
WHERE e.event_type = 'watchlist_hit'
ORDER BY e.created_at DESC
LIMIT 50;

EXPLAIN
SELECT 1
FROM events
WHERE source_id = 'source_00000000-0000-4000-8000-781078565686'
  AND event_type = 'watchlist_hit'
  AND created_at >= now() - interval '1 hour'
  AND COALESCE(status, 'new') <> 'suppressed'
LIMIT 1;

EXPLAIN
SELECT event_ts_ms, event_type, algorithm_type
FROM events
WHERE camera_id = 'source_00000000-0000-4000-8000-781078565686'
  AND source_event_id <> 'dummy'
  AND COALESCE(status, 'new') <> 'suppressed'
  AND event_ts_ms <= 9999999999999
  AND event_type = 'intrusion'
ORDER BY event_ts_ms DESC
LIMIT 1;
```

Short sampling check after applying indexes:

```sql
WITH before AS (
  SELECT seq_scan, seq_tup_read, idx_scan, idx_tup_fetch
  FROM pg_stat_user_tables
  WHERE relname = 'events'
),
wait AS (SELECT pg_sleep(300)),
after AS (
  SELECT seq_scan, seq_tup_read, idx_scan, idx_tup_fetch
  FROM pg_stat_user_tables
  WHERE relname = 'events'
)
SELECT
  after.seq_scan - before.seq_scan AS seq_scan_delta,
  after.seq_tup_read - before.seq_tup_read AS seq_tup_read_delta,
  after.idx_scan - before.idx_scan AS idx_scan_delta,
  after.idx_tup_fetch - before.idx_tup_fetch AS idx_tup_fetch_delta
FROM before, wait, after;
```

Do not run `EXPLAIN ANALYZE` on broad `events` scans during a pressure test.

## 9. Acceptance Criteria

The next goal is complete when:

- the new migration or documented operator SQL exists;
- planner output for the four P0 query shapes no longer shows
  `Seq Scan on events` or `Parallel Seq Scan on events`;
- 8090 event list and recent event queries still return the same response shape;
- event-worker cooldown and `has_event_type_since_ts_ms()` tests still pass;
- `git diff --check` passes;
- no compose/env/runtime topology changes are included;
- no runtime restart or pressure-test interference is required for code review.

Runtime acceptance, to be done outside active pressure testing:

- apply indexes with `CONCURRENTLY`;
- sample `pg_stat_user_tables.events` for at least 5 minutes while 8090 is
  refreshed normally;
- `seq_scan_delta` and `seq_tup_read_delta` should stay near zero for the fixed
  query paths;
- `idx_scan` should grow on the new indexes.

## 10. Suggested Next Goal Command

```text
Implement specs/25_midterm_events_table_performance_plan.md P0-P1 only.
Do not restart services, do not apply runtime config, do not run pressure tests,
and do not touch face/pgvector/evidence materialization. Add the events indexes
as a safe migration/operator SQL path, adjust only the events query code if the
planner still scans, and close with targeted tests plus read-only EXPLAIN
validation.
```
