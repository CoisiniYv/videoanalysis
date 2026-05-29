# R3.1 Code Inventory

Status: planning only. This inventory records the current implementation shape
and the files expected to change in later R3.1 subphases. It does not implement
snapshot generation, clip generation, watchlist hit logic, live search hit
logic, trajectory API, or performance testing.

## Reviewed Areas

### Savant / event model

Reviewed:

```text
modules/savant_security/custom/models/events.py
modules/savant_security/custom/services/event_exporter.py
modules/savant_security/custom/pyfuncs/behavior_rules.py
modules/savant_security/custom/rules/
modules/savant_security/module.yml
```

Current state:

1. `SecurityEvent` already contains the R3 unified fields:
   `event_type`, `source_event_id`, `camera_id`, `source_id`, `track_id`,
   `person_id`, `algorithm_type`, `algorithm_version`, `severity`,
   `confidence`, `start_ts_ms`, `end_ts_ms`, `snapshot_required`,
   `clip_required`, `evidence_policy`, and `payload`.
2. `SECURITY_EVENT_TYPES` includes behavior events plus `face_observed`,
   `watchlist_hit`, and `live_search_hit`.
3. `RedisStreamEventExporter` writes unified events to `security.events`.
4. `BehaviorRulesPyFunc` stamps `source_id`, `camera_id`, `event_ts_ms`, bbox
   payload, and media flags.
5. Intrusion is the only implemented behavior rule. Other behavior algorithms
   are registry/config contracts only.
6. `frame_uuid` and `keyframe_uuid` are currently set to `None`.
7. `module.yml` wires YOLO26-pose, behavior rules, YOLOv8-Face full-frame
   primary, face association, AdaFace, and face observation export. R3.1 should
   not modify this file.

### Event worker

Reviewed:

```text
services/event-worker/app/worker.py
services/event-worker/app/repository.py
services/event-worker/app/record_request.py
services/event-worker/app/config.py
```

Current state:

1. The worker consumes `security.events`, parses `data` as a unified
   `SecurityEvent`, inserts into `events`, and ACKs on success.
2. Insert is idempotent through `ON CONFLICT (source_event_id) DO NOTHING`.
3. `create_evidence_task()` exists and creates a single `snapshot_clip` task
   when evidence is required.
4. The worker may also publish a `security.record_requests` message when
   `RECORDING_ENABLED=true` and clip is required.
5. Evidence task creation failure is logged but does not block ACK. This is
   acceptable for ingestion, but R3.1 needs retry/repair visibility.

### Clip / media worker

Reviewed:

```text
services/clip-worker/app/
services/clip-worker/app/repository.py
services/clip-worker/app/worker.py
services/clip-worker/app/replay_client.py
```

Current state:

1. `clip-worker` consumes `security.record_requests`, not `evidence_tasks`
   directly.
2. It can call Replay keyframe lookup and create a Replay job.
3. It updates `events.media_status` and `payload.media.clip_status`.
4. It does not write `snapshot.jpg`, `clip.mp4`, or `metadata.json`.
5. It does not update `evidence_tasks.status`, `evidence_tasks.snapshot_path`,
   or `evidence_tasks.clip_path`.
6. The Replay timestamp-domain mapping is still incomplete: `find_keyframe()`
   logs anchored lookup values but currently omits `from`/`to` because event
   epoch milliseconds do not map cleanly to Replay's stored timestamps.

### Face worker

Reviewed:

```text
services/face-worker/app/
services/face-worker/app/gallery_repository.py
services/face-worker/app/face_observation_repository.py
services/face-worker/app/match_repository.py
services/face-worker/register_face_image.py
```

Current state:

1. `face-worker` stores validated AdaFace observations into
   `face_observations`.
2. `FaceVectorStore.search_gallery()` can search active
   `person_gallery_embeddings`.
3. `MatchResultRepository` stores `gallery_match` results in `match_results`.
4. `TrajectoryRepository` can read `registered_person_history` rows from
   `match_results`.
5. No production `watchlist_hit` or `live_search_hit` producer exists yet.
6. No face match path emits `SecurityEvent` into `security.events` yet.
7. `services/face-worker/app/match_repository.py` is currently dirty with a
   partial unique conflict target for `gallery_match`. It is related to future
   face intelligence correctness, but this R3.1 planning pass does not handle,
   revert, or commit it.

### API

Reviewed:

```text
services/api/app/main.py
services/api/app/routers/events.py
services/api/app/routers/algorithms.py
services/api/app/routers/cameras.py
services/api/app/schemas/events.py
services/api/app/schemas/algorithms.py
services/api/app/repositories/events.py
services/api/app/repositories/cameras.py
```

Current state:

1. `/api/v1/events` and `/api/v1/events/{event_id}` exist.
2. `/api/v1/events/{event_id}/evidence` returns the event and related
   `evidence_tasks`.
3. Event response includes `snapshot_path`, `clip_path`, media URLs,
   `media_status`, evidence flags, and payload.
4. `/api/v1/algorithms` returns the 8 algorithm registry entries.
5. Camera algorithm-rule APIs exist and write to `camera_rules`.
6. Runtime export can include rules, `zone_id`, `line_id`, and
   `evidence_policy`.
7. No trajectory / appearances API exists yet.

### Database

Reviewed:

```text
db/migrations/
db/migrations/008_r3_unified_event_evidence_algorithm_rules.sql
specs/05_database_schema.md
```

Current state:

1. `events` has `snapshot_path`, `clip_path`, and `media_status` from earlier
   migrations.
2. R3 migration `008` adds `algorithm_type`, `algorithm_version`,
   `start_ts_ms`, `end_ts_ms`, `snapshot_required`, `clip_required`, and
   `evidence_policy`.
3. `evidence_tasks` exists with task id, event id, source event id, source
   context, evidence flags, pre/post seconds, status, output paths, and error.
4. `evidence_tasks` currently lacks explicit retry count, claimed/locked
   fields, worker id, and storage fallback fields.
5. `face_observations`, `persons`, `person_gallery_embeddings`, and
   `match_results` are available for face intelligence and trajectory work.

### Docs

Reviewed:

```text
docs/r3_unified_event_and_evidence_architecture.md
docs/operator_event_evidence_flow.md
docs/api_algorithm_rule_design.md
docs/operator_add_algorithm_rule.md
docs/media_output_directory_policy.md
docs/performance_test_preflight.md
```

Current state:

1. R3 docs define the unified contracts.
2. `operator_event_evidence_flow.md` uses the R3 target path
   `/data/video-analytics/media/events/YYYY/MM/DD/<event_id>/`.
3. `media_output_directory_policy.md` still documents the older path
   `/data/video-analytics/media/evidence/events/{event_id}/` and older file
   names such as `clip_raw.mp4` and `evidence_metadata.json`.
4. R3.1 must reconcile the media path policy before implementation.

## Architecture Questions

1. Current `events` table support for `snapshot_path` / `clip_path` /
   `media_status`: yes. These fields exist and API responses expose them.
2. Current `evidence_tasks` table: yes, migration `008` creates it. Fields are
   sufficient for a contract MVP, but retry count, worker claim fields, and
   fallback metadata should be added before production media workers.
3. Event-worker creates `evidence_tasks`: yes, when `snapshot_required`,
   `clip_required`, or `evidence_policy` requires evidence.
4. Clip-worker can generate or write back snapshot/clip by
   `event_id`/`source_event_id`: partially no. It can update clip status and
   create Replay jobs, but it does not write files or update `evidence_tasks`.
5. Existing replay / raw mp4 fallback: partially yes. `ReplayClient` and older
   Phase 3H/E1 media cache concepts exist, but timestamp mapping and production
   event file output are not complete.
6. API can query event evidence: yes. `/api/v1/events/{event_id}/evidence`
   returns event and evidence tasks. It does not guarantee ready media.
7. Face-worker can produce `watchlist_hit` / `live_search_hit`: no. It can
   store observations and search gallery vectors, but it lacks watchlist/live
   search job state, thresholds, cooldown, event emission, and evidence payload
   assembly.
8. Trajectory / appearances foundation: partial. `TrajectoryRepository` reads
   `registered_person_history` rows from `match_results`, but the producer for
   those rows and API output are not complete.
9. Intrusion event replay fields: it has `event_ts_ms`, `source_id`, bbox, and
   media flags. It does not have `frame_uuid`, `keyframe_uuid`, or usable Replay
   timestamp mapping.
10. MVP fallback without `frame_uuid`/`keyframe_uuid`: snapshot can be
    `not_implemented`, synthetic metadata-only, or best-effort from a recent
    frame cache keyed by `source_id` and timestamp. Clip can remain
    `not_implemented` or use a raw media fallback only when source video/cache
    is explicitly available.
11. MediaMTX RTSP smoke evidence context: yes for `source_id` and
    `timestamp_ms`/`event_ts_ms`. It proves events and face observations can be
    tied to a source, but does not provide exact frame UUIDs.
12. Performance bottlenecks: GPU inference, Redis lag, event-worker DB writes,
    face-worker pgvector search, media worker CPU/disk I/O, Replay/NVR latency,
    and API query joins.
13. Must be async: snapshot extraction, clip extraction, annotation,
    pgvector-heavy matching for face hits, event notifications, and file writes.
14. Can be stubbed with stable schema: clip generation, annotated media,
    metadata-only evidence, Replay/NVR integration, trajectory best-snapshot
    selection, watchlist/live-search management UI.

## Reusable Code

1. `SecurityEvent` dataclass and Redis exporter.
2. Intrusion rule and behavior rule runtime.
3. Event-worker idempotent insert path.
4. `evidence_tasks` table and API evidence response.
5. Clip-worker Replay client and record request stream.
6. Face observation ingestion and AdaFace validation.
7. Gallery search and match result storage.
8. Camera zones, algorithm rules, and runtime config export.

## Files Likely To Modify Later

R3.1A:

```text
services/event-worker/app/repository.py
services/event-worker/app/worker.py
services/clip-worker/app/repository.py
services/clip-worker/app/worker.py
services/clip-worker/app/config.py
services/api/app/repositories/events.py
services/api/app/schemas/events.py
docs/media_output_directory_policy.md
db/migrations/009_r3_1_evidence_task_runtime.sql
```

R3.1B:

```text
services/face-worker/app/worker.py
services/face-worker/app/vector_store.py
services/face-worker/app/match_repository.py
services/face-worker/app/config.py
services/event-worker/app/repository.py
services/api/app/schemas/events.py
db/migrations/010_r3_1_face_hit_events.sql
```

R3.1C:

```text
services/face-worker/app/trajectory_repository.py
services/api/app/routers/face_intelligence.py
services/api/app/schemas/face_intelligence.py
services/api/app/main.py
db/migrations/011_r3_1_trajectory_output.sql
```

## Files Likely To Add Later

```text
services/clip-worker/app/media_paths.py
services/clip-worker/app/evidence_tasks.py
services/clip-worker/app/snapshot_writer.py
services/clip-worker/app/metadata_writer.py
services/face-worker/app/hit_event_exporter.py
services/face-worker/app/watchlist_service.py
services/face-worker/app/live_search_service.py
services/api/app/routers/face_intelligence.py
services/api/app/schemas/face_intelligence.py
harness/tests/test_r3_1_behavior_evidence_contract.py
harness/tests/test_r3_1_face_hit_evidence_contract.py
harness/tests/test_r3_1_trajectory_output_contract.py
```

## Files Not To Modify In R3.1 Implementation

```text
modules/savant_security/module.yml
modules/savant_security/custom/pyfuncs/face_observation_exporter.py
modules/savant_security/custom/pyfuncs/face_person_associator.py
modules/savant_security/custom/pyfuncs/face_reid_gate.py
YOLO26-pose / YOLOv8-Face / AdaFace model config
Redis producer contracts except additive fields already covered by SecurityEvent
```

## Current Blockers

1. Exact frame identity is missing: `frame_uuid` and `keyframe_uuid` are `None`.
2. Replay timestamp-domain mapping is unresolved.
3. `clip-worker` updates event clip status but does not own `evidence_tasks`.
4. No production snapshot writer exists for live RTSP events.
5. Media path policy must be reconciled between current policy docs and R3
   target paths.
6. No watchlist/live-search hit producer exists.
7. Trajectory output lacks an API and a producer for
   `registered_person_history` rows.
8. `services/face-worker/app/match_repository.py` has an unrelated dirty
   change. It is related to face matching semantics but outside this planning
   pass.

## Scope Guard

This inventory does not implement concrete algorithm logic. It does not run
performance tests. It does not change the Savant pipeline.
