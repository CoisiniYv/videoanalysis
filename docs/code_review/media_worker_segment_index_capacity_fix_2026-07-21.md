# Media-worker rolling segment index capacity closure (2026-07-21)

## Status

Ongoing. This document is a resumable measurement and change ledger, not a
completion claim. The source-window-bounded pin improved the width-three r300
comparison, but the strict r300 capacity and visibility gates still failed.
Neither the two retention short gates nor the two one-hour acceptance runs have
passed yet.

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

1. Repeat the unchanged width-three r300 gate at `1ec97fc`; Candidate B WIP,
   remux, max-per-poll, finalizer dimensions, fixture hash and all other inputs
   remain fixed. Compare refresh, slot wait, pinned-segment distribution,
   ready/media/lifecycle, service rate, and visibility against Round 9.
2. Only if that r300 gate passes every input, capacity, correctness, annotation,
   visibility, and residual gate, run r3840. Do not increase admission width or
   use the harness's watchlist-only failure list as a substitute for the strict
   latency gates.
3. Only after both short gates pass, run two comparable one-hour acceptances
   with the fixed fixture/hash and the full evidence/8090 validation set.
