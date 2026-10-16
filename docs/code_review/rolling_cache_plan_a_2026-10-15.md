# 正式滚动缓存方案 A 执行单（待决策，不执行）

## 1. 结论与授权边界

**未通过执行前决策门。** 正式 checkout 的 storage override 可以把三个目标服务的两个缓存有效源路径统一迁到 NVMe；但任务书字面的方案 A（`midterm_start.sh` 后从 8090 重新应用已存运行时）无法满足用户本次限定的最终差异白名单。当前只读核查和本文编写已获授权，本文中的任何 POST、stop、up、脚本启动、远端文件生成均未执行。

白名单仅包括：

1. `rolling-cache-sink-a`、`rolling-cache-sink-b`、`media-worker` 的 `/media/rolling-cache`、`/media/rolling-cache-materialized` 有效宿主源分别从 `/data/video-analytics/media/rolling-cache{,-materialized}` 改为 `/home/user/video-analytics-fast/rolling-cache{,-materialized}`。
2. 去掉 `media-worker` 对 10-13 压测产物恢复 override 的依赖。新的 `config_files` 必须逐项列明并审批，不把任意替换依赖视作自动获批。

不把“停运行时”解释为批准改变最终 camera.enabled、源 ID、保留期、epoch、调优参数、镜像、CPU 绑定或其他服务配置。阶段 0 仍未结束，未进入阶段 1。

## 2. 只读证据与时间

证据目录：`docs/code_review/evidence/rolling_cache_phase0_20261015/plan/`。

- `plan_a_config_comparison.json`：可直接审查的卷、镜像、CPU 绑定、有效环境差异及 API 预检结果。
- `summarize_plan_capture.mjs`：从只读原始采集结果结构化生成上项；比较环境时合并当前镜像的默认 Env，避免把镜像内默认值误记为部署漂移。
- `readonly_plan_a_capture_images.json`：最后一份采集的脱敏审查副本；同名 `.aes256gcm` 保存完整原始采集，`.encryption.json` 保存认证标签和摘要。密钥和明文原件在工作树外保管，未提交明文凭据，已验证解密回读一致。
- `readonly_plan_a_capture_corrected.json`：前一轮成功的只读采集；`readonly_plan_a_capture.json` 是保留的采集器参数错误记录，显式 `--project-directory /home/user/video-analytics` 使相对 `env_file` 找错路径，已纠正。这次采集错误不是正式部署配置故障。
- `../snapshot/pre_repair_snapshot.json`：全项目修复前快照，root agent 保存。执行维护时还必须重采，不能用阶段 0 快照冒充执行前瞬时状态。

最后一份原始采集开始时间 **2026-10-15 19:02:27.615573 KST**；原始 UTC 为 `2026-10-15T10:02:27.615573+00:00`。Docker Compose 为 **v2.27.0**，来源 `commands.compose_version.stdout`。远端只读文件读取、`docker inspect`、`docker image inspect`、`docker compose config`、8090 HTTP GET 均通过 SSH stdin 执行，没有远端落脚本文件。

正式工作目录由容器标签确认是 `/home/user/video-analytics/infra`，对应项目根 `/home/user/video-analytics`。该目录无 `.git`，宿主 `git` 不可用，不能确认正式文件对应的 commit SHA。正式 `docker-compose.midterm.yml`、`midterm-storage.override.yml`、`runtime_topology.py` 的 SHA256 与本阶段 0 worktree 对应文件不同，因此本方案使用已采集的正式文件，不用 worktree 文件替代。

## 3. 执行前 Compose 卷与配置对比

已确认正确文件名是 `infra/operator-dual-runtime.override.yml`，不是 `infra/midterm-operator.override.yml`。正式 storage override 为两个 sink 和 media-worker 都定义了两个缓存子挂载。

以下是已实际执行且成功的只读配置渲染命令。`env -i` 明确清空环境；不用 `--project-directory` 改变 Compose 的相对路径基准。`HOME` 仅为清洁子进程指定真实用户目录，没有改宿主环境。

```bash
cd /home/user/video-analytics
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/home/user USER=user LANG=C TZ=Asia/Seoul docker compose --env-file /home/user/video-analytics/infra/env/midterm.env -f /home/user/video-analytics/infra/docker-compose.midterm.yml --profile operator-dual-runtime config --format json
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/home/user USER=user LANG=C TZ=Asia/Seoul docker compose --env-file /home/user/video-analytics/infra/env/midterm.env -f /home/user/video-analytics/infra/docker-compose.midterm.yml -f /home/user/video-analytics/infra/midterm-storage.override.yml -f /home/user/video-analytics/infra/operator-dual-runtime.override.yml --profile operator-dual-runtime config --format json
```

两条命令返回码均为 **0**，来源 `config_results.{base_clean,storage_operator_clean}.returncode`。只渲染配置，不创建、启动、重建容器；完整输出包含密码，审查时只显示结构化差异。

| 服务 | 当前缓存有效源 | 加 storage/operator 后缓存有效源 | 其余卷 | 同镜像有效 Env 差异 | 当前 / 渲染 CPU 绑定 |
|---|---|---|---|---:|---|
| sink-a | `/data/video-analytics/media/rolling-cache{,-materialized}` | `/home/user/video-analytics-fast/rolling-cache{,-materialized}` | 4 项保持一致 | 0 | 空 / 空 |
| sink-b | 同上 | 同上 | 4 项保持一致 | 0 | 空 / 空 |
| media-worker | 同上 | 同上 | 4 项保持一致 | 24 | 空 / 空 |

来源：`service_diffs.storage_operator_clean.<service>.{before_mounts,rendered_mounts,effective_env_difference_count,live_cpuset_cpus,rendered_cpuset_cpus}`。当前 `/media` 父挂载继续保留 `/data/video-analytics/media`；两个新增的精确子挂载覆盖其子路径，既有证据 `/media/evidence` 继续使用原盘。三个 `latest` 标签在采集时仍指向当前运行镜像 ID（`rendered_tag_points_to_current_image=true`），执行前必须重查；`--no-build` 不等于保证标签未漂移，最终比较使用 Image ID。

MW 的 **24** 项差异全部在 JSON 中逐项列出。关键例子如下，来源同上 `effective_env_differences`：

| 键 | 当前 | 干净 Compose 有效值 |
|---|---|---|
| `ROLLING_CACHE_ENABLED` / `MATERIALIZATION_ENABLED` | true / true | false / false |
| `ROLLING_CACHE_RETENTION_SECONDS` | 4020 | 300 |
| `ROLLING_CACHE_SOURCES` | 60 个 `pressure60_allalg_rr157_uos157_1h_fixed_20261013_00..59` | 空 |
| `ROLLING_CACHE_FALLBACK_TO_REPLAY` | false | true |
| `ROLLING_CACHE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS` | 9 | 1 |
| `MEDIA_WORKER_FINALIZER_WORKERS` | 4 | 32 |
| `MEDIA_WORKER_FINALIZER_PROCESS_WORKERS` | 4 | 0 |
| `MEDIA_WORKER_REMUX_QUEUE_CAPACITY` | 1 | 4 |

因此只用上述三个 Compose 文件重建 MW，会产生未获批的环境变化，决策门已经失败。不能把这些变化藏在“恢复生产默认配置”中。

## 4. 方案 A 的三个具体阻断点

### A1：启动入口不是三服务重建入口

正式 `scripts/midterm_start.sh` 与本 worktree 文件 SHA256 一致：`3372e2a672c98c8b0cd2ebb6e64e9dbf8080c7ff4deb49af5e74813ae9492711`。脚本 `start_services()`（434 行）执行无目标限制的 `compose up -d --no-build`。`build_compose_args()`（152 行）只加入 base/storage 和 profile，不在该全栈 up 中加入 operator override。随后 `precreate_operator_dual_runtime()` 调用正确的 operator override，为 **14 个**双运行时服务执行预建流程，还会准备目录、symlink 和 B 分支模型缓存。不能通过追加三个服务名来限缩，解析器会报 unknown option。

因此下面这条字面方案 A 命令虽明确 `--no-build`，仍超过“只改三个服务的两个缓存挂载”的白名单：

```bash
env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/home/user USER=user LANG=C TZ=Asia/Seoul bash scripts/midterm_start.sh --no-build --profile operator-dual-runtime
```

### A2：8090 “停止完整运行时”需要明确范围

已确认 API 入口（正式 `routers/runtime.py` 与本 worktree hash 相同）：

- `POST /api/v1/runtime/control/dual/stop`：停止动态源和双分支推理容器，并执行 SQL 把所有启用摄像头禁用；**明确留下 event-worker、media-worker、sink-a、sink-b 排空**（`runtime_control.py` 182–225 行）。
- `POST /api/v1/runtime/control/single/stop`：停止单分支列表，包含 media-worker，但不含两个 sink。
- 因此完整停止还需要显式停止两个 sink。不能声称只调用 dual stop 已停完目标服务。
- GET `/control` 的 `mode=single` 在实现中是常量，不能据此判断真实运行拓扑；本轮依据各容器的实际 State。双分支主要推理容器为 exited/created，两个 sink 和单分支组多项为 running。

“禁用摄像头，再恢复精确原 enabled 集合”是 API 的数据库变更，必须在将来维护授权中写明。当前所有源的启用集合为空，并不允许推定后续执行时仍为空。

### A3：已存运行时不能无差异重应用

GET `/api/v1/runtime/topology-config` 当前 `saved_config.runtime_profile=production_t4_40`、`pipeline_mode=full_evidence`、`topology_mode=dual_same_gpu`。其 `preflight.ok=false`，唯一失败的 fatal check 是 `profile_enabled_source_count`：**expected_source_count=40、source_count=0**。来源 `api.topology.preflight.checks`。

已存 preset 的计划值包括 MW max_active=10、remux=5、finalizer=5、retention=600、cpuset=`6-7,14-15`，以及 sink cpuset=`0-15`。正式 `runtime_topology.py` 的 `_apply_dual_topology` 会创建新 epoch、写 module/source/replay_shards 配置，并克隆重建多个服务；MW 环境由 `_prepare_full_pipeline` 相应代码设置（原始采集的正式源码 1830–1889 行），sink retention 也改成 600。它不会只修挂载。

当前不带 camera selection 的 apply，进入双拓扑预检前还有 cooldown 应用步骤；不能通过“试一次看能否 409”来当只读预检。禁止 `force=true` 绕过。即使先另行解决 0/40 预检，其配置、CPU、epoch 和多服务变化仍超出白名单。

## 5. 有顺序的命令清单与强制守卫

这是审查清单，不是已批准执行脚本。维护开始前必须先取得新授权，并把本节未通过的三个门改成已审查通过；按现有授权流程只能走到第 3 步的 STOP。

1. 只读固定身份、文件与环境；stdout 由本地采集器保存，不能在正式 checkout 创建输出文件。

```bash
cd /home/user/video-analytics
pwd
date -u +%Y-%m-%dT%H:%M:%SZ
TZ=Asia/Seoul date +%Y-%m-%dT%H:%M:%S%z
test ! -e /home/user/video-analytics/.git
command -v git
git -C /home/user/video-analytics rev-parse HEAD
git -C /home/user/video-analytics status --short
env | grep -E '^(ROLLING_CACHE|PRESSURE_|MEDIA_WORKER)'
sha256sum scripts/midterm_start.sh scripts/runtime/precreate_operator_dual_runtime.sh infra/docker-compose.midterm.yml infra/midterm-storage.override.yml infra/operator-dual-runtime.override.yml infra/env/midterm.env services/api/app/services/runtime_topology.py
```

`command -v git` 和 `grep` 可能返回非零，必须逐条记录，不把“无 Git”或“无残留变量”当作静默通过。上次 SSH 环境中匹配上述前缀的变量名数量为 **0**，来源 `ambient_matching_key_names=[]`；执行时还要复查。干净子 shell 仅显式传入已审查的必需变量，不从当前终端继承调优参数。

2. 重新捕获全项目完整 inspect 与 API 配置、camera enabled 集合；重复第 3 节两条 `config --format json` 和 T0.1，并计算与执行前 snapshot 的差异。

```bash
docker ps -a --filter name=video-analytics-midterm- --format '{{.ID}} {{.Names}}'
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/runtime/control
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/runtime/topology-config
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/runtime/topology-config/apply-status
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/runtime/overview
```

每个 `docker ps` 返回的容器均单独 `docker container inspect <ID>`，保持包括缺失容器、查询返回码、State、RestartCount、Image、完整 Env、Mounts、CpusetCpus、working_dir、config_files。全量密码数据按修复前快照同等规则加密，不落正式 checkout。

3. **STOP：本轮已知门失败。** 目标 Env 差异 24 项、全栈启动范围过大、8090 预检 0/40 且重应用超白名单。现状下必须在任何 POST/stop/up 前退出：

```bash
printf '%s\n' 'STOP: plan A is not approved and its preflight/allowed-difference gates fail.' >&2
exit 78
```

4. 供审查的后续停止顺序如下，**当前不可执行**。每条请求后核对响应中各 action.ok、重新 inspect；先排空存量任务，阻塞任务不为零就停止，不使用 force；重建 MW 前确认没有正在物化的任务。

```bash
curl --noproxy '*' -fsS -X POST http://127.0.0.1:8090/api/v1/runtime/control/dual/stop
curl --noproxy '*' -fsS http://127.0.0.1:8090/api/v1/runtime/overview
curl --noproxy '*' -fsS -X POST http://127.0.0.1:8090/api/v1/runtime/control/single/stop
docker stop --time 30 video-analytics-midterm-rolling-cache-sink-a video-analytics-midterm-rolling-cache-sink-b
docker container inspect video-analytics-midterm-rolling-cache-sink-a video-analytics-midterm-rolling-cache-sink-b video-analytics-midterm-media-worker
```

5. 第 4 节的字面 `midterm_start.sh` 命令目前不可批准为窄差异迁移。若用户决定坚持窄白名单，应另审**从快照固定完整 Env/Image/Cpuset 的三服务定向 Compose 方案（属于任务书方案 B）**；它还需要外置、持久、已审查的环境 override，不能偷偷复用 10-13 产物文件。未生成、未上传这种 override，也没有把 B 冒充 A 执行。

6. 若用户另行批准一个能解决 A1–A3 的扩大版方案 A，8090 重新应用入口已确认如下；payload、camera 集合及所有新增差异必须在修改后的执行单中固定。目前没有可满足本白名单的 payload，所以不编造或执行。

```bash
curl --noproxy '*' -fsS -X POST http://127.0.0.1:8090/api/v1/runtime/topology-config/apply
```

不使用 PUT 改 saved_config、不启用任意压测摄像头来凑 40 路、不传 force，不直接运行 `midterm_start.sh` 全栈来试。

## 6. 回滚审查点

本轮没有执行服务变更，因此当前无须回滚。维护前必须备齐最终获批变体的完整回滚产物，并先对回滚执行 `compose config`；**不能把基础 Compose 直接 up 当作恢复当前环境**，因为同样会改变 MW 的 24 项 Env。

如将来批准三服务定向变体，准确回滚流程为：

1. 保持摄像头和推理停止；停止三个目标，避免切换期间两端读写不同根目录。
2. 从维护前完整快照生成外置 rollback override，固定三个服务原始完整 Env、精确 Image ID、Cpuset、其他 HostConfig 和原挂载；配置渲染必须与 snapshot 比对为零差异。原挂载只有 `/data/video-analytics/media:/media` 父挂载，不可残留两个 NVMe 子挂载。override 的存储路径与 `config_files` 差异需预先获批，不能依赖 10-13 产物。
3. 在正式根目录，以基础 Compose + 经审查 rollback override（**不叠加 storage override**）定向执行 `up -d --no-build --no-deps --force-recreate rolling-cache-sink-a rolling-cache-sink-b media-worker`。原本 stopped 的目标按 snapshot 用 `up --no-start --no-build --no-deps --force-recreate`，不能全启动。
4. 重跑全量快照差异和 T0.1，所有目标应回到相同 `/data` 有效路径、原 Env/Image/Cpuset；按快照恢复其他服务 State 与精确摄像头启用集合。实际原始 API apply 不能用来恢复当前漂移快照。
5. 绝不删除 `/data` 或 NVMe 下任何缓存和证据，也不对原数据复制覆盖。回滚意味着先恢复原读写位置，并不保证旧缓存还覆盖所需时间窗；保留所有原数据给用户决定。

上述第 2 步的实际 rollback override 尚不存在，因此还没有可批准的无占位回滚命令。若选择字面全栈 A，其影响超过三个服务，必须另生成所有受影响服务的恢复配置；只恢复三个目标不能声称完成回滚。这是执行前阻断项，不能留到中断后补。

## 7. 中断、低峰与执行后验证

**推断，未经实测：** 仅三服务定向修复可预留约 **30 分钟**维护窗口，其中正常切换及健康检查约 **5–15 分钟**，其余用于回滚；正式启动脚本的健康等待预算为 **240 秒**（源码 `WAIT_SECONDS=240`），不包含模型载入或业务可用性，不能据此承诺中断上限。字面全栈 A 当前阻断，无法给出可信的完成时间。

**建议，未经流量验证：** 由用户选择 **02:00–04:00 KST** 的低峰窗口；不是已批准日程，也未证实这一时段业务量最少。当前启用源为 0，不能外推未来窗口无业务。

任务书的“最近约 600 秒缓存录像”是方案说明中的估计，不是这次现场确认的丢失量。实际 sink 配置保留期为 300 秒，MW 为 4020 秒，已存 preset 为 600 秒，三者不同。切换不删除 `/data` 数据，但新 NVMe 根目录不会自动包含旧目录的录像，须在重新接流后等待事件前后窗覆盖和分片发布稳定。

获批执行后必须按顺序验收，任何未获批配置差异立即停止并按已获批回滚处理：

1. 对全部 `video-analytics-midterm-*` 重新做完整快照。比较完整 Image ID、Env、Mounts、Cpuset、working_dir、config_files。配置白名单仅第 1 节两项；容器重建自然产生的 ID、Created/StartedAt、RestartCount 重置、health 状态转换、Compose config-hash/container-number 等元数据应单列并在维护审批时明确，不能把这些正常生命周期字段误当 Env 配置许可，也不能因此忽略重启异常。非目标服务不应重建。
2. 重跑 T0.1：每服务解析最深覆盖挂载，对两个缓存目录执行 `realpath -e`、`findmnt -T`、`lsblk` 沿 dm/LVM 找叶子设备、`df -h`；sink-a/b 与 MW 两端有效路径完全相同、NVMe 非旋转、原证据父挂载不变。
3. 采集两个 sink 后续 30 分钟 `segment published`，按和修复前相同解析和 P50/P95 分位算法计算 `publication_queue_residence_ms`、`publish_total_ms`，另做时间分桶判断是否增长；必须有真实发布样本。只有 ready/healthy 而没有样本不能判通过。暂无业务发布时记“未核实”，不制造压测流量。
4. 在用户另行确认的真实摄像头/自然新事件上，查询新 evidence_tasks 的 source_id、task_type、materialization_status、时间戳与证据路径，确认图片和视频按所需类型生成且可访问；必要时只读检查文件/HTTP GET。当前没有启用源，因此现状下无法完成这一业务验证，不能把空任务集写作通过。启用摄像头或主动制造测试事件需要纳入维护授权。
5. 将源 ID、保留期、epoch、MW 并发、CPU 绑定和其他正式服务配置变化逐项对照白名单；本方案不顺带清理历史压测环境值。其业务合理性需另行决策。
6. 路线二的已批准漂移必须写入维护窗口结果：`savant-a`、`savant-b`、`replay-raw-fanout-a`、`replay-raw-fanout-b` 和 `adaface-roi-worker` 在 A 轮结束后只停止，不重建；它们会保持快照中允许的压测配置，并在报告中标为 `approved_known_drift`。这些 exited 容器不得直接执行 `docker start`，否则会再次运行压测配置；后续需要正式 Compose（以及届时获批的 operator/storage override）重建并做完整差异核验。该事项不等于本轮清理 10-13 残留，也不允许删除数据或镜像。

## 8. 已确认 / 推断 / 未核实与下一步

**已确认：** 正式 storage/operator 配置可以得到一致 NVMe 卷；目标卷之外的挂载保持一致；简单干净重建会改变 MW 24 项有效 Env；字面启动脚本范围超过三服务；8090 已存配置预检 0/40 失败，且成功应用时会改多个服务、epoch、CPU 与环境。

**推断：** 通过保留当前完整环境的定向三服务方案可以实现窄配置白名单，但这属于任务书方案 B，需先生成并审查实际 override、完整差异、回滚文件及验证方式。

**未核实：** 任何修复后的行为、实际中断时长、低峰流量、迁移后新事件取证、字面 A 的全部最终差异及全栈精确回滚。本文不是修复完成报告。

**下一步建议：** 用户先决定保留窄白名单并改为准备 B，或单独批准明确列出的 A 扩大差异。当前继续保持正式服务、正式 checkout 和所有缓存目录不变；不进入阶段 1。
