# Media-worker rolling segment index capacity closure (2026-07-21)

## Status

Ongoing. This document is a resumable measurement and change ledger, not a
completion claim. The Round 21 finalizer fix remains dynamically effective,
but the strict capacity gate still fails. Round 22 tied the remaining
visibility tail to synchronized rolling-sink durability stalls. Round 23 then
split the broad remux-admission stage and dynamically proved how the same host
storage stall propagates into PostgreSQL state work: an 8.331-second runner
poll spent 5.195 seconds preparing/claiming rows and 3.120 seconds persisting
completed handoffs while paired sink publications spent 4.060/4.478 seconds
almost entirely in required fsync boundaries. Candidate query and finalizer
admission remained milliseconds. The 360-second diagnostic passed harness
correctness, input and retained-evidence gates, but strict metadata visibility
p95 was 7.610 seconds and media queue p95 was 19.910 seconds. It is not the
exact 600-second r300 acceptance. Round 24 implements the selected fixed,
single-worker, 128-outstanding FIFO publication dispatcher and passes its
real-container durability/order/backpressure/error/shutdown smoke without
changing an fsync, rename, capacity, retention or deadline. The required short
60-route causal diagnostic is still pending. The exact r300 gate remains
pending; r3840 and both one-hour acceptance runs are still prohibited.

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
| `63e4288` | observation-group recovery and bounded worker logs | Recreates deleted event/face consumer groups from retained stream rows and applies `50m x 3` Docker log rotation to person/face workers |
| `db87559` | recovery/log-rotation tests | Covers group recreation and the effective Compose logging contract |
| `cf64609` | pressure override tests | Specifies default, validation, profile override, pressure environment, and run-config audit behavior for index I/O admission |
| `3f3abd9` | artifact-audited index I/O pressure override | Exposes the isolated discovery-through-pin admission width without changing remux, WIP, or finalizer capacity |
| `2a55ce1` | bounded-pin correctness tests | Proves the real worker/index/materializer preserves dual source/mux clocks, invalid/unmatched windows fall back conservatively, and selected leaves remain retention-fenced |
| `b7068b0` | source-window-bounded read-pin publication | Pins only source-window overlap plus one guard leaf on each side for complete modern manifests; mixed/legacy catalogs retain full-catalog pins |
| `6eb297b` | bounded-pin runtime proof documentation | Records the real container identity/retention/legacy smoke without claiming capacity |
| `ab5b7ad`, `0bc6c82` | finalizer count-metric preservation | Keeps segment-index count fields, including pinned-segment cardinality, when remux diagnostics are flattened into finalizer metrics |
| `db19d1f`, `1ec97fc` | changed-parent immutable membership reuse | A changed source parent probes only new/pending manifest leaves instead of resolving and probing every already cataloged immutable leaf |
| `25d5fec` | same-catalog duplicate-refresh red test | Reproduces two concurrent callers parsing the same new manifest while retaining a direct stale-version fence test |
| `b69575b` | per-catalog refresh/pin singleflight | Coalesces same-source refresh work before global I/O admission and publishes refresh/reconcile watermarks at completion |
| `43ca946` | retained/cumulative-counter documentation correction | Rejects the invalid 300s-retained-segment versus 600s-cumulative-operation ratio |
| `c1c3a20`, `1eeb1a2` | publication-journal red/recovery tests | Require atomic incremental publication, bounded rotation, corruption fallback, crash-window recovery, metrics and retention safety |
| `0f7d775` | bounded crash-recoverable publication journal | Sink appends checksummed immutable identities after atomic segment rename; index consumes only new records and keeps filesystem reconciliation authoritative |
| `c06cf28` | journal-first periodic-reconcile red test | Proves a scheduled audit must not reparse a journal-covered leaf while it still recovers an unjournaled crash-window leaf and prunes a deleted leaf |
| `307a9c4` | journal-first periodic reconciliation | Consumes and validates the journal tail before membership enumeration without weakening corruption, rotation, deletion, pin or identity recovery |
| `7e238a8` | remux metadata-gap red tests | Requires metadata publish time/bytes, immediate reload, handoff build, residual, finalizer log and pressure-artifact aggregation |
| `70d4e75` | instrumentation-only remux attribution | Carries the new metrics through materialization, durable handoff/recovery, finalization logs and retained pressure summaries without changing scheduling or serialization |
| `e88a4ed` | selected-identity complexity red test | Requires six selected leaves to cause six direct lookups and zero retained-catalog value iteration |
| `72413a2` | direct selected pin identity lookup | Resolves only the already-selected manifest keys under the short catalog lock while preserving missing-entry retry and exact identity fences |
| `d73759d` | normal-path metadata reload red test | Forbids parsing the freshly published metadata file while retaining unchanged durable bytes and recovery authority |
| `fcbe2bd` | in-memory metadata handoff | Carries the exact published metadata payload into the normal handoff and reports zero reload time without changing serialization or recovery |
| `7736ec5` | fallback-overlay propagation red test | Requires the DB person-context fallback to return the exact annotation rows it writes so expanded-row indexing cannot receive an empty overlay list |
| `7e01432` | fallback-overlay propagation fix | Retains recovered person-context rows in memory through finalizer DB indexing while preserving the diagnostic JSONL contract |
| `faee207` | compact metadata serialization red test | Requires compact durable JSON with exact payload/recovery equivalence, zero normal reload and a material byte reduction |
| `f11f561` | compact rolling metadata publication | Publishes compact JSON without changing payload semantics, handoff/recovery authority, capacity, retention, deadlines or index fences |
| `f65120c` | finalizer publish/lane attribution red tests | Requires heartbeat, prepare, rename, rebase, terminal, projection, DB-index, cleanup, post-terminal and complete lane-service timings |
| `478bff5` | finalizer publish/lane attribution | Carries the phase split through per-event logs and the `phase6-capacity-v4` pressure report without changing behavior |
| `a88472c` | DB-backed bulk-row rebase bypass | Preserves timeline/overlay row-list identity during publish while retaining canonical paths in top-level and path-bearing metadata/summary fields |
| `7da69fe` | rolling visibility attribution tests | Requires per-source/bucket/threshold/slowest-segment visibility evidence and the corrected lifecycle-claim timing source |
| `f904569` | scheduler/sink stall-retention tests | Requires non-overlapping scheduler body stages and durable rolling-sink publication phase aggregation in pressure artifacts |
| `6ce26c2` | visibility, scheduler-cycle and sink-publication attribution | Adds behavior-neutral phase timing from each durable sink boundary through retained pressure summaries |
| `a358b43` | retention-discovery race test | Reproduces a segment directory disappearing while post-run visibility discovery traverses it |
| `6d3753a` | retention-safe visibility discovery | Uses error-tolerant directory walking and counts vanished metadata while retention continues |
| `21e2438` | remux-admission attribution tests | Requires non-overlapping runner subphases and retained pressure-summary fields before implementation |
| `c128073` | remux-admission attribution | Splits completion/handoff, candidate, prepare/claim, reservation and finalizer-transfer work without changing admission behavior |
| `b92876c` | bounded publication dispatcher correctness tests | Requires off-callback execution, exact FIFO order, hard backpressure, error continuation and shutdown drain |
| `104472d` | dispatcher pressure-artifact red contract | Requires final queue/outstanding/failure/timeout and peak state to survive retained log aggregation |
| `c86430e` | bounded asynchronous segment publication | Moves the unchanged durable publish sequence to one process-lifetime FIFO worker per sink with a 128-outstanding bound and source error propagation |
| `647dd2f` | dispatcher artifact retention | Emits one structured shutdown snapshot and retains every dispatcher bound/backpressure/drain metric in pressure summaries |

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
- Recovery proof: `63e4288`/`db87559` recreated the person and face workers
  with `50m x 3` json-file rotation. A controlled deletion of the retained
  person-observation consumer group caused it to be recreated from stream ID
  `0` without an error/traceback loop. The relevant recovery/logging suite
  passed 103 tests with three expected skips, plus compile, critical Ruff,
  Compose rendering, and diff checks.
- Controlled two-slot repeat:
  `/data/video-analytics/artifacts/pressure60_8p1_ioadm2_b10m_r300c_20260721T1740Z`.
  The fixed fixture hash remained
  `42d477ae2bc4eadf4dcd6192ef9a963926e1d88b1755c59e935270230d047490`;
  the 600s/120s run sustained 60/60 sources at 8.048 FPS with zero
  sampling-window send failure, forwarder queue-full, raw drop, or raw send
  failure. All 966 formal tasks and all 1,006 retained tasks materialized.
  All 1,006 videos passed window, duration, frame-rate, 8090 detail, and
  timeline checks. Person persistence passed with 100,884 rows versus 100,881
  exports across 60 sources and zero measured loss.
- Correctness remained fenced: candidates/immediate-admitted/gap/fenced-retry
  were `1006/1004/2/2`; handoff recovery, retry failure, claim-busy, duplicate,
  finalizer failure, attempt-zero expiry, and all drain residuals were zero.
  Seventeen of 1,006 retained videos lacked annotation/bbox/person-context
  rows. Since person persistence passed, this is consistent with excessive
  evidence delay rather than the prior missing-consumer load, but it still
  fails the complete annotation gate.
- Capacity failed decisively. Ready-to-remux p95 was 105.451s, scheduler
  oldest-ready p95 112.267s, media queue p95 126.817s, and lifecycle p95
  127.207s. Poll-gap p95 was 2.515s. Slot-wait p95 was 4.942s and
  pre-pin-through-handoff p95 was 7.023s, while actual ffmpeg/remux p95 was
  only 0.910s; refresh and pin-publication p95 were 1.981s and 1.443s.
  Live ready work peaked at 167 with oldest-ready around 109s before the drain
  eventually returned all state to zero. Media-worker peak CPU was 266.84%.
- Conclusion: two admission slots preserve bounded behavior but remain below
  the required sustained service rate. The next and only capacity variable is
  segment-index I/O admission `2 -> 3`; Candidate B WIP/remux/max-per-poll and
  finalizer values remain unchanged. The pressure override in
  `cf64609`/`3f3abd9` makes that value explicit in wrapper output, runner
  config, effective Compose environment, and retained artifacts.
- Cleanup restored zero enabled pressure cameras, active tasks, leases,
  finalizer-pending rows, pressure publishers/containers, and the daily Redis,
  PostgreSQL, rolling-materialization-disabled, 300s-retention, two-slot
  configuration. About 255GB remained free on the root filesystem.

### Round 6: three-slot discovery and read-pin admission

- Hypothesis: the two-slot bound removed the 12-way convoy but remained below
  arrival; one additional bounded slot may raise aggregate discovery-through-
  pin service rate without changing Candidate B's WIP, remux, max-per-poll, or
  finalizer dimensions.
- Unique variable: `MEDIA_WORKER_SEGMENT_INDEX_IO_CONCURRENCY=3`, supplied
  through the artifact-audited `cf64609`/`3f3abd9` pressure path. Effective
  configuration remained WIP 20, remux 12, max-per-poll 8, finalizer
  threads/processes/queue `8/4/8`, 600s sample, 120s drain, and 300s retention.
- Artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_ioadm3_b10m_r300_20260721T1809Z`.
  `run_config.json`, the effective Compose override, and resource metrics all
  record admission width three. The fixed fixture SHA256 remained
  `42d477ae2bc4eadf4dcd6192ef9a963926e1d88b1755c59e935270230d047490`.
- Input and correctness passed: 60/60 sources, 8.0527 FPS, zero sampling-window
  Savant send failure, queue-full, raw drop, or raw send failure; 969/969
  formal tasks and all 1,007 retained tasks materialized. All 1,007 videos
  passed window/duration/FPS and 8090 detail/timeline/annotation/bbox/person-
  context checks. Person persistence was exact at 104,679 exported and 104,679
  stored rows over 60 sources, with final Redis lag/pending zero.
- Fences also passed: candidates/immediate-admitted/gap/fenced-retry were
  `1007/1006/1/1`; attempt-zero expiry, handoff recovery, retry failure,
  claim-busy, duplicate, finalizer failure, and active task/lease/WIP/lane/
  finalizer-pending residuals were zero.
- Capacity still failed. Ready-to-remux p95 was 37.205s, scheduler oldest-ready
  p95/max 44.209s/54.167s, media queue p95 57.800s, and lifecycle p95 58.305s.
  The live formal-window queue repeatedly cleared early, then grew to 78
  pending with oldest-ready about 36s before ending at 52 pending/about 20s;
  the bounded drain later returned it to zero. Rolling metadata visibility p95
  was 9.425s, above the 2s closure target.
- The comparison is nevertheless directional: versus the controlled two-slot
  repeat, ready/media/lifecycle p95 improved by about 65%/54%/54%, and
  poll-gap p95 improved from 2.515s to 1.360s. Actual ffmpeg/remux p95 was
  0.708s, DB claim wait p95 0.383s, finalizer process-pool wait p95 0.792s,
  and handoff-to-admission p95 1.528s; none is the primary ready backlog.
- Per-job index cost shows diminishing returns: slot-wait p95 was 5.102s,
  pre-pin-through-handoff p95 7.720s, refresh p95 1.926s, and pin publication
  p95 1.934s. Media-worker peak CPU rose from 266.84% to 344.77%, still about
  76.6% below the original 1,475.75% baseline. Remux depth remained p95 12/12
  while WIP was p95 18/20 and downstream waits passed.
- Conclusion: three slots materially improve aggregate throughput but still do
  not meet the r300 gate, so r3840 is prohibited. Width four is the next and
  final simple bounded-admission candidate, matching the existing per-source
  execution bound. It changes only the same measured axis. If width four does
  not clear the ready/visibility gates, do not continue increasing admission;
  return to the refresh/pin publication structure revealed by these metrics.
- Cleanup again restored zero pressure sources/processes, enabled pressure
  cameras, active tasks, leases, and finalizer-pending rows; Redis/PostgreSQL
  defaults, disabled rolling materialization, 300s retention, and daily
  two-slot configuration were restored. The worktree remained clean and the
  root filesystem retained about 251GB free.

### Round 7: four-slot bounded-admission limit

- Hypothesis: width four, matching the existing per-source execution bound,
  was the final simple admission candidate that might clear the residual
  width-three queue without changing any other capacity dimension.
- Unique variable: `MEDIA_WORKER_SEGMENT_INDEX_IO_CONCURRENCY=4`; WIP 20,
  remux 12, max-per-poll 8, finalizer `8/4/8`, 600s/120s, 300s retention, and
  the fixed input/hash were unchanged and artifact-audited.
- Artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_ioadm4_b10m_r300_20260721T1837Z`.
- Input/correctness again passed: 60/60, 8.0479 FPS, zero send/queue/raw loss;
  969/969 formal tasks and 1,008/1,008 retained tasks materialized. All retained
  videos passed window/duration/FPS plus 8090 detail/timeline/annotation/bbox/
  person-context with zero fallback. Person persistence passed with 100,906
  stored versus 100,904 exported rows over 60 sources and final lag/pending
  zero. Candidates/admitted/gap/retry were `1008/1007/1/1`; expiry, recovery,
  retry failure, claim-busy, duplicate, finalizer failure, and residuals were
  zero.
- Capacity regressed versus width three: ready-to-remux p95 rose from 37.205s
  to 55.772s, media queue from 57.800s to 76.731s, lifecycle from 58.305s to
  77.215s, and oldest-ready from 44.209s to 62.931s. The formal queue reached
  106 pending with oldest-ready about 54s near the end, then converged only in
  drain. Metadata visibility p95 also worsened from 9.425s to 10.459s.
- The attribution is an I/O convoy, not insufficient admission slots. Slot-
  wait p95 fell slightly from 5.102s to 4.830s, but refresh rose from 1.926s to
  2.458s, pin publication from 1.934s to 2.004s, lock hold from 1.606s to
  1.743s, and pre-pin-through-handoff from 7.720s to 8.087s. Actual remux p95
  remained only 0.826s, DB claim 0.193s, finalizer pool wait 0.566s, and
  handoff-to-admission 0.864s. Media-worker peak CPU was 294.76%.
- Conclusion: do not test width five or run r3840. Width three remains the best
  measured comparison point, but it is not accepted. The next loop must reduce
  refresh/pin publication work structurally and then repeat r300 at width
  three before any endurance run.
- Cleanup restored daily width two, disabled rolling materialization, 300s
  retention, Redis/PostgreSQL defaults, zero pressure sources/processes,
  enabled cameras, active tasks, leases, or finalizer-pending rows. About
  248GB remained free and the worktree was clean.

### Round 8: source-window-bounded read-pin publication

- Hypothesis: each 5+5 job was still publishing and identity-statting a read
  pin for the entire source retention catalog even though dual-clock mapping
  and remux read only a small local window. Reducing only that pin identity and
  marker set should shorten publication without changing catalog refresh,
  width three, remux, WIP, finalizer, or deadline policy.
- Unique implementation variable: `b7068b0`. After the existing atomic catalog
  refresh, modern manifests with complete immutable source-clock bounds select
  every source-window overlap plus one guard leaf on each side. Missing/mixed
  bounds, a missing/invalid request, or no overlap retain the full-catalog
  behavior. The filesystem mutation flock, exact metadata/video identity
  snapshot, read-pin marker, and retention fence are unchanged.
- Tests: `2a55ce1` adds a real worker/index/materializer dual-clock integration
  proof. A ten-segment catalog pins five leaves, maps the source-domain 5+5
  request onto the distinct mux domain, and remux-selects three leaves. It also
  covers legacy, invalid and unmatched fallback. The complete index/safety/
  rolling set passed 103 tests; pressure harness/analyzer passed 201; lifecycle,
  scheduler, finalizer and static-index tests passed 125 with one expected
  environment skip. A disposable PostgreSQL initialized through migrations
  001-032 then passed all eight repository/finalizer lease contracts.
- Bind-mounted container proof:
  `/data/video-analytics/artifacts/segment_index_window_pin_smoke_20260721T192612Z`.
  The recreated media-worker loaded the bind-mounted `b7068b0` code. Its modern
  ten-leaf catalog published a five-leaf marker (`0003`-`0007`), parsed only
  the three remux-selected leaves (`0004`-`0006`), and mapped the source event
  to mux PTS `122000000000` with requested mux bounds
  `117000000000..127000000000`.
- The real maintenance implementation ran while that marker was active: it
  deleted five unrelated expired leaves, skipped all five pinned leaves, and
  observed one active pin. Marker release left zero pin files. A legacy
  ten-leaf catalog still pinned 10/10. A same-size, same-mtime inode replacement
  was rejected with `SegmentPinRetryableError`, also leaving zero marker
  residual. The retained metric log contains
  `segment_index_pinned_segments=5` from the production formatter.
- The first recreate/smoke attempt is retained as
  `/data/video-analytics/artifacts/segment_index_window_pin_smoke_20260721T192405Z`.
  It executed no fixture or DB mutation because the recreated container used
  the base `host.docker.internal:5432` default and restarted before Docker
  could execute the smoke. Recreating only media-worker with the explicit
  Compose-network URL `postgres:5432` restored a stable worker with restart
  count zero; the passing smoke then ran. Daily width two, disabled
  materialization, 300s retention and zero active task/lease/finalizer-pending
  state remained intact.
- Pressure result: pending. This smoke establishes correctness and expected pin
  cardinality only; it does not claim the r300 capacity gate.

### Round 9: bounded-pin width-three r300 result

- Artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_pinwin_ioadm3_b10m_r300_20260721T192945Z`.
  The effective configuration retained the fixed Candidate B dimensions: WIP
  20, remux 12, max-per-poll 8, finalizer threads/processes/queue `8/4/8`,
  width-three index I/O admission, 600s sample, 120s drain, and 300s retention.
  The fixture SHA256 remained
  `42d477ae2bc4eadf4dcd6192ef9a963926e1d88b1755c59e935270230d047490`.
- Input and correctness passed: 60/60 sources sustained 8.0469 FPS with zero
  sampling-window Savant send failure, forwarder queue-full, raw drop, or raw
  send failure. All 973 formal tasks and all 1,010 retained tasks materialized.
  All retained videos passed window, duration, frame-rate, 8090 detail,
  timeline, annotation, bbox, and person-context checks. Person persistence
  passed with 100,974 stored rows versus 100,968 exports over 60 sources and
  zero measured loss; final Redis lag/pending was zero.
- Fences remained closed: candidates/immediate-admitted were `1010/1010`;
  attempt-zero expiry, handoff recovery, retry failure, claim-busy, duplicate,
  finalizer failure, and active task/lease/WIP/lane/finalizer-pending residuals
  were zero. The harness's only declared failure was the unchanged
  `adaface_roi_watchlist_events_zero` business gate; that does not waive the
  stricter media-capacity targets below.
- The bounded pin was materially effective. Pin-publication p95 fell from
  1.934s in the earlier width-three comparison to 0.073s. Ready-to-remux p95
  improved from 37.205s to 28.567s, scheduler oldest-ready p95 from 44.209s to
  37.020s, media queue p95 from 57.800s to 48.689s, and lifecycle p95 from
  58.305s to 48.965s. Media-worker peak CPU was 288.21%, about 80.5% below the
  original 1,475.75% baseline.
- Capacity and visibility still failed. The closure targets are 5s ready,
  10s media queue, 30s lifecycle, and 2s metadata visibility; observed metadata
  visibility p95 was 10.266s. Slot wait p95 remained 4.454s and pre-pin-through-
  handoff p95 was 6.990s while actual ffmpeg/remux p95 was only 0.894s.
  Refresh became the dominant measured index subphase at 3.009s p95; manifest
  parse, stat, and selected full-row parse p95 were only 0.120s, 0.098s, and
  0.046s respectively. Therefore r3840 remains prohibited.
- The artifact could not aggregate `segment_index_pinned_segments`: finalizer
  metric flattening retained `*_ms` fields but dropped segment-index count
  fields. `ab5b7ad` first captured the regression and `0bc6c82` fixes it. This
  was an observability defect, not evidence that the bounded pin was absent;
  the earlier real-container smoke and production log formatter already
  proved the five-leaf marker.
- Cleanup restored zero enabled pressure cameras, active tasks, leases,
  finalizer-pending rows, source/ffmpeg/MediaMTX processes, and the daily Redis,
  PostgreSQL, disabled-materialization, 300s-retention, width-two state. The
  media-worker remained stable with restart count zero and the root filesystem
  retained about 244GB free.

### Round 10: changed-parent immutable-leaf probe bound

- Hypothesis from Round 9: each atomic new segment changes the source segments
  parent. Refresh already knows immutable membership, but still resolved every
  retained child path and called `is_file()` on every known manifest before it
  could discover the one new leaf. That retention-width filesystem work is a
  plausible cause of the remaining 3.009s refresh p95.
- Unique implementation variable: `1ec97fc`. A resolved, non-symlink-following
  `scandir` parent now reuses catalog membership for known non-pending manifest
  leaves and probes only new or pending leaves. The existing COW version check,
  deletion membership audit, pending retry, exact identity check at read-pin,
  mutation flock, and retention fence remain unchanged.
- Red-to-green proof: `db19d1f` builds 32 known leaves, changes the parent by
  adding leaf 33, and asserts that refresh probes only the new manifest while
  returning all 33 catalog entries.
- Bind-mounted container proof:
  `/data/video-analytics/artifacts/segment_index_refresh_pin_smoke_20260721T200925Z`.
  The recreated media-worker used the explicit Compose-network PostgreSQL URL
  and loaded `1ec97fc` from the bind-mounted worktree. A 32-leaf catalog plus
  one atomic addition returned all 33 leaves while probing exactly the new
  manifest, issuing five `Path.resolve()` calls, and parsing zero full native
  rows. The modern 10-leaf fixture still pinned five and selected three;
  production retention deleted five unrelated leaves, skipped five pinned
  leaves, and observed one active marker. Legacy fallback pinned 10/10, the
  same-size/same-mtime inode replacement was fenced, and every path ended with
  zero marker residual.
- The same smoke dynamically closes the `0bc6c82` observability check: the
  finalizer-flattened value and production metric log both contain
  `segment_index_pinned_segments=5`, never `None`. Media-worker restart count
  remained zero and the database retained zero enabled cameras, active tasks,
  leases, or finalizer-pending rows.
- A preceding artifact
  `/data/video-analytics/artifacts/segment_index_refresh_pin_smoke_20260721T200639Z`
  is retained as a harness-only failure. Its dynamic maintenance module was not
  registered in `sys.modules` before Python 3.12 dataclass execution; it exited
  before creating any segment fixture or database mutation. The corrected
  smoke registered the copied production module before execution.
- Runtime correctness is now proved; the unchanged width-three r300 capacity
  comparison remains pending.

### Round 11: immutable-membership width-three r300 result

- Artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_leafreuse_ioadm3_b10m_r300_20260721T2011Z`.
  The only implementation change from Round 9 was the committed immutable-leaf
  membership reuse/count-metric path. WIP/remux/max-per-poll remained
  `20/12/8`, finalizer threads/processes/queue remained `8/4/8`, index I/O
  admission remained three, and the run retained the same 600s/120s,
  300s-retention fixture with SHA256
  `42d477ae2bc4eadf4dcd6192ef9a963926e1d88b1755c59e935270230d047490`.
- Input and correctness passed: 60/60 sources sustained 8.0499 FPS with zero
  sampling-window send failure, queue-full, raw drop, or raw send failure. All
  972 formal tasks and all 1,011 retained tasks eventually materialized. Every
  retained video passed duration/FPS plus 8090 detail/timeline/annotation/bbox/
  person-context. Person persistence passed at 103,206 stored versus 103,201
  exports over all 60 sources with zero measured loss and final Redis
  lag/pending zero.
- Exact fences also passed: candidates/immediate-admitted/gap/fenced-retry were
  `1011/1006/5/5`; handoff recovery, retry failure, claim-busy, duplicate,
  finalizer failure, attempt-zero expiry, and final active task/lease/WIP/lane/
  finalizer-pending residuals were zero. Pinned-segment aggregation is now real:
  count/min/p50/p95/max was `1011/4/6/6/6`.
- The strict r300 capacity gate failed and regressed against Round 9.
  Ready-to-remux p95 was 36.599s, scheduler oldest-ready p95 was 40.684s, media
  queue p95 was 57.029s, lifecycle p95 was 57.528s, and metadata visibility p95
  was 7.501s. The last formal sample held 972 tasks, 893 bundles, 79 active and
  39 ready; only the later drain returned all state to zero. Poll-gap p95 was
  2.043s, narrowly above two one-second intervals.
- Downstream remained secondary: actual ffmpeg/remux, DB claim, finalizer pool
  wait, and finalization p95 were 1.024s, 0.393s, 3.750s, and 4.789s. In
  contrast, pre-pin-through-handoff p95 was 7.231s, slot wait 4.103s, refresh
  2.879s, and short catalog lock hold 3.028s. Peak media-worker CPU was 361.54%,
  still about 75.5% below the 1,475.75% baseline but higher than Round 9's
  288.21%.
- `1ec97fc` had the intended narrow effect but did not remove the measured
  refresh cost.
  Cumulative `scanned_known` fell only from 94,400 to 89,220 and refresh p95
  only from 3.009s to 2.879s. The artifact's 5,847 segment count is a retained
  filesystem snapshot after a 600-second run with 300-second retention, while
  12,122 `new_or_changed` is a cumulative process counter. They cover different
  time domains and cannot be divided into an amplification ratio or used to
  attribute duplicate refresh work. The duplicate same-catalog refresh race is
  instead established independently by the deterministic concurrent red test
  in `25d5fec`.
- The harness's only declared failure remained
  `adaface_roi_watchlist_events_zero`; the independent strict capacity and
  visibility failures prohibit r3840 regardless. Cleanup restored disabled
  pressure cameras, zero active lifecycle state and pressure processes, daily
  width two, disabled materialization, 300s retention, Redis/PostgreSQL
  defaults, and a restart-zero media-worker. About 241GB root space remained.

### Round 12 hypothesis: per-catalog refresh singleflight

- High-confidence hypothesis: same-source jobs currently acquire separate
  global I/O admission slots, copy the same catalog version, and repeat changed
  parent enumeration/manifest parsing concurrently. The version fence protects
  correctness but only discards the duplicate work after it has consumed the
  filesystem/GIL budget. In addition, `last_refresh_at` records refresh start;
  a refresh longer than the 0.5s interval is already stale when it publishes,
  so a waiting job can immediately repeat it.
- Next unique structural variable: add one per `(source_id, runtime_epoch_id)`
  refresh/pin singleflight gate acquired before the global width-three slot,
  and publish the refresh watermark at completion. Other sources remain
  independent, while same-source waiters reuse the first published COW
  snapshot instead of occupying global slots and repeating work. The COW
  version check remains as a safety fence.
- Required red proof: two concurrent callers observing one new manifest must
  perform one refresh/manifest parse, publish the complete catalog to both, and
  retain the existing cross-source/snapshot non-blocking behavior. No WIP,
  remux, finalizer, retention, deadline, or admission-width value changes with
  this fix.
- Red-to-green implementation: `25d5fec` first reproduced the duplicate parse;
  `b69575b` adds a reentrant per-catalog I/O gate. Production pin callers wait
  on that source/epoch gate before consuming a global I/O slot. The gate spans
  refresh, identity snapshot, and marker publication, then releases before
  ffmpeg/remux, so it neither serializes other sources nor the actual remux
  lane. Direct lookup and forced reconciliation use the same gate; the COW
  version check remains as a stale-publication fence. Refresh/reconcile
  watermarks now use completion time, preventing a long refresh from being
  stale at publication.
- Verification: index/perf/rolling passed 106 tests; pressure harness/analyzer
  passed 201; lifecycle/scheduler/finalizer/static suites passed 118 with one
  expected environment skip. A disposable PostgreSQL initialized through
  migrations 001-032 passed all eight materialization/finalizer contracts.
  `py_compile`, critical Ruff, Compose rendering, and diff checks passed.
- Bind-mounted container proof:
  `/data/video-analytics/artifacts/segment_index_singleflight_smoke_20260721T204645Z`.
  Eight dynamic cases passed: same-catalog callers performed one refresh and
  one new-manifest parse with zero publish conflict; another source and metrics
  snapshot remained non-blocking; bounded and legacy pins, active-marker
  retention, refresh-to-pin atomicity, same-size/same-mtime inode fencing, and
  finalizer count flatten/logging all remained intact. Marker residual and
  database active task/lease/finalizer-pending counts were zero; media-worker
  restart count remained zero.
- A preceding harness artifact
  `/data/video-analytics/artifacts/segment_index_singleflight_smoke_20260721T204548Z`
  is retained. Its copied test module lacked the expected `harness/tests`
  parent depth and exited during module import before fixture or DB mutation.
  The corrected smoke restored that path layout.
- The structural/runtime correctness proof is complete. The unchanged
  width-three r300 repeat is recorded in Round 13 and did not pass, so capacity
  remains unclaimed.

### Round 13: singleflight width-three r300 result

- Artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_singleflight_ioadm3_b10m_r300_20260721T2049Z`.
  The only structural change from Round 11 was per-catalog singleflight and the
  completion-time watermark. WIP/remux/max-per-poll remained `20/12/8`,
  finalizer threads/processes/queue remained `8/4/8`, index I/O admission
  remained three, and the run retained the same 600s/120s, 300s-retention
  fixture and fixed SHA256.
- Input and correctness passed: 60/60 sources sustained 8.038 FPS with zero
  sampling-window send failure, forwarder queue-full, raw drop, or raw send
  failure. All 971 formal tasks and all 1,009 retained tasks materialized. All
  retained videos passed duration/FPS, 8090 detail/timeline, annotation, bbox,
  and person-context checks. Person persistence passed with 102,091 stored
  rows versus 102,088 exports across all 60 sources; the sampling report ended
  with person-consumer lag/pending `1/2` while the post-run observability
  snapshot had converged to zero.
- Exact fences passed: candidates/immediate-admitted/gap/fenced-retry were
  `1009/996/13/13`; attempt-zero expiry, handoff recovery, retry failure,
  claim-busy, duplicate, finalizer failure, and final active task/lease/WIP/
  lane/finalizer-pending residuals were zero. Pinned-segment
  min/p50/p95/max remained `4/6/6/6`.
- The strict r300 capacity gate failed. Ready-to-remux p95 was 53.417s,
  scheduler oldest-ready p95 58.929s, media queue p95 73.826s, lifecycle p95
  74.369s, and metadata visibility p95 15.235s. Poll-gap p95 was 2.156s,
  also above two one-second intervals. The final formal sample held 971 tasks,
  869 bundles, 101 active tasks, and 65 ready tasks; only the later drain
  returned all state to zero. Therefore r3840 remains prohibited.
- Singleflight did not remove the measured pressure bottleneck. Per-catalog
  lock wait p95 was 0.045ms and cumulative wait only about 21ms. In contrast,
  global I/O-slot wait p95 was 4.455s and refresh p95 2.575s; pre-pin through
  handoff p95 was 7.164s while actual ffmpeg/remux was 0.960s. Pin publication,
  DB claim, finalizer pool wait, and finalization p95 were respectively
  0.096s, 0.284s, 2.758s, and 4.823s. Media-worker peak CPU was 408.25%, about
  72.3% below the original 1,475.75% baseline, but the backlog was still moved
  into the drain window.
- The latest artifact again exposes two non-comparable counters: 5,645
  retained segments after 300-second retention and 12,273 cumulative
  `new_or_changed` operations. No amplification ratio or causal conclusion is
  drawn from them. Together with the negligible real singleflight wait, the
  next structural target is the repeated retention-directory discovery and
  manifest/stat work itself: inspect sink publication, retention generation,
  and index discovery as one contract, then require a red test for a crash-safe
  incremental publication feed (or equivalent) with periodic reconciliation
  and deletion safety before changing production code.
- Cleanup restored disabled pressure cameras, zero pressure publishers,
  ffmpeg/MediaMTX processes and lifecycle residuals, daily disabled
  materialization, 300s retention, width two, and Redis/PostgreSQL defaults.
  Media-worker restart count remained zero and about 237GB root space remained.

### Round 14: crash-recoverable incremental publication journal

- The retained 5,847 segments in Round 11 were a post-run filesystem snapshot
  after 300-second retention, while 12,122 `new_or_changed` operations were a
  process-lifetime cumulative counter over the 600-second run. Commit
  `43ca946` records that these time domains cannot form an amplification ratio;
  the independently reproduced duplicate-refresh race in `25d5fec` remains
  valid without that ratio.
- Red tests `c1c3a20`/`1eeb1a2` define the next structural contract. The sink
  first publishes the complete segment directory through the existing atomic
  rename, then best-effort appends a checksummed compact manifest plus immutable
  manifest/metadata/video identities. Append failure never invalidates a
  committed segment. The journal is bounded at 16 MiB and atomically rotates.
  Corruption, truncation, replacement or rotation force filesystem membership
  reconciliation.
- Implementation `0f7d775` makes ordinary index refresh consume only new
  records. Initial and periodic filesystem membership audits remain the
  recovery authority for a sink crash between rename and append and for
  retention deletion. The mutation flock, read-pin marker, exact identity
  fence, atomic segment rename and fenced finalizer handoff are unchanged.
- Focused index/sink/rolling tests passed 137; pressure harness/analyzer passed
  201; broader lifecycle/scheduler/finalizer/deployment/static tests passed;
  a fresh PostgreSQL database migrated through 001-032 passed all eight real
  materialization/finalizer contracts. Compile, Ruff, Compose rendering and
  diff checks passed.
- Bind-mounted smoke:
  `/data/video-analytics/artifacts/segment_index_publication_journal_smoke_20260721T213820Z`.
  It proved 65 atomic records, one new journal-only discovery with zero retained
  directory scan or extra manifest parse, zero journal error, a two-segment
  read pin, retention skipping both pins then deleting them after release, and
  zero marker/database residual.
- The combined launch artifact
  `/data/video-analytics/artifacts/pressure60_8p1_pubjournal_ioadm3_b10m_r300_20260721T2139Z`
  is deliberately retained but is not a capacity result. It contains one
  pre-runtime host DB-port error and one intentionally interrupted pre-sampling
  attempt caused by a minute-level run-id collision. Later commands must set
  both host `DATABASE_URL=...127.0.0.1:5439...` and container
  `VIDEO_ANALYTICS_DATABASE_URL=...postgres:5432...`.
- The clean unchanged r300 is
  `/data/video-analytics/artifacts/pressure60_8p1_pubjournal_ioadm3_b10m_r300_20260721T214242Z_r2`.
  It passed 60/60 input at 8.0504 FPS, materialized 969/969 formal and 1,005/
  1,005 retained tasks, passed every video/8090/timeline/annotation/bbox/person-
  context check, and retained zero send/queue/raw loss, expiry, recovery,
  duplicate, finalizer failure or residual. Exact admission counts were
  `1005/1001/4/4` candidates/admitted/gap/fenced retry.
- Strict capacity failed: ready-to-remux/oldest-ready/media queue/lifecycle/DB
  lifecycle p95 were `35.237/44.724/56.594/56.886/59.007s`; the final formal
  sample still had 88 active and 45 ready. Journal read p95 was only 77.8ms,
  with 10/17 records per job and zero journal error/reconcile fallback.
  Manifest parse time fell from 43.676s in the earlier comparison to 28.059s,
  cumulative refresh to 295.897s and scanned-known to 45,236, but the backlog
  still moved into drain. r3840 remained prohibited.

### Round 15: journal-first periodic membership ordering

- Round 14 still let a scheduled periodic audit enumerate membership before
  consuming its journal tail. It therefore parsed newly published leaves from
  the directory and only afterward consumed duplicate publication records.
  Red test `c06cf28` combines one journal-covered leaf, one unjournaled
  rename-before-append crash leaf, and one retention-deleted catalog leaf. Old
  code parsed both new manifests; the contract requires exactly one parse.
- `307a9c4` changes only the order: consume/validate the journal first, then
  enumerate membership to recover missed publications and deletions, and
  consume again after the audit to close the concurrent-append window. The
  journaled leaf is already immutable catalog membership and is not reparsed.
  Corruption and rotation still force reconciliation.
- Focused index/sink/rolling tests passed 137, pressure harness/analyzer 201,
  broader lifecycle/scheduler/finalizer suites 154 with one environment skip,
  and a fresh 001-032 PostgreSQL database passed all eight real contracts.
- A first isolated smoke is preserved at
  `/data/video-analytics/artifacts/segment_index_journal_first_reconcile_smoke_20260721T221307Z`.
  It is a harness-only failure: the fixture advanced maintenance wall time by
  two hours while using a 60-second pin TTL, so production maintenance correctly
  expired the marker. It touched no daily rolling root or database row.
- The corrected bind-mounted smoke
  `/data/video-analytics/artifacts/segment_index_journal_first_reconcile_smoke_20260721T221437Z`
  passed at `307a9c4`: zero manifest parse for the journal-covered leaf, one for
  the crash-window leaf, both leaves returned, the deleted catalog leaf pruned,
  zero full-row parse/journal error, two active pins skipped, both leaves
  deleted after release, and zero marker residual.
- Exact unchanged width-three r300 artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_journalfirst_ioadm3_b10m_r300_20260721T221602Z`.
  Fixed dimensions remained WIP/remux/max-per-poll `20/12/8`, finalizer
  threads/processes/queue `8/4/8`, 600s/120s, 300s retention and the same
  fixture SHA256. Input passed at 60/60 and 8.058 FPS with zero sampling-window
  send failure, queue-full or raw loss. All 973 formal and 1,011 retained tasks
  materialized; 1,011/1,011 passed duration/FPS and every 8090/timeline/
  annotation/bbox/person-context check. Person rows were exactly 102,490/
  102,490 with final lag/pending 0/0. Candidates/admitted/gap/retry were
  `1011/1002/9/9`, with zero expiry, recovery, retry failure, claim-busy,
  duplicate, finalizer failure or residual.
- The ordering optimization is dynamically effective but capacity still fails.
  Cumulative manifest parse time fell from 28.059s to 1.756s and refresh time
  from 295.897s to 241.788s with zero publication error/reconcile fallback.
  Nevertheless ready-to-remux/oldest-ready/media queue/lifecycle/DB lifecycle
  p95 worsened to `51.355/58.436/70.519/70.875/72.892s`; poll-gap p95 was
  2.265s. The formal tail held 101 active/64 ready and relied on drain.
  WIP/remux/finalizer-depth p95 reached `20/12/11`, while actual remux,
  finalizer pool wait and finalization p95 were `1.241/3.331/4.750s`.
- Existing phase fields now expose a new blind spot rather than justify blind
  capacity growth. `remux_total` was 3.475s p50/7.438s p95, but measured index
  admission/refresh/pin plus actual remux and pin release leave an unexplained
  residual of about 1.146s p50/4.771s p95. Static alignment shows
  `materialization_ms` stops before the selected frame metadata is pretty-
  serialized and atomically written, and `_materialize_rolling_cache_job()`
  immediately parses that same file back before durable handoff. The next
  variable is instrumentation-only: time metadata publish/bytes, immediate
  reload and handoff build, close the remux accounting gap with a red test,
  then optimize only the measured dominating subphase. Do not widen WIP,
  remux and finalizer together, and do not run r3840.

### Round 16: production remux metadata-gap attribution

- Tests-only commit `7e238a8` first required five new fields at the materializer
  result, durable handoff/phase, recovery/finalizer log and pressure-summary
  layers: `remux_metadata_publish_ms`, `remux_metadata_bytes`,
  `remux_metadata_reload_ms`, `remux_handoff_build_ms` and
  `remux_unattributed_ms`. All four focused contracts failed on the old code.
- Instrumentation-only `70d4e75` times the pretty JSON construction/write,
  immediate parse-back and immutable handoff construction, then carries those
  values through restart recovery and artifact aggregation. It changes no
  queue, lock, serialization format, capacity, deadline or state transition.
  Focused index/sink/rolling passed 137, pressure harness/analyzer 201, broader
  lifecycle/scheduler/finalizer 154 with one environment skip, and a fresh
  PostgreSQL 001-032 database passed all eight real contracts. Critical Ruff,
  compile, Compose rendering and diff checks passed.
- Exact diagnostic artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_metapubmetrics_ioadm3_b10m_r300_20260721T225212Z`.
  It kept the fixed input/hash and every Candidate B/width-three dimension.
  Input passed at 60/60 and 8.0518 FPS with zero send/queue/raw loss. All 970
  formal and 1,009 retained tasks materialized; all retained videos and 8090/
  timeline/annotation/bbox/person-context checks passed. Person persistence was
  100,968 stored versus 100,966 exported with zero measured loss and final
  lag/pending 0/0. Expiry, handoff recovery, retry failure, claim-busy,
  duplicate, finalizer failure and all residuals were zero.
- Strict capacity still failed: ready/media/lifecycle/DB lifecycle p95 were
  `28.290/48.466/48.663/51.081s`, oldest-ready p95 36.334s and poll-gap p95
  2.033s. The formal tail had 82 active/44 ready and relied on drain. Actual
  remux/finalizer-pool-wait/finalization p95 were `1.241/1.514/4.220s`; WIP,
  remux and finalizer depth p95 were `19/12/10`.
- The new fields prove that the stable approximately 239KB metadata document is
  not free under synchronized concurrency: pretty-JSON publish was
  262ms p50/1.101s p95 and immediate reload 158ms/0.885s. Handoff build was
  only 4ms/46ms. This round-trip is a valid later optimization candidate, but
  it is not the largest remaining phase.
- The computed residual was 80ms p50/3.334s p95. Per-job log correlation shows
  Pearson 0.972 between that residual and aggregate
  `segment_index_lock_hold_ms`, whose p95 was 3.067s; subtracting lock hold
  leaves only 26.5ms p50/598ms p95. Static alignment identifies a retained-
  width operation: `_catalog_segment_identities()` holds `catalog.lock`, scans
  every catalog entry and repeatedly resolves every directory merely to return
  identities for the already-selected 5-6 pin leaves. This defeats the bounded-
  pin intent without adding safety.
- The next single behavior variable is therefore direct identity lookup for
  only selected leaves under the short catalog lock. A deterministic red test
  must bound resolve/entry visits independently of retained catalog width and
  preserve missing-entry retry, exact metadata/video identity fencing and pin
  publication. Metadata serialization, WIP/remux/finalizer sizes, width three,
  retention and deadlines remain unchanged for its r300 comparison.

### Round 17: direct selected-identity lookup

- Tests-only `e88a4ed` first exposed the retained-width defect: asking for six
  selected identities visited all 128 catalog values. `72413a2` replaces that
  scan with direct manifest-key lookups while preserving the retry-on-missing
  contract and the metadata/video inode, size and mtime identity fence.
- Focused index/sink/rolling tests passed 138, pressure harness/analyzer 201,
  and lifecycle/scheduler/finalizer tests passed 154 with one environment
  skip. Fresh PostgreSQL migrations 001-032 passed all eight real contracts;
  Ruff, compile, Compose rendering and diff checks also passed.
- Bind-mounted smoke:
  `/data/video-analytics/artifacts/segment_index_selected_identity_smoke_20260721T232129Z`.
  A 128-leaf catalog returned six selected identities with zero catalog-wide
  value visits and exactly six directory resolves. It pinned IDs 0061-0065,
  deleted 123 unpinned leaves while skipping five pins, deleted those five
  after release, and left zero marker or full-row-parse residual.
- Exact unchanged r300 artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_selidentity_ioadm3_b10m_r300_20260721T232319Z`.
  It retained Candidate B WIP/remux/max-per-poll `20/12/8`, finalizer
  threads/processes/queue `8/4/8`, width three, 600s/120s, disk-backed cache,
  300s retention and the fixed fixture SHA256. Input passed at 60/60 and
  8.0482 FPS with zero send/queue/raw loss. All 971 formal and 1,011 retained
  tasks materialized; 1,011/1,011 retained bundles passed duration/FPS and
  every 8090/timeline/annotation/bbox/person-context check. Person persistence
  passed at 102,431 stored versus 102,430 exported with measured loss zero.
  Expiry, recovery, retry failure, claim-busy, duplicate, finalizer failure and
  final task/lease/lane/finalizer-pending residuals were zero. The harness's
  declared `adaface_roi_watchlist_events_zero` failure does not replace the
  independent media-capacity verdict.
- The behavior fix is dynamically proved: segment-index lock-hold p95 fell
  from 3.067s to 63ms, and remux unattributed p95 fell from 3.334s to 1.926s.
  It was nevertheless insufficient and this run varied worse at the queue
  level: ready-to-remux/media queue/lifecycle/DB lifecycle p95 were
  `54.823/74.657/74.899/77.233s`, oldest-ready p95 was 62.774s and metadata
  visibility p95 was 13.831s. The formal tail retained 97 active/60 ready and
  relied on drain. WIP/remux/finalizer depth p95 was `20/12/11`.
- The remaining measured synchronous round trip is now the narrowest safe
  variable. The approximately 238KB pretty metadata publish/reload costs
  `1.201/0.988s` p95; handoff build is only 58ms p95. The next red contract
  must prove the normal success path builds its immutable handoff from the
  exact selected metadata already held in memory, without immediately parsing
  the file it just atomically wrote. Crash recovery must continue reading the
  durable file, and the on-disk pretty JSON format, metadata contents,
  capacities, retention and deadlines remain unchanged for attribution.

### Round 18: normal-path metadata reuse

- Red test `d73759d` makes any normal-path
  `_load_scan_metadata_payload()` call fail, requires the returned handoff and
  finalizer phase to report `remux_metadata_reload_ms=0`, and verifies that the
  durable pretty JSON bytes do not change. `fcbe2bd` carries the exact dict
  already serialized by `materialize_window()` into the handoff. Recovery
  continues to load `metadata.json` from the immutable staged identity.
- Affected index/sink/rolling tests passed 141, pressure harness/analyzer 201,
  and broader lifecycle/scheduler/finalizer/deployment tests passed 134 with
  eight environment skips. Critical Ruff, compile and Compose rendering
  passed. A fresh PostgreSQL database migrated through 001-032 and passed all
  eight real materialization/finalizer contracts.
- Bind-mounted smoke:
  `/data/video-analytics/artifacts/rolling_metadata_handoff_smoke_20260721T235829Z`.
  The real index/materializer selected two leaves, performed zero normal
  metadata loader calls, preserved the pretty JSON payload/bytes, reported
  reload 0, then performed exactly one durable-file load during simulated
  recovery. It left zero pin-marker residual.
- Exact unchanged r300 artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_metareuse_ioadm3_b10m_r300_20260721T235925Z`.
  Input passed at 60/60 and 8.0385 FPS with zero send/queue/raw loss. All 969
  formal and 1,008 retained tasks materialized, all videos passed duration/FPS,
  all 1,008 bundle and timeline API checks passed, person persistence passed at
  102,834 stored versus 102,832 exported with zero measured loss, and expiry,
  recovery, retry failure, claim-busy, duplicate, finalizer failure and final
  task/lease/lane/finalizer-pending residuals were zero.
- Reload removal is dynamically effective but insufficient. Reload p95 became
  exactly 0 and remux-total p95 fell from 6.897s to 5.876s. Ready-to-remux,
  media queue, lifecycle and DB lifecycle p95 improved only to
  `50.869/70.168/70.691/72.855s`; oldest-ready and metadata-visibility p95 were
  57.646s and 21.637s. The formal tail still held 82 active/41 ready and relied
  on drain. WIP/remux/finalizer depth p95 was `20/12/12`, finalizer admission
  gap/retry reached 46, and handoff-to-admission p95 rose to 4.910s. The strict
  capacity/visibility gate therefore failed and r3840 remains prohibited.
- This run also exposed a separate correctness bug in the rare DB
  person-context fallback. Eight retained events logged 16-27 recovered
  annotation frames and wrote valid JSONL, but
  `_write_person_bbox_db_sidecar_fallback()` returned no `annotations` list.
  `_db_index_payload_kwargs()` consequently supplied `overlay_rows=[]`; the DB
  index inserted zero overlays and then pruned the valid sidecar. The outcome
  was 1,000/1,008 annotation checks passing, with eight bbox/person-context
  failures. A deterministic red test and narrow propagation fix are required
  before any further capacity candidate.

### Round 19: DB person-context fallback overlay propagation

- Red-test commit `7736ec5` reproduces the exact failure from Round 18: the
  fallback writes valid person-context JSONL rows, but the returned bundle has
  no `annotations`, so expanded DB indexing receives `overlay_rows=[]` and can
  prune the only copy. `7e01432` returns the same normalized rows it writes and
  carries them through the finalizer without changing fallback selection,
  frame matching or database index semantics.
- The disposable-PostgreSQL smoke is retained at
  `/data/video-analytics/artifacts/fallback_overlay_index_smoke_20260722T002952Z`.
  It produced two returned annotations, two JSONL rows, two durable overlay
  rows and two timeline rows; the API repository read both person-context
  records after sidecar pruning.
- Affected index/sink/rolling tests passed as part of the 143-test set described
  below. The wider pressure/lifecycle/finalizer/deployment selection passed 335
  with eight environment skips, and a fresh PostgreSQL database migrated
  001-032 and passed all eight real contracts. This is a correctness fix only;
  it does not claim capacity.

### Round 20: compact rolling metadata publication and exact r300

- Red-test commit `faee207` first required compact durable JSON, exact decoded
  payload equality, zero normal-path reload, recovery from the durable file and
  a material byte reduction. `f11f561` changes only JSON formatting; selected
  rows, handoff data, recovery authority, journal/pin/identity fences, WIP,
  remux/finalizer capacity, retention and deadlines are unchanged.
- Bind-mounted smoke:
  `/data/video-analytics/artifacts/compact_metadata_handoff_smoke_20260722T003409Z`.
  Its 240-frame payload shrank from 122,972 to 70,886 bytes (42.36%) with exact
  decoded payload equality, zero normal reload, one recovery load and zero pin
  residue.
- Exact unchanged r300 artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_metacompact_ioadm3_b10m_r300_20260722T003536Z`.
  It retained WIP/remux/max-per-poll `20/12/8`, finalizer
  threads/processes/queue `8/4/8`, index width three, 600s sample, 120s fixed
  drain, disk-backed 300s retention and fixture SHA256
  `42d477ae2bc4eadf4dcd6192ef9a963926e1d88b1755c59e935270230d047490`.
- Input passed at 60/60 and 8.0525 FPS against the 7.92 minimum. Savant send
  failure delta, queue-full samples, forwarder queue depth and raw-forwarder
  drop/send failures were zero. The formal window created 972 tasks. At the
  formal tail only 899 were playable and 72 remained active, including 31
  ready and 13 finalizer-pending; source quiescence plus postfill/drain later
  converged all 972 formal and all 1,011 retained tasks to materialized with
  zero expiry, active lease or finalizer-pending residue.
- Retained correctness passed. All 1,011 videos met the 5+5 duration and raw-FPS
  gates; 1,011/1,011 8090 details, timelines and annotations passed with zero
  missing bbox or person context. PostgreSQL retained 241,629 timeline rows and
  63,718 overlay rows containing 144,238 bbox objects and 66,403
  person-context objects. Five API annotation counts were one lower than the
  source-row count because DB overlay rows legitimately merge a clip frame
  index; none was a failure. Person persistence passed at 101,918 stored versus
  101,917 exported, with measured loss zero and final consumer lag/pending 0/0.
- Compaction was dynamically effective but insufficient. Median job metadata
  fell from 237,431 to 156,786 bytes (33.97%); publish p50/p95 improved from
  332ms/1.248s to 265ms/0.962s, reload remained exactly zero and remux-total
  p95 fell from 5.876s to 3.996s. Ready-to-remux/media queue/lifecycle/DB
  lifecycle p95 improved from `50.869/70.168/70.691/72.855s` to
  `26.231/48.305/48.606/50.535s`, but remained above the strict targets and the
  non-empty formal tail still required drain. Oldest-ready and poll-gap p95
  were 34.264s and 3.234s.
- The index is no longer the p95 limiter: per-job I/O-slot/refresh/pin-publish/
  catalog-lock-hold p95 was `80/288/106/0.474ms`; selected pin cardinality was
  six, all 1,011 pins were released, and publication errors/reconciles,
  fallback scans and active-pin residue were zero.
- The pressure has moved to finalizer service. WIP/remux/finalizer-depth p95
  was `20/8/12`, with finalizer depth reaching its 16 running-plus-queued
  bound. Finalizer pool wait/finalization/handoff-to-admission p95 was
  `4.658/4.908/6.340s`; 129/1,011 handoffs missed immediate admission and were
  durably retried, with zero retry failure or recovery. Preserved per-event
  logs further show the current blind spot: the fenced canonical-attempt
  publish stage was 1.890s p50/3.789s p95, while bundle construction was only
  351ms/1.409s. Work after the terminal transition occupied another
  230ms/3.092s before the finalizer lane returned, including DB index p95
  1.716s. Increasing process workers is not justified until the publish fence
  and full lane occupancy are split into attributable phases.
- The harness declared `adaface_roi_watchlist_events_zero`. This is a real
  business-gate failure but not an AdaFace transport loss: the ROI worker
  published 34,111 embeddings, PostgreSQL stored 30,696 observations across
  all 60 sources, and pending was zero. Face-worker performed zero gallery
  queries because the database had no active Reese/Finch targets and repeatedly
  logged `watchlist rule has no active targets`. The gallery precondition must
  be restored explicitly before the next exact gate; it does not waive the
  independent capacity failure.
- Validation at `f11f561`: affected index/sink/rolling 143 passed; combined
  pressure/lifecycle/finalizer/deployment 335 passed with eight skips; fresh
  PostgreSQL 001-032 plus real contracts eight passed. Critical Ruff, compile,
  Compose rendering and diff checks passed.

### Round 21: finalizer attribution, canonical rebase bypass, and exact r300

- The first attribution attempt is preserved at
  `/data/video-analytics/artifacts/pressure60_8p1_finalizerattrib_ioadm3_b2m_r300_20260722T012220Z`
  but is invalid: the container DSN override was omitted, so workers attempted
  `host.docker.internal:5432` and the run was interrupted before sampling. It
  is not attribution evidence.
- The corrected 120s behavior-neutral canary is
  `/data/video-analytics/artifacts/pressure60_8p1_finalizerattrib_ioadm3_b2m_r300_20260722T013251Z`.
  It used host PostgreSQL `127.0.0.1:5439` and container PostgreSQL
  `postgres:5432`, while retaining Candidate B `20/12/8`, finalizer `8/4/8`,
  index width three and 300s retention. Across 245 video finalizations,
  publish total p50/p95 was 903/2,253ms and canonical rebase alone was
  852/2,129ms. Heartbeat, prepare and rename p95 were only 100/82/26ms.
  Complete finalizer-lane service was 1,625/3,827ms; post-terminal p95 was
  1,702ms, including DB-index p95 1,116ms. Rebase was therefore the isolated
  next behavior variable.
- Reese and Finch were absent from PostgreSQL, not merely inactive. They were
  registered through the real ONNX operational path as
  `demo:midterm:reese` and `demo:midterm:finch`; both embeddings are active,
  primary, 512-dimensional, unit-normalized and record
  `real_embedding_used=true`. The corrected canary produced 100 formal
  `watchlist_hit` events, proving the hidden gallery precondition was repaired
  without weakening acceptance.
- `f65120c` first made the publish/lane attribution a deterministic contract;
  `478bff5` implemented instrumentation only. The next red test then supplied
  DB-backed timeline and overlay lists that fail on iteration during publish.
  The old recursive rebase failed that test. `a88472c` skips only
  `_db_timeline_rows` and `_db_overlay_rows`; it still recursively rebases the
  small path-bearing metadata/summary objects and all canonical top-level
  artifact paths. The row objects are passed unchanged to expanded DB indexing,
  preserving overlay and timeline semantics.
- The post-fix 120s representative smoke is
  `/data/video-analytics/artifacts/pressure60_8p1_rebasefast_ioadm3_b2m_smoke120_20260722T014950Z`.
  It passed with 303/303 formal and 375/375 retained evidence, including 100
  watchlist images and 246 videos. Rebase p50/p95 fell to 24/40ms, publish
  total to 26/66ms and complete lane service to 333/780ms. All video, 8090,
  timeline, annotation, bbox, person-context and person-persistence checks
  passed.
- Exact unchanged r300 artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_rebasefast_ioadm3_b10m_r300_20260722T020149Z`.
  It retained the fixed fixture/hash, width three, Candidate B `20/12/8`,
  finalizer `8/4/8`, 600s sample, 25s postfill, 120s drain and disk-backed 300s
  retention. The harness status is `passed` with only
  `validate_seq_iq_expected_sampling_gap`; this means its encoded correctness
  gates passed, not that the separate Spec 33 latency release gates passed.
- Input passed at 60/60 and 8.0538 FPS against 7.92. Sampling-window Savant
  send failures, queue-full samples, forwarder queue depth, raw-forwarder drops
  and raw send failures were all zero. The formal window produced 1,500 tasks:
  970 behavior videos and 530 real watchlist images. All 1,500 were materialized;
  the retained set contained 1,552 bundles, including 1,007 videos and 545
  watchlist images, with zero expiry, active lease or finalizer-pending residue.
- Retained correctness passed end to end: 1,552/1,552 8090 details, 1,007/1,007
  video duration/raw-FPS checks, timelines and annotations, with zero missing
  bbox or person context. PostgreSQL holds 240,574 timeline rows, 63,414
  overlay rows, 144,080 bbox objects and 66,306 person-context objects for the
  run. Four one-row annotation-count differences are the accepted DB merge of
  a duplicate clip-frame index. Person persistence passed at 100,893 stored
  versus 100,891 exported, measured loss zero and final lag/pending 0/0.
  Face-worker emitted 692 watchlist hits from 35,033 successful gallery queries
  with zero query or emit failure.
- The scoped finalizer fix was dynamically decisive. Across 1,007 videos,
  rebase p50/p95 is 24/49ms, publish total 27/109ms, lane service 354/1,249ms,
  finalizer pool wait 0/1ms, finalization 286/815ms and handoff-to-admission
  4/60ms. All 1,007 handoffs were admitted immediately with zero rejection,
  retry failure or recovery. Pins were created/released 1,068/1,068; fallback
  scans, publication errors/reconciles and final active pins were zero.
- The strict Spec 33 release comparison is:

  | Metric | Release gate | Observed p95 | Verdict |
  | --- | ---: | ---: | --- |
  | ready-to-claim | <=15s and >=50% better than 33.95s | 12.006s | release pass; misses <=5s closure |
  | finalizer queue/pool wait | <=5s | 0.001s | pass |
  | media queue | <=15s | 25.803s | fail |
  | evidence lifecycle | <=35s | 26.140s | pass |
  | DB claim wait | <=1s | 0.093s | pass |
  | rolling metadata visibility | <=2s | 19.008s | fail |

  DB lifecycle p95 was 24.864s and observed media-worker CPU was 296.42%,
  about 79.9% below the 1,475.75% baseline. At the formal cutoff 25 video
  tasks were still active; all completed within the 25s postfill, the latest
  15.82s after cutoff. This is much smaller than Round 20's 72-task tail and
  needs no 120s drain, but it does not waive the media-queue and visibility
  failures. r3840 remains prohibited.

### Round 22: rolling visibility stall attribution and failed diagnostic

- Tests-only commits `7da69fe` and `f904569` first required per-source and
  30-second-bucket visibility distributions, slowest-segment evidence,
  non-overlapping scheduler body stages, and every durable rolling-sink
  publication phase to survive pressure-log aggregation. Instrumentation-only
  `6ce26c2` added those measurements without changing publication order,
  fsyncs, rename, scheduling, retention, capacity or deadlines. The combined
  sink/scheduler/pressure selection passed 307 tests.
- Diagnostic artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_stallattrib_ioadm3_b6m_r300_20260722T031015Z`.
  It retained the fixed 4,800-second fixture and SHA256
  `42d477ae2bc4eadf4dcd6192ef9a963926e1d88b1755c59e935270230d047490`,
  60 routes, Candidate B WIP/remux/max-per-poll `20/12/8`, finalizer
  threads/processes/queue `8/4/8`, index width three, disk-backed cache,
  300-second retention, 360-second diagnostic sample and configured
  120-second drain.
- Result: failed diagnostic, not an exact r300 or acceptance result. Sampling
  completed, but post-sample `Path.rglob()` visibility discovery traversed a
  segment directory while retention removed it and raised
  `FileNotFoundError`. Red test `a358b43` reproduces that race; `6d3753a`
  replaces the traversal with error-tolerant `os.walk()` handling and counts
  vanished metadata. The pressure-harness suite passed 201 tests after the
  fix. No failed artifact was deleted or relabeled.
- Before the stopped pressure containers could be replaced, complete sink logs
  were copied into `rolling_cache_sink_a_logs_since_start.txt` and
  `rolling_cache_sink_b_logs_since_start.txt`. Across 7,403 published
  segments, publish-total p50/p95/p99/max was
  `5.918/19.991/43.058/6330.332ms`; six publications exceeded one second and
  two exceeded five seconds. Per-source publication-gap p50/p95/p99/max was
  `3.504/3.528/3.671/17.161s`; 59 gaps exceeded ten seconds across 59 of 60
  sources, showing one shared-loop stall per affected sink rather than 59
  independent source failures.
- The decisive Sink A publication at `2026-07-22 03:17:06.379` took
  `6330.332ms`: metadata fsync `1692.064ms`, manifest fsync `1190.978ms`,
  staging-directory fsync `668.315ms`, rename `0.024ms`, and parent-directory
  fsync `2776.694ms`. Sink B simultaneously published one segment in
  `5034.729ms`. Correlation with total publication time was `0.9694` for
  metadata fsync, `0.9344` for manifest fsync, `0.9055` for parent-directory
  fsync and `0.8656` for staging-directory fsync. Every existing durability
  boundary remains required; the evidence supports moving this work off the
  shared GLib callback, not deleting fsyncs.
- A live media-worker log line captured during the same synchronized tail
  reported scheduler cycle `13081ms` and
  `tick_stage_remux_admission_ms=13078.9`; every other scheduler stage was
  negligible. This broad stage still combines completed-remux handoff DB work,
  candidate discovery/claim and finalizer transfer, so it does not yet identify
  which individual main-loop operation inherited the filesystem stall. The
  next instrumentation variable must split `_RollingCacheMaterializationRunner.process()`
  before changing behavior.
- Retention itself was secondary. Across 15 maintenance passes, duration p95
  was `1036.1ms`, discovery p95 `898.9ms`, mutation-lock hold p95 `137.1ms`
  and lock wait remained zero. The largest maintenance pass was `1055ms`, far
  below the paired five-to-six-second publication stalls.
- The corrected PostgreSQL latency query now reads the actual
  `materialization_audit.lifecycle_v2_claim` record. For the retained prior
  exact-r300 rows it measured 1,007 video tasks, including 54
  `coverage_not_complete` retries: fixed policy wait p95 `14.0s`, overall
  ready-to-claim p95 `11.584s`, first-claim p95 `6.722s`, retried p95
  `19.716s`, retry-delay p95 `2.226s`, and claim-to-materialized p95 `5.743s`.
  The previous audit-key query returned no samples and must not be used.
- Failure cleanup and restoration completed. The daily single branch is
  active; pressure cameras, source publishers, ffmpeg and MediaMTX processes
  are zero; active materialization tasks, leases and finalizer-pending rows are
  zero; workers use Compose-network PostgreSQL `postgres:5432`; rolling
  materialization is disabled; index width two, Redis RDB settings,
  PostgreSQL checkpoint settings and 300-second retention are restored. About
  209GB remained free, and the worktree was clean at `6d3753a`.
- Conclusion: the visibility tail is now dynamically tied to rare host-storage
  durability spikes, and synchronous publication on a sink-wide GLib main loop
  amplifies one slow segment into source-wide visibility gaps. The next safe
  behavior candidate is a fixed, bounded publication dispatcher that preserves
  every fsync, atomic rename, per-source order, explicit backlog/backpressure,
  error propagation and shutdown drain. It may be implemented only after the
  remux-admission subphases show which main-loop operation inherited the same
  stall and deterministic red tests freeze those guarantees.

### Round 23: remux-admission subphase attribution

- Tests-only `21e2438` first required non-overlapping completion scan/result,
  handoff persist, convergence/release, finalizer admission, candidate query,
  capacity reservation, prepare/claim, heartbeat and executor-submit timings,
  plus counts and artifact aggregation. Instrumentation-only `c128073` carries
  those fields through the existing scheduler log and
  `downstream_observability_summary.json`. It does not change a claim, lease,
  queue, worker count, fsync, retention setting or deadline. The combined
  sink/scheduler/pressure suite passed 312 tests.
- Retention-crossing diagnostic artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_remuxattrib_ioadm3_b6m_r300_20260722T034818Z`.
  It used the same fixed 4,800-second fixture/hash, 60 routes, disk-backed
  300-second retention, index width three and Candidate B
  WIP/remux/max-per-poll/finalizer `20/12/8/8-4-8`. The measured sample was
  360 seconds with 25-second postfill and configured 120-second drain. This is
  a causal diagnostic, not the required exact 600-second r300 gate.
- Input and correctness passed: 60/60 sources, 8.0419 steady FPS, zero Savant
  send-failure delta, forwarder queue-full, raw-forwarder drop or raw send
  failure. The formal window ended with 920/920 tasks materialized; all 987
  retained evidence items passed 8090 detail, all 637 videos passed the 5+5
  duration/raw-FPS, timeline, annotation, bbox and person-context checks, and
  all residual task/lease/WIP/lane/finalizer-pending state returned to zero.
  Finalizer candidates/admitted were `637/637`, with zero handoff recovery,
  fenced retry failure, claim-busy, duplicate or finalizer failure.
- The harness status was `passed` with only
  `validate_seq_iq_expected_sampling_gap`, but the stricter Spec 33 gate did
  not pass. DB-backed ready-to-claim p95 was `4.413s`, DB claim wait p95
  `0.058s`, finalizer pool wait p95 `0.001s`, lifecycle p95 `20.334s`, and
  media-worker peak CPU `104.52%`; those pass their respective closure bounds.
  Media queue p95 was `19.910s` against the 15-second release/10-second closure
  gates, and rolling metadata visibility p95 was `7.610s` against 2 seconds.
  Visibility p50/p99/max was `0.586/12.446/15.596s`, with 587/443/115 segments
  above 2/5/10 seconds.
- Across 8,186 sink publications, total p50/p95/p99/max was
  `5.985/27.210/108.481/4478.115ms`; 30 exceeded one second and 10 exceeded
  three seconds. Per-source publication-gap p50/p95/p99/max was
  `3.504/3.564/16.603/50.203s`; 257 gaps exceeded eight seconds across all 60
  sources. Correlation with total publication time was `0.8421` metadata
  fsync, `0.8874` manifest fsync, `0.9387` staging-directory fsync and `0.8819`
  parent-directory fsync.
- The paired tail at `03:54:31-36Z` is the first fully retained cross-service
  causal proof. Sink A/B publication maxima were `4059.671/4478.115ms`; the
  media runner poll ending `03:54:36.552Z` was `8330.840ms`, of which
  prepare/claim was `5194.950ms` and handoff persistence `3119.980ms`.
  Candidate query was `3.267ms`, finalizer admission `10.881ms`, and all other
  runner subphases together were below two milliseconds. Later polls separately
  reached `6033.340ms` handoff persistence and `6327.690ms` prepare/claim while
  new three-to-four-second sink fsync bursts were active. This proves
  PostgreSQL claim/context and handoff transactions inherit host-storage flush
  stalls; it is not a slow candidate query, executor submission or finalizer
  admission problem.
- Retention maintenance remained secondary even though it also felt the busy
  disk: 18 passes had duration p50/p95/max `651.5/1612.1/3369ms`, discovery
  p95 `1406.3ms` and mutation-lock hold p95 `200.8ms`. It does not explain the
  two sink services synchronously pausing all their source publications or the
  paired 4.1-4.5-second fsync events.
- Cleanup restored the daily single branch, Compose-network PostgreSQL,
  disabled rolling materialization, 300-second retention, index width two and
  daily worker capacities. Enabled cameras, pressure publishers/containers,
  MediaMTX/ffmpeg processes, active tasks, leases and finalizer-pending rows
  are zero. The worktree was clean at `c128073`; about 206GB remained free.
- Conclusion: move the unchanged `AtomicSegmentPublisher.publish()` durability
  sequence onto one process-lifetime, single-worker, bounded FIFO dispatcher
  per sink. One worker preserves the current per-sink publication concurrency
  and global/per-source order; a finite outstanding limit absorbs rare fsync
  bursts and becomes explicit backpressure under sustained storage failure.
  Publish exceptions must mark the source unhealthy, and sink termination must
  drain queued publications before exit. Do not add publisher parallelism or
  change any fsync in this variable.

### Round 24: bounded publication dispatcher and runtime smoke

- Red contracts were committed separately. `b92876c` requires the GLib-facing
  ledger close to return before an injected slow durable publish, exact global
  and per-source FIFO order, a hard outstanding limit with blocking
  backpressure, no fragment drop, publish-error continuation/source failure
  and bounded shutdown drain. `104472d` requires terminal dispatcher state to
  survive the retained pressure-log summary.
- `c86430e` adds one process-lifetime dispatcher per rolling sink. Its only
  worker calls the unchanged `AtomicSegmentPublisher.publish()` sequence, so
  metadata/manifest/staging/parent fsyncs, same-filesystem atomic rename and
  journal behavior are unchanged. A semaphore bounds active plus queued work
  at 128; a full bound blocks the submitting callback rather than dropping or
  creating an unbounded executor queue. Publication failure invokes the source
  callback, and sink shutdown stops admission then drains before exit.
- `647dd2f` emits one structured terminal snapshot with capacity/worker count,
  current and peak queue/outstanding, active, submitted/completed/failed,
  capacity-wait total/max/events and shutdown-timeout count. The pressure
  summary retains every numeric field plus the stopped/drained marker.
- Unique validation was 386 passing tests: sink/pressure/analyzer 237,
  lifecycle/scheduler/performance 89, rolling/index/DB-index 31 and deployment
  contract 29. Compile, Compose rendering and diff checks passed. The local
  Python environment did not contain the optional Ruff module, so no Ruff pass
  is claimed.
- Real bind-mounted artifact:
  `/data/video-analytics/artifacts/rolling_publication_dispatcher_smoke_20260722T042517Z`.
  Both affected dual sink services were force-recreated without rebuilding and
  became healthy. Inside the real sink image, three actual atomic segments from
  two sources published and invoked callbacks in the exact submitted order.
  With smoke capacity two, the third submit blocked for 155.993ms while the
  first publish was held, outstanding peak stayed two, and all three durable
  manifests/videos completed with no failure or timeout.
- The injected error path recorded exactly one failed publication, continued
  to publish the following good segment, and marked the active source pipeline
  failed. Both dispatchers ended at queue/outstanding/active zero. The two real
  service shutdown logs independently reported `drained=True`, production
  capacity 128, worker count one and zero final queue/outstanding/active/
  failure/timeout.
- Restoration after the smoke left both temporary dual sinks stopped, zero
  enabled cameras, active tasks, leases and finalizer-pending rows, no pressure
  publishers/MediaMTX/ffmpeg process, 300-second retention and about 206GB
  free. The pre-existing runtime doctor still reports unrelated env/source-
  adapter drift; this loop did not modify it.
- This closes only deterministic and real-container dispatcher correctness.
  It does not prove the 60-route GLib publication-gap causal hypothesis or any
  latency gate. The next unique run is a short retention-crossing 60-route
  diagnostic at the unchanged fixed input, Candidate B `20/12/8`, finalizer
  `8/4/8`, index width three and r300.

## Recovery audit after Round 21

Both the failed attribution artifact and the two valid post-fix artifacts were
preserved. A fresh live audit after exact-r300 completion confirmed the daily
single branch, zero enabled cameras, no pressure source, ffmpeg publisher or
local MediaMTX process/container, and zero active materialization task, lease,
Replay slot or finalizer-pending row. Media/event/clip/face workers all use
`postgres:5432`, are running with restart count zero, and the two real gallery
targets remain active. Media-worker returned to max-active/remux/finalizer
`4/4/32`, process-finalizer `0`, rolling materialization disabled, 300s
retention, 256-row cache and segment-index width two. Redis `save` returned to
`3600 1 300 100 60 10000`; PostgreSQL returned to
`checkpoint_timeout=5min`, `max_wal_size=1GB`, `min_wal_size=80MB` and
`wal_compression=off`.

## Next gates

1. Keep r3840 and both one-hour acceptances blocked. Round 22 is a failed
   diagnostic and Round 23 is only 360 seconds; neither replaces the exact
   width-three 600-second r300 gate.
2. Run the short 60-route retention-crossing diagnostic with no capacity,
   retention, deadline, fsync or rename change. Confirm source publication
   gaps no longer follow slow fsync execution on the GLib callback, production
   outstanding peak stays at or below 128, and terminal queue/outstanding/
   active/failure/timeout are zero.
3. Repeat the exact r300 gate with the fixed fixture/hash. Only a complete
   input, capacity, visibility, watchlist, annotation and residual pass permits
   the 3,840-second-retention short gate.
4. Only after r300 and r3840 pass, run two comparable one-hour acceptances with
   full evidence/8090 validation, restore the daily runtime, and then consider
   Phase 7 legacy removal and completion.
