# Phase C2.7 - Event-Worker Persistence / API Event Query MVP

## Result

Expected result marker:

`PASS_C2_7_EVENT_WORKER_PERSISTENCE_API_QUERY_READY`

C2.7 proves that the C2.6R `watchlist_hit` event can be consumed by the
event-worker one-message Redis path, persisted into PostgreSQL `events`, and
queried back through repository and API/TestClient paths with evidence semantics
preserved.

## Input

Input event:

`/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035/redis_watchlist_event.json`

Input evidence bundle:

`/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035`

C2.6R identity/event linkage:

- `source_observation_id=face:c2_post_savant_fps_probe:4:17854:1`
- `person_id=4`
- `external_person_id=test:c2_4:person`
- `gallery_embedding_id=3`
- `match_result_id=6`
- `similarity=1.0`
- `threshold=0.99`
- `watchlist_rule_id=c2_6r_test_watchlist_rule`

## Execution Mode

C2.7 uses Mode A:

`Redis stream event-worker one-message consumer`

Isolated stream:

- input stream: `c2_7.security.events.test`
- consumer group: `c2_7_event_worker_test`

The smoke:

1. Loads the C2.6R `redis_watchlist_event.json`.
2. Rewrites `source_event_id` to a namespaced `c2_7:persisted:watchlist_hit:...`
   value.
3. Adds evidence path semantics:
   - `evidence.bundle_path`
   - `evidence.audit_path`
   - `capture_mode=stable_post_savant_sink_time_crop`
   - `workaround_used=true`
   - `event_style_replay_job_passed=false`
4. XADDs the event to the isolated Redis stream.
5. Reads exactly one message through `services/event-worker/app/redis_consumer.py`.
6. Persists it through `services/event-worker/app/worker.py::_process_batch()`
   and `services/event-worker/app/repository.py::EventRepository.insert_event()`.
7. Replays the same source event id once and verifies idempotency leaves one DB
   row.
8. Queries through the API repository and API TestClient route.
9. Deletes only the isolated C2.7 Redis stream when cleanup is enabled.

It does not consume production `security.events`, does not run the infinite
event-worker loop, and does not delete non-test DB rows.

## Persisted Payload Boundaries

The persisted event must preserve:

- `event_type=watchlist_hit`
- `source_observation_id`
- `person_id`
- `external_person_id`
- `gallery_embedding_id`
- `match_result_id`
- `similarity`
- `threshold`
- `watchlist_rule_id`
- `primary_identity_join_key=source_observation_id`
- `track_id_join_warning=true`
- `evidence_capture_mode=stable_post_savant_sink_time_crop`
- `workaround_used=true`
- `event_style_replay_job_passed=false`

The persisted payload must not contain:

- embedding vectors
- image bytes
- crop bytes
- frame bytes
- base64 image payloads

The event may keep C2.6R non-byte geometry/observation metadata for audit
context, but C2.7 does not use it to reconstruct bbox, pose, landmarks, or
evidence geometry.

## DB / Idempotency

`events.source_event_id` is unique. C2.7 verifies idempotency by processing the
same namespaced C2.7 event twice and checking:

- first processing inserts one row
- second processing is treated as duplicate
- DB row count for the source event id remains one

The namespaced C2.7 DB row may remain as test evidence. Non-test DB rows are not
deleted.

## Runtime Schema Alignment

The C2.7 helper can optionally align an empty legacy `events` table to the
current event-worker contract before the one-message proof. This is intentionally
limited:

- it is only enabled by the C2.7 smoke with `--ensure-event-schema`;
- it refuses to alter `events` if the table already has rows;
- it does not drop event data;
- it creates or aligns only the columns and `evidence_tasks` table required by
  `services/event-worker/app/repository.py`.

This is a C2.7 runtime harness guard, not a replacement for production database
migration management.

## Query Path

Preferred query proof:

- API repository `get_by_source_event_id()`
- FastAPI TestClient route `GET /api/v1/events/{source_event_id}`

Live API HTTP is not required for C2.7. `live_api_runtime_verified` is only true
if a running API service is explicitly checked, which this smoke does not
require.

## Output

Output directory:

`/data/video-analytics/media/evidence/c2_7_event_worker_persistence_YYYYMMDDTHHMMSS`

Files:

- `persisted_watchlist_event.json`
- `queried_event.json`
- `db_event_row.json`
- `repository_response.json`
- `api_response.json`
- `c2_7_event_worker_persistence_summary.json`

## Limitations

- Stable sink workaround remains active:
  `evidence_capture_mode=stable_post_savant_sink_time_crop`.
- Event-style Replay is still not passed.
- This is not a broad recognition accuracy test.
- This is an isolated one-message event-worker proof, not a long-running worker
  soak.
- Live API runtime HTTP is not required if API TestClient verifies the route.

## Verification

Required checks:

```bash
python -m pytest harness/tests/test_c2_7_event_worker_persistence_api_query.py -q
python -m pytest harness/tests/test_c2_6r_redis_consumer_watchlist_event.py -q
python -m pytest harness/tests/test_c2_6_live_watchlist_from_face_worker.py -q
python -m pytest harness/tests/test_c2_5_watchlist_evidence_semantics.py -q
python -m py_compile scripts/tools/build_c2_7_persist_watchlist_event.py
git diff --check
bash -n scripts/smoke/current/check_c2_7_event_worker_persistence_api_query.sh
```

Runtime smoke:

```bash
bash scripts/smoke/current/check_c2_7_event_worker_persistence_api_query.sh
```

## Next Options

- C2.8 API/UI evidence detail recovery.
- Long-running integrated worker smoke.
- Viewer runtime recovery.
- C2.3B-H Replay hardening.
