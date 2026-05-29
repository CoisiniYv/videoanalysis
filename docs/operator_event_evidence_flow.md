# Operator: Event Evidence Flow

R3 standardizes how an event asks for snapshot and clip evidence.

## Flow

```text
Savant / face-worker
  -> Redis security.events
  -> event-worker
  -> PostgreSQL events
  -> evidence_tasks
  -> security.clip_tasks or media-worker / clip-worker
  -> snapshot.jpg / clip.mp4 / metadata.json
  -> events.snapshot_path / events.clip_path / events.media_status
```

The event-worker consumes one `SecurityEvent` contract for all algorithms. It
does not branch into a different event format per algorithm.

## Media Paths

Preferred output:

```text
/data/video-analytics/media/events/YYYY/MM/DD/<event_id>/snapshot.jpg
/data/video-analytics/media/events/YYYY/MM/DD/<event_id>/clip.mp4
/data/video-analytics/media/events/YYYY/MM/DD/<event_id>/metadata.json
```

If that root is not writable, fallback to `./tmp` is allowed only when
`storage_fallback_used=true` is recorded explicitly.

## Evidence Policy

Each event may set:

```json
{
  "snapshot_required": true,
  "clip_required": true,
  "pre_seconds": 5,
  "post_seconds": 10
}
```

If neither snapshot nor clip is required, no evidence task is created.

## Current R3 Scope

R3 creates the contract and the database/API shape. Debug smoke outputs are not
production evidence. Full production clip generation remains a later
Replay/NVR-backed media-worker task.

R3 does not change the Savant module pipeline and does not run performance
tests. Performance testing is scheduled after the event and evidence trunk is
stable.
