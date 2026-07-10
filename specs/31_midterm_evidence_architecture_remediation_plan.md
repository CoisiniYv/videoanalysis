# 31_midterm_evidence_architecture_remediation_plan.md

Date: 2026-07-06

## 1. 背景

用户反馈：当前已经做过多轮 evidence 修复，但 60 路密集事件下保存证据的效果仍不理想。

静态审查结论见：

- `docs/code_review/architecture_evidence_vs_savant.md`
- `docs/code_review/architecture_evidence_slow_path_static_analysis.md`
- `specs/29_midterm_evidence_replay_latency_optimization_plan.md`
- `specs/30_midterm_rolling_cache_evidence_plan.md`
- `docs/midterm_evidence_bypass_diagnosis_2026-07-06.md`

本计划不再把重点放在“继续调大 Replay 并发”或“继续修某个单点慢函数”。当前问题更像架构问题：系统仍容易退回到逐事件生成 evidence 的慢路径，或者即使使用 rolling cache，也可能被 source 粒度、annotation proof、alias reconcile、DB 状态和 runtime 配置漂移拖慢。

目标是把 evidence 体系改成：

```text
continuous cache first
  -> event as index / alias
  -> fast raw clip availability
  -> annotation/proof async completion
  -> Replay as fallback, not high-density main path
```

## 2. 核心判断

当前 evidence 慢的主因不是 Savant/Replay 官方能力不够，而是当前产品层仍保留了太多逐事件同步步骤：

```text
event
  -> create evidence task
  -> publish record_request
  -> wait post-Savant proof
  -> acquire Replay slot
  -> create per-event Replay job
  -> wait video-file-sink
  -> wait media-worker finalizer
  -> rebuild annotations
  -> write DB index
```

60 路密集事件下，只要其中任一环节排队，后续任务就会进入 `waiting_proof`、`materialization_deferred`、`queued` 或 `materialization_expired`。

因此后续修改必须遵守三个原则：

1. 高密度场景默认不走 per-event Replay job。
2. raw clip 可回放和 annotation/proof 完整性要解耦。
3. 同源密集事件默认共享证据窗口，而不是每个事件都物化一个物理 clip。

## 3. 非目标

不要把本计划变成以下工作：

- 不重写 Savant 模型链路；
- 不改 watchlist 阈值或算法语义；
- 不用关闭 proof/guard 的方式伪造成功；
- 不靠无限增大 Replay/video-file-sink 并发掩盖架构问题；
- 不把 `materialization_skipped` 当成成功 evidence；
- 不删除现有 Replay 路径，Replay 仍然作为 fallback 和审计底座。

## 4. Phase 0 - 先确认当前实际走的是哪条路径

问题：

多轮修复之后效果仍差，第一风险是“代码有 fast path，但运行时没有真正走到 fast path”，或者 source/shard/epoch 配置让 fast path 失效。

2026-07-06 旁路快照已经看到一个现实例子：当时 PostgreSQL 中
`events/evidence_tasks/evidence_bundles` 均为空，Redis consumer lag/pending
为 0，但 worker 容器内仍是 `ROLLING_CACHE_MATERIALIZATION_ENABLED=false`、
`EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED=false`、
`FRAME_CACHE_SIDECAR_SOURCE_STREAM_ENABLED=false`，clip-worker 也没有启用
Replay shard routing。结论是：以后每轮压测必须先证明 worker 容器内
runtime flags 已经进入目标 evidence path，而不能只看 env 文件或代码。

同日后续 artifact
`readyat_cd60_drain120_w60_sharedfix_20260706T145959Z` 又给出第二个现实例子：
该轮 `report.json` 状态是 `failed_pressure_gates`，但 evidence 侧并未超时。
进入 evidence 队列的 `103/103` 个任务均 `materialized`，`103/103` 个 bundle
可回放，active materialization tasks 为 0；`downstream_observability_summary.json`
显示 `replay_slot_acquired=0`，live DB 显示
`materialization_mode=rolling_cache_copy` 为 103。失败原因在上游：
`savant_send_failures`、`forwarder_queue_full`、`validate_seq_iq_exceeded`，
forwarder `forwarded/seen` 只有 `0.5397`，`queue_full_samples=10`。

因此 Phase 0 的诊断必须把三类结果拆开，而不是用一个“证据慢/不慢”总判断：

```text
evidence materialization: retained evidence raw clip 是否按时可回放
annotation completeness: annotation 是否 complete / partial / missing
upstream pressure gates: forwarder/Savant/source ingress 是否已经丢帧或失稳
```

对 `readyat_cd60_drain120_w60_sharedfix_20260706T145959Z` 的当前判定是：
evidence materialization 通过，annotation completeness 未完全通过，上游压力门未通过。
另外该轮使用的是一个固定 RTSP publisher 加 60 个 source adapter 订阅同一路视频；
它能证明 shared-RTSP 条件下的 60 source evidence 路径，但不能证明 60 个独立
RTSP 发布入口稳定。

修改计划：

1. 增加一个只读诊断脚本或 API 汇总，输出最近 30 分钟 evidence 路径分布：
   - `materialization_mode=rolling_cache_copy` 数量；
   - `materialization_mode=post_savant_replay` / Replay job 数量；
   - Replay fallback count；
   - `materialization_expired` 数量；
   - `waiting_proof` 数量；
   - `covered_by` alias 数量；
   - active Replay slots；
   - active evidence tasks；
   - 每个 `source_id` 的事件数、bundle 数、alias 数。
2. 在压力报告中强制打印：
   - rolling-cache flags；
   - coverage-merge flags；
   - source stream flags；
   - `REPLAY_SHARDS_JSON` / source-to-shard mapping 摘要；
   - current runtime epoch；
   - unique `source_id` count。
3. 在压力报告中单独打印 annotation completeness：
   - `raw_clip_uri available / total`；
   - `annotation_status` 分布；
   - `annotation_count > 0` 分布；
   - source-scoped annotation 命中率和 fallback-global count。
4. 在压力报告中单独打印上游 pressure gates：
   - forwarder frames seen/forwarded/dropped；
   - forwarded/seen ratio；
   - forwarder queue depth/max/full sample count；
   - Savant send failures；
   - `validate_seq_iq` count；
   - source visibility stable sample count。
5. 如果 60 路压测 unique `source_id` 小于 60，直接判定该轮不可用于证明 60 路独立摄像头能力。
6. 如果 shared-RTSP 模式通过，报告必须标注为 evidence-path isolation pass，
   不能直接升级为 60 independent RTSP ingress pass。

涉及文件：

- `scripts/tools/analyze_midterm_pressure_artifact.py`
- `scripts/tools/check_midterm_evidence_drain.py`
- `scripts/runtime/run_midterm_pressure60.py`
- `services/api/app/routers/evidence.py` 或新增只读 runtime diagnostics endpoint

验收：

```text
PASS_EVIDENCE_PATH_DISTRIBUTION_DIAGNOSTIC
```

## 5. Phase 1 - 把 rolling cache 变成高密度主路径

问题：

`specs/30` 已经证明 rolling cache 能突破 per-event Replay 天花板，但当前默认配置仍可能保守，且 record_request/Replay fallback 仍容易成为高压主路径。

修改计划：

1. 增加一个明确的高密度 evidence profile：

```text
EVIDENCE_DENSITY_PROFILE=normal|high_density
```

2. `high_density` 下默认：

```text
ROLLING_CACHE_ENABLED=true
ROLLING_CACHE_MATERIALIZATION_ENABLED=true
ROLLING_CACHE_FALLBACK_TO_REPLAY=true
ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=true
EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED=true
```

3. event-worker 在 rolling cache materialization enabled 且 source 有 cache coverage 时，不发布 per-event `security.record_requests`。
4. Replay fallback 必须写明原因：
   - `rolling_cache_disabled`;
   - `source_not_enabled`;
   - `segment_coverage_miss`;
   - `runtime_epoch_mismatch`;
   - `manifest_unavailable`;
   - `materializer_error`.
5. 8090 evidence detail 必须显示 materialization mode，但不改变现有 API contract。

涉及文件：

- `infra/env/midterm.env`
- `infra/docker-compose.midterm.yml`
- `services/event-worker/app/worker.py`
- `services/event-worker/app/config.py`
- `services/media-worker/app/rolling_cache.py`
- `services/media-worker/app/worker.py`
- `services/media-worker/app/evidence_db_index.py`

验收：

```text
PASS_HIGH_DENSITY_EVIDENCE_PROFILE_ROLLING_CACHE_FIRST
```

## 6. Phase 2 - source_id / shard / stream 命名强校验

问题：

当前 per-source gate、rolling-cache segment lookup、frame annotation lookup、Replay shard routing 都依赖 `source_id`。如果 60 路被压成少数 source，或者不同组件的 source id 写法不一致，系统会表现为保存特别慢。

修改计划：

1. 在 source generation 阶段生成 source identity manifest：

```text
source_id
camera_id
camera_name
replay_shard_id
rolling_cache_source_dir
frame_annotation_stream
runtime_epoch_id
```

2. pressure runner 启动前校验：
   - 60 路必须有 60 个唯一 `source_id`；
   - 每个 source 必须有 Replay shard assignment；
   - 每个 source 必须有 rolling-cache 输出目录或明确禁用原因。
3. media-worker materialization 前校验 event source 与 segment source 一致。
4. clip-worker Replay fallback 前校验 event source 与 replay shard source map 一致。
5. 发现 source mismatch 时 fail fast，并把原因写进 evidence diagnostics，不允许静默 fallback 到慢路径。

涉及文件：

- `scripts/runtime/run_midterm_pressure60.py`
- `scripts/runtime/camera_source_controller.py`
- `services/clip-worker/app/replay_shards.py`
- `services/media-worker/app/rolling_cache.py`
- `services/media-worker/app/worker.py`

验收：

```text
PASS_EVIDENCE_SOURCE_IDENTITY_CONTRACT
```

## 7. Phase 3 - source-scoped frame annotation stream

问题：

当前 proof 和 sidecar 路径会扫描全局 `security.frame_annotations`。60 路高密度下，全局 stream 的反扫和过滤会越来越重，也会增加 proof miss 风险。

修改计划：

1. 打开 source-scoped frame annotation stream：

```text
FRAME_ANNOTATION_SOURCE_STREAM_ENABLED=true
FRAME_ANNOTATION_STREAM_MODE=global_and_source
FRAME_CACHE_SIDECAR_SOURCE_STREAM_ENABLED=true
FRAME_CACHE_SIDECAR_SOURCE_STREAM_FALLBACK_GLOBAL=true
```

2. clip-worker proof lookup 优先读 `security.frame_annotations.{source_id}`，只在缺失时 fallback global。
3. media-worker sidecar builder 优先读 source stream。
4. diagnostics 中增加：
   - `source_stream_used`;
   - `source_stream_fallback_global`;
   - `entries_scanned`;
   - `messages_filtered_source`;
   - `messages_filtered_runtime_epoch`;
   - `messages_filtered_stream_session`.
5. source stream Redis maxlen 根据 FPS 和 retention 计算，不能固定太小。

涉及文件：

- `modules/savant_security/module.yml`
- `modules/savant_security/custom/pyfuncs/frame_annotation_exporter.py`
- `modules/savant_security/custom/services/frame_annotation_exporter.py`
- `services/clip-worker/app/worker.py`
- `services/media-worker/app/frame_cache_sidecar_writer.py`
- `infra/env/midterm.env`
- `infra/docker-compose.midterm.yml`

验收：

```text
PASS_SOURCE_SCOPED_FRAME_ANNOTATION_STREAM
```

## 8. Phase 4 - raw clip ready 与 annotation ready 解耦

问题：

当前 evidence 容易因为 annotation/proof 不完整而拖慢 raw clip 可回放。高密度场景下，用户首先需要可回放 clip，annotation 可以后补或标记 degraded。

修改计划：

1. evidence 状态拆成两组字段：

```text
raw_clip_status=pending|ready|failed
annotation_status=pending|complete|partial|missing|failed
evidence_state=materializing|materialized|materialized_degraded|failed|expired
```

2. rolling cache 找到 segment 并成功 remux/copy 后，立即写 DB bundle raw clip index。
3. annotation sidecar 异步补齐：
   - complete：覆盖窗口内 frame/object 足够；
   - partial：可显示但不完整；
   - missing：raw clip 可回放但无可用 annotation；
   - failed：构建失败，有 error_message。
4. 8090 列表允许 `materialized_degraded` 可播放，并在详情里展示 annotation 状态。
5. 不再因为 annotation missing 让 raw clip 整体不可播放。

涉及文件：

- `db/migrations/017_evidence_database_artifacts.sql` 或新增 migration
- `services/media-worker/app/evidence_db_index.py`
- `services/media-worker/app/worker.py`
- `services/evidence-viewer/app/static/evidence.js`
- `services/api/app/routers/evidence.py`
- `services/api/app/services/evidence_detail_resolver.py`

验收：

```text
PASS_EVIDENCE_RAW_CLIP_READY_BEFORE_ANNOTATION_COMPLETE
```

## 9. Phase 5 - coverage merge 默认参与高密度路径

问题：

60 路密集事件不是 60 路均匀单点事件，而是大量同源连续事件。逐事件物化 clip 会产生重复 IO、重复 DB 写和重复 annotation work。

修改计划：

1. 高密度 profile 下默认启用：

```text
EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED=true
EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS=60
```

2. coverage parent 可以扩展窗口，但必须有上限：

```text
EVIDENCE_COVERAGE_PARENT_MAX_DURATION_SECONDS=60
```

3. covered child event 不创建独立 raw clip，只写 `evidence_event_links` 和 alias bundle/index。
4. 8090 搜索单个 child event 时能打开 parent/group clip。
5. 压测报告必须分别统计：
   - physical bundles；
   - covered aliases；
   - playable event coverage；
   - average events per physical bundle。

涉及文件：

- `services/event-worker/app/repository.py`
- `services/media-worker/app/worker.py`
- `services/media-worker/app/evidence_db_index.py`
- `services/api/app/repositories/events.py`
- `services/api/app/routers/evidence.py`
- `services/evidence-viewer/app/static/evidence.js`

验收：

```text
PASS_HIGH_DENSITY_EVIDENCE_COVERAGE_DEFAULT
```

## 10. Phase 6 - Replay fallback 的资源池与状态拆分

问题：

Replay fallback 仍然需要保留，但不应该被 media-worker finalizer 长时间占住 slot。当前 completion-aware slot 释放依赖 sink video stable，这比 Replay API job created 更安全，但仍会把 sink/media-worker 慢点反向传导到 Replay admission。

修改计划：

1. 拆分两个资源池：

```text
replay_export_slot: Replay job -> sink metadata/video first stable
materialization_slot: sink output -> evidence bundle/index
```

2. `replay_export_slot` 只代表 Replay/video-file-sink 输出阶段。
3. `materialization_slot` 只代表 media-worker 后处理阶段。
4. diagnostics 中明确：
   - `replay_export_wait_ms`;
   - `replay_to_sink_metadata_ms`;
   - `sink_video_to_stable_ms`;
   - `finalizer_pool_wait_ms`;
   - `finalization_duration_ms`.
5. Replay fallback 在 high_density profile 下设置较低优先级，rolling cache miss 才进入。

涉及文件：

- `db/migrations/022_replay_slot_lifecycle.sql` 或新增 migration
- `services/clip-worker/app/repository.py`
- `services/clip-worker/app/worker.py`
- `services/media-worker/app/worker.py`
- `scripts/tools/analyze_midterm_pressure_artifact.py`

验收：

```text
PASS_REPLAY_FALLBACK_SLOT_SPLIT
```

## 11. Phase 7 - 压测验收标准

必须同时跑两类压测：

1. deterministic 60-source 8 FPS 600s；
2. live RTSP high-density 60-source 8 FPS 600s。

每轮压测结束后只允许 120s evidence drain 窗口；超过 120s 后仍未落盘、
仍无 raw clip、仍无可读取 annotation，均不能算作最终通过。

每轮必须输出：

- artifact path；
- runtime epoch；
- unique source count；
- configured camera count；
- forwarder-visible source count；
- Savant-visible source count；
- sources with retained events；
- sources with playable evidence；
- physical bundle count；
- covered alias count；
- playable event coverage；
- raw clip availability；
- annotation status distribution；
- annotation complete ratio；
- rolling cache hit rate；
- Replay fallback count；
- `materialization_expired` count；
- `record_request_pending_ms` p50/p95/p99；
- rolling materialization p50/p95/p99；
- finalizer p50/p95/p99；
- forwarder seen/forwarded/dropped frames；
- forwarder queue full sample count；
- Savant send failure count；
- `validate_seq_iq` count；
- active evidence tasks after drain；
- active Replay slots after drain；
- pressure source containers after cleanup。
- rolling-cache segment/evidence FPS proof。

通过标准：

```text
unique_source_count == 60
rolling_cache_hit_rate >= 0.90
Replay fallback is not dominant
playable_event_coverage >= 0.95
raw_clip_available_ratio >= 0.95
annotation_complete_ratio reported separately; missing annotations may degrade
  quality but must not hide raw clip availability
materialization_expired == 0 for retained events
active_evidence_tasks_after == 0
active_replay_slots_after == 0
pressure_source_containers_after == 0
rolling_cache_segment_fps_p50 >= configured_fps * 0.90
all retained evidence has nonzero annotation count and 8090 annotation API coverage
```

Shared-RTSP isolation and independent RTSP ingress are separate gates:

```text
shared_rtsp_60_source_evidence_path_pass
independent_rtsp_60_source_ingress_pass
```

`readyat_cd60_drain120_w60_sharedfix_20260706T145959Z` would count as evidence
materialization progress because retained evidence reached `103/103` playable
rolling-cache bundles, but it must still fail the full architecture gate because
upstream pressure gates failed and annotation completeness was only `32/103`.

If a future run again shows evidence materialization passing while upstream
pressure gates fail, the next optimization priority is not Replay concurrency;
it is forwarder/Savant/source-ingress stability and backpressure.

最终验收 token：

```text
PASS_MIDTERM_EVIDENCE_HIGH_DENSITY_ARCHITECTURE_READY
```

## 12. 建议执行顺序

1. 做 Phase 0 诊断，先证明当前慢到底是没走 rolling cache、source 串行、proof 慢、fallback 多，还是 finalizer 慢。
2. 如果 Phase 0 显示 retained evidence 已经按时 materialized，但
   forwarder/Savant pressure gates 失败，先处理上游 backpressure/source
   stability，不要继续盲目加 Replay 并发。
3. 做 Phase 2 source identity contract。这个是 60 路能力的地基。
4. 做 Phase 1 high-density profile，让 rolling cache 成为明确主路径。
5. 做 Phase 5 coverage merge 默认化，减少物理 clip 数。
6. 做 Phase 4 raw/annotation 解耦，让可回放 clip 先出现。
7. 做 Phase 3 source-scoped annotation stream，降低 proof/sidecar 扫描成本。
8. 做 Phase 6 Replay fallback slot split，治理剩余 fallback 长尾。
9. 跑 Phase 7 压测，并把报告写入 `docs/`。

## 13. Goal Prompt

可直接用于后续 goal 执行：

```text
完成 specs/31_midterm_evidence_architecture_remediation_plan.md。

目标：解决当前多轮修复后 evidence 保存仍不理想的问题，把 midterm 60 路高密度事件 evidence 从逐事件 Replay/同步 proof 路径，收敛为 rolling-cache-first、coverage-merge、raw-clip-ready-first 的架构。

执行要求：
1. 先实现 evidence path distribution 诊断，确认当前运行时到底走 rolling_cache_copy、Replay fallback、waiting_proof、materialization_expired、covered_by 的比例。
2. 加 source identity contract，60 路压测必须证明 unique_source_count=60，source_id/camera_id/replay_shard/rolling_cache/frame_annotation_stream 一致。
3. 增加 EVIDENCE_DENSITY_PROFILE=high_density，让 rolling cache 和 coverage merge 成为高密度主路径；Replay 只做 fallback，fallback reason 必须可审计。
4. 打开并接通 source-scoped frame annotation stream，clip-worker/media-worker 优先读 source stream，fallback global 只做兼容。
5. 拆 raw_clip_status 与 annotation_status，raw clip 成功后先让 8090 可回放，annotation/proof 后补或 degraded，不再因为 annotation 缺失整体阻塞 raw clip。
6. coverage merge 默认参与高密度路径，同源连续事件共享/扩展证据窗口，child event 在 8090 仍可搜索并打开 parent/group clip。
7. 拆分 Replay fallback export slot 与 media materialization slot，避免 finalizer 慢反向占住 Replay admission。
8. 每阶段补 targeted pytest、compose config 检查和压力 artifact parser 测试；保护 dirty worktree，不回滚无关改动。
9. 最后跑 deterministic 60-source 8 FPS 600s 和 live RTSP high-density 60-source 8 FPS 600s，120s drain 内必须完成全部 evidence 落盘，报告 unique source count、rolling cache hit rate、Replay fallback count、playable event coverage、expired count、active task/slot cleanup、rolling-cache/evidence FPS proof 和 annotation coverage。

最终验收：
PASS_EVIDENCE_PATH_DISTRIBUTION_DIAGNOSTIC
PASS_EVIDENCE_SOURCE_IDENTITY_CONTRACT
PASS_HIGH_DENSITY_EVIDENCE_PROFILE_ROLLING_CACHE_FIRST
PASS_SOURCE_SCOPED_FRAME_ANNOTATION_STREAM
PASS_EVIDENCE_RAW_CLIP_READY_BEFORE_ANNOTATION_COMPLETE
PASS_HIGH_DENSITY_EVIDENCE_COVERAGE_DEFAULT
PASS_REPLAY_FALLBACK_SLOT_SPLIT
PASS_MIDTERM_EVIDENCE_HIGH_DENSITY_ARCHITECTURE_READY
```
