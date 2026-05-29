# R3.1 Performance Design

Status: design only. This is not a performance test phase and contains no
benchmark results.

R3.1 must keep the evidence architecture compatible with the later dual T4 /
60 streams target without running a performance matrix now.

## Dual T4 / 60 Streams Constraints

The later target is dual T4 / 60 streams. R3.1 must therefore assume:

1. The GPU inference path is the scarce resource.
2. Media generation is CPU, disk, network, or NVR bound and must not compete
   with inference on the hot path.
3. Redis and PostgreSQL must absorb bursts without making Savant wait.
4. Clip creation can lag behind event ingestion.
5. A single camera or noisy event type must not starve the shared media queue.

## Async Evidence Generation

R3.1 requires async evidence generation.

Evidence generation must be asynchronous evidence generation:

```text
SecurityEvent ingestion
  -> events insert
  -> evidence_tasks insert
  -> async media queue
  -> media worker claims work
  -> update task/event status
```

The event-worker must not wait for snapshot or clip completion before ACKing a
valid Redis event. A failed clip must not roll back the event row.

## Cooldown / Rate Limit

Every camera / event_type pair needs cooldown / rate limit before production
load testing:

```text
cooldown_key = camera_id:event_type:zone_id_or_line_id:track_id_or_none
```

Policy examples:

1. intrusion: per camera + zone + track cooldown.
2. crowd_gathering: per camera + zone cooldown, not per person.
3. watchlist_hit: per camera + person + gallery rule cooldown.
4. live_search_hit: per live_search_job + camera + person/observation cooldown.

Cooldown must apply before creating unbounded evidence tasks.

## Queue Backpressure

Backpressure rules:

1. Keep event ingestion available even when media is overloaded.
2. Cap active media workers and per-camera in-flight media tasks.
3. Prioritize snapshots over clips.
4. Preserve task records with `pending` or `failed` status rather than dropping
   them silently.
5. Allow clip deferral or skip when queue length exceeds threshold.
6. Expose queue length and oldest pending task age.

Degradation under overload:

```text
keep event
keep event payload
attempt snapshot
defer clip
mark clip failed or not_implemented after retry policy
```

## Snapshot First / Clip Later

Snapshot-first / clip-later strategy:

1. Snapshot is small, fast, and operator-visible.
2. Clip is larger, slower, and may depend on Replay/NVR availability.
3. `media_status` can move to `processing` while snapshot is ready and clip is
   pending, with detail in `payload.media`.
4. If clip fails, the event remains queryable with snapshot and error details.

## Media Worker Concurrency Limits

Media workers need explicit limits:

```text
MEDIA_WORKER_MAX_CONCURRENCY
MEDIA_WORKER_MAX_CLIP_CONCURRENCY
MEDIA_WORKER_MAX_SNAPSHOT_CONCURRENCY
MEDIA_WORKER_PER_CAMERA_INFLIGHT_LIMIT
MEDIA_WORKER_QUEUE_HIGH_WATERMARK
```

The initial MVP can use conservative single-process limits. The contract should
still support multiple workers claiming tasks idempotently.

## Metrics

Required metrics before performance testing:

1. event ingestion latency.
2. evidence task queue length.
3. oldest pending evidence task age.
4. snapshot generation latency.
5. clip generation latency.
6. Redis lag per consumer group.
7. DB write latency for `events`, `face_observations`, and `evidence_tasks`.
8. media failure rate.
9. retry count distribution.
10. face-worker pgvector search latency.
11. API query latency.

Latency categories must be separable:

```text
inference latency
event-worker latency
media-worker latency
API query latency
```

No performance test is meaningful until these categories can be measured
separately.

## Why Slow Work Is Outside Savant

Savant must not do pgvector slow queries or video clipping in the main pipeline
because:

1. GPU inference should stay deterministic and bounded.
2. pgvector latency depends on DB load, index state, and query scope.
3. Video clipping depends on disk, NVR/Replay state, and network.
4. Evidence media can be retried; inference frames cannot be blocked for retry.
5. A slow camera or failed storage path would otherwise stall unrelated streams.

Savant should emit metadata only: objects, tracks, bboxes, keypoints, face
observations, embeddings metadata, and `SecurityEvent`.

## Failure and Degradation Strategy

When the evidence queue is overloaded:

1. Always store the event.
2. Create or preserve the `evidence_task`.
3. Prefer snapshot output.
4. Defer clip until the queue drains.
5. Mark clip `failed` only after retry policy.
6. Store `error_message` and `retry_count`.
7. Keep API evidence response stable.

When storage root is unavailable:

1. use `/data/video-analytics/media` when writable;
2. development fallback to `./tmp` only when explicit;
3. record `storage_fallback_used=true`;
4. do not silently write large outputs into the repo.

## Performance Test Preconditions

Performance testing must wait until:

1. R3.1A intrusion evidence MVP is stable.
2. Event and evidence task schemas are final enough for migration.
3. Media path policy is reconciled.
4. Event-worker can ingest without waiting for media.
5. Media-worker can mark `pending`, `processing`, `ready`, `failed`, and
   `not_implemented`.
6. Redis lag and DB write latency are measurable.
7. Clip failure does not affect event ingestion.
8. The dual-primary Savant pipeline remains unchanged.

R3.1 does not run performance tests.
