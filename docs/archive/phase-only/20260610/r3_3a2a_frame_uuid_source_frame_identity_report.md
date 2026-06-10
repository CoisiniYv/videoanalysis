# R3.3A2a Frame UUID Source-Frame Identity Report

Status: PASS for source-frame identity. A2b Replay visual clip verification is
allowed to start as a separate POC-only step.

This report validates source-frame identity only. It does not start Replay
Service, does not deploy production Video File Sink, does not implement exact
snapshot, does not implement clip-worker/media-worker logic, and does not add a
DB migration or performance test.

## Test Input

- Source video / RTSP source:
  - RTSP: `rtsp://10.37.57.112:8554/live/1080movie`
  - Source MP4 used for inspection material:
    `/home/user/video-analytics/testVideo/1080movie.mp4`
- Source ID: `r3_3a2a_rtsp_identity_1780141642`
- Camera ID: `cam_r2_5_rtsp`
- Looping video: yes, current test source is a looped movie stream.
- PTS wrap risk: present for looping test video; timestamp fallback is not the
  A2a primary path.

## Event Information

- event_id: `373d94e7-199b-4ed3-be01-0eb9d68957e1`
- source_event_id:
  `savant_security:cam_r2_5_rtsp:19:intrusion:1780141672160`
- event_type: `intrusion`
- event_ts_ms: `1780141672202`
- event.frame_uuid: `019e78b6-4689-7d82-b6b6-ad39f7a37a77`
- keyframe_uuid: `019e78b6-4129-7912-bacc-96215b6c2b5d`
- previous_keyframe_uuid: `019e78b6-4129-7912-bacc-96215b6c2b5d`
- payload bbox: `x=1173.75 y=0.0 width=705.75 height=693.984375`
- rule state: full-frame intrusion ROI, `inside_ms=42`.

## Trace Hit Result

The required identity check is an exact lookup of `event.frame_uuid` in the
debug-only per-frame anchor trace. PASS requires a unique trace match.

- trace path:
  `/data/video-analytics/media/debug/r3_3a2a_frame_anchor_trace/r3_3a2a_rtsp_identity_1780141642/trace.jsonl`
- trace records: `198`
- frame_uuid unique trace match: yes.
- matched frame_num: `189`
- matched frame_pts: `13721388888`
- matched ntp_timestamp: `1780141672200209000`
- matched time_base: `1/1000000000`
- matched metadata_source: `video_frame`
- keyframe_uuid / previous_keyframe_uuid status: both present and equal to
  `019e78b6-4129-7912-bacc-96215b6c2b5d`.

## Visual Check

Smoke output should include a source-frame inspection material path. The visual
check is a manual review of the matched frame, not a timestamp fallback proof.

- inspection material path:
  `/data/video-analytics/media/debug/r3_3a2a_frame_uuid_identity/r3_3a2a_rtsp_identity_1780141642/matched_frame.jpg`
- summary path:
  `/data/video-analytics/media/debug/r3_3a2a_frame_uuid_identity/r3_3a2a_rtsp_identity_1780141642/identity_summary.json`
- source aligned clip path:
  `/data/video-analytics/media/debug/r3_3a2a_frame_uuid_identity/r3_3a2a_rtsp_identity_1780141642/source_aligned_clip.mp4`
- clip window: matched frame +/- 5 seconds, extracted from the source MP4,
  not from Replay.
- clip start: approximately `8.721s` in
  `/home/user/video-analytics/testVideo/1080movie.mp4`.
- clip duration: `10.010s`, `240` frames at `24000/1001` fps.
- event frame position in clip: approximately `5.000s`.
- event offset from clip center: `0` frames / `0 ms` for the intended
  source extraction window.
- manual judgment: aligned for A2a source-frame identity. The matched frame
  shows the visible human upper body/shoulder in the full-frame intrusion ROI;
  the event payload bbox is on the same right-side human region.
- source clip visual alignment: PASS. The event frame is inside the clip and
  centered by construction from the matched trace frame PTS.
- limitation: source-video extraction, NOT Replay extraction. This proves the
  propagated frame anchor can align back to the source video frame/clip, but it
  does not prove a Replay/cache clip can be generated.
- drift frames: `0` for identity lookup because the event frame_uuid matched
  exactly one trace record.
- drift milliseconds: `0` for identity lookup.
- budget: <= 5 frames or <= 500 ms, using the stricter applicable threshold.

If the rule fires after a configured inside/cooldown condition, the report must
separate rule trigger delay from frame-anchor error.

This event uses a full-frame intrusion ROI. The event does not represent a
single geometric boundary crossing; it represents a tracked person inside the
configured full-frame zone. Therefore any rule trigger delay is separate from
frame-anchor identity. The anchor itself points to the exact frame used by
`BehaviorRulesPyFunc._enrich_and_export()`.

## Conclusion

- source frame identity result: PASS.
- PASS means `event.frame_uuid` uniquely identifies the event-decision source
  frame in the trace and the matched frame visually represents the alarm moment
  or an explainably adjacent rule-trigger frame.
- FAIL means A2b must stop and the next step is gap analysis.

The A2a PASS only proves the event frame anchor identity. It does not prove
Replay clip extraction. A2b must still verify that a keyframe anchor can produce
a playable visual clip containing the event frame.

## Boundaries

- No Replay Service.
- No production clip-worker.
- No production media-worker.
- No exact snapshot production path.
- No Video File Sink production deployment.
- No DB migration.
- No performance test.
- Do not use event_ts_ms as the primary anchor.
