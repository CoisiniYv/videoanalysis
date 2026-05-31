# Phase P1 Single-Stream Replay Clip Output

Status: BLOCKED on Replay inline topology.

## Step 1 Inventory

| Check | Result |
|---|---|
| Current replay-service compose exists | Yes. `infra/docker-compose.phase3a.yml` and `infra/docker-compose.phase3b.yml` define `replay-service`. They are legacy POC stacks. |
| Current video-file-sink compose exists | Yes. Phase 3A/3B define Replay job sinks; C1/Phase 3H define Savant-output continuous sinks. |
| Current clip-worker can consume `security.record_requests` | Yes. `services/clip-worker/app/worker.py` consumes the stream, finds or uses a keyframe, creates Replay REST jobs, and writes status to DB. P1 adds `previous_keyframe_uuid` preference and stores the Replay job request. |
| Current media-worker exists and can organize sink output | Yes for legacy `clip_status=ready`. P1 adds an opt-in raw evidence finalizer with `P1_RAW_CLIP_FINALIZER_ENABLED=true`. |
| Current events table has `clip_path` / `snapshot_path` | Yes. The columns exist in `db/migrations/001_init.sql`, `002_phase2e_events.sql`, and `008_r3_unified_event_evidence_algorithm_rules.sql`. |
| Current `events.payload.media` can record strategy/status | Yes. Existing code stores `recording_strategy`, `clip_status`, `replay_job_id`, and errors in `payload.media`. P1 adds `evidence_dir`, `metadata_path`, `event_annotation_path`, and `replay_job_request`. |
| Current Replay config `out_stream` | `null` in `modules/savant_replay/config.json`. |
| Existing Phase 3A/3B/3C Replay smoke reusable | Partially. Phase 3A/3B prove Replay REST job + Video File Sink mechanics, but not the P1 inline topology. Phase 3C is snapshot-focused and not sufficient for P1 raw clip evidence. |

## Required P1 Topology

```
test video / RTSP source
  -> source adapter
  -> replay-service
  -> savant-security
  -> Redis security.events
  -> event-worker
  -> security.record_requests
  -> clip-worker
  -> Replay REST job
  -> video-file-sink
  -> media-worker P1 raw finalizer
  -> /data/video-analytics/media/evidence/{event_id}/raw_clip.*
```

## Blocker

The required single-ingestion hop `source adapter -> replay-service -> savant-security`
is not currently configured. The only Replay config in the repo has:

```json
"out_stream": null
```

That means Replay is configured as an ingress/cache service, not as an inline
pass-through feeding Savant. Existing Phase 3A/3B compose files avoid this by
running Savant from a separate source/RTSP path. That violates the P1 boundary:
Replay and Savant must not pull or receive independent source streams.

## What Was Added

- `infra/docker-compose.p1-replay-clip-poc.yml`
  - POC only.
  - Defines redis, postgres, optional api, event-worker, clip-worker,
    media-worker, replay-service, video-file-sink, savant-security, and
    source-adapter.
  - Documents the current inline topology blocker directly in the compose.
- `scripts/smoke/check_p1_single_stream_replay_clip_output.sh`
  - Fails as `BLOCKED` when Replay `out_stream` is `null`.
  - Does not fall back to source extraction.
  - Does not start a second RTSP/source pull.
- `harness/tests/test_p1_replay_clip_contract.py`
  - Covers record_request schema, Replay job payload, event annotation schema,
    metadata/evidence bundle behavior, generated/failed/blocked states, and
    source extraction fallback prohibition.
- Minimal worker updates:
  - `event-worker` includes `previous_keyframe_uuid` in record requests.
  - `clip-worker` prefers `previous_keyframe_uuid` over `keyframe_uuid`.
  - `clip-worker` records full Replay job request parameters.
  - `media-worker` has an opt-in P1 raw finalizer.

## Record Request Contract

P1 record requests include:

- `event_id`
- `source_event_id`
- `source_id`
- `event_ts_ms`
- `frame_uuid`
- `keyframe_uuid`
- `previous_keyframe_uuid`
- `pre_seconds`
- `post_seconds`
- `strategy = savant_replay`
- `status = pending`

## Replay Job Contract

`clip-worker` uses the following anchor order:

1. `previous_keyframe_uuid`
2. `keyframe_uuid`
3. `Replay /api/v1/keyframes/find`

The created Replay job request is stored at:

```text
payload.media.replay_job_request
```

The job uses:

- `offset.seconds = DEFAULT_PRE_SECONDS`
- `stop_condition.frame_count = (pre_seconds + post_seconds) * 30`
- `sink.url = REPLAY_JOB_SINK_URL`
- `configuration.labels.event_id = event_id`

## Evidence Bundle Contract

When the P1 finalizer is enabled, Video File Sink output is copied to:

```text
/data/video-analytics/media/evidence/{event_id}/
  raw_clip.{mov|webm|mp4|mkv}
  metadata.json
  event_annotation.json
```

`event_annotation.json` uses:

```json
{
  "schema_version": "1.0",
  "annotation_type": "event_frame",
  "event": {
    "event_id": "...",
    "event_type": "intrusion",
    "camera_id": "...",
    "source_id": "...",
    "track_id": "...",
    "event_ts_ms": 0,
    "frame_uuid": "..."
  },
  "overlays": []
}
```

P1 intentionally does not generate `annotated_clip.mp4`.

## DB Contract

On success:

- `events.clip_path = /media/evidence/{event_id}/raw_clip.*`
- `payload.media.clip_status = generated`
- `payload.media.recording_strategy = savant_replay`
- `payload.media.evidence_dir = /media/evidence/{event_id}`
- `payload.media.metadata_path = /media/evidence/{event_id}/metadata.json`
- `payload.media.event_annotation_path = /media/evidence/{event_id}/event_annotation.json`

On Replay/keyframe/job failure:

- `clip_status = failed`
- `payload.media.error_message` is populated.

On topology inability:

- `clip_status = blocked` is the intended status if an event was already
  inserted and the pipeline cannot proceed.
- The P1 smoke reports `BLOCKED` before attempting runtime validation when
  inline Replay pass-through is unavailable.

## Explicit Boundaries

- No source extraction fallback.
- No V1 visual renderer as production evidence.
- No second RTSP/source pull.
- No generated `annotated_clip.mp4`.
- No DB migration in P1.
- No media artifacts committed.
- Existing feasibility files remain uncommitted and unchanged.

## Runtime Test Policy

P1 must follow `docs/runtime_test_policy.md`:

- Code changes → restart container, not default rebuild.
- Bind mount must be verified before runtime smoke.
- P1b-RTSP / P1c-RTSP must use fixed RTSP: `rtsp://10.37.57.112:8554/live/1080movie`.
- Replay must have TTL configured.
- POC containers must use explicit names when C1 is also running.

## Manual Trigger Fallback

Manual Replay REST invocation is allowed only for diagnosis and must be reported
as a manual trigger fallback. It is not a P1 PASS condition and must not replace
`event-worker -> record_request -> clip-worker`.
