# C2.3B-R2 Time-Domain Integrity Repair

## Scope

C2.3B-R2 repairs the C2.3B event evidence strategy without entering C2.4
identity binding. It does not change the Savant model pipeline, viewer identity
semantics, face-worker, gallery matching, PostgreSQL schema, DB annotation
fallback, or legacy `annotations.jsonl` fallback.

## Prior Diagnosis

C2.3B-R produced `FAIL_C2_3B_R_FRAME_COUNT_WINDOW_UNSAFE`.

- The failed event-style Replay request used `stop_condition.frame_count=240`.
- That frame count was treated as a 10 second evidence window.
- The failed output proved that assumption false: 240 metadata rows covered
  about 41 seconds at roughly 5.8 FPS.
- Fixed frame count is therefore prohibited as the C2 production window
  expression.

C2.3B-V produced `FAIL_C2_3B_V_REPLAY_VIDEO_INTEGRITY_BLOCKED`.

- The failed raw clip was about 41 seconds with 238 decoded frames and 240
  metadata rows.
- The first decoded frame was not a keyframe.
- The only keyframe was around 18.685 seconds.
- Packet durations had large jumps, including multi-second gaps.
- The contact sheet showed obvious visual corruption and cross-scene artifacts.
- A clean ffmpeg decode log was not sufficient to make the clip trustworthy.

## R2 Repair Policy

C2.3B-R2 forbids:

- using `stop_condition.frame_count=240` to mean 10 seconds;
- trimming metadata rows to hide decoded/metadata mismatch;
- using PostgreSQL or DB-window fallback for production annotations;
- using legacy annotation fallback;
- claiming known-face identity before C2.4;
- replacing raw evidence with diagnostic remux/transcode output.

The production gate must fail closed when raw video has decode errors, zero
decoded frames, non-positive duration, a first keyframe far from start,
PTS/DTS discontinuity, large packet-duration jumps, unexplained duration drift,
or unreconciled decoded/sidecar mismatch.

## Scheme A: Keyframe-Safe Event Replay

The event-style Replay request is changed to a time-domain strategy:

- the evidence window is `event_frame_pts +/- pre/post seconds`;
- Replay uses `ts_delta_sec` where supported;
- the anchor is selected at or before the requested start;
- `offset.seconds=0` is used for the keyframe-safe strategy;
- if event-style Replay output still fails the video integrity gate, it is not
  production evidence.

## Scheme B: Stable Sink PTS Crop Workaround

If Scheme A remains blocked by Replay output integrity, R2 may produce a
workaround bundle from the stable FPS-gated post-Savant sink output.

The workaround must record:

- `evidence_capture_mode=stable_post_savant_sink_time_crop`;
- `event_style_replay_job_passed=false`;
- `replay_event_flow_status=blocked_by_video_integrity`;
- `workaround_used=true`;
- `workaround_reason=event_style_replay_resulting_stream_video_integrity_failed`;
- `time_domain_crop_applied=true`.

This proves a time-domain post-Savant evidence bundle can be produced from a
stable same-stream source. It does not prove the event-style Replay flow is
ready, so the result marker is partial.

## Current Runtime Result

Latest R2 smoke:

```bash
bash scripts/smoke/current/check_c2_3b_r2_time_domain_integrity.sh
```

Result marker:

```text
PARTIAL_C2_3B_R2_STABLE_SINK_WORKAROUND_READY
```

Scheme A attempted keyframe-safe event-style Replay:

- record request id:
  `c2_3b_r2:req:c2_3b_r2_20260607T201306`
- source event id:
  `c2_3b_r2:synthetic:c2_3b_r2_20260607T201306`
- post-Savant source id: `c2_post_savant_fps_probe`
- event-style sink output:
  `/data/video-analytics/media/c2-post-savant-replay-fps-probe/replay-event-66666666-6666-4666-8666-001780834386%/unknown%`
- event-style bundle:
  `/data/video-analytics/media/evidence/c2_3b_r2_20260607T201306_event_replay`
- result: failed closed with `annotation_status=video_integrity_failed`,
  `trim_occurred=true`, and
  `timeline_reconciliation_status=needs_visual_or_time_mapping_verification`.

Scheme B stable post-Savant sink crop passed the production gate:

- stable sink source:
  `/data/video-analytics/media/c2-post-savant-replay-fps-probe/c2-fps-probe-20260607T140420%/unknown%`
- evidence bundle:
  `/data/video-analytics/media/evidence/c2_3b_r2_20260607T201306_stable_sink_crop`
- audit output:
  `/data/video-analytics/media/evidence_audit/c2_3b_r2_20260607T201306_stable_sink_crop`
- requested window:
  `requested_start_pts=9726166666`,
  `requested_end_pts=19726166666`
- actual metadata window:
  `actual_start_pts=9762877777`,
  `actual_end_pts=19480911111`
- decoded frames / metadata rows / sidecar rows: `234 / 234 / 234`
- `production_ready=true`
- `timeline_reconciliation_status=frame_counts_match`
- `trim_occurred=false`
- `time_domain_crop_applied=true`
- video integrity:
  `integrity_status=pass`,
  `first_frame_keyframe=true`,
  `first_keyframe_pts_time=0.041`,
  `decode_error_count=0`,
  `pts_large_jump_detected=false`,
  `max_packet_duration_s=0.041708`
- object counts:
  `person=185`, `face=155`, `known_face=0`,
  `keypoints_count=3145`, `face_landmarks_count=775`
- C2.3Q audit marker:
  `PARTIAL_C2_3Q_MANUAL_REVIEW_REQUIRED`, matching the known pose/keypoint
  warning pattern that requires manual inspection.

The stable sink crop bundle records:

- `evidence_capture_mode=stable_post_savant_sink_time_crop`
- `event_style_replay_job_passed=false`
- `replay_event_flow_status=blocked_by_video_integrity`
- `workaround_used=true`
- `workaround_reason=event_style_replay_resulting_stream_video_integrity_failed`

This result does not prove event-style Replay evidence is production-ready.
C2.4 identity binding should not start until the project either accepts this
workaround as the C2.3B boundary or repairs event-style Replay output itself.

## Result Markers

Result marker is determined by
`scripts/smoke/current/check_c2_3b_r2_time_domain_integrity.sh`:

- `PASS_C2_3B_R2_KEYFRAME_SAFE_TIME_DOMAIN_REPLAY_READY` when Scheme A passes
  video integrity and C2.3Q audit;
- `PARTIAL_C2_3B_R2_STABLE_SINK_WORKAROUND_READY` when Scheme A fails but
  Scheme B passes;
- `FAIL_C2_3B_R2_TIME_DOMAIN_REPAIR_BLOCKED` when both fail.

Do not enter C2.4 until either Scheme A passes or the project explicitly
accepts the Scheme B workaround and documents the Replay event-flow gap.
