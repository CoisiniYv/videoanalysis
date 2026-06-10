# Phase C2.12B - External Person Watchlist Evidence Search

## Goal

C2.12B searches the C2.12A external Reese / Finch gallery embeddings against
real video-derived `face_observations`. It only builds watchlist evidence when
a Reese or Finch external gallery row matches a real video observation above
threshold and that observation is directly joinable to the existing C2 stable
sidecar.

This phase is not broad accuracy testing, not Replay repair, and not a new
model pipeline phase.

## Inputs

External gallery inputs from C2.12A:

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

Stable C2 evidence bundle used only for direct sidecar join checks:

`/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035`

## Search Method

The tool uses PostgreSQL pgvector cosine distance directly:

```sql
1 - (face_observations.embedding <=> person_gallery_embeddings.embedding)
```

The search output includes metadata only. Embedding vectors are never written to
JSON, reports, event payloads, or smoke output.

Default diagnostic settings:

- `top_k=10`
- `threshold=0.65`

The threshold is intentionally lower than gallery self-check thresholds because
external photo to video observation matching is not an exact self-match. A
gallery self-match is never accepted for C2.12B PASS.

## Result Modes

`PASS_C2_12B_EXTERNAL_PERSON_WATCHLIST_EVIDENCE_READY` requires:

- Reese or Finch gallery matches a real `face_observation` above threshold.
- The match is not a gallery self-match.
- The match has a real `source_observation_id`.
- The observation is directly joinable to the C2 stable sidecar.
- Evidence bundle is generated without changing geometry.

`PARTIAL_C2_12B_MATCH_FOUND_EVIDENCE_JOIN_GAP` applies when a real match is
above threshold but is not joinable to the stable sidecar.

`PARTIAL_C2_12B_NO_VIDEO_MATCH_FOUND` applies when video observations exist but
none match Reese or Finch above threshold.

`PARTIAL_C2_12B_NO_VIDEO_OBSERVATIONS_AVAILABLE` applies when no video
observations with embeddings exist.

## Output

Diagnostic output pattern:

`/data/video-analytics/media/evidence/c2_12b_external_watchlist_search_YYYYMMDDTHHMMSS`

Files:

- `candidate_observation_inventory.json`
- `reese_top_matches.json`
- `finch_top_matches.json`
- `unsafe_payload_scan.json`
- `c2_12b_search_summary.json`
- `decision_report.md`

If PASS is possible, the output directory also contains:

- `raw_clip.mov` or `raw_clip.mp4` copied from the stable bundle
- `annotations.frame_cache.identity.jsonl`
- `summary.json`
- `watchlist_event.json`
- `match_report.json`
- `operator_external_watchlist_report.html`
- `c2_12b_external_watchlist_summary.json`

## Current Run

Output:

`/data/video-analytics/media/evidence/c2_12b_external_watchlist_search_20260608T005309`

Result marker:

`PARTIAL_C2_12B_NO_VIDEO_MATCH_FOUND`

Current `face_observations` inventory:

- total observations: `1`
- observations with embedding: `1`
- source/camera: `c2_post_savant_fps_probe`

Top matches:

- Reese:
  - `source_observation_id=face:c2_post_savant_fps_probe:4:17854:1`
  - `similarity=0.5765962148043214`
  - `threshold=0.65`
  - `threshold_passed=false`
  - sidecar join: `true`
- Finch:
  - `source_observation_id=face:c2_post_savant_fps_probe:4:17854:1`
  - `similarity=-0.11739943532402841`
  - `threshold=0.65`
  - `threshold_passed=false`
  - sidecar join: `true`

Decision:

The only current video-derived face observation is directly joinable to the C2
stable sidecar, but neither Reese nor Finch matched it above the C2.12B
external-photo-to-video threshold. No watchlist evidence bundle was generated,
and no Reese / Finch watchlist hit is claimed.

## Safety Boundaries

- No fake match is allowed.
- Gallery self-match cannot produce PASS.
- `test:c2_4:person` cannot be used for C2.12B result.
- Event/report/API-style output must not include embeddings.
- Event/report/API-style output must not include image bytes, base64, or crop
  bytes.
- DB window fallback is not used.
- Legacy annotation fallback is not used.
- Evidence geometry is not changed by identity patching.
- Event-style Replay remains not passed and is not claimed.

## Current Limitations

- C2.12B depends on whether Reese or Finch actually appear in the current video
  observations.
- Current C2 DB may contain only the deterministic C2 stable-sink observation.
- This is not a broad recognition accuracy test.
- `watchlist_rules` table remains absent; the C2.12B test rule contract uses
  `c2_12b_external_person_watchlist_rule`.
- Stable sink workaround remains active if a bundle is generated:
  `stable_post_savant_sink_time_crop`.
- Event-style Replay is still not passed.

## Verification Commands

```bash
python -m pytest harness/tests/test_c2_12b_external_watchlist_evidence_contract.py -q
python -m pytest harness/tests/test_c2_12a_external_face_enrollment_contract.py -q
python -m py_compile scripts/tools/build_c2_12b_external_watchlist_evidence.py
git diff --check
bash -n scripts/smoke/current/check_c2_12b_external_watchlist_evidence.sh
bash scripts/smoke/current/check_c2_12b_external_watchlist_evidence.sh
```

The smoke is read-only for DB, Redis, Replay, workers, and Savant. It writes
only diagnostic artifacts under `/data/video-analytics/media/evidence`.
