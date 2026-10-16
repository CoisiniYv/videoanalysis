# 阶段 1 复审：Codex 修复 a16f926（Opus，2026-10-17 KST）

- **被审：** `codex/rolling-cache-storage-guard-20261015`，HEAD `4d98fb5`（修复提交 `a16f926`）。
- **对照：** 首次审查 `docs/code_review/rolling_cache_stage1_review_2026-10-16.md`。
- **方法：**
  - 两个维度并行审读；
  - 对严重问题做对抗式复核；
  - 用桩化的 `main()` 注入 12 个以上失败点，观察实际调用序列；
  - 用真实 compose 渲染与 10-13 的真实快照，对恢复结果做对比；
  - 用一次性的 PostgreSQL 16 跑真实数据库测试。

## 结论

**路线二要求的 B1、M1 已修好，可以放行。** Codex 额外实现的"双路容器精确恢复"（B2）还不能用：按现在的写法，A 轮每一轮都会以 `failed_restore_verification` 结束，还会改动 savant-b 的模型挂载。

建议 A 轮按路线二原定方案执行：双路容器只停止、不重建，留作已获批的已知漂移，同时修好下面列出的几个小问题。B2 的精确恢复改用 Docker API 克隆的方式另行实现，在 B 轮之前完成。

## 已确认修好

| 项 | 证据 |
|---|---|
| B1 | 6548 行已改为 `ANY(%s::text[])`。真实 PostgreSQL 测试在修复后的代码上通过；同一测试放到 `ba76375` 上，报 `UndefinedFunction: operator does not exist: text = uuid`，说明测试能有效拦住这个问题 |
| M1 主体 | 每个恢复步骤都经过 `restore_step` 执行，错误记入 `restore_errors` 后继续；先停双路运行时；`restore_runtime` 一定会执行；有错误时状态为 `failed_restore_verification`、退出码为 1。12 个注入场景的调用序列都符合预期 |
| M2 | 在 A 轮路径上不会发生：sink 快照在 CPU 隔离之前做；sink 与 media-worker 一起用正式 base + storage override 恢复之后，CPU 隔离才恢复 |
| M3 | sink 原本不存在时，恢复会把残留删掉并核验已不存在 |
| M5 | 只有 rolling-cache 模式才做盘型门控；按 bitrate 回退时标注了来源；null-sink 模式不回读 Savant |
| 次要问题第 1、3、4 条 | 冷却统计已含三种行为算法；压测源判定已共用；MPS 摘要改在停止后读取 |
| 全量 harness | 9 failed / 1968 passed / 74 skipped / 7 errors，失败集合是 `98d388c` 的子集，没有新增失败 |

## 新发现：Codex 的 B2 实现带来的问题（均已复核）

### N1 阻断：双路恢复的标签比对必然失败

- **位置：** `_dual_shard_snapshot_diff`（3554–3577 行）对 `Config.Labels` 做逐项完全比对。
- **机制：** compose 每次重建都会改写 `com.docker.compose.project.config_files`、`config-hash`、`replace` 和 `depends_on` 等记账用的标签。10-13 遗留的容器，其 config_files 带的是 10-13 产物目录里的 override，恢复时用的 `-f` 列表不可能和它一致。
- **后果：** 容器已经被重建完，校验才报 `dual runtime restore mismatch`。A 轮必然以 `failed_restore_verification`、退出码 1 收场（这本身就是停止条件），容器也成了"10-13 的环境变量 + 新标签和新 HostConfig"的混合体。
- **为什么测试没发现：** 单元测试 mock 了前后相同的标签。
- **复现：** `evidence/rolling_cache_stage1_rereview_20261017/probe_dual_render.py` 和 `probe_b2_labels.py`。

### N2 阻断：恢复会改动 savant-b 的 /models 挂载

- **机制：**
  - 10-13 时，savant-b 的 `/models` 来自 `start_dual_shard_runtime` 单次调用传入的 `SAVANT_B_MODEL_ROOT=/data/video-analytics/models`（3380 行）。
  - 恢复时调用 `run()` 没传这个变量，正式 compose 于是按默认值 `/data/video-analytics/models-savant-b` 渲染。
- **后果：**
  - 挂载比对失败；
  - 正式容器的模型目录被改掉。8090 下次启动双路时会照这个挂载克隆，目录里没有模型就会加载失败。

### N3 主要：`--preflight-runtime-readback` 在 uos157 上必然返回 2

- **机制：** 纯预检读取的是现存的 `replay-raw-fanout-a/b` 和 `savant-a/b`，而它们是已经退出的 10-13 遗留容器：
  - 对停止的容器执行 `docker exec sha256sum` 必然失败；
  - 遗留容器的环境变量（人脸间隔 4、没有队龄上限）和 A 轮请求本来就不同。
- **结论：** 这个开关检查的是"上一轮的残留"，不是"本轮将要创建的容器"。
- **复现：** `probe_m4.py`。
- **修法：**
  - 纯预检只比对渲染出的 `rendered_pressure_config` 与请求值：`FORWARDER_MAX_QUEUE_AGE_MS`、`FORWARDER_STRICT_FPS_BUDGET`、`FORWARDER_QUEUE_MAX_SIZE`、`FACE_INFER_INTERVAL`、`FACE_EMBEDDING_INFER_INTERVAL`、ROI 相关开关、`SAVANT_MODULE_FILE`。
  - 镜像代码哈希改为：先 `docker image inspect` 解析出 compose 将要使用的镜像 ID，再通过一个正在运行、且使用同一镜像 ID 的容器计算；找不到这样的容器时，标为"未知"。

### 次要问题（建议在 A 轮前一起修，改动都很小）

1. **压测源判定前缀过宽（涉及正式环境安全）。** 共享判定把裸 `rc` 和 `pressure60` 都当作前缀，`rcam_front` 这类正式源 ID 也会被认成压测源，`restore_cameras` 就会跳过它，这台正式摄像头在 A 轮结束后保持禁用。修法：
   - 锚定实际生成的格式，例如 `^(?:rc\d+_|pressure\d*_|forwarder60_null_)`；
   - `restore_cameras` 按快照恢复所有 `site_id<>'pressure'` 的摄像头，恢复后回读 enabled 状态。
2. **恢复完成后的读取不受保护。** 正常路径恢复完成后的 `db_summary`、`/runtime/overview`、`restored_cache_readback` 不在 `restore_step` 内（2781–2790 行）。任一项出现瞬时错误，就会进入异常处理，把整套恢复再执行一遍，并把这一轮报成 `failed_exception`。修法：用 `restore_step` 包住，或者设置 `restore_completed` 标记，跳过第二次恢复。
3. **异常处理在恢复前的写盘操作不受保护。** 异常和 Ctrl-C 处理在恢复之前要写 report 和 traceback、停 RTSP 转推、做 cleanup，这些都不受保护。磁盘写满（ENOSPC）或者第二次 Ctrl-C 都会跳过全部恢复（`probe_main2_enospc.py`）。修法：这部分放进 try，把 `restore_abort_steps` 放进 finally；恢复期间忽略 SIGINT。
4. **A 轮冷却上界被高估 2.5 倍。** A 轮没有行为规则，但上界按 5 种事件类型计算。修法：按"intrusion、watchlist 加实际请求的行为规则"计算。
5. **纯预检没有渲染和校验恢复配置。** 运行开始时，`formal_restore_config` 如果出错，不会写报告，直接以 traceback 退出。修法：在预检里渲染 `restore_cfg`（双路时包含 operator override），失败返回 2。
6. **其他（可延后）：**
   - 双路恢复没有比对 HostConfig（RestartPolicy、DeviceRequests 等），也没有固定镜像；
   - 单路模式的恢复后挂载回读仍要求 `rolling-cache-sink` 存在（不在阶段 2 路径）。

## A 轮放行条件（按路线二）

1. **双路容器只停止、不重建。** 关闭 `restore_dual_shard_runtime`，或改为只比对、不重建、不计入错误。快照和比对结果写进报告，标注为"已批准的已知漂移"。这 5 个容器 A 轮后的状态与现在同类（压测配置），savant-b 的模型挂载保持不变。
2. **纯预检不使用当前的 `--preflight-runtime-readback`。** 改用 N3 的修法比对渲染配置；如果来不及，就在开跑前人工检查 `$ART` 中渲染出的 compose，确认上述各值与请求一致，并记进报告。
3. **次要问题第 1、2、3、5 条修复**（各自很小）。第 4 条改不改都行，改了报告更准确。
4. **复核：** 全量 harness 仍是基线失败集合的子集；实机只读预检正向返回 0、反向返回 2；30 个正式容器前后一致。

满足以上条件后推送，由 Opus 快速复核。复核通过后，用户此前批准的维护窗口（复核通过后 24 小时内，由 Codex 选定开始时间）开始生效。

## B 轮之前要完成

- **B2 精确恢复改用 Docker API 克隆。**
  - 做法：保存完整的 pre-run inspect（Config、HostConfig、Networks），删除压测容器后按原样创建，与 8090 的 `_create_body_from_inspect` 一致；只有原本在运行的才启动。
  - 比对内容：Labels、Env、Image ID、Mounts、HostConfig。
  - 这些差异应视为正常：Id、Created、State 时间戳。
  - 测试：用 `pre_repair_snapshot.json` 加上真实形态的 compose 标签作为测试数据。
- **清理 10-13 残留：** 把双路容器规范化回正式配置，需要用户另行批准。
