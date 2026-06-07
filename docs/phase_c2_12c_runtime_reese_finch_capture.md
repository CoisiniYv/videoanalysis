# Phase C2.12C-A - Runtime Reese / Finch Capture

## Direction

The user clarified that the current runtime is looping the same movie and that
this movie should contain Reese / Finch. C2.12C therefore follows route A:

```text
current source-adapter / looped movie
-> current Savant runtime pipeline
-> current post-Savant face observation stream
-> current face-worker/DB path where available
-> Reese / Finch gallery search
```

This phase does not use random video discovery as the primary route and does
not use offline video tooling as the primary route.

## Inputs

C2.12A gallery:

- Reese:
  - `person_id=5`
  - `external_person_id=demo:f4_3:reese`
  - `gallery_embedding_id=4`
  - `embedding_model=adaface`
- Finch:
  - `person_id=6`
  - `external_person_id=demo:f4_3:finch`
  - `gallery_embedding_id=5`
  - `embedding_model=adaface`

Runtime stream:

- Redis URL: `redis://127.0.0.1:6395/0`
- stream: `security.face_observations`
- source_id observed: `c2_post_savant_fps_probe`

## Runtime Pipeline Status

Containers observed:

- `c2-poc-source-adapter`: running
- `c2-poc-savant`: running and healthy
- `c2-poc-video-file-sink`: running
- `c2-poc-redis`: running and healthy, host port `6395`
- `phase0-postgres`: running
- `c2-poc-evidence-viewer`: running

No face-worker container was running in the inspected container list.

Savant logs show current face detection / embedding activity:

- `face_assoc`
- `face_reid_gate`
- `face_obs_export`
- `face_embedding`

Redis stream status showed `security.face_observations` populated with runtime
messages. PostgreSQL initially had only the single historical C2 observation,
so the missing leg is the face-worker/DB writer path, not Savant face
observation generation.

## Capture Mode

The C2.12C tool:

`scripts/tools/probe_c2_12c_runtime_reese_finch_capture.py`

Capture behavior:

1. Inspect current containers, Redis, PostgreSQL, and Savant logs.
2. Try bounded `XREAD` for new `security.face_observations` messages.
3. If no new message arrives in the bounded window but the runtime stream has
   backlog, read a bounded recent sample with `XREVRANGE`.
4. Do not ACK or delete production Redis messages.
5. Write only namespaced C2.12C DB rows:
   `face:c2_12c_runtime:...`
6. Search only the captured/namespaced rows against Reese / Finch gallery.
7. Do not output embedding vectors or image/crop bytes.

The backlog sample mode is still route A because it reads the current runtime
Savant face observation stream. It is not offline video processing.

## Current Result

Smoke output:

`/data/video-analytics/media/evidence/c2_12c_runtime_capture_search_20260608T011352`

Result marker:

`PARTIAL_C2_12C_MATCH_FOUND_EVIDENCE_JOIN_GAP`

Capture summary:

- runtime source_id: `c2_post_savant_fps_probe`
- Redis stream: `security.face_observations`
- captured/namespaced DB observations: `200`
- Redis read mode: `xrevrange_non_destructive_backlog_sample`
- DB namespace: `face:c2_12c_runtime`

Top match:

- Finch:
  - `external_person_id=demo:f4_3:finch`
  - `person_id=6`
  - `gallery_embedding_id=5`
  - `original_source_observation_id=face:c2_post_savant_fps_probe:2196:36708508`
  - C2.12C source_observation_id:
    `face:c2_12c_runtime:c2_12c_runtime_capture_search_20260608T011352:c2_post_savant_fps_probe:2196:36708508288888:6_1780849127595-0`
  - `similarity=0.690014918944816`
  - threshold: `0.65`
  - threshold passed: true

Decision:

A real runtime/video-derived Finch match was found above threshold from the
current looped movie stream. No PASS was claimed because no direct sidecar
visual join was found for this runtime observation. The correct marker is
`PARTIAL_C2_12C_MATCH_FOUND_EVIDENCE_JOIN_GAP`.

## Safety Boundaries

- No fake match is allowed.
- Gallery self-match cannot produce PASS.
- `test:c2_4:person` is not used for C2.12C success.
- Redis image/crop bytes are not used.
- Embeddings are stored only in the DB vector column for captured observations;
  they are not written to JSON/report/event payloads.
- No image bytes, crop bytes, or base64 are written to JSON/report/event
  payloads.
- Track ID alone is not used as an identity join key.
- Event-style Replay is not repaired or claimed passed.

## Limitations

- Bounded windows can miss a target scene.
- The current face-worker/DB writer is not running as a live container in the
  inspected stack; C2.12C uses a bounded non-destructive Redis capture and
  C2.12C namespaced DB writes.
- A runtime Finch match exists, but visual evidence is blocked by sidecar join.
- This is not broad recognition accuracy testing.
- Event-style Replay is still not passed.
- Stable sink workaround may still be needed once a sidecar/evidence bundle is
  generated for the runtime observation.

## Verification Commands

```bash
python -m pytest harness/tests/test_c2_12c_runtime_reese_finch_capture.py -q
python -m pytest harness/tests/test_c2_12b_external_watchlist_evidence_contract.py -q
python -m py_compile scripts/tools/probe_c2_12c_runtime_reese_finch_capture.py
git diff --check
bash -n scripts/smoke/current/check_c2_12c_runtime_reese_finch_capture.sh
bash scripts/smoke/current/check_c2_12c_runtime_reese_finch_capture.sh
```
