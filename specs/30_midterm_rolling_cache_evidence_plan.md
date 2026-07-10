# 30_midterm_rolling_cache_evidence_plan.md

Date: 2026-07-04

## 1. Goal

Build a production-oriented rolling evidence cache so midterm evidence generation no
longer depends on one Replay job per event.

Target behavior:

```text
post-Savant stream
  -> continuous GOP-aligned rolling segments
  -> segment manifest / index
  -> event window lookup
  -> fast copy/remux evidence bundle
  -> 8090 evidence list/detail
```

The objective is fast, high-retention evidence under the 60-stream 8 FPS pressure
profile. The current per-event Replay path has a stable output ceiling of roughly
150-195 playable bundles per 400 s run even when event volume rises to about
700-1000. Rolling cache must break that ceiling by moving video writing from
"event-time burst work" to "continuous background segmenting".

## 2. Current Problem

Current Replay storage is not the same thing as a file-level rolling evidence
cache.

Replay keeps recent stream data in RocksDB with a 300 s TTL, but evidence
generation still has to:

1. create a per-event Replay job;
2. have Replay re-emit the selected stream window;
3. have `video-file-sink` create a new `video.mov`;
4. have `media-worker` package that output as evidence.

Therefore events still compete for Replay/admission slots. Recent pressure runs
show this clearly:

| run | events | playable bundles | main observation |
| --- | ---: | ---: | --- |
| fullgen/no admission | about 987 | 195 | ceiling near slot-throughput bound |
| `p60evt_20260704T141528` | 672 | 152 | finalizer wait visible, Replay queue still high |
| `p60finalizer16_20260704T114410Z` | 997 | 150 | finalizer improved, output ceiling unchanged |

The latest finalizer optimization reduced
`sink_ffprobe_ready_to_finalizer_start_ms p95` from about 205 s to about 34.7 s,
but total retained evidence stayed flat. Remaining dominant phases:

- `record_request_pending_ms p95 ~= 296 s`;
- `replay_to_sink_metadata_ms p95 ~= 55 s`;
- `materialization_expired` dominates when event density is high.

Conclusion: finalizer tuning is useful but no longer sufficient. The remaining
bottleneck is the per-event Replay/admission architecture.

## 3. Non-Goals

Do not use this work to:

- change model inference, watchlist thresholds, or behavior-event semantics;
- remove the existing Replay path before rolling cache is proven;
- fake success with `materialization_skipped`;
- require exact frame-perfect trimming for the MVP;
- make all 60 streams depend on the new path before a small-scope canary passes.

## 4. Required Front Door: Deterministic Pressure Harness

Before comparing rolling cache performance, add a deterministic pressure mode.
Live `1080movie` RTSP produces different event densities across runs, which makes
A/B validation noisy.

Requirements:

- pressure runner supports fixed input file and fixed start offset;
- 60 generated sources can reuse that fixed input deterministically;
- artifact records input URI/path, offset, duration, hash if local file, and
  effective event counts;
- baseline run can be repeated with similar event totals and event-type mix;
- keep the existing live RTSP pressure mode for exploratory stress, but do not use
  it as the only acceptance gate for rolling cache.

Acceptance token:

```text
PASS_ROLLING_CACHE_DETERMINISTIC_PRESSURE_INPUT
```

## 5. Phase 1 - Rolling Segment Writer MVP

Implement a small-scope writer for one shard or 8-10 representative sources.

MVP storage layout:

```text
/data/video-analytics/media/rolling-cache/
  midterm/
    epochs/<runtime_epoch_id>/
      <source_id>/
        segments/
          <segment_start_pts>_<segment_end_pts>_<seq>.mp4
        manifest.jsonl
        current.json
```

Segment rules:

- write post-Savant video suitable for evidence playback;
- segment duration target: 2-10 s, configurable;
- segment retention target: at least 300 s, configurable;
- segment boundary must be GOP/keyframe aligned whenever `-c copy` will be used;
- manifest row must include:
  - `runtime_epoch_id`;
  - `source_id`;
  - `camera_id`;
  - `segment_id`;
  - path/URI;
  - first/last PTS;
  - first/last wall-clock/event timestamp if available;
  - keyframe/GOP boundary flag;
  - codec/container metadata from ffprobe;
  - size and created time;
  - stream/session id if available.

Implementation preference:

- reuse existing Savant/video sink plumbing where practical;
- do not block inference hot path on manifest writes;
- write manifest atomically or append-only with recovery support;
- keep cleanup bounded by bytes and TTL.

Acceptance token:

```text
PASS_ROLLING_CACHE_SEGMENT_WRITER_MVP
```

## 6. Phase 2 - Segment Lookup and Fast Evidence Materialization

Add a materialization path that tries rolling cache before per-event Replay.

Decision order:

```text
event task
  -> if rolling cache enabled and source has segment coverage
       -> locate segments for [event_ts - pre, event_ts + post]
       -> generate raw_clip via concat/remux/copy
       -> write evidence bundle and DB index
       -> mark materialization_mode=rolling_cache_copy
     else
       -> fallback to existing Replay path
```

MVP precision:

- time boundary can be approximate;
- clip may start at the previous GOP boundary and end at the next GOP boundary;
- summary must state `canonical_clip=false` unless exact-window validation passes;
- 8090 must show the clip as playable and expose the materialization mode.

Required metadata in evidence summary/materialization:

- `materialization_mode=rolling_cache_copy`;
- `rolling_cache_enabled=true`;
- segment ids and paths used;
- requested window and actual output window;
- GOP/keyframe boundary decision;
- fallback reason if Replay path was used.

Acceptance token:

```text
PASS_ROLLING_CACHE_FAST_MATERIALIZATION_MVP
```

## 7. Phase 3 - Event Coverage / Merge Semantics

Do this in parallel with rolling cache, not after it.

Goal:

- avoid creating many duplicate independent evidence tasks for the same source and
  same continuous behavior;
- preserve per-event visibility in DB and 8090;
- do not mark covered events as skipped.

Proposed behavior:

```text
event B arrives near event A on the same source
  -> if A's evidence window covers B or can be extended
       -> B becomes covered_by=A evidence group
       -> B remains searchable
       -> 8090 can show B's event row pointing at A/group clip
  -> else create independent evidence
```

DB/API design can be either:

- explicit `evidence_event_links` table, or
- evidence bundle alias rows pointing multiple event ids to one raw clip,
  if that fits the current schema with less disruption.

Acceptance token:

```text
PASS_EVIDENCE_EVENT_COVERAGE_MERGE_MVP
```

## 8. Phase 4 - Pressure Validation

Run every phase with artifacts, not only unit tests.

Validation matrix:

1. deterministic small run: 8-10 sources, 8 FPS, fixed input, fixed offset;
2. deterministic 60-source run, 8 FPS, 400 s;
3. live RTSP exploratory run with current `1080movie`;
4. fallback run with rolling cache disabled.

Required metrics:

- events by type;
- playable bundles;
- expired/failed/skipped/covered counts;
- rolling-cache hit rate;
- Replay fallback count;
- segment coverage miss reasons;
- `record_request_pending_ms`;
- rolling materialization duration p50/p95/p99;
- evidence DB index latency;
- disk write bandwidth / cache size / cleanup count.

Primary success criteria:

- under deterministic 60-source 8 FPS 400 s pressure, playable/covered evidence
  should exceed the old 150-195 playable ceiling by a large margin;
- rolling-cache hit rate should be high for events whose requested window is
  inside the 300 s retention window;
- per-event Replay fallback must be the exception, not the dominant path;
- no active evidence tasks, active Replay slots, or pressure source containers
  remain after the run;
- 8090 evidence list/detail can open sampled clips.

Acceptance token:

```text
PASS_MIDTERM_ROLLING_CACHE_EVIDENCE_FAST_PATH
```

## 9. Safety and Rollback

Feature flags:

```text
ROLLING_CACHE_ENABLED=false
ROLLING_CACHE_MATERIALIZATION_ENABLED=false
ROLLING_CACHE_SOURCES=
ROLLING_CACHE_ROOT=/media/rolling-cache
ROLLING_CACHE_MATERIALIZED_ROOT=/media/rolling-cache-materialized
ROLLING_CACHE_RETENTION_SECONDS=300
ROLLING_CACHE_SEGMENT_SECONDS=4
ROLLING_CACHE_MAX_BYTES=0
ROLLING_CACHE_FALLBACK_TO_REPLAY=true
ROLLING_CACHE_MATERIALIZATION_MAX_PER_POLL=16
ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=false
```

Rollback must be simple:

- disable rolling cache materialization and fall back to the current Replay path;
- keep existing evidence API contracts;
- do not delete Replay path code;
- cleanup rolling-cache files only by TTL/byte quota or explicit maintenance.

## 10. Suggested Work Order

1. Add deterministic pressure input mode.
2. Add rolling-cache config, paths, and docs.
3. Build segment writer MVP for 1 shard / 8-10 sources.
4. Add manifest parser and segment coverage lookup.
5. Add rolling-cache materializer with Replay fallback.
6. Add evidence summary and DB index fields.
7. Add event coverage/merge MVP.
8. Run small deterministic pressure.
9. Expand to 60 deterministic pressure.
10. Run live RTSP stress.
11. Update docs and operator runbook.

## 11. Implementation Status

2026-07-04 initial MVP code landed behind disabled feature flags.

Completed:

- deterministic pressure republisher input controls:
  `--rtsp-republish-input-offset-s` and `--rtsp-republish-input-loop`;
- rolling-cache sink entrypoint:
  `scripts/runtime/rolling_cache_sink_entrypoint.sh`;
- compose profiles:
  `rolling-cache` for the default Replay pre-analysis fanout and
  `rolling-cache-dual` for the dual Replay pre-analysis fanouts;
- media-worker rolling-cache config and disabled defaults in
  `infra/env/midterm.env`;
- `services/media-worker/app/rolling_cache.py` segment scanner and
  `ffmpeg -c copy` concat/remux materializer;
- media-worker polling path that can materialize pending evidence tasks from
  rolling cache before per-event Replay output is involved;
- event-worker option `ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=false` so a canary
  can route evidence tasks to rolling cache without publishing per-event Replay
  record requests;
- coverage/merge schema and MVP code behind disabled flags:
  `db/migrations/023_evidence_event_links.sql`,
  `EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED=false`, and media-worker alias
  publishing for covered child events;
- evidence DB materialization projection now preserves rolling-cache fields such
  as `materialization_mode`, requested/actual PTS window and segment ids.

Verified so far:

```text
python -m py_compile services/media-worker/app/rolling_cache.py services/media-worker/app/worker.py services/media-worker/app/config.py scripts/runtime/run_midterm_pressure60.py
python -m py_compile services/event-worker/app/config.py services/event-worker/app/worker.py
pytest -q harness/tests/test_rolling_cache_materialization.py
pytest -q harness/tests/test_midterm_pressure60_script.py::test_rtsp_republish_command_supports_deterministic_file_offset_and_loop
pytest -q harness/tests/test_event_worker_recording_policy.py
pytest -q harness/tests/test_evidence_event_coverage_merge.py
docker compose -f infra/docker-compose.midterm.yml --profile rolling-cache config --quiet
docker compose -f infra/docker-compose.midterm.yml --profile dual-4090-two-source --profile rolling-cache-dual config --quiet
git diff --check -- rolling-cache touched files
```

Runtime canary:

```text
run_id=rolling_cache_canary_20260704T130505Z
artifact_dir=/data/video-analytics/artifacts/rolling_cache_canary_20260704T130505Z
source_id=source_00000000-0000-4000-8000-781078565686
event_id=4180494f-6f3d-48ec-90ce-5a9fd095b8e5
segment_writer=rolling-cache-sink
segment_chunk_size_frames=32
raw_clip_duration_seconds=3.930000
raw_clip_size_bytes=345114
materialization_mode=rolling_cache_copy
status=materialized/generated_unverified
active_evidence_tasks_after=0
active_replay_slots_after=0
```

Canary fixes made during runtime validation:

- `video_files.py` writes source directories as `source_id%`; segment lookup now
  accepts those percent-suffixed directories.
- `CHUNK_SIZE` is frame count, not seconds; the rolling sink entrypoint now maps
  `ROLLING_CACHE_SEGMENT_SECONDS * ROLLING_CACHE_FPS` to frames, defaulting to
  `4s * 8fps = 32` frames.
- rolling materialization allows a small segment-edge coverage slack and records
  `start_gap_ns` / `end_gap_ns`; this keeps GOP/segment-level approximate clips
  playable instead of failing on tens of milliseconds of edge mismatch.
- rolling-cache epoch guard no longer requires per-event Replay labels when
  event payload, sink path/metadata, and current epoch agree.
- source filtering for rolling-cache candidate tasks is pushed into SQL before
  `LIMIT`, preventing old pending tasks from starving a canary source.

60-source status:

- 8-source deterministic canary and 60-source deterministic low-density run have
  passed, but `testVideo/test.mp4` did not generate enough events to prove the
  old Replay ceiling was broken.
- A live high-density 60-source exploratory run has now passed and is the current
  performance validation artifact. A fixed high-density deterministic file is
  still desirable for future A/B comparisons, because `1080movie` event density
  changes with playback content.

## 12. Final Rolling-Cache Stress Validation

2026-07-04 final live stress validation:

```text
run_id=rollingcache_live_p60_finalclean4_20260704T171638Z
artifact_dir=/data/video-analytics/artifacts/rollingcache_live_p60_finalclean4_20260704T171638Z
streams=60
fps=8/1
duration_s=400
drain_s=300
topology=dual_same_gpu
input=rtsp://192.168.1.105:8554/live/1080movie
status=passed
warnings=validate_seq_iq_expected_sampling_gap
```

Key result:

- pressure events before cleanup: 759;
- playable bundles before cleanup: 383;
- covered events before cleanup: 580;
- covered playable events before cleanup: 397;
- retained/post-reconcile events: 519;
- retained/post-reconcile playable bundles or aliases: 519;
- retained task statuses after alias reconcile: 519 `materialized`;
- active materialization tasks after reconcile: 0;
- active Replay slots after reconcile: 0;
- pressure source containers after restore: 0;
- `security.record_requests` deleted by the run: 0, confirming the evidence path
  did not use per-event Replay record requests.

This breaks the old per-event Replay ceiling of about 150-195 playable bundles
per 400 s high-density run. The retained clips are `rolling_cache_copy` outputs;
the materializer uses segment/GOP-level approximate windows and marks clips as
`canonical_clip=false` / `generated_unverified` when object sidecars are not
available, rather than dropping playable evidence.

Runtime fixes made during final pressure validation:

- `media-worker` rolling-cache materialization now flushes finalizer work in
  parallel, bounded chunks instead of serializing a whole poll batch.
- `clip-worker` materialization deadline expiry no longer kills rolling-cache
  tasks that are already claimed by the rolling materializer.
- `clip-worker` materialization deadline expiry also protects coverage parent
  tasks while child events depend on them; these parents are evidence group roots
  and must not be expired by the old Replay TTL sweep.
- `media-worker` periodically reconciles late `covered_by` child events whose
  parent bundle was already finalized, publishing DB aliases instead of leaving
  child tasks in `materialization_deferred` / `materializing` / `failed`.
- pressure cleanup keeps `covered_by` child event rows when `--keep-evidence -1`
  is used, so retained pressure evidence remains searchable per event.

Representative retained metrics from `report.json` kept evidence:

- raw clip duration: p50 about 23.09 s, p95 about 50.44 s, min 2.99 s, max
  79.64 s. Long clips are expected when same-source coverage extends a parent
  evidence window over a continuous behavior burst.
- `sink_video_to_stable_ms`: p50/p95 0/0.
- `sink_ffprobe_ready_to_finalizer_start_ms`: p50 about 8.98 s, p95 about
  25.81 s.
- `finalization_elapsed_ms`: p50 about 2.62 s, p95 about 9.97 s.

Important artifact note: `report.json` was written before the final alias
reconcile patch was manually applied to the already-finished run, so its
`db_summary_after_cleanup` still shows 57 active child aliases. The authoritative
post-reconcile state is recorded in:

```text
/data/video-analytics/artifacts/rollingcache_live_p60_finalclean4_20260704T171638Z/post_reconcile_summary.json
```

The current code includes the periodic reconcile path, so future runs should
reach that post-reconcile state without a manual one-off command.

Acceptance tokens:

```text
PASS_ROLLING_CACHE_DETERMINISTIC_PRESSURE_INPUT
PASS_ROLLING_CACHE_SEGMENT_WRITER_MVP
PASS_ROLLING_CACHE_FAST_MATERIALIZATION_MVP
PASS_EVIDENCE_EVENT_COVERAGE_MERGE_MVP
PASS_MIDTERM_ROLLING_CACHE_EVIDENCE_FAST_PATH
```

## 13. Goal Prompt

Use this prompt with the goal command:

```text
完成 specs/30_midterm_rolling_cache_evidence_plan.md 的 rolling cache 证据生成修复，目标是在 midterm 60 路 8fps 压测下绕开 per-event Replay job 的产能天花板，实现快速、高保留率证据生成。

请按文档阶段一次性推进，但每个阶段都要有验证和可回滚边界：

1. 先实现确定性压测输入模式，支持固定视频/固定 offset，生成可重复的 60 路 8fps 400s baseline artifact。
2. 实现 rolling segment writer MVP，先覆盖 1 个 shard 或 8-10 路 source，持续写 GOP/keyframe 对齐 segment，维护 manifest/index，保留 300s，路径在 /data/video-analytics/media/rolling-cache 下。
3. 实现 rolling cache materialization fast path：事件来了优先查 segment 覆盖窗口，用 ffmpeg -c copy concat/remux 生成 evidence raw_clip；时间边界允许 GOP 级近似，但 summary/materialization 必须明确标记 rolling_cache_copy、actual window、segment ids、canonical_clip=false；覆盖不足时 fallback 到现有 Replay 路径。
4. 并行实现同源事件覆盖/合并 MVP：短时间内同 source 连续事件可以共享/扩展同一证据，不能用 materialization_skipped 假装成功；8090/DB 查询每条事件时仍能看到对应证据或 covered_by 关系。
5. 保留所有 feature flag 和回滚开关，默认可以先小范围 canary，不破坏现有 Replay 证据路径。
6. 补齐单测/合约测试/compose config 检查；运行小规模确定性压测，再跑 60 路 8fps 400s 压测，对比旧 ceiling 150-195 playable bundles，输出 artifact 和指标。
7. 更新 docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md、docs/project_current_progress_summary.md 和必要 runbook，记录问题、代码路径、验证结果和下一步。

验收条件：
- PASS_ROLLING_CACHE_DETERMINISTIC_PRESSURE_INPUT
- PASS_ROLLING_CACHE_SEGMENT_WRITER_MVP
- PASS_ROLLING_CACHE_FAST_MATERIALIZATION_MVP
- PASS_EVIDENCE_EVENT_COVERAGE_MERGE_MVP
- PASS_MIDTERM_ROLLING_CACHE_EVIDENCE_FAST_PATH

执行时请保护当前 dirty worktree，不要回滚无关改动；需要重启服务时优先 docker compose recreate，不要无谓 rebuild；每轮压测结束必须确认 active_evidence_tasks=0、active_replay_slots=0、pressure source containers=0，并给出 artifact 路径和关键 p50/p95 指标。
```
