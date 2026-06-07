# Phase C2.6R - Redis Consumer Loop Watchlist Event Proof

## Result

Expected result marker:

`PASS_C2_6R_REDIS_CONSUMER_WATCHLIST_EVENT_READY`

C2.6R closes the gap between the C2.6 face-worker matching harness and the
runtime Redis event production path. It uses isolated Redis streams and a
single-message consumer-group read, not the long-running face-worker loop.

## Inspection Summary

Current face-worker consumer pieces:

- `services/face-worker/app/redis_consumer.py`
  - `RedisStreamConsumer.ensure_group()`
  - `RedisStreamConsumer.read_new()`
  - `RedisStreamConsumer.ack()`
- `services/face-worker/app/worker.py`
  - `_parse_observation()` parses `security.face_observations` messages.
  - `_validate_embedding()` validates AdaFace embeddings.
  - `_process_batch()` exists, but duplicate `source_observation_id` rows do not
    trigger watchlist emission, which makes it unsuitable for the fixed C2.6
    observation without extra data changes.
- `services/face-worker/app/vector_store.py`
  - `FaceVectorStore.search_gallery()` performs pgvector gallery matching.
- `services/face-worker/app/face_match_event_service.py`
  - `build_watchlist_hit_event()` creates the watchlist SecurityEvent.
  - `publish_security_event()` writes to Redis event streams.

Answers:

1. A reusable one-message path exists through `RedisStreamConsumer.read_new()`,
   `_parse_observation()`, `_validate_embedding()`, vector search, and event
   publish.
2. The face-worker event service writes watchlist events to Redis streams.
3. Redis consumer group setup is required and is handled by
   `RedisStreamConsumer.ensure_group()`.
4. A one-message smoke is safe when isolated streams are used.
5. No runtime worker refactor is required for the isolated one-message proof.

## Execution Mode

C2.6R uses Mode A:

`Mode A Redis stream one-message consumer`

Isolated streams:

- input stream: `c2_6r.face_observations.test`
- output stream: `c2_6r.security.events.test`

The smoke:

1. Deletes only the isolated test streams before the run.
2. XADDs one test `face_observation` message built from the existing DB
   observation.
3. Reads it with `RedisStreamConsumer.read_new()` via a consumer group.
4. Parses and validates it with face-worker worker functions.
5. Runs `FaceVectorStore.search_gallery()`.
6. Writes a `match_results` row.
7. Builds a `watchlist_hit` event with `build_watchlist_hit_event()`.
8. Publishes it to the isolated output stream with `publish_security_event()`.
9. ACKs the isolated input message.
10. Deletes only the isolated test streams when cleanup is enabled.

It does not consume production `security.face_observations` and does not run the
infinite face-worker loop.

## Input Observation

Source observation:

- `source_observation_id=face:c2_post_savant_fps_probe:4:17854:1`
- `camera_id=c2_post_savant_fps_probe`
- `source_id=c2_post_savant_fps_probe`
- DB observation `track_id=4`
- evidence sidecar `track_id=1`
- `frame_num=194`
- `frame_pts=17854288888`

The DB/evidence track id mismatch is expected from the stable sink crop path.
C2.6R does not use track id alone as an identity join key.

Primary identity join key:

`source_observation_id`

Summary fields:

- `db_observation_track_id`
- `evidence_sidecar_track_id`
- `track_id_join_warning=true`
- `primary_identity_join_key=source_observation_id`

## Emitted Event Payload

The emitted event is written to `redis_watchlist_event.json` and
`live_watchlist_event.json` in the evidence bundle.

Required fields:

- `schema_version=1.0`
- `event_type=watchlist_hit`
- `source_event_id=c2_6r:watchlist_hit:...`
- `producer=face-worker-one-message-consumer`
- `source_observation_id`
- `person_id`
- `external_person_id`
- `gallery_embedding_id`
- `match_result_id`
- `similarity`
- `threshold`
- `watchlist_rule_id=c2_6r_test_watchlist_rule`
- `frame_pts`
- `frame_num`

Payload requirements:

- `identity_source=face_worker_pgvector_match`
- `watchlist_match_source=face_worker_consumer`
- `embedding_included=false`
- `image_bytes_included=false`
- `primary_identity_join_key=source_observation_id`
- `track_id_join_warning=true`
- `evidence_capture_mode=stable_post_savant_sink_time_crop`
- `workaround_used=true`
- `event_style_replay_job_passed=false`

The event payload must not contain embedding vectors, image bytes, crop bytes,
frame bytes, or base64 images.

## Output Bundle

Output bundle:

`/data/video-analytics/media/evidence/c2_6r_redis_watchlist_YYYYMMDDTHHMMSS`

Required files:

- `raw_clip.mov`
- `sink_metadata.json`
- `annotations.frame_cache.identity.jsonl`
- `summary.json`
- `identity_patches.jsonl`
- `live_watchlist_event.json`
- `redis_watchlist_event.json`
- `c2_6r_redis_watchlist_summary.json`

Required summary fields:

- `event_type=watchlist_hit`
- `watchlist_hit_count=1`
- `live_watchlist_from_face_worker=true`
- `redis_consumer_loop_verified=true`
- `face_worker_one_message_entrypoint_verified=true`
- `face_worker_match_verified=true`
- `identity_binding_connected=true`
- `known_face_count>=1`
- `evidence_capture_mode=stable_post_savant_sink_time_crop`
- `workaround_used=true`
- `event_style_replay_job_passed=false`
- `fallback_used=false`
- `legacy_used_for_visual_binding=false`
- `production_ready=true`
- `video_integrity.production_gate_passed=true`
- `track_id_join_warning=true`

## Limitations

- This is a one-message Redis consumer proof, not a long-running worker soak.
- Stable sink workaround remains active.
- Event-style Replay is still not passed.
- This is not a broad recognition accuracy test.
- Viewer HTTP is not required.

## Verification

Required checks:

```bash
python -m pytest harness/tests/test_c2_6r_redis_consumer_watchlist_event.py -q
python -m pytest harness/tests/test_c2_6_live_watchlist_from_face_worker.py -q
python -m pytest harness/tests/test_c2_5_watchlist_evidence_semantics.py -q
python -m pytest harness/tests/test_c2_4_identity_patch_contract.py -q
python -m py_compile scripts/tools/build_c2_live_watchlist_evidence_bundle.py
git diff --check
bash -n scripts/smoke/current/check_c2_6r_redis_consumer_watchlist_event.sh
```

Runtime smoke:

```bash
bash scripts/smoke/current/check_c2_6r_redis_consumer_watchlist_event.sh
```

## Next Options

- C2.7 event-worker persistence / API event query.
- Long-running face-worker loop smoke.
- Viewer runtime recovery.
- C2.3B-H Replay hardening.
