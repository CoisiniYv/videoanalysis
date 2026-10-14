# 任务书：滚动缓存落盘修复与分步复测（2026-10-14）

| 角色 | 谁 |
|---|---|
| 执行 | Codex |
| 审查 | Opus |
| 决策 | 用户 |

- **代码基线：** `98d388c`（分支 `CZray/evidence-remux-wake-review-20261011`）。10-12 一小时验收通过的取证调度修复就是这个版本；它和 `0523808` 只差文档提交。
- **本任务书：** 只布置工作，不改代码。
- **执行顺序：** 各阶段按顺序执行。前一阶段的决策门没过，不进入下一阶段。

## 0. 背景：已确认、推断、待核实、已推翻

### 已确认（有代码或仓库报告依据）

1. **取证调度问题已修好。**
   - 10-12：60 路 × 4 FPS × 1 小时，2905/2905 条视频 ≤180 秒，P95 76 秒。
   - 条件：intrusion + watchlist、人脸间隔 3、remux 1、WIP 4、media-worker 绑 CPU 7。
   - 报告：`docs/code_review/pressure60_remux_refill_uos157_1h_2026-10-12.md`。
   - 该轮 harness 状态本身就是 `failed_pressure_gates`，有 6 个已知失败项，不属于取证期限。
2. **压测脚本重建滚动缓存相关服务时，从不叠加 `infra/midterm-storage.override.yml`。** 函数和行号以 `98d388c` 的 `scripts/runtime/run_midterm_pressure60.py` 为准：
   - `start_rolling_cache_sinks_for_pressure`（4671 行）：只有 tmpfs 模式会额外叠加卷。
   - `configure_media_worker_rolling_cache`（4608 行）：非 tmpfs 模式下，override 里只有环境变量。
   - `restore_rolling_cache_sinks_after_pressure`（4932 行）：同样不带。
   - 只有 `restore_worker_cpu_isolation`（4303 行）会加它，判断条件只是"compose 同目录下有这个文件"。
   - **后果：** 缓存落到基础 compose 的 `/data/video-analytics/media:/media`，而不是 override 的 `${ROLLING_CACHE_HOST_ROOT:-/home/user/video-analytics-fast/rolling-cache}`。
   - **盘的类型：** `docs/code_review/evidence_pressure40_5s5s_fullvisual_2026-07-12.md` 记载 `/data` 是 2 TB 机械盘，滚动缓存应在 `/home` 系统盘。
3. **脚本只解析挂载源用于报告，不做门控。** `resolve_container_bind_source`（12431 行）会解析挂载源，但只用于填报告，不检查磁盘类型，也不在不对时中止。重建后只回读调优用的环境变量，这些变量在 NVMe 和机械盘上完全一样。
4. **`--pressure-rolling-cache-host-root` 指不到 NVMe 目录。** 它只接受 `/dev/shm/video-analytics-pressure/<run_id>/` 下的路径，而且必须和 materialized 参数成对给出（`_pressure_cache_tmpfs_base`，12276 行）。
5. **收尾恢复的顺序可能让两端落在不同的盘上。**
   - 顺序是：先恢复 media-worker（2278 行，不带 override），再恢复 sink（2285 行，不带），最后 `restore_worker_cpu_isolation`（2319 行）。
   - 最后这一步在原 cpuset 为空时，会带着 storage override 重建 media-worker。
   - 结果可能是：sink 写 `/data`，media-worker 读 NVMe，视频证据全部找不到素材。
6. **以前踩过同一个坑。** 10-10 冒烟测试因同样的遗漏，缓存落到 `/data`，发布排队约 100 秒，那轮已作废。该轮靠把挂载写进测试 compose 才恢复正常（`docs/code_review/pressure60_main_uos157_2026-10-10.md` 139–146 行）。
7. **切片发布是串行的，积压时不报警。**
   - 每个切片 4 次 fsync（`services/rolling-cache-sink/app/publishing.py` 576–600 行）。
   - 每个 sink 进程只有 1 个发布线程，最多 128 个待发布，提交时无超时阻塞（1000–1010 行）。
   - 阻塞发生在所有管线共用的 GLib 总线线程上，积压会在内存里无限增长。
   - `/readyz` 不看积压，一直显示就绪。
8. **正式部署的 sink 是 `rolling-cache-sink-a/-b`。** 它们属于 profile `operator-dual-runtime`，由 `scripts/runtime/precreate_operator_dual_runtime.sh` 预建。它们和 media-worker 的运行时环境变量，由 8090 通过 Docker API 克隆容器后改写（`services/api/app/services/runtime_topology.py` 约 1000、1865–1895 行）。
   - **后果：** 直接用 compose `--force-recreate` 会把这些环境变量还原成 `midterm.env` 的默认值，比如 media-worker 的 `ROLLING_CACHE_ENABLED=false`。
9. **代码挂载方式不同。**
   - media-worker、rolling-cache-sink、event-worker、Savant module 的代码，按 compose 文件所在 checkout 的相对路径 bind mount。在哪个目录执行 compose，正式服务就会挂上哪个目录的代码。
   - analysis-forwarder 的代码打包在镜像里（`COPY app`）。`replay-raw-fanout*` 也用同一个镜像。

### 推断（尚未核实）

- 10-11、10-12 能通过，是因为跑在手工改过挂载的测试 compose 上。只有 10-10 报告明确写了这一点；10-11 只写"保持原 NVMe 挂载"；10-12 完全没记录。
- 10-13 那轮缓存落在了机械盘上。

### 待核实（阶段 0 负责）

以下数字都来自服务器上的 10-13 产物，仓库里没有对应报告：

- 2,876 条过期、1,305 张图片成功、0 条视频；
- 12,015 个切片、发布排队约 102 秒、可见延迟中位约 1,187 秒；
- 剪辑中位 322 毫秒；
- 分析队列两个分片都顶在 8192。

阶段 0 要逐项注明来源字段，并标出"已确认"还是"推翻"。

### 已推翻，不要再用作证据

- **"只扫到 12,015 个切片，应有约 54,000"。**
  - 这是测试结束时按保留期留下的目录快照。保留 600 秒的几轮，测得的数量是 6,900–10,350。
  - 压测在不传 `--pressure-rolling-cache-retention-s` 时，保留期默认是 `max(900, 时长 + drain + 120)`，不是 600 秒。
  - 不同轮次之间不能直接比较这个数。
- **"加 remux worker 能改善"。** 剪辑只能用已经发布的切片，发布跟不上时加 remux 没有意义。

## 1. 全局规则（每个任务都适用）

1. **不合入 main。** 分支流转如下：
   - 阶段 0：从本任务书所在分支 `claude/rolling-cache-taskbook-20261014` 的头（基于 `98d388c`）拉 `CZray/pressure60-allalg-review-20261014`，放文档。
   - 阶段 1：基于阶段 0 分支的头继续开发。
   - 阶段 2：固定使用审查通过的 **commit SHA**，不用分支名。
2. **正式 checkout 只读。**
   - 正式部署目录是 `/home/user/video-analytics`，以容器标签 `com.docker.compose.project.working_dir` 为准。在里面禁止 `git checkout`、禁止改文件。
   - 开发和压测都在独立 worktree 里进行。
   - 正式容器的恢复，只能用正式 checkout 的 compose（或 10-10 留下的回退 compose），不能用测试 worktree 的 compose。
3. **不动正式常驻服务。** 只读检查除外。需要改正式环境的事（包括 T0.1 的修复和阶段 2 的每一轮），先把证据和计划报给用户，用户确认后再做。
4. **不删数据。** 不 `down -v`，不删数据卷和既有证据。`/data` 上遗留的滚动缓存目录只记录路径和大小，交给用户决定。
5. **快照与对比。** 凡是会重建容器的操作，都要做前后快照：
   - **前：** 记录正式 checkout 的 `git rev-parse HEAD` 和 `git status`。对每个 `video-analytics-midterm-*` 容器，记录 `docker inspect` 中的 Image、Config.Env、Mounts、HostConfig.CpusetCpus、State、RestartCount，以及 compose 标签 working_dir 和 config_files。
   - **后：** 逐项对比。除事先列明并获批的差异外，任何差异都是停止条件。
6. **一次只改一个变量。** 除本轮要测的那一项，其余参数与上一轮完全一致，差异逐项写进报告。
7. **测试环境不能连正式库。**
   - 运行 harness 前执行 `env -u DATABASE_URL -u VIDEO_ANALYTICS_DATABASE_URL`，或把所有测试库地址指向一次性的 pgvector 容器。
   - 原因：有些测试在检测到 `DATABASE_URL` 时会执行 DELETE。
8. **不重建镜像。** 阶段 1 只改 `scripts/runtime/` 和 `harness/`，不重建任何镜像，也不重建任何服务。如果确需重建 forwarder 镜像，必须先获批，并先 `docker tag ...:latest ...:pre-<日期>` 留出回滚点。
9. **文档按 CLAUDE.md 的开发期文档规则写。** 写清现象、影响、根因、非目标、开关、回滚点、风险和证据，区分"已确认 / 推断 / 未核实"，提交后补写 commit hash 和验证时间窗口。
10. **时间。**
    - 报告统一用首尔时间（KST，UTC+9）展示，原始 UTC 字段原样保留。
    - 服务器日志和 sysstat 用 `timedatectl` 显示的本地时区（以往报告是 Asia/Shanghai，UTC+8），查询时要换算。
11. **遇到停止条件（第 6 节）立即停下汇报，不要自行绕过。**

## 2. 阶段 0：只读核查（不改代码，不改服务）

### T0.1 现在的正式服务挂在哪块盘

对 `rolling-cache-sink-a`、`rolling-cache-sink-b`、`rolling-cache-sink`、`media-worker` 逐个执行。容器不存在时要显式打印 `missing`，不能用 `2>/dev/null` 吞掉。

```bash
for c in rolling-cache-sink-a rolling-cache-sink-b rolling-cache-sink media-worker; do
  n=video-analytics-midterm-$c
  if ! docker inspect "$n" >/dev/null 2>&1; then echo "$n missing"; continue; fi
  docker inspect -f '{{.Name}} status={{.State.Status}} wd={{index .Config.Labels "com.docker.compose.project.working_dir"}} files={{index .Config.Labels "com.docker.compose.project.config_files"}}' "$n"
  docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' "$n" | grep -E 'rolling-cache|/media( |$)'
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$n" | grep -E '^ROLLING_CACHE_(ENABLED|MATERIALIZATION_ENABLED|SOURCES|RUNTIME_EPOCH_ID)='
done
```

对每个挂载源路径，先用 `realpath -e` 确认路径存在（不存在就取最近的已存在父目录，并打印返回码），然后：

```bash
findmnt -T <路径> -o TARGET,SOURCE,FSTYPE; echo rc=$?
lsblk -no NAME,ROTA,TRAN,MODEL /dev/<设备>   # TRAN=nvme 可与 ROTA=0 互证
df -h <路径>
```

**交付：** 一张表，每个容器一行。

| 列 | 说明 |
|---|---|
| 容器 | |
| 状态 | |
| 是否在用 | `ROLLING_CACHE_ENABLED` 等 |
| `/media/rolling-cache` 源路径 | |
| `/media/rolling-cache-materialized` 源路径 | |
| 设备 / ROTA / TRAN | |
| compose 工作目录 | |
| 紧急度 | |

**判断与处理：**

- **sink 和 media-worker 的缓存源路径不一致：** 停止条件。说明证据功能实际已坏，立即报告。
- **在用的服务在 `/data`：** 报告用户，同时附上恢复方案，由用户选择，选定前不执行。
  - **方案 A（推荐）：** 在正式 checkout 中，先停掉 8090 的完整运行时。再用 `midterm_start.sh` 同等参数（含 storage override 和 operator override、`--profile operator-dual-runtime`），在干净的 shell 环境里重建这三个服务。重建前执行 `env | grep -E '^(ROLLING_CACHE|PRESSURE_|MEDIA_WORKER)'` 确认没有残留变量。重建后从 8090 重新应用运行时配置。
  - **方案 B：** 在正式 checkout 中，用 storage override、operator override，加一个从快照生成的环境变量 override（保留 `ROLLING_CACHE_*`、`RUNTIME_EPOCH_ID`、`MEDIA_WORKER_*`），带 `--profile operator-dual-runtime` 执行 `up -d --no-build --no-deps --force-recreate rolling-cache-sink-a rolling-cache-sink-b media-worker`。处于停止状态的预建容器，改用 `up --no-start --no-deps --force-recreate --no-build`。
  - **两种方案都要：**
    - 执行前用同样的参数跑 `docker compose ... config --format json`，只比对目标服务的 volumes；
    - 执行后对比快照，只允许滚动缓存的两个挂载源发生变化；
    - 写明切换会丢掉最近约 600 秒的缓存录像，因此要选低峰时段。
- **只是停着、没在用的服务在 `/data`：** 记录为"配置漂移，不紧急"，随报告提交。

### T0.2 复盘 10-13 那轮，不重跑

```bash
ART=/data/video-analytics/artifacts/pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2
ART_1012=/data/video-analytics/artifacts/pressure60_remux_refill_t4_4fps_1h_20261011_r2
```

10-13 的容器已经重建，`docker logs`、`/metrics` 和当前环境变量都不是那一轮的。**一律从 `$ART` 取数。** 可用文件包括：

- `report.json`、`run_config.json`、`pressure_diagnostics.json`
- `*.override.yml`
- `rolling_cache_sink_{a,b}_logs_since_start.txt`
- `media_worker_logs_since_start.txt`
- `replay_raw_fanout*_logs_since_start.txt`

如需读活容器的 metrics，用 `docker exec <容器> python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode())"`，**不要发布端口**。

逐项回答，并附原始输出：

1. **缓存位置。**
   - `report.json` 中 `rolling_cache_segment_visibility.root` 只有同时满足以下三条才算证据：
     - `segments_measured > 0`；
     - `run_config.json` 的 `pressure_rolling_cache_host_root` 为空；
     - 当时没有设置 `PRESSURE_ROLLING_CACHE_ROOT_HOST`。

     原因：解析失败时，这个字段会回落到硬编码的 `/data/...`。
   - 对该路径跑 `findmnt` 和 `lsblk`，确定磁盘类型。
   - **用了哪份 compose：** 只能推断。依据是 `run_config.json` 的 `compose_file`（打开文件看有没有写死滚动缓存挂载），以及 `$ART` 里有没有 `compose.rolling-cache-pressure-storage.override.yml`（这个文件只在 tmpfs 模式下生成）。
   - 注意：脚本的日志不记录命令行，所以这一项结论标为"推断"。
2. **保留期。**
   - 对 10-13 和 10-12 各取三处：`run_config.json` 的 `pressure_rolling_cache_retention_s`、`report.json` 的 `rolling_cache_sinks_pressure.rolling_cache_retention_seconds`、`observed_env.ROLLING_CACHE_RETENTION_SECONDS`。
   - 解释 10-12 的 41,161 个切片从何而来。它来自 `harness_rolling_cache_full_rate_gate.segments_measured`，而 10-12 报告写的是保留 600 秒，两者矛盾。
3. **发布耗时。**
   - **分布：** 取 `report.json` → `diagnostics.log_summary.rolling_cache_sink_a|rolling_cache_sink_b.rolling_cache_<字段>.{p50,p95}`，或从 `pressure_diagnostics.json` 取。字段包括：
     - `publication_queue_residence_ms`
     - `publication_capacity_wait_ms`
     - `publish_total_ms`
     - `publish_metadata_fsync_ms`
     - `publish_manifest_fsync_ms`
     - `publish_staging_dir_fsync_ms`
     - `publish_parent_dir_fsync_ms`
   - 确认"约 102 秒"对应的是哪个字段。
   - **随时间的变化：** 用 sink 日志里的 `segment published` 行计算整小时：日志时间减 `last_pts` 得到可见延迟，再加上 `publication_queue_residence_ms`，按 30 秒分桶。线性增长说明是容量不足，阶跃说明是某个事件触发。
   - `rolling_cache_segment_visibility` 只覆盖最后保留的那一段，只能作交叉校验，要注明它覆盖的时间窗。
4. **过期任务拆分。**
   - 背景：任务一过期，`materialization_retry_reason` 就被清成 NULL，最后一次重试原因只留在 `materialization_audit->'lifecycle_v2_retry'` 里（`materialization_repository.py` 1898–1912、437–463 行）。
   - 先确认这些行还在：查看 `run_config.json` 的 `discard_pressure_results` 和 `report.json` 的清理记录，并与 `db_summary_before_cleanup` 核对总数。

   ```sql
   SELECT COALESCE(task_type,'') AS tt,
          materialization_status,
          materialization_expired_reason,
          COALESCE(materialization_retry_reason,
                   materialization_audit->'lifecycle_v2_retry'->>'reason_code',
                   '<never_retried>') AS last_reason,
          (materialization_attempt_count = 0) AS never_attempted,
          count(*),
          percentile_cont(0.5) WITHIN GROUP (ORDER BY materialization_attempt_count) AS attempts_p50
   FROM evidence_tasks
   WHERE source_id LIKE 'pressure60_allalg_rr157_uos157_1h_20261013_qwen27b_face4_remux2\_%'
   GROUP BY 1,2,3,4,5 ORDER BY 6 DESC;
   ```

   - 每类原因各抽样 10 条 `materialization_audit->'lifecycle_v2_retry'->>'detail'` 原文。
   - 再按 5 分钟分桶（按 `materialization_ready_at`）统计各类任务的成功和过期数。重点看：图片成功是否集中在前期，视频是否完全没有成功。
5. **剪辑耗时样本。**
   - 统计 `media_worker_logs_since_start.txt` 中匹配 `media_event_finalized .* remux_ms=[0-9]+` 的条数和时间分布。注意：每行都带 `remux_ms=`，没有剪辑时是 `None`，不能只按字段名计数。
   - 也可以直接取 `report.json` 的 `diagnostics.log_summary.media_worker.media_remux_ms`。
6. **分析侧实际配置。**
   - **看哪两个容器：** 直连滚动缓存模式（`--rolling-cache-evidence` 加 `--dual-shard-same-gpu`）下，分析采样跑在 `video-analytics-midterm-replay-raw-fanout-a/-b`（metrics 端口 18185 / 18186），**不在** analysis-forwarder-a/-b。后两者此时是停用的，读它们的环境变量会得到误导性的默认值。
   - **读哪些值：** 从 `$ART` 的 override 文件（如 `compose.dual-shard-same-gpu.override.yml`）和报告中，读 `FORWARDER_MAX_QUEUE_AGE_MS`、`FORWARDER_STRICT_FPS_BUDGET`、`FORWARDER_QUEUE_MAX_SIZE`。
   - **镜像里的代码：** 只回读环境变量不够，要确认镜像里的代码真的认这些变量。
     - 执行 `docker run --rm --entrypoint sha256sum <镜像> /app/app/main.py /app/app/queueing.py /app/app/sampler.py`，与 `git show 98d388c:services/analysis-forwarder/app/<文件> | sha256sum` 比对；
     - 或者确认 `$ART` 中的 forwarder metrics 有 `va_forwarder_frames_stale_dropped_total` 和 `va_forwarder_frames_gop_resync_dropped_total`。
     - 如果报告里记录了 10-13 当时的镜像 ID，以它为准；当前镜像可能已经不同。
   - **结论：** 两个分片都顶在 8192，是否因为队龄上限没生效。
7. **算法与人脸间隔的实际值。**
   - 读 Savant A/B 的 `FACE_INFER_INTERVAL` 和 `FACE_EMBEDDING_INFER_INTERVAL`。如果开启了 AdaFace ROI 模式，注明 embedding 间隔不适用。
   - 摔倒、聚集、追逐是怎么开启的：脚本只创建 intrusion 和 face.watchlist 规则。查 `camera_rules` 中压测摄像头当前启用的规则，以及操作记录。
8. **磁盘层证据。**
   - 用 `report.json` 的正式窗口 UTC 起止时间，换算到服务器时区。
   - 命令：`sar -d -p -f /var/log/sysstat/sa13 -s HH:MM:SS -e HH:MM:SS`，取 tps、wkB/s、await、%util，对比 sda 和 nvme。
   - 没有记录就写"无"。

**交付：** `docs/code_review/pressure60_allalg_uos157_1h_2026-10-13.md`，放在阶段 0 分支。

- 每个数字注明来源字段，并标"已确认 / 推断 / 无法取得"。
- 把第 0 节"待核实"中的每个数字逐一核对。

### T0.3 决策门（用归一化原因码判断）

原因码定义在 `libs/evidence_lifecycle/contract.py` 75–77、231–261 行。下表中"原因"指 T0.2 第 4 项的 `last_reason`，"原文"指 `lifecycle_v2_retry` 的 `detail`。

| 视频任务过期的主要原因 | 下一步 |
|---|---|
| ① 素材缺失：`no_overlapping_final_segment`；或 `coverage_not_complete` 且原文含 `pre_gap_ns=`、`post_gap_ns=`、`window_not_covered`、`event_frame_not_covered`；或 `segment_not_stable`、`segment_disappeared`。同时缓存确在机械盘 | 进入阶段 1 |
| ② 视频专属问题：`coverage_not_complete` 且原文含 `internal_gap_ns=`、`requested_window_internal_gap`、`rolling_cache_output_duration_short`、`rolling_cache_output_frame_rate_low`；或 `materialization_failure_reason` 有值 | 进入阶段 1。另写一份独立的任务文档描述它，**本轮不实现** |
| ③ 大多数 `never_attempted` 为真，或 `capacity_unavailable` 占多数 | **停止汇报**，说明是调度或容量问题，不是缺素材 |
| ④ 缓存其实在 NVMe | **停止汇报**，机械盘假设不成立 |
| ⑤ 其他，或数据无法取得 | **停止汇报** |

## 3. 阶段 1：修压测脚本（先写测试，再改代码）

- **分支：** 基于阶段 0 分支的头。
- **测试位置：** `harness/tests/test_midterm_pressure60_script.py`，或新建测试文件。
- **范围：** 只改 `scripts/runtime/` 和 `harness/`，不重建镜像，不重建服务。

### T1.1 所有 compose 调用都带对存储 override

- **公共函数：** 新增一个，比如 `_compose_base_args(cfg)`，返回 `--env-file <env> -f <compose>`，并在 `<compose 同目录>/midterm-storage.override.yml` 存在时追加它。
  - 规则就是"同目录下有这个文件就加"，与 `restore_worker_cpu_isolation` 一致。
  - 把解析后的 compose 和 override 绝对路径写进报告。
- **审计全部 compose 调用：** 以下调用点统一改走这个函数，并在文档里逐处列出处理方式：
  - 4671、4608、4932 行的三个函数；
  - 2914、2934、2978、3003、3071、3127、4908、5396、5553、12855 行附近的调用。
- **所有 `up` 都加 `--no-build`。** 这是 CLAUDE.md 的规则：只有镜像需要更新时才 rebuild。
- **pressure 阶段：** tmpfs override（如果启用）排在 storage override 之后。
- **恢复路径：** 固定只用"基础 compose + storage override"，**绝不叠加 tmpfs 卷**。
  - 这是在修一个现有 bug：恢复时，`configure_media_worker_rolling_cache` 只要看到原环境里 `ROLLING_CACHE_ENABLED=true`，就会叠加本轮的 `/dev/shm` 卷。随后这个目录被清理，正式 media-worker 就挂到了一个已删除的目录上。
- **日志：** `run()` 在日志第一行写入完整命令行。
- **测试：**
  - 每条 compose 命令都包含 storage override；
  - pressure 阶段 tmpfs 排在 storage override 之后；
  - 恢复路径不含 tmpfs；
  - 日志首行是命令行。

### T1.2 盘位预检（开跑前）与重建后回读

- **纯预检模式：** 新增 `--preflight-only` 参数，只检查、不动任何服务。
  - 用和重建时完全相同的 `-f` 参数与环境，跑 `docker compose ... config --format json`，解析 sink 和 media-worker 在 `/media/rolling-cache`、`/media/rolling-cache-materialized` 上的源路径。
  - 正式流程中，这一步要放在禁用摄像头之前。`insert_pressure_cameras` 在 1789 行，它会禁用全部摄像头。
- **重建后回读：** 复用 `resolve_container_bind_source`（12431 行），确认 sink 和 media-worker 的源路径一致。
- **判断磁盘类型：** 必须在辅助容器里也能工作。宿主是 Python 3.7，脚本实际在辅助容器里运行，`findmnt` / `lsblk` 看到的是容器自己的挂载命名空间，不可靠。做法：
  1. 用 `os.stat(path).st_dev` 得到 major:minor；
  2. 读 `/sys/dev/block/M:m`：分区取父盘，device-mapper 设备遍历 `slaves/`；
  3. 任一叶子设备的 `queue/rotational` 为 1，就判为机械盘；
  4. tmpfs 放行，并记录；
  5. 解析不出来时**按失败处理**并中止。
- **中止方式：** 发现机械盘就中止，除非传了新参数 `--allow-rotational-rolling-cache`。中止要在 `try` 里抛异常，以触发现有的恢复路径。
- **空间检查：**
  - 保留期用 `pressure_rolling_cache_retention_seconds(cfg)` 的实际值；
  - 码率用输入文件的 ffprobe 结果，或上一轮实测的每路字节/秒；
  - 需求 = 保留期 × 路数 × 码率 × 1.3，加上 materialized 根目录的估算；
  - 要求跑完后 NVMe 剩余仍 ≥20%；
  - `/data` 证据盘和 `/dev/shm` 也分别检查。
- **写入报告：** `report.json` 新增 `rolling_cache_storage` 段，记录每个容器的源路径、设备链、rotational、文件系统类型、可用空间和预计占用。
- **测试：** 至少六种：
  - NVMe 放行；
  - 机械盘中止；
  - 带参数放行；
  - tmpfs 放行；
  - 无法解析时中止；
  - 中止后确实调用了恢复函数。

  另加一项：sink 和 media-worker 的源路径不一致时中止。

### T1.3 回读分析侧与 Savant 的实际配置

- **看哪两个容器：** 直连模式下读 `replay-raw-fanout-a/-b`，其他模式读 `analysis-forwarder-a/-b`。
- **回读内容：**
  - forwarder：`FORWARDER_MAX_QUEUE_AGE_MS`、`FORWARDER_STRICT_FPS_BUDGET`、`FORWARDER_QUEUE_MAX_SIZE`，以及镜像内三个代码文件的 sha256，并与当前 SHA 对应的代码比对；
  - Savant A/B：`FACE_INFER_INTERVAL`、`FACE_EMBEDDING_INFER_INTERVAL`。
- **中止规则：** 任一项与请求不符就中止。
- **写入报告：** 全部写入 `report.json`。

### T1.4 修正会误导的统计口径

1. **"队列满"按分片判断。** 判定标准改为该分片自己的 `FORWARDER_QUEUE_MAX_SIZE`，不再用"两个分片合计 ≥ 2048"（8935 行）。新旧两个值都写进报告，保留一个版本的可比性。
2. **保留丢帧计数。** `parse_forwarder_metrics_text`（15330 行）要保留 `va_forwarder_frames_stale_dropped_total`、`va_forwarder_frames_gop_resync_dropped_total` 和 `queue_head_age_ms`。
3. **切片统计配上保留期。** 改报"每路每分钟实际发布数"和"期望数"，并写明当时生效的保留秒数。

### T1.5 让全算法测试可复现，并清理残留规则

- **新参数：** `--behavior-rules fall,crowd_gathering,chasing`，由 `insert_pressure_cameras`（5796 行）创建这些规则。
  - 冷却统一用 `--pressure-algorithm-cooldown-s`（追逐在注册表里的默认是 30 秒，这里不能用它）；
  - 其他参数用 `services/api/app/algorithm_registry.py` 的默认值；
  - 不新增按事件类型的配额。
- **清理残留：** 压测摄像头的槽位会反复复用。不在请求列表里的规则，一律设为 `enabled=false`。这能清掉 10-13 手工开启后留下的规则，避免 A 轮在不知情的情况下跑成全算法。
- **开跑前回读：** 从数据库回读每个压测摄像头已启用的规则集，写入 `run_config.json`。和请求不一致就中止。

### 阶段 1 的验收

1. **新增测试全部通过。** 运行环境按全局规则 7，不连正式库。
2. **全量 harness 对比。** 基线 `98d388c` 和本分支在同一环境、用同一条命令运行，失败集合不能扩大。记录 passed / failed / skipped 数和失败用例名单。
3. **CLAUDE.md 最小验证集：**
   - `docker compose -f infra/docker-compose.midterm.yml config`；
   - `docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml -f infra/midterm-storage.override.yml config`；
   - `python -m pytest -q harness/tests/test_midterm_deployment_contract.py`；
   - `bash scripts/smoke/current/check_midterm_deployment.sh`；
   - `bash scripts/runtime/doctor_midterm.sh`（只读，在测试 worktree 中执行）；
   - `git diff --check`。
4. **用纯预检模式实测一次。** 在 uos157 上跑 `--preflight-only`，确认能正确识别 NVMe 和 `/data` 机械盘，并附上输出。这一步不动任何服务。
5. **文档：** `docs/midterm_pressure_storage_guard_<日期>.md`，链接 `specs/30_midterm_rolling_cache_evidence_plan.md`，并回写实现状态。
6. **推送，交 Opus 审查。** 审查通过前，不进入阶段 2。

## 4. 阶段 2：分步复测（每轮 60 路 × 4 FPS × 1 小时）

### 前置条件（全部满足才能开始）

- T0.1 发现的问题已解决，或用户明确接受现状。
- **每一轮都是用户单独批准的维护窗口。** 申请时写明：
  - 会停或重建哪些服务：全部摄像头被禁用、单路推理停止、workers / sinks / forwarders / Savant 重建；
  - 预计时长：约 2–2.5 小时，包括 1 小时负载、最长 1200 秒的启动等待、排空、8090 校验和恢复。
- 使用阶段 1 审查通过的 commit SHA。

### 每轮输入固定

A–D 四轮使用同一个文件输入，同一个 SHA256，同一个 offset，分别对应 `RTSP_REPUBLISH_INPUT_URI`、`RTSP_REPUBLISH_INPUT_SHA256`、`RTSP_REPUBLISH_INPUT_OFFSET_S`。这样各轮的事件密度可比。

### 轮次

| 轮次 | 相对上一轮只改一项 | 目的 |
|---|---|---|
| A | 参数照 10-12 的 `run_config.json` 生成（见下） | 证明 10-12 的通过能复现 |
| B | 加 `--behavior-rules fall,crowd_gathering,chasing` | 全算法的影响 |
| C | `--face-infer-interval` 3→4，embedding 间隔保持 A 的值不变 | 降人脸负载的收益 |
| D | `--media-worker-rolling-remux-workers` 1→2。**仅当** C 的"可剪→开剪"P95 比 A 高 30 秒以上，且调度统计显示 remux 通道 ≥90% 的采样处于占满状态 | 剪辑并发 |

**A 轮参数从 10-12 的 `$ART_1012/run_config.json` 生成，至少显式写出以下各项：**

- `--face-infer-interval 3`
- `--face-embedding-infer-interval <10-12 值>`
- `--media-worker-rolling-remux-workers 1`
- `--media-worker-materialization-max-active 4`
- `--media-worker-finalizer-workers 4`
- `--cpu-isolation-profile t4-16cpu-evidence`
- `--analysis-max-queue-age-ms 30000`
- `--pressure-algorithm-cooldown-s 60`
- `--pressure-rolling-cache-retention-s <T0.2 第 2 项确认的值>`

`--compose-file` 用仓库默认文件，不得使用手工改过的 compose。与 10-12 的差异逐项写进报告；预期的差异只有代码 SHA、compose 来源和 run-id。

### 每轮开跑前清单（任一项失败就不开跑）

- 辅助容器里 `docker compose version` 可用。
- `ss -ltn` 检查 6396、8090、8098、18080、18081、18554 端口没有冲突。
- `docker ps -a --filter name=pressure` 没有残留，压测用的 MPS 容器不存在。
- `nvidia-smi --query-compute-apps=pid --format=csv,noheader` 输出为空（推理服务已按计划停止）。
- 数据库迁移已到 033。
- 输入文件 SHA256 一致，RTSP 可达。
- `--preflight-only` 通过：缓存在 NVMe、空间足够、forwarder 与 Savant 配置一致、规则集一致。
- 做好全局规则 5 的"前"快照。

**基础设施类失败**（还没进入正式窗口就失败）：先做恢复核验，然后允许用 `_r2` 新编号重跑一次，旧产物保留。再失败就停止。

### 运行中看守

每 30 秒采集一次，写入 `$ART/watchdog.log`：

- `nvidia-smi --query-gpu=temperature.gpu,utilization.gpu --format=csv,noheader`
- `df --output=pcent /data /home/user/video-analytics-fast /dev/shm`
- savant-a/-b 的 `docker inspect -f '{{.RestartCount}} {{.State.StartedAt}}'`

**触发条件：** GPU 温度 > 85°C、任一盘使用率 > 90%、Savant 重启。

**触发后：** 用 `kill -INT <harness pid>` 让脚本自己走恢复流程，然后做快照对比和 T0.1 检查。**禁止 SIGKILL，禁止 `compose down`。**

### 每轮报告

报告表有三列：上一轮、本轮、10-12（参考）。

| 指标 | 上一轮 | 本轮 | 10-12 |
|---|---|---|---|
| 各事件类型数量与需求率（条/秒） | | | |
| 应保存 / 已保存 / ≤180 秒 / >180 秒 / 未生成 / 漏建任务 | | | |
| 总耗时 P50 / P95 / 最大 | | | |
| 事件→入库 P95；入库→登记 P95；可剪→开剪 P95 | | | |
| 切片发布：队列驻留 P95、publish_total P95、可见延迟 P95 | | | |
| 分析 FPS / 路；队列峰值（按分片）；stale + resync 丢帧占比 | | | |
| media-worker CPU 峰值；GPU 利用率 / 温度 | | | |
| 人脸图片就绪数；harness 失败项 | | | |

**每轮结束后：**

- 做"后"快照，与"前"快照对比。只允许事先获批的差异。
- 跑一遍 T0.1，确认 sink 和 media-worker 的缓存源路径一致，且在预期的盘上。
- 报告写入 `docs/code_review/`，每轮一份。

### 判定

- **A 通过的定义：**
  - 已保存数 = 应保存数，超过 180 秒 = 0，未生成 = 0，漏建任务 = 0；
  - harness 的失败项是 10-12 那 6 项的子集。出现新的失败项要单独列出，但不单独作为 A 不通过的理由。
- **A 不通过：** 停止汇报，说明 10-12 的通过不可复现。
- **B、C：** 按同样的取证标准判断。B 不通过时仍然执行 C，因为 C 本身就是降负载手段。C 之后停下汇报，由用户决定是否做 D。

## 5. 阶段 3：后续，本轮不做，只列出

1. **sink 积压可观测与就绪门（P1）。**
   - 新增三个量："分片关闭→进入发布队列"的等待时间、最老未发布分片的年龄、总线积压数。
   - 积压超过阈值时，让 `/readyz` 或 harness 门槛判失败。
   - 先写设计文档，再实现。
2. **降低每切片的持久化开销（P2）。**
   - 只有机械盘确实是部署目标时才做，比如改成只 rename 不 fsync，或者组提交。
   - 这会改变崩溃一致性契约（`services/rolling-cache-sink/tests/test_sink_contract.py` 约 232–246 行固定了 fsync 顺序），必须先写 spec。
   - 以前已经否决过的方案不要重试：加发布线程、commit slots、fdatasync、single_inode、metadata_only、父目录分组（见 `docs/current_mainline_status.md`）。
3. **分析侧约 3% 的处理缺口。** 单独做实验：
   - 队龄上限 30 秒→10 秒的对照；
   - 积压期间用 py-spy 对 Savant 采样 60 秒。
4. **图片取证的时钟域比较。** `_rolling_cache_frame_for_image_event` 用源时钟的帧 PTS 去比混流时钟的切片范围。这是潜在的正确性问题，不是 10-13 的原因。单独修，并补测试。
5. **T0.3 第 ② 类的视频专属问题。** 如果存在，按那份独立任务文档另行处理。

## 6. 停止条件汇总（遇到就停下，汇报用户）

- T0.1：sink 和 media-worker 的缓存源路径不一致；或在用的正式服务在 `/data` 上（报告并等待用户选择方案）。
- T0.3 命中第 ③、④、⑤ 行。
- 阶段 1：全量 harness 的失败集合比基线大；或纯预检的实测结果不对。
- 阶段 2：任一开跑前检查失败；基础设施失败重跑一次后仍失败；看守触发；A 不通过；快照对比出现未获批的差异。
- 任何一步需要：改正式服务配置、在正式 checkout 里切换分支、删数据、重建镜像、合入 main。

## 7. 每次汇报的格式

1. **一句话结论：** 通过、未通过还是停止，以及原因。
2. **做了什么：** 命令、分支、commit SHA、产物路径。
3. **关键数字：** 对比表（上一轮 / 本轮 / 10-12）。
4. **已确认 / 推断 / 未核实**：分开列。
5. **下一步建议：** 以及需要用户决定的事项。
