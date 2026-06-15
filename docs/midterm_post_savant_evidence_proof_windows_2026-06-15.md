# Midterm Post-Savant Evidence Proof Window Fix - 2026-06-15

## Summary

This document records the 2026-06-15 fix for midterm events that were detected
but did not reliably become evidence bundles.

The runtime symptom looked like "evidence is random": some events reached
`record_request`, but `clip-worker` failed them with
`missing_post_savant_frame_pts_window` before creating a Replay job. The direct
failure was not Redis backlog and not 8090 refresh. It was an overly strict
post-Savant frame proof window around stream session boundaries.

Commit:

```text
6620f6c Stabilize post-Savant evidence proof windows
```

## Problem Split

There are two separate failure classes and they must not be mixed:

1. **No upstream event.**
   The camera is live and frame annotations may be increasing, but Savant/rules
   do not produce an intrusion event or `record_request`. `clip-worker` cannot
   create evidence for events that do not exist. This remains the likely class
   for the lab camera when a person walks through the view but no event appears.

2. **Event exists but evidence fails.**
   `event-worker` emits a `record_request`, but `clip-worker` cannot prove the
   requested post-Savant frame window and fails before Replay job creation. This
   was observed on `primary_rtsp` and is what this fix targets.

## Root Cause

`clip-worker` required both sides of the evidence window to be proven inside the
same `runtime_epoch_id` and `stream_session_id`:

- start-window / pre-roll proof near `requested_start_pts`;
- post-window proof at or after `requested_end_pts`.

When the requested five-second pre-window crossed a stream session boundary,
current-session post-window proof could exist while the requested start-window
frame only existed in the previous session. The old behavior failed the event
instead of producing a shorter, auditable evidence clip.

A separate edge was also confirmed: post-window proof can legitimately arrive in
a different stream session after a source reset. If the PTS window and source
identity match, this should be visible and auditable rather than silently
failing.

## Implemented Behavior

`clip-worker` now supports two explicit post-Savant proof fallbacks:

- `POST_SAVANT_ALLOW_CROSS_SESSION_POST_WINDOW_PROOF=true`
  allows a post-window proof frame from another stream session when the
  runtime/source/camera and requested PTS window match.

- `POST_SAVANT_ALLOW_TRUNCATED_PRE_WINDOW_PROOF=true`
  allows evidence creation when the full pre-window crosses a stream session
  boundary but the current session has a provable decodable point before the
  event. The clip uses an effective start PTS and records the truncation.

The Replay job labels now preserve both audit and execution windows:

```text
original_requested_start_pts
effective_start_pts
requested_start_pts
requested_end_pts
requested_pre_window_seconds
effective_pre_window_seconds
pre_window_truncated_seconds
pre_window_truncated
pre_window_policy
frame_domain_session_policy
frame_domain_proof_method
```

Important policy:

- do not silently mark incomplete evidence as complete;
- do not drop full-rate evidence storage upstream;
- do not use cross-session frames unless the proof policy marks it explicitly;
- if proof is missing forever, fail quickly with diagnostics instead of hiding
  behind Redis redelivery.

## Media-Worker Alignment

`media-worker` now interprets `effective_start_pts` as the crop and guard start
for truncated evidence. It keeps `original_requested_start_pts` in summaries and
metadata so the operator can see that pre-roll was shortened.

The final bundle duration guard and sink-window guard are evaluated against the
effective window. This prevents a valid truncated clip from being marked failed
only because it cannot include frames from a previous stream session.

The frame-cache sidecar reader remains strict by default. It reads multiple
stream sessions only when Replay labels explicitly prove and mark a
cross-session post-window policy.

## Runtime Validation

Applied to runtime by recreating only Python workers:

```bash
docker compose --env-file infra/env/midterm.env \
  -f infra/docker-compose.midterm.yml \
  up -d --force-recreate --no-deps clip-worker media-worker
```

No Savant, Replay, or source-adapter restart was required.

Observed after applying:

```text
event_id=4cb41d57-4580-429a-8ab1-130b5df5f415
source_id=primary_rtsp
state=ready
reason=replay_job_created
clip_path=/media/evidence/4cb41d57-4580-429a-8ab1-130b5df5f415/raw_clip.mov
```

Runtime counters for events after `2026-06-15T11:17:14Z`:

```text
failed=0
missing_proof=0
ready=1
redis security.record_requests pending=0
redis security.record_requests lag=0
```

The ready bundle showed:

```text
production_ready=True
raw_clip_duration=10.0
expected_duration_seconds=10.0
duration_guard_status=passed
sink_window_guard_status=passed
time_domain_crop_applied=True
```

Container state after validation:

```text
clip-worker RestartCount=0 Status=running
media-worker RestartCount=0 Status=running
savant RestartCount=0 Status=running
replay-service RestartCount=0 Status=running
source-adapter Status=running
```

## Verification Commands

Local verification run:

```bash
python -m py_compile \
  services/clip-worker/app/config.py \
  services/clip-worker/app/worker.py \
  services/media-worker/app/worker.py \
  services/media-worker/app/post_savant_evidence_bundle.py

pytest -q \
  harness/tests/test_runtime_overview_api.py \
  harness/tests/test_operator_runtime_overview_static.py \
  harness/tests/test_api_event_evidence_camera_name.py \
  harness/tests/test_post_savant_evidence_bundle_crop.py \
  harness/tests/test_midterm_replay_duration_tuning.py \
  harness/tests/test_midterm_replay_evidence_duration_guard.py \
  harness/tests/test_clip_worker_queue_safety.py \
  harness/tests/test_media_worker_perf_safety.py \
  harness/tests/test_midterm_stream_session_isolation.py \
  harness/tests/test_midterm_replay_epoch_isolation.py

docker compose --env-file infra/env/midterm.env \
  -f infra/docker-compose.midterm.yml config

git diff --check
```

Result:

```text
55 passed
compose config passed
git diff --check passed
```

## Follow-Up

This fix closes the "event exists but evidence fails at proof window" path.

It does not prove that the lab camera always produces intrusion events. For lab
"person walked through but no evidence" reports, first check whether a new
event and `record_request` were created. If not, debug upstream detection and
rules:

- per-source person/pose object counts;
- ROI and intrusion rule geometry;
- confidence thresholds and camera angle;
- 8090 per-source detector health and recent event rate.

Future production readiness should keep this distinction visible in 8090:

- no upstream event: detector/rule health problem;
- event waiting proof: evidence pipeline in progress;
- proof failed: evidence proof/window problem;
- ready: production-ready evidence exists.
