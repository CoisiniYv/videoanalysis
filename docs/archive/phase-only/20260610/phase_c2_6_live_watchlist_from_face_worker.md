# Phase C2.6 - Live Watchlist Event From Face-Worker MVP

## Result

Expected result marker for the implemented safe mode:

`PARTIAL_C2_6_FACE_WORKER_HARNESS_ONLY`

C2.6 proves that a C2 watchlist evidence event can be produced from the
face-worker pgvector/gallery matching path. It does not repair event-style
Replay and does not claim the live Redis consumer loop was exercised.

## Inspection Summary

The current face-worker code already contains watchlist event generation:

- `services/face-worker/app/face_match_event_service.py`
  - `build_watchlist_hit_event()` builds a unified `watchlist_hit`
    SecurityEvent dict.
  - `publish_security_event()` writes the event to Redis `security.events`.
  - `emit_watchlist_hits_for_source()` searches gallery embeddings and can
    publish events for a source.
- `services/face-worker/app/vector_store.py`
  - `FaceVectorStore.search_gallery()` performs pgvector similarity search over
    active `person_gallery_embeddings`.
- `services/face-worker/match_gallery.py`
  - writes gallery search output to `match_results`.
- `services/face-worker/app/worker.py`
  - has `WatchlistMatchEmitter`, which calls `FaceVectorStore.search_gallery()`
    and publishes `watchlist_hit` events after newly inserted observations.

Answers to the C2.6 inspection questions:

1. Face-worker already has `watchlist_hit` generation.
2. It can write to Redis `security.events`.
3. The reusable worker emitter does not write `match_results`; the gallery match
   tool/repository does.
4. No production `watchlist_rules` table migration exists in this worktree.
5. A deterministic C2.6 test rule can be represented as file/summary event
   metadata without schema changes.
6. Processing one existing `face_observation` through the same vector matching
   code is safe in a harness.

There is also a code-level risk in the live consumer path: the current
`WatchlistMatchEmitter` calls `build_watchlist_hit_event(..., event_type=...)`,
but the function signature does not accept `event_type`. C2.6 therefore uses
the shared matching/event-building logic directly and reports
`PARTIAL_C2_6_FACE_WORKER_HARNESS_ONLY` instead of claiming full live-loop PASS.

## Input Observation

Preferred source observation:

- `source_observation_id=face:c2_post_savant_fps_probe:4:17854:1`
- `source_id=c2_post_savant_fps_probe`
- `external_person_id=test:c2_4:person`
- `threshold=0.99`

This observation was already used in C2.4/C2.5 and is backed by a real
face_observation, gallery embedding, and match result lineage.

## Test Person / Gallery / Rule

C2.6 reuses the deterministic C2.4 person and gallery when present:

- `external_person_id=test:c2_4:person`
- gallery embedding from `person_gallery_embeddings`
- match output written to `match_results`

The watchlist rule is a deterministic C2.6 test rule:

- `watchlist_rule_id=c2_6_test_watchlist_rule`
- `watchlist_rule_name=C2.6 Test Watchlist Rule`
- `watchlist_rule_source=c2_6_synthetic_file_rule`

This avoids schema changes while keeping the rule visible and auditable in the
event payload and bundle summary.

## Match Method

The C2.6 builder:

1. Reads the existing `face_observations` row by `source_observation_id`.
2. Uses `FaceVectorStore.search_gallery()` from face-worker to search active
   gallery embeddings for the target person.
3. Requires similarity above threshold.
4. Writes a `match_results` row through `MatchResultRepository`.
5. Builds a `watchlist_hit` event through
   `face_match_event_service.build_watchlist_hit_event()`.
6. Augments the event with C2.6 fields:
   - `producer=c2_6_face_worker_harness`
   - `identity_source=face_worker_pgvector_match`
   - `watchlist_match_source=face_worker_shared_matching_logic`
   - `embedding_included=false`
   - `image_bytes_included=false`
   - `evidence_capture_mode=stable_post_savant_sink_time_crop`
   - `event_style_replay_job_passed=false`

The harness does not publish the event to Redis and does not exercise the live
consumer loop.

## Output Evidence Bundle

Output bundle:

`/data/video-analytics/media/evidence/c2_6_live_watchlist_YYYYMMDDTHHMMSS`

Required files:

- `raw_clip.mov`
- `sink_metadata.json`
- `annotations.frame_cache.identity.jsonl`
- `summary.json`
- `identity_patches.jsonl`
- `watchlist_event.json`
- `live_watchlist_event.json`
- `c2_6_live_watchlist_summary.json`

The output bundle keeps the stable sink workaround:

- `evidence_capture_mode=stable_post_savant_sink_time_crop`
- `workaround_used=true`
- `event_style_replay_job_passed=false`

It must also include:

- `event_type=watchlist_hit`
- `watchlist_hit_count=1`
- `live_watchlist_from_face_worker=true`
- `face_worker_match_verified=true`
- `known_face_count>=1`
- `fallback_used=false`
- `legacy_used_for_visual_binding=false`
- `production_ready=true`
- `video_integrity.production_gate_passed=true`

## Limitations

- This is a deterministic controlled test, not a broad recognition accuracy
  test.
- The Redis consumer loop is not exercised.
- Redis `security.events` publishing is not claimed.
- The watchlist rule is a synthetic C2.6 file-level rule because production
  watchlist rule infrastructure is not present in this worktree.
- `evidence_capture_mode` remains the stable sink workaround.
- Event-style Replay is still not passed.
- Live evidence-viewer HTTP is not required.

## Verification

Required checks:

```bash
python -m pytest harness/tests/test_c2_6_live_watchlist_from_face_worker.py -q
python -m pytest harness/tests/test_c2_5_watchlist_evidence_semantics.py -q
python -m pytest harness/tests/test_c2_4_identity_patch_contract.py -q
python -m pytest harness/tests/test_c2_3b_video_integrity_gate.py -q
python -m pytest harness/tests/test_c2_3q_evidence_output_audit_contract.py -q
python -m py_compile scripts/tools/build_c2_live_watchlist_evidence_bundle.py
git diff --check
bash -n scripts/smoke/current/check_c2_6_live_watchlist_from_face_worker.sh
```

Runtime smoke:

```bash
bash scripts/smoke/current/check_c2_6_live_watchlist_from_face_worker.sh
```

## Next Options

After C2.6 harness-only partial:

- Exercise the live Redis consumer loop after fixing the emitter signature gap.
- C2.7 event-worker persistence / API event query.
- C2.3B-H event-style Replay hardening.
- Viewer runtime recovery if a demo needs live HTTP inspection.
