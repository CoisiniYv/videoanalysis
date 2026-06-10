# 14_replay_evidence_duration_guard_fix.md

## 1. Goal

Fix the midterm Replay evidence path so intrusion and watchlist evidence clips
cannot publish overlong raw clips when time-domain crop fails.

The target production behavior is fail-closed:

- successful evidence clips are centered around the requested event window;
- failed crop or mismatched metadata produces a diagnostic failure bundle, not a
  user-visible overlong clip;
- restart or source epoch changes cannot silently mix stale Replay output with a
  new event window.

## 2. Current Failure

The current runtime can produce both good and bad intrusion bundles in the same
configuration.

Good examples:

```text
df07fb8b... duration=10.01s crop_applied=true production_ready=true
b5242c9c... duration=9.97s  crop_applied=true production_ready=true
52851820... duration=9.97s  crop_applied=true production_ready=true
```

Bad examples:

```text
e99262d0... duration=61.35s crop_applied=false crop_failed=true
2d9fca4a... duration=61.35s crop_applied=false crop_failed=true
72c875ea... duration=61.35s crop_applied=false crop_failed=true
2c4560ed... duration=31.28s crop_applied=false crop_failed=true
49057d6d... duration=31.28s crop_applied=false crop_failed=true
13e7ac35... duration=84.15s crop_applied=false crop_failed=true
```

The failing summary records:

```text
time_domain_crop_error=ValueError:time_domain_crop_selected_zero_metadata_frames
video_crop.method=copy
video_crop.crop_video_to_time_window=false
```

The direct bug is that `media-worker` catches the crop-selection failure and
copies the source Replay sink output to the evidence `raw_clip.mov`.

## 3. Non-Goals

This fix must not:

- replace Savant Replay with a custom recorder;
- change behavior-rule intrusion detection logic;
- broaden operator frontend behavior beyond hiding or marking invalid evidence;
- change face recognition, gallery, or watchlist matching semantics;
- rely on a mock or local-file fallback for production evidence.

## 4. Required Runtime Contract

For post-Savant Replay evidence, a bundle is production-ready only when all of
these conditions are true:

```text
time_domain_crop_applied=true
clip_duration_seconds <= requested_duration_s + duration_slack_s
event_pts_inside_clip=true
event_centered_in_clip=true
annotations.frame_cache.identity.jsonl has at least one displayable row
metadata_path_used matches the final raw_clip.mov timeline
```

Default values:

```text
requested_duration_s = DEFAULT_PRE_SECONDS + DEFAULT_POST_SECONDS
duration_slack_s = EVIDENCE_MAX_DURATION_SLACK_SEC for legacy diagnostics only
production_duration_slack_s = 1.0 unless explicitly overridden
```

The production path should use a tight slack. The legacy
`EVIDENCE_MAX_DURATION_SLACK_SEC=10` is too large to catch the current failure
class when a 10-second clip becomes 31 seconds.

## 5. Fix Plan

### 5.0 Current Implementation Status

Implemented in the current working tree:

- `frame_cache_time_domain_crop_failed` no longer publishes the full Replay sink
  video as evidence `raw_clip.mov`.
- failed crop deletes any partial `raw_clip.mov`, returns `raw_clip_path=""`,
  and marks the bundle `clip_status=duration_guard_failed`.
- post-Savant finalization applies a requested-window duration guard before DB
  payload update.
- post-Savant finalization applies a sink metadata window guard before DB
  payload update. The guard fails closed when sink metadata does not cover the
  requested window, the event PTS is outside the final clip, the event is not
  centered, PTS values are non-monotonic, or a PTS gap exceeds
  `POST_SAVANT_MAX_PTS_GAP_SEC` seconds.
- zero selected metadata frames no longer fall back to a blind
  `requested_window_from_clip_start` crop.
- the guard uses `POST_SAVANT_DURATION_GUARD_SLACK_SEC`, falling back to
  `EVIDENCE_PRODUCTION_DURATION_SLACK_SEC`, then to a 1-second default.
- sink window edge tolerance defaults to `POST_SAVANT_WINDOW_EDGE_SLACK_SEC=0.75`.
- sink metadata max PTS gap defaults to `POST_SAVANT_MAX_PTS_GAP_SEC=2.0`.
- overlong final clips are removed from the evidence bundle and written as
  diagnostic failure bundles.
- focused pytest coverage exists in
  `harness/tests/test_midterm_replay_evidence_duration_guard.py`.

Still open:

- restart/source epoch isolation;
- runtime smoke script with a stable pass marker.

### 5.1 Media-worker fail-closed crop behavior

Change `services/media-worker/app/worker.py` so
`frame_cache_time_domain_crop_failed` does not copy the source Replay sink video
into the published evidence `raw_clip.mov`.

Status: implemented for the frame-cache finalizer path.

Expected behavior:

- Preserve diagnostic `summary.json` and `metadata.json`.
- Set `clip_status=duration_guard_failed` or another explicit failure status.
- Set `production_ready=false`.
- Set `overlay_available=false`.
- Do not expose the raw source Replay output as the evidence clip.
- Record `time_domain_crop_failed=true` and `time_domain_crop_error`.

Implementation options:

- write no `raw_clip.mov` for failed crop and store `raw_clip_path=null`; or
- write the source Replay path only under a diagnostic-only field that frontend
  routes never serve as evidence.

The first option is preferred.

### 5.2 Final duration guard

Add a final guard after any crop or copy operation and before event DB update.

Status: implemented for post-Savant finalization using the requested PTS
window and tight production slack.

The guard must compare the decoded or probed final clip duration with the
requested window:

```text
max_allowed_duration_seconds =
  requested_duration_s + production_duration_slack_s
```

If the final clip exceeds the limit:

```text
clip_status=duration_guard_failed
duration_guard_failed=true
duration_guard_status=failed
production_ready=false
frontend_overlay_required=false
```

The DB payload must not advertise the failed raw clip as ready evidence.

### 5.3 Event-window proof guard

For post-Savant Replay evidence, duration alone is not enough. The final bundle
must also prove the event is inside and centered in the final clip.

Status: still open as an explicit final DB-bundle guard. Existing sidecar
summary generation may record event-window proof fields. The current
implementation now adds a final sink metadata window guard for
`event_pts_inside_clip` and `event_centered_in_clip`; the remaining gap is a
true runtime epoch identity carried across source, Replay labels, sink metadata,
and frame-cache rows.

Required checks:

```text
event_pts_inside_clip=true
abs(event_projected_t_s - DEFAULT_PRE_SECONDS) <= event_center_tolerance_seconds
```

If either check fails, fail closed with the same status family as the duration
guard.

### 5.4 Runtime epoch isolation

Introduce a runtime epoch guard so a Replay job cannot combine event metadata
from one source/Savant lifetime with Replay, frame-cache, or video-file-sink
metadata from another lifetime.

Status: design locked here; implementation still open.

#### 5.4.1 Definitions

`runtime_epoch_id` is the authoritative identifier for one runtime lifetime of
the midterm inference stack.

It changes whenever any of these operations occur:

- 8090 `POST /api/v1/cameras/runtime/apply`;
- manual Savant or source-adapter restart;
- manual video-file-sink or media-worker restart used as part of evidence path
  recovery;
- source set changes that recreate one or more adapters.

Recommended format:

```text
midterm-YYYYMMDDTHHMMSSZ-<8hex>
```

The value must be a safe single path segment:

```text
[A-Za-z0-9_.-]+
```

It must not be inferred from file modification time, container creation time, or
event id. Those values are diagnostic only.

#### 5.4.2 Epoch owner

The runtime apply service is the owner of epoch creation. A manual restart script
must call the same epoch creation helper instead of inventing its own id.

Authoritative state:

```text
/data/video-analytics/media/replay-sink-output/midterm/.current_epoch.json
Redis key: video_analytics:midterm:runtime_epoch
```

The JSON file is for host/container inspection. Redis is for workers that do not
mount `/data/video-analytics/media`.

Example:

```json
{
  "runtime_epoch_id": "midterm-20260610T134500Z-a1b2c3d4",
  "created_at": "2026-06-10T13:45:00Z",
  "created_by": "api.runtime_apply",
  "reason": "camera_runtime_apply",
  "previous_runtime_epoch_id": "midterm-20260610T131500Z-00aa11bb"
}
```

#### 5.4.3 Directory layout

`video-file-sink` output must be scoped by epoch:

```text
/media/replay-sink-output/midterm/epochs/{runtime_epoch_id}/%source_id%/%src_filename%/
```

Host path:

```text
/data/video-analytics/media/replay-sink-output/midterm/epochs/{runtime_epoch_id}/...
```

`media-worker` must scan only the active epoch root:

```text
SINK_OUTPUT_DIR=/media/replay-sink-output/midterm/epochs/{runtime_epoch_id}
```

The old flat root remains read-only for diagnostics during migration:

```text
/media/replay-sink-output/midterm/replay-event-*
```

No production finalizer may package evidence from the flat root once epoch mode
is enabled.

#### 5.4.4 Service propagation contract

The same `runtime_epoch_id` must be present in every handoff:

```text
Savant frame annotations: data.runtime_epoch_id
Savant behavior events: payload.runtime_epoch_id
event-worker persisted event payload: runtime_epoch_id
event-worker record_request: runtime_epoch_id
clip-worker Replay job labels: runtime_epoch_id
clip-worker resulting_stream_id: replay-{runtime_epoch_id}-event-{event_id}
video-file-sink metadata labels: runtime_epoch_id
media-worker sidecar summary: runtime_epoch_id
media-worker event DB payload: media.runtime_epoch_id
```

The existing `event_id` remains the evidence bundle identity:

```text
/media/evidence/{event_id}
```

Do not put `runtime_epoch_id` into the final evidence bundle path. It is a
provenance and validation field, not the user-visible evidence id.

#### 5.4.5 Validation contract

For a bundle to become `clip_status=ready`, all epoch fields that exist must
match:

```text
event.payload.runtime_epoch_id
record_request.runtime_epoch_id
replay_job.labels.runtime_epoch_id
sink_metadata.labels.runtime_epoch_id
current_epoch.runtime_epoch_id
```

Strict mode is the production default:

```text
EVIDENCE_RUNTIME_EPOCH_STRICT=true
```

In strict mode, a missing epoch field is a failure, not a warning:

```text
clip_status=duration_guard_failed
duration_guard_failed=true
duration_guard_status=failed
production_ready=false
epoch_guard_failed=true
epoch_guard_reason=missing_or_mismatched_runtime_epoch
```

During a migration window, compatibility mode may allow legacy bundles only when
all of these are true:

- the sink output is under the current epoch root;
- no competing sink metadata for the same `event_id` exists in an older epoch;
- the final duration and event-window guards pass.

Compatibility mode must be explicit:

```text
EVIDENCE_RUNTIME_EPOCH_STRICT=false
```

#### 5.4.6 Runtime apply restart order

Runtime apply must rotate the epoch before any new evidence-producing work can
start.

Required order:

```text
1. acquire runtime-apply lock
2. stop or pause media-worker and clip-worker
3. generate runtime_epoch_id and write .current_epoch.json plus Redis key
4. create /media/replay-sink-output/midterm/epochs/{runtime_epoch_id}
5. restart or recreate video-file-sink with epoch-scoped DIR_LOCATION
6. write Savant camera config with runtime_epoch_id available to pyfuncs
7. restart Savant and recreate dynamic source adapters
8. wait for fresh frame annotations that carry runtime_epoch_id
9. resume clip-worker
10. resume media-worker with epoch-scoped SINK_OUTPUT_DIR
11. release runtime-apply lock
```

If the controller cannot safely restart `video-file-sink` with a new
`DIR_LOCATION`, runtime apply must fail before restarting Savant. Partial epoch
rotation is worse than no rotation because it can hide stale-output mixing.

#### 5.4.7 Old epoch cleanup

Old epoch data is not evidence. It is intermediate Replay sink output.

Cleanup policy:

- never delete `/data/video-analytics/media/evidence` as part of epoch rotation;
- never delete `/data/video-analytics/replay-midterm` as part of epoch rotation;
- old `replay-sink-output/midterm/epochs/{epoch}` directories may be moved to
  trash or hard-deleted only when `{epoch} != current_epoch`;
- default retention should be time and size bounded:

```text
REPLAY_SINK_EPOCH_RETENTION_HOURS=24
REPLAY_SINK_EPOCH_MAX_BYTES=20GiB
REPLAY_SINK_EPOCH_DELETE_MODE=trash
```

Trash target:

```text
/data/video-analytics/media/.trash/replay-sink-output/{cleanup_job_id}/{runtime_epoch_id}
```

Cleanup must use the same path-safety rules as storage maintenance:

- epoch id is a single safe segment;
- no `.` or `..` full path segments;
- resolved path must stay under
  `/data/video-analytics/media/replay-sink-output/midterm/epochs`.

#### 5.4.8 Operational fallback before implementation

Until epoch mode is implemented, safe manual recovery is:

```text
docker compose -f infra/docker-compose.midterm.yml stop clip-worker media-worker video-file-sink
move /data/video-analytics/media/replay-sink-output/midterm to a timestamped trash directory
docker compose -f infra/docker-compose.midterm.yml up -d video-file-sink clip-worker media-worker
POST http://127.0.0.1:8090/api/v1/cameras/runtime/apply
```

Do not delete formal evidence bundles in this fallback. Evidence deletion must
continue to use the 8090 storage-maintenance preview and execute flow.

#### 5.4.9 Implementation phases

Implement epoch isolation in small, reversible phases.

Phase A: passive provenance.

- runtime apply generates `runtime_epoch_id`;
- `.current_epoch.json` and Redis epoch key exist;
- workers log the current epoch but do not reject legacy records.

Phase B: sink namespace.

- `video-file-sink` writes to the epoch-scoped directory;
- `media-worker` scans only the epoch-scoped directory;
- old flat sink output is ignored by production finalization.

Phase C: end-to-end labels.

- Savant frame annotations and behavior events include `runtime_epoch_id`;
- event-worker persists and forwards it;
- clip-worker labels Replay jobs with it;
- media-worker writes it into sidecar summaries and event DB payloads.

Phase D: strict guard.

- enable `EVIDENCE_RUNTIME_EPOCH_STRICT=true`;
- missing or mismatched epoch fails closed;
- runtime smoke proves stale previous-epoch sink output cannot become ready
  evidence.

Phase E: retention.

- add old-epoch preview and cleanup to storage maintenance;
- default cleanup mode is trash;
- current epoch and formal evidence remain undeletable by epoch cleanup.

### 5.5 Replay job request tightening

Keep Savant Replay as the official replay primitive, but do not treat Replay
duration as the business evidence duration.

Recommended changes:

- continue using keyframe anchor plus offset for decodable replay start;
- prefer a stop condition that is tied to the post-window proof when available;
- always perform media-worker final crop and validation;
- fail closed if Replay output does not cover the requested event window.

## 6. Tests

Add focused tests before changing runtime behavior.

### 6.1 Unit tests

Add or update media-worker tests for:

1. `time_domain_crop_selected_zero_metadata_frames` does not publish source
   Replay output as `raw_clip.mov`.
2. A final clip longer than `requested_duration_s + slack` is marked
   `duration_guard_failed`.
3. A clip with event PTS outside final metadata fails even when duration is
   short enough.
4. A good crop preserves `clip_status=ready` and writes normal sidecar outputs.

Current coverage: items 1, 2, and a PTS-gap variant of item 3 are covered by
`harness/tests/test_midterm_replay_evidence_duration_guard.py`. Item 4 remains
open.

Add epoch-isolation unit tests for:

1. epoch id generation rejects unsafe path segments, including `.`, `..`, and
   values containing `/`;
2. runtime apply writes `.current_epoch.json` atomically and updates Redis with
   the same value;
3. clip-worker copies `runtime_epoch_id` from the record request into Replay job
   labels and the resulting stream id;
4. media-worker ignores metadata under an older epoch root even when the
   `event_id` matches a current event;
5. media-worker fails closed when event, record request, Replay labels, sink
   metadata, or current epoch disagree;
6. cleanup refuses to move or delete the current epoch and refuses paths outside
   the epoch root.

### 6.2 Contract tests

Add harness coverage for:

```text
summary.time_domain_crop_applied=true for ready evidence
summary.production_ready=true only when crop/event-window checks pass
metadata.status.clip_status != ready when duration_guard_failed=true
frontend payload does not expose failed raw_clip_path as ready evidence
ready evidence includes media.runtime_epoch_id
runtime_epoch_id mismatch never produces clip_status=ready
runtime apply does not expose api:8000 on the host
```

Compose/static contract coverage must assert:

```text
video-file-sink DIR_LOCATION is epoch-scoped when epoch mode is enabled
media-worker SINK_OUTPUT_DIR points at the current epoch root
evidence-viewer keeps /data/video-analytics/media/evidence mounted read-only
8090 remains the only operator entry
```

### 6.3 Runtime smoke

Create or extend a current smoke script that:

1. Starts the midterm stack with a newly generated runtime epoch.
2. Waits for intrusion events.
3. Finds the latest intrusion evidence bundles.
4. Runs `ffprobe` on each ready `raw_clip.mov`.
5. Fails if any ready evidence exceeds the requested duration plus slack.
6. Fails if any ready evidence has `time_domain_crop_applied=false`.
7. Creates or preserves a stale sink output in a previous epoch with the same
   `event_id` and proves media-worker ignores it.
8. Restarts Savant through 8090 runtime apply and proves the epoch changes.
9. Proves new ready evidence carries the new `runtime_epoch_id`.

Suggested marker:

```text
PASS_MIDTERM_REPLAY_EVIDENCE_DURATION_GUARD
PASS_MIDTERM_REPLAY_EVIDENCE_EPOCH_ISOLATION
```

## 7. Acceptance Criteria

The fix is complete only when all of these are true:

- No `ready` or frontend-visible evidence clip exceeds the configured event
  window plus production slack.
- Crop failure produces a diagnostic failure state, not a published full Replay
  clip.
- Restart after source/Savant reset does not cause stale sink output to be
  packaged as current evidence.
- Every ready post-Savant Replay evidence bundle records a non-empty
  `runtime_epoch_id`.
- `media-worker` scans only the active epoch root in production mode.
- Old epoch cleanup never touches formal evidence bundles or Replay RocksDB.
- 8090 runtime apply rotates epoch state before restarting Savant/source
  adapters.
- Existing successful 10-second intrusion and watchlist clips still pass.
- Targeted pytest and the runtime smoke marker pass.

## 8. Verification Commands

Expected verification set:

```bash
pytest -q harness/tests/test_midterm_replay_evidence_duration_guard.py
pytest -q harness/tests/test_frame_annotation_event_window.py
pytest -q harness/tests/test_midterm_replay_epoch_isolation.py
python -m py_compile services/media-worker/app/worker.py harness/tests/test_midterm_replay_evidence_duration_guard.py
python -m py_compile services/api/app/services/runtime_apply.py services/clip-worker/app/worker.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm-compose-config.yml
git diff --check
```

Broader follow-up verification before runtime rollout:

```bash
bash scripts/smoke/current/check_midterm_deployment.sh
bash scripts/smoke/current/check_midterm_replay_evidence_duration_guard.sh
```

If test names move during implementation, update this section in the same
change that adds the guard.
