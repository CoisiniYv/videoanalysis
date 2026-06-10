# Phase C2.10 - Runtime API / Evidence Viewer Recovery for Watchlist Evidence

## Result

Expected result marker:

`PASS_C2_10_RUNTIME_API_VIEWER_READY`

C2.10 proves that the persisted C2.7 watchlist event can be inspected through
live HTTP endpoints and an operator-facing static report without changing
evidence semantics.

## Inputs

C2.7 event:

- `event_id=b4cf4b6b-9d90-4282-9b7f-5e0c56e81a32`
- `source_event_id=c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:c2_7_event_worker_persistence_20260607T223424`

C2.8 API evidence detail:

`/data/video-analytics/media/evidence/c2_8_api_evidence_detail_20260607T225039/api_evidence_detail_response.json`

C2.6R evidence bundle:

`/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035`

C2.6R audit:

`/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035`

## Runtime Status

The C2 compose file is:

`infra/docker-compose.c2-post-savant-replay-poc.yml`

It runs the C2 replay/Savant/viewer stack and exposes evidence-viewer on port
`8090`. It does not include the API service. If no live API service is already
available, the C2.10 smoke starts a temporary local Uvicorn process for
`services/api/app/main.py`, verifies HTTP endpoints, and then stops that process.

No Docker containers are restarted by the C2.10 smoke.

## Live API HTTP Verification

Endpoint:

`GET /api/v1/events/{event_id_or_source_event_id}/evidence`

The smoke verifies both:

- DB event id: `b4cf4b6b-9d90-4282-9b7f-5e0c56e81a32`
- source event id:
  `c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:c2_7_event_worker_persistence_20260607T223424`

The response must preserve:

- `event_type=watchlist_hit`
- `source_observation_id=face:c2_post_savant_fps_probe:4:17854:1`
- `person_id=4`
- `external_person_id=test:c2_4:person`
- `watchlist_rule_id=c2_6r_test_watchlist_rule`
- `similarity=1.0`
- `threshold=0.99`
- evidence bundle path
- `evidence_capture_mode=stable_post_savant_sink_time_crop`
- `workaround_used=true`
- `event_style_replay_job_passed=false`

## Evidence Viewer Verification

Evidence-viewer endpoints:

- `GET /health`
- `GET /api/bundles/c2_6r_redis_watchlist_20260607T221035`
- `GET /api/bundles/c2_6r_redis_watchlist_20260607T221035/annotations?source=sidecar`

The viewer proof is file-based. It reads `summary.json` and
`annotations.frame_cache.identity.jsonl` from the evidence bundle. It does not
query the DB for identity, does not use legacy annotation fallback, and does not
modify geometry.

## Static Operator Report

Output directory:

`/data/video-analytics/media/evidence/c2_10_runtime_viewer_YYYYMMDDTHHMMSS`

Files:

- `live_api_response_by_event_id.json`
- `live_api_response_by_source_event_id.json`
- `evidence_viewer_health.json`
- `evidence_viewer_bundle_manifest.json`
- `evidence_viewer_annotations_summary.json`
- `operator_watchlist_evidence.html`
- `unsafe_payload_scan.json`
- `c2_10_runtime_viewer_summary.json`

The static report displays:

- Watchlist Hit
- known face label
- person id and external person id
- similarity and threshold
- source observation id
- event id and source event id
- evidence bundle path
- raw clip path
- sidecar path
- watchlist event path
- audit HTML and contact sheet paths
- stable sink workaround flags
- explicit warning that event-style Replay is not passed

## Safety Boundary

C2.10 returns metadata, paths, and small runtime JSON responses. It does not add
or expose:

- embedding vectors
- image bytes
- crop bytes
- frame bytes
- base64 image payloads

The existing persisted C2.7 payload can still contain boolean status keys such
as `embedding_included=false` or `image_bytes_included=false`; those are not
embedding or image-byte payloads.

## Limitations

- Evidence capture still uses
  `stable_post_savant_sink_time_crop`.
- `workaround_used=true`.
- `event_style_replay_job_passed=false`.
- C2.3B event-style Replay is still not passed.
- This phase does not repair Replay.
- This phase is not a long-running worker soak.
- This phase is not a broad recognition accuracy test.

## Verification

Required checks:

```bash
python -m pytest harness/tests/test_c2_10_runtime_api_viewer_recovery.py -q
python -m pytest harness/tests/test_c2_8_api_evidence_detail.py -q
python -m pytest harness/tests/test_c2_7_event_worker_persistence_api_query.py -q
python -m py_compile scripts/tools/build_c2_10_runtime_api_viewer_report.py
git diff --check
bash -n scripts/smoke/current/check_c2_10_runtime_api_viewer_recovery.sh
```

Runtime smoke:

```bash
bash scripts/smoke/current/check_c2_10_runtime_api_viewer_recovery.sh
```

## Next Options

- C2.9 long-running integrated worker smoke.
- C2.11 demo packaging.
- C2.3B-H Replay hardening.
