# Phase C2.11 - Demo Packaging / Operator Walkthrough Bundle

## Result

Expected result marker:

`PASS_C2_11_DEMO_PACKAGE_READY`

C2.11 packages the already-proven C2.10 and C2-R watchlist evidence artifacts
into a static operator-facing demo directory. It does not add runtime semantics,
does not repair Replay, does not run model inference, and does not write DB or
Redis state.

## Inputs

Rebaseline document:

`docs/c2_post_savant_watchlist_evidence_rebaseline_2026_06_07.md`

Demo checklist:

`docs/c2_demo_checklist_2026_06_07.md`

C2.10 output:

`/data/video-analytics/media/evidence/c2_10_runtime_viewer_20260607T230426`

C2.6R evidence bundle:

`/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035`

Verified evidence-viewer URLs from C2.10:

- `http://127.0.0.1:8090/api/bundles/c2_6r_redis_watchlist_20260607T221035`
- `http://127.0.0.1:8090/api/bundles/c2_6r_redis_watchlist_20260607T221035/annotations?source=sidecar`

## Package Output

Output pattern:

`/data/video-analytics/media/evidence/c2_11_demo_package_YYYYMMDDTHHMMSS`

Files:

- `index.html`
- `README.md`
- `demo_manifest.json`
- `demo_checklist.md`
- `api_response_by_event_id.json`
- `api_response_by_source_event_id.json`
- `operator_watchlist_evidence.html`
- `evidence_manifest.json`
- `sidecar_annotation_sample.json`
- `unsafe_payload_scan.json`
- `limitations.json`

Raw video is not copied into the package. The manifest links to the existing
raw clip path and evidence-viewer raw clip URL.

## How To Open The Demo

1. Open `index.html` in the generated C2.11 package directory.
2. Open `operator_watchlist_evidence.html` from the package.
3. Open `api_response_by_event_id.json` or
   `api_response_by_source_event_id.json`.
4. Open the evidence-viewer manifest URL if viewer runtime is running.
5. Open the evidence-viewer sidecar annotations URL.
6. Verify the `known_face` / `watchlist_hit` fields and workaround caveats.

## Packaged Event

- `event_type=watchlist_hit`
- `person_id=4`
- `external_person_id=test:c2_4:person`
- `source_observation_id=face:c2_post_savant_fps_probe:4:17854:1`
- `similarity=1.0`
- `threshold=0.99`
- `watchlist_rule_id=c2_6r_test_watchlist_rule`

## Safety Boundaries

- No embedding vector is copied into the demo package.
- No image bytes, crop bytes, face crop bytes, base64 image payloads, or frame
  bytes are copied into the demo package.
- The unsafe scan rejects embedding vectors and large suspicious numeric arrays
  while allowing normal bbox, landmark, and keypoint geometry.
- `fallback_used=false`.
- `legacy_used_for_visual_binding=false`.
- `allow_db_annotation_fallback=false`.
- `allow_legacy_annotation_fallback=false`.
- Geometry remains from the post-Savant sidecar.
- Identity remains from match result / gallery / person lineage.

## Caveats

- Event-style Replay is not passed.
- The stable sink workaround remains active:
  `evidence_capture_mode=stable_post_savant_sink_time_crop`.
- The demo uses a deterministic fixed sample and deterministic test person.
- Broad recognition accuracy is not proven.
- Long-running worker soak is not proven.
- C2.10 live API HTTP used temporary local Uvicorn, not a live production API
  container.

## Verification

Required checks:

```bash
python -m pytest harness/tests/test_c2_11_demo_package.py -q
python -m pytest harness/tests/test_c2_10_runtime_api_viewer_recovery.py -q
python -m py_compile scripts/tools/build_c2_11_demo_package.py
git diff --check
bash -n scripts/smoke/current/check_c2_11_demo_package.sh
bash scripts/smoke/current/check_c2_11_demo_package.sh
```

The smoke only verifies inputs, builds the static package, checks required JSON
and HTML fields, and runs the unsafe payload scan. It does not start containers,
run Replay, modify DB, modify Redis, or run worker loops.

## Next Options

- C2.9 long-running integrated worker smoke.
- C2.12 real external face enrollment / new video watchlist test.
- C2.3B-H Replay hardening.
- C2 demo handoff to main branch.
