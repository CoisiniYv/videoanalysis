# R3.3A Replay UUID Feasibility Inspection

Status: **inspection only**. No Savant pipeline change. No implementation. No commit.

Date: 2026-05-30

## Executive Summary

The Savant Replay UUID approach (event carries `keyframe_uuid` -> Replay archive
-> offset -> exact clip) is architecturally sound but **not feasible today**
without changes. The project has the Replay client code and compose skeletons
from Phase 3 POC, but the current mainline runtime does not deploy Replay, and
the Savant pipeline does not expose frame UUIDs.

## Direct Answers

| Question | Answer |
|----------|--------|
| Claude-described Replay UUID 方案和当前实现是否一致 | **NO** — 设计方向一致，但当前实现无任何 UUID 生产路径 |
| 当前是否有 frame_uuid | 字段存在于 SecurityEvent / events 表，但生产代码始终 `None` |
| 当前是否有 keyframe_uuid | 字段存在于 SecurityEvent / events 表，但生产代码始终 `None` |
| 当前是否有 Replay service | **POC only** — phase3a/phase3b compose 有定义，c1-official-adapter 无 |
| 当前是否能用 UUID anchor 创建 clip | **NO** — 无 UUID 来源，无运行中 Replay 服务 |
| 最小改动是什么 | 见 §7 Minimal Changes |
| 是否仍需 controlled segment recording fallback | **YES** — 见 §8 Recommendation |

## 1. Replay Service Status

### 1.1 Compose Definitions

| File | Replay Service | Status |
|------|---------------|--------|
| `infra/docker-compose.phase3a.yml` | `phase3a-replay-service` (port 8080) | Legacy POC |
| `infra/docker-compose.phase3b.yml` | `phase3b-replay-service` (port 8081) | Legacy POC, banned by compose_inventory |
| `infra/docker-compose.c1-official-adapter.yml` | **none** | Current mainline — no Replay |
| `infra/docker-compose.dev.yml` | **none** | Future dev — no Replay |

The POC compose files use `ghcr.io/insight-platform/savant-replay-x86:v0.6.0`.

### 1.2 Replay Config

`modules/savant_replay/config.json` defines:
- ZMQ ingress: `router+bind:tcp://0.0.0.0:5555`
- RocksDB storage: `/opt/rocksdb`, 60-second TTL
- Management REST API: port 8080

This config is **not mounted** in the current mainline compose.

### 1.3 Verdict

Replay service exists as a POC artifact but is **not deployed** in the current
mainline runtime. Enabling it requires:
1. Adding the replay-service container to c1-official-adapter compose.
2. Adding a source-adapter that feeds ZMQ frames to Replay (separate from Savant).
3. Or adopting single-ingestion topology where Replay sits between source-adapter
   and Savant (CLAUDE.md §9.2 target topology).

## 2. Frame UUID / Keyframe UUID Status

### 2.1 Schema — Present

`SecurityEvent` dataclass (`modules/savant_security/custom/models/events.py:89-90`):
```python
frame_uuid: Optional[str] = None
keyframe_uuid: Optional[str] = None
```

Database (`db/migrations/002_phase2e_events.sql:25-26`): nullable `TEXT` columns.

API schema (`services/api/app/schemas/events.py:60-61`): `Optional[str] = None`.

### 2.2 Production Code — Always None

Every event producer hardcodes both to `None`:

| File | Lines | Producer |
|------|-------|----------|
| `modules/savant_security/custom/pyfuncs/behavior_rules.py` | 181-182, 209-210 | Behavior rules |
| `modules/savant_phase2c/custom/pyfuncs/behavior_event_export_probe.py` | 242-243, 271-272 | Phase 2C probe |
| `modules/savant_phase3h_zmq/custom/pyfuncs/behavior_event_export_probe.py` | 242-243, 271-272 | Phase 3H probe |
| `services/face-worker/app/face_match_event_service.py` | 192-193 | Face match |

Downstream consumers (event-worker, record_request.py, repository.py) pass
`None` through without modification.

### 2.3 Savant Frame Meta — No UUID Available

`NvDsFrameMeta` properties exposed to pyfuncs:
- `source_id` (str)
- `frame_num` (int)
- `batch_id` (int)
- `pts` (int) — pipeline-relative presentation timestamp
- `duration` (Optional[int])
- `framerate` (str)
- `time_base` (Tuple[int, int])
- `objects` (iterator)
- `roi` (BBox)

**No `frame_uuid`, `keyframe_uuid`, or any UUID-like property exists.**

`savant_rs.primitives.VideoFrame` constructor accepts: `source_id`, `framerate`,
`width`, `height`, `pts`, `keyframe` (bool), `codec`, `dts`, `duration`,
`time_base`. No UUID parameter.

### 2.4 NDJSON Metadata — No UUID

Metadata sink output at `/data/video-analytics/media/c1-official-metadata/`
contains per-frame: `source_id`, `framerate`, `width`, `height`, `pts`,
`keyframe` (boolean, not UUID), `codec`, `dts`, `duration`, `metadata`,
`frame_num`. No `frame_uuid` or `keyframe_uuid` field.

### 2.5 Verdict

UUID fields are **forward-compatible placeholders** in the schema. No production
code path populates them. Savant does not expose frame UUIDs to pyfuncs.

## 3. ReplayClient Capabilities

`services/clip-worker/app/replay_client.py` implements:

| Method | API | Capability |
|--------|-----|-----------|
| `status()` | `GET /api/v1/status` | Health check |
| `find_keyframe(source_id, ts_ms, window_s)` | `POST /api/v1/keyframes/find` | Keyframe lookup |
| `create_job(source_id, keyframe_uuid, pre_seconds, post_seconds, sink_endpoint, labels)` | `PUT /api/v1/job` | Replay job creation |

### 3.1 find_keyframe — Timestamp Domain Problem

The method accepts `ts_ms` (epoch milliseconds) but the code **explicitly
disables** timestamp-anchored search (lines 68-72):

```python
# NOTE: from_ns/to_ns are epoch nanoseconds; Replay DB uses
# pipeline-relative timestamps. Unbounded search (omit from/to)
# until timestamp-domain mapping is established.
from_ns = None
to_ns = None
```

This means `find_keyframe` always does an **unbounded search** (`limit: 1`),
returning the most recent keyframe for the source — not the one nearest to the
event timestamp.

### 3.2 create_job — UUID Anchor Supported

The job payload includes:
- `anchor_keyframe`: keyframe UUID string
- `offset`: `{"seconds": pre_seconds}` — offset before anchor
- `stop_condition`: `{"frame_count": total_frames}` — frame count stop

This API design **does support** the UUID anchor pattern. If a valid
`keyframe_uuid` were provided, the job would replay from that anchor with the
specified offset.

### 3.3 Verdict

ReplayClient API supports UUID anchor + offset + stop condition. The blocking
issue is that no valid `keyframe_uuid` is ever available, and timestamp-domain
mapping is unresolved.

## 4. Clip-Worker Integration

`services/clip-worker/app/worker.py` already implements the full flow:

1. Read `record_request` from Redis `security.record_requests`.
2. If `keyframe_uuid` provided directly → use it.
3. Else → `replay.find_keyframe(source_id, event_ts_ms)` (unbounded).
4. `replay.create_job(source_id, keyframe_uuid, pre_seconds, post_seconds, ...)`.
5. Update `clip_status` in PostgreSQL.

The plumbing is complete. The gap is the UUID source.

## 5. Gap Analysis

```
Replay UUID Chain:

  [A] Savant frame_meta exposes frame UUID
    -> NOT AVAILABLE (frame_meta has no UUID property)

  [B] BehaviorRulesPyFunc reads UUID from frame_meta
    -> CANNOT WORK (nothing to read)

  [C] SecurityEvent carries frame_uuid / keyframe_uuid
    -> SCHEMA READY, always None

  [D] event-worker writes UUID to events table
    -> PLUMBING READY, always None

  [E] record_request carries keyframe_uuid
    -> PLUMBING READY, always None

  [F] Replay service running with archive
    -> POC EXISTS, not in mainline compose

  [G] clip-worker calls ReplayClient.create_job(keyframe_uuid=...)
    -> CODE READY, never receives valid UUID

  [H] Replay generates exact clip from anchor
    -> API SUPPORTS IT, never invoked with valid data
```

### What's Missing (ordered by dependency)

1. **Replay service not in mainline compose** — Must add replay-service + source
   adapter feeding ZMQ to Replay.

2. **Savant frame_meta has no UUID** — Savant 0.6.0 does not expose frame UUIDs.
   Options:
   - (a) Source adapter injects UUID into frame metadata before Savant.
   - (b) Pyfunc generates UUID from available fields (e.g., UUIDv7 from
     `pts + source_id`), but this would not match Replay's internal keyframe ID.
   - (c) Upgrade Savant if a later version adds frame UUID support.

3. **Timestamp-domain mapping missing** — Replay uses pipeline-relative
   timestamps; events use epoch milliseconds. Without a mapping function,
   `find_keyframe` can only do unbounded search.

4. **No keyframe injection from Replay to events** — Even if Replay generates
   keyframe UUIDs, there is no mechanism to feed them back into the Savant
   pipeline so events can carry them.

## 6. Current State Classification

Using the gap categories from the inspection request:

| Category | Status | Evidence |
|----------|--------|----------|
| A. 没启动 Replay service | **YES** — mainline compose has no Replay | c1-official-adapter.yml |
| B. 启动了但没有 archive | N/A — not started | — |
| C. 有 archive 但没有把 frame_uuid 写入事件 | **YES** — POC has archive but no UUID path | phase3a compose, behavior_rules.py |
| D. 有 frame_uuid 但 ReplayClient 未接 | N/A — no frame_uuid exists | — |
| E. Replay API 版本不匹配 | **NO** — API supports anchor+offset | replay_client.py |
| F. 需要改 Savant pipeline / adapter 配置 | **YES** — need UUID source | module.yml, frame_meta |

**Multiple gaps exist simultaneously: A + C + F.**

## 7. Minimal Changes Required

To make Replay UUID work, at minimum:

### Step 1: Deploy Replay in mainline compose

Add to `docker-compose.c1-official-adapter.yml`:
- `replay-service` container (from phase3a template)
- Source adapter that feeds ZMQ to Replay (single-ingestion topology)

### Step 2: Establish timestamp-domain mapping

Either:
- Record `pipeline_ts_ns` in events alongside `event_ts_ms` (requires Savant
  pipeline metadata enrichment).
- Or accept unbounded keyframe search as degraded (current behavior).

### Step 3: Generate or receive frame UUIDs

Either:
- Source adapter injects frame UUIDs into metadata.
- Or Replay exposes keyframe UUIDs via an event callback.
- Or pyfunc derives a deterministic UUID from `(source_id, frame_num, pts)`.

### Step 4: Wire UUID into event export

Modify `behavior_rules.py` to populate `event.frame_uuid` and
`event.keyframe_uuid` from whatever source is chosen in Step 3.

**Estimated scope: 3-5 changes across compose, adapter config, pyfunc, and
possibly Savant module config. Not trivial.**

## 8. Recommendation

### Short-term (R3.3 MVP): Controlled Segment Recording

The existing planning document (`docs/r3_3_options_replay_vs_segment_recording.md`)
already recommends this path:

```
event_ts_ms -> segment index -> offset_ms -> exact snapshot/raw_clip
```

This approach:
- Does not depend on frame UUIDs.
- Does not require Replay deployment.
- Makes `event_ts_ms -> offset_ms` explicit.
- Can be validated with deterministic smoke tests.
- Works with the current c1-official-adapter compose unchanged.

### Long-term: Replay UUID

Keep Replay UUID as the preferred long-term architecture. The API contract in
`ReplayClient.create_job()` already supports it. The blocker is the UUID source
and timestamp mapping, which require Savant-level changes or adapter-level
injection.

### Hybrid (Option D from planning doc)

Use Replay when available (with unbounded keyframe search as degraded mode) and
fall back to controlled segment recording. Store a normalized `timeline_mapping`
so evidence workers don't care which provider supplied the media.

## 9. Existing Documentation Cross-Reference

| Document | Relevant Section |
|----------|-----------------|
| `CLAUDE.md` §8.1 | Savant Replay 录像边界 |
| `CLAUDE.md` §9 | 生产视频接入与 Replay 拓扑硬约束 |
| `specs/01_architecture.md` §2.0, §8 | Replay as future component |
| `specs/11_docker_communication_and_clip_design.md` §12, §18, §20 | Replay architecture |
| `docs/r3_3_options_replay_vs_segment_recording.md` | Full comparison |
| `docs/r3_3_timeline_mapping_evidence_plan.md` | Timeline mapping planning |
| `docs/phase3f0_2_diagnosis_report.md` | UUID limitation diagnosis |

## 10. Conclusion

The Replay UUID approach is architecturally correct but requires 3+ non-trivial
changes (Replay deployment, UUID source, timestamp mapping) before it can
produce exact evidence clips. Controlled segment recording remains the
recommended R3.3 MVP path. Replay UUID should be pursued as the long-term
solution once the UUID source and timestamp-domain mapping are resolved.
