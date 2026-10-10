# 分析采样预算收紧与分析延迟上限（2026-10-11）

依据：[uos157 人脸身份刷新后一小时复测](code_review/pressure60_face_refresh_uos157_1h_2026-10-11.md)
（60 路、配置 4 FPS、单 T4，`29147cb`）。相关 spec：`specs/16_dual_path_30x2_t4_production_optimization.md` §5.2。

## 现象

- 180 秒保存要求未通过：2,804 条中 3 条超时，最长 208.9 秒。
- 总耗时分两段：事件帧 → 事件入库 p50 26.4 / p95 78.3 / 最大 89.5 秒；入库 → 保存登记 p50 22.8 / p95 58.4 / 最大 136.2 秒。
- 分析队列从 0 涨到 12,050 帧（峰值 12,786），105 次采样中 99 次非零，采样结束未收敛。

## 根因：配置 4 FPS，实际送入约 4.3–4.4 FPS

- 结果 JSON：一小时内 forwarder 共发送 959,000 帧，4 FPS 预算为 864,000（60 × 4 × 3600），超出 11%；
  另有 12,050 帧留在队列。Savant 实测处理约 4.31 FPS/路，送入速率高于处理能力，队列只能线性增长，
  “事件帧 → 入库”的延迟随之增长。
- 原因在 `services/analysis-forwarder/app/sampler.py`：关键帧必须放行（丢了关键帧，H.264 到下一个关键帧前都无法解码），
  但放行后额度按 `max(0, credit - interval)` 归零：没有额度的关键帧等于免费通过，欠账被丢弃。
- 片源是电影，镜头切换会产生 GOP 之外的额外 I 帧。离线复现（23.976 FPS、每 12 帧关键帧，4 FPS 预算）：

| 额外 I 帧 | 关键帧/秒 | 原逻辑放行 FPS | 新逻辑放行 FPS |
| --- | ---: | ---: | ---: |
| 无 | 2.00 | 4.020 | 4.000 |
| 约每 5 秒 | 2.19 | 4.165 | 4.000 |
| 约每 3 秒 | 2.30 | 4.268 | 4.000 |
| 约每 2 秒 | 2.47 | 4.423 | 4.000 |

  与实测各路 4.18–4.42 FPS 一致。关键帧在新旧逻辑下都 100% 放行。

## 改动

### 1. 严格采样预算（`FORWARDER_STRICT_FPS_BUDGET`，默认 true）

关键帧照常放行，但要占用一个名额：额度不足时记为欠账（最多欠 2 个间隔），后面的普通帧等欠账还清再放行。
`ANALYSIS_FPS` 因此成为真实的平均上限。关键帧本身超过预算时（全 I 帧流），只放关键帧，欠账封顶不会让普通帧永远饿死。

### 2. 分析延迟上限（`FORWARDER_MAX_QUEUE_AGE_MS`，分析 forwarder 默认 30000）

- 分析帧在队列里等待超过上限就丢弃（`stale_dropped`），该路随后丢弃普通帧直到下一个关键帧
  （`gop_resync_dropped`），Savant 不会解码缺关键帧的 GOP；
- EOS/关闭等控制消息不丢；
- 只作用于送往 Savant 的分析队列。原始码流的 raw 输出走独立队列，滚动缓存和证据视频仍是全帧率，不受影响；
- 对应 CLAUDE.md 的实时链路原则“处理最新帧、丢弃过期帧”：等了 30 秒以上的帧即使分析出事件也已经太晚，
  不如让后面的新帧尽快被分析；
- 代码默认 0（关闭）。只在分析 forwarder 上开启：compose 的 `analysis-forwarder`、`-a`、`-b`，8090 拓扑生成的分支 forwarder，
  以及压测脚本的直连路径。`replay-raw-fanout` 和 `midterm.env` 都不设置，避免共享 env 波及证据链路。

### 3. 监控（Prometheus，forwarder `:8081/metrics`）

| 指标 | 含义 |
| --- | --- |
| `va_forwarder_keyframes_over_budget_total{source_id}` | 无额度仍放行的关键帧数，即本次根因的直接计数 |
| `va_forwarder_frames_stale_dropped_total{source_id}` | 超过延迟上限被丢弃的分析帧 |
| `va_forwarder_frames_gop_resync_dropped_total{source_id}` | 等待关键帧重同步时丢弃的帧 |
| `va_forwarder_queue_head_age_ms` | 队首帧已等待的时间（≈ 当前分析延迟的排队部分） |

队列丢弃同时计入 `va_forwarder_frames_dropped_total`，保持 `seen − forwarded − dropped = 队列深度` 这一既有核算关系。
启动日志新增 `analysis_fps / strict_fps_budget / max_queue_age_ms`。

### 4. 配置入口

| 位置 | 项 |
| --- | --- |
| compose 分析 forwarder | `ANALYSIS_FORWARDER_STRICT_FPS_BUDGET`（默认 true）、`ANALYSIS_FORWARDER_MAX_QUEUE_AGE_MS`（默认 30000） |
| 8090 运行拓扑 分支 A/B | “分析最大延迟 ms”（`analysis_max_lag_ms`，预设 30000）→ 分支 forwarder 的 `FORWARDER_MAX_QUEUE_AGE_MS`；严格预算固定开启 |
| 8090 性能配置（单分支） | “分析最大延迟 ms”（`forwarder_max_queue_age_ms`）→ `analysis-forwarder` |
| 压测脚本 | `--analysis-max-queue-age-ms`（默认 30000，0 关闭）；直连路径 forwarder 固定严格预算 |

旧的已保存拓扑配置读取时补默认值 30000。

## 非目标

- 不改取证阶段（入库 → 保存登记 p95 58 秒、最大 136 秒）。60 路播放同一画面，同一时刻产生约 60 条入侵事件，
  取证阶段在这种同步突发下排队；是否加大 media-worker 并发需要单独测（见下）；
- 不改居中校验（480 条 `event_not_centered_in_clip`）。上一轮队列峰值 12,240、结束时回到 0，只有 31 条；
  本轮队列一小时内基本没有回落、结束仍有 12,050，达到 480 条，可能与持续的分析延迟有关。
  先看本改动后的数量，再决定是否修 sidecar 投影与最终 UUID 投影的校验顺序；
- 不改模型、batch、人脸检测间隔。

## 预期

- forwarder 发送量 / 预算 ≈ 1.00，`keyframes_over_budget` 有计数但不再转化为超额；
- 4.00 FPS 送入、约 4.31 FPS 处理能力，约 7% 余量，队列应在突发后回落；
- 稳态 `stale_dropped` 接近 0，只在突发时出现；`queue_head_age_ms` 不超过 30000；
- “事件帧 → 入库”上限约为 30 秒排队 + Savant 内部处理，不再随运行时间增长。

## 回滚

- 只回退严格预算：`ANALYSIS_FORWARDER_STRICT_FPS_BUDGET=false`（拓扑路径需改 `_forwarder_env` 或 revert）；
- 关闭延迟上限：`ANALYSIS_FORWARDER_MAX_QUEUE_AGE_MS=0`，或 8090 两处表单设 0，或压测 `--analysis-max-queue-age-ms 0`；
- 或 revert 本次提交，并重建 forwarder 镜像。

## 部署验证清单

1. 拉取后**需要重建 forwarder 镜像**：代码通过 Dockerfile `COPY app` 打进镜像，没有挂载目录。
   `docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml build analysis-forwarder`。
   该镜像同时用于 `analysis-forwarder*` 和 `replay-raw-fanout*`。raw fanout 采样关闭、未设延迟上限，行为不变，只多了新指标。
2. 重建容器：单分支 `up -d --no-build --force-recreate --no-deps analysis-forwarder replay-raw-fanout`；
   双分支在 8090 运行拓扑重新应用预设（会给 `replay-raw-fanout-a/b` 写入新 env）。重建 `api`、`evidence-viewer` 以显示新表单项。
3. 启动日志：`docker logs video-analytics-midterm-replay-raw-fanout-a 2>&1 | grep 'starting forwarder'`，
   确认 `analysis_fps=4/1 strict_fps_budget=True max_queue_age_ms=30000`。
4. 运行中看指标：`curl -s http://<forwarder>:8081/metrics | grep -E 'keyframes_over_budget|stale_dropped|gop_resync|queue_head_age|queue_depth'`。
5. 复跑同一 60 路 4 FPS 一小时测试，对比：
   - `final_forwarded_target_ratio` 应约等于 1.00（上一轮 1.11）；队列末值应接近 0；
   - “事件帧 → 入库”p95/最大值，180 秒达标率，`generated_unverified` 数量；
   - `stale_dropped` 总量（衡量分析覆盖损失），名单命中数和入侵事件数。
6. 若队列仍增长，说明 4 FPS 已超过该内容下的真实处理能力：降 `ANALYSIS_FPS`（如 7/2）或提高人脸检测 interval，不要只靠延迟上限丢帧。
7. 取证阶段单独实验：保持本改动，只把 media-worker 并发 `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE`（压测 `--media-worker-materialization-max-active`）
   从 4 调到 6 再到 8，观察 CPU/磁盘和“入库 → 保存登记”分布。

## 验证（本机，离线）

- 新增 15 个测试：
  - `harness/tests/test_analysis_forwarder.py`：镜头切换额外 I 帧下严格预算保持 4 FPS 且关键帧全放行；旧逻辑复现超额 >4.2 FPS；
    规则 GOP 速率；超额关键帧的判定；欠账上限；队列过期丢弃 + 关键帧重同步；控制消息不丢；默认关闭；队首等待时间；
    env 读取；指标与 `frames_dropped_total` 核算；
  - `harness/tests/test_analysis_lag_cap_config.py`：compose 三个分析 forwarder 有设置而 raw fanout 和 env 文件没有；
    8090 拓扑字段、预设、`_forwarder_env`（两种模式）、旧配置默认值；单分支性能字段；压测脚本参数和直连 env。
- 相关既有测试（forwarder、拓扑、性能、全运行编排、部署合同、压测脚本、shard 路由等）：无新增失败。
- 全量 harness（`-m "not integration"`）：12 failed / 1705 passed / 21 skipped / 7 collection errors，失败集合与改动前相同（环境项）。
- `docker compose ... config`、`node --check operator.js`、`git diff --check` 通过。
- 未做：真机运行。

## 剩余风险

- 延迟上限在突发期会跳过部分分析帧，突发期间的事件可能漏报（过期帧本来也来不及按时出证据）；需用 `stale_dropped` 量化；
- 若真实处理能力在某些内容下低于 4 FPS，严格预算本身不能收敛队列，只能由延迟上限兜底，应同时降低分析 FPS；
- 取证阶段和居中校验问题未在本次处理。

## 实现状态

- 分支 `claude/analysis-budget-20261011`，基于 `main` `c40e930`；
- 实现提交：`9f70869`（采样预算、延迟上限、指标、配置入口、测试与文档）；
- 验证时间：2026-10-11，离线验证。
