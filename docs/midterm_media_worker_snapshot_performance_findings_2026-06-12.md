# Midterm Media-Worker Snapshot and Performance Findings

Date: 2026-06-12

This note records the current midterm finding around screenshot generation and
media-worker performance risk. It is based on the checked-out code,
`infra/docker-compose.midterm.yml`, `modules/savant_security/config/cameras.midterm.yml`,
and a read-only runtime check against the running midterm containers.

## Summary

The current midterm path does not use screenshots as the primary evidence
surface for intrusion events. Intrusion evidence is currently clip-first:
`raw_clip.mov` plus production JSONL sidecar/overlay data. Static snapshots
still exist in media-worker, but they are only generated for events whose media
policy sets `snapshot_required=true`.

For the active intrusion rules, `snapshot_required=false` is configured
alongside `clip_required=true`. Therefore, when the clip becomes ready,
media-worker marks those events as `snapshot_status=not_required` instead of
extracting a JPEG. This is expected under the current policy, not a total
snapshot backend failure.

## Current Policy Path

Current midterm compose still wires snapshot directories:

- `SNAPSHOT_OUTPUT_DIR=/media/midterm-snapshots`
- `ANNOTATED_OUTPUT_DIR=/media/midterm-snapshots/annotated`
- `EVIDENCE_OUTPUT_DIR=/media/evidence`
- `EVIDENCE_TOPOLOGY=post_savant_replay`

The active intrusion rules in `cameras.midterm.yml` currently set:

- `clip_required: true`
- `snapshot_required: false`
- `evidence_policy.snapshot_required: false`
- `evidence_policy.clip_required: true`

`event-worker` only applies its default behavior evidence policy when the event
has no explicit evidence requirement. If upstream policy already says
`clip_required=true` and `snapshot_required=false`, event-worker does not turn
snapshots back on.

`media-worker` snapshot generation then applies this gate:

- only `clip_status in ('ready', 'generated')`
- `clip_path` must be present
- `payload.media.snapshot_required` or top-level `snapshot_required` must be
  true
- events with clean clips and `snapshot_required=false` are marked
  `snapshot_status=not_required`

## Runtime Evidence

Read-only checks against the currently running containers showed:

- `video-analytics-midterm-media-worker` is running.
- `media-worker` log includes normal evidence finalization and at least one
  recent `generating_snapshot` event.
- A recent watchlist event generated a real snapshot:
  - event id: `e107b6ea-5169-40b7-a529-3fe64dc947bc`
  - event type: `watchlist_hit`
  - `snapshot_required=true`
  - `snapshot_status=ready`
  - `snapshot_path=/media/midterm-snapshots/e107b6ea-5169-40b7-a529-3fe64dc947bc.jpg`
  - `raw_clip_path=/media/evidence/e107b6ea-5169-40b7-a529-3fe64dc947bc/raw_clip.mov`

Recent 30 minute distribution at the time of inspection:

| event_type | snapshot_required | snapshot_status | clip_status | count |
| --- | --- | --- | --- | ---: |
| intrusion | false | not_implemented | not_implemented | 39 |
| intrusion | false | not_implemented | failed | 12 |
| intrusion | false | not_required | ready | 9 |
| watchlist_hit | true | ready | ready | 3 |
| intrusion | false | not_implemented | skipped_by_poc_limit | 1 |

Aggregate checks at the time of inspection:

- `snapshot_required=true` + `snapshot_status=ready` + `snapshot_path IS NOT NULL`: 149 events
- `snapshot_required=false` + `snapshot_status=not_required` + `clip_status=ready`: 1911 events

So the accurate statement is:

> The snapshot backend can generate screenshots, but current intrusion policy
> disables them. Watchlist evidence still generates snapshots when
> `snapshot_required=true`.

## Performance Risk Points

The current media-worker path is adequate for demo and low-rate validation, but
several parts are not shaped for long-running multi-camera load without further
work.

1. Filesystem polling and full sink scans

   `media-worker` polls every 5 seconds and searches the active sink directory
   with `rglob("metadata.json")`. Even with a run-scoped epoch directory, this
   cost grows with accumulated sink output inside the active epoch. A long run or
   many cameras will increase repeated filesystem work.

2. Repeated ffmpeg/ffprobe subprocesses

   Snapshot extraction performs a duration probe and frame extraction, with a
   retry path on failure. Evidence finalization also performs duration checks,
   decoded-frame counting, and optional crop/transcode work. These subprocesses
   are correct for validation, but expensive as a per-event default path.

3. Whole-metadata reads

   Post-Savant bundle generation loads native sink metadata into memory, selects
   time-domain frames, writes sidecars, and then later reloads metadata again for
   guard checks. This is linear in metadata rows and becomes costly as clip
   windows or sink outputs grow.

4. Redis frame-cache lookback scan

   The frame-cache sidecar path reads Redis with `XREVRANGE` using a configured
   lookback/max scan. Current compose defaults set:

   - `FRAME_CACHE_SIDECAR_LOOKBACK_COUNT=20000`
   - `FRAME_CACHE_SIDECAR_MAX_SCAN=20000`

   Each sidecar attempt scans and filters those entries by runtime epoch,
   stream session, source, and camera. This is bounded, but still a per-event
   scan rather than an indexed/ranged lookup.

5. JSONB status filtering without expression indexes

   Snapshot and media finalization queries filter on expressions such as
   `payload -> 'media' ->> 'clip_status'` and
   `payload -> 'media' ->> 'snapshot_status'`. Current migrations include
   ordinary `events` indexes such as `event_type`, `camera_id`, `start_ts`,
   `status`, and `start_ts_ms`, but not expression indexes for these media
   status fields. As the event table grows, these queries are likely to degrade.

## Recommended Follow-Up

1. Treat static intrusion screenshots as an explicit product choice.

   If intrusion cards need JPEG snapshots, change the rule/evidence policy to
   set `snapshot_required=true` and validate the extra per-event ffmpeg cost.
   If the product direction is video-first evidence, keep intrusion snapshots
   disabled and avoid treating `snapshot_status=not_required` as a failure.

2. Add runtime metrics before larger performance work.

   Useful counters/timers:

   - sink scan duration and number of metadata files visited
   - per-event finalization duration
   - ffmpeg/ffprobe invocation count and duration
   - metadata rows loaded per bundle
   - Redis frame-cache entries scanned and retained
   - snapshot queue size and snapshot extraction duration

3. Replace full sink scans with incremental discovery.

   Prefer event-driven notification, a manifest/queue, or tracking only newly
   created sink output directories within the active runtime epoch.

4. Reuse probe and metadata results.

   Avoid loading the same `metadata.json` multiple times in one finalization
   pass. Cache decoded-frame counts, duration probes, and parsed metadata within
   the per-event bundle operation.

5. Add targeted database indexes for current worker queries.

   Candidate indexes should be validated with `EXPLAIN`, but likely include
   expression or partial indexes for:

   - `payload -> 'media' ->> 'clip_status'`
   - `payload -> 'media' ->> 'snapshot_status'`
   - `payload -> 'media' ->> 'snapshot_required'`
   - possibly `(updated_at)` or `(created_at)` partial filters for recent media
     worker scans

6. Make frame-cache sidecar reads time/range bounded.

   Prefer reading by Redis stream id/time range or maintaining per-source
   bounded streams, rather than scanning a fixed 20k recent entries for every
   event.

## Code Pointers

- `services/media-worker/app/worker.py`
  - `_find_metadata_files`
  - `_process_sink_output`
  - `_snapshot_needed`
  - `_mark_not_required`
  - `_process_pending_snapshots`
  - `_finalize_post_savant_evidence_bundle`
- `services/media-worker/app/snapshot.py`
  - `generate_snapshot`
- `services/media-worker/app/post_savant_evidence_bundle.py`
  - `build_post_savant_evidence_bundle`
  - `read_decoded_video_frame_count`
- `services/media-worker/app/frame_cache_sidecar_writer.py`
  - `_read_frame_annotations`
- `services/event-worker/app/worker.py`
  - `_apply_default_evidence_policy`
  - `_apply_recording_window`
- `infra/docker-compose.midterm.yml`
- `modules/savant_security/config/cameras.midterm.yml`
