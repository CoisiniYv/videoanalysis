# midterm Qdrant final 60-route 8 FPS pressure report

Date: 2026-06-29

## Scope

This rerun validates the current Qdrant/face-worker cutover after clearing old
pressure-test runtime residue. Existing pressure documents were kept; old
runtime artifacts and pressure-prefixed DB rows were removed before the run.

Run:

```text
RUN_ID=pressure60_qdrant_final_8fps_20260629T150103Z
ARTIFACT_DIR=/data/video-analytics/artifacts/pressure60_qdrant_final_8fps_20260629T150103Z
REPORT=/data/video-analytics/artifacts/pressure60_qdrant_final_8fps_20260629T150103Z/report.json
```

Command profile:

```text
--streams 60
--fps 8/1
--duration-s 300
--drain-s 600
--keep-evidence 50
--evidence-policy-groups 2:2,3:3,5:5,8:8,10:10,15:15
--dual-shard-same-gpu
--dual-shard-api
--dual-shard-gpu 0
FACE_VECTOR_BACKEND=qdrant
QDRANT_FALLBACK_TO_PGVECTOR=false
QDRANT_BATCH_QUERY_ENABLED=false
```

## Result

Status: `passed`

Failure reasons: `[]`

Warning: `validate_seq_iq_expected_sampling_gap`. This is expected sampling-gap
noise, not a send-failure, queue-full, source-exit, or negative-PTS failure.

## Evidence Acceptance

After cleanup, the retained run state was:

| Metric | Result |
| --- | ---: |
| retained evidence | 50/50 |
| playable bundles | 50/50 |
| 8090 evidence checked | 50/50 OK |
| 8090 evidence index source | `database` |
| retained event types | 30 `watchlist_hit`, 20 `intrusion` |

Evidence generation latency from `media-worker` logs:

| Metric | p50 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: |
| queue wait | 109.273s | 192.213s | 216.541s | 227.320s |
| lifecycle elapsed | 113.983s | 196.192s | 221.247s | 232.385s |
| finalization duration | 5.186s | 8.141s | 8.635s | 8.693s |
| post-Savant finalization | 5.149s | 8.102s | 8.610s | 8.677s |
| ffprobe duration | 0.255s | 0.416s | 0.485s | 0.514s |
| ffmpeg duration | 0.000s | 0.000s | 0.000s | 0.000s |

Interpretation: finalization itself is bounded to single-digit seconds at p99.
The remaining long tail is queue/proof/replay/sink wait before finalization
starts, not Qdrant lookup.

## Qdrant And Face-Worker

| Metric | Result |
| --- | ---: |
| Qdrant queries | 3283 |
| Qdrant query p50/p95/p99/max | 2ms / 4ms / 5ms / 9ms |
| exact rerank p50/p95/p99/max | 1ms / 2ms / 3ms / 8ms |
| face-worker gallery query p50/p95/p99/max | 3ms / 6ms / 8ms / 12ms |
| Qdrant fallback count | 0 |
| shadow mismatch count | 0 |
| Qdrant outbox active | 0 |
| watchlist hits emitted | 630 |
| watchlist emit failed | 0 |
| gallery query failed | 0 |
| face-worker consumer pending / lag | 0 / 0 |

This confirms registered-gallery vector lookup is not the current pressure
bottleneck at this scale.

## Downstream Health

| Gate | Result |
| --- | ---: |
| analysis-forwarder queue_full samples | 0 |
| Savant send failures | 0 |
| media finalizer failed | 0 |
| duplicate materialization | 0 |
| imageio fallback | 0 |
| media finalizer workers used | 4 |
| media finalized count | 87 |

`security.record_requests` still had pending 21 / lag 58 at the observability
snapshot, but this was not a retained-evidence failure: the selected 50 bundles
were materialized and queryable through 8090. The next evidence-latency work
should split proof wait, Replay job elapsed, sink-ready wait, and finalizer
start delay.

## Pre-run Cleanup

Before this run:

- pressure-prefixed DB rows were cleared:
  - events: 432
  - evidence_bundles: 415
  - evidence_tasks: 432
  - face_observations: 249
  - person_bbox_observations: 217
- 415 old pressure evidence directories were removed;
- 33 old `pressure60_*` / `qdrant_scale` artifact directories were removed;
- `/data/video-analytics/media/.trash/evidence` was cleared;
- pressure documents under `docs/` were not deleted.

After cleanup and this run, the remaining pressure-prefixed DB state is the
current retained set:

```text
events: 50
evidence_bundles: 50
face_observations: 4
cameras: 0
```

## Scale Benchmark Companion

The companion 20,000-vector benchmark was rerun after artifact cleanup:

```text
/data/video-analytics/artifacts/qdrant_scale/qdrant_gallery_scale_5000x4_rerun_20260629T150018Z.json
```

| Target size | p50 | p95 | p99 | max | top1 self | missing self |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 0.857ms | 1.587ms | 4.863ms | 7.258ms | 1.0 | 0 |
| 20 | 1.199ms | 1.691ms | 2.846ms | 3.537ms | 1.0 | 0 |
| 200 | 1.503ms | 1.757ms | 2.386ms | 3.876ms | 1.0 | 0 |
| all | 1.981ms | 4.275ms | 6.801ms | 7.833ms | 1.0 | 0 |

Benchmark acceptance: `passed`.

