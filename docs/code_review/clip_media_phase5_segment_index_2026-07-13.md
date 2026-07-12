# Clip / Media Phase 5 Rolling Segment Index

Date: 2026-07-13

Scope: Spec 33 Phase 5, executed as the `RollingSegmentIndex` substep of Spec 34
Phase 4. This checkpoint does not claim Spec 33 Phase 6 capacity/readiness
calibration, either required 60-source closure run, Spec 34 Phase 5 media job
boundary extraction, the full cross-worker soak, or legacy path removal.

## 1. Result

Media Worker now uses one process-lifetime, source/epoch-fenced rolling segment
catalog shared by rolling video and image jobs. Repeated work for the same
source no longer recursively scans and reparses every `metadata.json`. Parsed
rows are held in a bounded identity-keyed LRU, active remux/image reads create
bounded filesystem pins, and rolling retention is owned by one lock-arbitrated
maintenance process.

The remux result carries one immutable duration probe into finalization. The
finalizer reuses it only while device, inode, size, and mtime still match; a
changed file is probed again. Evidence DB indexing now measures bundle,
artifact, timeline, and overlay subphases separately.

Acceptance token:

```text
PASS_MEDIA_WORKER_INCREMENTAL_SEGMENT_AND_FINALIZER_PATH
```

No `services/evidence-viewer` file is part of this phase. Savant models,
thresholds, batch sizes, FPS, 5+5 policy, frame UUID/PTS/session contracts,
evidence type policy, and 8090 product semantics were not changed. No evidence,
trajectory, thumbnail, canary, observation, Redis entry, or rolling segment was
deleted.

## 2. Process-Lifetime Segment Catalog

`RollingSegmentIndex` is keyed by `(source_id, runtime_epoch_id)`. A catalog
performs one bounded initial source walk. Later lookups use directory mtimes and
known file identities to inspect changed paths; a periodic bounded reconcile
and the maintenance generation file remove stale entries after retention.

The index rejects empty, too-small, unstable, malformed, and cross-epoch
segments. An unchanged malformed identity is remembered and is not reparsed on
every task. Catalog count, scan depth/entries, and parsed-row cache are bounded.
The compatibility recursive scan remains available only as an observable
exception fallback and increments `segment_index_fallback_scans`. Exact source
roots are enforced even in that fallback, so a prefix neighbor such as
`camera-010` cannot enter the `camera-01` catalog.

Both remux and image lookup consume `rows_for_segment()`. The cache key includes
the metadata path and complete file identity, so replacing or modifying a
metadata file cannot return stale parsed rows. Runtime metrics expose hits,
misses, refreshes, reconciliations, parses, stale entries, fallback scans,
cache entries/evictions, read pins, and aggregate generation.

## 3. Read Pins And Single-Owner Maintenance

Before reading selected segment inputs, each job atomically creates a
`.read-pins/{token}.json` marker under a shared mutation lock. The marker holds
only relative segment directories plus PID/thread/timestamps and a bounded TTL;
it contains no frame or video payload. Normal completion removes it. A SIGKILL
marker expires and is removed by maintenance after its TTL.

The old cleanup loop embedded in each rolling sink entrypoint was replaced by
`rolling_cache_maintenance.py`. Single-sink and dual-A request ownership;
dual-B is standby. An exclusive filesystem owner lock is the final arbiter, so
misconfigured concurrent owners cannot both delete. The maintenance pass:

- takes the exclusive mutation lock before reading pins or deleting;
- honors active pins and conservatively blocks on a fresh corrupt marker;
- applies retention and an implemented oldest-first byte quota;
- removes complete segment directories, not independent half-files; and
- advances `.rolling-cache-generation` after a real mutation.

Effective retention, quota, interval, pin TTL, stability age, and owner role are
visible in Compose and startup/maintenance logs. A no-delete runtime dry-run
scanned 21 segments and 103867765 bytes with zero active/corrupt/expired pins
and zero deleted bytes.

## 4. Probe And DB Index Work Reduction

Rolling materialization stores a `rolling-cache-immutable-probe-v1` record with
duration and `{device,inode,size,mtime_ns}`. The durable handoff carries that
record. Sink readiness and finalization reuse the duration only when the source
file still has the same identity. General Replay output records one
`finalizer-authoritative-probe-v1` result after its necessary readiness probe;
later finalizer checks share it instead of probing the same immutable file
again.

An expanded regression fixture was updated to carry a matching immutable probe
and now raises if any redundant probe occurs. Separate tests prove that size or
mtime replacement invalidates the carried duration and requires a fresh probe.

`upsert_evidence_bundle_index()` reports:

```text
sidecar_build_ms
db_bundle_index_ms
db_artifact_index_ms
db_timeline_index_ms
db_overlay_index_ms
db_index_total_ms
```

Expanded timeline/overlay correctness remains the default. No DB batching or
row suppression was introduced because the retained canary sample was already
small: bundle `1ms`, artifact `0ms`, timeline `4ms`, overlay `1ms`, total `8ms`.
Sidecar build time excludes annotation-anchor sleep and is forwarded separately
into `media_worker_perf` and the DB-index completion log.

## 5. Retained Canary And 8090 Proof

Artifact:

```text
/data/video-analytics/artifacts/clip_media_phase5_segment_index_canary_20260712T175842Z
```

The retained results are:

```text
.510 rolling video: 1 bundle, 1 artifact, 82 timeline, 6 displayable overlays
.511 rolling image: 1 bundle, 3 image artifacts
.512 same-source video: 1 bundle, 1 artifact, 82 timeline, 6 overlays
```

The first two source catalogs produced `misses=2`, `metadata_parses=2`.
Processing `.512` reused `.510`'s source catalog: `hits=1` while parses remained
2. Three read pins were created and released, active pins returned to zero,
and fallback scans remained zero. The two video summaries report a ready 10.0s
immutable probe and `ffprobe_invocations=0`.

The `.512` fixture wrapper now explicitly reuses the six existing accepted
person observations for its shared source. `prepare-reuse` is idempotent and
exits successfully for the retained terminal event instead of attempting the
duplicate observation inserts that previously raised `UniqueViolation`.

PostgreSQL currently reports all three tasks as
`materialized/materialized/terminal`, one unique bundle each, no lease, and no
Replay slot. Global active Media leases, `finalizer_pending` tasks, and active
Replay slots are all zero.

8090 returned DB-backed annotations for `.510` and `.512`: six displayable
person bbox/person-context records each, `fallback_used=false`. Both MOV Range
requests returned HTTP 206. The `.511` face crop, full frame, and annotated
frame returned HTTP 200 with nonzero JPEG bodies.

## 6. Verification And Runtime Restoration

Executed checks:

```text
exact Phase 5 focused set:             146 passed, 1 skipped
Phase 4/5 + Spec 33 minimum regression: 345 passed, 1 skipped
compileall / py_compile:               passed
rolling sink shell syntax:             passed
docker compose effective config:       passed
pressure60 parser/artifact analyzer:   passed in the 339-test set
Viewer JavaScript syntax:              passed
git diff --check:                      passed
retained DB/8090/media audit:           passed
global DB/Redis/runtime residuals:      zero
```

The change is pure Python/shell/Compose with existing bind mounts and no
Dockerfile, dependency, or base-image change. Media Worker was recreated with
`--no-build`; the image digest remained
`sha256:5b451db0c4859a01471d2d05a183b3cc81f6109712e12592e42535162afa5c84`.

Daily runtime is restored with rolling cache and rolling materialization off,
while Scheduler V2, DB pool, and Segment Index are effective. Current image,
remux, and finalizer lane depths, shared permits, DB checkouts, heartbeat
registrations, and read pins are zero. Existing AdaFace orphan containers were
left untouched.

## 7. Explicit Remaining Work

Spec 33 Phase 6 must run comparable `max_active=4/8/12` capacity tests and only
then calibrate readiness/grace. Two comparable accepted 60-source runs and the
final restart/recovery closure remain required before Phase 7 can delete the
legacy scheduler and temporary flags.

Spec 34 Phase 4 is therefore still only partially complete. Its capacity and
performance gates remain open. Spec 34 Phase 5 responsibility extraction has
not started, and neither `PASS_MEDIA_WORKER_JOB_BOUNDARIES_PARITY` nor the final
Spec 33/34 readiness tokens are claimed here.
