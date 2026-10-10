# 取证 remux 吞吐上限：完成即唤醒调度（2026-10-11）

分支：`claude/evidence-remux-wake-20261011`，基于 Codex 的
`CZray/savant-decoder-guard-20261011`（`b3073d2`，含解码守护与严格分析预算）。
未合入 main，未部署。

## 现象

uos157 隔离复测（60 路 × 4 FPS × 1 h，报告
`docs/code_review/pressure60_isolated_decoder_guard_uos157_1h_2026-10-11.md`）中，
2959 条应取证视频里 959 条超过 180 秒、20 条在内部 300 秒期限前未生成。
同一配置的前两轮基本通过。三轮对比（数据来自各轮 `results.json`）：

| 轮次 | 应取证 | 需求速率 | 入库→登记 P50 / P95 | 总耗时 P95 / 最大 | 超 180 秒 |
|---|---:|---:|---:|---:|---:|
| 人脸刷新（29147cb） | 2804 | 0.777 条/秒 | 22.8 / 58.4 秒 | 104.8 / 208.9 秒 | 3 |
| 守护补丁（含 40 个旧 reader） | 2825 | 0.782 条/秒 | 25.9 / 65.0 秒 | 69.8 / 114.6 秒 | 0 |
| 隔离复测（b4723c9） | 2959 | 0.819 条/秒 | **90.2 / 242.6 秒** | 262.6 / 301.8 秒 | **959 + 20 未生成** |

需求只多了约 5%，取证阶段的中位耗时却变成原来的 3.5 倍。这是排队系统接近饱和时的典型表现。
它的根因不在本轮新加的分析侧改动。

## 影响范围

- **受影响：** 只影响 rolling-cache 视频取证（入侵、摔倒、聚集、追逐）从"可取"到"登记"这一段。
- **不受影响：** 人脸图片取证（1382/1382 全部按时）、事件检测和入库都不受影响。
- **事件到入库：** 这一段 P95 为 43 秒。它来自分析队列积压，是另一个问题，见下文"分析侧"。

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
- **需求上限：** 60 路、每路冷却 60 秒时，最多约 1.0 条/秒。现有上限低于最坏情况，前两轮只是因为片段内容碰巧需求较低才勉强通过。

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

结果：单通道每个 rolling tick 最多完成一条。remux 耗时加上对齐等待一旦跨过 tick 边界，就是两个 tick 一条。
这个上限和 remux 本身快慢无关，和 WIP 也无关，这就是第 2 节中"许可空着、任务等 2 分钟"的原因。

### 4. CPU 绑定是第二个限制，本次不改

- 压测 profile `t4-16cpu-evidence` 把 `media-worker` 绑在逻辑 CPU 7 上；event、person、clip worker 绑在 15。在常见的 8 核 16 线程编号下，二者是同一个物理核，未在 uos157 上核实。
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
   - 被 remux 完成唤醒时，`rolling_poll_after_wake(...)` 把下一次 rolling 准入提前到当下：先回收，再立刻补位。
   - finalizer 和 image 的完成本来每个 tick 都会回收；唤醒只是让这个 tick 提前发生，不强制 rolling 准入。
3. **观测：** `media_scheduler_tick` 增加 `wake_lanes=`，取值为 `none`、`remux` 或 `finalizer,image` 等；启动日志增加 `media_worker_completion_wake enabled=...`。

## 配置开关与回滚点

- **运行时开关：** `MEDIA_WORKER_COMPLETION_WAKE_ENABLED`，默认 `true`。
  - compose 写法为 `${MEDIA_WORKER_COMPLETION_WAKE_ENABLED:-true}`，`midterm.env` 中显式写了 `true`。
  - 设为 `false` 即恢复固定 tick 行为，不需要回滚代码。
- **压测开关：** `--media-worker-completion-wake on|off`，默认 `on`。它写入 media-worker 的环境变量，并纳入脚本已有的"重建后配置是否生效"核验。
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
- **tick 频率：** 每完成一个通道任务，最多多跑一个 tick。tick 日志条数会增加约 2–3 倍（按每秒约 1 条视频加 0.4 张图计算）。
- **空转边界：** 等待时消费信号，不会空转。连续快速失败的 remux 只会按完成次数唤醒，而失败任务会被延后重试。

## 非目标

- 不改 remux 通道宽度、WIP、压测 CPU 绑定、冷却时间、视频前后时长。
- 不改分析侧（forwarder 预算、解码守护）。
- 不改 180 秒验收口径或内部 300 秒期限。
- 不宣称已通过 60 路验收。

## 剩余风险与后续项

1. **CPU 成为新瓶颈：** 唤醒后 remux 通道上限变为约 `1 / remux_total`。如果 remux 本身在单核争用下要 1 秒以上，收益有限，需要做下面的 A/B（每次只变一个量）：
   - A：`--media-worker-completion-wake off` 与 `on` 对照（本修复）。
   - B：在 A 的 `on` 基础上，`--media-worker-rolling-remux-workers 2`。
   - C：给 media-worker 至少 2 个逻辑核（新建 profile，不改旧 profile，保证旧轮次可复现）。
2. **需求上限：** 60 路、冷却 60 秒时最坏约 1.0 条/秒。验收应以这个最坏需求为准，而不是以某段电影碰巧的事件密度为准。
3. **分析侧积压是另一个问题：**
   - 隔离复测的分析消费约为 3.87 FPS/路（约 233 FPS 合计），低于准入的 4.0，所以队列积压、过期丢帧约占 2%，事件到入库 P95 为 43 秒。
   - 人脸刷新那一轮 Savant 曾稳定消费 4.31 FPS/路（约 259 FPS）。
   - 积压峰值时 GPU 利用率只有约 51%，功耗 47 W，说明不是 GPU 饱和。
   - 候选原因均未验证：
     - 解码守护把原生 C++ probe 换成了逐缓冲区的 Python probe（每次都要拿 GIL）；
     - 共享 RTSP relay 让 60 路帧完全同步到达；
     - 片段内容不同。
   - 需要在同一输入下对照"守护开 / 原生 probe"来确认，不在本分支处理。
4. **"性能更差"的部分原因是严格预算：** 3.87 FPS 低于旧的 4.31，主要是严格预算不再超额准入（设计目标就是不超过 4），再叠加积压时的过期丢帧。这部分是预期行为，不是退化。

## 验证（开发机）

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

## 请 Codex 在 uos157 上核实

1. **先用已有产物确认根因，不需要重跑：**
   - 打开 `/data/video-analytics/artifacts/pressure60_isolated_guard_t4_4fps_1h_20261011/` 下的 harness 报告。
   - 查看 `ready_to_remux_claim_ms` 和 `remux_total_ms` 的分布（来自 `media_event_finalized` 日志）。
   - 预期：前者 P95 在百秒量级，后者在秒级以下。如果相反，本判断不成立。
2. **部署本分支，只重建 media-worker 容器**（上面的命令）。确认启动日志里有 `media_worker_completion_wake enabled=True`，tick 日志里出现 `wake_lanes=remux`。
3. **重跑 60 路 × 4 FPS × 1 h：** 其余参数与隔离复测相同。对比"保存/到达"窗口表、`oldest_ready_age_ms`、`permit_active`、media-worker CPU，以及入库→登记 P50/P95。
4. **如果 CPU 已打满但仍积压：** 再按上面的 B、C 分别做单变量对照。

## 实现状态

- 实现提交：`6828fbc`（Wake the media-worker scheduler when lane work finishes）。
- 开发机验证时间：2026-10-11（KST），结果见上文"验证（开发机）"。
- 未完成：uos157 运行验证（上文第 1–4 步）；分支未合入 main。
