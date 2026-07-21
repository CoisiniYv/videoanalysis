# Media-worker rolling segment index capacity closure (2026-07-21)

## Status

Ongoing. This document is a resumable measurement and change ledger, not a
completion claim. Neither the two retention short gates nor the two one-hour
acceptance runs have passed yet.

- Branch: `codex/segment-index-concurrency-fix-20260721`
- Clean baseline: `01b62acbc72aab9de57263425f3d9dea64d8827f`
- Fixed input SHA256:
  `42d477ae2bc4eadf4dcd6192ef9a963926e1d88b1755c59e935270230d047490`
- Candidate B capacity: WIP 20, remux 12, max-per-poll 8, finalizer threads 8,
  finalizer process workers 4, finalizer queue 8
- Correctness fences retained: filesystem mutation flock, read-pin marker,
  metadata/video identity checks, same-filesystem atomic rename, fenced
  finalizer admission, exact lease transfer, and PostgreSQL as the only
  durable finalizer queue.

## Commit ledger

| Commit | Dimension | Result |
| --- | --- | --- |
| `23793a5` | pre-pin through handoff, index, and full scheduler-cycle timing | Added observability without changing scheduling |
| `f07f4a5` | instrumentation tests | Timing schema and aggregation covered |
| `98b8c7c` | short catalog-map lock, per-catalog lock/version, COW publication, lock-free filesystem work, independent byte-bounded row cache, non-blocking snapshot | Removed the global Python-lock convoy as the primary hypothesis |
| `75f76f6` | concurrency/COW/cache tests | Lock isolation, version conflict, and cache bounds covered |
| `b241abe`, `f76cc93` | explicit retention pressure axis | Harness accepts and tests 300s and 3,840s retention overrides |
| `652621d` | bounded pressure log capture | Sampling now closes before log collection; each log has a fixed `--until`, 30s timeout, and partial-output diagnostics |
| `263411f` | compact per-segment manifest | Sink publishes a manifest inside the atomic segment rename; index discovery no longer parses native JSONL |
| `dbb872b` | manifest/index tests | Atomic publication, manifest-only discovery, lazy selected-row parsing, malformed/missing manifest, COW, retention, pin, and cache behavior covered |

## Measurement rounds

### Round 1: lock isolation with native-JSONL discovery

- Hypothesis: the process-wide index lock and lock-held filesystem work caused
  the Candidate C ready-to-claim queue.
- Unique implementation variable: `98b8c7c`; Candidate B was otherwise kept
  fixed.
- Configuration: 60 routes, 8 FPS, nominal 600s sample, 120s drain, 300s
  retention.
- Artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_segidxlock_b10m_r300_20260721T1450Z`
- Result: failed diagnostic run, not an acceptance short gate. The old harness
  synchronously ran `docker logs` before sealing the sample. Person/face worker
  log collection blocked long enough to extend the observed window from 600s
  to about 1,124s. The artifact is retained and must not be used as a formal
  pass/fail comparison.

Useful evidence that remains attributable before/through the extended window:

- Input visibility reached 60/60 with zero restart attempts.
- 1,832 tasks ended materialized; attempt-count-zero expiry, active task,
  active lease, and finalizer-pending residual were all zero.
- Ready-to-remux claim p50/p95 was 54.10s/96.33s; oldest-ready p95/max was
  97.84s/129.76s.
- Index lock wait p50/p95 was only 0.027ms/2.672ms. The global Python lock was
  no longer the main capacity limiter.
- Index refresh p95 was 5.44s and rebuild p95 was 6.53s. Cumulative stat time
  reached 568.95s and cumulative full-row parse time reached 166.90s.
- Native metadata was parsed 21,614 times. The 2,048-entry row cache reached
  19,566 evictions and about 107.36MB, proving retention-wide row churn rather
  than useful selected-segment reuse.
- Finalizer pool wait p95 was 4.824s and all lane/durable-finalizer residuals
  returned to zero. Expanding worker counts was therefore not the next
  justified variable.

Conclusion: lock isolation worked, but catalog refresh still parsed historical
native metadata. The next single variable was a compact immutable manifest so
discovery can retain only bounds and identities while native rows remain lazy.

### Round 2: compact manifest / lazy native rows

- Hypothesis: replacing retention-wide native JSONL parsing with compact
  manifest parsing will remove row-cache churn and shorten refresh/rebuild
  enough for service rate to exceed arrival rate.
- Unique implementation variable: `263411f`.
- Publication contract: `segment_manifest.json` contains schema, source/epoch
  identity, segment id, video/metadata filenames and sizes, mux-clock bounds,
  source-clock bounds, and frame count. Metadata and manifest files are fsynced
  in staging, then video + metadata + manifest become visible together through
  the existing directory `os.replace`.
- Discovery contract: the index scans only `segment_manifest.json`; validates
  manifest/source/epoch/path/size identities; publishes catalog snapshots by
  versioned COW; and parses native `metadata.json` only through
  `rows_for_segment()` for mapping/materialization candidates.
- Red-to-green proof: the focused manifest/index suite initially had 11
  failures, then passed 50/50. The wider media-worker, sink, and pressure
  harness selection passed 362 tests with one expected skip.
- Pressure result: pending.

## Recovery audit after Round 1

The failed artifact was preserved. The harness restored the daily single
branch, stopped all 60 pressure sources, ffmpeg publishers, and the local
MediaMTX container, disabled all pressure cameras, and restored rolling-cache
materialization to its default disabled state and retention to 300s. Redis
`save` returned to `3600 1 300 100 60 10000`; PostgreSQL returned to
`checkpoint_timeout=5min`, `max_wal_size=1GB`, and `min_wal_size=80MB`.
Post-cleanup PostgreSQL contained only materialized tasks and zero active
leases/finalizer-pending rows.

## Next gates

1. Recreate the bind-mounted media-worker and rolling-cache sink services and
   prove a live segment contains a valid compact manifest.
2. Run the unchanged Candidate B load for 10-15 minutes with 300s retention.
3. If and only if it passes, repeat with 3,840s retention.
4. Analyze manifest/stat/refresh/rebuild timing and service-rate/oldest-ready
   slope before changing another dimension.
5. Only after both short gates pass, run two comparable one-hour acceptances
   with the fixed fixture/hash and the full evidence/8090 validation set.

