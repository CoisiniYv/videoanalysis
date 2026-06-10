# C2.3B-R2H Stable Sink Workaround Rebaseline

## Result

Result marker:

```text
PARTIAL_C2_3B_R2_STABLE_SINK_WORKAROUND_READY
```

This is a C2.3B-R2 workaround boundary, not a full event-style Replay pass.

## What This Is Not

- Not `PASS_C2_3B_EVENT_CLIP_POST_SAVANT_REPLAY_MAPPING_READY`.
- Not C2.4 identity binding.
- Not known-face recognition proof.
- Not watchlist visual proof.
- Not proof that event-style Replay job output is production-safe.

`known_face_count=0` remains expected before C2.4.

## What Passed

The stable post-Savant sink time-domain crop passed the production evidence
checks:

- source:
  `/data/video-analytics/media/c2-post-savant-replay-fps-probe/c2-fps-probe-20260607T140420%/unknown%`
- final bundle:
  `/data/video-analytics/media/evidence/c2_3b_r2_20260607T201306_stable_sink_crop`
- C2.3Q audit:
  `/data/video-analytics/media/evidence_audit/c2_3b_r2_20260607T201306_stable_sink_crop`
- same-source `raw_clip.mov`, `sink_metadata.json`, and
  `annotations.frame_cache.identity.jsonl`
- decoded frames / metadata rows / sidecar rows: `234 / 234 / 234`
- `production_ready=true`
- `timeline_reconciliation_status=frame_counts_match`
- `trim_occurred=false`
- `time_domain_crop_applied=true`
- `video_integrity.integrity_status=pass`
- `fallback_used=false`
- `legacy_used_for_visual_binding=false`
- `allow_db_annotation_fallback=false`
- `allow_legacy_annotation_fallback=false`

Video integrity passed:

- `first_frame_keyframe=true`
- `first_keyframe_pts_time=0.041`
- `keyframe_count=1`
- `decode_error_count=0`
- `pts_large_jump_detected=false`
- `max_packet_duration_s=0.041708`
- `production_gate_passed=true`

Object counts:

- `person=185`
- `face=155`
- `known_face=0`
- `keypoints_count=3145`
- `face_landmarks_count=775`

The C2.3Q marker remained `PARTIAL_C2_3Q_MANUAL_REVIEW_REQUIRED` only for the
known pose/keypoint warning class previously accepted during C2.3Q manual
review.

## What Failed

Scheme A, the event-style Replay resulting stream path, still failed closed:

- resulting stream:
  `replay-event-66666666-6666-4666-8666-001780834386`
- event-style output:
  `/data/video-analytics/media/evidence/c2_3b_r2_20260607T201306_event_replay`
- failure: `trim_occurred=true` and `video_integrity_failed`
- timeline:
  `timeline_reconciliation_status=needs_visual_or_time_mapping_verification`

This confirms:

- fixed `stop_condition.frame_count=240` must not represent a 10 second
  evidence window;
- event-style Replay resulting stream cannot yet be assumed production-safe;
- trim must not hide decoded/metadata mismatch.

## Why The Same Visual Segment Repeats

The current R2 workaround intentionally uses the same stable FPS-gated
post-Savant sink output and a synthetic event around frame 125. Therefore the
same visual segment appears repeatedly across R2 runs. This is expected for the
current proof because the input source and event PTS are fixed.

This does not prove varied event coverage. Future validation should run the
stable sink crop with multiple event frames and, later, multiple streams.

## Why The Workaround Is Accepted

The stable post-Savant stream has already been validated through C2.2A,
C2.2R, C2.3A, and C2.3Q as visually aligned and time-stable. C2.3B-R2 adds a
time-domain PTS crop and a video integrity gate, proving that a short evidence
bundle can be generated from the same post-Savant video and metadata stream
without DB fallback or legacy annotation fallback.

This is still a workaround because event-style Replay automatic re-push has
not been proven production-safe.

## Decision

Short term:

- Treat `stable_post_savant_sink_time_crop` as the temporary C2.3B boundary.
- Do not call the state `PASS_C2_3B_EVENT_CLIP_POST_SAVANT_REPLAY_MAPPING_READY`.
- Before C2.4, manually review:
  `/data/video-analytics/media/evidence_audit/c2_3b_r2_20260607T201306_stable_sink_crop/index.html`
  and the contact sheet in the same audit directory.
- If manual review accepts the workaround, C2.4 may start, but its documents
  must explicitly state
  `evidence_capture_mode=stable_post_savant_sink_time_crop`.

Medium term backlog:

- C2.3B-H1: Event-style Replay keyframe-safe repair.
- C2.3B-H2: Replay stop condition time-domain support.
- C2.3B-H3: Replay video integrity regression suite.

Long term architecture options:

- Savant Replay event flow.
- Stable post-Savant rolling sink/cache plus PTS crop.
- External NVR replay.

## Current Boundary

C2 can proceed only if the workaround is explicitly accepted. Event-style
Replay remains a hardening backlog item, and C2.4 must not claim identity proof
from this C2.3B-R2 result.
