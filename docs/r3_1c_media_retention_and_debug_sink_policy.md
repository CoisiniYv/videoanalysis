# R3.1C Media Retention and Debug Sink Policy

Status: policy and contract only. This phase does not implement snapshot
generation, clip generation, a cleanup worker, a Savant pipeline change, or a
performance test.

R3.1C exists before production media generation so the system does not grow
disk usage without bounds when moving toward dual T4 / 60 streams.

## Production Evidence Path

Production evidence media is controlled output only:

```text
/data/video-analytics/media/events/YYYY/MM/DD/<event_id>/
  metadata.json
  snapshot.jpg
  raw_clip.mp4
```

`raw_clip.mp4` is the canonical production evidence video when
`clip_required=true` and the clip worker supports extraction. The clip worker
should prefer copy/remux and should not default to re-encoding.

`annotated_clip.mp4` is optional/on-demand only. It is allowed for debug,
operator-requested report export, or review workflows after the performance
baseline. The default production path must have no default dual video output:
one event must not automatically create both `raw_clip.mp4` and
`annotated_clip.mp4`.

Overlay data belongs in `metadata.json`. The frontend or large-screen client
should render dynamic overlays from metadata while playing `raw_clip.mp4`.
`snapshot.jpg` may be annotated because it is a small single-frame asset.

## Debug Sink Boundary

`video-file-sink` is a debug/development sink. It may continuously write video
under the shared media mount when enabled and must not be treated as production
evidence.

`metadata-sink` is a debug/development sink. It may continuously write NDJSON
metadata and must not be treated as production evidence.

Production deployments should not run `video-file-sink` for long periods by
default. Production deployments should not run `metadata-sink` for long
periods unless retention limits are enabled and monitored.

Debug sink output must use an explicit debug directory, for example:

```text
/data/video-analytics/media/debug/video-file-sink/
/data/video-analytics/media/debug/metadata-sink/
```

Debug sink output is not returned by the production evidence API. Evidence API
responses are driven by `events`, `evidence_tasks`, and the controlled event
evidence directory.

## Source Adapter Cache Boundary

The RTSP source adapter may receive `DOWNLOAD_PATH=/tmp/video-loop-cache`.
For RTSP this is not a production evidence path. It is container-local temporary
cache unless explicitly mounted, and it must not be used as evidence media.

Long-running source adapters must still be supervised so reconnect loops or
unexpected adapter cache behavior cannot hide disk growth inside containers.

## Retention Defaults

Recommended defaults:

```text
retention_days = 7
max_total_gb = 500
max_camera_gb = 50
min_free_disk_percent = 15
delete_oldest_first = true
```

High severity events may receive longer retention by policy, but that extension
must be explicit and bounded.

## Cleanup Rules

Cleanup order:

1. Delete debug sink output first.
2. Delete expired evidence media next.
3. Keep `metadata.json` and audit metadata longer than large video files when
   possible.
4. Delete oldest first when limits are exceeded.

Cleanup must never delete DB rows. It must not delete `events`,
`face_observations`, `match_results`, `persons`, or gallery records. When a
media file is removed, the database should keep the business record and mark
media as `media_expired` or `media_deleted`, preserving enough metadata to
explain what happened.

## Backpressure and Degradation

When storage approaches the threshold:

```text
event ingestion continues
evidence_task may still be created
snapshot-first / clip-later applies
clip may be delayed, skipped, or marked storage_limited
annotated video is not generated
media_status becomes failed, skipped, storage_limited, media_expired, or media_deleted
```

Storage pressure must not block Savant inference. The GPU path only emits
metadata and events; it does not write large media files, run pgvector slow
queries, or perform video clipping.

## Dual T4 / 60 Streams Design

For the dual T4 / 60 streams target:

1. GPU inference only runs the model and tracking path.
2. Clip generation is asynchronous and isolated from Savant.
3. Retention cleanup is isolated from Savant and media ingestion.
4. Media worker concurrency is bounded.
5. Queue backpressure must protect Redis, PostgreSQL, and disk.
6. Debug sinks are opt-in and capacity-limited.

Required metrics before performance testing:

```text
media_dir_total_gb
debug_sink_total_gb
evidence_total_gb
media_cleanup_deleted_bytes
media_cleanup_failures
evidence_task_queue_depth
storage_limited_count
```

This document defines the policy only. It does not implement cleanup worker
logic. It does not implement snapshot generation. It does not implement clip generation.
It does not make a Savant pipeline change. It does not run a performance test.
