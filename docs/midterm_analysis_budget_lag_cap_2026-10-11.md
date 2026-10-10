# 分析采样预算收紧与分析排队上限（2026-10-11）

依据：[uos157 人脸身份刷新后一小时复测](code_review/pressure60_face_refresh_uos157_1h_2026-10-11.md)
（60 路、配置 4 FPS、单 T4，`29147cb`），以及对首版分支 `5a101e6` 的复核意见。
相关 spec：`specs/16_dual_path_30x2_t4_production_optimization.md` §5.2。

**本文不宣称“所有视频 180 秒内保存”已解决。** 本次改动只处理分析入口的采样超额和排队时间，需要实机复测验收。

## 实测事实

| 项 | 数值 | 说明 |
| --- | --- | --- |
| 180 秒内保存 | 2,801 / 2,804，最长 208.943 秒 | 3 条超时集中在 `_15` 路 |
| 事件帧 → 事件入库 | p50 26.4 / p95 78.3 / 最大 89.5 秒 | |
| 入库 → 保存登记 | p50 22.8 / p95 58.4 / 最大 136.2 秒 | |
| 正式窗口 forwarder 发送增量 | **935,452 帧**，约 **4.323 FPS/路**，比 4 FPS 高约 **8.1%** | 复核按正式窗口计数差计算。首版写的 959,000 / 11% 是累计计数，含采样开始前的帧，**不适用于正式窗口** |
| 采样器实际接纳速率 | 未单独计量 | 发送速率 ≠ 接纳速率：接纳 = 发送 + 队列净增 + 队列内丢弃；旧版本没有区分这些的指标 |
| 分析队列（A+B 合计） | 确认峰值 12,786；采样结束 12,050 | 期间**多次明显回落**（如从约 1.28 万降到几千），末段回升；结论是“一小时结束时仍未稳定”，不是“从未回落” |
| Savant 处理速率 | 平均 4.309 FPS/路 | 这一轮的平均速率，**不是设备最大处理能力**，不能据此推算余量 |

## 原因分析：已证实与未证实

**已由代码确认：** 首版之前的采样器总是放行关键帧（丢关键帧会导致 H.264 在下一个关键帧前无法解码），
但放行后额度按 `max(0, credit - interval)` 归零。没有额度的关键帧不扣预算，**任何不在采样节奏上的关键帧都会让放行超过 `ANALYSIS_FPS`**。

**机制可能性（离线模拟，非实机证据）：** 23.976 FPS、每 12 帧一个关键帧，再加 GOP 之外的额外关键帧时，旧采样器放行率为：

| 额外关键帧 | 关键帧/秒 | 旧逻辑 | 新逻辑 |
| --- | ---: | ---: | ---: |
| 无 | 2.00 | 4.020 | 4.000 |
| 约每 5 秒 | 2.19 | 4.165 | 4.000 |
| 约每 3 秒 | 2.30 | 4.268 | 4.000 |
| 约每 2 秒 | 2.47 | 4.423 | 4.000 |

**未证实：** 这次推流实际有多少关键帧、多少落在 GOP 节奏之外、PTS 是否规整，都没有测量。推流配置为
libx264 ultrafast、g=12、keyint_min=12、每 0.5 秒强制关键帧，镜头切换是否额外产生关键帧、Replay/Savant 是否把它们标成关键帧，
都需要实测。8.1% 的发送超额也可能部分来自其他因素。因此“镜头切换额外关键帧”目前只是**与数据相容的假设**，需用下文的探针和指标确认。

## 改动

### 1. 严格采样预算（`FORWARDER_STRICT_FPS_BUDGET`，默认 true）

关键帧照常全部放行，但要占一个名额；额度不足时记欠账，后面的普通帧等欠账还清再放行。

- **欠账窗口**（`FORWARDER_KEYFRAME_DEBT_S`，默认 60 秒预算）：
  - 首版只保留 2 个帧间隔的欠账，复核用集中出现的关键帧构造出超额（关键帧平均 2.73 FPS 时放行 4.6 FPS）。
    本地用每 10 秒一次、27 个连续关键帧（平均约 2.7 FPS）复现：首版放行约 6.0 FPS；
  - 现在欠账可累积到 60 秒预算，同一输入放行不超过 4.0 FPS。
- **能保证的范围**：关键帧本身的长期平均低于预算、且关键帧超额能在 60 秒窗口内还清时，长期放行均值不超过 `ANALYSIS_FPS`。
- **不能保证的范围**：关键帧超额持续超过窗口的部分会被免除。这样设计是为了让普通帧在全关键帧片段后最多停约 60 秒，而不是无限期停住。
  关键帧本身超过预算时只放关键帧，放行率等于关键帧率。
- 5 秒全关键帧片段后，普通帧要等约 25 秒才恢复（欠账全部还清）；超过约 1 分钟的全关键帧片段之后，最多等约 60 秒。

### 2. 统一关键帧判定

采样器除显式 `keyframe` 标志外，还用 `uuid == keyframe_uuid` 识别关键帧；首版入队只读显式标志。
在只带 UUID 的兼容输入下，队列丢掉过期帧后会把新的恢复关键帧当普通帧丢掉（复核发现）。
现在入队、队列淘汰和恢复都使用同一个 `sampler.is_keyframe()`。实机走哪条路径仍需确认，但两处判定已一致。

### 3. 分析排队上限（`FORWARDER_MAX_QUEUE_AGE_MS`，分析 forwarder 默认 30000）

- 分析帧在 forwarder 队列里等待超过上限就丢弃（`stale_dropped`），该路随后丢普通帧直到下一个关键帧（`gop_resync_dropped`）。
  EOS/关闭等控制消息不丢。
- **只限制 forwarder 内部的排队时间。** 进入队列前的接收缓冲、Savant 内部积压、告警入库、剪辑和存储都不受它限制，
  **不能据此保证总耗时 ≤ 180 秒**。
- **丢弃的分析帧不会再产生告警**，短暂事件可能漏报。验收必须同时检查事件覆盖，不能只看已产生告警的保存情况。
- 只作用于送往 Savant 的分析队列。原始码流 raw 输出走独立队列，滚动缓存和证据视频仍是全帧率。
  代码默认 0（关闭），只在分析 forwarder 上开启：compose `analysis-forwarder*`、8090 拓扑分支 forwarder、压测直连路径；
  `replay-raw-fanout` 与 `midterm.env` 不设置。

### 4. 指标（forwarder `:8081/metrics`）

| 指标 | 用途 |
| --- | --- |
| `va_forwarder_keyframes_seen_total{source_id}` | 采样器实际看到的关键帧数：与探针对比，验证关键帧假设 |
| `va_forwarder_keyframes_over_budget_total{source_id}` | 额度不足仍放行的关键帧数 |
| `va_forwarder_pts_resets_total{source_id}` | PTS 回退/新会话次数 |
| `va_forwarder_frames_stale_dropped_total{source_id}` | 超过排队上限丢弃的分析帧（÷ 分析 FPS ≈ 未分析的秒数） |
| `va_forwarder_frames_gop_resync_dropped_total{source_id}` | 等关键帧恢复时丢弃的帧 |
| `va_forwarder_queue_head_age_ms` | 队首帧已等待时间 |

队列丢弃同时计入 `frames_dropped_total`，保持 `seen − forwarded − dropped = 队列深度`。
接纳速率 = `forwarded` 增量 + 队列深度增量 + `stale_dropped`/`gop_resync_dropped` 增量（均按正式窗口差值计算，不用累计值）。

### 5. 推流关键帧/PTS 探针

`scripts/tools/probe_stream_keyframes.py`：

- 用 ffprobe 读取包级元数据，不解码；
- 统计关键帧率、GOP 间隔直方图、不在常规 GOP 间隔上的关键帧数、PTS 间隔分布、PTS 非单调和缺失次数；
- 把实测序列分别送入旧采样器和新采样器，输出各自的放行 FPS 和超预算比例。

```bash
python scripts/tools/probe_stream_keyframes.py \
  --source rtsp://192.168.1.105:8554/live/1080movie --duration-s 300 --output keyframes_1080movie_rtsp.json
python scripts/tools/probe_stream_keyframes.py \
  --source /media/rr/zxlab/qwen27b/1080movie.mp4 --duration-s 600 --output keyframes_1080movie_file.json
```

包级 `K` 标志与 Savant VideoFrame 的关键帧标志可能不同，以 forwarder 的 `keyframes_seen` 为采样器视角的准数，两者对照。

### 6. 配置入口

| 位置 | 项 |
| --- | --- |
| compose 分析 forwarder | `ANALYSIS_FORWARDER_STRICT_FPS_BUDGET`（true）、`ANALYSIS_FORWARDER_MAX_QUEUE_AGE_MS`（30000） |
| forwarder env | `FORWARDER_KEYFRAME_DEBT_S`（60，未写入 compose，按需添加） |
| 8090 运行拓扑 分支 A/B | “分析最大延迟 ms”（`analysis_max_lag_ms`，预设 30000） |
| 8090 性能配置（单分支） | “分析最大延迟 ms”（`forwarder_max_queue_age_ms`） |
| 压测脚本 | `--analysis-max-queue-age-ms`（30000，0 关闭） |

## 未处理 / 独立问题

- **取证阶段**（入库 → 保存登记 p95 58 秒、最大 136 秒）：60 路同画面造成同步突发，取证排队。
  把 media-worker 并发 4 提到 6、8 可能加剧磁盘争用，需要单独实验，与本改动分开验收。
- **480 条 `event_not_centered_in_clip`**：480 条都找到了触发帧 UUID，但这不能证明画面位置正确；
  报告中最终 summary 位置 4.963 秒与 sidecar 投影 4.095 / 4.190 秒两种计算不一致。
  这是**独立的质量问题**，不归因于积压，需要单独定位两处投影的计算顺序和输入。
- 不改模型、batch、人脸检测间隔；保持人脸复识别间隔 5 秒。

## 回滚

- 严格预算：`ANALYSIS_FORWARDER_STRICT_FPS_BUDGET=false`（拓扑路径需改 `_forwarder_env` 或 revert）；
- 排队上限：`ANALYSIS_FORWARDER_MAX_QUEUE_AGE_MS=0`、8090 两处表单设 0，或压测 `--analysis-max-queue-age-ms 0`；
- 或 revert 本分支提交，并重建 forwarder 镜像。

## 部署与复测清单

1. **先测推流**：在 uos157 上对 RTSP 和源文件分别运行探针（见上），记录关键帧率、非常规间隔关键帧数、旧/新采样器放行 FPS。
   若实测额外关键帧很少、旧采样器放行接近 4.0，则说明 8.1% 超额另有来源，需继续定位后再判断本改动的作用。
2. **重建 forwarder 镜像**（代码通过 Dockerfile `COPY app` 打包，未挂载）：
   `docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml build analysis-forwarder`。
   该镜像同时用于 `analysis-forwarder*` 和 `replay-raw-fanout*`。raw fanout 采样关闭、未设排队上限，行为不变，只多了指标。
3. 重建容器：
   - 单分支：`up -d --no-build --force-recreate --no-deps analysis-forwarder replay-raw-fanout`；
   - 双分支：在 8090 运行拓扑重新应用预设；
   - 重建 `api`、`evidence-viewer` 以显示新表单项；
   - 启动日志应含 `strict_fps_budget=True keyframe_debt_s=60.0 max_queue_age_ms=30000`。
4. 保持人脸复识别 5 秒等其余参数不变，复跑 **60 路 × 4 FPS × 1 小时**，**直接按“全部视频 ≤ 180 秒”验收**，并同时记录：
   - 正式窗口的 `keyframes_seen` 率（与探针对照）、`keyframes_over_budget`、接纳速率 / 预算（按差值计算）；
   - 队列深度曲线与 `queue_head_age_ms`；
   - `stale_dropped`、`gop_resync_dropped`：换算成每路未分析秒数；
   - **事件覆盖**：每路入侵、名单事件数与上一轮对比（同片源，注意直播相位不同），排查过期丢帧造成的漏报；
   - 两段耗时分布，`generated_unverified` 数量单独统计。
5. 若队列仍持续增长，说明该内容下 4 FPS 超过实际处理能力：降低 `ANALYSIS_FPS` 或提高人脸检测 interval，不要只靠排队上限丢帧。

## 验证（本机，离线）

- `harness/tests/test_analysis_forwarder.py` 新增 16 项：
  - 额外关键帧下保持预算、旧逻辑超额、规则 GOP；
  - 集中关键帧不超预算，以及短欠账窗口复现复核发现的超额；
  - 短全关键帧片段的欠账全额偿还、长片段的欠账窗口上限；
  - 超预算判定；
  - 队列：过期丢弃与关键帧恢复、控制消息不丢、默认关闭、队首等待；
  - UUID 关键帧在采样器、入队、恢复三处一致；
  - env 读取与指标核算。
- `harness/tests/test_analysis_lag_cap_config.py` 4 项：compose / 8090 拓扑与性能 / 压测脚本的配置入口，raw fanout 不受影响。
- `harness/tests/test_probe_stream_keyframes.py` 5 项：ffprobe 命令、解析、统计、旧/新采样器重放、保存文件读取。
  另对仓库 `testVideo/test.mp4`（10 秒）实际运行探针，命令与输出格式正常；该片段不代表推流内容。
- 全量 harness（`-m "not integration"`）：12 failed / 1715 passed / 21 skipped / 7 collection errors，失败集合与改动前相同（环境项）。
- `docker compose ... config`、`node --check operator.js`、`git diff --check` 通过。
- 未做：真机运行、实际推流探针。

## 剩余风险

- 关键帧假设未经实测，见上。
- 排队上限会在突发期跳过分析帧，可能漏报；只覆盖 forwarder 排队一段。
- 若实际处理能力在某些片段低于 4 FPS，严格预算本身不能让队列收敛。
- 取证阶段与居中校验未处理。

## 实现状态

- 分支 `claude/analysis-budget-20261011`，基于 `main` `c40e930`；未合并、未部署。
- 首版：`9f70869`、`5a101e6`。
- 复核修正（欠账窗口、关键帧判定统一、关键帧/PTS 指标与探针、本文修正）：见后续补记。
- 验证时间：2026-10-11，离线验证。
