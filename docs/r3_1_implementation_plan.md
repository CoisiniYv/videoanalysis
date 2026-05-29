# R3.1 Implementation Plan

Status: planning only. R3.1 is split into independently reviewable subphases.
This document does not implement concrete algorithm logic, media generation, or
performance testing.

## R3.1A - Behavior Event Evidence MVP

### Goal

Use existing `intrusion` events to complete the behavior evidence path:

```text
event
  -> evidence_task
  -> snapshot_path / clip_path / media_status
  -> API evidence query
```

Clip generation may be `not_implemented` or a clearly labeled raw media
fallback in the first MVP, but task and event schemas must remain stable.

### Files To Modify

```text
services/event-worker/app/repository.py
services/event-worker/app/worker.py
services/clip-worker/app/repository.py
services/clip-worker/app/worker.py
services/clip-worker/app/config.py
services/api/app/repositories/events.py
services/api/app/schemas/events.py
docs/media_output_directory_policy.md
specs/05_database_schema.md
```

### Files To Add

```text
services/clip-worker/app/media_paths.py
services/clip-worker/app/evidence_task_repository.py
services/clip-worker/app/metadata_writer.py
harness/tests/test_r3_1_behavior_evidence_contract.py
harness/tests/test_r3_1_behavior_evidence_smoke_contract.py
```

### DB Migration

Likely yes:

```text
db/migrations/009_r3_1_evidence_task_runtime.sql
```

Expected additions:

```text
evidence_tasks.retry_count
evidence_tasks.max_retries
evidence_tasks.claimed_by
evidence_tasks.claimed_at
evidence_tasks.storage_fallback_used
evidence_tasks.output_root
evidence_tasks.metadata_path
```

### API Changes

1. Keep `GET /api/v1/events/{event_id}/evidence`.
2. Include task status, snapshot path, clip path, metadata path, retry count,
   and error message.
3. Keep event response stable for existing clients.

### Worker Changes

1. event-worker inserts `evidence_tasks` idempotently.
2. media-worker / clip-worker claims pending tasks.
3. media-worker computes stable output paths:

```text
/data/video-analytics/media/events/YYYY/MM/DD/<event_id>/
```

4. media-worker writes `metadata.json`.
5. media-worker updates event and task status.
6. clip output can initially be `not_implemented` if Replay/NVR is not ready.

### Contract Tests

1. Evidence task idempotency.
2. Media path format.
3. `media_status` transitions.
4. API evidence response includes event and tasks.
5. No event-type-specific event format in event-worker.

### Smoke Tests

1. Run one MediaMTX RTSP camera.
2. Trigger intrusion with ROI.
3. Verify event row.
4. Verify evidence task row.
5. Verify `media_status` reaches `ready` or `not_implemented`.
6. Verify API evidence query.

### Acceptance Commands

```bash
python3 -m pytest harness/tests/test_r3_1_behavior_evidence_contract.py -q
bash scripts/smoke/check_r2_5_single_rtsp_camera_inference.sh
```

The RTSP smoke should still use MediaMTX and should not become a performance
test.

### Boundaries

Do not modify:

```text
modules/savant_security/module.yml
YOLO26-pose / YOLOv8-Face / AdaFace pipeline
Redis producer behavior except additive SecurityEvent fields already agreed
```

### Independent Commit

Yes. R3.1A should be independently committable.

## R3.1B - Face Match Evidence MVP

### Goal

Turn face intelligence matches into unified evidence events:

```text
face_observation
  -> gallery / live_search match
  -> watchlist_hit / live_search_hit
  -> SecurityEvent
  -> evidence_task
  -> snapshot / clip
  -> matched person payload
```

This does not reimplement the face detector, does not change the Savant
pipeline, and does not move pgvector search into Savant.

### Files To Modify

```text
services/face-worker/app/worker.py
services/face-worker/app/vector_store.py
services/face-worker/app/match_repository.py
services/face-worker/app/config.py
services/event-worker/app/repository.py
services/api/app/schemas/events.py
specs/04_face_intelligence.md
specs/05_database_schema.md
```

### Files To Add

```text
services/face-worker/app/hit_event_exporter.py
services/face-worker/app/watchlist_service.py
services/face-worker/app/live_search_service.py
harness/tests/test_r3_1_face_hit_evidence_contract.py
```

### DB Migration

Likely yes:

```text
db/migrations/010_r3_1_face_hit_events.sql
```

Potential tables or fields:

```text
watchlist_rules
live_search_jobs
match cooldown state, if not handled in memory first
```

R3.1B can start with minimal configured thresholds if formal rule management is
deferred, but emitted events must use `SecurityEvent`.

### API Changes

1. Event detail payload must expose matched person id/name, gallery embedding
   id, similarity, face bbox, track id, and source observation id.
2. Algorithm registry stays as `face_intelligence`, not separate algorithm
   families for watchlist, trajectory, and live search.

### Worker Changes

1. face-worker consumes/stores observations as today.
2. A face intelligence service searches active gallery embeddings when enabled.
3. On threshold hit, it emits `SecurityEvent` with:

```text
event_type = watchlist_hit or live_search_hit
algorithm_type = face_intelligence
person_id
confidence / similarity
source_event_id
snapshot_required
clip_required
evidence_policy
payload.match
```

4. Event-worker and media-worker reuse the same evidence path as R3.1A.

### Contract Tests

1. face hit event uses `SecurityEvent`.
2. face hit payload contains matched person and similarity.
3. `algorithm_type` is `face_intelligence`.
4. `watchlist_hit` and `live_search_hit` are event types, not registry
   algorithm families.
5. No Savant pipeline change.

### Smoke Tests

1. Use existing registered gallery embeddings.
2. Run one MediaMTX RTSP stream.
3. Observe face observations.
4. Trigger a configured hit.
5. Verify event row and evidence task.
6. Verify API evidence query.

### Acceptance Commands

```bash
python3 -m pytest harness/tests/test_r3_1_face_hit_evidence_contract.py -q
```

Optional smoke should be separate and should not be a performance test.

### Boundaries

Do not modify:

```text
modules/savant_security/module.yml
YOLOv8-Face full-frame primary detector path
AdaFace embedding path
person-crop face detector strategy
```

### Independent Commit

Yes. R3.1B should be independently committable after R3.1A.

## R3.1C - Trajectory / Appearance Output MVP

### Goal

Expose person appearance history without treating every appearance as an alarm:

```text
person_id
  -> appearances
  -> camera timeline
  -> best snapshot
  -> optional clip references
```

Trajectory is a query capability. It should reuse existing observations and
media references where available.

### Files To Modify

```text
services/face-worker/app/trajectory_repository.py
services/api/app/main.py
specs/04_face_intelligence.md
specs/05_database_schema.md
specs/06_api_design.md
```

### Files To Add

```text
services/api/app/routers/face_intelligence.py
services/api/app/schemas/face_intelligence.py
harness/tests/test_r3_1_trajectory_output_contract.py
```

### DB Migration

Maybe. If existing `match_results` and `face_observations` are enough for MVP
queries, no migration is required. If best snapshot pointers or materialized
appearance summaries are needed, add a small migration later:

```text
db/migrations/011_r3_1_trajectory_output.sql
```

### API Changes

Candidate endpoints:

```http
GET /api/v1/persons/{person_id}/appearances
GET /api/v1/persons/{person_id}/trajectory
```

Response should include:

```text
camera_id
source_id
track_id
timestamp_ms
similarity
source_observation_id
snapshot_url
clip_url
```

### Worker Changes

No new long-running worker is required for read-side MVP. A later producer may
materialize `registered_person_history` rows if needed.

### Contract Tests

1. Trajectory API schemas exist.
2. Trajectory output references `face_observations` / `match_results`.
3. Trajectory output is not emitted as normal alarm events.
4. No mandatory clip generation for every appearance.

### Smoke Tests

1. Query a known person with gallery embeddings.
2. Return appearances from existing observations or match results.
3. Verify ordering by timestamp and camera.
4. Verify optional snapshot/clip references are nullable.

### Acceptance Commands

```bash
python3 -m pytest harness/tests/test_r3_1_trajectory_output_contract.py -q
```

### Boundaries

Do not create events for every appearance. Do not generate video for every
historical observation. Do not run performance tests.

### Independent Commit

Yes. R3.1C should be independently committable after the evidence schema has
stabilized.

## Global Scope Guard

R3.1 does not implement concrete algorithm logic. R3.1 does not run performance
tests. R3.1 does not change the Savant pipeline, Redis producer contract, or
dual-primary face/person inference design.
