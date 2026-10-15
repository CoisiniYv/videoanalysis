# 阶段 1 审查：压测脚本存储门控（Opus，2026-10-16 KST）

| 项目 | 内容 |
|---|---|
| 被审分支 | `codex/rolling-cache-storage-guard-20261015` |
| HEAD | `ba76375` |
| 实现提交 | `fc77792` |
| 基线 | `98d388c` |
| 审查方式 | 5 个维度并行审读，每个严重问题由独立复核者尝试推翻；另有一次独立全量 harness |

## 结论：不通过，暂不进入阶段 2

存储门控本身做得好：盘型识别、预检契约、tmpfs 隔离，以及恢复不再叠加 tmpfs，都经过复核确认无误。

问题出在**压测前后对正式环境的改动与恢复**：

- 有 **2 个阻断问题**，阶段 2 每一轮都会命中。
- 有 **5 个主要问题**，A 轮前需修复，或至少写明不适用的理由。

修完后按第 5 节重新交审。

## 1. 独立复核结果

在不连数据库的环境中运行全量 harness（`env -u DATABASE_URL -u VIDEO_ANALYTICS_DATABASE_URL`）：

| 分支 | failed | passed | skipped | error |
|---|---:|---:|---:|---:|
| `98d388c` | 12 | 1,779 | 73 | 7 |
| `ba76375` | 9 | 1,955 | 73 | 7 |

- 新增失败为 0。
- 3 项基线失败（sink 恢复相关的 mock 用例）转为通过。
- 名单见 `evidence/rolling_cache_stage1_review_20261016/`。

与 Codex 报告的数字不同，是因为两边的 Python 依赖环境不同；"失败集合是基线子集"这一结论一致。

**已确认无误的部分：**

- **compose 调用：** 全部经过 `compose_invocation_args`，每次 `up` 都带 `--no-build`。
- **profile：** 双路模式下 `dual-4090-two-source` 与 `rolling-cache-dual` 能正确渲染出 sink-a/b。
- **恢复不叠加 tmpfs：** 所有恢复路径都使用 `restore_cfg`，不会叠加 tmpfs。
- **盘型识别：**
  - 用合成的 sysfs 验证过：分区能回到所属整盘，dm/LVM 能沿 slaves 递归；
  - btrfs、zfs、overlay、NFS 这类匿名设备号（主设备号 0）无法解析时会拒绝；
  - `--allow-rotational-rolling-cache` 不能放行无法解析的设备。
- **纯预检：** `--preflight-only` 在连接数据库和 Redis、改动摄像头、执行 compose up 之前就返回；只写产物目录。
- **两份异常类问题：** 已修复，两个模块都从 `scripts.runtime.pressure_storage_guard` 导入。
- **参数推导：** A 轮按 10-12 参数时，`_prepare_pressure_run` 推导出的配置与基线一致。
- **调度参数：** `LOOKAHEAD=4` / `AGING=60` 与 compose 默认值一致，不会成为额外变量。
- **forwarder 回读：** 回读的容器选择正确；镜像中三个代码文件在 `98d388c..HEAD` 之间没有变化。
- **规则改动的作用域：** 禁用残留规则只作用于压测槽位（`site_id='pressure'`），不会碰正式摄像头；正式摄像头在三条退出路径上都能按快照恢复。

## 2. 阻断问题（阶段 2 每轮必中）

### B1 规则回读的 SQL 类型错误：每轮在准备摄像头这一步必然失败

- **位置：** `scripts/runtime/run_midterm_pressure60.py:6084`
  ```sql
  WHERE camera_id=ANY(%s::uuid[]) AND enabled=true
  ```
- **原因：** `camera_rules.camera_id` 是 TEXT 类型（migration 004；uos157 实库也是 text，正式摄像头 id 为 `cam_1080movie`）。PostgreSQL 会报 `operator does not exist: text = uuid`。
- **复现：** 在 PostgreSQL 16 上跑真实 migrations 已复现；把类型改成 `text[]` 后同一流程通过。
- **影响：**
  - 这条回读每轮都会执行，包括不带 `--behavior-rules` 的 A 轮。
  - 执行到这里时，正式的单路 Savant 已经停掉，event-worker、media-worker 和 sink 已经重建，CPU 隔离已经生效。
  - 所以每个获批的维护窗口都会以 `failed_exception` 结束，并走一遍恢复流程。
- **修法：**
  - 改为 `ANY(%s::text[])`，或与 `services/api/app/repositories/cameras.py` 一样直接传 Python 字符串列表。
  - 增加一个基于 `db/migrations` 建库、真正执行 `insert_pressure_cameras` 的测试（用临时 PostgreSQL，约 2 秒）。现有测试全是 mock，所以没发现这个问题。

### B2 operator 双路容器在每轮结束后留在压测配置

- **位置：**
  - 2779–2781 行：结尾的 operator 预建被改成 `skipped`，没有替代步骤。
  - `start_dual_shard_runtime`（3226–3269 行）：重建这些容器。
  - `restore_runtime`（13431–13440 行）：只 stop，不恢复。
- **机制：**
  - 每轮 `savant-a/b`、`replay-raw-fanout-a/b`、`adaface-roi-worker` 都会被重建，使用的是压测 checkout 的 compose 加上产物目录里的 dual override：
    - `SAVANT_MODULE_FILE=<ART>/module.pressure.yml`（ROI 模式下去掉了 adaface、face_reid_gate 和 face_observation_exporter）；
    - 本轮专用的 ROI stream 和 consumer group、MPS 管道、ipc host、cpuset、`restart: no`；
    - 代码挂载和 compose 标签都指向压测 checkout。
  - 恢复时这些容器只被停止。
- **后果：**
  1. 每轮前后快照都会出现未获批的差异，这本身就是停止条件。
  2. 8090 下次启动双路时，会按 `docker inspect` 克隆这些容器（`runtime_performance._create_body_from_inspect`），而且不会改写 `SAVANT_MODULE_FILE` 和代码挂载。正式环境会跑压测裁剪过的模块和测试分支的代码；一旦产物目录或 worktree 被删除，就会启动失败。
- **现场证据：** preflight-r1 显示这 5 个容器现在仍带着 10-13 产物目录的 dual override。
- **修法：**
  - 重建之前，对 `dual_shard_services(cfg)` 涉及的每个容器（包括 adaface-roi-worker 和 decoupled 模式的附加服务）做完整快照：Env、Image ID、挂载、标签、运行状态。
  - 恢复方式与 sink 相同：正式 base + `operator-dual-runtime.override.yml` + storage override + 快照环境变量 override，带 `--profile operator-dual-runtime`。原来是停止状态的容器用 `up --no-start --no-deps --no-build --force-recreate`。
  - 恢复后做双向比对，sink 不在此列（已有独立恢复）。
  - 如果想顺便清理 10-13 留下的残留、把它们规范化，那属于改变正式状态，需要用户另行批准。本轮只做精确恢复。

## 3. 主要问题

### M1 恢复步骤串成一条链，任一步失败就跳过推理服务的恢复

- **位置：** 2525、2641、2715 行。基线 `98d388c` 在 `restore_cameras` 之后立刻调用 `restore_runtime`。
- **现状：** `restore_runtime` 被挪到 clip / event / media / sink / topology 恢复全部完成之后。这些步骤共用一个 `try`，任一步抛异常，后面全部跳过，报告里也没有恢复错误记录。
- **复现：** 用桩化的 main（`evidence/.../probe_main.py`）已复现。
- **后果：**
  - savant-a/b 没有被停止，外层 `finally` 却把 MPS 停了；
  - savant-security 和 analysis-forwarder 保持停止状态；
  - 性能配置没有恢复。
- **修法：**
  - 每个恢复步骤单独 `try/except`，错误写入 `report["restore_errors"]` 后继续执行后续步骤。
  - 先做"停止双路压测运行时"。
  - 无论前面是否失败，都执行 `restore_runtime`。
  - 有任何失败就把状态置为 `failed_restore_verification`。

### M2 CPU 隔离恢复会把 media-worker 单独迁到 NVMe

- **位置：** 4573–4660 行。
- **机制：** 原 cpuset 为空的 media-worker，会用"正式 base + storage override"重建，于是迁到 NVMe；而 sink 在某些路径上没被恢复，仍留在 `/data`。两边从此读写不同的盘，正式视频证据就找不到素材。
- **触发路径：**
  - 加了 CPU 隔离后，在 `blocked_active_evidence` 提前退出；
  - 非滚动缓存模式（此时恢复后的挂载回读还不会执行，问题被掩盖）。
- **复核意见：**
  - 一位复核者指出：这是基线就有的问题；阶段 2 按 10-12 的参数（`force_runtime_restart=true`）跑，到不了这条路径。
  - 但这正是任务书第 0 节第 5 条要消除的"两端不同盘"，所以仍需修复。
- **修法（二选一）：**
  - CPU 恢复按快照固定 media-worker 原有的挂载；
  - 或者，只有在 sink 也在同一次恢复里迁移时，才允许迁移 media-worker。

  无论哪种，只要 media-worker 被重建过，都要执行恢复后的挂载回读，与模式无关。

### M3 单路滚动缓存的恢复必然失败

- **位置：** 5208–5211 行。
- **现状：** 正式环境没有 `rolling-cache-sink` 容器，快照是 `exists=False`，恢复时直接抛 `missing environment snapshot`，再连锁触发 M1。
- **说明：** 阶段 2 用的是双路，这条路径不在阶段 2 里，但它是一个确定会发生的失败。
- **修法：** 原本不存在的容器，恢复时应删除并核验确实已不存在；不做 Env 比对，也不纳入挂载回读。

### M4 纯预检通过，并不代表 forwarder、Savant 和规则配置一致

- **现状：**
  - `--preflight-only` 只记录了渲染出来的 `FORWARDER_*` 和 `FACE_*`，没有与请求值比对，也没有校验镜像里的代码哈希。
  - 真正的比对要等正式环境已经被改动之后才执行（2139–2146 行），配置不一致时会白白耗掉维护窗口。
- **修法：**
  - 在预检阶段就用已渲染的配置比对 forwarder 队龄、严格预算、队列容量，以及 Savant 的两个间隔；
  - 用现有容器做只读的镜像代码哈希校验，没有可用容器时标为"未知"并失败；
  - 提前解析 `--behavior-rules` 对应的注册表默认值；
  - 提前渲染并校验恢复配置 `restore_cfg`，确认恢复时要用到的服务都存在。
- **最低要求：** 至少把报告和任务书检查清单的措辞改准确，写明"只检查了存储"。

### M5 存储预检和运行时回读对所有模式都无条件执行

- **现状：**
  - 实时 RTSP 输入用 ffprobe 测不出码率，结果返回 2。这是 fail-closed，不会损伤正式环境，但会拒绝所有实时流输入。
  - 不涉及滚动缓存的模式，也被要求缓存盘是 NVMe。
  - null-sink 模式根本不重建 Savant，却仍回读 Savant 的人脸间隔。
- **修法：**
  - 只在 `rolling_cache_evidence` 为真时做盘型和空间门控，其他模式只记录；
  - 新增 `--rolling-cache-bytes-per-second`（报告里写明来源），用于 ffprobe 测不出码率的输入；
  - Savant 回读只在 Savant 确实被重建的模式下执行。
- **对阶段 2 的影响：** A 轮必须使用固定的文件输入（任务书本来就这样要求），因此不受影响。

## 4. 次要问题（可与上述一起修，不阻塞）

1. **行为算法事件不计入冷却统计。** `PRESSURE_COOLDOWN_EVENT_TYPES` 没有加入行为算法事件，B 轮的冷却违规统计和需求上界都会漏掉摔倒、聚集、追逐（275 行，以及 8198、8318、8971 行）。B 轮之前要修。
2. **污染检测把本脚本自己留下的规则标红。** 第二轮起，本脚本在压测槽位留下的 intrusion / watchlist 规则每次都会被标红，红色因此失去意义。修法：清理阶段禁用压测槽位规则；或者只把"启用中的压测摄像头上的规则"和"不在 {intrusion, watchlist} 内的规则"标红。
3. **压测源判定前缀不一致。** 污染检测与 `_is_pressure_source_id` 判定压测源的前缀不同（`rc<N>_`、`forwarder60_null_` 会漏判）。两边应共用同一个判定函数。
4. **MPS 停止状态读早了。** `cuda_mps_stop` 摘要在 MPS 停止之前读取（2459 行），报告恒为 `stopped=false`。
5. **报告可能被覆盖。** 参数错误或纯预检会覆盖已有运行的 `report.json`。环境类错误也被标成 `argument_validation`。
6. **media-worker 的 Env 回读只比单向。** 不检查恢复后多出来的键，也接受空快照。应与 sink 一样做双向比对，并拒绝空快照。
7. **恢复时没有固定原镜像和 HostConfig。** sink 和 media-worker 的恢复没有固定原 Image ID 和 cpuset，镜像 tag 一旦变动就会悄悄换镜像。
8. **decoupled 模式下停不掉服务。** `stop_dual_shard_runtime` 不再叠加 dual override，decoupled 模式下名单里有基础 compose 不认识的服务，compose 一个都停不掉。阶段 2 用 ROI 模式，不受影响。
9. **tmpfs 没有保留余量。** 可能把 `/dev/shm`（实际占用主机内存）写满。transcode 模式下码率估算偏低。辅助容器里对 `/data` 的探测可能因 overlay 文件系统而失败。正反两向的预检应在真实辅助容器里再跑一次。
10. **缺少 `main()` 级测试。** 没有测试驱动 `main()` 走异常、Ctrl-C 和 blocked 这几条恢复路径。M1、M2 和 B2 都是因此漏网的。参考 `evidence/rolling_cache_stage1_review_20261016/probe_main.py`，为每个注入点断言：恢复调用的集合与顺序、记录下来的恢复错误、最终状态和退出码。

## 5. 重新交审的标准

1. **B1、B2、M1–M5 全部修复。** M2、M3、M5 中如有不适用于阶段 2 的部分，可以只修阶段 2 路径，但必须写明理由和残留风险。
2. **新增测试：**
   - 真实 PostgreSQL 的准备摄像头测试，覆盖 B1；
   - `main()` 级恢复路径测试，覆盖 M1 和 B2，至少注入 3 个失败点；
   - operator 双路容器的快照与恢复测试，覆盖 B2；
   - blocked 加 CPU 隔离路径的测试，覆盖 M2。
3. **全量 harness 不变量：** 失败集合仍是 `98d388c` 的子集。
4. **实机只读预检：** 在真实辅助容器中运行，NVMe 返回 0，`/data` 返回 2；如果 M4 已实现，再加一个配置不一致时返回 2 的用例。30 个正式容器前后一致。
5. **文档：** 回写本审查各条的处理结果和提交 SHA。
