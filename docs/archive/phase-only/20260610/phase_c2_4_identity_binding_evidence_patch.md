# Phase C2.4 - Identity Binding / Known Face Evidence Patch MVP

## Result

This phase binds one C2 post-Savant sidecar face object to an auditable gallery
match result and writes an identity-patched evidence bundle.

Expected result marker:

`PASS_C2_4_IDENTITY_BINDING_EVIDENCE_PATCH_READY`

## Input Evidence

Input bundle:

`/data/video-analytics/media/evidence/c2_3b_r2_20260607T201306_stable_sink_crop`

This bundle remains a C2.3B-R2 stable sink workaround:

- `evidence_capture_mode=stable_post_savant_sink_time_crop`
- `workaround_used=true`
- `event_style_replay_job_passed=false`
- `fallback_used=false`
- `legacy_used_for_visual_binding=false`
- `production_ready=true`
- `video_integrity.production_gate_passed=true`

C2.4 does not repair event-style Replay and must not be reported as
`PASS_C2_3B_EVENT_CLIP_POST_SAVANT_REPLAY_MAPPING_READY`.

## Identity Binding Strategy

The C2.3B-R2 sidecar does not contain `source_observation_id` on face objects.
C2.4 therefore uses a strict deterministic join:

1. Select a sidecar face object from the accepted stable sink crop bundle.
2. Read real `security.face_observations` Redis messages from the C2
   post-Savant source.
3. Require a unique join using:
   - sidecar `frame_pts`;
   - Redis observation `payload.media.frame_pts`;
   - face bbox IoU above threshold;
   - a small PTS tolerance.
4. Materialize the joined Redis observation into `face_observations`.
5. Create or reuse a deterministic test person:
   `test:c2_4:person`.
6. Enroll the joined observation embedding as a gallery embedding.
7. Run a gallery match against the same observation and write `match_results`.
8. Patch sidecar identity fields only.

This is a deterministic C2.4 plumbing proof. It does not claim cross-event or
cross-person recognition robustness.

## Identity Patch Contract

The patch file is:

`identity_patches.jsonl`

Each patch records:

- `source_observation_id`
- sidecar join key
- Redis observation join key
- `person_id`
- `external_person_id`
- `person_name`
- `gallery_embedding_id`
- `match_result_id`
- `search_request_id`
- `similarity`
- `threshold`
- `identity_source=match_results`
- `identity_binding_status=matched`
- `geometry_modified=false`

The patch changes only identity/display fields. It does not change bbox,
landmarks, pose, track id, or frame timing.

## Output Bundle

The output bundle is written under:

`/data/video-analytics/media/evidence/c2_4_identity_binding_YYYYMMDDTHHMMSS`

Required files:

- `raw_clip.mov`
- `sink_metadata.json`
- `annotations.frame_cache.identity.jsonl`
- `summary.json`
- `summary.frame_cache.identity.json`
- `identity_patches.jsonl`
- `c2_4_identity_binding_summary.json`

The patched summary must keep the stable sink workaround boundary:

- `evidence_topology=post_savant`
- `evidence_capture_mode=stable_post_savant_sink_time_crop`
- `workaround_used=true`
- `event_style_replay_job_passed=false`
- `fallback_used=false`
- `legacy_used_for_visual_binding=false`
- `allow_db_annotation_fallback=false`
- `allow_legacy_annotation_fallback=false`

Identity fields:

- `identity_binding_connected=true`
- `identity_patch_source=match_results`
- `known_face_count > 0`
- `recognition_claim_allowed=true`
- `recognition_claim_scope=identity_patched_known_face_objects_only`

## Viewer Semantics

The evidence viewer already treats sidecar objects as matched faces when the
object has:

- `label.kind=known_face`, or
- `identity.status=matched`, or
- `identity.match_status=above_threshold`.

C2.4 does not add viewer DB lookup. The viewer reads identity from the sidecar.

## What This Phase Does Not Do

- Does not repair event-style Replay.
- Does not create new video capture.
- Does not expand multi-event coverage.
- Does not change Savant, YOLO, or AdaFace inference.
- Does not use PostgreSQL fallback to reconstruct bbox, pose, or landmarks.
- Does not use legacy annotations fallback.
- Does not fake known_face without match_result / gallery / person lineage.
- Does not claim watchlist semantics.

## Verification

Required checks:

```bash
python -m pytest harness/tests/test_c2_4_identity_patch_contract.py -q
python -m pytest harness/tests/test_c2_3q_evidence_output_audit_contract.py -q
python -m pytest harness/tests/test_c2_3b_video_integrity_gate.py -q
python -m py_compile scripts/tools/build_c2_identity_patched_evidence_bundle.py
git diff --check
bash -n scripts/smoke/current/check_c2_4_identity_binding_evidence_patch.sh
```

Runtime smoke:

```bash
bash scripts/smoke/current/check_c2_4_identity_binding_evidence_patch.sh
```

The runtime smoke must output either:

- `PASS_C2_4_IDENTITY_BINDING_EVIDENCE_PATCH_READY`
- `PARTIAL_C2_4_IDENTITY_JOIN_KEY_MISSING`
- `FAIL_C2_4_IDENTITY_BINDING_BLOCKED`

## Next Step

If this phase passes and the generated HTML/contact sheet are manually accepted,
the next decision is either:

- C2.5 Watchlist Event Evidence Semantics; or
- C2.3B-H Replay hardening.

C2.5 must continue to state that the current evidence capture mode is the
`stable_post_savant_sink_time_crop` workaround until event-style Replay is fixed.
