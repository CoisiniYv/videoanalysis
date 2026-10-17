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
- **更新（同日 08:34 KST 之后）：**
  - Codex 对生产做了只读核查，推翻了初版 F1 的"生产当前已损坏"；
  - 随后做了第二轮静态追踪，包括 3 路并行追踪和 2 路反驳审查，并用 `env -i docker compose config` 实际渲染验证；
  - 修订了 F1、F2，新增 F7、F8。

## 结论

1. **A 轮不通过。** 有效结论只有两条，下面"有效结论"一节列出。证据链路从事件这一步起就没有运行，所以 A 轮没有回答"NVMe 上的证据能否在 180 秒内完成"。
2. **生产当前正常（Codex 08:34 实机核查）。** 但恢复得对，靠的是 CPU 隔离恢复步骤顺带把容器整套环境变量钉回了快照值，不是恢复设计本身可靠。换一组参数，恢复会报"通过"，而 event、clip 仍连着错误的数据库。见 F1。
3. **数据库连接失败是 runner 的配置渲染问题，不是性能问题。** 我在 A 轮任务里要求"用仓库默认 compose"，正是这个要求暴露了它。同一个坑 2026-07-21 就踩过一次，当时的正确做法记录在 `media_worker_finalizer_admission_fenced_retry_2026-07-21.md` 第 429–433 行，但没有固化进 runner。见 F2。
4. **滚动缓存的帧率 17.4 fps（门槛 27）是真实问题，还是口径问题，目前还分不出来。** 需要逐跳统计帧数来定位，见 F3。

## 有效结论

| 项 | 结果 | 意义 |
|---|---|---|
| 滚动缓存放在 NVMe 上的发布延迟 | sink 发布 P95 约 21 ms | 10-13 在机械盘上 fsync 要 300–500 ms。把缓存放到 NVMe 这个方向已经确认有效 |
| 摄入端 | 60/60 路源；queue full 为 0；发送失败为 0 | 在 forwarder 计数器能看到的范围内没有丢帧。ZeroMQ PUB 端因 HWM 静默丢弃的帧不在这个范围内，见 F3 |
| 轨迹落库 | 94,204 / 94,204 | person-observation-worker 没有被重建，用的是生产的数据库地址，所以正常 |
| GPU | 最高 61°C | 没有热降频的风险 |

## F1（已修订）：生产没有坏，但恢复的正确性只是碰巧

- **初版判断（已被推翻）：** 初版认为恢复步骤和压测用的是同一套仓库配置，所以生产应该已经坏了。
- **推翻依据：** Codex 08:34 实机只读核查：
  - event、media、clip、face、person 这 5 个 worker 都是 running，DATABASE_URL 都是 `postgres:5432`；
  - 近期日志里连接错误为 0；
  - event、media、clip 的 compose 标签指向生产 checkout，并带有 r5 的 CPU 隔离恢复 override。
- **我漏看的地方：** 恢复时用的 `restore_cfg = formal_restore_config(cfg)`（1393–1410 行）会读取生产 media-worker 的 compose 标签，改用生产的 compose 和 env 文件。恢复期间只有 `compose stop`（2774、2029 行）还用仓库配置。
- **恢复结果正确的真正原因（第二轮追踪 + 反驳审查确认）：**
  1. event 和 clip 的恢复只回写各自请求的 16 个和 8 个键（5272–5305、5246–5269 行），校验也只比这些键（6229–6236 行），不涉及 DATABASE_URL。
  2. 正常恢复里最后一次重建是 `restore_worker_cpu_isolation`（2894–2901 行）。它把压测前（2196 行）快照里完整的 `Config.Env` 和镜像 ID 写成 override，并校验完全一致（5127–5159 行）。三个容器带着 CPU 隔离恢复 override 标签，说明它们的最终状态来自这一步。
  3. media 的恢复在 rolling-cache 模式下也会回写完整环境变量（2187、5308–5310 行）。
- **隐患：** 如果 `--cpu-isolation-profile none`（这是默认值，913–916 行），或者走的是中止恢复路径，就没有"回写完整环境变量"这一步。这时 event 和 clip 的 DATABASE_URL 取决于生产 env 文件里有没有 `VIDEO_ANALYTICS_DATABASE_URL`（目前未知），以及 runner 所在 shell 的环境。恢复照样报"通过"，生产却连着 `host.docker.internal:5432`。
- **修法：** 并入 F2 的第 2、4 项：
  - 恢复后校验完整环境变量（减去镜像自带的 ENV），而不是只比请求过的键；
  - 恢复后也要执行连通性检查。

## F2 数据库地址漂移：根因与修法（已修订）

- **根因（已确认）：**
  1. 压测阶段对 event、media、clip 的每一次重建都使用 `cfg`（事件 2268→6180→6214–6222，媒体 2268→6185→5444–5453，剪辑 2284/2333/2379→6366–6373）。
  2. `cfg` 的 compose 和 env 文件是相对路径默认值，按 runner 进程的 cwd 解析（77–78、1024–1025、3378、3382 行），也就是仓库 checkout 里的文件。
  3. 仓库 compose 写的是 `DATABASE_URL: ${VIDEO_ANALYTICS_DATABASE_URL:-…@host.docker.internal:5432…}`（53、1059、1124、1151、1261、1299、1569 行），仓库的 `midterm.env` 不定义这个变量，runner 也不设置。
  4. 生产里的这个值是 `scripts/midterm_start.sh:398-407` 在 `--local-postgres` 下，只在自己的进程里 export 的，不会落盘。
  5. 仓库 postgres 只映射宿主端口 5439（compose 133 行），宿主 5432 上没有服务，所以连 `172.17.0.1:5432` 被拒。
  6. face-worker 和 person-observation-worker 在压测期间只被 `docker update` 过（4971–4999 行），没有重建，所以正常。
- **验证：** 在空环境（`env -i`）里用仓库文件执行 `docker compose config`，对比两种渲染：
  - 不设任何变量的渲染；
  - 按 midterm_start 方式设置 `VIDEO_ANALYTICS_DATABASE_URL=postgres:5432` 并启用 `--profile local-postgres` 的渲染。

  两者只差 api、clip、event、face、media、person 这几个服务的 DATABASE_URL，以及 postgres 服务本身。
- **为什么 10-12 没出问题：** 10-12 用的是手工改过的生产 compose。2026-07-21 那次的正确做法是同时固定宿主侧的 `DATABASE_URL=:5439` 和容器侧的 `VIDEO_ANALYTICS_DATABASE_URL=postgres:5432`（见上述 07-21 文档），但这一点从未写进 runner。
- **另一个问题：** runner 在发现漂移之后也没有察觉。重建后的校验只比较 runner 自己设置的键（事件 6230–6236、媒体 5465–5471、剪辑 6438 行起），`run(check=True)` 只看 compose 的返回码。
- **修法（第 1–4 项必须做，第 5、6 项建议一起做）：**
  1. **启动时检查基线是否健康，再拍快照。** 在任何改动之前，对所有使用数据库的生产容器（api、event、media、clip、face、person）：
     - DATABASE_URL 必须一致；
     - 不得指向 `host.docker.internal`、`172.17.0.1` 或环回地址；
     - 在容器内执行 `SELECT 1` 必须成功。

     任一项不满足就返回 2，避免把已经坏掉的值当作基线带下去。
  2. **集中钉住连接值。** 用通过检查的值设置 `os.environ["VIDEO_ANALYTICS_DATABASE_URL"]`，不要逐个调用点去补：
     - 所有 compose 调用都继承进程环境，包括 `env=None` 的那些（3485、3540、3815 行起、3885、5145 行），以及中止、finally、错误分支的路径，所以设置一次就能全部覆盖；
     - 进程环境里的值不会被再次插值，密码中含 `$` 也安全；
     - 报告记录生产 env 文件里是否也定义了这个变量；两者不一致时如实记录，不要掩盖。
  3. **检查连通性，压测和恢复都要做。** 在最后一次重建之后执行：
     - 最后一次重建包括剪辑分片的 2333/2379 行、sink、cpuset 重新应用，以及 `--dual-shard-api` 下 8090 topology apply 按 inspect 克隆出的容器；
     - 对每个使用数据库的 worker：先读重建后的 RestartCount 作为基线，执行 `SELECT 1`（`docker exec` 失败直接算失败，不重试），60 秒内 RestartCount 不得增长；
     - 压测阶段失败就直接进入恢复，状态记为 `failed_preflight_runtime`；
     - 恢复之后执行同样的检查，失败时状态为 `failed_restore_verification`。
  4. **恢复后校验完整环境变量。** event、clip、media 的恢复校验改为比较完整 `Config.Env` 与快照：
     - 先减去 `docker image inspect` 得到的镜像自带 ENV；
     - 只忽略明确列出的少量键。

     这一项修掉 F1 的隐患。
  5. **预检时比较渲染结果与生产容器（建议）。** 对将要重建的服务，用与真实 `up` 完全相同的参数渲染：
     - 同样的 override 文件（事件 6205、媒体 5432–5446、双路 3491/3520、sink 5534）；
     - 同样的进程环境叠加。

     然后与生产容器比较：
     - 镜像自带的 ENV 要减掉；
     - inspect 失败时按失败处理；
     - 名称里带 `URL`、`HOST`、`ENDPOINT`、`ROOT`、`DIR`、`DEVICES`、`PORT` 的键，只要变化就失败，包括新增键的值指向网关或环回地址；
     - 其他差异只写进报告。

     不要手写一份宽白名单，那会把真正的漂移掩盖掉。
  6. **记录 cwd 并核对恢复配置的来源。** 报告记录 runner 的 cwd 和完整 argv。断言 `restore_cfg` 的 compose 文件与 `cfg` 不同，并且其 working_dir 等于显式传入的 `--formal-root`（例如 `/home/user/video-analytics`）。否则返回 2，防止 media-worker 的标签被上一轮污染后，"正式恢复"实际指向仓库。
- **脱敏：** 新增的任何输出都要去掉 URL 中的 userinfo。现有产物里已有明文副本，后续单独清理：`report.json` 和 `run_config.json` 的 `config.db_url`、`cpu_isolation_before.json`、CPU 隔离和 media 恢复的 override yml。
- **测试：** 至少覆盖以下情形：
  - 生产容器的 DATABASE_URL 指向 `postgres:5432`、仓库 env 不定义该变量时，钉住后渲染结果与生产一致；
  - 基线指向 `host.docker.internal` 时返回 2；
  - `docker exec` 失败时检查不通过；
  - `--cpu-isolation-profile none` 时，恢复校验能抓到 DATABASE_URL 被改。
- **当次运行的临时办法（代码修好之前，只能作为补充）：** 启动 runner 时同时 export 宿主侧的 `DATABASE_URL=…:5439…` 和容器侧的 `VIDEO_ANALYTICS_DATABASE_URL=postgresql://video:***@postgres:5432/video_analytics`，与 07-21 的做法一致。

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

## F7 双路容器仍处于压测配置（新增，需要只读核查）

- 按路线二，双路容器只停止、不重建，恢复过程只读取它们的状态（3715–3790 行，`recreated: False`）。Codex 08:34 的 5 容器核查没有覆盖它们。
- **范围：** savant-a/b、replay-raw-fanout-a/b、adaface-roi-worker。direct 模式下被停止的 analysis-forwarder-a/b、replay-a..h、video-file-sink-a..h 也在内。
- **它们目前保留的压测配置：**
  - `SAVANT_MODULE_FILE=<artifact>/module.pressure.yml`（3921 行）；
  - `SAVANT_B_MODEL_ROOT`、GPU 编号（3512–3515 行）；
  - `PRESSURE_FORWARDER_IMAGE` 指定的镜像（3985–3987 行）；
  - 仓库 checkout 的 bind mount；
  - 带 run 编号的 ROI stream 和 consumer group。
- **新风险：** 8090 启动双路时会按 `docker inspect` 克隆这些容器（`runtime_performance.py:980-1014`），而 `_savant_env` 不会重置 `SAVANT_MODULE_FILE`（`runtime_topology.py:1634-1700`）。只要 r5 的产物目录被移动或清理，8090 下次启动双路就会加载不到 module。
- **要求：**
  - 只读回读这些容器的 `SAVANT_MODULE_FILE`、模型挂载、镜像 ID、`config_files` 标签和状态；
  - B2（用 Docker API 克隆实现精确恢复，或者经用户批准把它们规范化回正式配置）的优先级提到 B 轮之前；
  - 在此之前，r5 的产物目录不得移动或删除。

## F8 缺少完整的部署快照（新增）

- Codex 报告：r5 目录里没有完整的 `deployment_snapshot_before.json`，只有 5 个容器的 CPU 和双路快照，所以无法对 30 个容器做压测前后的精确比对。
- **要求：** 在第一次改动之前，对所有 `video-analytics-midterm-*` 容器保存完整的 `docker inspect`（Config、HostConfig、Mounts、NetworkSettings.Networks、Image ID）。结束时再保存一份，并写出差异：
  - 可以忽略的差异：Id、Created、State 中的时间戳、RestartCount 被重置；
  - 其余每一项差异都必须归入"已批准的漂移"或"恢复失败"之一。
- 这份快照同时是 B2 精确恢复的输入。

## 下一次实机运行之前

1. ~~先处理 F1~~：Codex 08:34 实机核查确认生产正常，不需要修复。
2. 修好 F2 的第 1–4 项（第 5、6 项建议一起做）、F1 隐患（已并入 F2 第 4 项）、F5 的第 1、2、3 项、F6 遗留问题第 2 项和 F8，补上测试，推送后由 Opus 复审。实机运行的提交必须和复审通过的提交一致。
3. 拿到 F3、F4、F7 要求的只读证据之后，再决定下一轮的变量。需要短跑时单独申请窗口。
4. 片源与 10-12 保持一致，除非用户明确批准更换。

## 仍未知（需要 Codex 只读提供）

- r5 的完整 argv 和 cwd：`--cpu-isolation-profile`、`--rolling-cache-evidence`、`--dual-shard-same-gpu`、`--dual-shard-api`、`--db-url`，以及 shell 是否 export 了 `VIDEO_ANALYTICS_DATABASE_URL`。
- 生产 env 文件 `/home/user/video-analytics/infra/env/midterm.env` 是否定义了 `VIDEO_ANALYTICS_DATABASE_URL`（只报键是否存在，不报值）；event-worker 的 `Config.Env` 里是否有这个键。
- r5 的 `cpu_isolation_restore.json`、`compose_restore_event_worker_evidence_admission.log`、`compose_restore_clip_worker_replay_shards.log`：用来确认 F1 修订版的推断。
