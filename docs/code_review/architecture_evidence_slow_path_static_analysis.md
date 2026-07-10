# Static Architecture Review: Why Evidence Materialization Is Slow

范围：当前工作树 `/home/user/video-analytics`。本文只做静态逻辑分析，不修改源码/测试，不声明当前运行时的实测瓶颈。

## 1. 一句话结论

当前 evidence 慢，不是因为“Replay 导出 clip 这一步天然很慢”，而是因为系统把一个事件证据拆成了多段强一致/准强一致工作流：

```text
event-worker 写事件/任务
-> Redis record_request
-> clip-worker 等 post-Savant frame proof
-> DB Replay slot admission
-> Replay job 输出到 video-file-sink
-> media-worker 轮询 sink 文件、等待稳定、释放 slot
-> media-worker 复制/裁剪 raw clip、重建 annotation sidecar、跑 guard
-> DB evidence_bundles / artifacts / timeline / overlay index
```

每段都有自己的限流、等待、重试、DB 状态更新和失败保护。60 路密集事件下，慢点会被叠加成“保存证据很艰难”。

## 2. 最大慢点：每个 source 只允许 1 个活跃 evidence

midterm 默认配置里有两层 per-source 串行：

- `EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE=1`，见 `infra/env/midterm.env:113-115`。
- `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE=1`，见 `infra/env/midterm.env:116-118`。

event-worker 创建 evidence task 时也会按 active task 计数做 admission，见 `services/event-worker/app/repository.py:514-577`。clip-worker 在创建 Replay job 前又做一次 gate，`max_per_source` 命中时直接返回 `max_concurrent_per_source_reached`，见 `services/clip-worker/app/worker.py:3024-3061`。

真正原子预占 Replay slot 的 SQL 也会按全局、shard、source 三个维度计数，见 `services/clip-worker/app/repository.py:276-582`。所以逻辑上：

- 如果 60 路是 60 个真实不同 `source_id`，理论上可以并行，但每路仍只能 1 个证据在路上。
- 如果压力测试或实际接入把很多“摄像头”映射到同一个 `source_id`，就会被压成同一路串行，60 个事件会排队。
- 即使有 8 个 clip-worker consumer 和 32 个 media-worker finalizer，per-source=1 仍会把同一源的密集事件变成队列。

这是最像“保存不动”的静态原因。

## 3. 队列不是立即重试，而是 Redis pending 周期性重领

clip-worker 遇到并发/slot gate 时调用 `_queue_clip_request()`，只更新 DB 状态，不 `xack` Redis message，见 `services/clip-worker/app/worker.py:1768-1833`。后续靠 pending claim 重新取回：

- `CLIP_WORKER_PENDING_CLAIM_MIN_IDLE_MS=5000`
- `CLIP_WORKER_PENDING_CLAIM_COUNT=10`
- `CLIP_WORKER_PENDING_CLAIM_INTERVAL_S=5`

配置见 `infra/env/midterm.env:135-137`，主循环见 `services/clip-worker/app/worker.py:3193-3230`。

所以排队不是“slot 一释放立刻抢到”，而是以 pending claim 的时间粒度回流。密集 burst 下，这会把延迟切成 5 秒一档，且每次还要再过 proof、admission、Replay job。

## 4. post-Savant proof 是必经慢门

当前 evidence raw clip 来自 Replay，但标注/proof 来自 `security.frame_annotations`。clip-worker 在创建 Replay job 前要先证明：

- source/camera 对得上；
- runtime epoch 对得上；
- stream session 对得上；
- requested start/end PTS 覆盖得上；
- anchor keyframe PTS 合法。

代码在 `services/clip-worker/app/worker.py:2460-2853`。它会按 Redis Stream 反向扫描，默认 lookback 20000、page 2000，配置见 `infra/env/midterm.env:61-62`。找不到时会进入 `waiting_proof`，默认等 3 秒，每 0.5 秒轮询，见 `infra/env/midterm.env:47-51`；失败后还可能递延重试，逻辑见 `services/clip-worker/app/worker.py:3513-3738`。

这一步的设计目标是“证据可信”，但代价是明显的：每个 evidence 不再是“拿 keyframe 调 Replay 就完了”，而是先做一次跨 Redis/PTS/session 的证明。

## 5. frame annotation 本身是 best-effort，不是可靠证据队列

Savant hot path 里的 frame annotation exporter 通过 bounded async Redis writer 写流。这个 writer 明确是 drop-on-full，目的是不阻塞 `process_frame`，见 `modules/savant_security/custom/services/redis_stream_writer.py:27-33` 和 `modules/savant_security/custom/services/redis_stream_writer.py:103-127`。

这对推理吞吐是合理的，但对 evidence proof 不友好：一旦 Redis writer queue 满、frame annotation 丢了，clip-worker 就会等待 proof、重试、最后失败或过期。也就是说，这条链路是“推理优先，证据 proof 尽力而为”，不是一个严格可靠的 metadata commit log。

## 6. media-worker 也不是简单搬文件

Replay job 创建成功后并不等于 evidence 完成。media-worker 还要：

1. 扫描 sink 输出目录，优先 active epoch，必要时 fallback `rglob`，见 `services/media-worker/app/worker.py:316-403`。
2. 等 `metadata.json` 和视频文件出现并可 probe，见 `services/media-worker/app/worker.py:420-445`。
3. 等 sink video 稳定后才释放 Replay slot，见 `services/media-worker/app/worker.py:4521-4587` 和 `services/media-worker/app/worker.py:2209-2311`。
4. 复制/裁剪 raw clip，复制 sink metadata，读/写 annotation sidecar，跑 duration/window/epoch guard，见 `services/media-worker/app/worker.py:3811-4353`。
5. 写 `evidence_bundles` 和 artifact/timeline/overlay index，见 `services/media-worker/app/evidence_db_index.py:23-194`。

midterm 虽然开了 `POST_SAVANT_FAST_RAW_CLIP_ENABLED=true`，避免了严格裁剪的一部分成本，但 finalizer 仍要复制文件、构建 sidecar、读 metadata、做 guard 和 DB 写入，见 `services/media-worker/app/worker.py:3879-4021`、`services/media-worker/app/worker.py:4192-4278`。

## 7. “看起来 Replay 慢”的一部分其实是 slot 释放晚

clip-worker 的 Replay admission slot 不是在 `/api/v1/job` 返回后释放，而是在 media-worker 看到 sink video 稳定后释放，见 `services/media-worker/app/worker.py:2209-2311`。clip-worker 创建 job 后会把 slot hold 时间写入诊断，并记录 completion-aware release mode，见 `services/clip-worker/app/worker.py:4102-4186`。

这意味着任何下游问题都会反向占住上游 Replay slot：

- video-file-sink 写文件慢；
- metadata 出来慢；
- media-worker poll 慢；
- ffprobe/文件稳定检查慢；
- finalizer backlog 高；
- DB 更新慢。

这些都会表现为“新的 Replay job 进不去”，但根因不一定在 Replay API。

## 8. deadline 设计会让排队变成过期

event-worker 给 evidence task 设置 replay/annotation/materialization deadline，`materialization_deadline_at` 是 replay TTL 和 annotation TTL 的较小值，见 `services/event-worker/app/repository.py:591-604`。clip-worker 每 30 秒扫一次，把 pending/deferred/waiting/replay/finalizing 等状态中超过 deadline 的任务标成 `materialization_expired`，见 `services/clip-worker/app/worker.py:3193-3203` 和 `services/clip-worker/app/repository.py:916-996`。

所以在密集事件场景里，队列如果因为 per-source slot、proof wait、sink/finalizer backlog 被拖住，后面的任务会直接过期。用户感知就是“证据特别难保存”，因为系统选择了可信 deadline，而不是无限排队。

## 9. 和官方 Savant/Replay 的差距

当前用 Replay 导出 raw clip 的方式和官方思路并不离谱；真正的差距在应用层证据语义。官方 Replay 只负责缓存/回放/重推流，不负责 `evidence_tasks` 状态机、跨 Redis proof、runtime epoch guard、annotation sidecar、DB evidence index。

当前代码把“证据”定义成可审计业务对象，因此比官方裸 Replay 多了很多慢门：

- proof before replay；
- admission before job；
- sink stable before slot release；
- post-Savant sidecar rebuild；
- duration/window/epoch guard；
- DB index。

所以差距不是“我们没按官方 Replay 用”，而是“我们把官方 Replay 当作一个底座，上面叠了严格证据产品层”。这能提升可信度，但密集事件吞吐会比裸 Replay job 低很多。

## 10. 静态优先级判断

从代码逻辑看，最应该先怀疑的不是 keyframe lookup 或 Replay job create，而是：

1. `source_id` 维度是否把 60 路压成了少数几个 source，导致 per-source=1 串行。
2. `record_request_pending_ms` 是否持续增长，说明 Redis pending/admission 队列在堆。
3. `waiting_proof` / `missing_post_savant_frame_proof` 是否多，说明 frame annotation proof 赶不上。
4. `replay_active_source_count` / `max_concurrent_per_source_reached` 是否多，说明 slot 被 per-source gate 卡住。
5. `sink_video_to_stable_ms` / `finalizer_pool_wait_ms` / `finalization_duration_ms` 是否高，说明 media-worker 或 sink 释放 slot 慢。
6. `materialization_deadline_expired` 是否多，说明队列已经超过业务 deadline。

这些名字都已经在当前代码的 diagnostics/log 里写入，见 `services/clip-worker/app/worker.py:3271-3277`、`services/clip-worker/app/worker.py:4151-4173`、`services/media-worker/app/worker.py:4945-4985`。
