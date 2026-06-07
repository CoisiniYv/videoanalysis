# Phase C2.12D - Runtime Finch Visual Evidence Join

## Goal

C2.12D attempts to turn the real C2.12C runtime Finch match into inspectable
visual evidence. The phase is intentionally conservative: it only generates a
visual evidence bundle when the matched frame/time is covered by post-Savant
video-file-sink output and the matched face can be joined to metadata without
inventing geometry.

## C2.12C Finch Match

Input output:

`/data/video-analytics/media/evidence/c2_12c_runtime_capture_search_20260608T011352`

Matched person:

- person: Finch
- person_id: `6`
- external_person_id: `demo:f4_3:finch`
- gallery_embedding_id: `5`
- similarity: `0.690014918944816`
- threshold: `0.65`
- original_source_observation_id:
  `face:c2_post_savant_fps_probe:2196:36708508`
- C2.12C source_observation_id:
  `face:c2_12c_runtime:c2_12c_runtime_capture_search_20260608T011352:c2_post_savant_fps_probe:2196:36708508288888:6_1780849127595-0`
- source_id: `c2_post_savant_fps_probe`
- track_id: `2196`
- frame_num: `296551`
- timestamp_ms: `36708508`
- frame_pts: `36708508288888`

This is a real runtime/video-derived match from the current Savant face
observation stream. It is not a gallery self-match and does not use
`test:c2_4:person`.

## Join Strategy

Tool:

`scripts/tools/build_c2_12d_runtime_match_visual_evidence.py`

The join order is:

1. source_observation_id exact match.
2. source_id + frame_pts/timestamp + track_id + bbox IoU.
3. source_id + frame_num range + bbox IoU.

Track ID alone is not accepted. DB window fallback and legacy annotation
fallback are not used.

## Sink Outputs Inspected

Sink root:

`/data/video-analytics/media/c2-post-savant-replay-fps-probe`

The smoke inspected 10 metadata outputs. The closest retained sink ranges were:

| Sink | PTS range | Timestamp ms range | Covers Finch match |
| --- | ---: | ---: | --- |
| `c2-fps-probe-20260607T140420%` | `9512622222..19480911111` | `9512..19480` | false |
| replay-event `333.../444.../555...` outputs | up to `12796703711111` | up to `12796703` | false |
| replay-event `666...001780834012` | `20448889911111..20458899911111` | `20448889..20458899` | false |
| replay-event `666...001780834265` | `21523963911111..21533973911111` | `21523963..21533973` | false |
| replay-event `666...001780834386` | `21523963911111..21533973911111` | `21523963..21533973` | false |

Target Finch frame_pts is `36708508288888`, so no inspected sink output covers
the matched time.

## Result

Current marker:

`PARTIAL_C2_12D_RUNTIME_MATCH_NO_VIDEO_SINK_COVERAGE`

Diagnostic output:

`/data/video-analytics/media/evidence/c2_12d_finch_join_gap_20260608T012729`

Files:

- `sink_inventory.json`
- `match_geometry.json`
- `join_attempts.json`
- `unsafe_payload_scan.json`
- `decision_report.md`
- `summary.json`

Visual evidence was not generated. The real Finch match exists, but the current
stable sink files do not retain the matched timestamp/frame_pts, so generating
a raw clip or viewer bundle would require fake visual evidence.

## Safety Boundaries

- No fake Finch match was generated.
- Threshold remains `0.65`.
- Gallery self-match is not accepted.
- `test:c2_4:person` is not used.
- Embedding vectors are not written to JSON/report/event payloads.
- Image, base64, face crop, and crop bytes are not written to payloads.
- Geometry is not altered.
- Track ID alone is not accepted as a join key.
- DB window fallback is not used.
- Legacy annotation fallback is not used.
- Event-style Replay is not repaired or claimed passed.

## Next Action

The next runtime step should keep the current route A and make sink coverage
coincident with the runtime match:

1. Enable or retain stable post-Savant sink rolling output during bounded
   Reese/Finch capture.
2. Run the current looped movie until Finch/Reese appears while sink output is
   recording.
3. Rerun C2.12C capture and C2.12D join on the same retained sink window.

This remains separate from event-style Replay hardening and broad accuracy
testing.

## Verification

```bash
python -m pytest harness/tests/test_c2_12d_runtime_match_visual_evidence.py -q
python -m pytest harness/tests/test_c2_12c_runtime_reese_finch_capture.py -q
python -m py_compile scripts/tools/build_c2_12d_runtime_match_visual_evidence.py
git diff --check
bash -n scripts/smoke/current/check_c2_12d_runtime_match_visual_evidence.sh
bash scripts/smoke/current/check_c2_12d_runtime_match_visual_evidence.sh
```
