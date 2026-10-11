# 取证 remux 吞吐上限：完成即唤醒调度（2026-10-11）

原实现：`claude/evidence-remux-wake-20261011`（`6828fbc` + `4e0f462`），基于
`CZray/savant-decoder-guard-20261011`（`b3073d2`，含解码守护与严格分析预算）。
复核修复：`CZray/evidence-remux-wake-review-20261011`，继承原实现，补全共享许可释放后的准入唤醒及压测开关快照。
未合入 main，未部署；详细复核见 [审查交接记录](code_review/evidence_remux_wake_review_2026-10-11.md)。

## 现象

uos157 隔离复测（60 路 × 4 FPS × 1 h，报告
`docs/code_review/pressure60_isolated_decoder_guard_uos157_1h_2026-10-11.md`）中，
2959 条应取证视频里 959 条超过 180 秒、20 条在内部 300 秒期限前未生成。
前三轮采用相近的名义负载，但不是受控 A/B。守护补丁轮还有 40 个旧 reader，且其 12 条抽检视频均未通过完整解码，不能当作整体通过的容量基线。以下仅对比各轮 `results.json` 的视频登记时延：

| 轮次 | 应取证 | 需求速率 | 入库→登记 P50 / P95 | 总耗时 P95 / 最大 | 超 180 秒 |
|---|---:|---:|---:|---:|---:|
| 人脸刷新（29147cb） | 2804 | 0.777 条/秒 | 22.8 / 58.4 秒 | 104.8 / 208.9 秒 | 3 |
| 守护补丁（含 40 个旧 reader） | 2825 | 0.782 条/秒 | 25.9 / 65.0 秒 | 69.8 / 114.6 秒 | 0 |
| 隔离复测（b4723c9） | 2959 | 0.819 条/秒 | **90.2 / 242.6 秒** | 262.6 / 301.8 秒 | **959 + 20 未生成** |

需求只多了约 5%，取证阶段的中位耗时却变成原来的 3.5 倍。这是排队系统接近饱和时的典型表现。
这些差异支持取证调度存在容量限制；它们不能排除分析侧改动、输入内容、CPU 或存储对端到端结果的影响。

## 影响范围

- **主要针对：** rolling-cache 视频取证（入侵、摔倒、聚集、追逐）从"可取"到"登记"的等待；本轮实际启用的业务规则是入侵和 watchlist，没有验证另外三类行为的负载。
- **共享影响：** 修改的是 image/remux/finalizer 共用的调度器，图片取证的调度节奏也会变化。1382/1382 是人脸图片就绪数量，未单独验证其全部满足 180 秒。事件推理与入库代码未改，运行时资源竞争仍需实测。
- **事件到入库：** 这一段 P95 为 43 秒，同时存在分析队列积压；本次完成唤醒不能直接消除这一段延迟，见下文"分析侧"。

## 根因判断

### 1. 实测的保存速率被卡在约 0.75–0.85 条/秒

按 10 分钟窗口统计三轮的 `video_saved`（已登记）和 `pending`（已建任务未登记）：

| 窗口（秒） | 人脸刷新 保存/到达 | 守护补丁 保存/到达 | 隔离复测 保存/到达 | 隔离复测 pending |
|---|---|---|---|---|
| 0–600 | 0.696 / 0.718 | 0.679 / 0.712 | 0.695 / 0.717 | 0→14 |
| 600–1200 | 0.826 / 0.807 | 0.853 / 0.846 | 0.812 / 0.817 | 14→17 |
| 1200–1800 | 0.725 / 0.719 | 0.798 / 0.783 | 0.745 / **0.824** | 17→63 |
| 1800–2400 | 0.700 / 0.725 | 0.814 / 0.802 | 0.760 / **0.858** | 63→124 |
| 2400–3000 | 0.805 / 0.830 | 0.759 / 0.767 | 0.784 / **0.855** | 124→167 |
| 3000–3600 | 0.796 / 0.791 | 0.722 / 0.776 | 0.813 / 0.805 | 167→162 |

- **保存速率：** 三轮都没有超过约 0.85 条/秒。
- **隔离复测中段：** 1200–3000 秒的到达速率持续高于保存速率，pending 从 17 涨到 167。
- **与延迟的对应：** 167 条按约 0.78 条/秒排空需要约 214 秒，和入库→登记 P95 242.6 秒相符。
- **需求口径：** 若仅有一个会生成视频的规则，每路每 60 秒最多一条，60 路的长期平均上界约为 1.0 条/秒，但仍可能同步突发 60 条。多规则会叠加需求。此前轮次的成功登记速率不能证明已经覆盖这种负载。

### 2. 调度快照：许可没用满，remux 通道占满

35 分钟时（06:54:45–47 KST）抓到的 `media_scheduler_tick` 日志：

- `permit_active=2–3`，`permit_limit=4`：说明 WIP 许可没有用满。
- `remux_lane_depth=1`：单一 remux 通道被占满。
- `oldest_ready_age_ms=116871→118869`：已可取证的任务已经等了约 2 分钟。
- 每个 rolling tick（约 1 秒一次）最多 `remux_admission_submitted_count=1`。
- 本例中，前一个 rolling tick 时通道仍忙（`candidate_count=0`），下一个 tick 才 `completed=1, submitted=1`。也就是一条视频用了约 2 秒、两个 tick。

### 3. 代码机制

主循环在 `services/media-worker/app/worker.py` 的 `run_worker` 中：

1. **remux 通道宽度：**
   - 压测脚本默认 `ROLLING_CACHE_MATERIALIZATION_WORKERS=1`（`--media-worker-rolling-remux-workers 1`）。
   - midterm 部署（`midterm.env`、compose 默认值）同样是 1。
2. **准入和回收时机：**
   - `_RollingCacheMaterializationRunner.process()` 负责回收已完成的 remux 并准入下一条。
   - 它只在 `rolling_cache_due` 时运行，间隔是 `ROLLING_CACHE_MATERIALIZATION_POLL_INTERVAL_S=1`。
3. **主循环空闲：** 原来是 `time.sleep(sleep_s)`，期间没有任何唤醒。remux 在睡眠期间完成，也要等到下一个 rolling tick 才被回收，下一条才能开始。

结果：单 remux 名额每次 rolling poll 最多准入一个新任务。完成后对齐下一个 poll 的等待，会额外占用串行流水线的时间；跨过一个 tick 可能要等两个 tick，耗时更长时会等更多 tick。实际吞吐还受 remux 服务时间、WIP、CPU 和存储限制，不能仅由 tick 推导。

2026-10-11T09:09:28.230589+09:00 从旧轮次 `pressure_diagnostics.json` 只读提取的分布进一步支持这一方向：

| 指标 | 样本数 | P50 | P95 | 最大 |
|---|---:|---:|---:|---:|
| 可剪 → remux 认领 | 2996 | 88.192 秒 | 245.492 秒 | 285.463 秒 |
| remux 执行 | 2996 | 0.615 秒 | 0.856 秒 | 1.942 秒 |
| remux 总耗时 | 2996 | 0.838 秒 | 1.104 秒 | 2.379 秒 |
| finalizer 池内等待 | 2996 | 0 秒 | 0.001 秒 | 0.014 秒 |
| finalizer 通道服务 | 2996 | 1.431 秒 | 1.923 秒 | 6.596 秒 |

这是 harness 抓取日志的聚合，包含预热和收尾，不是只含正式一小时窗口的百分位。等待远大于 remux 执行，但 remux 总耗时 P95 并非低于 1 秒；finalizer 服务和其他资源也有成本。它支持先修调度，不能证明调度是唯一瓶颈或保证修后全部 ≤180 秒。

### 4. CPU 绑定是第二个限制，本次不改

- 压测 profile `t4-16cpu-evidence` 把 `media-worker` 绑在一个逻辑 CPU（7）上；event、person、clip worker 绑在 15。没有核实物理核拓扑，不据此认定二者共享物理核。
- 各轮 media-worker CPU 峰值都在约 100%。
- 生产 compose 没有 cpuset，这只是压测里的限制。
- 修掉调度量化后，下一道瓶颈可能就是这颗核。所以单独排成后续 A/B，不和本修复混在一起。

## 修复（本分支）

1. **完成信号：**
   - `materialization_scheduler.py` 新增 `CompletionSignal`，`MaterializationResources.completion_signal` 始终存在。
   - image、remux、finalizer 三条 `BoundedExecutorLane` 在任务结束时，通过 future done-callback 发出 `notify(lane)`。
   - done-callback 在结果写入之后执行，唤醒时 `future.done()` 已为真。
   - 任务失败也会通知。通知回调自身出错只记日志，不影响任务。
2. **主循环等待：**
   - 改为 `scheduler_wait(...)`：在完成信号上等待，最长仍是原来的睡眠时间。
   - 被 remux、image 或 finalizer 完成唤醒时，`rolling_poll_after_wake(...)` 把下一次 rolling 准入提前到当下：先回收，再按原有容量、优先级和每源限制检查是否可以补位。
   - 复核发现：image/finalizer 也持有共享 WIP/每源许可。仅唤醒其完成回收而不提前 rolling 准入，会让刚释放的名额闲置到旧定时点。因此补全了这两类唤醒；general/recovery 定时计划不变。
3. **观测：** `media_scheduler_tick` 增加 `wake_lanes=`，取值为 `none`、`remux` 或 `finalizer,image` 等；启动日志增加 `media_worker_completion_wake enabled=...`。

## 配置开关与回滚点

- **运行时开关：** `MEDIA_WORKER_COMPLETION_WAKE_ENABLED`，默认 `true`。
  - compose 写法为 `${MEDIA_WORKER_COMPLETION_WAKE_ENABLED:-true}`，`midterm.env` 中显式写了 `true`。
  - 设为 `false` 即恢复固定 tick 行为，不需要回滚代码。
- **压测开关：** `--media-worker-completion-wake on|off`，默认 `on`。复核前漏了环境快照字段，on/off 都会在重建后核验失败，也不能保存原开关用于恢复。本复核分支将该字段加入 `media_worker_rolling_cache_env_snapshot()`，并测试真实的配置、回读核验和恢复流程（仅模拟 Docker 执行）。
- **代码回滚：** revert 本分支的提交即可。没有数据库或 schema 变化。
- **部署：** media-worker 以挂载方式运行（`../services/media-worker:/app`），验证时不需要重建镜像：

  ```bash
  docker compose -f infra/docker-compose.midterm.yml up -d --no-build --force-recreate --no-deps media-worker
  ```

## 数据和状态语义

- **不变的部分：**
  - WIP 许可、通道宽度、每源上限、租约、截止时间。
  - remux/finalizer 的执行内容、证据文件和数据库记录。
- **变化的部分：** 只改变"何时检查并补位"。
- **tick 频率：** 完成通知可提前产生 tick；同一次等待前的多条通知会合并。日志量和数据库查询次数预计增加，增幅尚未实测。
- **空转边界：** 等待时消费信号，不会空转。连续快速失败的 remux 只会按完成次数唤醒，而失败任务会被延后重试。

## 非目标

- 不改 remux 通道宽度、WIP、压测 CPU 绑定、冷却时间、视频前后时长。
- 不改分析侧（forwarder 预算、解码守护）。
- 不改 180 秒验收口径或内部 300 秒期限。
- 不宣称已通过 60 路验收。

## 剩余风险与后续项

1. **CPU 或其他阶段成为新瓶颈：** 去掉定时等待后，串行 remux 容量仍受平均服务时间限制，整个流水线还受 finalizer、共享许可、CPU 和存储限制。若这些成本占主导，收益可能有限。后续可做以下单变量对照：
   - A：`--media-worker-completion-wake off` 与 `on` 对照（本修复）。
   - B：在 A 的 `on` 基础上，`--media-worker-rolling-remux-workers 2`。
   - C：给 media-worker 至少 2 个逻辑核（新建 profile，不改旧 profile，保证旧轮次可复现）。
2. **需求与验收：** 单一视频规则的 60 路、60 秒冷却对应长期平均至多约 1.0 条/秒，同时仍有突发；多规则的总需求更高。电影复测只证明该输入和规则组合的表现，还应分别验证同步突发与实际启用的多规则负载。
3. **分析侧积压是另一个问题：**
   - 隔离复测的正式窗口转发差分约为 3.87 FPS/路（约 233 FPS 合计）；结合队列和丢帧差分推导的准入下界接近 4.0，而不是已测得精确准入值。结束时仍有积压，过期丢帧约占 2%，事件到入库 P95 为 43 秒。
   - 人脸刷新轮的窗口平均约为 4.31 FPS/路（约 259 FPS），这不是已证明的稳定容量。
   - 积压较高时的一组快照显示 GPU 利用率约 51%、功耗 47 W；只能说明该快照未满载，不能排除其他时段或流水线阶段的瓶颈。
   - 候选原因均未验证：
     - 解码守护把原生 C++ probe 换成了逐缓冲区的 Python probe（每次都要拿 GIL）；
     - 共享 RTSP relay 可能改变到达节奏；此前也读取同一电影源，不能把同步场景归因于新 relay；
     - 片段内容不同。
   - 需要在同一输入下对照"守护开 / 原生 probe"来确认，不在本分支处理。
4. **帧率口径：** 严格预算取消超过 4 FPS 的放行是设计目的；但 3.87 FPS 也低于 4 FPS 目标及此前 3.96 的检查线，不能将全部下降都算作预期行为或判为通过。预算、丢帧和处理能力各自的贡献还需同输入对照。

## 原实现验证（Opus 报告，复核前）

- **新增测试：**
  - `harness/tests/test_media_worker_completion_wake.py`：11 项。
  - 覆盖信号语义、通道失败时仍通知、观察者异常隔离、唤醒时 future 已完成、单通道吞吐不再受 tick 限制（8 个 10 ms 任务、0.1 秒 tick：固定 tick ≥0.7 秒，唤醒 <0.45 秒）、主循环与部署开关、压测开关。
- **相关测试：** `test_media_worker_long_lived_resources.py`、`test_analysis_lag_cap_config.py` 通过。
- **全量 harness：**
  - 本分支：12 failed / 1757 passed / 69 skipped / 7 errors。
  - 基线 `b3073d2`：12 failed / 1746 passed / 69 skipped / 7 errors。
  - 两边失败集合完全相同，均为环境问题（缺 python-multipart、无 PostgreSQL、缺仓库外文件等）。
- **静态检查：** `docker compose -f infra/docker-compose.midterm.yml config` 通过；`git diff --check` 通过。
- **未做：** uos157 上的运行验证。

## Codex 复核与后续实测

1. **已只读核对旧产物：** 上述分布支持先修调度，结果和范围见“代码机制”及复核记录。
2. **已补回归测试：** 8 个新增用例中，4 个在 Opus 原代码上复现上述两个缺陷；修复后唤醒及长驻资源测试 32 项通过。扩展调度器/压测套件 251 passed / 5 failed；与原提交 `4e0f462` 在同一依赖环境下的失败集合一致，原因见复核记录。没有重跑全量 harness。
3. **先交 Opus 审查新分支，审查后再部署复测。** 本次没有重建服务或启动新压测。部署时只需重建 media-worker 容器（上面的命令），并核实开关与 `wake_lanes` 日志。
4. **后续 60 路 × 4 FPS × 1 h：** 保持本轮其余参数；对比保存/到达窗口、等待分布、积压、CPU、完整视频解码及事件覆盖。验收仍是全部应保存视频从事件发生到保存完成 ≤180 秒，不能只检查入库后的耗时或平均值。
5. **若仍积压：** 再按上面的 B、C 分别做单变量对照，不能先假设加并发一定有收益。

## 实现状态

- 实现提交：`6828fbc`（Wake the media-worker scheduler when lane work finishes）。
- 原实现开发机验证时间：2026-10-11（KST），结果见上文“原实现验证（Opus 报告，复核前）”。
- 复核修复分支：`CZray/evidence-remux-wake-review-20261011`。
- 未完成：Opus 复审、uos157 修后运行验证；分支未合入 main。
