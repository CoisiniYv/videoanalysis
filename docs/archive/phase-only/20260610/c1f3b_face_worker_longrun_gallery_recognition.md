# C1F.3b — Production-like Face Worker Long-run Gallery Recognition

## Why C1F.3 Was Not Production-grade

C1F.3 returned `PASS_NO_MATCH_PIPELINE_OK`. It proved that archived Finch/Reese
images could be registered, C1 runtime could emit Redis face observations,
PostgreSQL could store `face_observations`, and gallery search could run.

It was not production-grade because observation ingest used a one-shot
parser/validator/repository compatibility path from the smoke script. The C1
official replay compose did not run a persistent `face-worker` service consuming
`security.face_observations`.

## Production-like Definition

C1F.3b validates this path:

```
RTSP
  -> Source Adapter
  -> Replay
  -> Savant module.c1f2d_face_observation_redis.yml
  -> Redis security.face_observations
  -> face-worker service
  -> PostgreSQL face_observations
  -> gallery search / match_results
```

Production-like means the runtime ingest path is a service, not a smoke-script
shortcut. The smoke starts `face-worker` from
`infra/docker-compose.c1-official-replay-dev.yml`, and the worker reads Redis
using a consumer group.

## Face-worker Service

The C1 official dev compose now includes `face-worker`:

- container: `c1-official-face-worker`
- input stream: `security.face_observations`
- consumer group: `face-worker`
- consumer name: `c1f3b-smoke`
- database: `postgresql://video:video@postgres:5432/video_analytics`
- GPU: not requested
- real-time embedding: not performed by the worker

The worker consumes Redis stream messages, validates AdaFace vectors, and writes
`face_observations`. Gallery matching remains a separate smoke-time search step.

## ACK After DB Commit

`face-worker` ACKs Redis only after `FaceObservationRepository.insert_observation`
returns successfully. The PostgreSQL connection uses autocommit, so successful
insert or duplicate no-op is committed before ACK. DB failures are not ACKed,
leaving the message pending for recovery.

## Idempotency

`face_observations.source_observation_id` is unique. The repository uses:

```sql
ON CONFLICT (source_observation_id) DO NOTHING
```

Duplicate Redis messages are ACKed only after the duplicate no-op succeeds in
PostgreSQL. The long-run smoke verifies that
`COUNT(*) = COUNT(DISTINCT source_observation_id)`.

## Active Registration Asset Path

Archived files are recovery inputs only:

- `/data/video-analytics/archive/r2_3_20260529_052937/repo_uncommitted/face/finch.jpg`
- `/data/video-analytics/archive/r2_3_20260529_052937/repo_uncommitted/face/reese.jpg`

C1F.3b copies them into active media registration paths:

- `/data/video-analytics/media/face-registration/finch.jpg`
- `/data/video-analytics/media/face-registration/reese.jpg`

The deterministic external ids remain:

- `test:archive:finch`
- `test:archive:reese`

For these test persons only, the smoke keeps one active primary gallery embedding
per person pointing at the active media path. Old archive-path gallery rows may
be soft-deactivated, but archive files are not deleted.

## Long-run Movie Soak Strategy

The smoke runs the fixed RTSP movie stream through the same C1F.2d Savant module
for a longer configurable duration:

```bash
RUN_DURATION_SEC=600 scripts/smoke/check_c1f3b_face_worker_longrun_gallery_recognition.sh
```

Default:

- `RUN_DURATION_SEC=600`
- `RTSP_TRANSPORT=tcp`
- `SAVANT_MODULE_FILE=module.c1f2d_face_observation_redis.yml`
- `FACE_OBSERVATION_EXPORT_ENABLED=true`

Longer runs can be requested:

```bash
RUN_DURATION_SEC=1200 scripts/smoke/check_c1f3b_face_worker_longrun_gallery_recognition.sh
RUN_DURATION_SEC=1800 scripts/smoke/check_c1f3b_face_worker_longrun_gallery_recognition.sh
```

The fixed movie may not contain Finch or Reese during a given run. A no-match
result can still pass if Redis export, face-worker ingest, PostgreSQL storage,
and gallery search all operate correctly.

## Result Semantics

- `PASS_MATCH`: Finch or Reese is matched at or above the threshold.
- `PASS_NO_MATCH_PRODUCTION_PIPELINE_OK`: Redis messages were generated,
  face-worker inserted PostgreSQL observations, gallery search executed, but no
  Finch/Reese match reached threshold.
- `FAIL_FACE_WORKER_INGEST`: face-worker did not consume messages or insert new
  observations.
- `FAIL_NO_REDIS_MESSAGES`: Savant did not produce new
  `security.face_observations` messages.
- `FAIL_NO_GALLERY`: no active valid gallery embeddings exist.
- `FAIL_SEARCH_ERROR`: gallery search failed.
- `FAIL_IMAGE_BYTES`: Redis or DB payload contains image, crop, raw, or base64
  bytes.

## Not Validated

C1F.3b explicitly does not validate or implement:

- `watchlist_hit`
- `live_search_hit`
- API/frontend flows
- production evidence clip generation
- source extraction or manual ffmpeg clipping
- second RTSP intake

YOLOv8-Face remains a full-frame primary detector. The face-worker remains a CPU
business worker and does not run real-time GPU embedding.

## Validation Commands

```bash
bash -n scripts/smoke/check_c1f3b_face_worker_longrun_gallery_recognition.sh
python -m py_compile services/face-worker/app/config.py services/face-worker/app/worker.py services/face-worker/main.py services/face-worker/register_face_image.py services/face-worker/match_gallery.py
pytest harness/tests/test_c1f3b_face_worker_longrun_production_contract.py
docker compose -f infra/docker-compose.c1-official-replay-dev.yml config
git diff --check
RUN_DURATION_SEC=600 scripts/smoke/check_c1f3b_face_worker_longrun_gallery_recognition.sh
```

## Next Steps

- If `PASS_MATCH`: proceed to watchlist/live_search audit.
- If `PASS_NO_MATCH_PRODUCTION_PIPELINE_OK`: use a video containing Finch/Reese
  or run a positive self-match production control.
- If `FAIL_FACE_WORKER_INGEST`: fix the face-worker service consumption path
  before extending recognition behavior.
