# Midterm 证据生成延迟与分片流水线改造方案

日期：2026-07-03

## 1. 背景

当前讨论的问题不是“证据是否最终能生成”，而是：

- 单摄像头触发事件后，8090 可见证据仍可能延迟约 30-60 秒；
- 60 路压测时，证据生成容易成为最大尾部瓶颈；
- 业务直觉认为 `30s` 冷却、`300s` Replay 缓存、`5s + 5s`
  证据窗口应当足够覆盖证据生成，但运行现象并不符合这个直觉。

本文件固化当前问题、现象、根因解释和后续代码 / 架构修改方向。

## 2. 当前证据链路

当前链路是事件驱动的 per-event Replay 证据模型：

```text
Savant / face-worker / event-worker
  -> PostgreSQL events / evidence_tasks
  -> Redis security.record_requests
  -> clip-worker
  -> Replay job
  -> video-file-sink 写出视频文件和 metadata
  -> media-worker 等文件稳定并 finalization
  -> evidence_bundles / 8090 DB-backed evidence API
```

这个模型不是“事件直接从 300s 环形缓存中切出 10s 文件”。每个可物化事件都会创建
Replay job，并等待 `video-file-sink` 产出、EOS、文件稳定可见，然后才由
`media-worker` 生成最终证据包。

## 3. 当前观测现象

2026-07-03 现场复核最近单源物化记录时，看到如下模式：

- 多数最近单源 evidence task 总耗时约 `31-32s`；
- 有一条 `watchlist_hit` 总耗时约 `62.5s`；
- 该 `watchlist_hit` 的分段为：
  - `task_created -> replay_slot_acquired`: `36.3s`;
  - `replay_slot_acquired -> replay_slot_released`: `25.2s`;
  - `finalization_duration_ms`: 约 `0.87s`。

这说明单摄像头约 1 分钟延迟的主要耗时不是最终 bundle 写入，也不是
`ffmpeg` finalization，而是：

1. 进入 Replay slot 前的排队 / admission / worker 认领等待；
2. Replay 到 `video-file-sink` 输出稳定之间的固定等待。

同一批最近样本还显示：

- 成功任务的 `replay_duration_seconds_effective` 约 `15s`；
- `sink_video_to_stable_ms` 常见约 `22-25s`；
- `finalization_duration_ms` 常见小于 `1s`。

7 天汇总也支持同一结论：

- `intrusion` 物化 p50 约 `45s`，p95 约 `196s`；
- `watchlist_hit` 物化 p50 约 `121s`，p95 约 `232s`；
- `sink_video_to_stable_ms` p95 在几十秒级；
- finalization p95 仅数秒级。

2026-07-03 修复前的代码和数据库复核确认了一个关键触发点：

- `services/clip-worker/app/replay_client.py` 的 Replay job payload 过去固定
  `configuration.ts_sync=true`；
- 修复前真实 Replay payload 也显示 `ts_sync=true`，同时
  `stop_condition.ts_delta_sec.max_delta_sec` 约 `15s`；
- `ts_sync=true` 会按原始 PTS 节奏重流，15 秒证据窗口天然接近 15 秒墙钟输出；
- `clip-worker` 发起 Replay job 后会 ack 并继续处理下一条 record request，并不是一直
  阻塞到 Replay 完成，所以优先级隔离仍然有意义；
- `media-worker` 当前仍以文件大小连续稳定作为视频可读判定，`metadata.json` 出现并不等价于
  视频已经 EOS、flush、可读。

最近 7 天分段分布显示，最大单项不是 finalization：

| event_type | replay_to_sink_metadata p50 | sink_video_to_stable p50 | sink_video_to_stable p95 | stable_to_probe p50 |
| --- | ---: | ---: | ---: | ---: |
| intrusion | 约 `2.4s` | 约 `24.6s` | 约 `59.8s` | 约 `46ms` |
| watchlist_hit | 约 `4.5s` | 约 `35.3s` | 约 `71.3s` | 约 `47ms` |

因此，当前“保存回放像按原始时间走一遍”的质疑是成立的：`ts_sync=true` 很可能是单路延迟中最不合理、且最先应该动手的默认行为。

2026-07-03 最新状态校正：`REPLAY_TS_SYNC=false` 已经落地并在当前机器压测中生效。
这意味着“按视频原始时长重放一遍”已经不是最新 200 秒长尾的主因。最新问题转移到了
Replay job 创建之前的 `clip-worker` 消费/排队阶段，以及仍然存在的 sink 文件稳定判定。

最新 60 路 8fps 压测：

```text
run_id=pressure60_fast_export_8fps_prepost_5_10_20_20260703T103559Z
artifact_dir=/data/video-analytics/artifacts/pressure60_fast_export_8fps_prepost_5_10_20_20260703T103559Z
status=passed
streams=60
fps=8/1
duration_s=300
policy_groups=5:5,10:10,20:20
kept_evidence=60
```

最新观测：

- 保留证据 `60/60` 的 Replay payload 都是 `configuration.ts_sync=false`；
- `video-file-sink` pipeline operation 已经是毫秒级：`video-file-sink-a` p95 约
  `80ms`，`video-file-sink-b` p95 约 `287ms`，最大约 `2.16s`；
- 但 retained evidence 的 `record_request_pending_ms` 仍有长尾：整体 p50 约
  `133s`，p95 约 `225s`，max 约 `233s`；
- `lifecycle_elapsed_ms` 仍有长尾：整体 p50 约 `167s`，p95 约 `264s`，max 约
  `275s`；
- `sink_video_to_stable_ms` 仍是秒级固定尾部：整体 p50 约 `15.7s`，p95 约
  `24.5s`，max 约 `38.4s`；
- 实际 `replay_slot_active_age_s` 不是 100 多秒：retained evidence p50 约
  `19.4s`，p95 约 `33.0s`，max 约 `48.6s`。

因此，最新 200 秒现象的解释不是“Replay 还在按 10/20/40 秒视频慢慢播放”，而是：

```text
record_request 在 Redis stream 中等待 clip-worker 处理
  -> clip-worker 单主循环逐条做 post-Savant frame proof
  -> 每条 proof wait 几秒，72 条累计约 258.8s
  -> 后面的 record_request 消息年龄自然变成 100-230s
  -> Replay/sink 实际导出已经很快，但开始得太晚
```

## 4. 为什么 30s 冷却和 300s 缓存没有自然解决问题

`30s` 冷却限制的是事件继续产生 record request 的频率；它不代表证据流水线能在
30 秒内消化已进入队列的 Replay job。

`300s` Replay TTL 代表原始素材在 Replay 中仍可查；它也不代表证据已经被切好、
落盘、稳定可读、并完成 8090 bundle 建索引。

当前实现里，`5s pre + 5s post` 的业务窗口还叠加了
`REPLAY_DURATION_EXTRA_SLACK_S=5`，所以每条证据的有效 Replay 时长常见约
`15s`，不是纯 `10s`。当 `ts_sync=true` 时，这个有效 Replay 时长还会直接变成
Replay 输出阶段的墙钟下限；换句话说，15 秒素材不是“尽快导出 15 秒内容”，而是“按 15 秒媒体节奏重流”。除此之外，每条证据还要支付：

- Replay job 创建和调度；
- `video-file-sink` 新 writer 写文件；
- metadata / video 文件可见；
- 文件大小稳定检查；
- media-worker 扫描和 DB claim；
- finalization 与 evidence bundle 写回。

因此 “60 路同时爆发 10s 证据 = 600s 视频，30s 冷却期间足够生成” 这个估算只在
“已有连续缓存、事件只做快速切片”的架构下成立。当前 per-event Replay 模型下，
每个事件都有固定调度和落盘稳定成本，不能按纯视频秒数线性估算。

## 5. 当前架构问题

### 5.1 证据流水线没有按摄像头组并行化

当前运行态曾出现 `REPLAY_SHARDS_JSON` / `REPLAY_SHARDS_CONFIG_PATH` 为空的情况，
这会使 `clip-worker` 退回 default Replay/sink 路径。即使代码已有
`REPLAY_SHARDS_JSON` / `REPLAY_SHARDS_CONFIG_PATH` 支持，如果运行时没有实际启用
shard map，多路证据仍会压到同一条 Replay / sink 出口。

需要避免的模型：

```text
60 cameras -> one record_request queue -> one clip-worker -> one Replay/sink/media path
```

目标模型：

```text
sources 00-09 -> evidence shard e00
sources 10-19 -> evidence shard e01
sources 20-29 -> evidence shard e02
sources 30-39 -> evidence shard e03
sources 40-49 -> evidence shard e04
sources 50-59 -> evidence shard e05
```

每个 evidence shard 包含自己的：

- `clip-worker-eXX`;
- `replay-eXX`;
- `video-file-sink-eXX`;
- `media-worker-eXX`;
- 独立 sink 输出目录和 processed state。

### 5.2 单摄像头高优先级事件可能被普通事件挡住

当前 admission / gate 有全局、每源、每 shard 限制，但高优先级事件不应被同一
摄像头上已经占位的普通 `intrusion` 长时间阻塞。

现象上，`watchlist_hit` 可能等待前一个同源任务释放 replay slot，导致用户看到
“名单命中证据也要 1 分钟”。

应该把同源 quota 拆成：

```text
normal source slot: intrusion / lower-value event
high-priority source slot: watchlist_hit / live_search_hit
```

至少保证同一摄像头同时允许：

- 1 个普通证据任务；
- 1 个高优先级证据任务。

### 5.3 多核 CPU 没有被证据链路自然线性利用

`clip-worker` 本身主要是调度 Replay job；真正慢的阶段通常是 Replay 输出、
`video-file-sink` 写文件 / EOS / 文件稳定、`media-worker` 扫描和 finalization。

如果所有事件共用一个 sink 或一个扫描根目录，多核 CPU 只能利用一部分，不能自动把
60 路证据分摊成多个独立出口。要利用多核，应拆成多个证据流水线和多个进程，而不是只
提高单 worker 内部并发。

### 5.4 最新 200 秒长尾的代码定位

最新压测里最大的延迟字段是 `record_request_pending_ms`，它不是视频生成耗时，而是
Redis Stream 消息年龄。

对应代码：

- `services/clip-worker/app/worker.py:3003-3008`：
  `clip_worker_claimed_at = _utc_now_iso()` 后立刻计算
  `record_request_pending_ms = _message_age_ms(msg_id)`；
- 这表示 record request 从写入 `security.record_requests` 到被 `clip-worker`
  当前 delivery 真正拿出来处理，已经过去了多少毫秒；
- 所以 `record_request_pending_ms=225000` 的含义是“请求排队/等待认领约 225 秒”，
  不是“Replay 导出视频花了 225 秒”。

`clip-worker` 当前消费模型仍然是单主循环逐批、逐条处理：

- `services/clip-worker/app/worker.py:2961-2964`：
  `XREADGROUP count=10` 读取 record requests；
- `services/clip-worker/app/worker.py:2971-2973`：
  对 batch 中每条 message 顺序执行；
- `services/clip-worker/app/worker.py:3278-3298`：
  每条 post-Savant evidence 在创建 Replay job 之前调用
  `_prepare_post_savant_replay_request()`；
- `services/clip-worker/app/worker.py:2338-2605`：
  `_prepare_post_savant_replay_request()` 在循环里查 frame annotation、找
  start/post window proof，并在未满足时按 `poll_interval_s` sleep；
- `services/clip-worker/app/worker.py:2602-2603`：
  proof 未就绪时会在同一个消费循环内 `time.sleep(sleep_s)`。

这形成了最新的主要阻塞：

```text
单个 clip-worker 主循环
  -> 处理 request A 时同步等 proof 约 2-6s
  -> request B/C/D... 只能继续留在 Redis stream
  -> 60 路突发下，后面的 msg_id age 累积到 100-230s
```

这轮 artifact 直接支持该判断：

- `clip_worker_phase_timing` 共 72 条；
- `proof_wait_ms` 总和约 `258.8s`，平均约 `3.59s`；
- `record_request_pending_ms` p50 约 `137s`，p95 约 `229s`，max 约 `235s`；
- `replay_job_create_ms` p50 约 `14ms`，p95 约 `24.5ms`；
- 也就是说，Replay job 创建本身很快，慢在创建之前的同步 proof wait 和队列老化。

另一个次要但会放大尾部的代码路径是 per-source concurrency：

- `infra/env/midterm.env:106`：
  `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE=1`；
- `services/clip-worker/app/worker.py:2798-2804`：
  同 source 已有 active replay slot 时返回
  `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE reached`；
- `services/clip-worker/app/worker.py:3133-3158`：
  concurrency gate 命中后调用 `_queue_clip_request(...)` 并 `continue`；
- `services/clip-worker/app/worker.py:1571-1635`：
  `_queue_clip_request()` 只写 DB diagnostics/status，不 ack Redis message；
- 该 message 之后依赖 pending reclaim 再次被取出，这会增加尾部抖动。

最新压测里该路径出现 26 次，reason 全部是
`max_concurrent_per_source_reached`。它不是 200 秒主因，但会让同源突发事件反复进入
pending/reclaim 路径。

最后，`replay_slot_hold_ms` 这个字段在当前报告里容易误读。它不是实际 Replay slot
持有时长，而是 timeout budget：

- `services/clip-worker/app/worker.py:317-327`：
  `timeout_budget_s = duration_s + poll_stability_s + sink_stability_budget_s + finalizer_budget_s + grace_s`；
- `services/clip-worker/app/worker.py:3631-3641`：
  `replay_slot_hold_ms` 直接由 `replay_slot_timing.timeout_budget_s` 计算；
- 实际释放由 `media-worker` 在 sink video stable 后执行：
  `services/media-worker/app/worker.py:1759-1850`。

因此，`replay_slot_hold_ms=114000/124000/134000` 不能解释成 Replay 真占用了
114-134 秒。最新 retained evidence 的实际 `replay_slot_active_age_s` p95 约
33 秒，max 约 49 秒。

### 5.5 最新问题-现象-代码对照表

| 问题 | 现象 | 对应代码 / 配置 |
| --- | --- | --- |
| record request 排队老化 | `record_request_pending_ms` p95 约 `225-229s` | `services/clip-worker/app/worker.py:3003-3008` 用 `_message_age_ms(msg_id)` 记录 Redis message age |
| clip-worker 单循环同步 proof wait | 72 条 job 的 `proof_wait_ms` 总和约 `258.8s` | `services/clip-worker/app/worker.py:2971-2973` 顺序处理；`:3278-3298` 创建 Replay 前等 proof；`:2602-2603` 在循环内 sleep |
| per-source gate 放大同源突发尾部 | 26 次 `max_concurrent_per_source_reached` | `infra/env/midterm.env:106` 每 source 1；`services/clip-worker/app/worker.py:2798-2804` gate；`:3133-3158` queue 后不 ack |
| sink 稳定仍是固定尾部 | `sink_video_to_stable_ms` p50 约 `15.7s`，p95 约 `24.5s` | `services/media-worker/app/worker.py:3883-3903` 文件大小连续稳定检查；`infra/env/midterm.env:110-111` poll=2s、checks=2 |
| `replay_slot_hold_ms` 容易误读 | 报告中约 `114/124/134s`，但实际 active age p95 约 `33s` | `services/clip-worker/app/worker.py:317-327` timeout budget；`:3631-3641` 写入字段；实际释放在 `services/media-worker/app/worker.py:1759-1850` |
| Replay 原速导出问题已缓解 | `ts_sync=false`，sink operation 毫秒级 | `infra/env/midterm.env:190`，`infra/docker-compose.midterm.yml:769`，`services/clip-worker/app/replay_client.py` payload `ts_sync` 开关 |

### 5.6 最新修正计划补充

这次最新分析的核心补充是：`REPLAY_TS_SYNC=false` 砍掉了第一层瓶颈后，不能立刻认为
Replay/sink 分片就是下一步唯一答案。当前最大长尾已经前移到 `clip-worker` 创建 Replay
job 之前，必须先把这一段从单主循环里并行化出来。

数字上可以闭环：

```text
72 条 job 的 proof_wait_ms 总和约 258.8s
record_request_pending_ms p50 约 137s
record_request_pending_ms max 约 235s
```

如果 72 条请求在压测初期密集到达，单个 `clip-worker` 顺序处理时，中位请求大约要等前面
一半请求的 proof wait 累加。`258.8s / 2` 与 `137s` 的 p50 非常接近，因此这是排队论
上可以解释的主因，而不是偶发抖动。

Phase 0b 有两条可落地路径：

**方案 A：横向扩 `clip-worker` consumer，优先推荐先试。**

`security.record_requests` 已经是 Redis Stream + consumer group。可以先启动多个
`clip-worker` consumer，让多个 consumer 并行执行 `_prepare_post_savant_replay_request()`
和 Replay job create，而不必等 Phase 2 完整拆分 high/normal stream、Replay、sink、
media-worker。

方案 A 的前置检查和改动点：

- 每个实例必须有唯一 `CONSUMER_NAME`；当前默认是
  `services/clip-worker/app/config.py:138-139` 的 `clip-worker-1`，横向扩容时不能复用；
- `active_replay_slot_counts()` 当前通过 PostgreSQL 统计 active slot，见
  `services/clip-worker/app/repository.py:205-265`，但它与后续
  `acquire_replay_slot()` 之间仍可能存在多进程 TOCTOU 竞态；
- 横向扩容前需要把 per-source/per-shard/global slot 获取改成原子 admission：
  同一 DB transaction 内完成 active count 判断和 slot acquire，必要时使用
  advisory lock、`SELECT ... FOR UPDATE` 或唯一/部分索引约束；
- 进程内的 `seen_requests`、`event_type_counts`、`jobs_created` 不能继续作为跨进程
  correctness 依据；多 consumer 后，这些只能作为本进程诊断，真实 quota 必须落到 DB
  或 Redis 原子结构；
- 如果原子 admission 未完成，不应直接生产启用多 consumer，否则两个 consumer 可能同时
  看到同一 source 空闲并同时开 job。

方案 A 的价值是改动路径短，并且是 Phase 2 N-shard 的子集：先证明多个 consumer 能并行
吃掉 proof wait，再决定是否继续拆 Replay/sink/media 出口。

**方案 B：单进程 bounded worker pool。**

主循环只做 `XREADGROUP`、基础解析、gate、分发和最终 ack/queue 决策；proof lookup 与
Replay job create 放入 bounded worker pool。建议初始并发 `8-16`：

```text
258.8s / 8  ~= 32s
258.8s / 16 ~= 16s
```

这足以覆盖“60 路 8fps 下 `record_request_pending_ms` p95 不超过 60s”的 Phase 0b
门槛，不需要一次性把证据前置阶段拉到 60 路全并发。

方案 B 的主要风险：

- ack 时机必须保持在 Replay job 创建成功或终态失败之后，不能在 worker pool 入队时提前
  ack；
- worker pool 满时，主循环必须停止读新消息或显式 backpressure，不能无界积压内存任务；
- exception、shutdown、pending reclaim、per-source gate 需要重新定义；
- 如果 admission 仍是“先 count、后 acquire”，线程池也会遇到与多进程相同的竞态，只是
  竞态范围从多进程变成多线程。

因此，最稳妥的顺序是：

1. 先实现 DB/Redis 原子 admission；
2. 再做多 consumer 或 bounded worker pool；
3. 再做 high/normal 优先级流和 N-shard。

### 5.7 `sink_video_to_stable_ms` 的二次可疑点

最新压测中，`video-file-sink` 的 pipeline operation 已经是毫秒级，但
`sink_video_to_stable_ms` 仍然是 p50 约 `15.7s`、p95 约 `24.5s`。如果两者测的是同一批
job，中间差距过大，不能只用 `MEDIA_POLL_INTERVAL_S=2` 和
`MIDTERM_SINK_STABILITY_CHECKS=2` 解释；理想的连续稳定检查通常不应自然扩到 15-25 秒。

当前可疑机制是：`media-worker` 的扫描和稳定性判定也可能形成二次串行队列。

对应代码：

- `services/media-worker/app/worker.py:3762-3786` 扫描 metadata files 并排序；
- `services/media-worker/app/worker.py:3834-3903` 逐个 meta/video 做文件存在和稳定检查；
- `services/media-worker/app/worker.py:3883-3892` 使用 `candidate_dirs` 记录文件 size 和
  stable count；
- `services/media-worker/app/worker.py:3904-3914` 达到稳定次数后才标记
  `sink_video_stable` 并释放 Replay slot；
- finalizer pool 在 `services/media-worker/app/worker.py:3787-3833` 后才接管，但稳定判定
  本身仍需要先被扫描循环发现。

需要补充一次低成本验证：

```text
对每个 sink output 记录：
- sink_metadata_first_seen_at
- sink_video_first_seen_at
- sink_video_stable_at
- 当时候选 candidate_dirs 数量
- 当时 metadata_files 待扫描数量
- scan_duration_ms
- stable_count 达标前经历的 poll 次数
```

判断方式：

- 如果 `sink_video_to_stable_ms` 与“待判定文件数 / candidate_dirs 数量”正相关，说明
  media-worker 稳定判定也被单循环排队拖长，需要和 clip-worker 一样并行化或分片化；
- 如果不相关，再转向检查输出目录所在存储介质、NFS/网络挂载、文件 flush/muxer finalize
  行为。

### 5.8 报告字段和 pending reclaim 修正

`replay_slot_hold_ms` 应从人读摘要中移除或改名。它当前是 timeout budget，不是实际占用。
后续报告建议：

- 新增或改名为 `replay_slot_timeout_budget_ms`；
- 人读摘要优先展示 `replay_slot_active_age_s`；
- 对比时使用 `replay_slot_acquired_at -> replay_slot_released_at`，不要再用
  `replay_slot_hold_ms` 代表真实耗时。

`max_concurrent_per_source_reached` 当前只出现 26 次，是次要因素；但主循环 proof wait 修完后，
同摄像头短时间多次触发会更容易暴露这个 gate。因此 Phase 0b 同时要把 pending reclaim
指标固化：

- `CLIP_WORKER_PENDING_CLAIM_MIN_IDLE_MS`;
- `CLIP_WORKER_PENDING_CLAIM_INTERVAL_S`;
- 每次 `_queue_clip_request()` 后到下一次成功处理的 reclaim wait；
- per-source gate 命中次数、重试次数、最终成功/失败状态；
- high-priority event 是否被 normal event 的 source slot 阻挡。

## 6. 设计原则

1. 不把“每个摄像头一个完整 worker 栈”作为默认方案。这样资源碎片化严重，运维复杂。
2. 不让 60 路共享单个证据出口。单 sink / 单 media scan 根目录会形成长尾。
3. 以实测 sink 并发承载反推分片大小；`5-10` 路只是初始假设，不是固定规则。
4. 高优先级事件必须有独立通道，不能被普通事件的 source slot 阻塞。
5. admission/backpressure 必须保留。60 路事件风暴时，低价值事件应可解释地
   `materialization_skipped`，而不是无限排队到 TTL 过期。
6. 最终目标是连续缓存切片，而不是永远依赖 per-event Replay job。

## 7. 阶段化改造方案

### Phase 0 - 单路延迟快速降噪

目标：先把单摄像头常见可见延迟从 `30-60s` 压低，并明确每一段耗时。

建议：

- 默认将证据导出的 `REPLAY_TS_SYNC` 改为 `false`，让 Replay 按 sink 背压尽快导出；
  保留 `REPLAY_TS_SYNC=true` 作为真人实时观看 / 回归排查的显式开关；
- 用同一 source、同一事件窗口做 `REPLAY_TS_SYNC=false/true` A/B，重点比较
  `replay_to_sink_metadata_ms`、`sink_video_to_stable_ms`、raw clip duration、decode
  error、8090 可播放性；
- 继续拆解 `sink_video_to_stable_ms`：区分 EOS/muxer flush、文件稳定轮询延迟、存储写入耗时；
- 将完成判定从“文件大小轮询稳定”推进到“明确完成信号”：优先使用 sink 完成 metadata/EOS/done marker；在没有完成信号前，稳定轮询只能作为保守 fallback；
- 将 `REPLAY_DURATION_EXTRA_SLACK_S` 从 `5` 做 A/B 降到 `0`；
- 将 `clip-worker` 的 `POLL_TIMEOUT_MS` 从 `5000` 做 A/B 降到 `1000`；
- 将 `MEDIA_POLL_INTERVAL_S` 从 `2` 做 A/B 降到 `1`；
- 对 `MIDTERM_SINK_STABILITY_CHECKS=1` 做受控试验，但必须用 `ffprobe`、
  raw clip 可播放性和 duration guard 验证，不能直接作为默认值；
- 将 `clip-worker` 的 post-Savant proof wait 从主消费循环中移出，或至少在高压时启用
  proof fast-path，避免每条 record request 在单循环内同步 sleep；
- 优先试验多 `clip-worker` consumer 或 bounded worker pool；无论选哪条路径，都必须先把
  global/shard/source admission 做成 DB/Redis 原子操作，避免并发创建 Replay job 的竞态；
- 若采用多 consumer，必须为每个实例设置唯一 `CONSUMER_NAME`，并将跨进程 quota 从进程内
  `event_type_counts` / `jobs_created` 迁移到 DB 或 Redis 原子结构；
- 若采用 bounded worker pool，主 loop 只做读取、gate、分发和 ack/queue 决策，proof 查找
  与 Replay job create 由 bounded pool 并发执行；worker pool 满时必须 backpressure；
- 将 `record_request_pending_ms` 拆成更精确的阶段：Redis message age、主循环等待、
  proof wait、Replay job create、Replay slot active age，避免继续把所有前置等待都归为
  “证据生成慢”；
- 把 `replay_slot_hold_ms` 从人读摘要中改名为 `replay_slot_timeout_budget_ms`，并把真实耗时
  切换为 `replay_slot_active_age_s`；
- 为 `media-worker` sink 稳定判定增加 candidate backlog / poll count / scan duration 指标，
  判断 `sink_video_to_stable_ms` 是否也被单循环扫描放大；
- 压测报告必须继续输出：
  - `record_request_pending_ms`;
  - `proof_wait_ms`;
  - `replay_job_create_ms`;
  - `replay_to_sink_metadata_ms`;
  - `sink_video_to_stable_ms`;
  - `finalizer_pool_wait_ms`;
  - `finalization_duration_ms`。

验收：

```text
PASS_SINGLE_SOURCE_EVIDENCE_LATENCY_SPLIT_REDUCED
```

建议门槛：

- 单摄像头 `watchlist_hit` p50 ready latency <= `30s`;
- 60 路 8fps 下 `record_request_pending_ms` p95 不再超过 `60s`；
- `proof_wait_ms` 不再在单个 clip-worker 主消费循环内线性累积；
- 多 consumer / worker pool 并发下不出现同 source 双 job 竞态；
- `sink_video_to_stable_ms` 能解释为实际文件稳定/flush，或证明是 media-worker 扫描队列导致；
- `finalization_duration_ms` 不回退；
- raw clip 可播放，duration guard 通过；
- 8090 list/detail 数据库索引正常。

2026-07-03 已落地的第一步代码改动：

- 新增 `REPLAY_TS_SYNC` 开关；
- midterm 默认 `REPLAY_TS_SYNC=false`；
- `build_job_payload()` 和 `ReplayClient.create_job()` 都保留显式 `ts_sync=True`
  回退能力；
- constant-cadence 兼容字段暂不移除，避免旧 Replay API 兼容性一次性回退。

运行态验证：

- 手工向当前 `source_00000000-0000-4000-8000-781078565686` 发起一个 15s
  `ts_delta_sec` Replay job；
- Replay 日志确认 payload 为 `ts_sync: false`，且同一秒内 `finished by stop condition`；
- `video-file-sink` 日志显示该 job 的 pipeline operation 为 `0:00:00.029478`；
- `ffprobe` 显示生成的 `video.mov` duration 为 `15.055s`；
- 验证目录已在 probe 后删除，避免没有 DB event 的手工产物被 `media-worker` 反复扫描。

这说明 `ts_sync=false` 后，15 秒证据内容不再需要按 15 秒墙钟重流；后续剩余尾部应优先看
worker 排队、文件稳定判定、目录扫描和最终证据建索引。

### Phase 1 - 高优先级事件不被普通事件阻塞

目标：解决单摄像头 `watchlist_hit` 被同源 `intrusion` 挡住的问题。

代码方向：

1. `event-worker` admission 改为 priority-aware source limit：

```text
EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE_NORMAL=1
EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE_HIGH=1
```

2. `clip-worker` gate 改为 priority-aware source concurrency：

```text
EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE_NORMAL=1
EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE_HIGH=1
```

3. admission decision / quota decision 写入明确字段：

```json
{
  "scope": "source_priority",
  "priority_class": "high",
  "limit": 1,
  "observed": 0,
  "source_limit_bypassed_for_priority": true
}
```

4. 保留高优先级事件类型默认值：

```text
watchlist_hit,live_search_hit
```

验收：

```text
PASS_PRIORITY_EVIDENCE_SOURCE_SLOT_RESERVED
```

建议测试：

- 同一 source 先来 `intrusion`，再来 `watchlist_hit`，`watchlist_hit` 不应因普通
  source slot 被占用而 `materialization_skipped`；
- 同一 source 连续多个 `watchlist_hit` 仍受 high-priority source limit 保护；
- 低优先级事件仍可在压力下被 `materialization_skipped`，并有可解释 reason。

### Phase 2 - N 路 evidence shard

目标：把 60 路证据生成拆成多个小流水线，真实利用多核 CPU、多进程和多 sink 出口。
每个 shard 的 source 数必须由 `video-file-sink` 和 media-worker 的实测并发承载反推，
而不是简单平均分配。

建议拓扑：

```text
source group e00 -> clip-worker-e00 -> replay-e00 -> video-file-sink-e00 -> media-worker-e00
source group e01 -> clip-worker-e01 -> replay-e01 -> video-file-sink-e01 -> media-worker-e01
...
```

配置新增：

```text
EVIDENCE_SHARD_ENABLED=true
EVIDENCE_SOURCES_PER_SHARD=8
EVIDENCE_SHARD_COUNT=auto
EVIDENCE_SHARD_HIGH_STREAM_SUFFIX=.high
EVIDENCE_SHARD_NORMAL_STREAM_SUFFIX=.normal
```

`runtime_topology` 需要生成：

- `replay_shards.topology.json`;
- `sources.generated.yml` 中每个 source 的 `replay_shard_id`;
- 每个 shard 的 replay API、in-stream endpoint、job sink endpoint；
- 每个 shard 的 sink output subdir；
- 每个 shard 的 worker container 参数。

建议 Redis stream 从单流：

```text
security.record_requests
```

改为分片和优先级流：

```text
security.record_requests.e00.high
security.record_requests.e00.normal
security.record_requests.e01.high
security.record_requests.e01.normal
```

`event-worker` 根据 `source_id -> evidence_shard_id` 路由 record request。

`clip-worker-eXX` 只消费本 shard 的 high/normal 两条流：

1. 优先 `XREADGROUP` high；
2. high 空时再读 normal；
3. pending reclaim 也按 high 优先；
4. 日志和 DB diagnostics 记录 `evidence_shard_id`、`stream_name`、
   `priority_class`。

Phase 2 落地前必须先验证 shard 内部不是新的串行瓶颈：

- 单个 `video-file-sink-eXX` 在 `REPLAY_TS_SYNC=false` 下同时接收 N 个 Replay job 时，
  `sink_video_to_stable_ms` 是否近似稳定，还是随 N 线性变长；
- 单个 `media-worker-eXX` 扫描独立 shard 输出目录时，是否会因目录规模、state 文件或
  finalizer worker 数形成新排队；
- 如果 shard 内仍串行，应继续拆小 shard 或增加 sink/media 实例，而不是只增加
  `clip-worker` 并发。

`media-worker-eXX` 只扫描：

```text
/media/replay-sink-output/midterm/epochs/<epoch>/shards/eXX/
```

并使用独立 state：

```text
/media/replay-sink-output/midterm/.media-worker.eXX.processed.json
```

DB 侧复用已有字段：

- `evidence_tasks.replay_shard_id`;
- `evidence_tasks.replay_api_url`;
- `evidence_tasks.replay_job_sink_url`;
- `evidence_tasks.replay_source_id`;
- `evidence_tasks.replay_slot_*`;
- `evidence_tasks.sink_video_to_stable_ms`;
- `evidence_tasks.finalization_duration_ms`。

验收：

```text
PASS_EVIDENCE_N_SHARD_PIPELINE_READY
```

建议门槛：

- 60 路按 `5-10` 路一组分片；
- 每个 shard 有独立 Replay/sink/media worker；
- 压测 artifact 能显示每个 shard 的 source count、active slot、sink stable p95、
  finalization p95、skipped reason count；
- retained evidence 仍为 8090 可查、可播放、DB-backed；
- duplicate materialization 为 0；
- Redis pending/lag 收敛为 0。

### Phase 3 - 连续缓存切片

目标：让 `30s` 冷却、`300s` 缓存的业务直觉真正成立。

长期目标不是每个事件启动一个 Replay job，而是每个 shard 或每个 source 持续写
post-Savant rolling segments：

```text
source -> shard rolling segment cache
event -> record requested PTS/window
media-worker -> 从已有 segment 快速拼接/裁剪
```

这样事件证据生成从“Replay job + sink writer + 等稳定”变成“已有片段快速切片”，
per-event 固定开销显著下降。

需要新增：

- rolling segment manifest；
- source/shard/epoch/PTS 到 segment 的索引；
- segment TTL 与磁盘配额；
- window cut API；
- event window 到 segment cut 的一致性校验；
- GOP/keyframe 对齐策略：如果希望用 `-c copy` 快速 remux/裁剪，segment 边界必须落在
  GOP/keyframe 边界；否则会退回重编码，抵消 rolling cache 的核心收益；
- 8090 bundle 仍保持现有 DB-backed 语义。

验收：

```text
PASS_ROLLING_CACHE_EVIDENCE_CUTOVER_READY
```

建议门槛：

- 单源 `5s+5s` 证据 ready p50 <= `15s`;
- 60 路同时触发时，高优先级证据 p95 <= `60s`;
- 不依赖 per-event Replay writer；
- 证据 raw clip 仍可追溯到 Replay / segment authority；
- duration guard、epoch guard、overlay/timeline 对齐不回退。

## 8. 当前不建议的做法

不建议只做以下动作：

- 只把 `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY` 调大；
- 只增加 `MEDIA_WORKER_FINALIZER_WORKERS`；
- 只扩大 `EVIDENCE_REPLAY_TTL_SECONDS`；
- 只依赖 `30s` cooldown；
- 在 `clip-worker` 主循环仍同步 proof wait 时，先拆 high/normal stream；
- 只换更强 CPU。

这些动作可能改善局部数据，但无法解决“所有摄像头共享单证据出口”和“per-event Replay
固定成本”的架构问题。

## 9. 推荐实施顺序

1. Phase 0a：保持并验证 `REPLAY_TS_SYNC=false`，确认 Replay 导出不再按原始时长重流。
2. Phase 0b-1：先把 Replay admission 做成原子操作，覆盖 global/shard/source limit；
   多 consumer 或 worker pool 都依赖这个前置条件。
3. Phase 0b-2：修 `clip-worker` 主循环同步 proof wait。优先试多 `clip-worker`
   consumer；如果部署复杂，再做 bounded worker pool。目标是 proof lookup / Replay job
   create 不再在单个主循环内线性累积。
4. Phase 0b-3：同步修压测报告字段，把 `replay_slot_hold_ms` 改成人读安全的
   `replay_slot_timeout_budget_ms`，并默认展示 `replay_slot_active_age_s`。
5. Phase 0c：把 `sink_video_to_stable_ms` 拆成 EOS/flush、轮询延迟、存储写入、media-worker
   candidate backlog / scan queue 四段；确认它是否是第二个串行扫描瓶颈，再决定是否把
   media-worker 稳定判定并行化。
6. Phase 1：实现 high-priority source slot，优先修复单摄像头 `watchlist_hit`
   被普通 `intrusion` 阻塞。
7. Phase 2：按实测 sink/media 承载把 dual replay shard 泛化为 N evidence shard，
   每个 shard 一个 replay/sink/media/clip 小流水线。
8. Phase 3：设计并切换 rolling segment cache，让事件证据变为缓存切片，而不是
   per-event Replay job。

Phase 1 不建议抢在 Phase 0b 前面做。原因是只要 `clip-worker` 主循环仍然在同步
proof wait，即使 record requests 拆成 high/normal 两条流，已经进入主循环并正在等待 proof
的 normal request 仍会挡住后面的 high request。只有 proof wait 并发化之后，“优先读 high”
才会真正变成“立刻分配给空闲 worker”。

## 10. 结论

用户关于“一个证据生成流水线看 5-10 个视频源”的理解是正确方向。当前问题不是
`30s` 冷却或 `300s` 缓存数值本身太小，而是当前证据生成模型仍以 per-event Replay
job 和共享 sink/media 出口为主，导致单源也有 30-60 秒固定尾部，多源压测时进一步
放大。

短期第一优先级已经从 `REPLAY_TS_SYNC=false` 转移到 `clip-worker` 主消费循环：最新
60 路 8fps 证据长尾主要是 record request 在 Redis stream 中等待，以及每条请求在创建
Replay job 前同步做 post-Savant proof wait。下一步应先把 proof wait / Replay job create
从单循环中并发化或分片化，再做 priority-aware admission 和 N shard evidence pipeline。
长期仍应转向 rolling cache cut，才能让业务上的缓存窗口和冷却策略真正转化为稳定、
低延迟的证据生成能力。

## 11. Phase 0b-1 落地记录 - atomic Replay admission

日期：2026-07-03

本阶段只做 atomic admission，不启用多 `clip-worker` consumer，不做 bounded worker
pool，不拆 high/normal stream，也不泛化 N shard。

代码落点：

- `services/clip-worker/app/repository.py`
  - 新增 `try_acquire_replay_slot()`：在单条 SQL 语句内使用
    `pg_advisory_xact_lock(hashtext('clip_worker_replay_admission_v1'))`，完成
    active slot 计数、global/shard/source limit 判定、`evidence_tasks` active slot
    预占和 `events.payload.media` 回写；
  - 新增 `record_replay_job_for_slot()`：Replay job 创建成功后，把
    `replay_job_id` / `resulting_stream_id` 绑定到已预占的 active slot；
  - 继续保留 `release_timed_out_replay_slots()`，避免异常情况下 active slot 永久占用。
- `services/clip-worker/app/worker.py`
  - Replay job create 前调用 `try_acquire_replay_slot()`，atomic deny 时只 queue，不再创建
    Replay job；
  - Replay job create 成功后调用 `record_replay_job_for_slot()`；
  - Replay job create 返回空或抛异常时调用 `release_replay_slot()`，释放已预占 slot。
- `harness/tests/test_completion_aware_replay_admission.py`
  - 覆盖 atomic acquire 成功、atomic deny 不创建 job、job create 异常释放 slot、
    repository SQL 中 advisory lock / decision / reserved CTE 等行为。
- `harness/tests/test_clip_worker_queue_safety.py`
  - 测试 fake DB 适配 DB-backed active slot 计数，确保旧的 queue/concurrency 行为仍成立。

本阶段解决的是后续多 consumer / worker pool 的竞态地基：同一时刻多个 worker 不能再通过
“先 count 后 create job 再 acquire”的非原子窗口同时打穿同一 source slot。

校验：

```text
python -m compileall services/clip-worker/app
python -m compileall services/clip-worker/app \
  harness/tests/test_completion_aware_replay_admission.py \
  harness/tests/test_clip_worker_queue_safety.py
pytest -q harness/tests/test_completion_aware_replay_admission.py \
  harness/tests/test_clip_worker_queue_safety.py
docker compose -f infra/docker-compose.midterm.yml config
docker compose -f infra/docker-compose.midterm.yml up -d --no-build --force-recreate \
  --no-deps clip-worker media-worker
```

结果：

```text
compileall: passed
pytest: 40 passed
compose config: passed
clip-worker/media-worker recreate: passed
```

60 路 8fps 压测：

```text
run_id=pressure60_phase0b1_atomic_admission_8fps_prepost_5_10_20_20260703T114939Z
artifact_dir=/data/video-analytics/artifacts/pressure60_phase0b1_atomic_admission_8fps_prepost_5_10_20_20260703T114939Z
status=passed
streams=60
fps=8/1
duration_s=300
policy_groups=5:5,10:10,20:20
kept_evidence=60
warnings=validate_seq_iq_expected_sampling_gap
```

关键验收结果：

- retained evidence `60/60` 都是 `replay_slot_admission_mode=atomic`；
- retained evidence `60/60` 的 `replay_slot_status=released`，
  `replay_slot_release_reason=sink_video_stable`；
- retained evidence 中 `replay_active_source_count_before_create` 最大值为 `0`，
  未观察到同 source 双 active job；
- `media_worker.duplicate_materialization_count=0`；
- `event_worker.record_request_dedupe.duplicate_count=0`；
- Redis `security.record_requests` consumer group 收敛到 `pending=0`、`lag=0`；
- `failure_reasons=[]`，8090 failed evidence 列表为空；
- 全量 run 中 DB 侧曾出现 `6` 条 `materialization_failed`，但 retained evidence
  和最终验收通过，`media_worker.finalizer_failed_count=0`。

retained evidence 整体分布：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| `lifecycle_elapsed_ms` | `142246.5` | `243795.0` | `256708.0` |
| `record_request_pending_ms` | `106055.0` | `192881.8` | `201477.0` |
| `proof_wait_ms` | `2728.0` | `6231.4` | `6836.0` |
| `replay_job_create_ms` | `14.0` | `53.2` | `65.0` |
| `sink_video_to_stable_ms` | `15933.0` | `33150.4` | `40958.0` |
| `finalization_duration_ms` | `4860.5` | `9237.5` | `11753.0` |
| `replay_slot_active_age_s` | `21.8` | `44.0` | `48.5` |

按 evidence window 分组：

| group | count | lifecycle p95 ms | pending p95 ms | proof p95 ms | sink stable p95 ms | active slot p95 s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `5:5` | `18` | `243498.6` | `190323.5` | `2479.3` | `33155.1` | `44.2` |
| `10:10` | `24` | `238379.3` | `188748.4` | `4011.7` | `25119.9` | `38.0` |
| `20:20` | `18` | `247850.1` | `197034.9` | `6416.9` | `33116.2` | `42.5` |

结论：

Phase 0b-1 证明 atomic admission 地基已生效：Replay slot 判定和预占进入同一个 DB
原子边界，且压测中没有 duplicate materialization、Redis pending/lag 能收敛，也没有观察到
同 source 双 active job。它没有、也不应该解决 `record_request_pending_ms` 100-200 秒长尾；
该长尾仍然由单 `clip-worker` consumer 同步 proof wait 串行累积触发。下一阶段应进入
Phase 0b-2，把 proof wait / Replay job create 从单主循环中并行化。

## 12. Phase 0b-2 落地记录 - bounded multi-consumer + proof lookup cache

日期：2026-07-03

本阶段目标是修 `clip-worker` 单主循环同步 proof wait。实际验证过程说明：不能简单把
consumer 数拉大；如果 8 个 consumer 同时扫描 `security.frame_annotations`，会把 Redis
proof lookup 从几秒放大到几十秒，反而使 `record_request_pending_ms` 和 materialization
expired 增加。

代码落点：

- `services/clip-worker/main.py`
  - 新增同容器多 consumer 启动能力；
  - 每个 consumer 使用独立 `consumer_name`、Redis connection、PostgreSQL connection；
  - 默认从 `CLIP_WORKER_CONSUMER_COUNT` 读取并发数。
- `services/clip-worker/app/config.py`
  - 新增 `consumer_count`；
  - 新增 `frame_annotation_lookup_concurrency`。
- `services/clip-worker/app/worker.py`
  - 启动日志输出 consumer name 和 proof lookup 并发；
  - 对 proof lookup 增加共享 concurrency gate；
  - `_find_replay_frame_domain_proofs()` 对同一请求的 Redis range 只扫描一次；
  - 扫描后立即解析并过滤成当前 `runtime_epoch/source/camera` 的小列表，post/start/fallback
    proof 选择复用该列表，避免重复 JSON parse/filter。
- `infra/docker-compose.midterm.yml` / `infra/env/midterm.env`
  - 当前稳定配置为 `CLIP_WORKER_CONSUMER_COUNT=4`；
  - 当前稳定配置为 `CLIP_WORKER_FRAME_ANNOTATION_LOOKUP_CONCURRENCY=4`。

反例压测：

```text
run_id=pressure60_phase0b2_multi_consumer_8fps_prepost_5_10_20_20260703T132510Z
status=failed_pressure_gates
kept_evidence=39
failure_reasons=insufficient_playable_evidence
```

8 consumer 直接并行时，`proof_wait_ms` p50 约 `29.2s`，p95 约 `73.6s`，
`record_request_pending_ms` p95 约 `356.8s`。这证明问题不是“consumer 越多越好”，而是
frame proof lookup 本身会在并发扫描 Redis 大 stream 时形成新瓶颈。

最终采用的 4 consumer + parsed cache 压测：

```text
run_id=pressure60_phase0b2_4consumer_parsed_cache_8fps_prepost_5_10_20_20260703T142437Z
artifact_dir=/data/video-analytics/artifacts/pressure60_phase0b2_4consumer_parsed_cache_8fps_prepost_5_10_20_20260703T142437Z
status=passed
streams=60
fps=8/1
duration_s=300
policy_groups=5:5,10:10,20:20
kept_evidence=60
warnings=validate_seq_iq_expected_sampling_gap
```

关键验收结果：

- retained evidence `60/60` 都是 `replay_slot_admission_mode=atomic`；
- retained evidence 中 `replay_active_source_count_before_create` 最大值为 `0`；
- `event_worker.record_request_dedupe.duplicate_count=0`；
- `media_worker.duplicate_materialization_count=0`；
- `media_worker.finalizer_failed_count=0`；
- `failure_reasons=[]`；
- Redis `security.record_requests` 在报告采样时仍有 `pending=10`、`lag=28`，说明 Phase 0b-2
  仍未完全达到 backlog 收敛目标；
- run 内仍有 `materialization_pending=36`、`materialization_expired=2`，后续仍需继续压低
  proof lookup 和 sink/media 尾部。

retained evidence 整体分布：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| `lifecycle_elapsed_ms` | `159217.5` | `211999.6` | `226924.0` |
| `record_request_pending_ms` | `113489.5` | `174830.8` | `186184.0` |
| `proof_wait_ms` | `6528.0` | `11849.8` | `12053.0` |
| `replay_job_create_ms` | `36.0` | `94.0` | `135.0` |
| `sink_video_to_stable_ms` | `13510.0` | `45954.6` | `63550.0` |
| `finalization_duration_ms` | `4991.0` | `15248.9` | `19774.0` |
| `replay_slot_active_age_s` | `32.1` | `64.5` | `124.6` |

按 evidence window 分组：

| group | count | lifecycle p95 ms | pending p95 ms | proof p95 ms | sink stable p95 ms | active slot p95 s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `5:5` | `24` | `207486.0` | `176765.5` | `6634.6` | `45145.5` | `50.0` |
| `10:10` | `15` | `208902.1` | `159677.2` | `10409.2` | `39555.7` | `83.4` |
| `20:20` | `21` | `216839.0` | `155654.0` | `12040.4` | `28758.0` | `63.3` |

结论：

Phase 0b-2 已经证明 4 个 bounded consumer 比单 consumer 有改善：`lifecycle_elapsed_ms`
p95 从 Phase 0b-1 的约 `244s` 降到约 `212s`，且保持 retained evidence `60/60`、
duplicate materialization 为 `0`。但它没有把 `record_request_pending_ms` p95 压到
`60s` 以内，主要原因是 frame proof lookup 仍然需要按时间窗口扫描 Redis
`security.frame_annotations` 大 stream；即使做了单请求 scan/cache，单条 retained evidence 的
`proof_wait_ms` p95 仍约 `11.8s`。

下一步不能继续靠调大 consumer 数。应进入更实质的 proof lookup 优化：

- 为 `security.frame_annotations` 增加 source/session 级索引或侧路 stream；
- 或在 `clip-worker` 内维护按 `runtime_epoch_id/source_id/stream_session_id` 的短 TTL
  frame proof cache，多个同源事件复用一次 Redis scan；
- 同时进入 Phase 0c，拆解并修复本轮重新显著的 `sink_video_to_stable_ms` p95
  `45.9s` 尾部。

## 13. Phase 0b-3 / 0c / 0d 落地记录 - range cache、快速 ready、source 冷却

日期：2026-07-03

本轮继续追 `record_request_pending_ms` 为什么还存在。结论是：已经不是 Replay 按真实时间
吐视频，也不是文件稳定轮询；新的主因变成了“5 分钟压测持续产生多批同源事件”，同一 source
在 30 秒后又能进入下一轮 record_request，clip-worker/replay admission 仍要处理 90+ 个
Replay slot，而不是理想化的 60 个一次性 burst。

### 13.1 代码落点

- `services/clip-worker/app/worker.py`
  - 增加进程内 `security.frame_annotations` range TTL cache；
  - 多个 consumer 在同一时间窗口、不同 source 上找 proof 时，复用一次 Redis
    `XREVRANGE` 结果，避免 60 路 burst 把 Redis 大 stream 扫成 N 倍。
- `services/clip-worker/app/config.py`
  - 新增 `CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_TTL_S=0.75`；
  - 新增 `CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_BUCKET_MS=1000`；
  - 新增 `CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_MAX_ENTRIES=64`。
- `services/media-worker/app/worker.py`
  - `video.mov + metadata.json + ffprobe duration ready` 后立即视为 sink ready；
  - 不再必须等文件大小连续稳定轮询后才释放 Replay slot；
  - 保留稳定轮询作为 ffprobe 未 ready 时的 fallback。
- `services/event-worker/app/config.py` / `services/event-worker/app/worker.py`
  - 新增 `RECORDING_COOLDOWN_SCOPE`；
  - midterm 配置改为 `RECORDING_COOLDOWN_SCOPE=source`，即同一摄像头 30 秒内
    intrusion/watchlist 不再各自放一个 record_request；
  - `EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE` 从 `2` 收紧为 `1`，避免 event-worker
    放入两个同源 active 任务，而 clip-worker 又只能单源串行。

验证：

```text
compileall: passed
pytest: 87 passed
compose config: passed
event-worker/clip-worker/media-worker recreate: passed
git diff --check: passed
```

### 13.2 压测 A：range cache + 快速 ready + source admission=1

```text
run_id=pressure60_phase0c_fast_ready_admission1_8fps_prepost_5_10_20_20260703T145449Z
artifact_dir=/data/video-analytics/artifacts/pressure60_phase0c_fast_ready_admission1_8fps_prepost_5_10_20_20260703T145449Z
status=passed
streams=60
fps=8/1
duration_s=300
policy_groups=5:5,10:10,20:20
kept_evidence=60
warnings=validate_seq_iq_expected_sampling_gap
```

关键变化：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| `lifecycle_elapsed_ms` | `123536.0` | `241765.9` | `252818.0` |
| `record_request_pending_ms` | `110924.0` | `223820.8` | `236461.0` |
| `proof_wait_ms` | `8247.0` | `10578.8` | `10818.0` |
| `sink_video_to_stable_ms` | `45.0` | `53.1` | `59.0` |
| `finalization_duration_ms` | `5118.5` | `8778.1` | `9987.0` |

`sink_video_to_stable_ms` 从上一轮 p95 `45.9s` 直接降到约 `53ms`，说明“稳定性轮询”
确实是一个独立大头，已经被快速 ready 信号砍掉。

### 13.3 压测 B：source 级 recording cooldown

```text
run_id=pressure60_phase0d_source_cooldown_8fps_prepost_5_10_20_20260703T150720Z
artifact_dir=/data/video-analytics/artifacts/pressure60_phase0d_source_cooldown_8fps_prepost_5_10_20_20260703T150720Z
status=passed
streams=60
fps=8/1
duration_s=300
policy_groups=5:5,10:10,20:20
kept_evidence=60
warnings=validate_seq_iq_expected_sampling_gap
```

关键变化：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| `lifecycle_elapsed_ms` | `101436.5` | `165400.1` | `175030.0` |
| `record_request_pending_ms` | `73311.5` | `150724.4` | `158992.0` |
| `proof_wait_ms` | `8599.0` | `11027.0` | `12103.0` |
| `replay_job_create_ms` | `33.0` | `68.0` | `117.0` |
| `sink_video_to_stable_ms` | `46.0` | `57.3` | `82.0` |
| `finalization_duration_ms` | `4494.5` | `9739.2` | `10998.0` |

DB 侧事件量从上一轮 `771` 降到 `257`，说明 source 级 recording cooldown 生效；
但 `record_request_pending_ms` p95 仍约 `150s`，没有到目标 `60s`。这说明剩余瓶颈已经不是
文件稳定，也不主要是 Replay 导出，而是 admission 队列仍在 5 分钟持续压测下处理多轮
同源事件。

### 13.4 当前剩余问题

最新日志还暴露了一个需要继续修的 race：少数任务在 media-worker 已经记录
`replay_slot_released release_reason=sink_video_stable` 后，最终报告里仍变成
`replay_slot_status=timeout`。典型现象是：

- media-worker 日志在 `15:10:21` 已释放 slot，`sink_video_to_stable_ms=49`；
- retained evidence payload 最终却显示 `replay_slot_release_reason=timeout`，
  `replay_slot_released_at=15:12:43`；
- 这会让 admission 误以为 slot 仍 active 到 fallback deadline，继续拉长
  `record_request_pending_ms`。

下一步应优先修这个 slot release/timeout 覆盖 race，并把压测报告中
`replay_slot_hold_ms` 彻底改名为 timeout budget，避免继续误读。

## 14. Phase 0e/0f/0g: terminal slot、clip 多核与并发上限

### 14.1 terminal slot 不能再回到队列

修正点：

- `services/clip-worker/app/repository.py::try_acquire_replay_slot()` 已把
  `released` / `timeout` 视为 terminal slot，CAS admission 不再重新写回
  `active`。
- `services/clip-worker/app/worker.py` 在 atomic admission 返回
  `replay_slot_terminal_state` 时直接 `XACK` 当前 Redis message，不再调用
  `_queue_clip_request()` 把已经 terminal 的 slot 重新写成 pending/deferred。
- 新增回归测试：
  `harness/tests/test_completion_aware_replay_admission.py::test_terminal_replay_slot_admission_is_acked_without_queue`。

对应压测：

```text
run_id=pressure60_phase0f_terminal_slot_ack_8fps_prepost_5_10_20_20260703T160407Z
status=passed
kept_evidence=60
```

结果说明：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| `record_request_pending_ms` | `106778.5` | `185911.5` | `210152` |
| `proof_wait_ms` | `8671` | `13612.7` | `20052` |
| `sink_video_to_stable_ms` | `48` | `56` | `28020` |
| `finalizer_pool_wait_ms` | `102` | `14743` | `20538` |

terminal-slot 重排队被压住了，但 p95 仍在百秒级，说明剩余主因不是这条
race，而是 clip-worker 仍没有真正利用多核。

### 14.2 clip-worker 线程并发不够，要用多进程

`services/clip-worker/main.py` 原来的 `CLIP_WORKER_CONSUMER_COUNT=4` 是同进程
多线程。压测中 clip-worker CPU 长期约 `100%`，等价于只吃一个核；这解释了
为什么 proof wait 已经只有几秒，`record_request_pending_ms` 仍不断累积。

修正点：

- `services/clip-worker/main.py` 改为按 consumer 启动多个 `multiprocessing.Process`。
- 当前保守配置：
  - `CLIP_WORKER_CONSUMER_COUNT=8`
  - `CLIP_WORKER_FRAME_ANNOTATION_LOOKUP_CONCURRENCY=8`
- 8 个 consumer 对 60 路约等于每个 consumer 负责 7-8 路，符合“每条流水线负责
  8-10 路摄像头”的目标区间。

对应压测：

```text
run_id=pressure60_phase0g_clip_multiprocess8_8fps_prepost_5_10_20_20260703T161813Z
status=passed
kept_evidence=60
```

结果说明：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| `record_request_pending_ms` | `24787` | `73569.8` | `97213` |
| `proof_wait_ms` | `3253` | `6118.8` | `7946` |
| `replay_job_create_ms` | `20` | `40.4` | `377` |
| `sink_video_to_stable_ms` | `48` | `59` | `83` |
| `finalizer_pool_wait_ms` | `9464` | `54207.9` | `63300` |

这轮是本阶段最好的平衡点：clip-worker CPU 提升到约 `667%`，证明多核已被利用；
`record_request_pending_ms` p95 从 `185.9s` 降到 `73.6s`。仍未完全进入 `60s`
目标，剩余 tail 主要来自 Replay/sink 活跃 slot 饱和和 media finalizer 排队。

### 14.3 不应继续盲目加本机并发

尝试把 clip consumer 从 8 提到 10，并把 media finalizer 从 4 提到 8：

```text
run_id=pressure60_phase0h_clip10_media8_8fps_prepost_5_10_20_20260703T162831Z
status=passed
kept_evidence=60
```

结果反而恶化：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| `record_request_pending_ms` | `36133.5` | `203170.2` | `285477` |
| `proof_wait_ms` | `3981.5` | `7555.25` | `10108` |
| `replay_to_sink_metadata_ms` | `51290` | `72100.1` | `73873` |
| `finalizer_pool_wait_ms` | `866` | `29687.25` | `34384` |

clip-worker 日志中 `max_concurrent_reached` 从 8-consumer 轮的 `257` 次增至
`1131` 次，说明并发已把 `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY=18` 和
Replay/sink 出口打满。继续加 consumer 只会制造 admission retry storm，不会缩短
证据产生时间。

随后验证 `clip=8, media=8`：

```text
run_id=pressure60_phase0i_clip8_media8_8fps_prepost_5_10_20_20260703T163855Z
status=passed
kept_evidence=60
```

`record_request_pending_ms p95=81113.55`，`finalizer_pool_wait_ms p95=57611.2`，
仍不如 `clip=8, media=4`。因此当前保留 `clip=8 多进程`，media finalizer 维持
原来的 `4 workers / 4 threads`。

### 14.4 当前结论

已经修掉的：

- Replay 不再按原始时间走一遍：`REPLAY_TS_SYNC=false`。
- 文件 ready 不再靠轮询稳定：`video + metadata + ffprobe duration ready` 即可释放。
- terminal slot 不再被重新 queue。
- clip-worker 从多线程改为多进程，开始真实利用多核。

仍存在的：

- `record_request_pending_ms p95` 最好实测约 `73.6s`，还略高于 `60s` 目标。
- 盲目增加本机 consumer 或 finalizer 会触发 Replay/sink/global active slot 饱和，
  tail 反而恶化。

下一步不应继续调大单容器并发，而应进入 Phase 2 分片：

- 以 6-8 个 Replay/sink shard 承接 60 路，每 shard 约 8-10 路摄像头。
- admission 计数和 Redis consumer 按 shard/source 路由，避免 60 路共享同一组
  Replay/sink 出口。
- 每个 shard 内继续保留 per-source active=1，避免同源证据相互挤占。

## 15. Phase 2.0.5: artifact 分布诊断

在正式改 Phase 2 shard 拓扑前，先补了 artifact 级分析工具，避免在“热点 source
自己排队”和“全局 / shard 出口瓶颈”之间靠猜。

新增工具：

```text
scripts/tools/analyze_midterm_pressure_artifact.py
```

输入为一次 pressure artifact 目录，输出 JSON，包含：

- `record_request_pending_ms` 总体分布，以及 retained evidence 按 source / shard
  的分布；
- `media_worker.queue_wait_ms` 总体分布，以及按 source / shard 的分布；
- `replay_to_sink_metadata_ms`、`sink_video_to_stable_ms`、
  `finalizer_pool_wait_ms` 等 media 分段；
- `max_concurrent_reached`、`max_concurrent_per_source_reached`、
  `max_concurrent_per_shard_reached`、`clip_worker_queued`、
  `replay_slot_terminal_state` 日志计数；
- 每个 retained source 的 evidence count；
- 自动诊断结论：`global_or_shard_outlet_bottleneck_likely` /
  `clip_or_admission_queue_bottleneck_likely` /
  `hot_source_self_queue_likely` / `mixed_or_inconclusive`。

已对当前最佳 baseline 重新分析：

```text
artifact_dir=/data/video-analytics/artifacts/pressure60_phase0g_clip_multiprocess8_8fps_prepost_5_10_20_20260703T161813Z
analysis=/data/video-analytics/artifacts/pressure60_phase0g_clip_multiprocess8_8fps_prepost_5_10_20_20260703T161813Z/pressure_artifact_analysis.json
diagnosis=global_or_shard_outlet_bottleneck_likely
```

关键分布：

| metric | count / scope | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| `record_request_pending_ms` | `138` | `24787` | `73569.8` | `97213` |
| `media_worker.queue_wait_ms` | `108` | `68060.5` | `127197.55` | `134987` |
| `media_worker.queue_wait_ms replay-a` | `58` | `68450.5` | `130180.4` | `134987` |
| `media_worker.queue_wait_ms replay-b` | `50` | `67619` | `119383.2` | `130236` |
| `sink_video_to_stable_ms` | `219` | `48` | `59` | `83` |
| `finalizer_pool_wait_ms` | `108` | `9464` | `54207.9` | `63300` |

source 分布结论：

- `media_worker.queue_wait_ms` 有 `56` 个 source 被测到；
- 其中 `53` 个 source 的 max 超过 `60s`；
- tail source fraction 约 `94.6%`；
- tail 同时出现在 `replay-a` 和 `replay-b`，不是一两个热点摄像头自己卡自己。

日志计数：

| marker | count |
| --- | ---: |
| `clip_worker_queued` | `433` |
| `max_concurrent_reached` | `257` |
| `max_concurrent_per_source_reached` | `141` |
| `max_concurrent_per_shard_reached` | `35` |
| `replay_slot_terminal_state` | `5` |

因此，Claude 提到的“先看 source 分布再决定分片是否对症”是对的；本轮数据支持
继续 Phase 2 分片，而不是回头只做同源合并策略。当前尾部不是集中在少数 source，
而是 2 个 replay shard 都有广泛 queue wait，说明出口 / finalizer 资源仍然太粗。

下一步进入 Phase 2.1 前要保持两个约束：

- 不使用 `hash(source_id) % shard_count` 作为长期路由；先实现稳定 manifest /
  mapping version，避免 2 -> 4 -> 6/8 时 source 重新洗牌造成孤儿状态。
- 切换 shard_count 前必须先 drain：停止新 record request，等待 active evidence /
  replay slot / media materialization 清空，再 recreate 对应服务。

## 16. Phase 2.1/2.2: 稳定 manifest 与 drain 检查

Phase 2.1 先补了稳定 shard manifest 的诊断字段，而不是直接用
`hash(source_id) % shard_count`。当前 `REPLAY_SHARDS_JSON` 仍是显式
`source_ids` 列表，新增 `mapping_version` 字段，用来标记这批 source -> shard
归属。

代码变化：

- `services/clip-worker/app/replay_shards.py`
  - `ReplayShardMap` 增加 `mapping_version`；
  - `parse_replay_shard_map()` 接受 `mapping_version` / `version`；
  - 默认单 shard 配置使用 `implicit-default`。
- `services/clip-worker/app/worker.py`
  - `replay_shard` diagnostics 中写入 `mapping_version`；
  - 每条 record request 消费时记录：
    - `record_request_shard_id`
    - `consumer_resolved_shard_id`
    - `record_request_shard_mapping_version`
    - `shard_mapping_version`
    - `replay_shard_mapping_mismatch`
  - 如果 request 产生时 shard 和消费时当前 manifest 解析出的 shard 不一致，记录
    `replay_shard_mapping_mismatch` warning。
  - Replay job labels 也写入 `record_request_shard_id`、
    `consumer_resolved_shard_id`、`shard_mapping_version`，让 sink metadata 侧可回溯。
- `services/event-worker/app/record_request.py`
  - 如果事件 / media payload 中已有 `record_request_shard_id`、
    `replay_shard_id`、`shard_mapping_version` 或
    `replay_shard_mapping_version`，会保留到 Redis record_request。
- `scripts/runtime/run_midterm_pressure60.py`
  - dual-shard pressure manifest 写入
    `mapping_version=<run_id>:dual-shard:v1`；
  - pressure artifact 的 `clip_worker_replay_shards_pressure.json` 记录
    mapping version。

Phase 2.2 同步增加 drain 检查工具：

```text
scripts/tools/check_midterm_evidence_drain.py
```

用途是在 2 -> 4 -> 6/8 shard 切换前显式确认系统已经排空。它检查：

- `evidence_tasks` 中 active materialization 状态；
- `evidence_tasks.replay_slot_status='active'` 且未过 deadline 的 slot；
- `events.payload.media` 中仍处于 active 的 clip / materialization 状态
  （仅作为历史状态诊断，不作为 drain 阻断条件）；
- Redis `security.record_requests` consumer group pending / lag。

推荐 shard 切换 SOP：

```text
1. 停止新 record_request 摄入：
   - 停止 pressure source / quiesce runtime sources；
   - 或临时停止 event-worker。

2. 等待 drain：
   python scripts/tools/check_midterm_evidence_drain.py \
     --wait --timeout-s 600 --poll-s 5

3. drain_complete=true 后再切换 REPLAY_SHARDS_JSON / shard_count。

4. recreate 对应服务，不 rebuild：
   docker compose --env-file infra/env/midterm.env \
     -f infra/docker-compose.midterm.yml \
     up -d --no-build --force-recreate --no-deps \
     clip-worker media-worker replay-* video-file-sink-*

5. 压测 artifact 中检查 mapping mismatch：
   - `replay_shard_mapping_mismatch=false`
   - request-time shard 与 consumer-time shard 一致。
```

已加回归测试：

- `test_replay_job_routes_to_source_shard`
- `test_replay_job_records_request_consumer_shard_mismatch`
- `test_record_request_preserves_replay_shard_manifest_fields`
- `test_write_dual_shard_pressure_sources_splits_sources_30_30`
- `test_midterm_evidence_drain_check`

当前验证：

```text
pytest -q \
  harness/tests/test_midterm_pressure_artifact_analyzer.py \
  harness/tests/test_midterm_evidence_drain_check.py \
  harness/tests/test_video_file_sink_pressure_parser.py \
  harness/tests/test_midterm_pressure60_script.py \
  harness/tests/test_clip_worker_queue_safety.py \
  harness/tests/test_event_worker_recording_policy.py

75 passed
```

## 17. Phase 2.3: 单 shard 容量探针支持

为了先测出单个 Replay/sink shard 在当前机器上的实际饱和点，pressure runner 增加了：

```text
--dual-shard-source-mode balanced|all-a|all-b
```

默认 `balanced` 保持原来的 30/30。`all-a` 或 `all-b` 会把本轮 pressure source
全部放到一个 replay shard 上，用于小规模容量探针，例如：

```text
python scripts/runtime/run_midterm_pressure60.py \
  --fps 8/1 --min-fps 2/1 --streams 10 --duration-s 120 --drain-s 180 \
  --keep-evidence 20 --evidence-group-size 10 \
  --evidence-policy-groups 5:5,10:10 \
  --batch-size 4 --dual-shard-same-gpu --dual-shard-api \
  --dual-shard-source-mode all-a \
  --run-id pressure60_phase23_single_shard_a_10src_8fps
```

这个探针不改变长期 manifest 策略，只用于测 `replay_to_sink_metadata_ms`、
`sink_video_to_stable_ms`、`finalizer_pool_wait_ms`、`max_concurrent_per_shard_reached`
随单 shard source 数增长的曲线。之后再决定 60 路应切成 4 shard、6 shard 还是
8 shard，而不是继续靠“每 shard 8-10 路”的经验值猜。

第一次尝试：

```text
run_id=pressure60_phase23_single_shard_a_10src_8fps_20260704T0119
status=failed_pressure_gates
kept_evidence=0
failure_reasons=insufficient_playable_evidence,
  savant_pose_objects_zero,
  savant_person_observations_zero,
  savant_face_observations_zero
artifact_dir=/data/video-analytics/artifacts/pressure60_phase23_single_shard_a_10src_8fps_20260704T0119
```

这轮不能作为 shard 容量结论，原因是探针参数没有真正作用到 8090 topology API：
`topology_pressure_payload.json` 仍显示 `shard_strategy=balanced`。已经修正
`topology_pressure_payload()`，当 `--dual-shard-source-mode all-a/all-b` 时改用
`shard_strategy=manual`，并写出每个 pressure source 到 `a` 或 `b` 的
`manual_assignments`。后续单 shard 探针必须使用修正后的 runner 重新跑。

修正后重新跑：

```text
run_id=pressure60_phase23_single_shard_a_10src_8fps_20260704T0128
status=failed_pressure_gates
kept_evidence=12
failure_reasons=insufficient_playable_evidence
artifact_dir=/data/video-analytics/artifacts/pressure60_phase23_single_shard_a_10src_8fps_20260704T0128
```

这轮 pressure gate 失败是因为 `keep_evidence=20`，而 10 路 / 120s 只产生并保留了
12 条 playable evidence；但它已经能用于单 shard 容量判断，因为
`topology_pressure_payload.json` 显示 `shard_strategy=manual` 且 10 个 source 全部
assign 到 `a`。关键指标：

| metric | count / scope | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: |
| `record_request_pending_ms` | `12` | `5805.5` | `9431.45` | `9597` |
| `proof_wait_ms` | `32` | `156.5` | `3288` | `3316` |
| `replay_to_sink_metadata_ms` | `12` | `1472.5` | `3509.05` | `4009` |
| `media_worker.queue_wait_ms replay-a` | `12` | `8035.5` | `11606.25` | `11862` |
| `sink_video_to_stable_ms` | `24` | `44` | `53` | `53` |
| `finalizer_pool_wait_ms` | `12` | `0` | `60.4` | `78` |

日志计数：

```text
clip_worker_queued=0
max_concurrent_reached=0
max_concurrent_per_source_reached=0
max_concurrent_per_shard_reached=0
replay_slot_terminal_state=0
```

结论：当前机器上，单个 Replay/sink shard 承接 10 路、8fps、120s 小压测时，证据链路
本身没有出现出口饱和；p95 远低于 60s。60 路 baseline 的 `media_worker.queue_wait_ms`
p95 达到 `127s`，更像是 30 路 / shard 的压力太大，而不是 10 路 / shard 的必然成本。
下一步扩到 4 shard 时每 shard 约 15 路，有机会把 `record_request_pending_ms p95`
压进 60s；如果 4 shard 仍不够，再按本探针结果推进 6/8 shard。

## 18. Phase 2.4/2.5: 4 shard 收敛与 8 shard 探索

Phase 2.4 先扩到 4 个 evidence Replay/sink shard。第一次 4 shard 跑通后发现
`record_request_pending_ms p95` 已进入目标线内，但 `max_concurrent_reached`
仍大量出现。原因不是 Replay/sink 分片无效，而是 pressure runner 只把
`REPLAY_SHARDS_JSON` 切到了 4 shard，clip-worker 的全局 materialization limit
仍沿用 2 shard 时代的 `18`。这样 per-shard 仍是 `9`，但全局只允许 `18` 个 active，
4 个 shard 的真实容量没有被打开。

修正：

- `scripts/runtime/run_midterm_pressure60.py`
  - `clip_worker_replay_shard_env_snapshot()` 增加
    `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY` 和
    `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD` 诊断；
  - pressure runner 按 `shard_count * per_shard_limit` 自动设置全局 limit；
  - 4 shard 时为 `36`，8 shard 时为 `72`；
  - 支持 `--evidence-shard-count 8`。
- `infra/env/midterm.env` / `infra/docker-compose.midterm.yml`
  - midterm 默认全局 materialization limit 调整为 `36`；per-shard limit 仍是 `9`，
    因此 2 shard 运行时仍会被 per-shard gate 限制在实际 `18`。
- `infra/docker-compose.midterm.yml`
  - 增加 `replay-e/f/g/h` 和 `video-file-sink-e/f/g/h`；
  - 8 shard 仍复用双 Savant / 双 analysis-forwarder 拓扑，e/g 路由到
    `analysis-forwarder-a`，f/h 路由到 `analysis-forwarder-b`。
- `modules/savant_replay/config.midterm.replay-{e,f,g,h}.json`
  - 增加对应 Replay 配置。

4 shard 修正后压测：

```text
run_id=pressure60_phase24b_evidence4_global36_8fps_prepost_5_10_20_20260704T0208
artifact_dir=/data/video-analytics/artifacts/pressure60_phase24b_evidence4_global36_8fps_prepost_5_10_20_20260704T0208
status=passed
kept_evidence=60
warnings=validate_seq_iq_expected_sampling_gap
observed_materialization_max_concurrency=36
observed_materialization_max_concurrency_per_shard=9
```

关键指标：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| `record_request_pending_ms` | `24810.5` | `45549.8` | `53014` |
| `proof_wait_ms` | n/a | `6571.45` | n/a |
| `replay_to_sink_metadata_ms` | `13936` | `81930.4` | `86149` |
| `sink_video_to_stable_ms` | n/a | `72.85` | n/a |
| `finalizer_pool_wait_ms` | `17460` | `101440.05` | `106296` |
| `media_worker.queue_wait_ms` | n/a | `134395` | n/a |

日志计数：

| marker | count |
| --- | ---: |
| `clip_worker_queued` | `143` |
| `max_concurrent_reached` | `0` |
| `max_concurrent_per_shard_reached` | `6` |
| `max_concurrent_per_source_reached` | `137` |
| `replay_slot_terminal_state` | `23` |

结论：4 shard + global limit 36 是当前最终推荐配置。它满足本轮目标中的关键验收项：
60 路 8fps、5/10/20 三组 evidence policy 下 `status=passed`、
`kept_evidence=60`、`record_request_pending_ms p95 < 60s`、`sink_video_to_stable_ms`
仍为毫秒级，且全局并发闸门不再成为主因。

为了确认“每 shard 8-10 路”是否进一步改善尾部，又做了 Phase 2.5 的 8 shard 探索：

```text
run_id=pressure60_phase25_evidence8_8fps_prepost_5_10_20_20260704T0220
artifact_dir=/data/video-analytics/artifacts/pressure60_phase25_evidence8_8fps_prepost_5_10_20_20260704T0220
status=passed
kept_evidence=60
observed_materialization_max_concurrency=72
source distribution=8/8/8/8/7/7/7/7
```

关键指标：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| `record_request_pending_ms` | `27859.5` | `61364.4` | `69156` |
| `proof_wait_ms` | n/a | `6043.5` | n/a |
| `replay_to_sink_metadata_ms` | `4489` | `71457` | `73902` |
| `sink_video_to_stable_ms` | n/a | `71.2` | n/a |
| `finalizer_pool_wait_ms` | `10084` | `49371.6` | `60703` |
| `media_worker.queue_wait_ms` | n/a | `129713` | n/a |

日志计数：

| marker | count |
| --- | ---: |
| `clip_worker_queued` | `151` |
| `max_concurrent_reached` | `0` |
| `max_concurrent_per_shard_reached` | `0` |
| `max_concurrent_per_source_reached` | `151` |
| `replay_slot_terminal_state` | `27` |

8 shard 的判断：

- 不采纳为当前默认压测配置，因为 `record_request_pending_ms p95=61364.4`，略高于
  60s 验收线；
- 它确实降低了 `finalizer_pool_wait_ms`，说明更多出口能缓解一部分后处理等待；
- 但 tail 仍分布在全部 shard，且 `max_concurrent_per_source_reached` 成为唯一主要
  admission 计数，说明继续增加 Replay/sink shard 已经不是最直接的下一刀。

剩余风险与后续方向：

- 当前目标已由 4 shard + global 36 满足，但 evidence 可见尾部仍能从
  `media_worker.queue_wait_ms` / `replay_to_sink_metadata_ms` 看到 2 分钟级长尾；
- 这个长尾不再是 `ts_sync`、文件稳定轮询或全局 replay slot 闸门，而更像是
  “单 media-worker 扫描/后处理 + 同源 per-source active=1 + Replay/sink 突发写入”
  的组合；
- 下一阶段若要继续压低“看到证据”的 p95，应优先做 media-worker 分片或独立
  finalizer service，并评估同源短时间多事件合并策略，而不是继续单纯增加
  Replay/sink shard 数。

## 2026-07-04 Fast Raw Clip Mode

400s、60 路、8 FPS、关闭 evidence admission 的全量生成压测暴露了另一个事实：
事件产生没有问题，但如果每条证据都继续走精确时间域裁剪、时长守卫和窗口守卫的
fail-closed 路径，60 路压力下大量任务会在 materialization 队列里过期。用户明确
接受“证据时间边界不必非常精确”，因此当前优化目标调整为：

- 优先快速发布可播放 `raw_clip.mov`；
- 尽量多保留证据，不再因为 raw clip 比请求窗口略长而删除输出；
- 精确裁剪、严格 canonical 判断和 overlay 可信绑定后续再做二阶段产物。

本次代码引入 `POST_SAVANT_FAST_RAW_CLIP_ENABLED`：

- 默认部署配置启用：`infra/env/midterm.env` 与
  `infra/docker-compose.midterm.yml` 均设置为 `true`；
- `services/media-worker/app/worker.py` 中
  `_finalize_post_savant_evidence_bundle()` 在该模式下跳过
  `FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED` 的 ffmpeg time-domain transcode；
- video-file-sink 产出的 `video.mov` 直接拷贝为证据目录里的
  `raw_clip.mov`，`metadata.json` 直接拷贝为 `sink_metadata.json`；
- `duration_guard` 和 `sink_window_guard` 若失败，会被标记为 `relaxed`，
  不再删除 raw clip；
- summary 会记录 `fast_raw_clip_enabled=true`、
  `raw_clip_time_precision=replay_or_gop_window`、
  `video_crop.materialization_mode=fast_raw_copy`，
  并将 `canonical_clip=false`、`production_ready=false`，让 8090 可以播放证据，
  但不把它误报为严格精确裁剪的 canonical evidence。

这不是最终的 rolling 300s GOP segment cache，但它先砍掉了当前最浪费的
“为了精确窗口重编码/裁剪导致大量证据丢失”的路径。后续如果要继续提速，真正方向是
GOP 对齐滚动切片 + `ffmpeg -c copy` remux/concat，让 Replay 输出本身也从
“按请求生成临时视频”变成“从长期缓存快速拼接”。

## 2026-07-04 Fast Evidence No-Queue Direction

本轮重新校正了单卡双分支压力拓扑：

- 60 路 8 FPS 压测使用 `dual_shard_same_gpu=true`、`dual_shard_gpu=0`，
  这是为了提高单卡利用率，不是多卡调度失败；
- live `1080movie` 的不同 400s 片段会显著改变事件密度。已观测到同样
  baseline 下事件数从 `479` 到 `672` 波动，`PER_SOURCE=2` 那轮 `1151`
  不能直接当成干净 A/B；
- 当前证据慢的主因不能再简化为“复制慢”或“磁盘慢”。fast raw copy 和
  sink 稳定都已经很快，真正的问题是 per-event Replay/materialization 在高事件密度下
  仍然要为每个事件占用一次导出名额。

关键结论：

- 如果业务要求“每一条事件都独立生成一个前后 5/10/20s 文件”，那么在事件到达率持续
  高于 Replay/materialization 吞吐时，排队是数学必然，靠继续调大并发只会把压力转移到
  Replay、sink 或 media-worker；
- 要达到“快速出证据且不排队”，最终架构必须把证据生成从
  **per-event Replay job** 改成 **持续 rolling cache + 事件触发快速切片/索引**；
- 同源短时间事件合并可以作为中间层减少重复任务，但它不能替代 rolling cache：
  如果合并后的证据仍需等待一个很长 post window 结束，用户看到证据仍会变慢。

落地顺序固定为：

1. **短期消除已暴露的 media-worker 后处理排队**：
   - media-worker finalizer pool 从 `4` 提升到 `16`；
   - 增加 `MEDIA_WORKER_FINALIZER_MAX_PER_SOURCE_PER_POLL`，默认 midterm 为 `4`；
   - 增加 `MEDIA_WORKER_FINALIZER_SOURCE_SERIAL`，midterm fast raw 默认 `false`，
     允许同一 source 的多个已就绪 sink 输出并行 finalization；
   - 这只优化 `sink_ffprobe_ready_to_finalizer_start_ms` / `finalizer_pool_wait_ms`，
     不声称解决 Replay job 本身排队。
2. **中期增加事件覆盖/合并语义**：
   - 同一 source、同一时间邻域内的事件不再全部启动独立 Replay；
   - 被覆盖事件应在 DB/8090 上显示为“由父证据覆盖”，而不是
     `materialization_skipped`，避免自欺欺人的 skipped 成功率；
   - 父证据 materialized 后，应给子事件写入 evidence bundle alias，让检索每条事件时仍能看到视频。
3. **长期切换 rolling GOP segment cache**：
   - 按 source/shard 持续写 GOP 对齐 segment 和 manifest；
   - event 只负责定位 `[event_ts - pre, event_ts + post]` 覆盖的 segment；
   - media-worker 通过 `ffmpeg -c copy` concat/remux 或直接索引 segment 输出证据；
   - 这一路径不再占用 per-event Replay slot，才是“快速且不排队”的根本解。

本次已完成短期第一刀，代码与配置位置：

- `services/media-worker/app/config.py`：新增
  `MEDIA_WORKER_FINALIZER_MAX_PER_SOURCE_PER_POLL`、
  `MEDIA_WORKER_FINALIZER_SOURCE_SERIAL`；
- `services/media-worker/app/worker.py`：finalizer pool 可配置同源每轮调度数量，
  并可关闭同源串行锁；
- `infra/env/midterm.env` 与 `infra/docker-compose.midterm.yml`：
  midterm 默认 `MEDIA_WORKER_FINALIZER_WORKERS=16`、
  `MEDIA_WORKER_FINALIZER_MAX_PER_SOURCE_PER_POLL=4`、
  `MEDIA_WORKER_FINALIZER_SOURCE_SERIAL=false`。

下一轮压测重点不再只看 evidence 数量，还要对比：

- `sink_ffprobe_ready_to_finalizer_start_ms` 是否从 200s 级下降；
- `finalizer_pool_wait_ms` 是否明显下降；
- `record_request_pending_ms` / `replay_to_sink_metadata_ms` 是否仍为主尾部。

如果后两者仍高，说明短期 finalizer 并行已经吃完收益，必须进入事件覆盖/合并与
rolling cache，而不能继续把“无排队”寄托在 per-event Replay 调参上。

### 验证结果：finalizer 16 并行压测

验证 run：

- `run_id=p60finalizer16_20260704T114410Z`
- artifact：
  `/data/video-analytics/artifacts/p60finalizer16_20260704T114410Z`
- 压测口径：60 路、8 FPS、400s、drain 600s、保留全部证据、
  `pressure_disable_evidence_admission=true`、4 evidence shard、
  `dual_shard_same_gpu=true`。

本轮 live `1080movie` 片段事件密度更高：

| 指标 | 上一轮 `p60evt_20260704T141528` | 本轮 `p60finalizer16_20260704T114410Z` |
| --- | ---: | ---: |
| events | 672 | 997 |
| watchlist_hit | 219 | 581 |
| intrusion | 453 | 416 |
| playable bundles | 152 | 150 |
| expired tasks | 502 | 847 |

短期 finalizer 并行的效果是成立的：

| phase p95 | 上一轮 | 本轮 |
| --- | ---: | ---: |
| `media_worker.queue_wait_ms` | 294494.65 ms | 130330.45 ms |
| `sink_ffprobe_ready_to_finalizer_start_ms` | 205111.55 ms | 34654.75 ms |
| `finalizer_pool_wait_ms` | 34188.15 ms | 34654.75 ms |

解释：

- `sink_ffprobe_ready_to_finalizer_start_ms` 从 205s 级降到 35s 级，说明
  `MEDIA_WORKER_FINALIZER_WORKERS=16`、同源每轮最多 4 个并行调度、关闭同源串行锁，
  确实打掉了 media-worker 已就绪输出等待 finalizer start 的大头；
- 但 playable bundles 基本没有提升：本轮事件更多，最终仍只有 150 条可播放，
  847 条过期；
- `record_request_pending_ms p95=295791.5ms`、
  `replay_to_sink_metadata_ms p95=54681.5ms`，说明剩余主瓶颈已经前移到
  clip-worker Replay admission / per-event Replay 导出，而不是 finalizer；
- video-file-sink 实际 pipeline 仍是快路径：本轮 681 个 writer、687 个 pipeline
  operation，说明 sink 不是“复制慢”的证据。

结论：

这一刀是有效但不充分的局部优化。它改善了“已产出文件排队等 finalizer”的阶段，
但没有改变 per-event Replay 架构的产能天花板。要满足“快速导出且不排队”，下一阶段
必须做事件覆盖/合并和 rolling GOP segment cache；继续只调 finalizer worker 或
Replay 并发不会把 997 events / 400s 这种密度下的全量独立证据变成稳定实时产出。

## 2026-07-04 Rolling Cache MVP Code Path

已经开始把“持续缓存，事件来了快速 remux”落到代码里，但仍处于 feature-flag
关闭的 MVP 阶段，尚未声明压测达标。

本次新增路径：

- `scripts/runtime/run_midterm_pressure60.py`
  - 增加 `--rtsp-republish-input-offset-s` 和
    `--rtsp-republish-input-loop`，用于固定文件/固定 offset 的确定性压测；
  - republish manifest 会记录 input offset / loop，避免继续用不可复现的 live
    片段做关键 A/B。
- `scripts/runtime/rolling_cache_sink_entrypoint.sh`
  - 复用 Savant gstreamer `video_files.py`，从 Savant post-output 订阅并按
    `ROLLING_CACHE_SEGMENT_SECONDS` 写连续 segment；
  - 默认输出到 `/media/rolling-cache/midterm/epochs/<runtime_epoch>/<source_id>/...`。
- `infra/docker-compose.midterm.yml`
  - 新增 `rolling-cache` profile 的单 Savant sink；
  - 新增 `rolling-cache-dual` profile 的 `rolling-cache-sink-a/b`，用于
    单卡双分支拓扑；
  - media-worker/event-worker 均新增 rolling cache feature flags，默认关闭。
- `services/media-worker/app/rolling_cache.py`
  - 扫描 rolling segment 的 `metadata.json`，按 native frame PTS 建立 segment
    覆盖；
  - 对事件窗口选择覆盖 segment，用 `ffmpeg -c copy` concat/remux 生成
    sink-like `video.mov` + `metadata.json`；
  - metadata 标记 `materialization_mode=rolling_cache_copy`、
    `canonical_clip=false`、requested/actual PTS window 和 segment ids。
- `services/media-worker/app/worker.py`
  - 当 `ROLLING_CACHE_ENABLED=true` 且
    `ROLLING_CACHE_MATERIALIZATION_ENABLED=true` 时，media-worker 会先扫描
    pending evidence task，从 rolling cache 生成 sink-like 输出，再复用现有
    post-Savant finalizer/DB index 路径发布 evidence bundle；
  - 这一路径不需要创建 per-event Replay job，也不占用 Replay slot。
- `services/event-worker/app/worker.py`
  - 新增 `ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS`，canary 时可让 event-worker
    只创建 evidence task，不再向 `security.record_requests` 发布 per-event
    Replay 请求。
- `db/migrations/023_evidence_event_links.sql`、
  `services/event-worker/app/repository.py`、
  `services/media-worker/app/worker.py`
  - 新增同源事件覆盖/合并 MVP：短时间同 source/type 子事件可写入
    `evidence_event_links`，父 evidence bundle materialized 后，media-worker
    为子事件发布 DB alias；
  - 该路径默认由 `EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED=false` 关闭，语义是
    `covered_by_event`，不是 `materialization_skipped`。

当前验证：

- `python -m py_compile` 覆盖 media-worker rolling cache、media-worker worker/config、
  event-worker worker/config/repository、pressure runner；
- `pytest -q harness/tests/test_rolling_cache_materialization.py`：3 passed；
- `pytest -q harness/tests/test_event_worker_recording_policy.py`：10 passed；
- `pytest -q harness/tests/test_evidence_event_coverage_merge.py`：3 passed；
- `pytest -q harness/tests/test_midterm_pressure60_script.py::test_rtsp_republish_command_supports_deterministic_file_offset_and_loop`：1 passed；
- `pytest -q harness/tests/test_midterm_deployment_contract.py::test_midterm_rolling_cache_controls_are_disabled_and_wired_by_default`：1 passed；
- `docker compose -f infra/docker-compose.midterm.yml --profile rolling-cache config --quiet`：通过；
- `docker compose -f infra/docker-compose.midterm.yml --profile dual-4090-two-source --profile rolling-cache-dual config --quiet`：通过。

剩余验收缺口：

- 单路 runtime canary 已验证 writer 和 fast materialization 基本闭环，但还不能
  宣布 8-10 路或 60 路压力验收通过；
- 尚未跑 8-10 路确定性压测，也没有 60 路 8 FPS 400s artifact；
- 同源事件覆盖/合并已有 disabled-by-default MVP 代码和合约测试，但仍需在
  runtime canary 中确认 8090 列表/详情能正确看到子事件 alias；
- rolling sink 的 segment GOP/keyframe 边界仍需用实际输出和 ffprobe 验证。

### Runtime canary：单路 rolling-cache writer + materialization

2026-07-04 继续执行单路 canary，先不跑 60 路压力，目标是证明“持续 segment
写入 + 不经 Replay job 的快速 materialization”这条链路真的能在当前 runtime
上工作。

canary 结果：

```text
run_id=rolling_cache_canary_20260704T130505Z
artifact_dir=/data/video-analytics/artifacts/rolling_cache_canary_20260704T130505Z
source_id=source_00000000-0000-4000-8000-781078565686
event_id=4180494f-6f3d-48ec-90ce-5a9fd095b8e5
raw_clip=/data/video-analytics/media/evidence/4180494f-6f3d-48ec-90ce-5a9fd095b8e5/raw_clip.mov
raw_clip_duration_seconds=3.930000
raw_clip_size_bytes=345114
materialization_mode=rolling_cache_copy
bundle_status=materialized/generated_unverified
active_evidence_tasks_after=0
active_replay_slots_after=0
```

关键观测：

- `rolling-cache-sink` 能从 `savant-security:5558` 订阅 post-Savant 输出并持续落
  `video.mov + metadata.json`；
- `CHUNK_SIZE` 实测是帧数，不是秒。原先 `ROLLING_CACHE_SEGMENT_SECONDS=4`
  会得到约 `0.5s` segment；已改成 `ROLLING_CACHE_SEGMENT_SECONDS *
  ROLLING_CACHE_FPS`，默认 `32` 帧，实测 segment 约 `3.9s`；
- media-worker 从 rolling cache 生成 sink-like 输出并复用现有 finalizer/DB index，
  本次 finalizer 日志显示 `post_savant_finalization_elapsed_ms=120ms`、
  `sink_video_to_stable_ms=0`、`sink_ffprobe_ready_to_finalizer_start_ms=44ms`；
- 产出的 DB bundle 标记 `materialization_mode=rolling_cache_copy`、
  `rolling_cache_enabled=true`、`epoch_guard_status=relaxed`；
- 这条路径没有 Replay job create / Replay slot 指标，说明它确实绕开了
  per-event Replay 导出。

canary 期间修正的代码问题：

- `services/media-worker/app/rolling_cache.py`：
  - 兼容 Savant `video_files.py` 的 `source_id%` 目录；
  - 增加小的 segment-edge coverage slack，并记录 gap；
- `scripts/runtime/rolling_cache_sink_entrypoint.sh`：
  - 将目标秒数换算成 `CHUNK_SIZE` 帧数；
- `services/media-worker/app/worker.py`：
  - rolling-cache candidate 的 source filter 下推到 SQL；
  - `_defer_rolling_cache_task()` 的 reason 参数显式 cast，避免 coverage miss
    时 SQL 类型不明确；
  - rolling-cache epoch guard 不再要求 Replay labels，只要求 event/sink/current
    epoch 一致。

仍未完成：

- 这只是单路 canary，不是 `PASS_MIDTERM_ROLLING_CACHE_EVIDENCE_FAST_PATH`；
- 尚未验证 `ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=true` 的 event-worker 端到端
  事件入口；
- 尚未验证 `EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED=true` 时 8090 子事件 alias；
- 尚未跑 8-10 路 deterministic pressure 和 60 路 8 FPS 400s 验收。

### Runtime canary：8 路 deterministic rolling-cache pressure

2026-07-04 跑通 8 路、8 FPS、小窗口 deterministic canary，重点验证
event-worker `ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=true`、media-worker
rolling fast path 和 8090 DB evidence index 的端到端闭环。

先失败了一轮：

```text
run_id=rollingcache_canary8_20260704T131838Z
artifact_dir=/data/video-analytics/artifacts/rollingcache_canary8_20260704T131838Z
result=failed
root_cause=rtsp_republish_output_base 使用了多级 RTSP path，source-adapter 拉流得到 404
```

修正：

- `scripts/runtime/run_midterm_pressure60.py`
  - restore event-worker/media-worker env 时，原始 snapshot 中缺失的空值不再强制
    与 compose 默认值逐字相等，避免正常 restore 被误判为失败；
  - local file republish 不再加 `-use_wallclock_as_timestamps 1`。这个参数适合
    live RTSP 输入，但对固定文件推 RTSP 会导致当前 RTSP server/source-adapter
    组合无法稳定拉到流；
  - rolling-cache pressure runner 现在会配置 event-worker suppress、media-worker
    rolling source filter，在新 `runtime_epoch_id` 已经创建之后再启动
    `rolling-cache-sink*`，并显式传入 `ROLLING_CACHE_RUNTIME_EPOCH_ID`；
  - 同时 pressure artifact 额外保留 `rolling_cache_sink*_logs_since_start.txt`，
    用于直接核对写入侧 coverage；
  - run 结束/异常时仍会恢复 event-worker/media-worker 和 rolling-cache sink 状态。

2026-07-05 继续修正了 rolling-cache recount 之后暴露的统计/收尾问题：

- `covered_by` 子事件在 `evidence_tasks.materialization_defer_reason` 中显式写入
  `covered_by_existing_evidence`，不再落成大量 `reason=NULL` 的模糊 deferred；
- pressure runner 的 `db_summary` / `wait_for_drain` / failure gate 改成按
  **distinct event coverage** 统计，而不是用
  `playable_bundles + covered_playable_events` 这种会重复计数的口径；
- 新增 `distinct_events_with_playable_evidence`、
  `distinct_events_without_playable_evidence`、
  `distinct_events_with_terminal_nonplayable_outcome`、
  `blocking_materialization_tasks` 等字段，用来保证“每个 event 最终必须落在一个
  明确桶里”，避免 pressure run 因重复计数而过早结束并在 restore 后悬空老任务。

2026-07-05 epoch barrier 已按上面的约束落地到生产 runtime path：

- 这不是 pressure runner 的补丁，而是 **生产 runtime epoch 切换** 的前置条件。
  任何 `runtime restart` / `topology apply` / 源重连触发的 epoch 切换都必须走同一
  套 barrier 逻辑。
- 默认采用 **阻塞式 barrier + 短超时兜底**：
  - `force=false`：先等待旧 epoch 的非终态任务 drain；超时则返回 `409 blocked`；
  - `force=true`：超时后允许继续切换，但必须在切换动作自身的一部分中，把剩余旧
    epoch 非终态任务显式转成终态，不能留给“切换后某个善后清理”。
- `force=true` 的终态化必须是 **单次条件更新（CAS）**，不能“先查再写”：
  - 需要在一个 `UPDATE ... WHERE runtime_epoch_id=old_epoch AND status IN (...)`
    中完成；
  - 这样既能防止 barrier 误把已经完成的任务覆盖成失败，也能与 media-worker 的
    正常完成路径复用同一类 CAS 约束。
- 被 barrier 强制终态化的任务，使用显式原因
  `epoch_superseded_incomplete`，不允许出现“非终态但已经没有任何 worker 会再处理”
  的沉默悬空状态。
- `409 blocked` 的 API 响应必须带诊断信息，至少包括：
  - 当前仍阻塞的任务数量；
  - 这些任务涉及的 source / shard / epoch；
  - 是否已经超过 barrier 超时窗口；
  - 以便调用方判断“正常等待后重试”还是“应该带 `force=true` 再次提交”。
- `runtime_epoch_id` 先升成 `evidence_tasks` 的一等字段，再在其上做 barrier /
  监控 / CAS 条件更新。migration 需要：
  - 优先从现有 event payload/runtime 元数据回填历史行；
  - 对无法回填的历史行允许 `NULL`；
  - 但 `NULL + 非终态` 必须被监控标记为异常，而不是静默忽略。
- 监控信号必须能主动发现“epoch 不匹配但仍非终态”的 orphan 任务，例如：
  - `task.runtime_epoch_id != current_runtime_epoch_id`
  - 且 `materialization_status` 仍处于非终态。
  这类任务需要能在 runtime overview / diagnostics 中直接看到，不再依赖人工查日志。
- `epoch_superseded_incomplete` 在当前设计里先视为**明确终态**，不做自动补偿重试。
  如果业务后续要求“运维切换不能牺牲边界事件证据”，再追加独立的补偿/requeue 机制；
  当前 barrier 版本先保证正确性和状态守恒。
- 验证除了压测外，必须补两类确定性测试：
  - 卡住 `materializing` 任务后触发 epoch 切换，验证阻塞/超时/force 终态化语义；
  - barrier 终态化与 media-worker 正常完成并发写同一任务时，验证 CAS 不会把成功
    任务错误覆盖，也不会把 superseded 终态回写成成功。
- 额外补一项 barrier 查询本身的性能验证：在当前 `evidence_tasks` 历史数据规模下，
  确认“检查旧 epoch 是否还有非终态任务”的查询不会成为 runtime restart / topology
  apply 的新瓶颈；必要时为 `runtime_epoch_id + materialization_status` 建立索引。

当前实现对应：

- `db/migrations/024_evidence_task_runtime_epoch_barrier.sql`
  已把 `runtime_epoch_id` 升成 `evidence_tasks` 一等字段，并为 barrier 查询增加
  活跃任务索引；
- `services/api/app/services/runtime_apply.py`
  现在默认执行阻塞式 epoch barrier，超时后 `force=true` 通过单条条件更新把剩余
  非终态任务终态化为 `epoch_superseded_incomplete`，并在 `409 blocked` 中返回
  source/shard/epoch 诊断；
- `services/media-worker/app/worker.py`
  对 `epoch_superseded_incomplete` 增加了防复活保护，避免 barrier 已失败的任务被旧
  worker 后续 materialized/ready 回写覆盖；
- `services/api/app/services/runtime_overview.py`
  新增 `epoch_barrier` 运行态摘要，用于直接暴露 orphan 任务信号。

通过的 canary：

```text
run_id=rollingcache_canary8c_20260704T133300Z
artifact_dir=/data/video-analytics/artifacts/rollingcache_canary8c_20260704T133300Z
streams=8
fps=8/1
duration_s=60
drain_s=60
input=/home/user/video-analytics/testVideo/test.mp4
rtsp_republish_output_base=rtsp://192.168.1.105:8554/pressure_{source_id}
status=passed
kept_evidence=3
warnings=validate_seq_iq_expected_sampling_gap
active_evidence_tasks_after=0
active_replay_slots_after=0
pressure_source_containers_after=0
```

关键结果：

- retained evidence 的 DB bundle 均为 `materialization_mode=rolling_cache_copy`、
  `rolling_cache_enabled=true`、`canonical_clip=false`；
- 8090 evidence health OK，3 条 retained evidence detail 检查均通过；
- clip-worker / Replay admission 指标为 0，说明本轮 evidence 入口没有创建
  per-event Replay job，也没有占用 Replay slot；
- media-worker rolling finalizer：
  - `sink_video_to_stable_ms p50/p95=0/0`；
  - `sink_ffprobe_ready_to_finalizer_start_ms p50/p95=66/66.9ms`；
  - `post_savant_finalization_elapsed_ms p50/p95=714.5/967.85ms`；
  - `finalization_duration_ms p50/p95=720/972.9ms`；
- 保留样本：
  - `fc59c369-a0d5-4f74-a0a1-e7e0ec12d116`：
    `raw_clip_duration_seconds=12.639`；
  - `d7881bfe-3eef-418d-b38f-b0c43662ae3b` 及 covered/alias 子事件：
    `raw_clip_duration_seconds=43.943`。

当前解释：

- 8 路 canary 已证明 rolling cache fast path 可以绕开 Replay/admission，并且能
  产出 8090 可打开的 evidence bundle；
- 本轮只有 6 个上游事件、3 个保留 evidence，不足以证明 60 路高密度场景的
  产能上限已被打破；
- `validate_seq_iq_expected_sampling_gap` 是固定文件循环/采样输入下的已知 warning，
  不影响本轮 rolling evidence 结论；
- 下一步必须跑 60 路 8 FPS、400s、5/10/20 窗口，并重点看：
  `rolling_cache_copy` 总数、covered alias 数、Replay fallback 数、
  `materialization_expired`、8090 样本播放质量，以及 run 后 active task/slot/source
  清零。

### Runtime pressure：60 路 8 FPS rolling-cache deterministic run

2026-07-04 继续跑 60 路、8 FPS、400s deterministic run，使用单卡双分支拓扑和
rolling-cache-dual sink。

第一轮启动失败：

```text
run_id=rollingcache_p60_8fps_20260704T133936Z
artifact_dir=/data/video-analytics/artifacts/rollingcache_p60_8fps_20260704T133936Z
result=failed_before_sources
root_cause=rolling-cache-dual compose 命令只带 rolling-cache-dual profile，未同时带 dual-4090-two-source profile，导致 savant-b 被视为 undefined service
```

修正：

- `scripts/runtime/run_midterm_pressure60.py`
  - dual rolling-cache sink 的 compose 命令同时带
    `--profile dual-4090-two-source --profile rolling-cache-dual`；
  - rolling-cache pressure 且 `--pressure-disable-evidence-admission` 时，现在同步把
    event-worker 的 global/per-source/event-type admission 和 cooldown 置 0；
  - `--keep-evidence -1` 不再只看 retained sample 数。新 gate 会要求
    `playable_bundles >= events`，且出现 `materialization_expired` 时失败，避免
    “有部分证据丢了但 report 仍 passed”的自欺欺人。

第二轮结果：

```text
run_id=rollingcache_p60_8fps2_20260704T134217Z
artifact_dir=/data/video-analytics/artifacts/rollingcache_p60_8fps2_20260704T134217Z
streams=60
fps=8/1
duration_s=400
drain_s=300
topology=dual_same_gpu
input=/home/user/video-analytics/testVideo/test.mp4
status=passed_by_old_gate
events=40
playable_bundles=36
rolling_cache_copy_bundles=36
record_requests=0
replay_slot_acquired=0
active_evidence_tasks_after=0
active_replay_slots_after=0
pressure_source_containers_after=0
```

关键指标：

- 8090 evidence detail：36/36 OK；
- media-worker rolling finalizer：
  - `finalization_duration_ms p50/p95=507.5/824.95`；
  - `post_savant_finalization_elapsed_ms p50/p95=500/787.45`；
  - `sink_video_to_stable_ms p50/p95=0/0`；
  - `sink_ffprobe_ready_to_finalizer_start_ms p50/p95=67.5/83.7`；
  - `queue_wait_ms p50/p95=10460/23663.15`；
- replay/admission：`slot_acquired_count=0`，说明本轮仍然绕开 per-event Replay；
- restored state：event-worker rolling suppress/admission/cooldown 已恢复，source
  pressure containers 清零。

不能把这轮当最终验收：

- deterministic 输入 `testVideo/test.mp4` 事件密度太低，400s 只产生 40 个
  intrusion 事件，不能证明 rolling cache 已突破旧的 150-195 playable ceiling；
- 40 个事件中只有 36 个 playable bundle，且旧 gate 没有把 4 个
  `materialization_expired` 判为失败。代码已修正，后续同类 run 会正确失败；
- 16 个 task 的 `materialization_status=materialization_failed` 但已有
  `rolling_cache_copy` bundle，错误为 `generated_annotation_failed`，说明 raw clip
  已生成但 annotation/状态语义还需要收敛；
- 下一步需要一个更高事件密度的 deterministic 输入，或用 live `1080movie` 跑一轮
  exploratory stress，证明 `events` 接近旧压力水平时 playable/covered evidence
  仍显著高于旧 Replay ceiling。
### 2026-07-04 rolling-cache final live stress

最终采用 live `1080movie` 做 60 路 8 FPS、400s、5/10/20 evidence window 的
rolling-cache exploratory stress：

```text
run_id=rollingcache_live_p60_finalclean4_20260704T171638Z
artifact_dir=/data/video-analytics/artifacts/rollingcache_live_p60_finalclean4_20260704T171638Z
status=passed
warnings=validate_seq_iq_expected_sampling_gap
events_before_cleanup=759
playable_bundles_before_cleanup=383
covered_events_before_cleanup=580
covered_playable_events_before_cleanup=397
post_reconcile_events=519
post_reconcile_playable_bundles_or_aliases=519
active_materialization_tasks_after=0
active_replay_slots_after=0
pressure_source_containers_after=0
```

本轮结论：

- rolling cache 已突破旧 per-event Replay 架构的 150-195 playable ceiling；
- `security.record_requests` 为 0，证据路径没有再为每个事件创建 Replay job；
- 证据输出以 `rolling_cache_copy` 为主，时间边界允许 GOP/segment 级近似；
- 同源连续事件通过 `evidence_event_links.relation='covered_by'` 合并为证据组，
  子事件通过 DB alias 指向 parent clip，而不是 `materialization_skipped`；
- 8090 可以看到 retained playable evidence；post-reconcile 后 retained 的 519 条
  event/task 全部为 `materialized`。

最终修复点：

- `media-worker` rolling finalizer 改为并行、分块 flush，避免大批量任务等完整
  poll batch 才开始 finalizer；
- `clip-worker` 的 materialization deadline sweep 不再杀掉已经被 rolling-cache
  claim 的任务；
- coverage parent 是证据组根，不能被旧 Replay TTL sweep 提前 expire，因此
  `expire_materialization_deadlines()` 也保护 `bundle_event_id=parent` 的任务；
- late-arriving covered child event 会由 `media-worker` 周期性 reconcile 成
  `materialized_alias`，补齐 `evidence_bundles` / artifacts / timeline DB alias；
- pressure runner 在 `--keep-evidence -1` 时保留 covered child event rows，保证
  “保留全部压测证据”时每个事件仍可追溯。

注意：`report.json` 在 reconcile patch 手工补账前写出，所以其中
`db_summary_after_cleanup` 仍显示 57 个 active child alias。权威收口状态以同目录
`post_reconcile_summary.json` 为准；当前代码已经包含周期性 reconcile，后续 run
应无需手工补账。

### 2026-07-06 ready_at / not_before 调度验证

本轮把 rolling-cache 物化任务从“worker claim 后在 worker 内等待 post window /
segment ready”改为“入库时先计算 `materialization_ready_at`，media-worker 只 claim
已经 ready 的任务”。核心语义变化：

- event-worker 在创建 `evidence_tasks` 时写入
  `materialization_ready_at = event_ts + post_seconds + segment_grace`；
- media-worker 候选查询只扫描
  `materialization_ready_at IS NULL OR materialization_ready_at <= now()` 的任务；
- worker claim 后重新设置 processing deadline：
  `materialization_deadline_at = claim_time + processing_deadline_s`；
- deadline 不再把“视频未来帧尚未产生 / segment 尚未关闭”的自然等待期算作处理超时；
- `evidence_tasks_materialization_ready_idx` partial index 已加入迁移，实测
  `EXPLAIN` 使用该索引扫描 ready 任务。

实现和运行配置：

```text
migration=db/migrations/025_evidence_task_materialization_ready_at.sql
event_worker_grace=EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS=5
media_worker_grace=ROLLING_CACHE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS=1
processing_deadline=ROLLING_CACHE_MATERIALIZATION_PROCESSING_DEADLINE_SECONDS=120
expanded_db_overlay_rows=disabled
annotation_source=filesystem sidecar fallback through 8090 API
```

固定输入尝试：

```text
fixture=/data/video-analytics/pressure-fixtures/1080movie_fixed_20260706T065104Z.mp4
duration=60.018s
codec=h264
resolution=1920x1080
frames=1445
```

该固定片段不能作为吞吐验收输入：60 路运行数分钟只有 5 个事件，虽然每路都有
frame annotation 输出，但事件密度过低，无法证明 60 路 evidence materialization
吞吐。另一次 live RTSP host republish 也被判无效，因为 60 个 host ffmpeg 同时拉取
同一路 live RTSP 导致 source 容器大量 `255` 重启，事件为 0。

最终使用常规 runtime/source 路径直接跑 live `1080movie`，作为非 deterministic 的
真实压力探索：

```text
run_id=readyat_live_direct_p60_20260706T070513Z
artifact_dir=/data/video-analytics/artifacts/readyat_live_direct_p60_20260706T070513Z
status=passed
streams=60
fps=8/1
duration_s=400
event_ts_span_s=409.722
created_at_span_s=409.725
warnings=validate_seq_iq_expected_sampling_gap
```

事件与证据守恒：

```text
events_before_cleanup=674
new_events=275
suppressed_events=399
cooldown_scope=source:event_type
cooldown_violation_count=0
materialized_tasks=275
playable_bundles=275
deadline_expired=0
epoch_superseded_incomplete=0
active_materialization_tasks_after=0
ready_waiting_tasks_after=0
unready_waiting_tasks_after=0
```

本轮的关键进步不是原始事件数变多，而是所有通过 cooldown/admission 的
`new` 事件都走到了可播放证据：

```text
new_events_to_playable=275/275
all_events_to_playable_or_suppressed=674/674
distinct_events_without_playable_evidence=399
terminal_nonplayable_outcome=399  # 全部为 suppressed，不是物化失败
```

ready 调度计时：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| ready_to_claim | `4.86s` | `13.11s` | `19.05s` |
| claim_to_materialized | `7.92s` | `16.40s` | `17.08s` |
| task lifecycle | `30.23s` | `46.43s` | `54.32s` |
| media finalization duration | `4.21s` | `10.65s` | `12.21s` |
| finalizer pool wait | `3ms` | `12ms` | `45ms` |

解释：

- 旧架构里最容易被误判成“worker 忙”的自然等待已被剥离，claim 后没有再出现几十秒
  “傻等 post window / segment ready”的现象；
- `materialization_deadline_expired=0`，说明 deadline 从 ready/claim 后起算是正确的；
- 但 `ready_to_claim_p95=13.11s` 仍高于目标 `<=3s`，瓶颈已转移到 ready 任务扫描 /
  poll cadence / candidate batch / DB 查询调度；
- `claim_to_materialized_p95=16.40s` 略高于目标 `<=15s`，主要来自真实 copy/index
  工作，不再是 Replay slot 或 ffmpeg/ffprobe；本轮 `ffmpeg_duration_ms=0`、
  `ffprobe_duration_ms=0`。

8090 与证据质量复核：

```text
8090 evidence health=index_source:database
retained_count=275
checked_count=275
ok_count=275
annotation_checked_count=275
annotation_ok_count=275
API /api/v1/evidence/bundles p50-ish manual check <300ms for 50-row page
annotation include_records=false manual check ~157ms
annotation include_records=true sample ~8ms, 24 records
```

证据时长按 5/10/20 秒三组恢复到预期窗口，没有再出现 1 分钟以上长证据：

| policy group | n | min | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| `5+5 expected_10s` | 93 | `7.02s` | `10.00s` | `10.00s` | `10.00s` |
| `10+10 expected_20s` | 91 | `12.86s` | `20.00s` | `20.00s` | `20.00s` |
| `20+20 expected_40s` | 91 | `33.43s` | `40.00s` | `40.00s` | `40.00s` |

短于目标的少量 clip 是测量窗口边界 / pre-roll 可用性导致，不是 coverage merge 把
窗口拉成长片；本轮 `raw_clip_duration_seconds > 45s` 的证据为 0。

压测后恢复状态：

```text
pressure cameras removed
pressure source containers removed
lab camera enabled=true
primary_rtsp enabled=false
running dynamic source=lab
runtime overview sources_active=1
epoch_barrier blocking_count=0
epoch_barrier orphans_present=false
```

本轮仍未完成的事项：

- deterministic A/B 仍缺一个高事件密度固定输入。`test.mp4` 和本次 60s
  1080movie fixture 都太低负载，不能作为吞吐验收输入；
- `ready_to_claim_p95=13.11s` 说明 ready 任务发现/claim 仍有十秒级尾部，应继续优化
  media-worker poll interval、ready scan batch size、candidate query 和 shard-local
  并行 claim；
- `claim_to_materialized_p95=16.40s` 略超目标，应继续拆 `rolling_cache_copy` /
  sidecar index / DB bundle upsert 的分段耗时；
- segment grace 这轮主要靠结果反证：`no_overlapping_segments=0`、
  `rolling_cache_event_frame_not_covered=0`。还需要补 segment close -> index visible
  的直接分布指标，不能长期只靠 failure count 间接判断。

### 2026-07-06 fast ready poll 追加优化

上一轮 `ready_to_claim_p95=13.11s` 与 media-worker 主循环
`MEDIA_POLL_INTERVAL_S=10` 的机制高度吻合：任务即使已经过了
`materialization_ready_at`，也可能自然等到下一轮 10 秒扫描才被 claim。

修复：

- 新增 `ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S`，默认 `1.0s`；
- media-worker 主循环拆成两个 cadence：
  - rolling-cache ready/materialization scan：按
    `ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S`；
  - 普通 Replay sink / snapshot / annotation scan：仍按 `MEDIA_POLL_INTERVAL_S`；
- 压测脚本在 rolling-cache pressure 中设置并验证：
  - `ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S=1`;
  - `ROLLING_CACHE_MATERIALIZATION_MAX_PER_POLL=240`;
  - `ROLLING_CACHE_MATERIALIZATION_WORKERS=15`;
- 对非 coverage/gap 的 rolling-cache runtime error（例如 `ffmpeg_failed`）新增
  终态化路径：写入 `materialization_failed` 和
  `materialization_failure_reason`，不再作为 `materialization_deferred` 每秒无限重试。

验证：

```text
pytest harness/tests/test_media_worker_perf_safety.py \
       harness/tests/test_midterm_pressure60_script.py \
       harness/tests/test_rolling_cache_materialization.py \
       harness/tests/test_evidence_viewer_database_index.py
result=104 passed

py_compile=config.py worker.py run_midterm_pressure60.py
docker compose config --quiet
git diff --check
```

第一轮 fast-poll 压测暴露了终态化 bug：

```text
run_id=readyat_fastpoll_live_direct_p60_20260706T072515Z
status=failed_pressure_gates
events=677
new_events=433
suppressed_events=244
playable_bundles=432
failure_reasons=event_outcomes_unaccounted,materialization_unresolved_present
root_cause=1 个 rolling-cache ffmpeg_failed:1 任务反复从 deferred 被重试，report 截图时仍未终态
ready_to_claim_p50/p95/max=1.32s/7.50s/12.16s
claim_to_materialized_p50/p95/max=3.15s/11.17s/12.42s
```

这轮虽然 gate 失败，但证明 fast poll 有直接收益：

| metric | 10s main loop | 1s rolling poll failed run |
| --- | ---: | ---: |
| ready_to_claim p50 | `4.86s` | `1.32s` |
| ready_to_claim p95 | `13.11s` | `7.50s` |
| claim_to_materialized p50 | `7.92s` | `3.15s` |
| claim_to_materialized p95 | `16.40s` | `11.17s` |

修复 terminal error 后重跑：

```text
run_id=readyat_fastpoll_terminal_live_direct_p60_20260706T073930Z
artifact_dir=/data/video-analytics/artifacts/readyat_fastpoll_terminal_live_direct_p60_20260706T073930Z
status=passed
warnings=validate_seq_iq_expected_sampling_gap
streams=60
fps=8/1
duration_s=400
event_ts_span_s=430.866
created_at_span_s=430.865
```

事件与证据：

```text
events_before_cleanup=286
new_events=206
suppressed_events=80
cooldown_scope=source:event_type
cooldown_violation_count=0
materialized_tasks=206
playable_bundles=206
deadline_expired=0
epoch_superseded_incomplete=0
active_materialization_tasks_after=0
ready_waiting_tasks_after=0
unready_waiting_tasks_after=0
new_events_to_playable=206/206
all_events_to_playable_or_suppressed=286/286
```

ready 调度计时：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| ready_to_claim | `0.79s` | `4.87s` | `11.65s` |
| claim_to_materialized | `1.45s` | `7.83s` | `10.76s` |
| task lifecycle | `17.26s` | `34.45s` | `36.94s` |
| media finalization duration | `0.89s` | `5.03s` | `6.61s` |
| finalizer pool wait | `0ms` | `2ms` | `4ms` |

解读：

- fast poll 把 `ready_to_claim_p95` 从 `13.11s` 降到 `4.87s`，但仍未达到
  `<=3s` 的验收门槛；
- `claim_to_materialized_p95=7.83s` 已低于目标 `<=15s`，也远低于旧 Replay
  slot active `~80s`；
- 处理阶段已不再是主要瓶颈，下一步应继续压 ready claim 尾部，重点看：
  ready 任务 burst 时的 DB claim 竞争、单 media-worker 轮询与最终批量 flush 的耦合、
  以及是否需要 shard-local rolling materializer 进程；
- 本轮事件负载偏低，主要因为 live 内容波动，不能作为 deterministic A/B 结论。

segment_grace 校准状态：

- fast poll 早期确实出现 transient `no_overlapping_segments` /
  `rolling_cache_event_frame_not_covered`；
- 中途复核时这些 deferred 已全部自愈，最终 report 中没有保留 coverage/gap failure；
- 因此当前 `segment=4s + grace=1s` 在本轮 live direct 压测中可工作，但还不是严格的
  segment close -> lookup visible 分布校准。后续仍需把 segment writer 的 close time
  和 DB lookup hit time 打点，才能把 `5s` 从“经验值”升级成“实测 p95”。

8090 与证据时长：

```text
8090 checked=206/206 OK
annotation checked=206/206 OK
raw_clip_available=true
annotations_available=true
over_45s=0
max_duration=40s
```

证据窗口分布：

| policy group | n | min | p50 | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: |
| `5+5 expected_10s` | 73 | `6.65s` | `10.00s` | `10.00s` | `10.00s` |
| `10+10 expected_20s` | 59 | `19.18s` | `20.00s` | `20.00s` | `20.00s` |
| `20+20 expected_40s` | 74 | `24.84s` | `40.00s` | `40.00s` | `40.00s` |

压测后恢复状态：

```text
pressure cameras removed
pressure source containers removed
lab camera enabled=true
primary_rtsp enabled=false
running dynamic source=lab
retained pressure evidence=206
```

## 2026-07-06 ready_at 第二阶段：异步 materializer 池与 60-worker burst 验证

上一节的 `ready_at` 改造只解决了“还没到 post window/segment ready 的任务不应被
worker claim”这个问题，但 media-worker 里仍有一个隐藏同步点：

```text
poll -> claim 一批 ready tasks -> ThreadPoolExecutor 跑完整批 copy/remux
     -> flush finalizer batch -> 下一次 poll
```

当 `ROLLING_CACHE_MATERIALIZATION_MAX_PER_POLL=240` 且 worker 数较小时，一个 poll
会阻塞几十秒，配置上的 `ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S=1` 就不再是真
实扫描间隔。修复方向是把 rolling-cache materialization 改成常驻异步池：

- media-worker 启动时创建 `_RollingCacheMaterializationRunner`；
- 每次 1s poll 只 claim 当前空闲 worker 数量的任务；
- copy/remux 在常驻 `ThreadPoolExecutor` 里运行；
- 主循环每轮只 drain 已完成 futures，并把完成的 metadata 送进 finalizer；
- 不再“先 claim 240 个，然后同步等整批跑完”。

实现文件：

```text
services/media-worker/app/worker.py
scripts/runtime/run_midterm_pressure60.py
harness/tests/test_media_worker_perf_safety.py
```

验证：

```text
pytest -q harness/tests/test_midterm_pressure60_script.py harness/tests/test_media_worker_perf_safety.py
python -m py_compile scripts/runtime/run_midterm_pressure60.py services/media-worker/app/worker.py
git diff --check -- scripts/runtime/run_midterm_pressure60.py services/media-worker/app/worker.py harness/tests/test_media_worker_perf_safety.py
```

结果：

```text
82 passed
py_compile OK
diff check OK
```

### 15-worker 异步池验证

```text
run_id=readyat_asyncpool_rolling_live_direct_p60_20260706T082020Z
artifact_dir=/data/video-analytics/artifacts/readyat_asyncpool_rolling_live_direct_p60_20260706T082020Z
status=passed
warnings=validate_seq_iq_expected_sampling_gap
streams=60
fps=8/1
duration_s=400
rolling_cache_workers=15
```

事件与证据：

```text
events_before_cleanup=1275
new_events=518
suppressed_events=757
cooldown_violation_count=0
materialized_tasks=518
playable_bundles=518
deadline_expired=0
epoch_superseded_incomplete=0
active_materialization_tasks_after=0
new_events_to_playable=518/518
8090 checked=518/518 OK
annotation checked=518/518 OK
```

ready 调度计时：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| ready_to_claim | `17.50s` | `55.40s` | `65.74s` |
| claim_to_materialized | `5.38s` | `13.37s` | `22.07s` |
| finalization_duration_ms | `3198ms` | `7463ms` | `11347ms` |
| finalizer_pool_wait_ms | `3ms` | `10ms` | `319ms` |

解读：

- 异步池把真正处理阶段压下来了：
  `claim_to_materialized_p95` 从上一轮 `37.22s` 降到 `13.37s`；
- 但 `ready_to_claim_p95` 仍在 `55s`，说明剩余等待不再是“自然 post window 等待”，
  而是 60 路 cooldown 对齐后形成 ready burst，15 个 worker 需要排队消化；
- 这验证了用户提出的直觉：等待不该占 worker；但 ready 后的 copy/remux 也需要按流规模
  并发，而不是固定 15。

### 60-worker 异步池验证

随后把压力脚本中的 rolling-cache worker 公式从：

```text
max(4, min(16, stream_count // 4 or 1))
```

改为：

```text
max(4, min(64, stream_count or 1))
```

即 60 路压测时使用 60 个 copy/remux worker，上限 64，模拟“每路摄像头都能有并发证据
生成能力，但仍有全局上限”的模型。

```text
run_id=readyat_asyncpool_w60_rolling_live_direct_p60_20260706T083355Z
artifact_dir=/data/video-analytics/artifacts/readyat_asyncpool_w60_rolling_live_direct_p60_20260706T083355Z
status=passed
warnings=validate_seq_iq_expected_sampling_gap
streams=60
fps=8/1
duration_s=400
rolling_cache_workers=60
```

事件与证据：

```text
events_before_cleanup=922
new_events=325
suppressed_events=597
cooldown_scope=source:event_type
cooldown_violation_count=0
event_ts_span_s=375.265
materialized_tasks=325
playable_bundles=325
deadline_expired=0
epoch_superseded_incomplete=0
active_materialization_tasks_after=0
new_events_to_playable=325/325
all_events_to_playable_or_suppressed=922/922
8090 checked=325/325 OK
annotation checked=325/325 OK
```

ready 调度计时：

| metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| ready_to_claim | `1.82s` | `10.22s` | `15.25s` |
| claim_to_materialized | `2.73s` | `13.34s` | `15.00s` |
| finalization_duration_ms | `1238ms` | `8231ms` | `8360ms` |
| finalizer_pool_wait_ms | `1ms` | `15.4ms` | `246ms` |
| lifecycle_elapsed_ms | `24.10s` | `45.79s` | `54.68s` |

对比：

| metric | 15-worker async | 60-worker async |
| --- | ---: | ---: |
| ready_to_claim p50 | `17.50s` | `1.82s` |
| ready_to_claim p95 | `55.40s` | `10.22s` |
| claim_to_materialized p50 | `5.38s` | `2.73s` |
| claim_to_materialized p95 | `13.37s` | `13.34s` |
| finalizer_pool_wait p95 | `10ms` | `15.4ms` |

结论：

- `ready_at/not_before` 语义成立：自然等待不再消耗 materializer；
- 15-worker 下的长尾来自 ready burst 排队，不是 post window 或 segment 等待；
- 60-worker 显著压低 `ready_to_claim`，且没有引入 8090 播放/标注失败，也没有
  deadline/epoch orphan；
- `ready_to_claim_p95=10.22s` 仍未达到 `<=3s`，下一步要继续拆的是：
  1. ready burst 的 claim 排队是否还被单 media-worker 主循环/DB claim 串行限制；
  2. 是否需要每 shard / 每 source group 的 rolling materializer 进程；
  3. 60-worker 在 T4 生产机器上的 CPU/IO 上限不能沿用 4090 压测值，必须单独标定。

注意：

- 两轮都是 live direct 压测，事件数量受电影内容波动影响；本轮 60-worker 的事件量低于
  15-worker，不能当作严格 A/B；
- 但“所有 new events 都 materialized，8090 全部可播放且 annotation OK”是当前真实运行
  结果；
- `validate_seq_iq_expected_sampling_gap` 仍是预期采样 warning，不作为证据失败处理。

### 2026-07-06 ready_at 复核与失败诊断补强

对 `ready_at/not_before` 设计的复核结论：

- 用户提出的“等待不应占 worker，可以并发等”判断是正确的。当前实现已经把自然等待移到
  `materialization_ready_at` 之前，media-worker 只 claim 已经 ready 的任务；
- deadline 语义已经同步调整：claim 后写
  `materialization_deadline_at = now() + processing_deadline_s`，因此
  event time 到 ready time 的物理等待不会再被算作处理超时；
- epoch barrier 已把 `manifest_ready`、`materialization_pending`、`materializing`
  和普通 task 的 `pending/materializing/finalizing` 等非终态纳入检查，未 ready 的
  `ready_at` 任务不会因为 epoch 切换变成新孤儿；
- pressure runner 同时给 event-worker 写
  `EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS=9`，给 media-worker fallback
  写 `ROLLING_CACHE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS=5`。权威 ready time
  来自 event-worker 字段，media-worker fallback 只覆盖旧任务。

后续仍不能把该项宣布为最终完成，原因是：

- `segment_grace` 目前仍主要由 live run 的 coverage/gap failure 归零来反证，缺少
  segment close -> lookup visible 的直接分布；
- deterministic 高事件密度固定输入还没落地，live `1080movie` 事件密度波动仍会影响
  A/B；
- 60-worker 当前实现功能正确，但性能 gate 不稳定：最近 current-code run 仍有
  `ready_to_claim_p95=18.68s`、`claim_to_materialized_p95=22.01s`。

回滚过的方向：

```text
async finalizer batch executor
multi-batch run: claim_to_materialized_p95=24.65s
single-batch run: claim_to_materialized_p95=51.93s
decision=reverted; extra finalizer concurrency worsened IO/DB contention
```

最近 current-code live run：

```text
run_id=readyat_asyncpool_current_w60_rolling_live_direct_p60_20260706T091624Z
status=passed
events=963
new_events=352
suppressed_events=611
materialized=351
materialization_failed=1
failure_reason=rolling_cache_error:RollingCacheError
retained_playable=351/351
8090_checked=351/351 OK
annotation_checked=351/351 OK
ready_to_claim_p50/p95/max=3.90s/18.68s/25.99s
claim_to_materialized_p50/p95/max=7.13s/22.01s/26.08s
deadline_expired=0
epoch_superseded_incomplete=0
```

这轮唯一失败来自 rolling-cache ffmpeg copy/remux：

```text
event_id=ba4cefd4-0717-4663-b9fd-118294ba3877
artifact=/data/video-analytics/artifacts/readyat_asyncpool_current_w60_rolling_live_direct_p60_20260706T091624Z/media_worker_logs_since_start.txt
symptom=RollingCacheError: ffmpeg_failed:1
gap=失败后的 rolling_cache_ffmpeg.log / retry log 被 cleanup 删除，错误原因不可诊断
```

已补的诊断改动：

- `_default_command_runner` 在 `ffmpeg_failed:<code>` 后追加 ffmpeg log tail；
- retry 失败时保留 initial attempt 的错误摘要；
- media-worker 终态化 reason 从单纯
  `rolling_cache_error:RollingCacheError` 改为包含异常摘要，后续 report/DB 中能看到
  ffmpeg stderr 尾部；
- 新增测试覆盖 log tail 和 initial/retry 双失败场景。

验证：

```text
python -m py_compile services/media-worker/app/rolling_cache.py services/media-worker/app/worker.py
pytest -q harness/tests/test_rolling_cache_materialization.py harness/tests/test_media_worker_perf_safety.py
result=32 passed
```

### 2026-07-06 固定输入、事件时间戳与短 GOP rolling-cache 复测

本轮针对用户提出的两个问题继续收敛：

- “400s 到底是起始窗口还是事件生成总耗时”；
- “10s evidence 为什么还会等几十秒”。

修复与结论：

- 行为事件时间戳此前优先使用 DeepStream `ntp_timestamp`。在固定视频/压测 backlog
  场景中，`ntp_timestamp` 会更接近“处理时刻”，不是“画面时刻”，导致 60s pilot
  被统计成接近 200s。已改为：当 `frame_meta.pts/buf_pts` 归一化后是 epoch ms
  时，行为事件优先使用 PTS；只有 PTS 是相对时间时才回退到 `ntp_timestamp`。
- pressure runner 的 observed-window gate 不再用 `created_at_span_s` 判失败。
  `created_at_span_s` 表示后台处理/入库延迟，不能代表输入画面窗口；窗口判定只看
  `event_ts_span_s`。
- 60 路 transcode RTSP republisher 暴露出输入侧 GOP 问题：rolling-cache writer
  配置是短 segment，但实际 segment duration 在普通 transcode 源下被关键帧间隔拖长：
  p50 51.611s、p95 86.479s、max 134.378s。这解释了“10s 证据为什么等几十秒”：
  不是 copy/remux 10s 很慢，而是在等超长 segment 关闭。
- transcode republisher 已补短 GOP 参数：
  `-g 8 -keyint_min 8 -sc_threshold 0 -bf 0`。
- 为避免 60 个 host ffmpeg 同时 transcode 污染压测，生成了固定 fixture：
  `/data/video-analytics/pressure-fixtures/1080movie_o300_8fps_gop8_520s.mp4`，
  8fps、520s、H264、短 GOP。后续固定输入压测优先用 copy-mode republish 该 fixture。

验证：

```text
PYTHONPYCACHEPREFIX=/tmp/pycache-video-analytics python -m py_compile \
  scripts/runtime/run_midterm_pressure60.py \
  modules/savant_security/custom/adapters/person_pose_adapter.py

pytest -q \
  harness/tests/test_midterm_pressure60_script.py \
  harness/tests/test_person_pose_adapter.py \
  harness/tests/test_rolling_cache_materialization.py \
  harness/tests/test_media_worker_perf_safety.py \
  harness/tests/test_event_worker_recording_policy.py

result=114 passed
```

最新 60 路固定 fixture-copy 压测：

```text
run_id=readyat_fixedfull_w60_fixturecopy_20260706T112858Z
input=/data/video-analytics/pressure-fixtures/1080movie_o300_8fps_gop8_520s.mp4
mode=copy RTSP republish
duration_s=400
streams=60
status=failed_pressure_gates
failure_reasons=forwarder_did_not_see_all_sources,savant_did_not_see_all_sources,validate_seq_iq_exceeded
```

这轮 gate 没有全绿，但失败点已经转移到固定 RTSP/source 接入侧，不是 rolling-cache
materializer 的 deadline/epoch/orphan 问题：

```text
before_cleanup.events=1052
before_cleanup.new_events=632
before_cleanup.suppressed_events=420
before_cleanup.tasks=632
before_cleanup.playable_bundles=629
before_cleanup.active_materialization_tasks=3
before_cleanup.ready_waiting_materialization_tasks=3

after_cleanup.events=629
after_cleanup.tasks=629
after_cleanup.playable_bundles=629
after_cleanup.task_statuses=materialized:629
after_cleanup.event_types=intrusion:477,watchlist_hit:152
8090_api_total=629
```

cooldown 与窗口：

```text
cooldown_scope=source:event_type
cooldown_s=30
cooldown_violation_count=0
event_ts_span_s=413.806
created_at_span_s=412.521
max_created_minus_event_ts_s=5.852
```

rolling-cache 与 materializer 性能：

```text
rolling_cache_segment_duration_s p50=20.584 p95=35.848 p99=42.082 max=138.957
rolling_cache_metadata_visible_lag_s p50=1.781 p95=4.182 p99=6.197 max=11.508

media_queue_wait_ms p50=19095 p95=30123.55 max=31511
media_claim_wait_ms p50=3 p95=18 max=121
media_finalization_duration_ms p50=243 p95=438 max=1100
media_lifecycle_elapsed_ms p50=19338.5 p95=30491.4 max=31957
```

解释：

- `ready_at`/claim 机制有效：claim wait p95 18ms，说明 worker 没有再傻等；
- 真正剩余的几十秒主要来自 segment close/ready 以及 source 接入节奏，而不是
  finalization/remux 本身。finalization p95 438ms；
- fixture-copy 比直接 60 路 transcode 明显更稳定，最终保留 629 条可播放证据；
- 但 `max_forwarder_sources=59`、`max_savant_sources=59`，说明固定 RTSP/source
  bring-up 仍有 1 路没被观测到，且 `validate_seq_iq_exceeded` 仍需继续处理；
- 当前 8090 页面应显示 629 条该轮证据，且 API 首条返回
  `raw_clip_available=true`、`annotations_available=true`。

后续补刀：

- pressure runner 已增加 source visibility barrier：60 路 source adapter 启动后，必须
  连续看到所有 pressure source 都进入 forwarder/Savant metrics，才开始 400s 采样窗口。
  这避免把 source bring-up/preroll 抖动混进“400s 压测窗口”，也避免 59/60 这种接入侧
  问题拖到压测末尾才暴露。
- barrier 会输出 `pressure_source_visibility_ready.json` 和
  `pressure_source_visibility_snapshots.json`；若超时，先按缺失 source 做一次显式
  stop/start 重启，再不齐则 fail early，并在报告里留下缺失 source 列表。
- barrier 之后、正式采样之前会清理 pressure warmup rows。也就是说 400s 是
  “所有 source 已可见后的正式采样窗口”，不是 source adapter 启动到事件完全 drain
  的墙钟总耗时。
- media-worker fallback grace 已从 5s 调到 9s，与 event-worker 写入
  `materialization_ready_at` 的 9s 保持一致。权威 ready time 仍由 event-worker 入库；
  fallback 只覆盖旧行/空值行，避免旧任务比新任务更早被 claim。

补刀验证：

```text
PYTHONPYCACHEPREFIX=/tmp/pycache-video-analytics python -m py_compile \
  scripts/runtime/run_midterm_pressure60.py \
  harness/tests/test_midterm_pressure60_script.py

pytest -q harness/tests/test_midterm_pressure60_script.py
result=69 passed

pytest -q \
  harness/tests/test_midterm_pressure60_script.py \
  harness/tests/test_person_pose_adapter.py \
  harness/tests/test_rolling_cache_materialization.py \
  harness/tests/test_media_worker_perf_safety.py \
  harness/tests/test_event_worker_recording_policy.py
result=116 passed
```

补刀后的 60 路固定 fixture-copy 压测：

```text
run_id=readyat_visibility_w60_fixturecopy_20260706T195338Z
input=/data/video-analytics/pressure-fixtures/1080movie_o300_8fps_gop8_520s.mp4
mode=copy RTSP republish
duration_s=400
streams=60
status=passed
warnings=validate_seq_iq_expected_sampling_gap
kept_evidence=581
8090_total=581
```

source visibility barrier 生效：

```text
visibility_status=ready
forwarder_visible_count=60
savant_visible_count=60
restart_attempts=1
restarted_sources=7
sampling_started_at=2026-07-06T12:02:53Z
```

barrier 前的 warmup rows 已清理：

```text
warmup_events_deleted=1127
warmup_face_observations_deleted=4387
warmup_person_observations_deleted=7068
warmup_evidence_dirs_removed=639
```

事件与 cooldown：

```text
events_before_cleanup=874
new_events=596
suppressed_events=278
event_ts_span_s=418.033
created_at_span_s=416.827
cooldown_scope=source:event_type
cooldown_violation_count=0
min_delta_ms=30005
paper_upper_bound_new_events=1680
new_events_by_type=intrusion:483,watchlist_hit:113
```

evidence 结果：

```text
tasks_before_cleanup=596
materialized=581
materialization_failed=11
materialization_deferred=4
blocking_materialization_tasks=0
after_cleanup.events=581
after_cleanup.tasks=581
after_cleanup.playable_bundles=581
8090.health=index_source:database
8090.annotation_checked=581
8090.annotation_ok=581
```

性能：

```text
ready_to_claim_s p50=0.560 p95=3.634 max=54.697
claim_to_materialized_s p50=0.971 p95=1.297 max=5.434
media_claim_wait_ms p50=2 p95=19 max=942
media_finalization_duration_ms p50=195.5 p95=393 max=3444
media_lifecycle_elapsed_ms p50=15482.5 p95=25069.95 max=67088
```

这轮结论：

- pressure gate 已通过，之前 59/60 的问题由 source visibility barrier 兜住；
- `ready_at` 后真正 claim 到 materialized 的 p95 约 1.3s，说明“worker 傻等几十秒”
  已经被拆掉；
- 页面慢和 400s 后还要等，主要是“事件 quiescence + drain + 8090 全量 retained
  evidence 检查”的测试收尾，不是 10s clip remux 本身；
- 当前 8090 页面保留的是这轮 581 条可播放 pressure evidence，lab source 已恢复为唯一
  enabled camera。

另外补了 ready-claim 顺序索引：

```text
migration=db/migrations/026_evidence_task_ready_claim_order_idx.sql
index=evidence_tasks_ready_claim_order_idx
columns=priority DESC, materialization_ready_at, created_at
```

验证补充：

```text
pytest -q \
  harness/tests/test_midterm_pressure60_script.py \
  harness/tests/test_midterm_worker_indexes_static.py \
  harness/tests/test_person_pose_adapter.py \
  harness/tests/test_rolling_cache_materialization.py \
  harness/tests/test_media_worker_perf_safety.py \
  harness/tests/test_event_worker_recording_policy.py
result=122 passed
```

2026-07-06 追加修正：`materialization_deferred` 不能再算作 rolling-cache
active/candidate 状态。`rolling_cache_event_frame_not_covered` 这类带 reason 的
deferred task 已经是终态；如果继续留在 claim/drain 状态集合或 ready-claim index
谓词里，会被 media-worker 反复捞取，表现为压测 drain 被少量 deferred 行永久卡住。

修复内容：

```text
services/media-worker/app/worker.py
  ROLLING_CACHE_TASK_STATUSES = manifest_ready, materialization_pending, pending

scripts/runtime/run_midterm_pressure60.py
scripts/tools/check_midterm_evidence_drain.py
  ACTIVE_MATERIALIZATION_STATES 不再包含 materialization_deferred
  只有 deferred 且 reason 为空才作为 unresolved/blocking

db/migrations/025_evidence_task_materialization_ready_at.sql
db/migrations/026_evidence_task_ready_claim_order_idx.sql
db/migrations/027_evidence_task_ready_indexes_terminal_deferred.sql
  ready-at/claim-order indexes 不再索引 materialization_deferred 终态行
```

本机 DB 已应用 027 后的索引谓词：

```text
evidence_tasks_materialization_ready_idx:
  materialization_status IN ('manifest_ready','materialization_pending','pending')
evidence_tasks_ready_claim_order_idx:
  materialization_status IN ('manifest_ready','materialization_pending','pending')
```

重新验证：

```text
py_compile:
  scripts/runtime/run_midterm_pressure60.py
  scripts/tools/check_midterm_evidence_drain.py
  services/media-worker/app/worker.py
  services/event-worker/app/repository.py
  services/api/app/services/runtime_apply.py

pytest -q \
  harness/tests/test_midterm_pressure60_script.py \
  harness/tests/test_midterm_worker_indexes_static.py \
  harness/tests/test_person_pose_adapter.py \
  harness/tests/test_rolling_cache_materialization.py \
  harness/tests/test_media_worker_perf_safety.py \
  harness/tests/test_event_worker_recording_policy.py \
  harness/tests/test_camera_runtime_apply_service.py
result=143 passed

docker compose -f infra/docker-compose.midterm.yml config --quiet
git diff --check
scripts/tools/check_midterm_evidence_drain.py --timeout-s 30 --poll-s 2
result=drain_complete true, active_evidence_tasks=0, active_replay_slots=0
```

同日补充：重跑 `readyat_deferredfix_w60_fixturecopy_20260706T130820Z` 时，正式
400s 采样没有开始，source visibility barrier 正确失败：

```text
forwarder_visible_count=51/60
savant_visible_count=51/60
missing=01,08,22,23,27,34,40,53,54
```

这 9 路不是 evidence 侧失败，source adapter 日志显示 RTSP 拉流初始化超时：

```text
ffmpeg_input: Unable to initialize the worker thread. Error is: Timeout
ffmpeg_src: Failed to start element: Timeout
FFMPEG_TIMEOUT_MS=20000
```

60 路同时从本机 RTSP republisher 拉流时，20s adapter 启动预算太紧。修复为：

```text
scripts/runtime/camera_source_controller.py
  新增 --ffmpeg-timeout-ms / CAMERA_SOURCE_FFMPEG_TIMEOUT_MS

scripts/runtime/run_midterm_pressure60.py
  新增 --pressure-source-ffmpeg-timeout-ms
  pressure 默认 60000ms，并传给初始 start 和 visibility restart

harness/tests/test_camera_source_controller.py
  覆盖自定义 FFMPEG_TIMEOUT_MS=60000
```

重新验证：

```text
pytest -q \
  harness/tests/test_camera_source_controller.py \
  harness/tests/test_midterm_pressure60_script.py \
  harness/tests/test_midterm_worker_indexes_static.py \
  harness/tests/test_rolling_cache_materialization.py \
  harness/tests/test_media_worker_perf_safety.py \
  harness/tests/test_event_worker_recording_policy.py \
harness/tests/test_camera_runtime_apply_service.py
result=153 passed
```

第三轮 `readyat_sourceffmpeg60b_w60_fixturecopy_20260706T134009Z` 确认：初始
start 和 visibility restart 都使用了 `FFMPEG_TIMEOUT_MS=60000`，但 60 路仍未
全部进入指标面：

```text
forwarder_visible_count=53/60
savant_visible_count=53/60
missing=17,18,20,22,35,42,50
drain_complete=true
```

结论：单纯调大 RTSP source adapter 初始化 timeout 不够。当前 60 个 adapter
几乎同时拉同一个本机 RTSP republisher，启动瞬间会形成 RTSP pull burst。继续
用 53/60 或 56/60 的输入去评价 evidence 吞吐会污染结论，所以 barrier 失败是
正确行为。

新增 harness 修复：

```text
scripts/runtime/run_midterm_pressure60.py
  --pressure-source-start-stagger-s，默认 0.5s
  初始 pressure source start 之间按小间隔错峰，避免 60 路同时拉流
```

验证：

```text
py_compile scripts/runtime/run_midterm_pressure60.py
pytest -q harness/tests/test_midterm_pressure60_script.py
result=70 passed
git diff --check
```

第四轮 `readyat_stagger_w60_fixturecopy_20260706T135503Z` 使用：

```text
FFMPEG_TIMEOUT_MS=60000
pressure_source_start_stagger_s=0.5
visibility_timeout_s=300
restart_attempts=1
```

结果仍未进入正式 400s 采样：

```text
forwarder_visible_count=55/60
savant_visible_count=55/60
missing=04,22,32,44,50
drain_complete=true
```

因此当前不能继续用这条 60 路 RTSP-republish 输入链路评价 ready_at/evidence
吞吐；输入面本身没有稳定满足 60/60。下一步应改 pressure 输入方式，而不是继续
放宽 evidence 指标：

```text
优先方向 A：让 pressure source adapter 直接读取固定文件/挂载文件，绕开 60 路
            本机 RTSP republisher 拉流启动风暴；
备选方向 B：继续使用 RTSP，但增加更强的分批启动/分批可见性 barrier，并把
            mediamtx/ffmpeg_src 连接失败作为单独的 source-readiness 指标。
```
