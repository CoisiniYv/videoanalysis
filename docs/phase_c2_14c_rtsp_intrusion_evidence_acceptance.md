# C2.14C RTSP Intrusion Evidence Acceptance

## Scope

C2.14B proved that a real RTSP `intrusion` event from
`c2_post_savant_fps_probe` can be bound to a post-Savant segment ring window and
exported as a video evidence bundle without relying on event-style Replay.

C2.14C is the acceptance audit for that produced bundle:

```text
/data/video-analytics/media/evidence/c2_14b_rtsp_event_clip_20260608T041137
```

The selected event is:

- `event_type`: `intrusion`
- `source_id`: `c2_post_savant_fps_probe`
- `source_event_id`:
  `savant_security:c2_post_savant_fps_probe:682:intrusion:1780891860187`
- `frame_pts`: `8313013044444`
- `event_ts_ms`: `1780891860312`

## Bundle Relationship

The bundle contains three primary evidence files:

- `raw_clip.mp4`: the event-window video clip cut from the RTSP segment ring.
- `sink_metadata.json`: filtered post-Savant metadata for the same clip window.
- `annotations.frame_cache.identity.jsonl`: production sidecar generated from
  the filtered metadata.

`summary.json` binds these files together and marks the bundle as
`evidence_capture_mode=rtsp_segment_ring`.

## Acceptance Checks

`scripts/tools/audit_c2_14c_rtsp_evidence_bundle.py` performs a read-mostly
audit of the bundle. It verifies:

- `raw_clip.mp4` exists, decodes, has non-zero frames, and is about 10 seconds.
- `sink_metadata.json` and `annotations.frame_cache.identity.jsonl` exist and
  parse.
- decoded frame count and sidecar frame count match, or a mismatch is explicitly
  explained.
- sidecar and metadata source IDs are consistently
  `c2_post_savant_fps_probe`.
- the selected `frame_pts` is inside clip metadata and sidecar coverage.
- the selected `event_ts_ms` matches the event and summary records.
- `db_window_fallback_used=false`.
- `legacy_used_for_visual_binding=false`.
- `event_style_replay_job_passed=false`.
- unsafe payload scan finds no embedding vector, image bytes, base64 image, or
  crop bytes in the event/summary/sidecar/report payloads.

This is ring evidence, not event-style Replay evidence. Replay was not claimed
as the passed path, and the summary explicitly keeps
`event_style_replay_job_passed=false`.

## Retention Fixture

C2.14C does not delete from the real ring:

```text
/data/video-analytics/media/rtsp-ring
```

Retention behavior is tested with `/tmp/c2_14c_retention_fixture_*`. The fixture
constructs target-source and non-target-source segments and verifies:

- expired target-source segment is deleted;
- segment younger than `MEDIA_RING_MIN_KEEP_SECONDS` is preserved;
- current active segment is preserved;
- non-target source segment is preserved;
- unsafe/path-traversal target is skipped and preserved;
- delete candidate count matches the actual deleted count;
- the fixture directory is cleaned after the test.

## Operator Report And Viewer

`summary.json` references `raw_clip.mp4`, `sink_metadata.json`, and
`annotations.frame_cache.identity.jsonl`. The existing
`operator_rtsp_event_clip_report.html` references the raw clip and core event
status, but does not currently repeat the sidecar and metadata filenames; the
C2.14C audit records this as an HTML reference gap while accepting the summary
report as the binding authority.

The live evidence-viewer is recorded as `not_checked` in this phase and does not
fail the bundle audit.

## Caveats

- C2.14C proves only the RTSP `intrusion` event clip.
- It does not prove a `watchlist_hit` known-face clip.
- Runtime module provenance is not normalized yet; follow-up R0.4/R0.5 can
  decide whether to remove the live `/tmp/c2-fps-probe-module` dependency.
- `/data/video-analytics` was not cleaned.
- Worker/API product integration is not restored by this phase.

## Validation

Required commands:

```bash
python -m pytest harness/tests/test_c2_14c_rtsp_evidence_acceptance.py -q
python -m py_compile scripts/tools/audit_c2_14c_rtsp_evidence_bundle.py
bash -n scripts/smoke/current/check_c2_14c_rtsp_evidence_acceptance.sh
git diff --check
scripts/smoke/current/check_c2_14c_rtsp_evidence_acceptance.sh
```

Expected result marker:

```text
PASS_C2_14C_RTSP_INTRUSION_EVIDENCE_ACCEPTANCE_READY
```
