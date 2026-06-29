# Midterm media-worker finalizer pool 8 FPS 压测报告

日期：2026-06-29

## 结论

本轮实现并验证了 media-worker 第二阶段扩展模型：单进程内部 finalizer worker pool。
该模型不改变现有 evidence 存储方式，不新增多 media-worker 容器，也不改变 8090 产品语义。

60 路同卡双分支 8 FPS retained-evidence 压测通过：50/50 retained evidence 可播放，
8090 list/detail 50/50 可查，analysis-forwarder queue full 为 0，Savant send failures 为 0，
duplicate materialization 为 0，finalizer failed 为 0，imageio fallback 为 0。

和 Qdrant authoritative 8 FPS 基线相比，media queue wait p95 从约 302.6 秒降到约 175.5 秒，
lifecycle p95 从约 306.0 秒降到约 181.3 秒。目标 60-90 秒尚未达到，剩余等待已经不再主要是
Qdrant 查询；它由 clip-worker proof/replay 调度、Replay sink 输出到达时间、以及 4 个
finalizer worker 的 CPU/IO 处理窗口共同构成。

## 实现范围

- 新增 `MEDIA_WORKER_FINALIZER_WORKERS`，当前 midterm 默认值为 4，代码 fallback 为 1。
- media-worker 内部使用 `ThreadPoolExecutor` 执行 finalizer job。
- 按 source 做公平调度：同一 source 同一轮最多安排 1 个 finalization，不同 source 可并行。
- 保留 deadline-aware pacing、deadline guard、CPU thread limit、sink stability check 和 cleanup 逻辑。
- 对 evidence task claim 使用 `FOR UPDATE SKIP LOCKED`，claim 后写入 `finalizing` 和 finalizer audit 字段。
- 终态更新保持幂等：重复 terminal/ready 不重复生成 bundle，不重复 materialized。
- `media_event_finalized` 增加 `worker_id`、`source_id`、`replay_shard_id`、`claim_wait_ms` 等字段。
- pressure report 增加 finalizer worker counts、按 source/shard 的 queue wait、claim wait 和 duplicate materialization 指标。

## 验证配置

小规模 smoke：

- Run ID：`smoke_media_finalizer_pool_8src_4fps_20260629T1432Z`
- Artifact：`/data/video-analytics/artifacts/smoke_media_finalizer_pool_8src_4fps_20260629T1432Z`
- 8 路、4 FPS、保留 8 条 evidence
- 结果：passed，8/8 retained 8090 OK，queue wait p95 约 50.1 秒，finalizer failed 0，duplicate 0，imageio fallback 0

60 路 pressure：

- Run ID：`pressure60_media_finalizer_pool_8fps_20260629T143548Z`
- Artifact：`/data/video-analytics/artifacts/pressure60_media_finalizer_pool_8fps_20260629T143548Z`
- 拓扑：同卡双分支 30+30，`--dual-shard-same-gpu --dual-shard-api --dual-shard-gpu 0`
- FPS：`8/1`
- Batch：`BATCH_SIZE=4`，`POSE_BATCH_SIZE=4`，`FACE_DETECTOR_BATCH_SIZE=4`，`FACE_EMBEDDING_BATCH_SIZE=16`
- `MAX_PARALLEL_STREAMS=32`
- 压力时长：300 秒
- drain：600 秒
- 保留证据：50 条
- Evidence length groups：`2:2,3:3,5:5,8:8,10:10,15:15`

## 关键结果

| 指标 | 结果 |
| --- | ---: |
| pressure status | passed |
| retained evidence | 50 |
| retained playable | 50/50 |
| 8090 evidence proof | 50/50 OK |
| source containers | 60 running, 0 exited, 0 restarted |
| negative PTS | 0 |
| analysis-forwarder queue_full samples | 0 |
| Savant send failures | 0 |
| final_forwarded_target_ratio | 0.9257 |
| Qdrant query p95 / p99 | 3ms / 5ms |
| Qdrant fallback / shadow mismatch | 0 / 0 |
| face-worker gallery query p95 / p99 | 5ms / 7ms |
| duplicate materialization | 0 |
| finalizer failed | 0 |
| imageio fallback | 0 |

cleanup 前数据库状态：

- 793 events / 793 evidence_tasks
- 102 bundles / 83 playable bundles
- 538 tasks 被 admission/backpressure 标记为 `materialization_skipped`
- 50 条 retained evidence 在 cleanup 后全部为 `materialized`

## media-worker 指标

| 指标 | 结果 |
| --- | ---: |
| finalized_count | 99 |
| finalizer workers used | 4 |
| worker counts | 33 / 28 / 21 / 17 |
| media-worker CPU peak | 1104.25% |
| claim_wait p50 / p95 / p99 | 1ms / 27.1ms / 762ms |
| finalization p50 / p95 / p99 | 4.351s / 7.433s / 10.903s |
| queue_wait p50 / p95 / p99 | 97.768s / 175.545s / 199.193s |
| lifecycle p50 / p95 / p99 | 99.628s / 181.293s / 205.493s |
| deadline_slack min | 78.579s |
| ffprobe p95 | 436.9ms |
| ffmpeg duration p95 | 0ms |

与旧基线对比：

| Run | queue_wait p95 | lifecycle p95 | retained/8090 |
| --- | ---: | ---: | ---: |
| `pressure60_qdrant_authoritative_8fps_20260629T130224Z` | 302.637s | 306.049s | 50/50 |
| `pressure60_media_fullobs_8fps_20260629T092901Z` | 189.913s | 192.325s | 50/50 |
| `pressure60_media_finalizer_pool_8fps_20260629T143548Z` | 175.545s | 181.293s | 50/50 |

## 当前瓶颈判断

Qdrant 不是本轮瓶颈：Qdrant p95/p99 为 3ms/5ms，fallback 为 0。

media finalizer pool 已经降低了长尾排队，但没有把 queue wait p95 压到 60-90 秒。
原因是当前 `queue_wait_ms` 的定义是 event 创建到 media finalization 开始的总等待时间，
不是纯 media-worker 内部队列时间。finalization 自身 p95 约 7.4 秒，claim p95 约 27ms，
说明剩余长尾主要需要继续拆分为：

- clip-worker proof wait；
- Replay job 创建到 video-file-sink 输出的等待；
- sink output 稳定检查等待；
- media finalizer worker 可用等待。

本轮 pressure report 在采样点仍看到 `security.record_requests` pending/lag，但 cleanup 后 live
Redis `XPENDING security.record_requests clip-workers-midterm` 已回到 0。这说明 retained evidence
已完成，但非保留事件和 admission 后的低价值任务仍会在压力窗口内形成短时 clip/replay backlog。

## 下一步

1. 在 report schema 中继续拆 `queue_wait_ms`：新增 proof wait、replay job elapsed、sink ready wait、
   finalizer claim wait、finalizer execution wait。
2. 用同一 profile 对比 `MEDIA_WORKER_FINALIZER_WORKERS=6/8`，观察 queue wait p95 是否能进入
   60-90 秒，同时约束 media-worker CPU 峰值、deadline slack 和 finalizer failed。
3. 如果增加内部 worker 后仍由 proof/replay 堵住，则优化 clip-worker proof/replay 调度，而不是继续加 media worker。
4. 只有当内部 worker pool 无法满足生产目标时，再评估多 media-worker 容器或独立 finalizer service。

本轮可以关闭“Qdrant 导致证据后处理变慢”的假设，也可以确认 internal finalizer pool 的方向有效；
但不能宣称全量事件 materialization 已经可在 60 路 8 FPS 下完成。
