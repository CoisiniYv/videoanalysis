# Phase C2.5 - Watchlist Event Evidence Semantics MVP

## Result

Expected result marker:

`PASS_C2_5_WATCHLIST_EVENT_EVIDENCE_SEMANTICS_READY`

C2.5 builds on the accepted C2.4 identity-patched evidence bundle and adds
business event semantics for a deterministic `watchlist_hit`.

This phase does not repair event-style Replay, does not add recognition
algorithms, and does not claim multi-event recognition accuracy.

## Input Bundle

Input evidence bundle:

`/data/video-analytics/media/evidence/c2_4_identity_binding_20260607T211846`

Identity used:

- `person_id=4`
- `external_person_id=test:c2_4:person`
- `gallery_embedding_id=3`
- `match_result_id=3`
- `source_observation_id=face:c2_post_savant_fps_probe:4:17854:1`
- `similarity=1.0`
- `threshold=0.99`
- `join_method=frame_pts_bbox_iou_unique`

The input remains a stable sink workaround:

- `evidence_capture_mode=stable_post_savant_sink_time_crop`
- `workaround_used=true`
- `event_style_replay_job_passed=false`

## Watchlist Event Contract

C2.5 writes `watchlist_event.json` with a file-level deterministic
`watchlist_hit` event. The event reuses the existing face-worker
`watchlist_hit` SecurityEvent semantics where practical, but it is not emitted
to Redis or PostgreSQL in this phase.

Existing contract inspection found:

- `services/face-worker/app/face_match_event_service.py` already defines
  `watchlist_hit` SecurityEvent construction for face/gallery matches.
- `services/media-worker/app/frame_annotation_event_window.py` recognizes
  `watchlist_hit` as an event type for evidence annotation windows.
- `db/migrations/002_phase2e_events.sql` and
  `db/migrations/008_r3_unified_event_evidence_algorithm_rules.sql` define the
  unified event/evidence data plane used by the event-worker.
- The current worktree does not include a production `watchlist_rules` table
  migration. C2.5 therefore uses a deterministic file-level synthetic rule id
  (`c2_5_test_watchlist_rule`) and keeps the output clearly scoped as test
  evidence semantics.

Required fields include:

- `schema_version=1.0`
- `event_type=watchlist_hit`
- `source_event_id`
- `camera_id`
- `source_id`
- `track_id`
- `source_observation_id`
- `person_id`
- `external_person_id`
- `gallery_embedding_id`
- `match_result_id`
- `similarity`
- `threshold`
- `watchlist_rule_id`
- `watchlist_rule_name`
- `severity`
- `event_ts_ms`
- `frame_pts`
- `frame_num`
- `evidence_bundle`

The event payload records:

- `identity_source=match_results`
- `identity_binding_status=matched`
- `evidence_capture_mode=stable_post_savant_sink_time_crop`
- `workaround_used=true`
- `event_style_replay_job_passed=false`
- `geometry_source=post_savant_sidecar`
- `geometry_modified=false`
- `join_method=frame_pts_bbox_iou_unique`

The event payload must not include embedding vectors, image bytes, crop bytes,
frame bytes, base64 images, or Redis image payloads.

## Output Bundle

Output bundle:

`/data/video-analytics/media/evidence/c2_5_watchlist_hit_YYYYMMDDTHHMMSS`

Runtime output from the accepted C2.5 smoke:

`/data/video-analytics/media/evidence/c2_5_watchlist_hit_20260607T214030`

C2.3Q audit output:

`/data/video-analytics/media/evidence_audit/c2_5_watchlist_hit_20260607T214030`

Runtime counts:

- `decoded_video_frame_count=234`
- `original_metadata_frame_count=234`
- `sidecar_frame_count=234`
- `known_face_count=1`
- `watchlist_hit_count=1`
- `unknown_face_count=154`
- `production_ready=true`
- `video_integrity.production_gate_passed=true`

Required files:

- `raw_clip.mov`
- `sink_metadata.json`
- `annotations.frame_cache.identity.jsonl`
- `summary.json`
- `summary.frame_cache.identity.json`
- `identity_patches.jsonl`
- `watchlist_event.json`
- `watchlist_evidence_summary.json`
- `watchlist_evidence_report.html`

The C2.5 builder copies the C2.4 bundle and augments event semantics only. It
does not modify bbox, landmarks, pose, frame timing, track id, or object id.

## Sidecar Semantics

For the selected known face object, the sidecar keeps `object_type=known_face`
and adds watchlist identity fields:

- `identity.event_type=watchlist_hit`
- `identity.source_event_id`
- `identity.watchlist_rule_id`
- `identity.watchlist_rule_name`
- `identity.watchlist_hit_status=matched`
- `identity.person_id`
- `identity.external_person_id`
- `identity.gallery_embedding_id`
- `identity.match_result_id`
- `identity.similarity`
- `identity.threshold`
- `identity.identity_source=match_results`
- `identity.source_observation_id`

The sidecar label/action/style fields may expose the event for report/viewer
display, but identity patching does not create new geometry.

## Summary Requirements

`summary.json` must retain the C2.4/C2.3B-R2 safety boundary:

- `evidence_topology=post_savant`
- `evidence_capture_mode=stable_post_savant_sink_time_crop`
- `workaround_used=true`
- `event_style_replay_job_passed=false`
- `event_type=watchlist_hit`
- `watchlist_hit_count=1`
- `known_face_count>=1`
- `identity_binding_connected=true`
- `identity_patch_source=match_results`
- `recognition_claim_allowed=true`
- `fallback_used=false`
- `legacy_used_for_visual_binding=false`
- `allow_db_annotation_fallback=false`
- `allow_legacy_annotation_fallback=false`
- `production_ready=true`
- `video_integrity.production_gate_passed=true`

## Report And Audit

`watchlist_evidence_report.html` is a static inspection report. It shows:

- Watchlist Hit
- person id and external person id
- similarity and threshold
- frame number, track id, and source observation id
- evidence capture mode
- warning that stable sink workaround is active and event-style Replay has not
  passed

The C2.3Q audit can still be run on the generated bundle. C2.3Q pose/keypoint
manual-review warnings are not C2.5 semantic failures unless the audit returns a
blocking failure marker.

Live evidence-viewer HTTP is not required for this phase. A 502 from the viewer
does not block C2.5 when generated sidecar, event, summary, report, and audit
files prove the watchlist semantics.

## Limitations

- The identity is a deterministic C2.4 test person.
- `similarity=1.0` is a controlled self-match used to prove event plumbing.
- This phase does not prove broad face recognition accuracy.
- `evidence_capture_mode` remains `stable_post_savant_sink_time_crop`.
- `event_style_replay_job_passed=false`; event-style Replay is still not a
  production-ready evidence source.
- Live viewer HTTP may remain unverified if the service still returns 502.

## Verification

Required checks:

```bash
python -m pytest harness/tests/test_c2_5_watchlist_evidence_semantics.py -q
python -m pytest harness/tests/test_c2_4_identity_patch_contract.py -q
python -m pytest harness/tests/test_c2_3b_video_integrity_gate.py -q
python -m pytest harness/tests/test_c2_3q_evidence_output_audit_contract.py -q
python -m py_compile scripts/tools/build_c2_watchlist_evidence_bundle.py
git diff --check
bash -n scripts/smoke/current/check_c2_5_watchlist_evidence_semantics.sh
```

Runtime smoke:

```bash
bash scripts/smoke/current/check_c2_5_watchlist_evidence_semantics.sh
```

The runtime smoke builds a C2.5 bundle from the C2.4 bundle, validates
`watchlist_event.json`, validates `summary.json`, validates the sidecar
watchlist fields, runs the offline C2.3Q audit, and does not require live viewer
HTTP.

## Next Options

After C2.5 passes, choose one:

- C2.6 live watchlist event from face-worker.
- C2.3B-H event-style Replay hardening.
- Viewer runtime recovery if a demo requires live HTTP inspection.
