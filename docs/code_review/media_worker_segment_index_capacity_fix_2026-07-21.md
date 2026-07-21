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
| `3c02771` | mutation-driven reconciliation | Periodic reconciliation compares immutable-leaf membership through tracked parents, spreads first reconcile phase, and carries exact catalog identities into read-pin fencing |
| `a5782d8` | reconciliation/identity tests | No periodic full walk, bounded steady stats, leaf pruning, retention invalidation, and same-size inode replacement fencing covered |
| `2cf5301` | bounded index I/O admission | Caps only discovery-through-pin publication at two concurrent jobs, releases admission before ffmpeg/remux, and exposes job/cumulative wait plus effective capacity |
| `9587dde` | admission/config/observability tests | Covers the concurrency bound, observable slot wait, env clamping, pressure override, and downstream summary extraction |

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

Runtime interoperability proof used the real bind-mounted containers. The sink
published one segment into a temporary shared media root; media-worker observed
`manifest_parses=1`, `full_row_parses=0`, and `row_cache_entries=0` after
discovery, then `full_row_parses=1` and `row_cache_entries=1` only after loading
the selected segment. The temporary root and sink container were removed and
daily `rolling_cache_materialization_enabled=false` / 300s retention restored.

### Round 3: diagnostic 300s-retention run after compact manifest

- Artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_segmanifest_b10m_r300_20260721T1543Z`
- Result: intentionally interrupted diagnostic, not a formal short gate. The
  harness reached 60/60 sources with no restart, then was stopped once the
  failure mechanism was unambiguous so another six minutes of invalid work was
  not admitted.
- Early improvement: discovery parsed 2,466 manifests in about 670ms while 498
  selected/candidate native-row loads took about 797ms; row cache had 498
  entries and zero evictions. By 120s, service rate matched arrival rate and
  oldest-ready had returned to about 1.7s.
- Remaining failure: synchronized 60s periodic catalog reconciliation caused
  repeated 3.8-10.5s scheduler cycles. Oldest-ready rose to 36.6s despite an
  intermittently empty remux lane. In one 16s interval cumulative rebuild time
  rose from 21.2s to 31.2s; cumulative stat time reached 16.5s. This is a
  filesystem/GIL reconcile convoy, not evidence that WIP or remux concurrency
  should be increased.
- Cleanup: the KeyboardInterrupt path restored single-branch runtime, Redis
  and PostgreSQL defaults, 300s retention, and disabled rolling
  materialization. It left zero enabled cameras, source/ffmpeg/MediaMTX
  processes, active leases, or finalizer-pending rows. Eight interrupted tasks
  were explicitly terminal-failed rather than left active.

### Round 4: mutation-driven immutable catalog reconciliation

- Hypothesis: immutable manifest leaves do not need retention-wide payload
  re-stat or a full tree walk every 60s under the pressure override. Atomic
  parent membership plus the retention generation is sufficient for
  discovery/deletion; read-pin must retain exact identity fencing.
- Unique implementation variable: `3c02771`.
- New behavior: steady refresh retries only pending manifests and watches the
  source/segments discovery parents. Parent membership detects atomic additions
  and retention deletions. Periodic reconcile performs the same membership
  audit without `_walk`, and the first reconcile deadline is deterministically
  spread across 0.5-1.5 intervals per source/epoch.
- Correctness: read-pin now compares the catalog's exact metadata/video
  device, inode, size, and mtime identities under the mutation flock. A
  same-size, same-mtime inode replacement is rejected as retryable.
- Verification: focused suite 52/52; combined media-worker, rolling-cache,
  pressure-harness, deployment, topology, and materialization suite 450 passed
  with one expected skip.
- Artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_manifestinc_b10m_r300_20260721T1604Z`
- Result: intentionally interrupted diagnostic, not a formal short gate.
  Mutation-driven reconciliation removed the periodic full-walk/rebuild storm,
  but 12 remux workers still entered Python directory discovery, JSON/stat, and
  read-pin publication concurrently. Across samples 5-8, ready work rose
  `1 -> 18 -> 72 -> 44`, oldest-ready approached 36s, and expiry remained zero.
- For 425 completed jobs, refresh p50/p95/max was
  135.5ms/2,968.7ms/3,815ms; pin publication was
  86.4ms/1,692.6ms/2,627ms; and remux-total was
  1,462ms/5,676ms/6,157ms. Stat p95 was 124.5ms and selected full-row parse p95
  was 20.9ms. The remaining convoy was concurrent Python filesystem/JSON/stat/
  pin amplification, not a reason to add workers.
- Cleanup converged through the KeyboardInterrupt path and restored the daily
  disabled-camera, no-source-process, rolling-materialization-disabled,
  300s-retention state with no active leases or finalizer-pending rows.

### Round 5: bounded discovery and read-pin admission

- Hypothesis: limiting only the filesystem-heavy discovery-through-pin
  publication region to two concurrent jobs will prevent the 12-way Python I/O
  amplification while preserving Candidate B's 12-way ffmpeg/remux capacity.
- Unique implementation variable: `2cf5301`. A process-local bounded semaphore
  covers the shared mutation lock, catalog discovery/refresh, exact identity
  snapshot, read-pin construction, and atomic pin publication. The slot is
  released before materialization begins; the read pin remains active for the
  existing fenced lifetime.
- Observability: every completed job reports
  `segment_index_io_slot_wait_ms`; scheduler snapshots report cumulative
  `segment_index_io_slot_wait_ms_total`; resource logs report
  `segment_index_io_concurrency=2`. The pressure artifact summaries retain all
  three levels so a run that merely transfers delay into admission wait cannot
  be called a pass.
- Verification: combined relevant suite 452 passed with one expected skip;
  disposable PostgreSQL initialized by migrations 001-032 retained all eight
  exact-lease/fenced-finalizer passes; `py_compile`, critical Ruff rules,
  compose rendering, and diff checks passed.
- Runtime interoperability: real bind-mounted sink/media-worker containers
  reported `segment_index_io_concurrency=2`; discovery produced one manifest
  parse with zero native-row parse/cache entry, and loading the selected row
  then produced one native-row parse/cache entry. A finalized job emitted the
  new slot-wait diagnostic. The temporary shared root and containers were
  removed afterward.
- Invalid pressure attempt:
  `/data/video-analytics/artifacts/pressure60_8p1_ioadm2_b10m_r300_20260721T1635Z`.
  It reached 60/60 sources and began sampling, but the root filesystem returned
  `ENOSPC` while writing the first runtime samples. The attempt is retained as
  an environmental failure and is not a capacity pass/fail result. Manual
  recovery restored zero enabled cameras, active tasks, leases, finalizer
  pending rows, pressure processes, and the daily Redis/PostgreSQL/media
  settings.
- Completed 300s-retention diagnostic:
  `/data/video-analytics/artifacts/pressure60_8p1_ioadm2_b10m_r300b_20260721T1705Z`.
  The 600s formal window sustained 60/60 sources at 8.0201 FPS with zero
  sampling-window Savant send failure, forwarder queue-full, or raw loss.
  All 968 formal tasks and all 1,004 warmup/postfill-inclusive tasks eventually
  materialized; attempt-zero expiry, duplicate, claim-busy, finalizer failure,
  handoff recovery, retry failure, and drain residuals were zero. All 1,004
  retained videos passed window, duration, frame-rate, 8090 detail, and timeline
  checks.
- Capacity result: failed. During the formal window cumulative ready arrivals
  versus completions ended at 934/822, about 1.56/1.37 tasks/s. Ready work ended
  at 107 with oldest-ready about 70.6s; scheduler oldest-ready p95/max was
  78.24s/138.65s. Ready-to-remux claim p95 was 70.94s, media queue p95 91.73s,
  and lifecycle p95 92.12s. Although poll-gap p95 was 1.736s and media-worker
  peak CPU was 314.32%, backlog was moved into bounded admission rather than
  removed.
- Attribution: slot wait p50/p95 was 2.012s/4.455s. Pre-pin-through-handoff
  `remux_total` p95 was 6.308s while actual ffmpeg/remux p95 was 0.786s;
  refresh and pin-publication p95 were 1.609s and 1.401s. Compared with Round 4,
  the two-slot bound reduced per-job refresh/pin amplification but its sustained
  service rate stayed about 12% below arrival. The next isolated capacity
  variable is therefore I/O admission `2 -> 3`, not WIP/remux/finalizer growth.
- Full-run validity caveat: this completed run is a media-capacity diagnostic,
  not the required complete r300 gate. A pre-existing
  `person-observation-worker` lost its Redis stream/group and looped on
  `NOGROUP`; trajectory persistence was zero and 34/1,004 videos had no DB
  annotation rows. Unbounded Docker json-file logs reached about 168GB for the
  person worker and 85GB for face-worker, repeatedly pushing the root disk
  toward ENOSPC. The consumer-group recovery and bounded-log remediation must
  pass before repeating the unchanged two-slot baseline.

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

1. Recreate person/face workers with bounded json-file rotation and prove a
   deleted person-observation group self-recovers from retained stream rows
   without an exception loop; confirm trajectory persistence and log bounds.
2. Repeat the unchanged Candidate B 600s/120s-drain load with 300s retention
   and I/O admission still at two. This is the controlled baseline because the
   first completed media diagnostic lacked the person-consumer load and full
   annotation gate.
3. Add an artifact-audited pressure override for I/O admission and change only
   `2 -> 3`. Treat slot wait as part of ready-to-remux service time; admission
   is not a pass if oldest-ready or wait continues to accumulate.
4. If and only if the three-slot r300 gate passes, repeat with 3,840s retention.
5. Only after both short gates pass, run two comparable one-hour acceptances
   with the fixed fixture/hash and the full evidence/8090 validation set.
