# R3.3A2b Replay Keyframe Visual Clip Report

Status: UNVERIFIED / BLOCKED.

Reason: same-stream Replay/cache POC not deployed; verification could not be
executed.

This report is a POC-only Replay/keyframe visual alignment check. It is not a
production deployment and does not connect clip-worker, media-worker, API, or
retention logic.

## POC Harness

- Temporary services used: none.
- Replay/cache API calls: not executed in this convergence pass.
- Temporary compose: none.
- Output directory:
  `/data/video-analytics/media/debug/r3_3a2b_replay_visual`
- Production boundary: no production clip-worker, no production media-worker,
  no DB migration, no production Video File Sink deployment, no performance
  test.

## Event And Anchor Information

Input A2a summary:

`/data/video-analytics/media/debug/r3_3a2a_frame_uuid_identity/r3_3a2a_rtsp_identity_1780141642/identity_summary.json`

- event_id: `373d94e7-199b-4ed3-be01-0eb9d68957e1`
- source_event_id:
  `savant_security:cam_r2_5_rtsp:19:intrusion:1780141672160`
- event_type: `intrusion`
- source_id: `r3_3a2a_rtsp_identity_1780141642`
- camera_id: `cam_r2_5_rtsp`
- event_ts_ms: `1780141672202`
- frame_uuid: `019e78b6-4689-7d82-b6b6-ad39f7a37a77`
- keyframe_uuid: `019e78b6-4129-7912-bacc-96215b6c2b5d`
- previous_keyframe_uuid: `019e78b6-4129-7912-bacc-96215b6c2b5d`

## Replay Anchor Decision

- Replay anchor used: `019e78b6-4129-7912-bacc-96215b6c2b5d`
- Anchor source: `previous_keyframe_uuid`
- Reason: A2b must use a legal keyframe UUID candidate. The current
  `event.frame_uuid` is the alarm frame identity and was not sent as a Replay keyframe anchor.
- Fallback: none.

## Replay Job Result

- Replay status: unverified; pending same-stream Replay/cache POC.
- Replay job request: not submitted.
- Clip path: none.
- Metadata path: none.
- Playable: unverified.

The current c1 mainline runtime does not include a same-stream Replay/cache POC.
The legacy Phase 3A/3B POC compose files are not acceptable evidence for this
event because they do not consume the exact same source-frame stream that
created the A2a event frame_uuid. Using those legacy paths would recreate the
old drift failure mode.

## Visual Check

- Alarm frame from A2a was visually checked at:
  `/data/video-analytics/media/debug/r3_3a2a_frame_uuid_identity/r3_3a2a_rtsp_identity_1780141642/matched_frame.jpg`
- Replay clip: not generated; pending same-stream Replay/cache POC.
- Alarm picture in Replay clip: not applicable.
- Centering: not applicable.
- Measured offset: not available.
- Visual alignment result: UNVERIFIED.
- Reason: same-stream Replay/cache POC not deployed; verification could not be
  executed.

## Conclusion

R3.3A2a passed frame identity, but R3.3A2b is blocked and unverified. It is
pending same-stream Replay/cache POC work that consumes the same frame stream
that produces behavior events, or an equivalent same-source replay/cache
mechanism.

Do not start production clip-worker/media-worker work from this state.
