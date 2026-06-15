# Midterm Phase 0 Evidence Alignment Baseline - 2026-06-15

## Scope

This captures the pre-fix runtime baseline for
`specs/17_clip_worker_evidence_realtime_alignment_fix.md`.

No code or service changes were made during this phase.

Acceptance token:

```text
PASS_PHASE0_EVIDENCE_BASELINE_CAPTURED
```

## Runtime Snapshot

Collected at approximately 2026-06-15 08:39 UTC / 16:39 CST.

- 8090 runtime overview reported `health.ok=true`, no stale sources, and two
  active Savant sources.
- `primary_rtsp` was actively annotated at about 8.1fps.
  - `frames_seen_total=484112`
  - `pose_objects_total=29648`
  - `face_objects_total=78718`
- Lab source `source_00000000-0000-4000-8000-781078565686` was active at about
  8.1fps.
  - `frames_seen_total=480791`
  - `pose_objects_total=55`
  - `face_objects_total=477`
- Analysis-forwarder was healthy.
  - `queue_depth=0`
  - `primary_rtsp savant_send_failures_total=0`
  - lab `savant_send_failures_total=0`
- Source containers were running.
  - fixed compose source restart count: 1
  - dynamic lab source restart count: 0
  - clip-worker/media-worker/event-worker restart count: 0

## Redis Record Request State

`security.record_requests` / `clip-workers-midterm` at capture time:

- `pending=0`
- `lag=0`
- one consumer
- `last-delivered-id=1781512761521-0`

This confirms the immediate evidence gap is not a Redis backlog problem.

## Clip-Worker and Event-Worker Log Counts

Last 30 minutes of clip-worker logs:

- `clip_worker_deferred=105`
- `clip_worker_final_failed=7`
- `missing_post_savant_frame_pts_window=190`
- `max_concurrent_reached=9`
- `replay_job_created=14`
- `frame_domain_proof_selected=14`

Last 30 minutes of event-worker logs:

- `event_suppressed=22`
- `record_request_published_or_created=21`
- `camera_algorithm_cooldown=22`
- `record_request_skipped=1`

This supports the spec diagnosis: events and record requests are being created,
but clip-worker often waits for post-Savant frame proof through Redis redelivery
and sometimes treats replay concurrency as a terminal retry failure.

## Database State

Recent 3-hour event counts by source/status:

| source | event type | media_status | count |
| --- | --- | --- | ---: |
| `primary_rtsp` | intrusion | failed | 62 |
| `primary_rtsp` | intrusion | not_implemented | 206 |
| `primary_rtsp` | intrusion | ready | 68 |
| `primary_rtsp` | watchlist_hit | failed | 4 |
| `primary_rtsp` | watchlist_hit | not_implemented | 11 |
| `primary_rtsp` | watchlist_hit | ready | 13 |
| `source_00000000-0000-4000-8000-781078565686` | intrusion | failed | 1 |

Recent 3-hour `evidence_tasks` status by source:

| source | event type | task status | count |
| --- | --- | --- | ---: |
| `primary_rtsp` | intrusion | pending | 140 |
| `primary_rtsp` | watchlist_hit | pending | 17 |
| `source_00000000-0000-4000-8000-781078565686` | intrusion | pending | 1 |

For clip-required events in the same window:

| event `media_status` | evidence task `status` | count |
| --- | --- | ---: |
| failed | pending | 67 |
| ready | pending | 81 |
| not_implemented | pending | 10 |
| not_implemented | null | 207 |

The status mismatch is broader than the single lab failure: even ready and
failed evidence events commonly leave `evidence_tasks.status=pending`. Phase 1
must therefore update both event media state and evidence task state.

## Lab Failure Anchor

The lab source did produce an intrusion event and record request, but evidence
failed before Replay job creation:

- `event_id=eee6b526-4341-428b-9508-0638414f1c8f`
- `source_id=source_00000000-0000-4000-8000-781078565686`
- event time: 2026-06-15 07:48:47 UTC / 15:48:47 CST
- event `media_status=failed`
- event `payload.media.clip_status=failed`
- event `payload.media.error_message=retry_budget_exhausted reason=post_savant_frame_proof retries=12 error=missing_post_savant_frame_pts_window source_id=source_00000000-0000-4000-8000-781078565686`
- linked `evidence_tasks.task_id=357b4d8f-a9b8-51d9-8bcf-933501a61c1e`
- linked `evidence_tasks.status=pending`

This is the concrete failure this repair must make visible and diagnosable in
8090.

## Recent Ready Evidence Latency

The last 10 ready events were all `primary_rtsp`. Event creation to ready update
was roughly 44-65 seconds:

| event_id | event_type | create-to-ready seconds | task_status |
| --- | --- | ---: | --- |
| `d44f2451-d916-4c9f-bc72-858ee54d3d5b` | watchlist_hit | 47.7 | pending |
| `545d6a8f-e386-4270-a006-a721f4f293ac` | intrusion | 44.2 | pending |
| `8a124e3a-6bf4-4f58-a264-272c3768a538` | intrusion | 45.9 | pending |
| `0e6c6519-147e-4c7f-9b73-98d83436c90c` | intrusion | 50.3 | pending |
| `e0fac7ae-a6b5-422b-b0fa-d6b05ffeee1d` | intrusion | 46.0 | pending |
| `1ad0685e-22dc-45ac-89f6-8fb5cc159c55` | intrusion | 47.0 | pending |
| `fe12e82e-f508-40b8-acf1-2d14b6a5b023` | watchlist_hit | 45.3 | pending |
| `47b99e32-ce2e-4c57-aadf-bd9cf22b5b40` | intrusion | 49.6 | pending |
| `6a2d98d2-c2ab-49ad-8ba7-e7dceb89723a` | intrusion | 46.7 | pending |
| `a014aca9-fb77-4331-9c22-29c88f242aae` | watchlist_hit | 64.8 | pending |

## Recent Bundle Shape

The latest 10 evidence bundles had the expected modern crop/timeline fields:

- `time_domain_crop_applied=true`
- `video_crop.crop_video_to_time_window=true`
- `video_crop.method=ffmpeg_segment_normalized_transcode`
- `decoded_video_frame_count=240`
- `timeline_reconciliation_status=metadata_time_aligned_sparse_sidecar`

This keeps the current repair focused on evidence state, proof waiting, and
queue semantics before overlay changes.

## Phase 1-3 Implications

- Phase 1 must introduce an explicit evidence state visible from 8090 and keep
  `events` and `evidence_tasks` synchronized.
- Phase 2 must replace proof waiting via Redis redelivery with bounded local
  polling and include same-source/latest-frame/session diagnostics on terminal
  proof failure.
- Phase 3 must ensure `CLIP_WORKER_MAX_CONCURRENT_JOBS` only gates active
  Replay/finalization work. Proof waiting and queueing must not become terminal
  retry-budget failures.
