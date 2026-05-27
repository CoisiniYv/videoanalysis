# Phase F3.6 — Registered Person Trajectory Query Harness

## Summary

F3.6 implements a read-side trajectory query harness. It queries `match_results`
with `search_mode = 'registered_person_history'` for a given registered person,
outputting appearance trajectory summaries (camera, timestamp, similarity,
snapshot).

**Scope**: read-side harness only. Does NOT write to any table. The
`registered_person_history` producer (writing match_results with that search_mode)
remains future work — the match_gallery.py F3.5 harness produces `gallery_match`
rows, not `registered_person_history`.

## Files

| File | Purpose |
|---|---|
| `services/face-worker/app/trajectory_repository.py` | TrajectoryRepository — read-only query for registered person trajectory |
| `services/face-worker/query_trajectory.py` | CLI tool: query trajectory by person-id or external-person-id |
| `harness/tests/test_trajectory_query.py` | Unit tests (CLI args, repository SQL, filters) + integration tests |
| `scripts/smoke/check_f3_6_trajectory_query.sh` | Deterministic seed + query + cleanup smoke script |

## TrajectoryRepository

`get_person_trajectory(person_id, *, time_from_ms, time_to_ms, camera_id, min_similarity, limit)`

- Queries `match_results` with `search_mode = 'registered_person_history'`
- `matched_observation_id IS NOT NULL` — excludes gallery_match rows
- JOINs `persons` for `person_name` / `external_person_id`
- LEFT JOINs `face_observations` for `snapshot_path` / `crop_path` fallback
- COALESCE: prefers `match_results.snapshot_path`, falls back to `face_observations.snapshot_path`
- ORDER BY `matched_timestamp_ms DESC, rank ASC, id DESC`
- Filter support: time range, camera_id, min_similarity, limit

## CLI — query_trajectory.py

```
python query_trajectory.py --person-id 42
python query_trajectory.py --external-person-id EMP-00123
python query_trajectory.py --person-id 42 --time-from 2026-05-01T00:00:00+08:00 --time-to 2026-05-28T23:59:59+08:00
python query_trajectory.py --person-id 42 --camera-id cam-lobby --min-similarity 0.7 --json
```

- `--person-id` and `--external-person-id` are mutually exclusive, one required
- `--time-from` / `--time-to`: ISO 8601 timezone-aware (naive rejected)
- `--camera-id`, `--min-similarity`, `--limit` (default 100)
- `--json` for JSON output; default is human-readable table
- Resolves `--external-person-id` via `PersonRepository.get_by_external_person_id()`
- Read-only: does not modify database

## Tests

### Unit tests (no DB) — 48 tests

- CLI argparse: mutually exclusive, required group, defaults, all options (7 tests)
- ISO 8601 parsing: valid, naive rejected, invalid rejected (4 tests)
- Table formatting: empty, single row, missing fields (3 tests)
- TrajectoryRepository SQL: mocked connection, verify WHERE clauses, JOINs,
  ORDER BY, filter parameters, limit clamping, similarity boundary validation (24 tests)
- Real `query()` runtime: external-person-id resolution, JSON clean output,
  not-found exit, no embedding in JSON, table output person_id (5 tests)
- Real `main()` validation: reversed time range rejected, invalid
  min_similarity rejected, boundary values accepted (5 tests)

### Integration tests (real PostgreSQL) — 11 tests

- Basic trajectory query with seeded `registered_person_history` data
- ORDER BY `matched_timestamp_ms DESC`
- Time range filter
- Camera filter
- Min similarity filter
- Limit
- Empty result for unknown person
- Excludes gallery_match rows (matched_observation_id IS NULL)
- JOIN with persons for person_name
- COALESCE for snapshot_path and crop_path fallback
- External-person-id resolution via real `query()` function

All integration tests use `test:f3_6:` prefixed data and `conn.rollback()`
before cleanup in finally blocks.

## Smoke Script

`scripts/smoke/check_f3_6_trajectory_query.sh`

NOT read-only: seeds deterministic test data and cleans it up via trap.

Seeds:
1. 1 test person with `external_person_id`
2. 1 gallery embedding
3. 3 face_observations (different cameras/timestamps)
4. 3 `registered_person_history` match_results (different similarities)
5. 1 `gallery_match` interference row (matched_observation_id=NULL)

Verifies:
- `--person-id` returns 3 `registered_person_history` rows
- `--external-person-id` returns 3 `registered_person_history` rows
- `gallery_match` row is excluded (total=3, not 4)
- Timestamp DESC ordering (300000 > 200000 > 100000)
- Camera filter (`--camera-id cam-lobby` returns 2 rows)
- Min similarity filter (`--min-similarity 0.80` returns 2 rows)
- `--json` output is valid JSON
- JSON output does not contain embedding vectors

Cleanup (trap-based):
- Removes match_results, person_gallery_embeddings, persons, face_observations
- Verifies zero `test:f3_6:smoke:` leftovers in all 4 tables

## Verification

DB-backed integration and smoke were run in the project host environment:

```
DATABASE_URL=postgresql://video:video@localhost:5438/video_analytics

# Integration (11 tests)
env DATABASE_URL=... pytest harness/tests/test_trajectory_query.py -q -k "integration"
→ 11 passed

# Smoke (10 checks)
bash scripts/smoke/check_f3_6_trajectory_query.sh
→ PASS (all 10 checks)

# Cleanup (4 tables)
docker exec c1-official-postgres psql ... → all 0 leftovers

# Unit/regression (212 tests across 5 suites)
→ 212 passed
```

Note: Codex review shell could not connect to `localhost:5438`, so Codex
could not independently reproduce the DB-backed run. Code inspection and
local project-host run passed.

## What This Phase Does NOT Do

- Does NOT implement the `registered_person_history` producer (writing match_results
  with that search_mode)
- Does NOT implement watchlist_hit or live_search_hit
- Does NOT write to security.events
- Does NOT provide a REST API
- Does NOT implement retention cleanup
