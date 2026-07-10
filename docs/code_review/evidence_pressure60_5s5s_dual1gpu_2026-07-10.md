# 60 路单卡双分支 5+5 秒 Evidence 压测报告（2026-07-10）

## 1. 运行结论

本轮在本机完成了 60 路、单张 RTX 4090、双 Savant 分支、400 秒正式采样的 rolling-cache evidence 压测。YOLO26 pose 与 YOLOv8 face detector 均为 batch 4，AdaFace 为 batch 16；video evidence policy 统一为 pre 5 秒、post 5 秒，采样后 drain 为 120 秒。

Run ID：

`pressure60_8p1_dual1gpu_5s5s_yolob4_ada16_drain120_20260710T054418Z`

Artifact：

`/data/video-analytics/artifacts/pressure60_8p1_dual1gpu_5s5s_yolob4_ada16_drain120_20260710T054418Z/`

最终状态为 `failed_pressure_gates`，但失败项只有：

- `forwarder_queue_full`
- `validate_seq_iq_exceeded`

Evidence 固化本身没有 deadline expiry、annotation incomplete 或 drain 未收敛：372 个未抑制事件创建了 372 个 tasks，371 个 materialized，1 个 `duration_guard_failed`；清理后保留 371 个可用 bundles。120 秒 drain 足够，实际停止 source 后约 34 秒 active/pending tasks 已归零。

本轮最重要的结论是：**rolling segment 写入很快，落盘慢主要发生在 media-worker 的 ready 后队列、批量 ffprobe 和同步 finalizer 阶段。**

## 2. 压测参数

| 参数 | 生效值 |
| --- | --- |
| 输入 | `rtsp://192.168.1.105:8554/live/1080movie` |
| stream 数 | 60 |
| 拓扑 | 单 GPU、双 Savant 分支，balanced 30/30 |
| GPU | GPU 0，RTX 4090；GPU 1 空闲 |
| analysis FPS | `8/1` |
| YOLO26 pose batch | 4 |
| YOLOv8 face batch | 4 |
| AdaFace batch | 16 |
| face / embedding interval | 7 / 7 |
| evidence 模式 | rolling cache，Replay record request suppressed |
| evidence policy | 单一 `5:5` group |
| 正式采样 | 400s |
| rolling prefill / postfill | 25s / 25s |
| drain | 120s |
| cooldown | source + event type 维度 60s |
| finalizer workers | 32 |

60/60 sources 在约 10 秒内同时被 forwarder 和 Savant 看见，无 source restart；400 秒计时从 visibility gate 和 rolling prefill 完成后才开始。

## 3. Evidence 结果

| 指标 | 结果 |
| --- | ---: |
| 正式窗口 events | 1,162 |
| suppressed / unsuppressed | 790 / 372 |
| materialized / failed / expired | 371 / 1 / 0 |
| 最终保留 bundles | 371 |
| intrusion video bundles | 252 |
| watchlist image bundles | 119 |
| 8090 bundle check | 371 / 371 OK |
| video timeline check | 252 / 252 OK |
| video annotation check | 252 / 252 OK |
| video duration check | 252 / 252 OK |

252 个 video bundles 全部属于 `5:5`，允许时长误差为 1.25 秒；unexpected window、duration mismatch、missing policy 和 missing duration 均为 0。

### 3.1 “全部 5+5 秒”的实际语义偏差

pressure camera policy 虽然统一生成 `clip_required=true`、`snapshot_required=false`，但 harness 会单独给 `watchlist_hit` 写入 face-image policy。最终 119 个 watchlist events 实际为：

```text
clip_required=false
snapshot_required=true
evidence_state=image_ready
```

所以本轮证明的是“全部 video evidence 都是 5+5 秒”，不是“watchlist 与 intrusion 每一个事件都有 5+5 秒视频”。如果验收合同要求 watchlist 也必须生成 10 秒视频，当前 pressure harness / event policy 不满足该合同，需要先明确并修改策略后再跑；本轮没有通过改业务代码绕过该语义。

### 3.2 单个失败存在状态矛盾

唯一失败 task 的外层状态和 failure reason 是 `duration_guard_failed`，但其诊断同时记录：

- input duration `9.677s`；
- 5+5 秒目标、最大允许时长 11 秒；
- `duration_guard_status=passed`；
- `duration_guard_failed=false`。

这是失败状态与内层 guard 结果不一致，不像真实的时长越界。该 task 在最终保留集清理时被移除，因此 371 个保留 bundles 均可用，但此处需要单独修正状态归因或补充失败阶段字段。

## 4. 落盘延迟分解

### 4.1 数据库任务生命周期

| 阶段 | p50 | p95 | max / p99 |
| --- | ---: | ---: | ---: |
| evidence task 生命周期 | 24.18s | 45.13s | p99 45.91s |
| ready 到 claim | 7.79s | 33.95s | max 38.32s |
| claim 到 materialized（video） | 14.55s | 25.59s | max 42.65s |

### 4.2 media-worker 日志分解（306 个 finalizer 样本）

| 阶段 | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| media queue wait | 18.16s | 39.16s | 57.73s |
| finalization | 8.92s | 12.02s | 12.80s |
| lifecycle | 27.14s | 44.89s | 58.55s |
| ffprobe work | 7.01s | 10.37s | 10.37s |
| ffprobe ready 到 finalizer start | 4.21s | 16.42s | 40.37s |
| finalizer pool wait | 0.024s | 15.22s | 16.31s |
| DB claim wait | 0.025s | 0.545s | 1.91s |

结论：DB row claim 锁不是主因；ready 后未被及时调度，以及进入 pool 后等待批次收口，才是主要等待。

## 5. 为什么不是 rolling segment 写盘慢

本轮测得 1,368 个 finalized rolling segments，覆盖 60 个 sources：

| 指标 | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| segment duration | 3.963s | 4.417s | 4.856s |
| segment metadata 可见延迟 | 0.226s | 0.816s | 0.974s |
| segment FPS | 24.245 | 25.252 | 26.914 |

full-rate gate 通过，metadata parse errors 为 0。segment metadata 在 1 秒内可见，而 evidence p95 生命周期超过 45 秒，因此不能把慢归因于 rolling sink 需要长时间写完分段。固定 9 秒 ready grace 仍是可削减成本，但最新 p95 的 33.95 秒 ready-to-claim 表明它不是主导项。

## 6. 主要慢点

### 6.1 probe 指标是并发批次累计值，不是可靠的 per-job 值

压力上升后，单个 finalizer log 经常同时显示约 55-57 个 `metadata_files_visited`、18-32 次 ffprobe，以及 2.7-10.4 秒 ffprobe duration。同一批 jobs 会记录相同或相近的计数。源码中的 `_PROBE_METRICS` 是进程级全局字典，每个并发 job 都用全局开始/结束快照求差；它会把同一时段其他 finalizer 的 probe 一并算入当前 job。因此这些数值证明整批 probe 工作很重，但不能解释成“每个 job 独立执行了 18-32 次 ffprobe”。当前 instrumentation 缺少 thread/job 隔离，会放大 per-job 归因。

### 6.2 32 finalizers 没有形成有效的全局限流

media-worker 峰值 CPU 为 `1475.75%`，约占用 14.76 个 CPU 核；clip-worker 峰值仅 `0.95%`，且本轮没有 record requests、Replay admission 或 Replay job create 数据。CPU burst 与 queue wait 上升同步，静态审查中 `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE` 未约束 finalizer pool 的问题在本轮再次被运行数据验证。

### 6.3 异步 remux 后仍同步等待 finalizer batch

队列会阶段性清空，但后续任务波次仍从约 2-7 秒 queue wait 逐渐增长到 39-46 秒，最高 57.73 秒。说明问题不是永久卡死，而是 persistent rolling runner 在完成一批 remux 后，同步执行 metadata/ffprobe/finalizer/sidecar/DB index，下一批只能等待。

### 6.4 PostgreSQL 不是本轮入口堵塞

Redis 的 events、face observations、person observations consumer lag 和 pending 均为 0。DB claim wait p50 仅 24.5ms。PostgreSQL 累计统计仍有大量 seq scans，但本轮直接时延证据首先指向 media-worker 内部批次，而不是 Redis consumer 或 DB claim lock。

## 7. 与昨晚最新批次对比

| 指标 | 昨晚 5/10/15 混合窗口 | 本轮统一 5+5 | 变化 |
| --- | ---: | ---: | ---: |
| lifecycle p50 | 46.42s | 24.18s | -47.9% |
| lifecycle p95 | 111.62s | 45.13s | -59.6% |
| ready-to-claim p50 | 49.51s | 7.79s | -84.3% |
| ready-to-claim p95 | 86.09s | 33.95s | -60.6% |
| media queue p50 | 78.42s | 18.16s | -76.8% |
| media queue p95 | 113.54s | 39.16s | -65.5% |
| finalization p95 | 18.19s | 12.02s | -33.9% |
| materialization expired | 1 | 0 | 消除 |
| playable retained | 521 | 371 | 事件密度不同，不直接比较吞吐 |

统一 5+5 明显降低延迟，但 p95 仍为 45 秒，远高于 segment 可见时间。这说明较长窗口会放大问题，但不是根因；根因仍是 media-worker 的批次调度与 finalizer 工作模型。

## 8. 独立的上游压力失败

- forwarder max queue depth：3,117；
- queue-full samples：1；
- Savant send failures sampling delta：0；
- forwarder / Savant sources：60 / 60；
- forwarded target ratio：1.0695；
- `validate_seq_iq`：178,311 行。

这两个失败项说明 8 FPS 双分支仍有上游队列/序号连续性压力，但它们不解释 rolling segment 可见后几十秒的 media queue wait，应作为独立验收轴处理。

## 9. 优先处理建议（本轮未修改代码）

1. 让一个共享的全局 active limiter 同时约束 remux 与 finalizer pool，而不是每个 pool job 自建 guard。
2. 将 rolling runner 的 completed-future drain 与 finalizer batch 解耦，避免主 poll 同步等待整批 sidecar/DB index 完成。
3. 把 source/epoch segment index 做成长生命周期 manifest/cache，避免每轮 `rglob` 和重复 metadata JSON 解析。
4. 将 ffprobe 作用域收敛到最终产物，避免批次内对多个中间文件重复 probe；同时区分 batch totals 与 per-job metrics。
5. 使用 PostgreSQL connection pool，避免 32 finalizers burst 时逐 job 建连。
6. 修正 `duration_guard_failed` 与内层 `duration_guard_status=passed` 的状态矛盾。
7. 明确 watchlist evidence 产品合同：图片证据还是 5+5 秒视频；压测参数和验收报告必须使用同一语义。

## 10. 当前代码干净度与排队机制复核

### 10.1 结论

当前实现具备可工作的完整链路，但不属于干净、边界清晰的实现。`services/media-worker/app/worker.py` 已有 8,296 行、约 187 个函数/类，同时承载 rolling task claim、image extraction、segment remux、sink scan、finalizer、annotation、sidecar、DB index 和 cleanup。重复 `_metadata_labels()`、production 不可达的旧同步 rolling 分支、重复 expiry 查询和未使用的 fallback 配置都说明多轮演进没有完成收口。

### 10.2 为什么 ready 后仍排队

1. 每个 video task 首先有策略等待：event time + post 5s + segment grace 9s。这是 ready 前的业务/安全等待，不属于 ready-to-claim 指标。
2. 每次 rolling poll 先同步执行 `_process_rolling_cache_image_tasks()`，逐个处理 image-only watchlist task并调用 ffmpeg 抽帧，然后才执行 video runner。本轮有 119 个 watchlist image tasks，因此图片线路会阻塞视频 claim。
3. video runner 虽有 60-thread persistent remux pool，但 `_drain_completed()` 在 media-worker 主线程同步调用 finalizer batch。finalizer 未完成时，标称 1s 的下一轮 poll 无法执行。
4. 每批 finalizer 都临时创建 `ThreadPoolExecutor(32)`，`with` 作用域会等待整批 futures 完成。这形成 batch barrier，直接对应本轮 finalizer pool wait `p95=15.22s`。
5. `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=4` 只在外层创建 guard；pool 内每个 job 使用自己的 `_MaterializationGuard(1)`，所以全局上限没有约束 32 个 finalizers。本轮峰值 `1475.75% CPU` 是运行证明。
6. 每个 finalizer job 单独 `psycopg.connect()`，随后同步构建 sidecar，并按帧 upsert timeline/overlay DB rows；这些都位于 materialized 状态收敛前的关键路径。
7. `find_segments()` 对每个 source 执行 `rglob("metadata.json")`；segment cache 只活在单次 poll/process 中，下一轮重新扫描。`_select_rows()` 又重读选中的 metadata。

### 10.3 两个“queue”指标不是同一含义

- DB `ready_to_claim` 是 task 已到 ready_at 后、真正被 worker claim 前的等待，本轮 `p50=7.79s/p95=33.95s`，最能说明 poll 被 image/finalizer 阻塞或 remux capacity 已满。
- 日志 `queue_wait_ms` 实际是 `events.created_at -> finalizer started_at`，本轮 `p50=18.16s/p95=39.16s`。它把 ready 前剩余等待、claim、rolling remux 和 finalizer submit wait 混在一起，不是单一队列深度。

因此“为什么还要排队”的精确回答是：任务不是只进入一个队列，而是依次经过 ready schedule、主线程 image-first 阶段、60-slot remux pool、同步 completed drain、32-thread finalizer batch barrier，最后才进入 sidecar/DB index。任一阶段占住主循环，后续 ready tasks 都继续累计。

### 10.4 其他代码语义问题

- `_PROBE_METRICS` 为无 job/thread 隔离的全局计数，并发日志不能可靠表达单任务 probe 成本。
- `materialization_deferred` 在 worker 中是 retryable，但 migration 027 把它定义为 terminal 并排除出 ready partial indexes。
- `_process_rolling_cache_tasks()` 和 runner 都调用 overdue expiry，形成每 poll 重复 UPDATE 查询。
- `ROLLING_CACHE_FALLBACK_TO_REPLAY` 被读取但生产逻辑不使用。
- watchlist image policy 与“所有 evidence 都是 5+5 秒视频”的表述不一致。

## 11. 恢复与验证

- 60 个 pressure source 已全部删除，无残留容器；
- dual-shard、rolling sinks、worker 临时环境均已恢复；
- 日常单分支 Savant、analysis-forwarder、Replay、event/clip/media/face workers 均运行；
- `/api/v1/cameras` 返回 HTTP 200；
- 原 `lab` camera 已恢复为 enabled 并重新启动；
- 本轮未修改业务代码或配置文件。
