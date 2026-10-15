# 滚动缓存阶段 0：盘位、配置漂移与修复前快照（2026-10-15）

后续状态：用户已接受现状并明确放行阶段 1，见 [阶段 0 决策记录](rolling_cache_phase0_decision_2026-10-15.md)。本文保留首次核查时的停止结论与原始口径。

## 1. 一句话结论

**停止，阶段 0 决策门未通过。** 正式三个缓存相关容器的有效缓存源一致，但均在 SATA 机械盘；当前没有启用的摄像头或发布样本，不能据此验证取证健康。方案 A 的只读渲染额外改变 media-worker 的 24 项环境配置，且保存的运行时预检失败，无法满足本次仅允许改缓存挂载和去除产物 override 依赖的白名单。T0.2 的 ART 缺少归一化重试原因，另命中 T0.3 第 ⑤ 行。

用户在首次 T0.1 停止后，已明确授权继续只读核查、T0.2、创建文档分支及准备方案 A；该授权不包括服务变更或进入阶段 1。

## 2. 做了什么

- 任务书提交：`68400c8f86940c972296c2ae300ad140c3d2e746`，父提交 `98d388cd6f64f45ec36431b35f5e4e316bf41f55`。
- 本地分支：`CZray/pressure60-allalg-review-20261014`；独立工作树 `/Users/khonsu/.codex/worktrees/pressure60-phase0/SavantProject`。本轮只有 `docs/code_review/` 文档和证据变更。
- 目标主机：SSH `uos157`，`192.168.1.100:22`；`hostname` 为 `localhost.localdomain`，时区回读 `Asia/Beijing`。此前 `user157` 别名的初始读取已排除，不作为本次证据。
- 现场命令只读：`hostname`、`timedatectl show -p Timezone`、`docker ps -a`、`docker container inspect`、`realpath -e`、`findmnt -T`、`lsblk`、`df`、`du -sx`、`docker logs`、只读事务内的 SELECT、8090 HTTP GET、`docker compose ... config --format json`、`docker image inspect`。没有运行 POST、start/stop/up/restart/build、写 SQL、正式目录文件修改或删除。
- 执行偏差：初次业务采集额外只读查询了历史库行，超出 T0.2“只读 `$ART`”限定；发现后停止该查询，没有写入。原始记录保留于业务报告附录，不作为 T0.2/T0.3 当轮证据。当前业务摄像头和最近 24h 任务查询属于用户明确授权项。
- 盘位证据：[storage.json](evidence/rolling_cache_phase0_20261015/storage/storage.json)，原始命令/返回码在 [storage/capture.json](evidence/rolling_cache_phase0_20261015/storage/capture.json)。采集 `2026-10-15 18:57:34 KST`，原始 UTC `2026-10-15T09:57:34.334667Z`。
- 业务证据及精确窗口：[业务影响报告](rolling_cache_business_impact_2026-10-15.md)、[current-business.json](evidence/rolling_cache_phase0_20261015/business/current-business.json)。
- 历史复盘：[10-13 报告](pressure60_allalg_uos157_1h_2026-10-13.md)。方案和卷/环境比对：[方案 A 执行单](rolling_cache_plan_a_2026-10-15.md)、[plan_a_config_comparison.json](evidence/rolling_cache_phase0_20261015/plan/plan_a_config_comparison.json)。
- 本地文档提交和核验窗口在 [阶段 0 提交记录](rolling_cache_phase0_provenance_2026-10-15.md) 补记；该记录不是服务器部署 SHA 或推送时间。

### T0.1 容器有效挂载

来源：修复前快照的 `containers[].Mounts`、`State`、`Config.Env` 和 compose 标签。采用最长目标前缀解析 `/media/rolling-cache*`，不是仅凭父 `/media` 判断。

| 容器（省略 `video-analytics-midterm-`） | 状态 | 是否在用 | `/media/rolling-cache` 源 | `/media/rolling-cache-materialized` 源 | 设备 / ROTA / TRAN | compose 工作目录 | 紧急度 |
|---|---|---|---|---|---|---|---|
| rolling-cache-sink-a | running / healthy | 容器运行；当前发布样本为 0 | `/data/video-analytics/media/rolling-cache` | `/data/video-analytics/media/rolling-cache-materialized` | sda1 → sda / 1 / sata | `/home/user/video-analytics/infra` | 配置问题需修；当前业务影响无法验证 |
| rolling-cache-sink-b | running / healthy | 容器运行；当前发布样本为 0 | 同上 | 同上 | sda1 → sda / 1 / sata | 同上 | 同上 |
| rolling-cache-sink | missing | 不适用 | 无 | 无 | 不适用 | 无 | 明确记录不存在 |
| media-worker | running | `ROLLING_CACHE_ENABLED=true`，`ROLLING_CACHE_MATERIALIZATION_ENABLED=true`；启用摄像头为 0 | 同上 | 同上 | sda1 → sda / 1 / sata | 同上 | 已触发任务书停止条件，等待修复审批 |

没有检测到 sink/worker 挂载源不一致。容器 running / healthy 不等于有活跃缓存管线或业务验收通过。

### 盘位与空间

来源：`storage.json.paths[]` 的 `realpath`、`findmnt`、`df_h`，`devices[]` 和 `lsblk_inverse`。三个 `realpath -e` 均返回 0，无需替代父目录。

| 路径 | 挂载点 / 文件系统 | 设备链 | 底层盘型 | `df -h` 容量 / 可用 / 使用率 |
|---|---|---|---|---|
| `/data/video-analytics/media/rolling-cache` | `/data` / ext4 | `/dev/sda1` → `/dev/sda` | ST2000DM001-1CH164，ROTA=1，TRAN=sata | 1.8T / 1.6T / 7% |
| `/data/video-analytics/media/rolling-cache-materialized` | `/data` / ext4 | 同上 | 同上 | 同上 |
| `/home/user/video-analytics-fast` | `/home` / xfs | `/dev/mapper/uos-home`（dm-2）→ nvme0n1p3 → nvme0n1 | ZHITAI Ti600 500GB，ROTA=0，TRAN=nvme | 379G / 291G / 24% |

遗留目录只记大小、不删除：`storage.json.legacy_cache_usage` 的 `du -sx --block-size=1` 分别为 **22,491,136 字节**和 **7,749,632 字节**。它们是采集时磁盘占用，不是历史发布量、录像时长或可复用证据量。

### media-worker 的 compose 依赖与环境漂移

标签 `com.docker.compose.project.config_files` 完整内容：

```text
/home/user/video-analytics/infra/docker-compose.midterm.yml,/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2/compose_restore_media_worker_rolling_cache.override.yml
```

| 标签引用文件 | 当前存在 | 字节数 | SHA256 |
|---|---|---|---|
| `/home/user/video-analytics/infra/docker-compose.midterm.yml` | 是 | 85,761 | `2517b188a835f70e32a24fe7892446b9521f80882ca8f50200cf933b269133b5` |
| `/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2/compose_restore_media_worker_rolling_cache.override.yml` | 是 | 5,486 | `c75bda0a8d71fa16fe287b9d66b2c5402c2d0533380bbb04191bb58522b9f0be` |

来源：`snapshot/pre_repair_snapshot.json.config_files[]`。没有悬空引用；对历史产物的依赖本身仍存在。

对比来源：10-13 `report.json.media_worker_rolling_cache_restore.requested/observed` 与当前 `.Config.Env`，完整逐键结果在 [media_worker_environment_comparison.json](evidence/rolling_cache_phase0_20261015/snapshot/media_worker_environment_comparison.json)。

| 比较项 | 10-13 恢复快照 requested | restored observed | 当前 | 状态 |
|---|---|---|---|---|
| 可比较环境键 | 45 | 45 | 对应 45 键 | 全部一致，差异 0 |
| 当前完整环境键数 | 完整基线未保存 | 完整基线未保存 | 308 | 其余 263 键无法判断是否发生漂移 |
| `ROLLING_CACHE_SOURCES` | `pressure60_allalg_rr157_uos157_1h_fixed_20261013_00` 至 `_59`，60 个 | 相同 | 相同 | 已确认有压测源残留，且原恢复快照本身就带这些值 |
| `ROLLING_CACHE_RETENTION_SECONDS` | 4020 | 4020 | 4020 | 已确认残留值；两个 sink 当前各为 300 |
| `ROLLING_CACHE_ENABLED` / `MATERIALIZATION_ENABLED` | true / true | true / true | true / true | 相同 |
| `MEDIA_WORKER_FINALIZER_WORKERS` / `MATERIALIZATION_MAX_ACTIVE` | 4 / 4 | 4 / 4 | 4 / 4 | 相同 |

因此不能写成“10-13 恢复后又漂移了这些键”，也不能把 263 个无基线键说成新增键。恢复快照是脚本记录的选定变量，不是完整压测前 `Config.Env`，更不是已经确认正确的正式业务配置。

### 全局规则 5：修复前快照

快照窗口：`2026-10-15 18:57:34.332556–18:57:34.403817 KST`；原始 UTC `2026-10-15T09:57:34.332556Z` 至 `2026-10-15T09:57:34.403817Z`。

- 已按 `docker ps -a` 列出并 inspect **30 个** `video-analytics-midterm-*` 容器，含停止、created 和回滚备份容器。
- 原始完整 inspect 包含 `Image`、`Config.Env`、`Mounts`、`HostConfig.CpusetCpus`、`State`、`RestartCount`、working_dir/config_files 标签及其他 inspect 字段。
- [脱敏快照](evidence/rolling_cache_phase0_20261015/snapshot/pre_repair_snapshot.json) 保留全部结构、环境键和非敏感值；凭据值以摘要标记替代。
- [完整加密快照](evidence/rolling_cache_phase0_20261015/snapshot/pre_repair_snapshot.json.aes256gcm) 使用 AES-256-GCM；[元数据](evidence/rolling_cache_phase0_20261015/snapshot/pre_repair_snapshot.json.encryption.json) 保存 IV、认证标签和明/密文 SHA256。已在本机解密回读并逐字节验证。密钥和明文原件保留在工作树外 `/Users/khonsu/.codex/private-evidence/rolling_cache_phase0_20261015/`，未推送。
- **快照缺口：** 正式目录 `/home/user/video-analytics/.git` 不存在，宿主机 `git` 不可用；`git rev-parse HEAD` 和 `git status` 均 rc=127。没有伪造正式部署 SHA 或干净状态；已保存错误原文及引用配置文件 SHA256，不能声称全局规则 5 的 Git 项完整满足。
- 将来真正维修前必须刷新快照、核对容器集合和配置摘要；本次快照不能替代未来维护窗口的即时基线。

## 3. 关键数字对比

本表“上一轮”指首次 T0.1 报告，不把当前空载读数与历史压测指标混为同一负载。10-12 的历史指标详见独立 T0.2 报告。

| 指标 / 来源 | 上一轮 | 本轮只读核查 | 10-12 |
|---|---|---|---|
| 当前缓存底层盘 / `storage.json.devices` | 未核实 | sda，ROTA=1，sata | 当前主机探测不能回证当轮盘型 |
| 目标快盘 / `storage.json.lsblk_inverse` | 未核实 | dm-2 → nvme0n1p3 → nvme0n1，ROTA=0 | 不适用 |
| 启用非压测摄像头 / `business.queries.camera_counts.rows` | 未查 | 0；非压测总数 1，全部摄像头 61、全部禁用 | 未作同口径比较 |
| 最近 24h 非压测任务 / `current_evidence_counts/population` | 未查 | 0；按 task_type / status 分组结果为空，全表同期任务也为 0 | 未作同口径比较 |
| 最近一次非压测过期 / `current_latest_expired`、`alltime_latest_nonpressure_expired` | 未查 | 查询无匹配行，时间无法取得 | 未作同口径比较 |
| sink A/B 发布样本 / `business.logs.*.summary.count` | 未查 | 返回日志中均 0；P50/P95 和增长趋势无法取得 | 历史负载分布见 T0.2 |
| 修复前容器快照 / `snapshot.container_count` | 未做 | 30 个，Git 项存在缺口 | 不适用 |
| storage/operator 渲染 Env 差异 / `plan.service_diffs.storage_operator_clean` | 未查 | sink A=0、B=0；media-worker=24 | 不适用 |

业务窗口：24 小时 `2026-10-14 19:00:19.928700` 至 `2026-10-15 19:00:19.928700 KST`；sink 查询窗口 `2026-10-15 18:30:19.928700` 至 `19:00:19.928700 KST`。sink 日志最早只到约 `18:44:54 KST`；返回日志不证明完整 30 分钟的运行覆盖，不能把分位数写成 0。

## 4. 已确认 / 推断 / 未核实

**已确认：** 当前缓存实际在机械盘，目标快盘实际在 NVMe；当前全部摄像头禁用；当前 24h 无任务、可用 sink 日志无发布样本；media-worker 依赖存在的 10-13 override 且保留压测源与保留期；完整原始容器快照已加密留存；原生方案 A 的 config/运行时重新应用超出白名单。每项证据及口径见以上链接。

**推断：** 当前没有活跃录像业务，因此从这些读数看不出正在受影响的非压测事件；这不证明未来启用摄像头后取证健康。当前压测残留可能源自更早一轮，但仅凭 10-13 恢复记录不能确定引入时间。

**未核实 / 无法取得：** 现场正式部署 Git SHA/工作区状态、未被恢复快照保存的 263 个环境键的压测前值、当前有负载时的发布延迟和新事件取证、真实流量的日内低峰、只读 ART 范围内完整的历史重试审计。当前盘型不得替代 10-13 当时宿主环境变量和挂载证据。

## 5. 下一步建议与需要用户决定的事项

方案 A 已写成独立审查单，但**不是可直接批准执行的维修单**：media-worker 24 项额外 Env 变化、8090 保存配置要求 40 个启用源而当前为 0、`midterm_start.sh` 会处理目标三服务以外的服务，均未获准。执行单在任何 POST/stop/up 前强制停止，不用扩大默认值或重放保存配置来绕过这些问题。

建议先由用户确定维修后的运行时/摄像头目标，再决定是否允许单独修复运行时配置，或者另行准备严格保留全部当前配置的挂载切换方案。后者也不在本轮执行。新事件验证需要至少一个经批准启用的非压测源，不能在全部禁用时虚报通过。

不进入阶段 1，不执行维修，不启动阶段 2。阶段 1 即使后续获准，仍需完成测试并推送后交 Opus 审查；阶段 2 每轮仍需单独维护窗口批准。
