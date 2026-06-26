# Midterm 迁移、性能、程序完整度分析报告

日期：2026-06-26

范围：当前 `video-analytics` checkout 的 midterm 运行栈、两路视频源推理能力、干净迁移能力、未来 60 路扩展可行性，以及程序完整度风险。

## 1. 总体结论

### 1.1 两路推理是否满足

结论：当前提交时的运行态是 2 台摄像头已注册、1 台启用并持续进入推理链；默认拓扑支持两路都启用后进入同一条推理链，但默认运行态不是“两套独立推理模块”。

当前默认拓扑是：

```text
RTSP/source-adapter（按 8090 启用状态动态收敛）
  -> replay-service
  -> analysis-forwarder
  -> 单个 savant-security 推理模块
  -> Redis/Postgres/证据链
```

已核对的运行证据：

- 8090 API 返回 2 台摄像头，其中 `primary_rtsp` / `Primary RTSP Camera` 为 `enabled=false`，`source_00000000-0000-4000-8000-781078565686` / `lab` 为 `enabled=true`。
- `infra/generated/sources.generated.yml` 与 8090 一致：primary 停用、lab 启用，且启用源走 `dealer+connect:tcp://replay-service:5555`。
- `modules/savant_security/config/cameras.midterm.yml` 与 8090 一致，当前 `runtime_epoch_id=midterm-20260626T062143Z-0b1fb842`。
- runtime overview 显示 `sources_active=1.0`，运行中的动态 source adapter 是 lab。
- lab 在 10 秒窗口内的 Savant effective FPS 约为 `8.1`。
- analysis-forwarder 当前队列深度为 `0`，lab `savant_send_failures_total=0`。
- evidence database index health 返回 `status=ok`、`index_source=database`。

如果“两个推理模块”指 `savant-a` / `savant-b` 两个独立 GPU shard，则当前只是配置具备基础：`dual-4090-two-source` compose profile 可以渲染，但运行中的默认栈仍是单 `savant-security`。

### 1.2 当前能否运行

结论：当前程序可以运行，且已启用的 lab 推理链正在处理帧；但当前运行态有一个编排完整度问题需要在迁移基线前修正。

已核对：

- 默认 compose 配置渲染通过。
- `dual-4090-two-source` profile compose 配置渲染通过。
- 关键容器处于 running，`savant-security` 和 `analysis-forwarder` 均 healthy。
- 8090 `/health`、runtime overview、cameras、people、events、evidence health 均可访问。
- GPU 可用：当前主机是 2 张 NVIDIA GeForce RTX 4090，不是 T4。

当前问题：

- `video-analytics-midterm-video-file-sink` 容器实际 running，日志也在写 Replay job 输出，但它没有 `com.docker.compose.*` 标签，`docker compose ps video-file-sink` 查不到它，所以 `scripts/midterm_health.sh` 报 `video-file-sink` 失败。
- `scripts/midterm_health.sh` 对 cameras/people 的计数逻辑只支持 `.data` 为数组；当前 API 返回 `.data.cameras` / `.data.people` 对象结构，所以脚本误报摄像头和人员为 0。直接 API 查询显示摄像头 2 个、人员 3 个。

这两个问题不影响“当前帧链路正在跑”的判断，但影响运维自检和迁移验收可信度。

### 1.3 是否可迁移

结论：支持“干净迁移”，即迁移代码和模型、目标机空状态启动；不支持在现有 clean 脚本下保留旧数据库、人员库、历史证据和 Replay 缓存。

已具备：

- `scripts/midterm_package_clean.sh`：打包 repo snapshot 和 `/data/video-analytics/models`。
- `scripts/midterm_deploy_clean.sh`：目标机恢复代码/模型，创建空运行目录，可 `--no-start` 只校验不启动。
- `scripts/midterm_start.sh`：检查 Docker/GPU/模型/端口，创建目录，构建并启动栈。
- 必需模型当前存在：
  - `yolo26_pose/yolo26_pose.onnx`
  - `yolov8_face/yolov8n-face.onnx`
  - `yolov8_face.onnx` 软链接
  - `adaface/adaface_ir50_webface4m.onnx`

迁移边界：

- clean 迁移不会携带 PostgreSQL 人员/摄像头/向量库状态，也不会携带 Redis、Replay RocksDB、历史 evidence media。
- 如果目标是“新机器继续使用当前 2 路摄像头、当前 3 个注册人员、现有人脸向量和历史证据”，需要新增状态迁移方案：PostgreSQL dump/restore、必要 media 文件、校验 manifest，而不能只用 clean package。
- 本报告对应的运行基线包含 `modules/savant_security/config/cameras.midterm.yml` 变更。该文件与 8090 当前状态一致，迁移包应从已提交且干净的 worktree 生成；不要把未标注 dirty worktree 当作正式基线。

### 1.4 未来扩展至 60 路是否方便

结论：摄像头配置层扩展较方便，生产级 60 路不方便直接扩；需要按既有 60 路 readiness 计划补齐性能、分片、证据和保留窗口验收。

当前 readiness 脚本结果：

```text
FAIL enabled_rtsp_sources actual=2 required>=30
FAIL gpu_t4_count actual=0 required>=1 names=NVIDIA GeForce RTX 4090,NVIDIA GeForce RTX 4090
FAIL runtime_source_count actual=2 required>=30
PHASE2_SINGLE_T4_READY=false
```

60 路目标不是简单把 sources 加到 60 个。当前已设计的目标是：

```text
30 路 / T4 shard
2 x T4
2 套 Replay + forwarder + Savant shard
总计 60 路
```

当前阻塞项：

- 当前主机不是 T4，不能产出 T4 readiness token。
- 当前实际运行只有 2 路，不是 30/60 路压力输入。
- 单 shard 默认 `BATCH_SIZE=1`、`POSE_BATCH_SIZE=1`、`FACE_DETECTOR_BATCH_SIZE=1`、`MAX_PARALLEL_STREAMS=4`，不适合直接推到 30 路每 shard。
- Redis exporter 仍在 Savant 热路径上，60 路下需要异步队列、超时、drop/error metrics。
- single-shard `savant-security` 仍有 debug PyFunc 和 h264/nvenc 输出成本，生产 60 路前应 gate 或移出。
- 证据物化当前是潜在瓶颈：`MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=1`，annotation Redis maxlen/TTL 对 60 路保留窗口不足。
- 30 路同 shard 的 forwarder fairness 尚未证明。

因此，“从 2 路扩到 60 路”的便利程度可以分成两层：

- 管理平面：通过 8090 增加摄像头、导出 runtime config，路径已经有。
- 生产吞吐：还需要 Step A-D 的工程闭环，不应直接承诺 60 路 ready。

## 2. 程序完整度分析

### 2.1 已完成度较高的部分

- 部署入口清晰：`infra/docker-compose.midterm.yml`、`infra/env/midterm.env`、`scripts/midterm_start.sh`。
- 推理模块链完整：YOLO26-pose、nvtracker、YOLOv8-Face、AdaFace、规则评估、frame annotation、face observation export、性能指标。
- 证据链完整：Replay full-rate 存储、clip-worker Replay job、video-file-sink、media-worker evidence bundle、8090 evidence viewer。
- 8090 管理面完整度较高：摄像头、人员/人脸、算法支持矩阵、runtime apply/restart、证据索引、存储维护。
- 干净迁移脚本和文档已经存在，且当前脚本语法检查通过。
- 测试覆盖较多，本轮目标测试 65 个用例通过。

### 2.2 仍需修正的完整度问题

1. 健康检查脚本需要适配当前 API shape。

   `scripts/midterm_health.sh` 的 `json_count()` 只识别 `.data` 数组；当前 cameras/people API 返回 `.data.cameras` 和 `.data.people`。这会制造误报警，影响交付验收。

2. `video-file-sink` 当前运行但不归 compose 管理。

   容器没有 compose 标签。短期可以通过直接 `docker inspect`/runtime overview 判断实际状态；迁移或验收前应恢复为 compose 可见，或修正 runtime recreate 逻辑/health 脚本，避免“实际运行”和“编排视图”不一致。

3. 算法支持状态不是全 production-ready。

   当前支持矩阵中：

   - `behavior.intrusion`：production_ready
   - `face.watchlist`：production_ready
   - `behavior.chasing` / `behavior.crowd_gathering` / `behavior.fall`：event_only
   - `face.observation`：config_only
   - `behavior.loitering` / `behavior.running` / `behavior.wall_climb_suspicious`：unsupported
   - `face.live_search`：deferred

   当前 lab 配置里存在多条非 production-ready/disabled 规则。它们可以作为 UI/config 能力展示，但不能当作完整生产告警证据能力。

4. 本次 config 记录了新的运行基线。

   `cameras.midterm.yml` 基线变更包括 primary / lab 的 watchlist 规则、lab watchlist 阈值/间隔/目标人员、evidence policy 和 runtime epoch。当前文件与 8090/generator 的摄像头启停一致：primary 停用、lab 启用。正式迁移前要确认这份运行基线已随目标提交固化。

## 3. 性能现状

历史两路启用窗口数据：

| 指标 | primary_rtsp | lab/source_... |
| --- | ---: | ---: |
| Savant 10s effective FPS | 8.1 | 8.1 |
| Savant frames_seen_total | 5199 | 5188 |
| frame_annotations_exported_total | 5199 | 5188 |
| pose_objects_total | 4131 | 0 |
| face_objects_total | 5067 | 0 |
| face_observations_exported_total | 544 | 0 |
| forwarder frames_seen_total | 15856 | 19810 |
| forwarder frames_forwarded_total | 5292 | 5283 |
| forwarder frames_dropped_total | 10564 | 14527 |
| forwarder send failures | 0 | 0 |

解读：

- 该窗口证明默认单 `savant-security` 拓扑可以同时接收两路并按约 8 FPS 做分析采样。
- 当前提交时 primary 已停用，实时运行入口只有 lab；不要把这张表误读为当前两路都在运行。
- forwarder 正在按设计丢弃分析分支帧，不影响 Replay full-rate evidence 分支。
- lab 当前窗口没有 pose/face object，不能据此说明推理链未运行；它的帧计数和 annotation 计数在增长。
- 当前是 2 路轻负载结果，不能外推到 30/60 路。

## 4. 迁移建议

### 4.1 如果目标是干净迁移

执行前建议：

1. 确认 worktree 干净，且 `modules/savant_security/config/cameras.midterm.yml` 已按预期纳入或排除 baseline。
2. 修正或解释 `video-file-sink` compose 标签问题，保证 `bash scripts/midterm_health.sh` 不再误判关键服务。
3. 修正 `scripts/midterm_health.sh` 的 cameras/people 计数逻辑。
4. 运行：

```bash
docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml config
pytest -q harness/tests/test_midterm_deployment_contract.py
bash scripts/midterm_package_clean.sh
```

目标机：

```bash
bash midterm-clean-*_deploy_clean.sh midterm-clean-*.tgz --no-start
bash scripts/midterm_start.sh
```

### 4.2 如果目标是保留当前业务状态迁移

clean 脚本不够，需要新增状态迁移包，至少包括：

- PostgreSQL dump：cameras、rules、zones、persons、person_gallery_embeddings、events/evidence task 状态等。
- 必要 media：人脸注册源图/crop，如 `face_uploads`、`face_registration`、历史 `face-registration`。
- 是否迁移历史 evidence 和 Replay RocksDB 的明确策略。
- manifest：git SHA、dirty diff、数据库 dump checksum、模型 checksum、media inclusion policy。

## 5. 60 路扩展建议

建议按 `specs/22_midterm_60_stream_readiness_risk_closure_plan.md` 的顺序推进：

1. Step 0：先做可迁移 baseline，输出 `PASS_60R_STEP_0_MIGRATION_BASELINE`。
2. Step A：移除/隔离热路径成本，重点是 debug PyFunc、unused output encoding、Redis exporter 异步化。
3. Step B：补 per-source fairness 指标，做 30 路同 shard 压力。
4. Step C：在真实 T4 上测 batch/interval/MAX_PARALLEL_STREAMS，产出 `PASS_PHASE2_SINGLE_T4_30`。
5. Step D：证据物化、annotation retention、Replay TTL、存储 quota 和 10 -> 30 -> 60 压力闭环。

当前可以做的低风险前置项：

- 修复 health 脚本误报。
- 让 `video-file-sink` 重新回到 compose/runtime 一致状态。
- 为 `dual-4090-two-source` 做一次只跑两路的 topology smoke，证明 `replay-a/b`、`analysis-forwarder-a/b`、`savant-a/b`、`video-file-sink-a/b` 实际端到端闭环。
- 明确 60 路时是否保留当前 single-shard 路径，还是完全切到双 shard profile。

## 6. 本轮验证记录

执行日期：2026-06-26

通过：

```text
docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml config
docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml --profile dual-4090-two-source config
bash -n scripts/midterm_start.sh scripts/midterm_stop.sh scripts/midterm_health.sh scripts/midterm_package_clean.sh scripts/midterm_deploy_clean.sh scripts/runtime/doctor_midterm.sh scripts/runtime/video_file_sink_entrypoint.sh scripts/runtime/prepare_dual_4090_savant_b_model_cache.sh
pytest -q harness/tests/test_midterm_deployment_contract.py harness/tests/test_camera_config_export.py harness/tests/test_camera_runtime_apply_service.py harness/tests/test_analysis_forwarder.py harness/tests/test_phase2_single_t4_readiness.py harness/tests/test_replay_shard_routing.py harness/tests/test_savant_perf_metrics_contract.py
git diff --check
```

结果：

```text
默认 compose config: 1123 lines
dual-4090-two-source compose config: 1767 lines
pytest: 65 passed
git diff --check: clean
```

运行态检查：

```text
8090 cameras API: total=2
8090 people API: total=3
runtime overview: sources_active=1.0
analysis-forwarder queue_depth=0
evidence health: status=ok, index_source=database
GPU: 2 x NVIDIA GeForce RTX 4090, 24564 MiB each
phase2 readiness: PHASE2_SINGLE_T4_READY=false
```

未通过/需解释：

```text
bash scripts/midterm_health.sh:
  Passed: 33
  Warnings: 3
  Failed: 1
```

失败原因不是服务未运行，而是 `video-file-sink` 当前没有 compose 标签；摄像头/人员警告是 health 脚本 API 计数逻辑不适配当前响应结构。
