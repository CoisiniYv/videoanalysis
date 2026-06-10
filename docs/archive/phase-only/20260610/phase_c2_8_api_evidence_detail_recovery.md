# Phase C2.8 - API/UI Evidence Detail Recovery MVP

## Result

Expected result marker:

`PASS_C2_8_API_EVIDENCE_DETAIL_READY`

C2.8 proves that a persisted C2.7 `watchlist_hit` event can be queried through
the API/TestClient and repository paths and returned as an operator-safe
evidence detail response. The response exposes event, watchlist, identity, and
evidence bundle metadata without returning embeddings, image bytes, crop bytes,
or base64 payloads.

## Input

C2.7 DB event:

- `event_id=b4cf4b6b-9d90-4282-9b7f-5e0c56e81a32`
- `source_event_id=c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:c2_7_event_worker_persistence_20260607T223424`
- `event_type=watchlist_hit`
- `camera_id=c2_post_savant_fps_probe`
- `source_id=c2_post_savant_fps_probe`
- `source_observation_id=face:c2_post_savant_fps_probe:4:17854:1`

C2.7 output directory:

`/data/video-analytics/media/evidence/c2_7_event_worker_persistence_20260607T223424`

Evidence bundle referenced by the persisted event:

`/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035`

## Query Path

API route:

`GET /api/v1/events/{event_id_or_source_event_id}/evidence`

For C2.8 the route is verified with FastAPI TestClient using the C2.7
`source_event_id`. Live HTTP runtime is not required.

Repository path:

- `services/api/app/repositories/events.py::EventRepository.get_by_source_event_id()`
- `services/api/app/repositories/events.py::EventRepository.list_evidence_tasks()`

Resolver:

`services/api/app/services/evidence_detail_resolver.py::resolve_event_evidence_detail()`

The resolver reads only small JSON summaries from the event payload evidence
bundle path, validates expected paths, and returns metadata-only file paths or
`/media/...` URLs where the API media mapping is available.

## Response Contract

The `EventEvidenceResponse` now includes `evidence_detail` with these groups:

- event identity: `event_id`, `source_event_id`, `event_type`, `status`,
  `camera_id`, `source_id`, `track_id`, `source_observation_id`
- person identity: `person.person_id`, `person.external_person_id`
- watchlist identity: `watchlist_rule_id`, `similarity`, `threshold`,
  `match_result_id`, `gallery_embedding_id`
- evidence metadata: bundle path, raw clip path, summary path, sidecar path,
  watchlist event path, audit path, report path, video integrity, production
  readiness, known face count, watchlist hit count, capture mode, workaround
  flags
- limitations: `stable_sink_workaround`, `event_style_replay_not_passed`,
  `not_broad_accuracy_test`
- unsafe scan: `payload_has_embedding=false`,
  `payload_has_image_bytes=false`, `forbidden_key_paths=[]`

The response intentionally returns metadata and paths, not sidecar contents,
image bytes, crop bytes, base64 strings, or embedding vectors.

## Output

Output directory:

`/data/video-analytics/media/evidence/c2_8_api_evidence_detail_YYYYMMDDTHHMMSS`

Files:

- `api_evidence_detail_response.json`
- `repository_evidence_detail_response.json`
- `unsafe_payload_scan.json`
- `c2_8_api_evidence_detail_summary.json`

## Persisted Payload Boundary

The existing C2.7 event payload contains boolean status fields such as
`embedding_included=false`, `image_bytes_included=false`, and
`crop_bytes_included=false`. C2.8 does not remove or mutate the persisted event
payload. The C2.8 safety claim applies to the new UI-facing `evidence_detail`
metadata response, which contains no embedding vectors, image bytes, crop bytes,
or base64 values.

## Limitations

- Stable sink workaround remains active:
  `evidence_capture_mode=stable_post_savant_sink_time_crop`.
- `workaround_used=true`.
- `event_style_replay_job_passed=false`.
- C2.3B event-style Replay is still not passed.
- Live API HTTP runtime is not required when API TestClient verifies the route.
- This phase does not render the evidence viewer.
- This phase does not prove broad recognition accuracy.

## Verification

Required checks:

```bash
python -m pytest harness/tests/test_c2_8_api_evidence_detail.py -q
python -m pytest harness/tests/test_c2_7_event_worker_persistence_api_query.py -q
python -m pytest harness/tests/test_c2_6r_redis_consumer_watchlist_event.py -q
python -m py_compile scripts/tools/build_c2_7_persist_watchlist_event.py
python -m py_compile scripts/tools/build_c2_8_api_evidence_detail.py
git diff --check
bash -n scripts/smoke/current/check_c2_8_api_evidence_detail.sh
```

Runtime smoke:

```bash
bash scripts/smoke/current/check_c2_8_api_evidence_detail.sh
```

## Next Options

- C2.9 long-running integrated worker smoke.
- C2.10 evidence-viewer runtime recovery.
- C2.3B-H Replay hardening.
