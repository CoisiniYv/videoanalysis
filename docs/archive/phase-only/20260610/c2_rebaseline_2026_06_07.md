# C2 Rebaseline - Post-Savant Evidence Topology

Date: 2026-06-07

Status: C2-H rebaseline after C2.2 timeline failure and C2.2A FPS-gated probe.

## Current Decision

C1M small-fix work is stopped. The active evidence direction is C2: production evidence should be generated from a post-Savant stream where video frames and object metadata belong to the same frame domain.

The production annotation path must not rely on PostgreSQL wide-window reconstruction, Redis frame-cache backfill, or legacy `annotations.jsonl` auto fallback. PostgreSQL remains for business facts such as events, persons, face observations, and watchlist records. Redis remains a decoupling transport and must not carry raw image bytes. Legacy `annotations.jsonl` is explicit debug only.

The current topology direction is:

```text
RTSP source-adapter
-> Savant security module
-> post-Savant object metadata
-> Replay Service
-> video-file-sink output
-> production annotation sidecar
-> evidence-viewer dynamic overlay
```

Production evidence duration should be 10 seconds: 5 seconds before the event and 5 seconds after the event. The current C2 POC probes do not yet attach this to an event anchor.

## Completed Milestones

### C2.0

Commit: `efbf6493d3a7bf6d7f0ae3a744c3e6bff4c407a3`

Result: `PASS_C2_POST_SAVANT_REPLAY_METADATA_RETAINED`

Post-Savant Replay output to video-file-sink retained Savant object metadata in `metadata.json`, including person bbox, person track identity, pose keypoints, face bbox, and face landmarks.

Representative output:

```text
/data/video-analytics/media/c2-post-savant-replay-poc/c2-poc-20260607T105448%/unknown%/
```

Key observed counts from the retained metadata report:

```text
frames_count=48
frames_with_objects_count=48
person_objects_count=210
face_objects_count=210
keypoints_exists=true
landmarks_exists=true
frame_pts_exists=true
source_observation_id_exists=false
```

### C2.1

Commit: `ab2a9bac2357e59ad8ae2dfdc174d1ae5491329b`

Result: post-Savant `metadata.json` can be converted into a production sidecar. The builder emits `annotations.frame_cache.identity.jsonl` and summary metadata with `production_ready=true` when post-Savant objects exist.

Known limitation: ordinary face observations remain `unknown_face` / `observation_only`. The C2 sidecar does not yet contain identity binding or `source_observation_id`.

### C2.1B

Commit: `2431606dae481f1fd1de2c7ff4a1384eb01b027a`

Result: C2 evidence-viewer runtime verification was added to the post-Savant POC compose. HTTP verification on port 8090 confirmed production sidecar loading for the C2 viewer wrapper bundle.

Representative bundle:

```text
/data/video-analytics/media/evidence/c2_1_viewer_runtime_20260607T121530
```

Observed response summary:

```text
annotation_source=sidecar
annotation_source_kind=production_sidecar
production_ready=true
fallback_used=false
legacy_used_for_visual_binding=false
object_counts.person=142
object_counts.face=142
object_counts.known_face=0
```

### C2.1C

Commit: `99a3c14665a51f33415baa338474a17706683e2e`

Result: ordinary face observations from the production sidecar are visible in the evidence-viewer overlay. Person bbox, pose keypoints, face bbox, and face landmarks were visually aligned in the verified C2.1C samples.

No gallery identity binding or watchlist visual proof was implemented in C2.1C.

### C2.2

Result: `FAIL_C2_2_VISUAL_TIMELINE_MISALIGNED`

C2.2 attempted standard bundle packaging with simple canonical trimming from an older output:

```text
input_dir=/data/video-analytics/media/c2-post-savant-replay-poc/c2-poc-20260607T105448%/unknown%
output_dir=/data/video-analytics/media/evidence/c2_2_20260607T132851
original_metadata_frame_count=120
decoded_video_frame_count=106
sidecar_frame_count=106
trim_occurred=true
```

The early and middle frames aligned, but tail frames drifted. The failure means naive row trimming is not sufficient as a production timeline policy.

The failed C2.2 WIP code remains isolated in `/tmp/video-analytics-c2-poc` and is not committed.

### C2.2A

Result: `PASS_C2_2A_FPS_GATING_STABILIZES_TIMELINE`

A fresh 10 second FPS-gated post-Savant probe produced stable visual alignment without the previous tail drift.

Runtime-only FPS override:

```text
MAX_FPS_CONTROL=true
MAX_FPS=8/1
MIN_FPS=2/1
FACE_REID_MIN_INTERVAL_MS=2000
FRAME_ANNOTATION_INCLUDE_KEYPOINTS=full
SOURCE_ID=c2_post_savant_fps_probe
```

Representative output:

```text
/data/video-analytics/media/c2-post-savant-replay-fps-probe/c2-fps-probe-20260607T140420%/unknown%
```

Observed counts:

```text
metadata_rows=240
decoded_video_frames=240
video_duration=10.010013
metadata_fps_estimate=23.976030656950194
first_pts=9512622222
last_pts=19480911111
person_objects_count=187
face_objects_count=157
keypoints_objects_count=187
landmarks_objects_count=157
```

Important nuance: FPS gating was applied in Savant, but the emitted metadata row cadence still matched the encoded video cadence at about 23.976 FPS. Future C2.2 work must record input FPS, output metadata FPS, decoded video FPS, configured `MAX_FPS` / `MIN_FPS`, and whether FPS gating was applied. Do not assume `MAX_FPS=8/1` means sparse metadata rows.

## Current Known Limitations

- Identity binding is not implemented in C2.
- C2 sidecar faces still have `source_observation_id=null`.
- `known_face_count=0` is expected.
- Gallery match and watchlist visual proof are not implemented.
- Redis `security.face_observations` did contain C2.2A source observations for `source_id=c2_post_savant_fps_probe`, but no face-worker was running in the C2 POC stack, and the available PostgreSQL instance did not contain `face_observations`, `match_results`, or watchlist tables.
- C2.2 standard bundle flow is not committed.
- C2.2 failed WIP files remain isolated in `/tmp/video-analytics-c2-poc`.
- FPS gating must be treated as a production requirement.
- Timestamp-based annotation-to-video mapping remains the fallback if drift returns.
- The C2.2A 10 second probe was not yet event anchored as event-5s/event+5s.

## Identity Binding Status

C2 currently has ordinary face observations only. This is not evidence of face-recognition failure. The current C2 POC has not connected the face-worker, complete PostgreSQL face schema, pgvector gallery matching, match result persistence, or watchlist-hit identity path.

Current proven facts:

- C2 production sidecars contain ordinary face objects.
- `known_face_count=0` is expected before identity binding.
- The C2 POC compose does not currently include face-worker.
- The PostgreSQL instance observed during C2.2A did not contain the complete face schema needed for `face_observations`, `match_results`, gallery match records, or watchlist records.
- Redis `security.face_observations` exists and received C2.2A messages for `source_id=c2_post_savant_fps_probe`.
- Redis face observations include `source_observation_id`, `track_id` / `person_track_id`, `frame_pts`, face bbox, landmarks, and embedding data.
- Redis observation records and C2 sidecar face objects can be precisely joined with `frame_pts + track_id + bbox`.
- This join proves that C2 has usable keys for a future identity-binding merge.

Representative observed join:

```text
source_id=c2_post_savant_fps_probe
redis.source_observation_id=face:c2_post_savant_fps_probe:1:9721:1
redis.track_id=1
redis.frame_pts=9721166666
redis.face_bbox.cxcywh=[1132.8235, 540.4671, 74.3597, 73.6284]
sidecar.frame_index=5
sidecar.frame_pts=9721166666
sidecar.face.track_id=1
sidecar.face.bbox.xyxy=[1095.6436, 503.6529, 1170.0034, 577.2813]
```

Current unproven identity facts:

- face-worker consumption of C2 Redis observations;
- `face_observations` database writes for C2 observations;
- pgvector gallery match;
- match result persistence;
- `watchlist_hit`;
- `known_face` sidecar annotation;
- viewer identity proof.

Rules before C2.4:

- Do not mark ordinary face objects as `known_face`.
- Do not claim the visible people were matched by gallery search.
- Do not claim watchlist visual proof.
- Do not write temporary or fake `person_name` values for identity display.
- Viewer should display only `Face Observation` / `Unknown Face` for these C2 sidecar face objects.

C2.4 should implement identity binding by consuming face-worker / gallery match / watchlist results into an `identity_patch`, then merging that patch back into the matching sidecar face object using the proven join keys. The expected join should include `source_observation_id` when it is written back to the C2 sidecar, with `frame_pts + track_id + bbox` as the conservative verification guard.

## Docker Compose Rules

Canonical / allowed compose and env files:

```text
infra/docker-compose.c1-official-replay-dev.yml
infra/env/c1-official-replay-dev.env
infra/docker-compose.c2-post-savant-replay-poc.yml
infra/env/c2-post-savant-replay-poc.env
```

At the time of this rebaseline, the C2 compose/env files exist on the protected local branch `c2/post-savant-poc`, not on `master`.

Rules:

- C1 compose remains for old mainline regression only. Do not continue adding C1M fallback fixes.
- C2 compose is the canonical post-Savant Replay POC entrypoint.
- C2.2 and C2.3 should reuse the C2 POC compose rather than adding new full compose files.
- If a new env file is required, document why the existing env cannot be parameterized.
- Runtime-only overrides belong under `/tmp` and must not be committed.
- C2 evidence-viewer must be included in the C2 compose for HTTP runtime verification.
- Do not run `docker compose down` without listing affected services and getting explicit confirmation.
- Do not randomly stop containers during evidence inspection.
- Do not add endless phase-specific compose files.

## Runtime Inventory

Worktrees:

```text
/home/user/video-analytics               master at 13ade24ec8703034f50e672ab2e1e406dd3ceb3e
/tmp/video-analytics-c2-poc              detached at 99a3c14665a51f33415baa338474a17706683e2e
/tmp/video-analytics-c2-fps-probe        detached at 99a3c14665a51f33415baa338474a17706683e2e
/tmp/video-analytics-c1m-verify          detached at 3476ac3
/tmp/video-analytics-c1m-unblock-verify  detached at 13ade24
```

Protected C2 branch:

```text
c2/post-savant-poc -> 99a3c14665a51f33415baa338474a17706683e2e
```

Current running containers observed during C2-H:

```text
c2-poc-redis             Up, healthy, port 6395
c2-poc-replay-service    Up, port 8098
c2-poc-savant            Up, healthy
c2-poc-source-adapter    Up
c2-poc-video-file-sink   Up
phase0-postgres          Up, healthy, port 5432
```

Container ownership:

- Must keep running for current inspection: `c2-poc-*` containers if more C2.2A log/sample inspection is needed.
- Safe to stop later, but do not stop without confirmation: `c2-poc-*` containers after C2.2A inspection is no longer needed.
- Unknown ownership, do not touch without confirmation: `phase0-postgres`, started by older `phase0-dev`.
- Already stopped or not running in current C2 POC: C2 `evidence-viewer` service was defined but not running during the FPS probe; C1 mainline services were not running under `infra/docker-compose.c1-official-replay-dev.yml`.

## Artifact Inventory

Runtime media output that must not be committed:

```text
/data/video-analytics/media/c2-post-savant-replay-poc/c2-poc-20260607T104839%/unknown%
/data/video-analytics/media/c2-post-savant-replay-poc/c2-poc-20260607T105123%/unknown%
/data/video-analytics/media/c2-post-savant-replay-poc/c2-poc-20260607T105420%/unknown%
/data/video-analytics/media/c2-post-savant-replay-poc/c2-poc-20260607T105448%/unknown%
/data/video-analytics/media/evidence/c2_1_viewer_runtime_20260607T121441
/data/video-analytics/media/evidence/c2_1_viewer_runtime_20260607T121530
/data/video-analytics/media/evidence/c2_2_20260607T132851
/data/video-analytics/media/c2-post-savant-replay-fps-probe/c2-fps-probe-20260607T140420%/unknown%
```

Temporary files and visual samples that must not be committed:

```text
/tmp/c2-fps-only-probe.override.yml
/tmp/c2-fps-probe-module
/tmp/c2_1_visual_samples
/tmp/c2_1c_face_overlay_samples
/tmp/c2_2_visual_samples
/tmp/c2_2a_sidecar
/tmp/c2_2a_visual_samples
/tmp/c2_2a_stats.json
/tmp/c2_2a_latest_output_dir
/tmp/c2_2a_latest_metadata
/tmp/c2_2a_latest_video
/tmp/c2_2a_latest_report
```

Evidence examples worth keeping temporarily:

- C2.0 retained metadata output: `/data/video-analytics/media/c2-post-savant-replay-poc/c2-poc-20260607T105448%/unknown%`
- C2.1B viewer wrapper: `/data/video-analytics/media/evidence/c2_1_viewer_runtime_20260607T121530`
- C2.2A FPS-gated aligned output: `/data/video-analytics/media/c2-post-savant-replay-fps-probe/c2-fps-probe-20260607T140420%/unknown%`

Failed experiment output:

- C2.2 naive trim bundle: `/data/video-analytics/media/evidence/c2_2_20260607T132851`

Safe to delete later after confirmation:

- Older C2.0 repeated POC output dirs that are not referenced by current reports.
- `/tmp` visual samples and resolved config files after the rebaseline is no longer needed.

Never commit:

- `video.mov`
- `raw_clip.mov`
- `metadata.json`
- `sink_metadata.json`
- runtime evidence bundle directories
- screenshots
- `/tmp` probe files
- Docker logs

## Dirty WIP Inventory

Failed C2.2 bundle-packaging WIP remains in `/tmp/video-analytics-c2-poc`:

```text
M  harness/tests/test_c2_post_savant_metadata_annotation_builder.py
M  services/media-worker/app/post_savant_metadata_annotation_builder.py
?? harness/tests/test_c2_post_savant_evidence_bundle.py
?? scripts/tools/build_c2_post_savant_evidence_bundle.py
?? services/media-worker/app/post_savant_evidence_bundle.py
```

These files are failed C2.2 WIP and were not committed. Recommended handling when C2.2 resumes: use them only as reference, then either deliberately salvage scoped pieces or restart the bundle flow from the clean protected C2 branch. Do not auto-stash or delete without confirmation.

## Next Recommended Sequence

1. Resume C2.2 from the protected `c2/post-savant-poc` branch.
2. Use a fresh FPS-gated 10 second post-Savant output, not the old 120 metadata / 106 decoded-frame output.
3. Implement the standard evidence bundle format:

```text
raw_clip.mov
sink_metadata.json
annotations.frame_cache.identity.jsonl
summary.json
```

4. C2.2 summary should record:

```text
event_window_seconds=10
event_pre_roll_seconds=5
event_post_roll_seconds=5
input_fps
output_metadata_fps
decoded_video_fps
max_fps
min_fps
fps_gating_applied
original_metadata_frame_count
decoded_video_frame_count
sidecar_frame_count
```

5. If timeline drift returns, stop naive trimming and move to timestamp-based annotation-to-video mapping.
6. C2.3 should integrate event / clip / media-worker bundle flow.
7. C2.4 should implement `source_observation_id` / identity patch / gallery `known_face` binding and watchlist visual proof.
8. Later work should define production FPS/backpressure policy and multi-stream performance validation.
