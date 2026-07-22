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
60-route causal diagnostic in Round 25 proves that the dispatcher preserves
correctness and reduces some extreme source gaps, but does not pass the causal
capacity gate: the shared FIFO reaches 92/97 outstanding, host-wide fsync
stalls still block PostgreSQL, metadata visibility p95 is 7.901 seconds and
media queue p95 is 19.316 seconds. Round 26 adds exact queue-residence/service
attribution and repeats the unchanged 360-second diagnostic. Both sink queues
reach the hard 128 bound; queue-residence p95 is 5.172/4.916 seconds while
durable-service p95 is only 28.8/33.2ms, and the slowest visible segments spend
13.5-14.1 seconds queued before their own 5-33ms service. This dynamically
confirms a rare fsync-service burst followed by FIFO head-of-line residence,
not insufficient average service rate. Strict ready/media/visibility p95 is
6.668/23.426/3.256 seconds. Round 27 then tested one bounded natural FIFO
preparation cohort before the unchanged serial durable commits. All correctness
gates passed, but ready/media/visibility p95 regressed to
19.265/33.298/22.799 seconds, dispatcher residence p95 rose from 5.041 to
14.004 seconds, and dispatch-total p95 rose from 5.216 to 18.385 seconds.
Grouping is therefore rejected and the production preparation limit is one;
explicit multi-item grouping remains only as a diagnostic/test mechanism.
Round 28 then serialized cross-sink durable commits behind one epoch lock. It
removed overlap as designed but accumulated 74.592 seconds of explicit lock
wait, raised dispatcher residence p95 to 13.879 seconds, dispatch p95 to
14.025 seconds and metadata visibility p95 to 16.531 seconds. Arbitration is
therefore rejected and disabled by default. Round 29 implements the next
evidence-backed single variable: two deterministic source-sharded publication
workers per sink are available behind an explicit bounded setting, while the
daily/default value remains one. Its tests, full static suites and real
container durability/failure/shutdown smoke pass, but the Round 29 diagnostic
rejects the behavior: four concurrent process-wide commit workers nearly
double mean durable service, increase >=1-second services from 25 to 90,
raise residence/dispatch p95 to 6.12-6.19/6.43-6.63 seconds and metadata
visibility p95 to 11.859 seconds. The next bounded hypothesis is host-wide
durable commit concurrency two, between the rejected one-slot Round 28 arbiter
and rejected four-worker Round 29 experiment. Round 30 implements two
deterministic cross-process lock lanes behind a default-off setting; all static
and real dual-container correctness gates pass, but its unchanged 360-second
diagnostic rejects the behavior. Correctness still passes, while lock waits add
139.866 seconds of service, residence/dispatch p95 regresses to
11.20-11.33/11.55-11.58 seconds, capacity-wait events rise to 294 and metadata
visibility p95 rises to 18.638 seconds. Two lanes are therefore diagnostic-only
and remain disabled by default. Round 31 implemented and then rejected the
selected default-off regular-file `fdatasync` diagnostic. Test, static,
real-container and pressure correctness gates all pass without changing either
directory `fsync` or the daily `fsync` default, but its unchanged causal
diagnostic roughly doubles both sinks' queue-residence and dispatch p95, raises
capacity-wait events from 55 to 233, and moves metadata visibility p95 from
3.256 seconds to 14.787 seconds. Regular-file and directory-sync cumulative
time both worsen, proving that `fdatasync` still induces the same ext4 device
durability episodes on this host. Exact r300 remains pending; r3840 and both
one-hour acceptance runs are still prohibited. Round 32 then implemented and
rejected the default-off single-inode metadata/manifest diagnostic. Its test,
static, dual-image smoke and pressure correctness gates pass, and regular-file
sync time falls below Round 26, but directory-sync time more than doubles,
residence/dispatch p95 roughly doubles, capacity-wait events rise from 55 to
200 and metadata visibility rises from 3.256 to 14.251 seconds. The hard-link
alias therefore moved the ext4 durability cost rather than removing it. The
next isolated contract removes only that alias while preserving the embedded
control record, one file fsync, both directory fsyncs and atomic rename. Round
33 implements that default-off metadata-only v3 representation, passes its red
contracts, static suites and real dual-image publication/index/recovery smoke,
then rejects it with the unchanged 360-second causal diagnostic. All input and
correctness gates pass, including 927/927 formal and 993/993 retained tasks,
but Round 26 -> Round 33 residence becomes 5.172/4.916 -> 6.739/6.746 seconds,
dispatch becomes 5.275/5.188 -> 7.162/7.325 seconds, capacity waits become
55 -> 165 and visibility becomes 3.256 -> 12.040 seconds. Regular-file sync
falls 80.352 -> 57.439 seconds and total worker service falls
176.954 -> 142.397 seconds, but directory sync remains worse at
47.179 -> 70.838 seconds and >=1-second services rise 25 -> 42. Parsing and
scheduler/DB headline p95 do not absorb the missing time. Metadata-only v3 is
therefore diagnostic-only; exact r300, r3840 and both one-hour runs remain
blocked.

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
| `d12bc70`, `f816202` | dispatcher smoke and Round 25 documentation | Records the real-container contract and failed short causal capacity result without widening its claim |
| `3fb27f0` | dispatcher timing red contracts | Requires per-segment capacity wait, FIFO residence, durable service, dispatch total and peak identity to survive pressure aggregation |
| `3c80722` | dispatcher queue-residence attribution | Carries monotonic per-segment timing, cumulative/maximum service state and peak transition identity through real sink logs and retained artifacts |
| `0479f07` | bounded FIFO preparation red contracts | Requires stage/commit separation, bounded natural cohorts, exact FIFO callbacks, per-item stage-error continuation, backpressure, shutdown and retained diagnostics |
| `f4bf094` | bounded FIFO preparation experiment | Stages at most 32 queued items, then commits each through every original fsync/rename/journal fence on the same single worker |
| `db20d24` | production-disable red contract | Requires the effective/default preparation group limit to remain one while explicit grouping stays supported |
| `555afef` | production grouping disable | Sets the production/default preparation limit to one without removing the stage/commit diagnostics or explicit multi-item test path |
| `d874756`, `2888927` | cross-sink commit-arbitration red contracts and experiment | Serialized the unchanged durable commit region behind one epoch-root flock and measured lock wait/hold separately |
| `a0b7f63`, `e590f9b` | arbitration production-disable contract and implementation | Restored independent commits by default after the Round 28 negative causal result |
| `7a45389` | Round 28 rejection ledger | Records the retained diagnostic, comparison and restored daily runtime without claiming closure |
| `d36e046` | source-sharded publication red contracts | Requires cross-source overlap, same-source FIFO/callback order, one global capacity bound, peer progress after shard failure, clean multi-worker shutdown and bounded config/deployment/pressure surfaces |
| `e7e2478` | bounded source-sharded publication | Adds deterministic `crc32(source_id) % worker_count` queues, active-peak/worker-index observability and an explicit pressure override while leaving the daily default at one |
| `19a0a47` | Round 29 rejection ledger | Preserves the passed correctness result, four-way fsync amplification analysis and restored daily runtime without advancing exact r300 |
| `1b33028` | two-lane durable-commit red contracts | Requires same-lane cross-process exclusion, peer-lane progress, stable assignment, error release, bounded shutdown and explicit config/deployment/pressure evidence |
| `b9ea4f1` | deterministic host commit lanes | Maps each source to one of 0-4 epoch-root flock lanes, retains the default-off/legacy one-lock paths and carries slot count/index through live and retained observability |
| `e0c1d16` | two-lane real-container proof | Records host peak two, per-slot peak one, failure release and clean dual-container shutdown without claiming pressure capacity |
| `cd3b327` | Round 30 rejection ledger | Preserves passed correctness, lane-convoy attribution, comparison artifacts and restored daily runtime without advancing exact r300 |
| `8333309` | regular-file sync-mode red contracts | Requires explicit fdatasync only for metadata/manifest, unchanged directory fsync/rename order, failure staging, default fsync and full pressure/deployment audit |
| `a618796` | bounded regular-file fdatasync diagnostic | Adds a default-off fsync/fdatasync choice while preserving every directory fence, capacity, callback, retention and scheduling behavior |
| `0f0efb6` | fdatasync implementation proof | Records the static and real-container durability/failure/shutdown proof without claiming pressure capacity |
| `640538c` | single-inode publication red contracts | Requires one aliased inode/file fence, legacy compatibility, bounded v2 discovery, exact frame rows, journal/reconcile recovery, failure staging, pressure/deployment audit and daily split default |
| `fa7731f` | default-off single-inode publication layout | Adds the v2 manifest control record and hard-linked historical paths while retaining staging/rename/directory fences and rejecting cross-inode v2 identities |
| `c4e54e6` | single-inode implementation proof | Records the static and dual-image durability/index/recovery smoke without claiming pressure capacity |
| `2e5c173` | pressure sink restore red contracts | Requires diagnostic containers to be recreated from daily Compose while preserving their original running/stopped state |
| `da35730` | pressure sink config restoration | Recreates only the affected sinks without dependencies, verifies state and prevents a diagnostic metadata layout from surviving cleanup |
| `22aa940` | metadata-only v3 red contracts | Requires alias-free one-fence publication, metadata-keyed journal/reconcile/fallback discovery, exact native rows, v1/v2 compatibility, failure staging and pressure observability |
| `f075b46` | default-off metadata-only v3 layout | Embeds the bounded v3 control record in `metadata.json`, omits the hard-link alias and preserves every directory, rename, journal, read-pin, FIFO and daily-default fence |
| `8e39094` | metadata-only implementation proof | Records static and real dual-image durability/index/recovery correctness without claiming pressure capacity |
| `af29c21` | sink-restore environment red contract | Reproduces diagnostic shell interpolation overriding the daily env file during stopped-container recreation |
| `3e35f16` | isolated daily sink restoration | Removes pressure-controlled interpolation keys for daily Compose, verifies nine recreated env values and fails cleanup on drift |

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

### Round 25: bounded-dispatcher retention-crossing diagnostic

- Artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_pubdispatch_ioadm3_b6m_r300_20260722T043320Z`.
  It retained the fixed 4,800-second fixture/SHA256, 60 routes, disk-backed
  300-second retention, Candidate B WIP/remux/max-per-poll `20/12/8`,
  finalizer threads/processes/queue `8/4/8`, index width three, 360-second
  sample, 25-second postfill and configured 120-second drain. It is a causal
  diagnostic, not the exact 600-second r300 gate.
- Harness/input/correctness passed: 60/60 sources with zero restart, steady
  effective FPS 8.0751 against 7.92, zero sampling send-failure delta,
  queue-full, raw drop or raw send failure. All 918 formal tasks and all 989
  warmup/postfill-inclusive tasks materialized. The retained set was 636
  behavior videos plus 353 watchlist images; 989/989 8090 detail, 636/636
  duration/raw-FPS/timeline/annotation, bbox and person-context checks passed.
  Person persistence was 73,806 rows versus 73,793 exports over 60 sources
  with measured loss zero. Gallery query/emission, duplicate, claim-busy,
  finalizer failure, expiry, lease and all drain residual gates passed.
- Dispatcher correctness held under real pressure. Sink A/B submitted and
  completed `4070/4083` publications, outstanding peaks were `92/97` of 128,
  queue-depth peaks `91/96`, and both final logs reported `drained=True` with
  queue/outstanding/active/failure/shutdown-timeout zero. Maximum capacity-
  acquisition wait was only `0.056/0.110ms`, so no submit reached the one-
  millisecond backpressure-event threshold.
- Strict latency partly passed: DB-backed ready-to-claim p95 was 3.748s,
  finalizer pool wait 1ms, media-worker claim wait 269ms, lifecycle 19.892s,
  DB lifecycle 17.444s and media-worker CPU 153.58%. Media queue p95 remained
  19.316s, failing both the 15-second release and 10-second closure bounds.
  Rolling metadata visibility p50/p95/p99/max was
  `0.589/7.901/10.383/11.622s`; 809/413/103 segments exceeded 2/5/10 seconds,
  so the 2-second gate failed. Exact r300 and r3840 remain prohibited.
- The dispatcher improved only some extremes. Compared with Round 23, source
  publication gaps above eight seconds fell from 257 to 180 and visibility
  max fell from 15.596s to 11.622s. However gap p95 rose from about 3.56s to
  about 5.1s, visibility p95 moved from 7.610s to 7.901s, and media queue p95
  only moved from 19.910s to 19.316s. This is not an accepted improvement.
- The causal hypothesis that GLib callback execution was the remaining primary
  boundary is falsified. At `04:39:53-04:40:03Z`, each sink's single worker
  performed a series of 1.6-3.15-second publications dominated by required
  metadata/manifest/staging/parent fsyncs. The matching media scheduler tick
  lasted 10.298 seconds: 6.275 seconds handoff persistence plus 4.015 seconds
  prepare/claim. The next cycle reported the 10.401-second poll gap. Moving
  the calls off GLib did not isolate PostgreSQL from the same host-wide flush.
- The slowest visible segments make the new FIFO delay explicit even without
  a queue-residence timestamp. Their last frame PTS clustered near
  `04:39:52Z`; they became visible/published near `04:40:03.6-04:40:04.0Z`,
  yet their own `publish()` calls took only 8-100ms. Delay therefore accumulated
  before each segment's own publish while the shared worker drained earlier
  fsync-heavy items. The low correlation between a segment's own publish time
  and its completion lag (`0.077-0.107`) agrees with upstream FIFO residence.
- Retained comparison analysis is
  `publication_dispatcher_comparison.json` in the artifact. The next unique
  variable is instrumentation only: record submit-to-worker-start queue
  residence, worker-start-to-durable-complete service, submit-to-complete total
  and peak-transition time/source/segment. Carry them through segment logs and
  the pressure artifact before selecting another behavior variable. Do not
  add publisher workers, remove fsyncs, widen media capacity, change retention/
  deadlines or run exact r300 based on this failed result.
- Cleanup restored daily single-branch services, rolling materialization off,
  300-second retention, index width two, Redis/PostgreSQL defaults and zero
  enabled cameras, pressure processes, active tasks, leases or
  finalizer-pending rows. About 204GB remained free and the worktree was clean
  before this documentation update.

### Round 26: direct dispatcher queue-residence attribution

- Tests-only `3fb27f0` first required per-segment capacity acquisition wait,
  submit-to-worker-start FIFO residence, worker-start-to-durable-complete
  service, submit-to-complete total, submit depth and peak transition identity.
  Instrumentation-only `3c80722` uses monotonic clocks for durations and a wall
  clock only for the peak timestamp; it attaches diagnostics before success or
  error callbacks and changes no queue, worker, fsync, rename, retention,
  deadline or scheduling behavior.
- Validation before runtime was 237/237 sink/pressure tests plus 287 passed and
  one expected skip across lifecycle, scheduler, finalizer, segment-index,
  DB-index, deployment and topology suites. Compile, Compose rendering,
  deployment smoke and diff checks passed.
- Real-container timing smoke:
  `/data/video-analytics/artifacts/rolling_publication_dispatcher_timing_smoke_20260722T051218Z`.
  The exact two-source FIFO and callbacks matched; a capacity-two third submit
  waited 157.108ms, the queued second fragment resided 157.059ms, every task's
  total decomposed into capacity wait + residence + durable service, the error
  task retained diagnostics and the following good task published. Both real
  production services then logged `drained=True`, capacity 128, one worker and
  zero final queue/outstanding/active/failure/timeout.
- A first pressure launch is retained as
  `pressure60_8p1_pubtiming_ioadm3_b6m_r300_20260722T051537Z`; it stopped before
  runtime mutation because the host DSN defaulted to closed port 5432. The
  valid launch explicitly fixed host PostgreSQL at `127.0.0.1:5439` and the
  container DSN at `postgres:5432`.
- Valid diagnostic artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_pubtiming_ioadm3_b6m_r300_20260722T051559Z`.
  It retained the fixed fixture/hash, 60 routes, disk-backed r300, Candidate B
  `20/12/8`, finalizer `8/4/8`, index width three, 360-second sample, 25-second
  postfill and 120-second drain. This remains a causal diagnostic, not the
  exact 600-second r300 acceptance.
- Harness, input and correctness passed: 60/60 sources with zero restart,
  8fps minimum/input-loss gates, all 930 formal tasks and all 1,000 retained
  tasks materialized, and 639 video plus 361 watchlist-image bundles survived.
  All 639 videos passed exact 5+5 duration/raw-FPS/timeline/annotation/bbox/
  person-context, all 1,000 8090 details passed, and person persistence was
  74,260 rows versus 74,252 exports over 60 sources with measured loss zero.
  Expiry, recovery, retry failure, claim-busy, duplicate, finalizer failure and
  final task/lease/WIP/lane/finalizer-pending residuals were zero.
- Strict capacity still failed. DB-backed ready-to-claim p95 was 6.668s versus
  the 5s closure gate, media queue p95 23.426s versus 10s, and rolling metadata
  visibility p95 3.256s versus 2s. Finalizer pool wait 1ms, DB claim wait
  84.1ms, lifecycle 19.923s and media-worker CPU 126.88% passed their closure
  bounds. The scheduler poll-gap p95 remained 1.000s, but two storage episodes
  produced 12.090s and 14.430s maximum cycles through prepare/claim and handoff
  persistence.
- Direct sink attribution is decisive. Sink A/B processed 4,100/4,114
  publications. Durable-service p50/p95/max was
  `6.455/28.782/4761.355ms` and `6.392/33.226/6413.449ms`; each worker was busy
  only about 16.4% of the observed span, so average service capacity exceeded
  arrival. In contrast FIFO residence p50/p95/p99/max was
  `0.524/5171.767/13768.015/17584.347ms` and
  `0.508/4916.136/13724.345/17487.711ms`. Both reached outstanding 128 and
  queue depth 127; 28/27 submits incurred >=1ms capacity backpressure, with
  maxima 2.490/2.379s.
- Dispatch total correlates 0.9979/0.9975 with queue residence but only
  0.1878/0.1778 with the segment's own service. The slowest visibility records
  had the normal approximately 0.59s close-to-submit delay, then
  13.5-14.1s residence and only 5-33ms own durable service. Twenty overlapping
  cross-sink >=1s service intervals were retained; the largest paired episode
  included A's 4.761s and B's 6.413s services, each split across the unchanged
  metadata, manifest, staging-directory and parent-directory fsync fences.
  The causal analysis is retained as
  `publication_dispatcher_timing_analysis.json` in the artifact.
- The next single behavior hypothesis is bounded FIFO group preparation: stage
  a natural publication cohort's metadata/manifest before committing it in
  exact FIFO order through the unchanged per-segment fsync/rename sequence.
  The intended mechanism is filesystem group commit/writeback smoothing, not
  extra durable workers or a removed fence. It must first be frozen by tests
  for hard bounds, order, per-item error continuation, crash windows and
  shutdown; a short unchanged r300 diagnostic must then prove lower service
  burst area and residence without moving delay into callback backpressure.
- Cleanup restored daily single-branch services, rolling materialization off,
  r300, index width two, Redis/PostgreSQL defaults, zero enabled cameras,
  pressure processes and lifecycle residuals. About 202GB remained free.

### Round 27: bounded FIFO preparation is rejected

- Tests-only `0479f07` first required separate stage/commit phases, a bounded
  natural FIFO cohort, exact serial commit and callback order, hard outstanding
  backpressure, middle-stage-error continuation, preserved per-item failure,
  and clean shutdown. `f4bf094` implemented a maximum preparation group of 32
  on the existing single worker. Metadata and manifest writes occur during
  stage; metadata/manifest/directory/parent fsync, atomic rename and journal
  append remain in every per-segment commit and keep exact FIFO order.
- Static validation passed 237 sink/pressure tests; broader selections passed
  89, 31, 156 with one expected skip, and 167 tests. Analyzer/deployment
  selections passed 32 tests. Compile, Compose render, deployment smoke and
  diff checks also passed.
- Real-container smoke:
  `/data/video-analytics/artifacts/rolling_publication_group_smoke_20260722T060238Z`.
  It proved natural group sizes `1/3/1`, exact stage/commit/callback order,
  bounded capacity wait, continuation after an injected middle stage error,
  source failure propagation and clean production sink drain. This closed only
  the grouping correctness contract.
- The unchanged 360-second diagnostic artifact is
  `/data/video-analytics/artifacts/pressure60_8p1_pubgroup_ioadm3_b6m_r300_20260722T060611Z`.
  It retained the fixed fixture/hash, 60 routes, disk r300, Candidate B
  `20/12/8`, finalizer `8/4/8`, index width three, 25-second postfill and
  120-second drain. It is not the exact 600-second r300 acceptance.
- Input and correctness passed: 60/60 sources, zero send failure, queue-full or
  raw loss, 915/915 formal tasks materialized, and 986/986 retained details
  passed. All 636 videos passed duration/FPS/timeline/annotation/bbox/
  person-context; 350 retained items were images. Expiry-without-attempt,
  duplicate, claim-busy, finalizer failure, lease and final residual counts
  were zero.
- Capacity regressed materially against Round 26. Ready-to-claim p95 moved
  `6.668s -> 19.265s`, media queue `23.426s -> 33.298s`, lifecycle
  `19.923s -> 33.987s`, and metadata visibility `3.256s -> 22.799s`.
  Dispatcher residence p95/p99 moved `5.041s/13.761s ->
  14.004s/31.765s`; dispatch-total p95 moved `5.216s -> 18.385s`; commit
  service p95 moved `31.6ms -> 51.2ms`.
- Once saturation began, size-32 groups became common. Commit-wait p95/p99 was
  `0.619s/15.771s`; grouped-item dispatch p95 was 33.362s versus 31.55ms for
  singleton items. Capacity-wait events rose from `28/27` to `339/341` across
  sinks. Combined service time rose 62.9%, service events over one second rose
  from 25 to 81, and 68 cross-sink >=1s service pairs overlapped. Per-sink busy
  ratio rose from about 16.4% to 28.8-29.2% without gaining useful throughput.
- The mechanism is negative causal evidence: staging 32 metadata/manifest
  pairs dirtied more filesystem state before the first retained per-item fsync,
  then every serial durability fence remained. A rare stall therefore grew
  into a larger writeback/flush convoy instead of smoothing it. The hypothesis
  is rejected; exact r300, r3840 and one-hour runs are not authorized from this
  result.
- `db20d24` added a red production contract for preparation limit one, and
  `555afef` changed only the effective/default constant from 32 to 1. Explicit
  multi-item construction and every stage/commit/group diagnostic remain for
  historical reproduction. The focused sink suite passed 37 tests, the
  pressure aggregation check passed, compile and diff checks passed.
- The next evidence-backed single variable is cross-sink durable-commit
  arbitration: one shared epoch-root filesystem `flock` across sink A/B commit
  sections, with no additional publisher worker and no removed fsync/rename/
  journal fence. Lock wait and hold must be measured separately. Concurrent
  same-process, real cross-process, error and shutdown contracts must go red
  first, followed by a real-container smoke and one unchanged 360-second r300
  diagnostic. It advances only if service burst area, total dispatch latency,
  visibility and capacity backpressure all improve materially.

### Round 28: cross-sink durable-commit arbitration is rejected

- Tests-only `d874756` first required one epoch-root filesystem lock across
  concurrent sources and processes, bounded shutdown while waiting, preserved
  staging on lock failure, release after commit failure, and separate lock-wait
  and lock-hold diagnostics. `2888927` added that arbitration around each sink
  A/B durable commit. Staging stayed outside the lock; every metadata/manifest/
  directory/parent fsync, atomic rename, journal append, one worker per sink,
  exact per-sink FIFO order and the 128 outstanding bound remained unchanged.
- The focused rolling-sink suite passed 41 tests and the pressure harness passed
  201. Compile and diff checks passed. Real cross-container smoke:
  `/data/video-analytics/artifacts/rolling_publication_commit_arbiter_smoke_20260722T064452Z`.
  Both containers observed the same lock inode; one waited 253.952ms behind the
  other's approximately 254ms hold. An injected rename failure retained its
  staging directory and released the lock, allowing the peer to wait 247.203ms
  and commit successfully. Production preparation remained one.
- The unchanged 360-second diagnostic is
  `/data/video-analytics/artifacts/pressure60_8p1_pubarb_ioadm3_b6m_r300_20260722T064800Z`.
  It kept the fixed fixture/hash, 60 routes, disk r300, Candidate B `20/12/8`,
  finalizer `8/4/8`, index width three, 25-second postfill and 120-second drain.
  This is negative causal evidence, not the exact 600-second r300 acceptance.
- Input and correctness passed: 60/60 sources at 8.0591 effective FPS with zero
  send failure, queue-full or raw loss; 925/925 formal tasks materialized; all
  993 retained details passed. All 635 retained videos passed the 5+5 window,
  raw FPS, timeline, annotation, bbox and person-context checks; 358 retained
  items were watchlist images. Expiry-without-attempt, active task, lease and
  finalizer-pending residuals were zero. The only harness warning was the
  expected `validate_seq_iq` sampling gap.
- Capacity regressed against the ungrouped Round 26 baseline. Ready-to-claim p95
  moved `6.668s -> 10.714s`, media queue `23.426s -> 26.607s`, lifecycle
  `19.923s -> 27.131s`, and metadata visibility `3.256s -> 16.531s`.
  Dispatcher residence p95 moved `5.041s -> 13.879s`, dispatch-total p95
  `5.216s -> 14.025s`, and capacity-wait events `55 -> 373`. Worker-service p95
  fell `31.621ms -> 22.105ms`, but cumulative service rose
  `176.954s -> 197.333s`; the lower per-item percentile did not produce useful
  end-to-end capacity.
- Arbitration itself functioned: 8,147 commits had zero reconstructed overlap
  between cross-sink lock holds over 50ms. It instead converted peer stalls into
  74.592s of explicit lock wait across 817 >=1ms waits. Lock holds totaled
  119.851s, with 78 over 100ms and 31 over one second. Two independent FIFO
  queues therefore accumulated behind one global slow holder, increasing
  residence, dispatch latency, visibility and callback backpressure. The
  hypothesis is rejected; exact r300, r3840 and one-hour runs remain blocked.
- Red production contract `a0b7f63` and implementation `e590f9b` now keep
  `SEGMENT_PUBLICATION_COMMIT_ARBITRATION_ENABLED=False`. A normal publisher
  never opens the epoch commit-lock file and reports exactly zero lock wait/hold.
  Explicit constructor opt-in, arbitration behavior and all diagnostics remain
  available for historical reproduction. The complete sink suite passes 42
  tests, the pressure harness 201, and the rendered deployment smoke 29. No
  further capacity experiment is authorized until this artifact is reviewed
  and one new single variable is justified without combining knobs.

### Round 29: bounded source-sharded publication implementation

- Round 26 showed that each sink worker was busy only about 16.4% of the
  observed span while residence p95 was about five seconds and 20 slow
  cross-sink commits overlapped. Round 28 then proved that removing that
  overlap is harmful. The remaining structural hypothesis is therefore the
  global FIFO inside each sink: one rare durability stall should not make
  unrelated sources sharing that sink wait behind the same head item.
- Tests-only `d36e046` first required two different CRC32 source shards to
  execute concurrently; every fragment from one source to remain serial and
  callback-ordered; the capacity semaphore to remain global across shards;
  one shard's failure not to stop peer progress; and multi-worker shutdown to
  drain all accepted work. Configuration must default to one, reject values
  outside 1-4, wire all three Compose sink services, propagate an explicit
  pressure value and retain effective worker count, worker index and active
  peak in pressure summaries.
- Implementation `e7e2478` creates one bounded queue/thread per worker and
  assigns it with `crc32(source_id) % worker_count`. The same source therefore
  cannot cross workers, while different shards may overlap. A single existing
  `BoundedSemaphore` still caps aggregate accepted work at 128 across all
  queues. Production preparation remains one and cross-sink commit arbitration
  remains false. Every metadata/manifest/staging-directory/parent-directory
  fsync, atomic directory rename, journal append, failure callback and shutdown
  drain remains in its original per-fragment order.
- The daily and Compose default is
  `ROLLING_CACHE_PUBLICATION_WORKERS=1`; both the sink config and pressure CLI
  bound explicit values to 1-4. The pressure profile default also remains one,
  so only a labeled diagnostic can select two. Per-fragment logs now expose
  `publication_worker_index`; dispatcher metrics and terminal logs expose
  `publication_worker_count` and `publication_active_peak`.
- Static validation passed the complete rolling-sink suite (51 tests), the
  pressure harness/analyzer selection (206 tests) and deployment suite (29
  tests). Python compile, shell syntax, Compose rendering and diff checks also
  passed.
- An ephemeral real-container smoke used image
  `sha256:237f063901d48adfd4265b519d1a40813d94df62717d506fcd197d71d8182bd0`
  with the current app bind-mounted. Sources `camera-04` and `camera-00`
  deterministically selected workers zero and one and reached
  `publication_active_peak=2`. Three real atomic publications completed with
  three manifests and three journal records. A fourth publication injected an
  `OSError` on worker zero and waited for worker one's following same-source
  publication, proving peer progress before failure propagation. The terminal
  marker was `PASS_PUBLICATION_SHARD_CONTAINER_SMOKE`: 4 submitted, 3
  completed, 1 failed, zero outstanding/active, `drained=true`, and both
  worker threads stopped.
- The unchanged 360-second diagnostic artifact is
  `/data/video-analytics/artifacts/pressure60_8p1_pubshard2_ioadm3_b6m_r300_20260722T0745Z`.
  It used the same 4,800-second fixture and
  `42d477ae2bc4eadf4dcd6192ef9a963926e1d88b1755c59e935270230d047490`
  hash, 60 routes, disk r300, Candidate B `20/12/8`, finalizer `8/4/8`,
  index width three, preparation one, 25-second pre/postfill and 120-second
  drain. Publication workers two was the only behavior variable against the
  Round 26 baseline. This is a rejected causal diagnostic, not exact r300.
- Input and correctness passed. Both forwarder and Savant observed 60/60
  sources; steady effective FPS was 8.0791 versus the 7.92 minimum, and send
  failure, queue-full, raw drop and raw-send failure were zero. All 924 formal
  tasks materialized with 588 behavior videos and 336 watchlist images; all
  998 retained 8090 details passed. The 636 retained videos passed the 5+5
  duration, >=20 FPS, timeline, annotation, bbox and person-context checks.
  Attempt-zero expiry, duplicate/finalizer failure, active task, lease, WIP,
  lane and finalizer-pending residuals were zero. Person persistence covered
  all 60 sources with 77,591 rows versus 77,580 exports and measured loss zero.
- The intended variable was exercised exactly. Both sinks reported worker
  count and active peak two, zero publication failure/timeout and a clean
  drain. Each sink assigned 15 sources to each CRC32 worker and no source
  crossed workers. Preparation limit was one, commit-lock wait/hold remained
  zero and both queues returned to zero.
- Capacity nevertheless regressed against Round 26. Sink A/B residence p95
  moved `5.172/4.916s -> 6.122/6.186s`; dispatch p95 moved
  `5.275/5.188s -> 6.430/6.627s`. Both still reached 128 outstanding.
  Capacity-wait events rose `55 -> 162`, cumulative capacity wait rose
  `6.215s -> 22.830s`, and maxima rose to 3.275/3.912 seconds. Ready-to-claim
  p95 moved `6.668s -> 6.958s`; media queue remained failing at 21.933
  seconds; metadata visibility regressed `3.256s -> 11.859s`. Media-worker
  CPU rose `126.88% -> 144.17%`. Lifecycle improved to 17.545 seconds and
  finalizer/DB claim remained fast, but those downstream passes do not offset
  the publication and visibility regressions.
- The mechanism is four-way fsync amplification, not bad sharding. Across
  8,600 publications, mean durable service rose `21.543ms -> 39.635ms` and
  cumulative service rose `176.954s -> 340.864s`; >=1-second services rose
  `25 -> 90`. Commit-service concurrency reached four and remained at four
  for 60.878 seconds; 90 slow services formed 183 overlap pairs, including 60
  within one sink across its workers and 123 across sinks. Per-sink aggregate
  service busy time rose from about 16.4% to 29.5-30.3%. The slowest visible
  segments then spent about 25.1 seconds in their source-shard queue before
  only 6-124ms of their own service.
- Reproducible comparisons are retained as
  `publication_shard_timing_analysis.json` and
  `publication_shard_concurrency_analysis.json` in the artifact. Two workers
  per sink without a host-wide commit ceiling are rejected. The next single
  variable is a two-slot host-wide durable-commit bound while retaining the
  same source-stable queues. One slot already failed in Round 28; four active
  commits failed here; two preserves one peer-progress lane and matches the
  Round 26 host overlap envelope. It must beat the Round 26 residence,
  dispatch, capacity-wait and visibility baseline, not merely improve on this
  rejected run, before exact r300 is authorized.

### Round 30: two-lane host commit bound implementation

- Tests-only `1b33028` first required sources on the same lane to serialize
  while a different lane progresses; the same exclusion to hold against an
  external process; a blocked lane to make bounded shutdown return false and
  later drain; and a commit exception to preserve staging, release its lane and
  allow a same-lane successor. The default diagnostics must report zero slots
  and index -1. Sink config, all three Compose services, pressure CLI/profile,
  applied container environment and retained summaries must expose a bounded
  0-4 slot count plus per-fragment slot index.
- Implementation `b9ea4f1` leaves
  `ROLLING_CACHE_PUBLICATION_COMMIT_SLOTS=0` in the daily environment. A value
  of one reuses the historical Round 28 epoch lock; values 2-4 select
  `.segment-publication-commit-slot-N.lock` with
  `crc32(source_id) % slot_count`. With workers two and slots two, each source
  remains on one dispatcher queue and the matching commit lane across both
  sink processes. The four worker threads may prepare independently, but at
  most one commit per lane can execute the metadata/manifest/directory/parent
  fsync, rename and journal sequence. No fence, capacity, retention, deadline,
  callback or drain order was removed.
- Full static validation passed 58 rolling-sink tests, 208 pressure/analyzer
  tests and 29 deployment tests. Python compile, shell syntax, Compose render
  and diff checks passed. Historical default-off and explicit one-lock tests
  remain green.
- Real dual-container artifact:
  `/data/video-analytics/artifacts/rolling_publication_commit_slots_smoke_20260722T081551Z`.
  Two service-image containers bind-mounted the current app and one shared
  epoch, each ran two dispatcher workers and submitted both slot-zero and
  slot-one sources. A shared audit around the real atomic rename observed
  host commit peak two, per-slot peak one, 16 balanced start/end events and
  zero concurrency violation. Both containers reached active-worker peak two.
- Eight fragments were staged. Seven completed every durable fence and
  produced seven journal records. One slot-zero rename raised the injected
  `OSError`; its partial staging directory remained, the lock was released and
  the same lane later published successfully. Cross-container lock waits were
  about 245-257ms. Role A ended 3 complete/1 failed and role B 4/0; both ended
  drained with zero outstanding/active/shutdown timeout and both worker threads
  stopped. This closes the implementation gate only.
- The next unique runtime variable against rejected Round 29 is commit slots
  two. The short diagnostic must otherwise retain its exact 4,800-second
  fixture/hash, 60 routes, 360-second sample, disk r300, Candidate B
  `20/12/8`, finalizer `8/4/8`, index width three, preparation one and two
  source workers per sink. Actual commit overlap must reconstruct to at most
  two. It must preserve all correctness/residual gates and beat Round 26
  residence/dispatch p95, 55 capacity-wait events and 3.256-second visibility
  p95 before exact r300 is authorized.
- The retained diagnostic is
  `/data/video-analytics/artifacts/pressure60_8p1_pubslots2_ioadm3_b6m_r300_20260722T0820Z`.
  It kept the fixed fixture/hash, 60 routes, 360-second sample, disk r300,
  Candidate B `20/12/8`, finalizer `8/4/8`, index width three, preparation one,
  25-second pre/postfill and 120-second drain. Publication workers two and
  commit slots two were applied to both sinks; commit slots were the only
  behavior change from rejected Round 29. This is negative causal evidence,
  not the exact 600-second r300 gate.
- Input, correctness and retained evidence passed. Forwarder and Savant both
  observed 60/60 sources, steady effective FPS was 8.0511, and send failure,
  queue-full, raw drop and raw-send failure were zero. All 926 formal tasks
  materialized. All 1,001 retained 8090 details passed; all 642 retained videos
  passed the 5+5 duration, raw-FPS, timeline, annotation, bbox and
  person-context checks, alongside 359 watchlist images. Three accepted
  annotation-count differences were DB overlay rows merging to one clip frame;
  no annotation, bbox or person-context row was missing. Person persistence was
  exactly 74,134/74,134 over 60 sources. Attempt-zero expiry, duplicate,
  claim-busy, finalizer failure, task, lease, WIP, lane and finalizer-pending
  residuals were zero.
- The intended lanes were exercised. Both sinks reported worker count and
  active peak two, slot count two, slot indexes zero and one, zero publication
  failure/shutdown timeout and a clean drain. Each sink assigned 15 sources to
  each worker; all 8,203 records retained one stable worker and slot per source.
  The exact dual-container audit already measured host peak two/per-slot peak
  one. Pressure-log projection is limited because `on_published` logs after the
  monotonic worker-completion timestamp: it creates 576 impossible same-slot
  overlaps totaling 242.959ms, but the maximum is only 3.266ms and none exceeds
  a 4ms callback/log skew allowance. The two shared kernel flocks, 1,270
  measured >=1ms waits and stable slot assignment therefore support the
  physical two-commit bound without pretending callback timestamps are exact
  lock timestamps.
- Capacity failed more severely than both comparison runs. Round 26 -> Round
  29 -> Round 30 sink A/B residence p95 was
  `5.172/4.916 -> 6.122/6.186 -> 11.331/11.204s`; dispatch p95 was
  `5.275/5.188 -> 6.430/6.627 -> 11.551/11.582s`; combined capacity-wait events
  were `55 -> 162 -> 294`; and metadata visibility p95 was
  `3.256 -> 11.859 -> 18.638s`. Ready-to-claim p95 regressed to 11.633s, media
  queue p95 to 25.663s and lifecycle p95 to 24.884s. Exact r300 is not
  authorized.
- The mechanism is explicit lane convoying, not an unenforced bound. Across
  8,203 publications, lock holds totaled 195.485s and lock waits added
  139.866s; 46 holds and 46 waits exceeded one second. Mean/cumulative worker
  service stayed at 41.252ms/338.388s, essentially the rejected Round 29
  service burden, while each long holder delayed its same-lane peer. This
  converted four-way fsync amplification into two longer per-source queues.
  Reproducible results are retained in
  `publication_commit_slots_timing_analysis.json` and
  `publication_commit_slots_concurrency_analysis.json`.
- Phase attribution returns the next single variable to the unarbitrated,
  one-worker Round 26 path. Its metadata and manifest regular-file `fsync`
  calls consumed 80.352s and produced 11 >=1s calls; directory fences remain a
  separate required 47.179s. The next bounded diagnostic will preserve every
  rename and directory `fsync`, but use `fdatasync` for the two regular files so
  file data/size durability remains while unrelated inode metadata need not be
  forced. It must be default-off, test-first and beat Round 26 before exact
  r300 can advance.

### Round 31: regular-file fdatasync implementation

- Tests-only `8333309` first required the historical default to retain the
  exact `fsync(metadata)`, `fsync(manifest)`, `fsync(staging directory)`, atomic
  rename, `fsync(final parent)` order. Explicit `fdatasync` mode must replace
  only the first two regular-file calls, retain both directory calls and every
  later fence, report its effective mode, preserve staging and avoid rename on
  sync failure, reject unknown modes and propagate through all three Compose
  sinks and the pressure profile/artifact surface.
- Implementation `a618796` adds
  `ROLLING_CACHE_PUBLICATION_FILE_SYNC_MODE=fsync|fdatasync`. Daily env, Compose
  and pressure defaults remain `fsync`; only an explicit labeled diagnostic can
  select `fdatasync`. `AtomicSegmentPublisher` still stages invisible files,
  synchronizes both regular files before the staging directory, performs the
  same-filesystem atomic rename, synchronizes the final parent, appends the
  same journal record and invokes the callback only after that sequence.
  `fdatasync` retains data and size durability for the newly-created files;
  directory `fsync` retains both names and rename durability.
- Static validation passed 62 rolling-sink tests and 239 combined pressure,
  analyzer and deployment tests. Python compile, shell syntax, diff checks and
  Compose rendering passed. The tests include regular-file sync order,
  injected sync failure, commit-lane concurrency/error/shutdown history,
  bounded config, applied container env and retained log aggregation.
- Real service-image artifact:
  `/data/video-analytics/artifacts/rolling_publication_fdatasync_smoke_20260722T085418Z`.
  Image `sha256:237f063901d48adfd4265b519d1a40813d94df62717d506fcd197d71d8182bd0`
  bind-mounted the committed app. Two fdatasync fragments completed with two
  fdatasync calls plus the unchanged two directory fsyncs and produced two
  journal records. A historical fsync fragment completed with four fsync calls
  and one journal record. An injected metadata fdatasync `OSError` performed no
  rename, retained its partial staging directory and did not stop the same
  worker from publishing its successor.
- The smoke marker is `PASS_PUBLICATION_FDATASYNC_CONTAINER_SMOKE`.
  Dispatcher state ended 4 submitted, 3 completed, 1 intentionally failed,
  zero outstanding/active/queue/shutdown timeout and preparation limit one.
  This closes only the implementation gate. The next unique runtime variable
  is fdatasync versus Round 26; workers must return to one and commit slots to
  zero so rejected Round 29/30 behaviors are not combined.
- Causal diagnostic artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_pubfdatasync_ioadm3_b6m_r300_20260722T0857Z`.
  It retained the Round 26 fixture/hash, 60 routes, disk-backed r300, Candidate
  B `20/12/8`, finalizer `8/4/8`, index width three, one publication worker per
  sink, zero commit slots, 360-second sample, 25-second postfill and 120-second
  drain. The only intended behavior variable was regular-file `fdatasync`.
- Harness, input and correctness passed: 60/60 sources sustained 8.0466 FPS
  with zero send failure, queue-full, raw drop or raw-send failure. All 922
  formal tasks and all 993 retained tasks materialized. All 638 retained videos
  passed duration, FPS, timeline, annotation, bbox and person-context; all 993
  retained items passed their DB-backed 8090 checks. Person persistence passed
  at 74,116 stored rows versus 74,112 exports with measured loss zero. Expiry,
  duplicate, claim-busy, handoff recovery, retry failure, finalizer failure and
  all task/lease/WIP/lane/finalizer-pending residuals were zero.
- Dispatcher correctness also held. Sink A/B submitted and completed
  `4089/4107` publications with worker/active peak one, fdatasync enabled, zero
  commit slots and clean terminal drain. No failure or shutdown timeout was
  reported.
- Capacity regressed decisively against Round 26. Sink A/B queue-residence p95
  moved from `5.172/4.916s` to `10.697/10.547s`; dispatch p95 moved from
  `5.275/5.188s` to `10.798/10.672s`. Capacity waits at or above one
  millisecond rose from 55 to 233 and cumulative wait rose from 6.215 seconds
  to 27.034 seconds. Ready-to-claim, media queue, lifecycle and metadata
  visibility p95 were `7.260/23.655/22.138/14.787s`; only the lifecycle bound
  passed. Media-worker CPU fell from 126.88% to 105.46%, but that does not
  compensate for the storage and latency regression.
- Phase attribution rejects the inode-metadata hypothesis. Total publication
  worker service was essentially flat at `176.954s -> 177.989s`, while regular
  file sync worsened from `80.352s -> 103.982s`, directory sync worsened from
  `47.179s -> 61.461s`, combined service p95 rose from 31.621ms to 49.584ms and
  services at or above one second rose from 25 to 48. On this ext4 workload
  `fdatasync` still forces device durability barriers; it does not remove or
  smooth the synchronized flush episodes.
- Corrected retained analyses are
  `publication_fdatasync_timing_analysis.json`,
  `publication_fdatasync_phase_analysis.json` and
  `round26_vs_round31_comparison.json` in the artifact. The copied Round 29
  interpretation in the first file was corrected without changing its raw
  measurements. Round 31 is rejected; fdatasync remains diagnostic-only and
  the daily regular-file mode remains `fsync`.

### Round 32: single-inode metadata/manifest implementation

- Durability assessment rejected mount-wide group-sync shortcuts before code.
  Rolling media, PostgreSQL and Docker all live on `/dev/nvme0n1p2` ext4;
  `os.sync()` or a ctypes `syncfs()` wrapper would flush unrelated database and
  container writes and destroy causal isolation. A directory fsync alone does
  not establish regular-file content durability. Neither can replace an
  explicit file fence.
- Tests-only `640538c` freezes a default-off single-inode contract. In
  `single_inode` mode, `metadata.json` begins with one bounded
  `rolling-segment-manifest-v2` control record followed by the exact native
  frame JSONL. `segment_manifest.json` is a hard link to the same immutable
  inode. The index must parse only the bounded first record for discovery,
  exclude it from frame rows, require manifest/metadata identity equality,
  retain legacy v1 split-layout behavior, consume journal records without a
  retained-directory scan, recover the post-rename/pre-journal crash window by
  filesystem reconcile and preserve exact read-pin identity fencing.
- Implementation `fa7731f` adds
  `ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT=split|single_inode`. Daily env,
  Compose and pressure defaults remain `split`; only an explicit labeled
  diagnostic selects `single_inode`. The v2 representation performs one
  metadata-inode `fsync`, then the unchanged staging-directory `fsync`, atomic
  directory rename and final-parent `fsync`. It never uses mount-wide sync.
  File-sync, staging-directory or rename failure remains invisible under
  `.rolling-cache-staging`; journal append remains a post-commit accelerator.
- Static validation passed 201 sink/index/materialization/performance tests and
  241 pressure, analyzer and deployment tests. Python compile, shell syntax,
  Compose rendering and diff checks passed. Default split publications still
  use distinct inodes and two regular-file fences; v1 manifests and existing
  readers remain covered.
- Real dual-image artifact:
  `/data/video-analytics/artifacts/rolling_publication_single_inode_smoke_20260722T094317Z`.
  The rolling-sink image bind-mounted `fa7731f` and completed two v2
  publications plus one legacy split publication. Each v2 publication used one
  regular-file fsync plus the same two directory fsyncs; both historical names
  had the same inode/link count two, exact self-sized v2 header and exact frame
  rows. The split publication retained distinct inodes and two file fsyncs.
- An injected v2 metadata fsync `OSError` performed no rename, retained both
  hard-linked names in its partial staging directory and did not stop the same
  one-worker dispatcher from publishing its successor. Final dispatcher state
  was four submitted, three completed, one injected failure, zero outstanding,
  active, queue or shutdown timeout. The sink marker is
  `PASS_PUBLICATION_SINGLE_INODE_CONTAINER_SMOKE`.
- The real media-worker image consumed both layouts, excluded both v2 control
  records from exact two-row native metadata, published and released a
  two-segment read pin with zero residual, consumed one newly appended v2
  journal record without another manifest parse, and recovered the same two v2
  leaves with zero journal records through manifest-only crash-window
  reconciliation. Both paths reported zero parse error. Its marker is
  `PASS_PUBLICATION_SINGLE_INODE_INDEX_CONTAINER_SMOKE`.
- Two harness-only index attempts are preserved in the artifact. The first
  incorrectly expected source/epoch fields outside the established read-pin
  schema; the second used `shutil.copytree`, which correctly broke hard-link
  identity and was rejected by the production fence. The corrected copy
  explicitly restored the copied hard links before testing reconciliation.
  Neither attempt touched a live service, database, Redis or daily config.
- This closes only the implementation gate. The next sole runtime variable is
  `split -> single_inode` with regular-file mode restored to `fsync`, one
  publication worker and zero commit slots. It must beat Round 26 before exact
  r300 can advance.
- Causal diagnostic artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_pubsingleinode_ioadm3_b6m_r300_20260722T0952Z`.
  It retained the fixed fixture/hash, 60 routes, disk r300, Candidate B
  `20/12/8`, finalizer `8/4/8`, index width three, one publication worker,
  zero commit slots, regular-file `fsync`, 360-second sample, 25-second
  pre/postfill and 120-second drain. Metadata layout was the only behavior
  variable against Round 26.
- Harness, input and retained correctness passed. Both branches observed 60/60
  sources at 8.0375 effective FPS with zero send failure, queue-full or raw
  loss. All 922 formal tasks materialized. All 992 retained 8090 details and
  all 639 retained videos passed the 5+5 duration, raw-FPS, timeline,
  annotation, bbox and person-context checks; 353 retained items were images.
  Person persistence passed at 77,707 stored versus 77,702 exported rows over
  60 sources. Expiry, duplicate, claim-busy, finalizer failure, task, lease,
  WIP, lane and finalizer-pending residuals were zero.
- The intended representation was exercised exactly. Sink A/B each reported
  metadata layout `single_inode`, regular-file sync count one, manifest-file
  sync zero, worker/active peak one, zero commit slots, zero publication
  failure and a clean drain. They completed 4,289/4,322 publications.
- The barrier-count reduction is real but insufficient. Combined regular-file
  sync time fell from Round 26's `80.352s` to `74.568s`. In contrast, the two
  directory sync phases rose from `47.179s` to `98.547s`, total worker service
  rose from `176.954s` to `188.904s`, and services at or above one second rose
  from 25 to 52. The hard-link directory entry and inode link-count update
  therefore shifted journal/flush work into the unchanged directory fences.
- Capacity regressed. Sink A/B residence p95 moved from
  `5.172/4.916s` to `10.107/9.956s`; dispatch p95 moved from
  `5.275/5.188s` to `10.209/10.154s`. Capacity-wait events rose from 55 to
  200 and cumulative wait from 6.215 to 25.836 seconds. Ready-to-claim, media
  queue, lifecycle and metadata visibility p95 were
  `9.745/27.291/24.945/14.251s`; only lifecycle remained inside its closure
  bound. Scheduler-cycle p95 stayed one second, but its 17.649-second maximum
  again split into storage-stalled prepare/claim and handoff persistence.
  Segment-index parse/refresh p95 did not regress, so metadata parsing did not
  absorb the missing time.
- Reproducible analyses are retained as
  `publication_single_inode_timing_analysis.json`,
  `publication_single_inode_phase_analysis.json` and
  `round26_vs_round32_comparison.json` in the artifact. Round 32 is rejected;
  exact 600-second r300, r3840 and both one-hour runs remain blocked.
- Cleanup exposed and closed a harness-only restoration defect: stopped sink
  containers retained the pressure layout even though daily Compose rendered
  `split`. `2e5c173` captures the failure and `da35730` now recreates only the
  affected services from daily Compose with `--no-deps`, preserves original
  running/stopped state and fails on a state mismatch. The current stopped A/B
  sink containers were recreated with split/fsync/one-worker/zero-slot/r300.
- The next test-first single variable is a metadata-only v3 representation:
  retain the bounded first manifest record plus exact native rows in
  `metadata.json`, but create no `segment_manifest.json` hard-link alias.
  Legacy split and v2 hard-link readers/recovery remain supported. One
  regular-file fsync, staging-directory fsync, atomic directory rename,
  final-parent fsync, journal/reconcile authority, failure staging, FIFO bound
  and daily split default remain unchanged.

### Round 33: metadata-only v3 implementation and causal rejection

- Tests-only `22aa940` freezes the alias-free contract. `metadata.json` begins
  with one bounded `rolling-segment-manifest-v3` record followed by the exact
  native JSONL rows; `segment_manifest.json` must not exist. Publication still
  performs one regular-file sync, staging-directory sync, atomic directory
  rename and final-parent sync. Sync failure remains staged and invisible.
- Implementation `f075b46` adds
  `ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT=metadata_only` while keeping the
  daily/Compose/profile default `split`. The journal retains equal manifest/
  metadata identities for v3, but Media Worker keys the catalog by
  `metadata.json`. Initial rebuild, periodic membership reconciliation and
  fallback discovery recognize only a bounded v3 first record when no alias
  exists. v1 split and v2 hard-link entries retain their historical keys and
  identity rules; both v2/v3 control records are excluded from native rows.
- Static validation passed 191 sink/index/materialization/performance tests,
  243 pressure/analyzer/deployment tests, and 47 additional Spec 33 lifecycle,
  static-index, DB-index and finalizer tests with one expected skip. The focused
  sink/index/pressure selection passed 329 tests. Compile, shell syntax,
  Compose rendering and diff checks passed; the local Python environment has no
  Ruff module, so no Ruff result is claimed.
- Real dual-image artifact:
  `/data/video-analytics/artifacts/rolling_publication_metadata_only_smoke_20260722T104522Z`.
  Sink image
  `sha256:237f063901d48adfd4265b519d1a40813d94df62717d506fcd197d71d8182bd0`
  proved the exact v3 one-file/two-directory fence order, no alias, v1/v2
  compatibility, five journaled successes, one injected metadata-sync failure
  retained under staging, and a successful same-publisher successor. Marker:
  `PASS_PUBLICATION_METADATA_ONLY_SINK_CONTAINER_SMOKE`.
- Media Worker image
  `sha256:5b451db0c4859a01471d2d05a183b3cc81f6109712e12592e42535162afa5c84`
  consumed the mixed catalog, discovered a later v3 record journal-only while
  retained-leaf enumeration was forbidden, recovered an unjournaled
  post-rename crash-window v3 leaf through periodic membership reconciliation,
  found all seven leaves through fallback, preserved metadata catalog keys and
  exact frame rows, and rejected a changed v3 identity at read-pin publication
  with zero marker residue. Marker:
  `PASS_PUBLICATION_METADATA_ONLY_INDEX_CONTAINER_SMOKE`.
- Causal diagnostic artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_pubmetadataonly_ioadm3_b6m_r300_20260722T1055Z`.
  It retained the fixed 4,800-second fixture/hash, 60 routes, disk r300,
  Candidate B `20/12/8`, finalizer `8/4/8`, index width three, one publication
  worker, zero commit slots, regular-file `fsync`, 360-second sample, 25-second
  pre/postfill and 120-second drain. The sole intended behavior variable
  against Round 26 was `split -> metadata_only`; this remains a causal
  diagnostic, not the exact 600-second r300 acceptance.
- Harness, input and correctness passed. Both branches reached 60/60 at 8.0822
  effective FPS with zero Savant-send, queue-full, raw-drop or raw-send failure.
  All 927 formal tasks and all 993 retained tasks materialized. The retained
  set contains 638 videos and 355 images; all 638 videos passed 5+5 duration,
  raw FPS, DB-backed timeline, annotation, bbox and person-context checks, and
  all 993 items passed 8090 detail checks. Person persistence passed at 73,849
  stored rows versus 73,844 exports over all 60 sources with measured loss
  zero. Task/lease/Replay/WIP/lane/finalizer-pending residuals were zero. The
  only warning was the expected `validate_seq_iq` sampling gap.
- The intended representation was exercised exactly. Sink A/B completed
  4,079/4,091 publications with metadata layout `metadata_only`, one
  regular-file sync, zero manifest write/sync, one worker, zero commit slots,
  zero publication failure and clean terminal drain.
- The representation saving is real but does not pass the causal gate.
  Regular-file sync falls from Round 26's `80.352s` to `57.439s`, combined
  worker service falls `176.954s -> 142.397s`, and service p95 falls
  `31.621ms -> 16.904ms`. The two required directory sync phases nevertheless
  consume `70.838s` versus Round 26's `47.179s`, and >=1-second services rise
  `25 -> 42`. Removing the hard-link alias improves Round 32, but rare ext4
  directory/writeback convoys remain.
- Capacity therefore fails. Sink A/B residence p95 changes from
  `5.172/4.916s` to `6.739/6.746s`; dispatch p95 changes from
  `5.275/5.188s` to `7.162/7.325s`. Both queues still reach 128 outstanding.
  Capacity-wait events at or above one millisecond rise `55 -> 165`, cumulative
  callback wait rises `6.215s -> 19.444s`, and metadata visibility changes
  `3.256s -> 12.040s`. Ready-to-claim/media-queue/lifecycle p95 is
  `6.225/21.789/21.055s`; the first two still miss closure.
- The missing time did not move into v3 parsing or headline scheduler/DB work.
  Segment-index full-row/manifest parse p95 improves
  `19.451/3.384ms -> 15.159/1.492ms`; scheduler poll p95/max improves
  `63ms/14.327s -> 59ms/12.156s`; DB claim p95 improves
  `84.1ms -> 66.15ms`, with zero checkout timeout. Dispatch remains correlated
  0.9988/0.9987 with residence and only about 0.20/0.19 with own service. The
  failed directory-shift, callback, FIFO and visibility gates retain the rare
  durability-convoy attribution.
- Reproducible analyses are retained as
  `publication_metadata_only_timing_analysis.json`,
  `publication_metadata_only_phase_analysis.json`,
  `round26_vs_round33_comparison.json` and
  `analyze_metadata_only_comparison.py` in the artifact. Round 33 is rejected;
  `metadata_only` remains default-off and does not authorize exact r300,
  r3840 or either one-hour run.
- Cleanup exposed a second restoration boundary missed by `da35730`: launcher
  shell values outrank `--env-file`, so stopped A/B containers were recreated
  in `metadata_only` even though daily `midterm.env` says `split`. Tests-only
  `af29c21` reproduces the leak; `3e35f16` removes all nine pressure-controlled
  sink interpolation keys before daily Compose and validates the recreated
  env, not only stopped/running state. Real proof
  `compose_restore_rolling_cache_sinks_after_fix.json` deliberately injected
  all nine hostile values and restored both stopped sinks to
  split/fsync/one-worker/zero-slot/r300 with empty pressure epoch and zero
  mismatch.

### Round 34: final-parent cohort commit is causally rejected

- Tests-only `008fe10` freezes a different durability shape from rejected
  Round 27. Every item must finish both regular-file fences and its
  staging-directory fence before the next item is touched. Implementation
  `09e9cc8` then renames the natural cohort in exact FIFO order, explicitly
  fences every distinct final parent, appends journals, and releases callbacks
  in FIFO only after the complete cohort fence. The default remains one;
  values above one reject preparation grouping, multiple publication workers
  and commit slots. The selected diagnostic limit was eight.
- Focused rolling-sink plus pressure/deployment validation passed 326 tests,
  the analyzer selection passed eight, and the broader segment-index,
  materialization, finalizer and lifecycle selection passed 204 with eight
  expected skips. Python compilation, shell syntax and profiled Compose
  rendering passed.
- Real-image artifact:
  `/data/video-analytics/artifacts/rolling_publication_final_parent_group_smoke_20260722T121401Z`.
  Marker `PASS_PUBLICATION_FINAL_PARENT_GROUP_CONTAINER_SMOKE` proves
  multi-parent fencing, FIFO callback release, retained staging after a middle
  rename failure, successor progress, parent-fsync failure isolation and
  shutdown drain.
- Causal diagnostic artifact:
  `/data/video-analytics/artifacts/pressure60_8p1_pubparentgrp8_ioadm3_b6m_r300_20260722T1222Z`.
  It retained the fixed 4,800-second fixture/hash, 60 routes, disk r300,
  Candidate B `20/12/8`, finalizer `8/4/8`, index width three, split metadata,
  regular-file `fsync`, one publisher, preparation limit one, zero commit slots,
  a 360-second sample, 25-second pre/postfill and 120-second drain. The sole
  intended behavior variable against Round 26 was final-parent group limit
  `1 -> 8`; this is not the exact 600-second r300 acceptance.
- Harness, input and correctness passed. All 60 inputs reached both branches
  without restart, send failure, queue-full sample or raw loss. All 921 formal
  tasks and all 994 retained tasks materialized. The retained set contains 641
  videos and 353 images; every video passed 5+5 duration/raw-FPS, timeline,
  annotation, bbox and person-context checks, and all retained items passed the
  8090 gates. Task, lease, Replay, WIP, lane and finalizer-pending residuals
  were zero. The only warning was the expected `validate_seq_iq` sampling gap.
- The mechanism was exercised but was normally idle. Sink A/B completed
  `4,255/4,260` publications in 7,522 natural cohorts: 7,366 singletons, 135
  full groups of eight, and 21 partial groups. FIFO position validation passed.
  The 8,515 renames required 8,511 distinct-parent fences; four repeated-parent
  fences were legitimately coalesced, and every distinct parent was explicitly
  fenced. No durability fence was omitted.
- Required file service regressed. Round 26 -> Round 34 regular-file fsync was
  `80.352s -> 104.693s`, or `9.782ms -> 12.295ms` per publication. The two
  directory fences were `47.179s -> 51.175s`, or
  `5.744ms -> 6.010ms` per publication. Own commit-service p95 improved
  `31.621ms -> 15.974ms`, but services at or above one second rose `25 -> 42`.
  After shared cohort wait is counted once rather than once per callback, the
  cohort busy estimate is `210.611s`, 1.148 times Round 26 per publication;
  16 cohorts exceeded one second and five exceeded ten seconds.
- Capacity therefore fails the frozen Round 26 comparison. Sink A/B residence
  p95 changes `5.172/4.916s -> 9.140/9.502s`; dispatch p95 changes
  `5.275/5.188s -> 10.292/9.929s`. Both queues still reach 128 outstanding.
  Capacity waits at or above one millisecond fall `55 -> 42`, but cumulative
  capacity wait rises `6.215s -> 35.691s`; the few largest waits are
  `12.319/11.535s`. Callback cohort-wait p95 is 28.749ms and its member-weighted
  total is 330.624s. Metadata visibility improves `3.256s -> 2.096s`, which is
  still above the absolute 2-second closure gate.
- Downstream work did not absorb the missing time. Ready-to-claim,
  media-queue and lifecycle p95 are `7.817/25.369/22.591s`; the first two miss
  closure. Segment-index full-row/manifest parse p95 improves
  `19.451/3.384ms -> 14.003/2.305ms`; scheduler poll p95/max improves
  `63ms/14.327s -> 53.25ms/12.251s`; DB claim p95 improves
  `84.1ms -> 68ms`, with zero checkout timeout.
- The decisive event is a synchronized storage-wide convoy at
  `2026-07-22 12:31:28Z`. Sink A/B each spent about 16.53/16.59 seconds on an
  eight-item cohort while the members' own regular-file and staging fences had
  already accumulated most of that service. Final-parent grouping begins only
  after backlog exists and does not remove those fences; it packages the
  resulting queue and adds callback fence wait rather than preventing the
  convoy. Reducing or enlarging the cohort limit would tune this failed
  mechanism, not address the causal service tail.
- Reproducible analyses are retained as
  `publication_final_parent_group_timing_analysis.json`,
  `publication_final_parent_group_phase_analysis.json`,
  `round26_vs_round34_comparison.json` and
  `analyze_final_parent_group_comparison.py` in the artifact. Round 34 is
  rejected; group mode remains default-off with daily limit one. Exact r300,
  r3840 and both one-hour runs remain blocked.

## Runtime recovery audit

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

The post-Round-27 audit reconfirmed the same daily state: 0/60 enabled cameras,
no active tasks, leases or finalizer-pending rows, daily single branch running,
dual branches and rolling sinks stopped, rolling materialization disabled,
r300, index width two, media WIP/remux/finalizer `4/4/32`, process finalizers
zero, Redis/PostgreSQL defaults restored, no pressure/source/MediaMTX/ffmpeg
processes, and about 199GB free.

The post-Round-28 audit again confirmed zero enabled cameras, active tasks,
leases and finalizer-pending rows; only the daily single branch was running.
Dual branches, rolling sinks, pressure sources, MediaMTX and pressure ffmpeg
were stopped. Rolling materialization was disabled with r300 and index width
two; media WIP/remux/finalizer returned to `4/4/32`, process finalizers to zero,
and Redis/PostgreSQL defaults were restored. About 197GB remained free.

The post-Round-29 implementation audit still reports 0/60 enabled cameras and
`drain_complete=true`: zero active evidence tasks, Replay slots, record-request
pending/lag and pressure containers. The daily media-worker remains at WIP
four, finalizer threads 32, process finalizers zero, segment-index width two,
rolling materialization disabled and r300. Redis `save` remains
`3600 1 300 100 60 10000`; 197GB is free. The ephemeral container smoke was
removed automatically and did not start or alter the live rolling sinks.

The post-Round-29 diagnostic cleanup again reports `drain_complete=true`,
0/60 enabled cameras, zero active evidence task, Replay slot,
record-request lag/pending and pressure/source/MediaMTX container. The daily
media-worker returned to WIP four, finalizer threads 32, process finalizers
zero, segment-index width two, rolling materialization disabled and r300.
Redis `save` and PostgreSQL checkpoint settings were restored; 195GB remains
free and the worktree is clean. All failed-run artifacts were preserved.

The post-Round-30 diagnostic cleanup reports 0/60 enabled cameras and zero
active task, lease or finalizer-pending row. No pressure source, local
MediaMTX, pressure ffmpeg, dual Savant or rolling-sink container/process
remains. The daily media-worker is back at WIP/remux/finalizer `4/4/32`, process
finalizers zero, segment-index width two, rolling materialization disabled,
r300, publication workers one and commit slots zero. Redis `save` is
`3600 1 300 100 60 10000`; PostgreSQL is back at checkpoint timeout 5min,
WAL `1024/80MB` and compression off. The worktree is clean and about 192GB is
free. The passed-correctness/failed-capacity artifact remains intact.

The post-Round-31 diagnostic cleanup again reports 0/60 enabled pressure
cameras and zero active task, lease or finalizer-pending row. No live pressure
source, local MediaMTX, pressure ffmpeg, dual Savant or rolling-sink process/
container remains; the two Compose rolling-sink containers are stopped. The
daily media-worker is back at WIP/remux-queue/finalizer `4/4/32`, rolling remux
worker one, process finalizers zero, segment-index width two, rolling
materialization disabled and r300. Publication workers are one, commit slots
zero and regular-file mode is `fsync`. Redis `save` is
`3600 1 300 100 60 10000`; PostgreSQL is back at checkpoint timeout 5min, WAL
`1GB/80MB` and compression off. The worktree was clean before this ledger
update, the completion status file is absent and about 190GB is free.

The post-Round-32 dual-image smoke audit made no live-runtime mutation and
reconfirmed 0/60 enabled pressure cameras, zero active task/lease/
finalizer-pending row, no live pressure/MediaMTX/ffmpeg/rolling-sink process or
container and an absent completion status file. The daily media-worker remains
at WIP/remux-queue/finalizer `4/4/32`, process finalizers zero, index width two,
rolling materialization disabled, r300, publication workers one, commit slots
zero and file mode `fsync`; the not-yet-recreated daily containers therefore
continue the historical split layout by default. Redis `save` remains
`3600 1 300 100 60 10000`.

The post-Round-32 pressure audit reports 0/60 enabled pressure cameras and zero
active task, lease, Replay slot or finalizer-pending row. No pressure source,
local MediaMTX, pressure ffmpeg, dual Savant or rolling-sink process remains;
the daily single branch is running. Media-worker is back at WIP/remux/finalizer
`4/4/32`, process finalizers zero, index width two, rolling materialization
disabled and r300. The stopped A/B sink containers now render publication
workers one, commit slots zero, file mode `fsync` and metadata layout `split`.
Redis `save` is `3600 1 300 100 60 10000`; PostgreSQL is back at checkpoint
timeout 5min, WAL `1GB/80MB` and compression off. The completion status file is
absent, all pressure artifacts are retained and about 184GB is free.

The post-Round-33 audit first caught the launcher-env restoration leak above,
then reran the corrected restore under deliberately hostile pressure values.
At 2026-07-22T11:30:26Z the stopped A/B sink containers were freshly recreated
with split/fsync/one-worker/zero-slot/r300, 24 FPS and empty pressure epoch.
The daily media-worker is running with restart count zero at
WIP/remux-queue/finalizer `4/4/32`, process finalizers zero, index width two and
rolling materialization disabled. The database has 0/60 enabled pressure
cameras and zero active task, lease, Replay slot or finalizer-pending row; no
pressure/MediaMTX/fixture publisher container or process remains. Redis `save`
is `3600 1 300 100 60 10000`; PostgreSQL is `5min/1GB/80MB/off`; about 186GB
is free and the completion status file remains absent. The legacy runtime
doctor still reports `ok=false` for its pre-existing expectation drift
(`MAX_FPS_CONTROL`, pose threshold, replay-config surface and absent optional
source-adapter), not for pressure cleanup or durability restoration.

The fresh post-Round-34 audit at 2026-07-22T12:50:39Z reports 0/60 enabled
cameras, zero active evidence task, lease, Replay slot or finalizer-pending row,
and zero Redis record-request pending/lag. No pressure harness, fixture ffmpeg,
local MediaMTX or pressure source process remains. The daily single branch is
running; dual Savant/fanout branches and both rolling sinks are stopped. Both
stopped sinks were freshly restored to split/fsync/one-worker/zero-slot/r300
with final-parent group limit one. Daily Media Worker is at WIP/remux/finalizer
`4/4/32`, process finalizers zero, index width two, rolling materialization
disabled and restart count zero. Redis `save` is
`3600 1 300 100 60 10000`; PostgreSQL is `5min/1GB/80MB/off`. The completion
status file is absent, the worktree was clean before this ledger update, and
about 183GB remains free.

## Next gates

1. Keep production/default preparation at one, commit arbitration/slots
   disabled and publication workers at one. Retain grouping, one/two-slot
   arbitration and two-worker sharding only for historical reproduction; none
   of the rejected Round 27-30 modes may become a daily default.
2. Keep regular-file `fdatasync` diagnostic-only and daily/default mode
   `fsync`. Round 31 passed correctness but failed every causal capacity
   comparison; it must not advance exact r300.
3. Do not use `os.sync()`, a ctypes `syncfs()` wrapper or a directory-only fsync
   as an apparent group commit. Rolling media, PostgreSQL and Docker all share
   the same ext4 mount, so filesystem-wide sync would flush unrelated database
   and container writes; directory fsync alone does not durably order regular
   file contents. Neither has an acceptable isolated crash contract here.
4. Retain Round 32 as negative causal evidence. It passed correctness and
   reduced regular-file sync time, but failed residence, dispatch,
   capacity-wait, visibility and no-directory-shift gates. `single_inode` must
   remain diagnostic-only and cannot advance exact r300.
5. Retain Round 33 as negative causal evidence. Metadata-only v3 passes static,
   dual-image and pressure correctness and lowers file-sync/total service, but
   fails directory-shift, residence, dispatch, capacity-wait and visibility
   gates. It remains default-off and cannot become a daily layout.
6. Retain Round 34 as negative causal evidence. Final-parent grouping preserves
   every durability boundary and improves visibility plus capacity-wait event
   count, but worsens file/directory service, residence, dispatch and cumulative
   capacity wait. Keep its daily limit at one; do not tune group size as a new
   capacity claim.
7. Keep the `af29c21`/`3e35f16` restore fence: diagnostic launcher environment
   must not outrank daily Compose during cleanup, and recreation must fail on
   any of the nine sink env mismatches. Stopped state alone is not restoration
   proof.
8. Do not select another behavior variable merely because Round 34 is negative.
   First explain the remaining rare directory/writeback convoy and freeze one
   falsifiable, test-first mechanism. Do not remove either directory fence or
   combine worker, queue, retention, index, finalizer or deadline changes.
9. Keep exact r300, r3840 and both one-hour acceptances blocked. Rounds 27-34
   are negative 360-second causal evidence and do not replace the exact
   width-three 600-second r300 gate.
10. Repeat exact r300 with the fixed fixture/hash only after a future unchanged
   short diagnostic beats Round 26 on file/directory service, both residence/
   dispatch p95 values, fewer-than-55 capacity waits and 3.256-second
   visibility without shifting work into callback, parsing or scheduler/DB.
11. Only a complete exact-r300 input/capacity/visibility/watchlist/annotation/
    residual pass permits r3840. Only after both short gates pass may two
    comparable one-hour acceptances and Phase 7 legacy removal be considered.
