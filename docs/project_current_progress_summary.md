# Project Current Progress Summary

更新时间：2026-06-18

## 总体结论

当前可部署主线是 midterm 版本，入口集中在
`infra/docker-compose.midterm.yml`。客户侧主要访问 `8090`，由
`services/evidence-viewer` 对外提供操作入口，并代理内部 `api:8000`。

当前项目已经具备从 RTSP 输入、Replay 缓存、analysis-forwarder 分析限流、
Savant 推理、事件入库、录像取证、证据 sidecar、到 8090 查看证据的主链路。
生产级能力以
`behavior.intrusion` 入侵检测证据链路为当前最完整基线；其他算法开关仍按不同
成熟度分层管理。

2026-06-11 的运行态、算法齐全度、未实现项和下一步实现计划已固化到
`docs/midterm_progress_snapshot_2026-06-11.md`，后续继续计划应以该快照作为
对比基线。

2026-06-12 已完成最近 24 小时性能和运行态修复 goal：多源 source-adapter
收敛、证据 camera_name 展示、Savant 性能可观测性、clip-worker pending 恢复、
media-worker 扫描/probe 降本、worker 查询索引均已落地并完成 10 分钟双源验收。
中文完成记录见
`docs/repair_goal/midterm_recent_24h_goal_completion_2026-06-12.md`。

2026-06-14 到 2026-06-15 已完成 Replay/Savant backpressure 的 Phase 0/0.5/1
主线：Phase 0 记录并保留有效实验项，Phase 0.5 证明 forwarder 的
`savant_rs`/frame-lineage 可行性，Phase 1 将 `analysis-forwarder` 插入
Replay 与 Savant 之间。当前 full-rate evidence 仍由 Replay/video-file-sink
生成，analysis-forwarder 只影响 sampled analysis path。Phase 2/3 仍未完成。

2026-06-15 已完成 post-Savant evidence proof window 的部分修复并运行验证：
`clip-worker` 能暴露 `waiting_proof`、`queued`、`replaying`、`ready`、`failed`
等状态，proof 等待不再提前占用 Replay concurrency，跨 session post-window
proof 和 truncated pre-window proof 均带审计标签。该修复关闭的是“事件已经产生
但 evidence proof 失败”的一类问题，不证明所有摄像头都会产生 upstream event。

2026-06-18 已完成 Replay 双分片录制最小闭环：新增 `source_id -> replay shard`
的一等路由能力，`runtime_apply` 能生成 per-source shard endpoint，`clip-worker`
能按 source 所属 shard 创建 Replay job，并将 shard/job 诊断写入 DB/event payload。
60 个模拟 `source_id` 已通过 30/30 分片计划检查，少量循环源已证明
`replay-a` / `replay-b` 能分别录制并输出 clip。该结论只证明录制分片、路由和
DB identity 正确，不等同于真实 60 路吞吐通过。修改记录见
`docs/midterm_replay_shard_change_record_2026-06-18.md`。

```text
RTSP -> Replay storage -> analysis-forwarder -> Savant inference -> Redis/PostgreSQL
  -> event-worker -> clip-worker -> Replay job -> video-file-sink
  -> media-worker evidence sidecar -> 8090 operator portal
```

## 当前运行入口

| 项目 | 当前值 |
| --- | --- |
| Compose | `infra/docker-compose.midterm.yml` |
| Env | `infra/env/midterm.env` |
| Replay config | `modules/savant_replay/config.midterm.json` |
| Savant module | `modules/savant_security/module.yml` |
| Camera config | `modules/savant_security/config/cameras.midterm.yml` |
| 8090 门户 | `http://0.0.0.0:8090/` |
| Replay API | host port `8098` |
| Analysis-forwarder metrics | host port `18081` |
| Redis | host port `6396` |
| Internal API | compose network `api:8000`，不直接发布宿主机 |

启动和预检查参考 `docs/midterm_deployment.md`。

## 已具备能力

### 8090 客户侧入口

- 8090 首页、健康检查、静态资源加载正常。
- 8090 原生证据接口支持 evidence bundle 列表、详情、标注、sink metadata、
  raw clip Range 读取。
- 8090 代理 `/api/v1/*` 到内部 API，代理 `/media/*` 到内部媒体文件。
- 证据列表和详情展示报警机器时间；新 bundle 写入
  `event.alarm_machine_time`，来源为 `events.created_at`。
- 存储维护支持 summary、preview、job detail；默认执行删除关闭，返回
  `403 storage maintenance execute disabled` 属于预期保护。

详细对接见 `docs/midterm_8090_port_integration.md`。

### 摄像头和运行时应用

- 摄像头基础字段、启停状态、RTSP 参数、区域、算法规则通过内部 API 入库。
- 8090 的 runtime apply/restart 是受控运行时入口，保持 8090 管理端和内部 API
  在线，只重启推理/录像链路。
- 受控流程会先停 source-adapter 和 workers，创建新的 runtime epoch，重建
  `video-file-sink`，重启 Replay 和 Savant，再恢复 workers 和 source-adapter。
- 动态 RTSP 源通过 `infra/generated/sources.generated.yml` 和 Docker socket
  创建，继续走 replay-first 路径。

详细部署和操作见 `docs/midterm_deployment.md`。

### 人员与人脸注册

- 人员列表、人员详情、图库信息通过数据库读取。
- 人脸注册是真实链路，不是 mock；依赖 YOLOv8 face ONNX 和 AdaFace ONNX。
- API 镜像使用 `services/api/Dockerfile.face-runtime`，复用 face-worker 运行层，
  避免 API 构建时重复安装 ONNX Runtime/OpenCV/Numpy。
- 浏览器通过 8090 的 `/media/*` 代理查看上传图和裁剪图。

### 证据生成与查看

- Replay-first 证据链路已接通。
- `analysis-forwarder` 已插入 Replay `out_stream` 与 Savant 之间，用于分析分支
  采样、drop-on-backpressure 和 `va_forwarder_*` 指标输出；它不是证据视频来源。
- evidence bundle 当前包含 `raw_clip.mov`、`sink_metadata.json`、
  `annotations.frame_cache.identity.jsonl`、`summary.frame_cache.identity.json`、
  `metadata.json`。
- media-worker 写入 `project_version=midterm` 和 `schema_version=2.0-midterm`。
- 8090 能读取生产 sidecar 标注和 raw clip。

## 算法能力分层

### 端到端已实现

- `behavior.intrusion`

入侵规则是当前完整基线。8090 的规则配置、证据窗口、Savant 行为事件、
event-worker record request、clip-worker Replay job、media-worker evidence
bundle 能形成完整链路。

### 部分实现

- `behavior.crowd_gathering`
- `behavior.fall`
- `behavior.chasing`

这些规则有注册模块和事件能力，但默认 recording/evidence 策略仍主要面向
`intrusion` 和 `watchlist_hit`。不能按生产证据能力等同于入侵规则。

### 配置可见但未完整接通

- `behavior.loitering`
- `behavior.running`
- `behavior.wall_climb_suspicious`
- `face.observation`
- `face.watchlist`
- `face.live_search`

这些开关可以在 UI/API 层保存或导出部分配置，但还不是完整的 per-camera runtime
gate。face/watchlist 当前仍主要由 face-worker 和环境变量控制。live search 仍是
deferred/contract 状态。

算法控制边界见 `docs/midterm_operator_algorithm_controls_runtime_status.md`。

## 最近固化的问题修复

### 2026-06-18 Replay 双分片录制最小闭环

问题：未来 60 路摄像头不能继续依赖单一 Replay API / job sink。否则 Replay job
可能发到没有存储该 `source_id` 的 shard，导致录像混路、取证失败或 DB 无法区分
事件归属。

当前状态：已新增 shard 配置、runtime apply 分片输出、clip-worker shard-aware job
routing、DB shard/job 诊断字段、双 Replay compose profile 和 60 source 分片检查脚本。
验证脚本显示 `source_00..source_29 -> replay-a`、
`source_30..source_59 -> replay-b`，30/30 平衡；小规模循环源已证明两个 shard
能分别录制并输出 clip。

边界：没有真实 60 路摄像头，因此不能声明 60 路吞吐、RocksDB 写入延迟或
video-file-sink/media-worker burst capacity 已通过。后续仍需执行 10 -> 30 -> 60
staged runtime pressure test。

详细记录：`docs/midterm_replay_shard_change_record_2026-06-18.md`。

### 2026-06-12 最近 24 小时性能修复

问题：最近 docs 和 `specs/15_savant_performance_observability.md` 暴露了多源
运行态、证据展示、Savant metrics、clip-worker 队列恢复、media-worker 扫描
probe、worker 查询索引等性能和可观测性缺口。

当前状态：已完成修复并通过静态测试、doctor/smoke 和 10 分钟双源运行验收。
验收 artifact 位于：

```text
/data/video-analytics/artifacts/perf/midterm-perf-20260612T070006Z-cd58154
```

关键结果：

- source convergence healthy；
- `primary_rtsp` 与 `lab` 双源 evidence 均有 ready/verified 样本；
- `PASS_SAVANT_PERF_OBSERVABILITY_READY` 通过；
- 10 分钟窗口内 worker restart count 无变化；
- bottleneck classification 为 `no bottleneck observed in the run window`。

详细记录：`docs/repair_goal/midterm_recent_24h_goal_completion_2026-06-12.md`。

### Replay routing_id 拒帧

问题：source-adapter 重连或局部重启后，Replay 缓存旧 ZeroMQ routing identity，
出现容器看似健康但 Replay 拒收帧，导致无新事件、无 Replay job、无 evidence。

修复方向：runtime apply/restart 改为受控顺序，先停所有源和 workers，再重启
Replay/Savant，最后恢复源和 workers。

详细记录：`docs/midterm_replay_routing_id_recovery.md`。

### video-file-sink 网络别名丢失

问题：手动重建 `video-file-sink` 容器时丢失 compose 服务名别名
`video-file-sink`，Replay job 使用
`dealer+connect:tcp://video-file-sink:6666` 时无法解析 peer，导致无 raw clip 和
无 evidence bundle。

修复方向：runtime apply/restart 重建 sink 时保留 Docker 网络别名
`video-file-sink`。已验证 Replay job 清空，新事件生成 `clip_status=ready`、
`epoch_guard=passed`，8090 raw clip Range 返回 HTTP 206。

详细记录：`docs/midterm_replay_routing_id_recovery.md`。

### 官方 sink metadata 缺少 runtime epoch

问题：官方 `video-file-sink` 输出的逐帧 metadata 不保留 Replay labels，导致
`sink_metadata_runtime_epoch_id` 缺失。原 strict guard 把缺失误判为失败。

修复方向：把 `sink_metadata_runtime_epoch_id` 作为可选佐证；字段存在且不匹配
时 fail closed，字段缺失但 event payload、record request、Replay labels、
sink path 和 current epoch 一致时允许发布 evidence。

详细记录：`docs/midterm_replay_routing_id_recovery.md`。

### 入侵 evidence raw clip 超长

问题：time-domain crop 失败时，media-worker 曾回退复制完整 Replay sink 输出，
导致用户可见 raw clip 超出请求的 10 秒窗口。

修复方向：crop 失败、overlong clip、sink-window mismatch 均 fail closed；保留
诊断文件，但不发布不可信 raw clip。

详细记录：`docs/midterm_replay_intrusion_clip_duration_diagnosis.md` 和
`specs/14_replay_evidence_duration_guard_fix.md`。

### 8090 报警机器时间

问题：证据列表/详情需要展示报警发生的机器时间，不能把帧相对时间或视频相对时间
当成真实时间。

当前状态：新 evidence metadata 写入 `event.created_at` 和
`event.alarm_machine_time`；8090 bundle API 返回 `alarm_machine_time` 和来源。
旧 bundle 仅在字段看起来像 Unix epoch 毫秒时做兼容 fallback。

详细记录：`docs/midterm_8090_port_integration.md`。

### Replay/Savant backpressure 与 analysis-forwarder

问题：2026-06-14 运行态显示 Replay `out_stream` 被 Savant 分析路径反压，
Replay 进入长时间 send retry，source-adapter 随后出现
`WriterResultSendTimeout` 和重启风暴。

当前状态：

- 8090 runtime overview 已显示 restart count/rate，并能把 restart storm 标红。
- Phase 0A 保留 `MAX_FPS_CONTROL=false`，但 `INGRESS_FPS_GATE_ENABLED=true`
  保持项目 ingress FPS gate。
- Phase 0B 保留 Replay analysis `out_stream` 短 retry，降低未来卡住时的
  in_stream 阻塞窗口。
- Phase 0C 保留 live RTSP `SYNC_OUTPUT=false`，固定源和动态源创建路径一致。
- Phase 0D 证明 RTSP adapter entrypoint 不读取 send-timeout/retry env，因此
  不添加假保护配置。
- Phase 1 已增加 `services/analysis-forwarder/`，Replay 输出改为
  `dealer+connect:tcp://analysis-forwarder:5557`，forwarder 再写 Savant。

详细记录：

- `docs/repair_goal/midterm_phase0_observability_baseline_2026-06-14.md`
- `docs/repair_goal/midterm_phase0a_max_fps_control_experiment_2026-06-15.md`
- `docs/repair_goal/midterm_phase0b_replay_short_retry_experiment_2026-06-15.md`
- `docs/repair_goal/midterm_phase0c_sync_output_experiment_2026-06-15.md`
- `docs/repair_goal/midterm_phase0d_source_adapter_tolerance_verification_2026-06-15.md`
- `docs/repair_goal/midterm_phase05_forwarder_spike_2026-06-15.md`
- `docs/repair_goal/midterm_phase1_analysis_forwarder_2026-06-15.md`
- `specs/16_dual_path_30x2_t4_production_optimization.md`

### Post-Savant evidence proof window

问题：部分事件已被 Savant 检测并进入 `record_request`，但 `clip-worker` 在
`missing_post_savant_frame_pts_window` 上耗尽重试，证据迟迟不可见或失败。

当前状态：已部分修复并运行验证，最近一次记录显示 `failed=0`、
`missing_proof=0`、`ready=1`、Redis pending/lag 均为 0。剩余边界是 frontend
overlay frame-identity hardening，以及“无 upstream event”的检测/规则问题。

详细记录：

- `specs/17_clip_worker_evidence_realtime_alignment_fix.md`
- `docs/midterm_post_savant_evidence_proof_windows_2026-06-15.md`

### Phase 2/3 production capacity

当前状态：只有 gate 和 runner 完成，产能结论未完成。

- `scripts/runtime/check_phase2_single_t4_readiness.py` 会检查真实 T4、至少
  30 路 RTSP/gstreamer sources、forwarder topology 和 runtime overview。
- `scripts/runtime/check_phase2_single_t4_pressure.py` 是 30 分钟 acceptance
  runner，只有通过才可声明 `PASS_PHASE2_SINGLE_T4_30`。
- `scripts/runtime/update_phase2_operating_point.py` 只接受通过的 pressure report
  并更新 spec Appendix A。

当前开发主机记录为 2 路源且不是 T4 30 路目标环境，因此 Phase 2/3 仍 gated。

## 当前已知问题和风险

1. `docs/current_mainline_status.md` 和本文件是当前入口；历史 phase-only 文档只作
   追溯，不应反向覆盖 current mainline。
2. 8090 文档仍记录“绑定区域的算法规则”保存失败问题：
   `camera_zones.zone_id` 是文本 ID，而 `camera_rules.zone_id` 是 UUID。凡是需要
   绑定 polygon 或 line 的规则，都需要继续确认当前 DB/API 是否已完全修复。
3. 可见算法开关多于端到端生产能力。UI 上的开关不能直接等同于 runtime detector
   或 Replay evidence 能力。
4. face observation、watchlist、live search 的 per-camera 开关不是完整 runtime
   gate，当前仍以环境变量和 worker 逻辑为主。
5. 8090 的 `/api/v1/ws/alerts` 还不是真正 WebSocket 透明代理；实时告警如需从
   8090 浏览器入口接入，需要单独实现。
6. runtime apply/restart 依赖 Docker socket 和容器网络名；部署机需要保持
   compose project、网络、容器命名与 midterm 配置一致。
7. evidence fail-closed 后会保留诊断文件，但不会发布不可信 raw clip；上线验收时
   需要把“无 raw clip 但有失败原因”当作保护行为，而不是静默成功。
8. Phase 2/3 的 30/60 路能力必须在真实 T4 和足够源数上验证；当前 dev runtime
   的两源稳定性不能外推为生产容量。
9. Lab camera “人走过但无 evidence”必须先分清无 upstream event 还是 event 已产生
   但 evidence 失败。后者已有 proof-window 修复路径，前者应查规则/ROI/检测。

## 建议下一步

1. 复核并修复 `camera_rules.zone_id` 类型不匹配，跑通 8090 新建 zone + intrusion
   rule + runtime apply + 新 evidence 的完整 smoke。
2. 为算法开关增加显式 support matrix，区分 `configurable`、
   `runtime_detecting`、`evidence_enabled`。
3. 如果 crowd/fall/chasing 需要生产 evidence，扩展 event-worker pending task 和
   recording policy。
4. 如果 face/watchlist/live-search 需要 per-camera 控制，把 Savant/face-worker
   运行时逻辑接入 camera rule。
5. 完成 `specs/17` 的 frontend overlay frame-identity hardening，避免 sparse
   annotations 通过宽时间窗口画错框。
6. 在真实 T4 30 路环境运行 Phase 2 readiness 和 pressure runner，生成
   `PASS_PHASE2_SINGLE_T4_30` 后再更新 operating point。
7. 在已完成 Phase 3A shard routing 最小闭环的基础上，继续执行 10 -> 30 -> 60
   staged pressure test，并落实 `specs/21` 的 evidence IO 优化，避免
   `replay-sink-output` 成为 60 路下的磁盘瓶颈。

## 详细文档索引

- 当前主线：`docs/current_mainline_status.md`
- 文档知识网络：`docs/project_knowledge_network.md`
- 当前进度快照：`docs/midterm_progress_snapshot_2026-06-11.md`
- 部署说明：`docs/midterm_deployment.md`
- 8090 对接：`docs/midterm_8090_port_integration.md`
- 算法开关状态：`docs/midterm_operator_algorithm_controls_runtime_status.md`
- 操作门户设计：`docs/midterm_operator_portal_runtime_design.md`
- Replay routing_id / sink alias 修复：`docs/midterm_replay_routing_id_recovery.md`
- 入侵 clip 时长诊断：`docs/midterm_replay_intrusion_clip_duration_diagnosis.md`
- 数据目录盘点：`docs/midterm_data_directory_inventory.md`
- 存储维护方案：`docs/storage_maintenance_delete_plan.md`
- 最新证据时长修复规格：`specs/14_replay_evidence_duration_guard_fix.md`
- Savant 性能可观测性：`specs/15_savant_performance_observability.md`
- Dual-path / T4 产能计划：`specs/16_dual_path_30x2_t4_production_optimization.md`
- Evidence 实时对齐计划：`specs/17_clip_worker_evidence_realtime_alignment_fix.md`
