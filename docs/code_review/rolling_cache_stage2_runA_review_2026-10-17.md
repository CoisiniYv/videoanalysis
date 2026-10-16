# 阶段 2 A 轮（r5）结果审查（Opus，2026-10-17 KST）

- **被审运行：** `pressure60_stage2a_uos157_20261017_r5`。
  - 采样窗口 06:55:46–07:55:53 KST。
  - 报告位置 `uos157:/data/video-analytics/artifacts/pressure60_stage2a_uos157_20261017_r5/`，含 `report.json`、`sample_summary.json`、`pressure_diagnostics.json` 和 `watchdog.log`。
- **运行代码：** `codex/rolling-cache-storage-guard-20261015` 的 `d37618b`。
- **对照：** `rolling_cache_stage1_rereview_2026-10-17.md` 的 A 轮放行条件。
- **方法：**
  - 根据 Codex 的结果摘要做静态追踪；
  - 检出 `d37618b`，核对 compose 渲染路径、恢复路径和帧率门控的计算方式；
  - 审读 `4d98fb5..d37618b` 的 8 个提交，其中代码改动约 660 行。
- **限制：** 本次没有拿到 report 原件，以下需要实机数据的结论都标为"待证据"。

## 结论

1. **A 轮不通过。** 有效结论只有两条，下面"有效结论"一节列出。证据链路从事件这一步起就没有运行，所以 A 轮没有回答"NVMe 上的证据能否在 180 秒内完成"。
2. **生产可能正处于故障状态（最高优先级）。** 恢复步骤用同一份仓库 compose 重建了 event-worker、media-worker、clip-worker，它们的数据库地址和压测期间一样，很可能仍是错的。"30 个容器都在"不代表它们健康。必须先做只读核查，见 F1。
3. **数据库连接失败是 runner 的配置渲染问题，不是性能问题。** 我在 A 轮任务里要求"用仓库默认 compose"，正是这个要求暴露了它：仓库的 compose 和 env 与 uos157 生产的连接配置并不一致。
4. **滚动缓存的帧率 17.4 fps（门槛 27）是真实问题，还是口径问题，目前还分不出来。** 需要逐跳统计帧数来定位，见 F3。

## 有效结论

| 项 | 结果 | 意义 |
|---|---|---|
| 滚动缓存放在 NVMe 上的发布延迟 | sink 发布 P95 约 21 ms | 10-13 在机械盘上 fsync 要 300–500 ms。把缓存放到 NVMe 这个方向已经确认有效 |
| 摄入端 | 60/60 路源；queue full 为 0；发送失败为 0 | 在 forwarder 计数器能看到的范围内没有丢帧。ZeroMQ PUB 端因 HWM 静默丢弃的帧不在这个范围内，见 F3 |
| 轨迹落库 | 94,204 / 94,204 | person-observation-worker 没有被重建，用的是生产的数据库地址，所以正常 |
| GPU | 最高 61°C | 没有热降频的风险 |

## F1 最高优先级：恢复后的生产 worker 很可能仍连错数据库

- **机制：**
  - 正常路径的恢复（2784、2818、2827 行）和中断路径的恢复（2035、2061、2070 行）都调用 `configure_clip_worker_replay_shards`、`configure_event_worker_evidence_admission` 和 `configure_media_worker_rolling_cache`，用 `--force-recreate` 重建这三个服务。
  - 这三个函数用的都是 `_compose_base_args(cfg)`，也就是 `--env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml`，和压测期间用的是同一套。
  - compose 里写的是 `DATABASE_URL: ${VIDEO_ANALYTICS_DATABASE_URL:-postgresql://video:video@host.docker.internal:5432/video_analytics}`。
  - 仓库的 env 里没有 `VIDEO_ANALYTICS_DATABASE_URL`，runner 也从来不设置它。所以恢复出来的容器仍然连 `172.17.0.1:5432`。
- **后果：** 生产的事件入库、证据生成和剪辑全部停止，8090 看不到新证据。face-worker 和 person-observation-worker 没有被重建，所以不受影响，这也会让问题看起来像"部分正常"。
- **核查（只读，立即执行）：** 对这三个容器读取 `DATABASE_URL`、`State.Status`、`RestartCount` 和最近 50 行日志，与压测前的快照比对。
- **修复（需要用户批准）：** 不要再用仓库 compose 去"修"。按生产原来的创建方式重建这三个容器：使用 `/home/user/video-analytics` 下生产自己的 compose 和 env，`--no-build --no-deps --force-recreate`。重建后回读 `DATABASE_URL`，与压测前快照一致；60 秒内 RestartCount 不增长；日志里没有 `connection refused`。

## F2 数据库地址漂移：根因与修法

- **根因：** runner 只知道如何覆盖压测相关的参数，并不知道生产的连接配置是什么。从仓库渲染出来的 compose 和生产实际在跑的容器不一致，而现有的预检只核对压测相关的键，所以发现不了。
- **为什么 10-12 没出问题：** 10-12 用的是手工改过的生产 compose，连接配置碰巧是对的。
- **修法（三项都要）：**
  1. **连接配置取自生产快照。** 压测开始前，从将要重建的每个生产容器的 `docker inspect` 中读出连接类变量：`DATABASE_URL`、`VIDEO_ANALYTICS_DATABASE_URL`、`REDIS_URL`、`API_BASE_URL` 等，以及 compose 的插值来源。runner 在压测和恢复时调用 compose 的进程环境里，都要显式带上这些值。报告记录键名和脱敏后的主机与端口。
  2. **预检时比对渲染结果与生产容器（失败即拒绝）。** 对每个将要重建的服务，用 `compose config --format json` 渲染出环境变量，与该服务当前生产容器的环境变量逐键比较：
     - 差异键只能出现在压测允许改动的白名单里（`FORWARDER_*`、`FACE_*_INTERVAL`、`ROLLING_CACHE_*`、`EVIDENCE_*` 中压测确实请求的那些键等）；
     - 白名单之外有任何差异，就返回 2，并列出差异键（值脱敏）。

     这一项比对能直接拦下 A 轮的问题。
  3. **预热阶段尽早失败。** 正式采样开始前，对每个被重建的 worker：
     - 在容器内执行一次 `SELECT 1`（或者读 worker 自己的健康接口）；
     - 60 秒内 RestartCount 不得增长。

     任一项失败，就直接进入恢复，状态记为 `failed_preflight_runtime`，不要白白跑满一小时。
- **测试：** 用"生产容器的 env 有 `DATABASE_URL=…:5433…`、仓库 env 没有"作为夹具：预检必须返回 2；带上快照后，渲染出的结果与生产一致。

## F3 滚动缓存帧率 17.4 fps

- **口径：**
  - `segment_frame_rate_fps` 等于每个 4 秒段的帧数除以段内 PTS 跨度，单位是纳秒（11145–11182 行）；
  - 门槛 = max(输入 ffprobe 的 `avg_frame_rate` × 0.90, 最低值)（12765–12782 行，以及 5499 行的探测）。
  - 因此"预期 30"来自本轮的输入文件，17.4 是 PTS 域里实测的结果。
- **即使输入换回 10-12 用的 23.976 fps 片源，门槛也有 21.6，17.4 仍然不够。** 所以这不只是"换了输入"造成的。但换输入本身违反了"每轮只改一个变量"，必须先确认。
- **待证据的假设（按可能性排序）：**
  1. **raw 输出端的 ZeroMQ PUB 在 HWM 满时静默丢帧。** `FORWARDER_RAW_OUT_ENDPOINT` 是 `pub+bind`，订阅端（sink）跟不上时 PUB 直接丢弃，不计入 queue full，也不计入发送失败。这与"queue full 0、发送失败 0、sink 发布正常"完全吻合。
  2. **单个 sink 的接收吞吐不够。** 每个 sink 有 30 路 × 30 fps = 900 帧/秒，而 sink 只有一个工作线程（发布 P95 低，只说明落盘快，不代表接收快）。
  3. **RTSP 转推跟不上实时。** 60 个 ffmpeg 进程以 copy 模式转推，`speed=` 可能小于 1，PTS 间隔被拉大。
  4. **片源是可变帧率（VFR），或者 PTS 时间基不一致。** 可能性较低：如果是这样，`avg_frame_rate` 与段内 PTS 的口径就对不上。
- **定位方法：** 取同一个 60 秒窗口、同 3 路源，逐跳统计帧数：
  1. 转推 ffmpeg 日志里的 `frame=`、`speed=`；
  2. 用 ffprobe 读 RTSP 输出 10 秒（`-count_frames`）；
  3. Replay 摄入的帧数；
  4. replay-raw-fanout 每路的入帧数和 raw 出帧数；
  5. sink 每路收到的帧数；
  6. 段内写入的帧数。

  在哪一跳从约 30 掉到约 17，根因就在哪一跳。
- **要附上的资料：**
  - 输入文件的路径、SHA256，ffprobe 的 `avg_frame_rate`、`r_frame_rate` 和 `nb_frames`；
  - 同样指标在 10-12 片源上的结果；
  - 10-12 的报告里如果有 sink 帧数，也一并附上。

## F4 ROI embedding 为 0

- **结论：** 与数据库无关。face-worker 不在重建列表里（只有 `configure_*` 会重建 event、media、clip 这三个 worker），它保留了生产的数据库地址。
- **链路：** Savant 导出 ROI（run 专属 stream `FACE_ROI_STREAM`）→ adaface-roi-worker → `security.face_observations` → face-worker → PostgreSQL。
- **要逐跳取证：**
  - savant-a/b 是否在导出 ROI：`FACE_ROI_EXPORT_ENABLED` 回读，以及 ROI 导出计数；
  - 用 `XLEN` 查 run 专属 ROI stream 的长度，以及 consumer group 的 pending；
  - adaface-roi-worker 的 metrics 是否可达（`127.0.0.1:18187` 需要映射宿主端口），`va_adaface_roi_messages_total` 是多少，以及日志；
  - 片源里有没有人脸：用 10-12 同一片源时 face 观测的数量。
- **注意：** 如果本轮换了片源，"没有人脸"就可能是最简单的解释，这又回到"每轮只改一个变量"。

## F5 watchdog 在恢复阶段误发 SIGINT

- **watchdog 不在仓库里，没有经过审查，却可以向 runner 发信号。** 它把"savant-a/b 退出"判为异常，而按路线二的设计，恢复阶段本来就要停止双路容器。
- **代码问题（`cc5b33d`）：** 只有中断和异常时走的 `abort_recovery` 会忽略 SIGINT，正常路径的恢复（2780–2930 行）没有这层保护。
  - 恢复中途收到 SIGINT 会抛出 KeyboardInterrupt，`restore_step` 只捕获 `Exception`，拦不住它。于是程序转入 `abort_recovery("after_interrupt")`，把恢复再做一遍。
  - 本轮实际发生的就是这个过程，所以最终状态被覆盖成了 `interrupted`，而不是 `failed_pressure_gates`。
- **修法：**
  1. 正常路径的恢复整段也忽略 SIGINT（复用 `abort_recovery` 的做法）；
  2. runner 把当前阶段（warmup、sampling、drain、restore）写到 `$ART/phase`；
  3. 在 runner 内做尽早失败的判断（F2 第 3 项），取代外部 watchdog；
  4. 外部 watchdog 如果要保留，必须提交进仓库并经过审查，只在 sampling 阶段生效，只看 RestartCount 的增量，不看容器停止，只处理主机级的安全问题（GPU 温度、磁盘剩余空间）。

## F6 流程

- A 轮实际运行的是 `d37618b`，比我复审放行的 `4d98fb5` 多 8 个提交（代码改动约 660 行），最后一个提交在采样前 3 分钟。
- 补审结论：
  - `cc5b33d` 落实了放行条件的第 1、2、3、5 条：双路容器只比对、不重建；预检渲染恢复配置；压测源判定只看 `site_id`，并回读 enabled 状态；恢复完成后的读取包进了 `restore_step`；冷却统计按实际请求的规则计算；
  - `60f780c` 让预检的渲染带上本轮的运行参数，这是对的；
  - `74f1a52` 和 `d37618b` 都是很小的改动。
- 遗留问题：
  1. 正常路径的恢复没有 SIGINT 保护，见 F5；
  2. `PRESSURE_FORWARDER_IMAGE` 从进程环境读取，报告必须记录解析后的镜像 ID；
  3. 预检仍没有比对连接配置，见 F2。

## 下一次实机运行之前

1. 先处理 F1，并经用户批准，把生产恢复到压测前的状态。
2. 修好 F2 的三项和 F5 的第 1、2、3 项，补上测试，推送后由 Opus 复审。复审时运行的提交必须和推送的提交一致。
3. 拿到 F3、F4 要求的证据（只读取证，或者 10 分钟的短跑，需要单独批准窗口）之后，再决定下一轮的变量。
4. 片源与 10-12 保持一致，除非用户明确批准更换。
