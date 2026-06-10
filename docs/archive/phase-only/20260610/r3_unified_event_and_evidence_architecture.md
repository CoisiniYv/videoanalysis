# R3 Unified Event, Evidence, and Algorithm Plugin Architecture

R3 defines the shared contracts for events, evidence, and algorithm rules. It
does not implement new loitering, fall, chasing, wall-climb, or other complex
algorithm logic, and it is not a performance test phase.

## Algorithm Registry

The first registry contains eight algorithm families:

1. `intrusion`
2. `loitering`
3. `crowd_gathering`
4. `running`
5. `chasing`
6. `fall`
7. `wall_climb`
8. `face_intelligence`

`intrusion`, `loitering`, `crowd_gathering`, `running`, `chasing`, `fall`, and
`wall_climb` are behavior-rule algorithms. `face_intelligence` is the single
face intelligence pipeline for `watchlist_hit`, trajectory and appearances
queries, and `live_search` / one-click person search. Watchlist, trajectory, and
live search are not split into separate algorithm families.

## Event Types

The unified event type set is:

```text
intrusion
loitering
crowd_gathering
running
chasing
fall
wall_climb_suspicious
face_observed
watchlist_hit
live_search_hit
```

`face_observed` may be stored without becoming an alarm. `watchlist_hit` and
`live_search_hit` are alert events produced by the Face Intelligence Pipeline.
Trajectory and appearances are query capabilities, not required alarm events.

## SecurityEvent Contract

Every producer uses the same `SecurityEvent` shape:

```python
class SecurityEvent:
    event_type: str
    source_event_id: str
    camera_id: str
    source_id: str
    track_id: str | None
    person_id: int | None
    algorithm_type: str
    algorithm_version: str | None
    severity: str
    confidence: float
    start_ts_ms: int
    end_ts_ms: int | None
    snapshot_required: bool
    clip_required: bool
    evidence_policy: dict
    payload: dict
```

`source_event_id` is the idempotency key. Algorithm-specific fields belong in
`payload`; algorithms must not invent separate event formats.

## EvidenceTask Contract

Events that request media create one evidence task:

```python
class EvidenceTask:
    task_id: str
    event_id: int
    source_event_id: str
    camera_id: str
    source_id: str
    event_type: str
    event_ts_ms: int
    snapshot_required: bool
    clip_required: bool
    pre_seconds: int
    post_seconds: int
    status: str
    snapshot_path: str | None
    clip_path: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
```

The current `events.id` implementation is UUID-based, so the migration draft
uses a UUID `event_id` foreign key while keeping the public contract field name
`event_id`.

## Media Output Path

Production event media uses:

```text
/data/video-analytics/media/
  events/
    YYYY/MM/DD/<event_id>/
      snapshot.jpg
      clip.mp4
      metadata.json
```

If `/data/video-analytics/media` is not writable, the media worker may fall
back to `./tmp`, but it must explicitly write `storage_fallback_used=true` in
metadata and task payload.

## Event And Evidence Flow

```text
Savant / face-worker
  -> Redis security.events
  -> event-worker
  -> upsert events by source_event_id
  -> if snapshot_required or clip_required:
       create evidence_task
       publish security.clip_tasks or call media-worker
  -> media-worker / clip-worker
  -> save snapshot / clip
  -> update events.snapshot_path / clip_path / media_status
```

R3 only contracts this flow. Production clip generation remains tied to the
later Replay/NVR-capable media worker path.

## Database Extension

R3 reuses the existing `events` and `camera_rules` tables and extends them with:

- `events.algorithm_type`
- `events.algorithm_version`
- `events.start_ts_ms`
- `events.end_ts_ms`
- `events.snapshot_required`
- `events.clip_required`
- `events.evidence_policy`
- `camera_rules.zone_id`
- `camera_rules.line_id`
- `camera_rules.evidence_policy`
- new `evidence_tasks`

The draft migration is
`db/migrations/008_r3_unified_event_evidence_algorithm_rules.sql`.

## Boundaries

R3 does not implement new concrete algorithm logic. It does not change the
YOLO26-pose, YOLOv8-Face full-frame primary, face-person association, AdaFace,
or Redis producer pipeline. It does not run performance tests. Performance testing must wait until the event and evidence trunk is stable.
