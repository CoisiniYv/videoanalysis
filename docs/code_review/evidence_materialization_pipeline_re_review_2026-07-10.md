# Evidence 固化链路重新审查（2026-07-10）

## 1. 审查范围与结论

本次审查基于当前工作树，只读取源码、配置、迁移、测试、Docker 运行态和最近一次完整 pressure60 产物；未修改业务代码、配置或运行服务。

审查范围：

- `services/event-worker`
- `services/clip-worker`
- `services/media-worker`
- Replay / video-file-sink / rolling-cache sink
- `infra/docker-compose.midterm.yml`、`infra/env/midterm.env`
- evidence 相关数据库迁移与最近 pressure60 产物

结论不是“某一个 clip worker 写文件太慢”，而是下面四件事叠加：

1. **当前机器的 evidence 固化链路实际上不可用，而非只是慢。** PostgreSQL 未监听 `5432`，event-worker、clip-worker、media-worker 均处于重启循环；8090 `/health` 和 evidence `/health` 仍返回 200，掩盖了该故障。
2. **默认日常链路没有启用 rolling cache。** 当前容器实际值为 `ROLLING_CACHE_ENABLED=false`、`ROLLING_CACHE_MATERIALIZATION_ENABLED=false`，使用的是 event-worker -> clip-worker -> Replay -> video-file-sink -> media-worker。
3. **昨晚最新一次 rolling-cache pressure60 没有通过，且性能较此前通过批次明显退化。** 最新产物的 evidence 生命周期为 `p50=46.42s`、`p95=111.62s`、`p99=132.55s`；主要时间消耗发生在 ready 之后的 claim 积压、media queue 和 finalizer，而不是 rolling segment 写入或 Replay。
4. **代码中存在真实的冗余和配置语义漂移。** 包括未实现的 Replay fallback、未生效的并发配置、rolling-cache 双清理器、同步旧路径残留、重复函数定义，以及 deferred 状态与索引不一致。

因此，“总感觉比理想慢”有三个不同层次，不能混在一起：

```text
当前运行故障：DB 不可用，根本不生成
正常 Replay 路径：proof + admission + Replay + sink + finalizer 的多阶段等待
pressure rolling 路径：post/grace 主动等待 + rolling claim 排队 + remux/finalizer
```

## 2. 当前运行态：首先不是性能问题

观察时间：`2026-07-10T12:56:39+08:00`。

### 2.1 三个 evidence worker 正在重启循环

观察到：

| 容器 | restart count | 直接错误 |
| --- | ---: | --- |
| event-worker | 42 | PostgreSQL `172.17.0.1:5432` connection refused |
| clip-worker | 41 | 8 个 consumer 创建 PostgreSQL 连接失败，父进程退出后重启 |
| media-worker | 41 | 启动阶段 PostgreSQL 连接失败 |

宿主 `postgresql` systemd 状态为 inactive，`phase0-postgres` 已退出约 12 小时。当前 midterm 容器的 `DATABASE_URL` 是 `host.docker.internal:5432`，而启动时没有启用 `local-postgres` profile。

三个入口都没有启动连接重试：event-worker 在 `services/event-worker/main.py:46-47` 直接连接，media-worker 在 `services/media-worker/main.py:23-24` 直接连接；clip-worker 会先创建 8 个进程，每个在 `services/clip-worker/main.py:27-40` 各自连接并同时失败。这会把一个 DB 故障放大成每分钟大量异常日志和进程重建。

### 2.2 8090 健康信号会误报

- `http://127.0.0.1:8090/health` 返回 200，只检查 evidence 根目录存在，见 `services/evidence-viewer/app/main.py:202-212`。
- `/api/v1/evidence/health` 返回固定的 `status=ok,index_source=database`，没有实际查询 DB，见 `services/api/app/routers/evidence.py:61-63`。
- 同一时刻 `/api/v1/cameras` 和 `/api/v1/events` 均返回 500 connection refused。
- `scripts/midterm_start.sh:445-461` 只等待 8090 和 runtime overview；`scripts/midterm_start.sh:508` 又用 `wait_for_health || true` 吞掉失败，随后仍打印 Startup Complete。

这是一个明确的使用方式问题：8090 页面可打开、evidence health 为绿，不代表证据链路能够写 DB 或生成新 evidence。

## 3. 当前到底有几条线路

| 线路 | 当前日常容器 | pressure60 | 是否自动 fallback |
| --- | --- | --- | --- |
| Replay 路径 | 启用 | rolling pressure 中通过 suppress 关闭 per-event request | 是当前默认主线 |
| rolling-cache 路径 | 关闭，目录仅约 8 KB | 显式启用 profile 和环境覆盖 | 不是默认主线 |
| 历史 evidence-worker / clip-worker archive | 不启用 | 不启用 | 仅历史归档 |

默认 Replay 路径：

```text
event-worker
  -> evidence_tasks + security.record_requests
clip-worker
  -> post-Savant proof
  -> DB admission slot
  -> Replay /api/v1/job
video-file-sink
  -> video.mov + metadata.json
media-worker
  -> stability/probe/finalizer/sidecar/DB index
```

rolling pressure 路径：

```text
Replay raw fanout
  -> rolling-cache-sink 分段持续落盘
event-worker
  -> evidence_tasks，但 suppress record_request
media-worker rolling runner
  -> segment scan + concat/remux
  -> 共用 media finalizer/sidecar/DB index
```

这两条线路不是当前同时执行的双写，但它们在同一套 compose、状态机和 media-worker 中长期共存，已经产生较高维护复杂度。

## 4. 昨晚最新产物的延迟分解

证据来源：

`/data/video-analytics/artifacts/pressure60_8p1_dual1gpu_cd60_bs4_20260709T132615Z/`

该运行创建于 `2026-07-09T13:26:15Z`，报告于本地时间 `2026-07-09 21:40:56` 收口，是昨晚最新的完整 pressure60 产物。最终状态为 `failed_pressure_gates`，失败项为：

- `materialization_expired_present`
- `savant_send_failures`
- `forwarder_queue_full`
- `validate_seq_iq_exceeded`
- `evidence_8090_annotations_missing`
- `evidence_8090_annotations_incomplete`

清理前共有 4,054 个 events、561 个未 suppressed evidence tasks、537 个 bundles，其中 521 个可播放、517 个 materialized、43 个 materialization_failed、1 个 materialization_expired。清理后保留 521 个可播放 bundles，517 个 tasks 为 materialized、4 个为 materialization_failed，active tasks 为 0。

核心时延：

| 阶段 | p50 | p95 | max / p99 |
| --- | ---: | ---: | ---: |
| evidence task 生命周期 | 46.42s | 111.62s | p99 132.55s |
| `materialization_ready_at` 到 claim | 49.51s | 86.09s | max 106.82s |
| claim 到 materialized | 24.40s | 40.10s | max 92.15s |
| media queue wait | 78.42s | 113.54s | max 132.88s |
| 单次 finalization | 12.66s | 18.19s | max 18.82s |
| sink ffprobe ready 到 finalizer start | 6.64s | 29.76s | max 82.32s |
| finalizer pool wait | 0.022s | 26.49s | max 29.01s |
| ffprobe | 6.34s | 13.81s | max 13.81s |

与昨晚较早的通过批次对比：

| 报告收口时间 | 产物 | 状态 | 生命周期 p50 | 生命周期 p95 |
| --- | --- | --- | ---: | ---: |
| 18:01 | `pressure60_8p1_dual1gpu_cd60_rcfix4_dbonly_20260709T094613Z` | passed | 18.16s | 36.93s |
| 21:40 | `pressure60_8p1_dual1gpu_cd60_bs4_20260709T132615Z` | failed_pressure_gates | 46.42s | 111.62s |

### 4.1 固定等待存在，但不是最新批次的主导瓶颈

pressure runner 为 rolling 路径设置：

- post window 分组为 5s / 10s / 15s；
- `EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS=9`，见 `scripts/runtime/run_midterm_pressure60.py:2398-2404`；
- ready 时间由 event time + post window + grace 计算，见 `services/event-worker/app/repository.py:703-716`。

因此仅调度下限已经是约 14s / 19s / 24s，用户从事件发生开始计时，天然不会看到“立即生成”。

但最新产物测得 1,421 个 rolling segments、覆盖 60 个 sources，segment duration 为 `p50=3.961s`、`p95=4.426s`，metadata 可见延迟为 `p50=0.170s`、`p95=0.649s`、`max=4.115s`，observed segment FPS `p50=24.251`，full-rate gate 通过。固定 9s grace 仍偏保守，但相对最新批次几十秒到上百秒的 ready 后积压，它已经不是主导瓶颈。

### 4.2 主导瓶颈是 ready 之后的 media-worker 饱和

ready-to-claim `p50=49.51s`、`p95=86.09s`，media queue wait `p50=78.42s`、`p95=113.54s`，已经远大于 9s grace。media-worker 聚合 CPU 为 `1461.74%`，采样时常处于约 `1319%-1423%`，说明 60 个 rolling workers 与 32 个 finalizers 把瓶颈推到了同一进程内的 CPU、ffprobe、finalizer pool、sidecar 和 DB index 工作。

claim-to-materialized `p50=24.40s`、`p95=40.10s`；finalizer pool wait 的 `p95=26.49s`，sink ffprobe ready 到 finalizer start 的 `p95=29.76s`。这与静态代码发现一致：`MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE` 没有限制住 32 线程 finalizer pool，每个 job 新建 guard 和 PostgreSQL 连接，rolling runner 又同步等待 finalizer batch 收口。

最新批次没有 record requests，因此 clip-worker/Replay 不参与 retained rolling evidence 生成。该批次还出现 rolling-cache duration-short，例如期望 `19.999s` 实得 `17.518s`、期望 `19.969s` 实得 `18.477s`；这属于 coverage/成片完整性问题，但不是本次整体延迟的主要来源。

此外，上游 `forwarder queue` 深度达到 1,972，Savant send failures 窗口增量为 2，`validate_seq_iq` 计数为 154,440，8090 annotation 完整性也失败。这些是独立的验收轴，不能归因于 rolling segment writer，也不能用 evidence 可播放率掩盖。

## 5. 高优先级代码与配置问题

### 5.1 `ROLLING_CACHE_FALLBACK_TO_REPLAY` 没有实现

该配置在 `services/media-worker/app/config.py:49,203-208` 被读取，compose/env 也公开它，但生产代码没有其他引用。

与此同时，`ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=true` 会在 evidence task 创建后直接 ACK，不发布 Replay request，见 `services/event-worker/app/worker.py:382-392`。rolling coverage miss 只会 defer/retry，最终失败；不会重新发布 record request。

因此：

```text
SUPPRESS_RECORD_REQUESTS=true
+ rolling cache coverage miss / sink 未启动 / epoch 错位
= 没有 Replay fallback
```

这属于“配置名字承诺了一条不存在的线路”。当前 pressure runner 把 fallback 设为 false，所以其验收没有被伪 fallback 污染；但默认 `true` 会给日常运维错误预期。

### 5.2 两个并发开关没有按名字生效

`CLIP_WORKER_MAX_CONCURRENT_JOBS=8` 被加载并打印，但 gate 实际使用 `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY`、per-shard 和 per-source，见 `services/clip-worker/app/worker.py:3024-3061`。除日志/诊断外，`cfg.max_concurrent_jobs` 不参与 admission。

`MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=4` 在主循环创建 `_MaterializationGuard`，但当 finalizer workers > 1 时进入 pool 分支，pool 不共享该 guard；每个 job 自建 `_MaterializationGuard(1)`，见 `services/media-worker/app/worker.py:4495-4500,5901-5916`。当前 `MEDIA_WORKER_FINALIZER_WORKERS=32`，所以“max active 4”不是 32 线程 finalizer 的全局上限。

结果是配置表面看起来有限流，实际压力路径可能同时打开更多 DB 连接、sidecar 读取和 ffprobe 工作。

### 5.3 rolling runner 仍会被同步 finalizer 卡住

新 `_RollingCacheMaterializationRunner` 已解决旧实现“整批 remux 阻塞 60-100s 才再 poll”的问题，见 `services/media-worker/app/worker.py:6951-7001`。但 future 完成后 `_drain_completed()` 同步调用 `_flush_rolling_cache_finalizer_batch_or_defer()`，见 `services/media-worker/app/worker.py:7003-7037`。

后者进入 `_process_sink_output()`，finalizer pool 使用 `with ThreadPoolExecutor(...)` 并等待整批 futures 完成，见 `services/media-worker/app/worker.py:5803-5869`。因此：

```text
persistent remux pool 是异步的
remux 完成后的 finalizer batch 仍同步占住 media-worker 主循环
```

当 sidecar、expanded DB rows 或 probe 变慢时，标称 1s 的 rolling poll 仍可能被拉长。

### 5.4 rolling 分段索引仍是文件系统扫描

`find_segments()` 对 source root 执行 `rglob("metadata.json")`，逐文件解析 frame PTS，见 `services/media-worker/app/rolling_cache.py:66-103`。`materialize_window()` 随后又通过 `_select_rows()` 重读选中 metadata，见 `services/media-worker/app/rolling_cache.py:280` 和 `:580-591`。

runner 的 `segment_cache` 只在一次 `process()` 中有效，下一 poll 重新建立，见 `services/media-worker/app/worker.py:6980-6990`。60 source、5-15 分钟 retention 下，这会形成持续目录遍历和 JSON 解析放大。

### 5.5 “rolling_cache_copy” 不保证只是 copy

`materialize_window()` 至少执行 concat/remux 和 ffprobe；首次 copy 失败会第二次 copy-remux，时长不足会回退到 libx264 重编码，见 `services/media-worker/app/rolling_cache.py:191-278,395-431`。

因此该模式的名字更接近“无需 Replay job 的 rolling materialization”，不是文件系统层面的零成本 copy。遇到分段边界/GOP/时长问题时，单事件可退化成两次 remux 加一次转码。

## 6. 冗余与语义漂移

### 6.1 production 中不可达的旧 rolling 同步分支

`_process_rolling_cache_tasks()` 同时保留：

- 有 runner 时的 persistent pool 路径；
- runner 为 None 时的旧整批同步/临时 ThreadPoolExecutor 路径。

生产 `run_worker()` 在 rolling enabled 时总会创建 runner，见 `services/media-worker/app/worker.py:8179-8184`，所以 `services/media-worker/app/worker.py:6020-6183` 的旧路径在当前生产入口不可达，主要只剩测试/兼容价值。

同一函数先执行一次 `_expire_overdue_rolling_cache_tasks()`，runner.process 又执行一次，见 `services/media-worker/app/worker.py:6015-6018,6970-6972`，每个 rolling poll 会重复 deadline UPDATE 查询。

### 6.2 重复函数定义

`services/media-worker/app/worker.py` 在 `:1352` 和 `:1936` 两次定义 `_metadata_labels()`，第二个定义静默覆盖第一个。两者当前逻辑接近，但这是典型的线路演进残留；以后只修改前一个不会产生任何运行效果。

该文件已经达到 8,296 行、约 182 个函数，混合 sink scan、Replay slot、rolling materialization、snapshot、annotation、bundle 和 DB 状态机。冗余不是只有多一条调用链，更来自职责无法独立演进。

### 6.3 deferred 状态与索引相互矛盾

migration 027 声明“带 reason 的 `materialization_deferred` 为 terminal，不应再次 claim”，并从 ready claim 索引中排除 deferred，见 `db/migrations/027_evidence_task_ready_indexes_terminal_deferred.sql:1-24`。

当前 worker 却把 `materialization_deferred` 放在 `ROLLING_CACHE_TASK_STATUSES`，coverage miss 设置新 `materialization_ready_at` 后继续重试，见 `services/media-worker/app/worker.py:5999-6004,7465-7502`。candidate SQL 也按这个状态集合查询并排序，见 `services/media-worker/app/worker.py:7310-7388`。

这会导致两类问题：

- 注释/运维语义无法判断 deferred 是 terminal 还是 retryable；
- retryable deferred 查询不能使用 migration 027 的 ready claim partial index，数据量大时更可能扫描和排序。

### 6.4 双 rolling sink 重复清理共享目录

rolling-cache-sink-a/b 共享 `/media/rolling-cache`。每个 entrypoint 都启动一个后台清理器，每 30s 对 root 做两次全树 `rglob`，见 `scripts/runtime/rolling_cache_sink_entrypoint.sh:29-75`。

双 sink profile 下会出现：

- 两个清理进程重复扫描同一目录；
- 两者并发删除同一批过期文件；
- 与 media-worker `find_segments()` 的 rglob 竞争 metadata I/O。

这是明确的冗余运行线路，不只是代码重复。

### 6.5 retention 和磁盘上限配置不闭环

- `ROLLING_CACHE_RETENTION_SECONDS` 传给 media-worker，但 rolling sink compose 没有把该变量传进 sink；sink entrypoint 会回到默认 300s。pressure runner 声称设置 900s，并不等价于 sink cleanup 实际使用 900s。
- `Config.rolling_cache_retention_seconds` 只加载不使用。
- `ROLLING_CACHE_MAX_BYTES` 只存在于 `infra/env/midterm.env`，生产实现没有引用。

因此 rolling cache 当前真正的容量治理只有 sink entrypoint 的 mtime retention，而且 profile/pressure 报告中的配置值不一定是执行清理的实际值。

## 7. finalizer 和 PostgreSQL 为什么会拉长尾延迟

### 7.1 每个 finalizer job 新建 PostgreSQL 连接

pool 中每个 job 在 source lock 内执行 `psycopg.connect()`，完成后关闭，见 `services/media-worker/app/worker.py:5901-5945`。32 workers 的 burst 会反复创建连接，没有连接池。

这既增加连接握手成本，也会在 DB 压力或 DB 重启时把一批 finalizer 同时打失败。当前运行态中 DB down 已经直接证明这些 worker 没有降级能力。

### 7.2 DB expanded rows 位于完成关键路径

`EVIDENCE_DB_INDEX_EXPANDED_ROWS_ENABLED=true` 时，每个 bundle 同步 upsert：

- `evidence_bundles`
- artifact rows
- 每帧 `evidence_frame_timeline`
- 每帧 `evidence_overlay_segments`

见 `services/media-worker/app/evidence_db_index.py:23-227,278-405`。只有这些写入和 sidecar 完成后，任务才完整收敛。最新产物中 finalization `p95=18.19s`，sink ffprobe ready 到 finalizer start `p95=29.76s`，说明“视频已 remux”与“证据可查询”之间仍有明显产品层成本。

最近产物的 PostgreSQL 累计统计还显示：`events` 仅约 1,072 live rows，却有约 305k seq scans、118 亿 seq tuples read；`evidence_tasks` 约 295 live rows，却有约 112k seq scans、2.69 亿 seq tuples read。该统计是累计值，不能直接归因于单次 run，但足以说明周期性 worker 查询和状态同步仍是热点，不应继续只盯 Replay/ffmpeg。

## 8. 哪些不是当前主因

- 最新 rolling run 中没有 `security.record_requests`，clip-worker/Replay job 不参与该次 retained evidence 生成；因此不能用它证明 clip-worker 已快或慢。
- rolling segment metadata visibility `p95=0.649s`，该次不是 segment writer 普遍需要 9s 才可见。
- 最新 run 清理后保留的 521 个 bundle 均可播放，说明 rolling raw clip 路径没有整体崩溃；但 4 个 materialization failed、duration-short 和 annotation incomplete 仍需单独处理。
- 9s grace 是可优化的固定成本，但最新批次的主导延迟已经是 ready 后的 media queue、claim 和 finalizer 积压。
- 当前机器的 failure 是 DB 不可用；在 DB 恢复前继续比较 rolling/Replay 参数没有意义。

## 9. 建议的处理顺序（本次未实施）

1. **先修运行契约**：启动必须验证实际 DB，evidence health 必须做 DB probe，不能吞掉 health failure。
2. **明确唯一日常主线**：日常到底默认 Replay 还是 rolling；8090 应显示 effective mode，而不是只看 env 文件。
3. **修正假配置**：实现或删除 `ROLLING_CACHE_FALLBACK_TO_REPLAY`；删除/接线 `CLIP_WORKER_MAX_CONCURRENT_JOBS`、`ROLLING_CACHE_MAX_BYTES`；明确 `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE` 与 finalizer workers 的关系。
4. **统一 deferred 状态机与索引**：区分 retryable coverage miss 和 terminal covered/deferred，不复用同一 status。
5. **降低固定等待**：用 segment visibility 分位数推导 ready grace，并保留动态安全余量；不要固定沿用 9s。
6. **减少 rolling metadata 扫描**：使用 manifest/index 或长生命周期 source/epoch segment cache，避免每 poll rglob + 重读 JSON。
7. **拆开 materialize 与 index-ready**：至少单独量化 raw clip ready、sidecar ready、DB index ready；否则用户看到的“生成慢”无法定位。
8. **限制 finalizer 实际并发**：让全局 active limit 对 pool 生效，使用 DB connection pool，并基于 CPU/IO/DB 三种压力分别调度。
9. **单点负责 rolling cleanup**：双 sink 不应各自遍历和删除共享 root；同时把 retention/max-bytes 的 effective value纳入 runtime status。

## 10. 验证记录

- 针对 rolling cache、media worker、completion-aware Replay admission、clip queue、event recording policy 和 worker indexes 的测试：`100 passed in 0.72s`。
- 代码知识图专项扫描：全仓 1,140 文件；深度分析 evidence 相关 7 个语义批次，合并结果 857 nodes / 1,557 edges，无 skipped file。
- 当前 Docker/HTTP/Redis/文件系统检查均为只读；未启动 PostgreSQL、未重启容器、未修改源码或配置。

测试通过说明现有行为与测试合同一致，并不否定本文问题：fallback、dead config、pool 全局限流、health DB probe、双 cleanup 等路径目前缺少对应行为测试。
