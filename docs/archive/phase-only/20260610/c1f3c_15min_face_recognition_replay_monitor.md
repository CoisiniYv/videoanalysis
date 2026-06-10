# C1F.3c — 15-minute Face Recognition Replay Monitor

## Goal

C1F.3c extends C1F.3b's 10-minute production-like face-worker soak to a 15-minute
run with additional monitoring of the Replay RocksDB cache size, periodic sampling,
and a visual output capability assessment.

The primary goal is to validate that the full face recognition pipeline can sustain
continuous operation for 15 minutes without resource exhaustion, Replay cache
runaway growth, Redis backlog accumulation, or face-worker ingest failure.

## Why 15 Minutes

C1F.3b ran for 600 seconds (10 minutes). C1F.3c extends to 900 seconds (15 minutes)
to:

1. **Replay cache stability** — RocksDB log/sst files grow as the replay service
   buffers video. A 15-minute soak reveals whether the cache stabilizes or grows
   unboundedly.
2. **Redis stream lag** — Under sustained load, the face-worker consumer group lag
   should remain bounded. A 15-minute window exposes any lag drift.
3. **PostgreSQL write throughput** — face_observations inserts should remain stable
   over the full duration without connection pool exhaustion or deadlocks.
4. **GPU memory stability** — Savant DeepStream should not leak GPU memory over 15
   minutes of continuous inference.

## Production-like Face-worker Chain

Identical to C1F.3b:

```
RTSP (rtsp://10.37.57.112:8554/live/1080movie)
  -> Source Adapter
  -> Replay Service (inline)
  -> Savant module.c1f2d_face_observation_redis.yml
     -> YOLO26-pose -> nvtracker -> YOLOv8-Face -> FacePersonAssociator
     -> AdaFace -> FaceReidGate -> FaceObservationExporter
  -> Redis security.face_observations
  -> face-worker service (c1-official-face-worker)
  -> PostgreSQL face_observations
  -> gallery search / match_results
```

No changes to the pipeline topology. The face-worker remains a CPU-only service
consuming from a Redis consumer group.

## Replay File Size Monitoring

The script discovers the Replay RocksDB directory from these candidates:

- `/data/video-analytics/replay-c1-official-replay-dev`
- `/data/video-analytics/replay`
- `/data/video-analytics/replay/rocksdb`
- `/data/video-analytics/media/replay-sink-output`

At baseline (before soak) and at finalize (after soak), the script records the
total byte size of the directory. The difference is `replay_size_growth_bytes`.

During the soak, each 60-second sample also records `replay_dir_size_bytes`.

**Expected behavior:** The Replay service uses RocksDB with a TTL-based cache.
Size should stabilize after initial ramp-up. Unbounded growth indicates a TTL
configuration issue or missing compaction.

## Periodic Sampling

Every `SAMPLE_INTERVAL_SEC` seconds (default 60), the script records a JSONL
sample to:

```
/data/video-analytics/artifacts/c1f3c/15min_face_recognition_monitor.jsonl
```

Each sample contains:

| Field | Description |
|---|---|
| `timestamp` | ISO 8601 UTC |
| `elapsed_sec` | Seconds since soak start |
| `replay_dir_size_bytes` | Replay directory total size |
| `redis_stream_length` | XLEN of security.face_observations |
| `redis_pending_count` | Consumer group pending count |
| `redis_lag` | Consumer group lag |
| `pg_face_observations_count` | Total face_observations rows |
| `face_worker_error_count` | Error lines in face-worker logs |
| `latest_match_candidate` | Most recent valid observation ID |

## Monitoring Metrics

### Redis

- `stream`: `security.face_observations`
- `messages_before` / `messages_after` / `new_messages`
- `consumer_group`: `face-worker`
- `pending_count`: Should be 0 or near-0 after drain
- `lag`: Should be 0 or near-0 after drain
- `forbidden_image_bytes`: Must be 0

### PostgreSQL

- `face_observations_before` / `face_observations_after` / `new_observations_inserted`
- `match_results_before` / `match_results_after`
- `idempotency_verified`: `COUNT(*) = COUNT(DISTINCT source_observation_id)`
- `valid_new_observations`: 512-d adaface yolov8_face with norm 0.90-1.10

### Face-worker

- `face_worker_consumed_messages`: Sum of inserted+duplicates+skipped+failed from logs
- `db_rows_inserted_from_worker_logs`: Successfully inserted count
- `duplicates_from_worker_logs`: Duplicate no-op count
- `face_worker_error_count`: ERROR/Traceback/Exception lines

## Finch/Reese Gallery Search

After the soak and drain, the script searches up to `QUERY_OBSERVATION_LIMIT` (20)
recent valid face observations against the active gallery:

- `top_k=5`
- `threshold=0.5` (cosine similarity)
- Target external IDs: `test:archive:finch`, `test:archive:reese`

For each observation, the script calls `FaceVectorStore.search_gallery()` and
writes `match_results` via `MatchResultRepository.insert_gallery_match_result()`.

### Similarity Threshold Risk

The threshold of 0.5 (cosine similarity) is intentionally low for smoke testing.
In production, the threshold should be higher (0.6-0.7) depending on the false
positive tolerance. A low threshold here maximizes the chance of detecting a match
in the movie stream while accepting higher false positive risk.

The movie may not contain Finch or Reese during a given run. A no-match result is
valid if the pipeline operated correctly.

## Result Semantics

| Result | Meaning |
|---|---|
| `PASS_MATCH` | Finch or Reese matched at or above threshold |
| `PASS_NO_MATCH_PRODUCTION_PIPELINE_OK` | Pipeline ran correctly, no Finch/Reese match |
| `FAIL_NO_REDIS_MESSAGES` | Savant did not produce face observations |
| `FAIL_FACE_WORKER_INGEST` | face-worker did not insert observations |
| `FAIL_REDIS_LAG` | Redis consumer lag did not drain |
| `FAIL_SEARCH_ERROR` | Gallery search failed |
| `FAIL_IMAGE_BYTES_IN_REDIS_OR_DB` | Forbidden image/crop/base64 bytes detected |
| `FAIL_PIPELINE_STOPPED` | Pipeline stopped prematurely |

## Visual Output

The script attempts to generate visual output for gallery recognition results.
However, the current C1 face recognition path stores metadata and embeddings but
does **not** retain frame images or snapshots for matched observations.

The V1 visual result generator (`scripts/debug/generate_visual_result.py`) requires
frame-UUID-aligned proof before drawing bounding boxes. Gallery match_results do
not provide this proof. Therefore:

```
visual_output_status = VISUAL_OUTPUT_NOT_AVAILABLE
reason = current C1 face recognition path stores metadata/embedding
         but does not yet retain frame image/snapshot for matched observation
```

This is **not a failure**. The face recognition pipeline correctly generates
embeddings, stores observations, and performs gallery search. The visual output
limitation is a known gap in the current media pipeline.

### If Visual Output Were Available

Output would go to `/data/video-analytics/artifacts/c1f3c/visual/` containing:

- `report.md` or `index.html`
- `match_summary.json`
- `annotated_snapshot.jpg` (debug only)
- `annotation.json` (debug only)

All visual output would be explicitly marked:

- `production_evidence = false`
- `debug_visual_only = true`

## Not Validated

C1F.3c explicitly does not validate or implement:

- `watchlist_hit` — Not implemented
- `live_search_hit` — Not implemented
- API/frontend flows — Not implemented
- Production evidence clip generation — Not implemented
- Source extraction or manual ffmpeg clipping — Not implemented
- Second RTSP intake — Not implemented
- Image/crop/base64 bytes in Redis or DB — Prohibited

YOLOv8-Face remains a full-frame primary detector. The face-worker remains a CPU
business worker and does not run real-time GPU embedding.

## Validation Commands

### Static

```bash
bash -n scripts/smoke/check_c1f3c_15min_face_recognition_replay_monitor.sh
pytest harness/tests/test_c1f3c_15min_replay_monitor_contract.py
docker compose -f infra/docker-compose.c1-official-replay-dev.yml config
git diff --check
```

### Runtime (15 minutes)

```bash
RUN_DURATION_SEC=900 scripts/smoke/check_c1f3c_15min_face_recognition_replay_monitor.sh
```

### Custom Duration

```bash
RUN_DURATION_SEC=1200 scripts/smoke/check_c1f3c_15min_face_recognition_replay_monitor.sh
```

## Next Steps

- If `PASS_MATCH`: proceed to watchlist/live_search audit.
- If `PASS_NO_MATCH_PRODUCTION_PIPELINE_OK`: use a video containing Finch/Reese
  or run a positive self-match production control.
- If `FAIL_FACE_WORKER_INGEST`: fix the face-worker service consumption path.
- If Replay size grows unboundedly: investigate RocksDB TTL configuration.
- If Redis lag does not drain: investigate face-worker throughput or batch size.
