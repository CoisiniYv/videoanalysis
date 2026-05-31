# P1c.1 Controlled Event Recording Policy

Status: POC only.

## Problem

P1c-RTSP proved the media path, but the first accepted run was not a controlled
recording policy. The event-worker treated every intrusion event with
`clip_required=true` as a recordable event, and the clip-worker created one
Replay job for every record_request.

Observed before this fix:

- security.events: more than one event during the smoke run.
- record_requests: one per recordable intrusion event.
- Replay jobs: one per record_request.
- sink output dirs: `751` under
  `/data/video-analytics/media/replay-sink-output/p1c-rtsp/1780219194094704418`.
- sink videos: `751` `video.mov` files in the same P1c run output.
- evidence dirs: the old `/media/evidence` output was removed before this
  audit, but the sink output count shows uncontrolled clip generation.
- source adapter after smoke: stopped manually after the user asked to pause
  Docker output.

Root cause:

- event-worker had no source/event/max-request/cooldown recording gate.
- record_request deduplication was based only on event clip_status and did not
  check Redis for an existing `source_event_id + strategy` request.
- clip-worker had no max-job or per-camera cooldown limit.
- Replay job payload used `frame_count` instead of the requested
  `ts_delta_sec.max_delta_sec` stop condition.
- smoke did not stop the source adapter immediately after the first evidence
  bundle.

## P1c.1 Policy

The P1c RTSP POC is one-shot by default:

- `max_events=1`
- `max_record_requests=1`
- `max_replay_jobs=1`
- `max_evidence_bundles=1`

The P1c compose enables:

- `RECORDING_EVENT_TYPES=intrusion`
- `RECORDING_SOURCE_ID=p1c_rtsp_replay`
- `RECORDING_MAX_REQUESTS_PER_RUN=1`
- `RECORDING_COOLDOWN_SECONDS=30`
- `CLIP_WORKER_MAX_JOBS_PER_RUN=1`
- `CLIP_WORKER_MAX_CONCURRENT_JOBS=1`
- `CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS=30`
- `REPLAY_STOP_CONDITION_MODE=ts_delta_sec`

Events that do not pass the recording gate may still be inserted into
PostgreSQL, but they must not generate additional Replay jobs or evidence
bundles.

## Replay Job

Preferred stop condition:

```json
{
  "offset": {"seconds": 5},
  "stop_condition": {
    "ts_delta_sec": {"max_delta_sec": 10}
  }
}
```

`frame_count` is allowed only as an explicit fallback and must include a
`fallback_reason` in the stored Replay job request.

## Smoke Enforcement

`scripts/smoke/check_p1c_rtsp_replay_event_evidence_bundle.sh` now:

- clears only P1c RTSP POC sink output and P1c evidence bundles,
- uses a fresh P1c Redis container instead of deleting streams after workers
  start,
- checks Replay TTL from `modules/savant_replay/config.p1c_rtsp_inline.json`,
- stops `source-adapter` after the first evidence bundle,
- fails with `uncontrolled_clip_generation` if more than one evidence bundle is
  generated.

Boundaries remain unchanged:

- no local file source
- no source extraction fallback
- no second RTSP pull
- no production compose change
- no annotated_clip
- no media artifacts committed
