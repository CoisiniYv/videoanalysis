# C1F.3 — Restore Archived Face Registration + Gallery Match Smoke

## Goal

C1F.3 restores the two archived external face registrations and runs one
minimal gallery recognition smoke:

```
archived finch/reese images
  -> persons
  -> person_gallery_embeddings

RTSP -> Source Adapter -> Replay -> Savant
  -> Redis security.face_observations
  -> face-worker repository ingest
  -> PostgreSQL face_observations

face_observations
  -> pgvector gallery search
  -> match_results when threshold candidates exist
  -> PASS_MATCH or PASS_NO_MATCH_PIPELINE_OK
```

This phase validates the smallest registration + observation ingest + gallery
search loop. It is not a production evidence phase.

## Background

The current C1 checks showed:

- active persons: `0`
- active gallery embeddings: `0`
- the C1 official PostgreSQL data directory was empty
- `phase0-postgres` had the old schema and must not be used for C1F.3

C1F.3 therefore uses the current C1 official PostgreSQL container from
`infra/docker-compose.c1-official-replay-dev.yml`:

- container: `c1-official-postgres`
- database: `video_analytics`
- schema source: `db/migrations`
- required extension: `vector`

## Archived Images

The registration inputs are existing archive files. The smoke must not delete
or rewrite them:

- Finch:
  `/data/video-analytics/archive/r2_3_20260529_052937/repo_uncommitted/face/finch.jpg`
- Reese:
  `/data/video-analytics/archive/r2_3_20260529_052937/repo_uncommitted/face/reese.jpg`

The deterministic person identifiers are:

- `test:archive:finch`
- `test:archive:reese`

## Why Registration First

Gallery recognition cannot run with an empty `persons` table or empty
`person_gallery_embeddings`. C1F.3 first restores external image registration
with `services/face-worker/register_face_image.py`, then verifies that the
runtime observations can be compared against those active gallery embeddings.

## Registration Acceptance

Each archived person must have:

- active row in `persons`
- at least one active row in `person_gallery_embeddings`
- `embedding_dim = 512`
- `embedding_norm` between `0.90` and `1.10`
- `embedding_model = 'adaface'`
- `source_image_path` pointing to the archived image path
- no image bytes, crop bytes, raw bytes, or base64 payload in PostgreSQL

The smoke is idempotent: when a valid active gallery row already exists for the
deterministic external id and archived image path, it reuses that row instead of
creating another gallery embedding.

## Face Observation Ingest Acceptance

C1F.3 reuses the C1F.2d Redis producer path when PostgreSQL has no current
usable observations. Because the C1 official replay compose does not include a
face-worker service, the smoke performs a compatibility ingest by calling the
existing face-worker parser, embedding validator, and
`FaceObservationRepository` directly.

Accepted PostgreSQL observations must have:

- unique `source_observation_id`
- `camera_id`, `source_id`, `track_id`, and `timestamp_ms`
- `face_bbox` and `landmarks`
- `embedding_dim = 512`
- `embedding_norm` between `0.90` and `1.10`
- `detector_model = 'yolov8_face'`
- `embedding_model = 'adaface'`
- no duplicate embedding vector inside `payload`
- no image bytes, crop bytes, raw bytes, or base64 payload

Redis is used only for metadata and AdaFace vectors from
`security.face_observations`; it is not used to carry JPEG, PNG, RAW, crop
bytes, or base64 image data.

## Gallery Match Result Types

The smoke selects a recent valid `face_observations` row and searches active
`person_gallery_embeddings` with `top_k = 5` and `min_similarity = 0.5` by
default.

- `PASS_MATCH`: Finch or Reese is returned at or above the threshold.
- `PASS_NO_MATCH_PIPELINE_OK`: gallery embeddings and observations exist, and
  search executes successfully, but no Finch/Reese candidate reaches the
  threshold. This is expected when the fixed RTSP video does not contain Finch
  or Reese.
- `FAIL_NO_GALLERY`: no valid active gallery embedding exists.
- `FAIL_NO_OBSERVATIONS`: no valid PostgreSQL face observation exists.
- `FAIL_SEARCH_ERROR`: gallery search failed or returned no candidates despite
  valid gallery rows.
- `BLOCKED_NO_IMAGE_REGISTRATION_TOOL`: the repository lacks a working external
  image registration path. This should not occur while
  `register_face_image.py` and the offline YOLOv8-Face + AdaFace embedder are
  available.

`PASS_NO_MATCH_PIPELINE_OK` proves the registration, observation ingest, and
gallery search plumbing are runnable. It does not prove a positive identity
recognition for Finch or Reese.

## Not Validated

C1F.3 explicitly does not validate or implement:

- `watchlist_hit`
- `live_search_hit`
- API/frontend behavior
- production evidence generation
- second RTSP intake
- source extraction or ffmpeg clipping

## Smoke Script

Run:

```bash
scripts/smoke/check_c1f3_restore_registration_and_gallery_match.sh
```

The script:

1. checks the archived images exist
2. starts only current C1 official Redis/PostgreSQL
3. verifies the current C1 schema and pgvector extension
4. registers or reuses Finch/Reese with deterministic external ids
5. confirms active gallery embeddings
6. prepares or confirms `face_observations`
7. runs gallery top-k search
8. writes deterministic `match_results` only for this smoke request id
9. outputs `PASS_MATCH`, `PASS_NO_MATCH_PIPELINE_OK`, or `FAIL_*`

The fixed search request id is:

```text
c1f30000-0000-4000-8000-000000000001
```

Only rows for that search request id are cleared before re-running the match
portion. The smoke does not delete archive files, volumes, persons,
gallery embeddings, or face observations.

## Validation Commands

```bash
bash -n scripts/smoke/check_c1f3_restore_registration_and_gallery_match.sh
python -m py_compile services/face-worker/register_face_image.py services/face-worker/match_gallery.py services/face-worker/main.py
pytest harness/tests/test_c1f3_restore_registration_gallery_match_contract.py
docker compose -f infra/docker-compose.c1-official-replay-dev.yml config
git diff --check
scripts/smoke/check_c1f3_restore_registration_and_gallery_match.sh
```

## Next Steps

- If the result is `PASS_MATCH`, proceed to watchlist/live_search audit.
- If the result is `PASS_NO_MATCH_PIPELINE_OK`, prepare a video containing the
  registered people or run a dedicated self-match smoke for positive identity
  proof.
- If the result is `BLOCKED_NO_IMAGE_REGISTRATION_TOOL`, implement the external
  image registration MVP before retrying C1F.3.
