# Project Current Progress Summary

更新时间：2026-06-11

## 总体结论

当前可部署主线是 midterm 版本，入口集中在
`infra/docker-compose.midterm.yml`。客户侧主要访问 `8090`，由
`services/evidence-viewer` 对外提供操作入口，并代理内部 `api:8000`。

当前项目已经具备从 RTSP 输入、Replay 缓存、Savant 推理、事件入库、录像取证、
证据 sidecar、到 8090 查看证据的主链路。生产级能力以
`behavior.intrusion` 入侵检测证据链路为当前最完整基线；其他算法开关仍按不同
成熟度分层管理。

2026-06-11 的运行态、算法齐全度、未实现项和下一步实现计划已固化到
`docs/midterm_progress_snapshot_2026-06-11.md`，后续继续计划应以该快照作为
对比基线。

```text
RTSP -> Replay storage -> Savant inference -> Redis/PostgreSQL
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

## 当前已知问题和风险

1. 8090 文档仍记录“绑定区域的算法规则”保存失败问题：
   `camera_zones.zone_id` 是文本 ID，而 `camera_rules.zone_id` 是 UUID。凡是需要
   绑定 polygon 或 line 的规则，都需要继续确认当前 DB/API 是否已完全修复。
2. 可见算法开关多于端到端生产能力。UI 上的开关不能直接等同于 runtime detector
   或 Replay evidence 能力。
3. face observation、watchlist、live search 的 per-camera 开关不是完整 runtime
   gate，当前仍以环境变量和 worker 逻辑为主。
4. 8090 的 `/api/v1/ws/alerts` 还不是真正 WebSocket 透明代理；实时告警如需从
   8090 浏览器入口接入，需要单独实现。
5. runtime apply/restart 依赖 Docker socket 和容器网络名；部署机需要保持
   compose project、网络、容器命名与 midterm 配置一致。
6. evidence fail-closed 后会保留诊断文件，但不会发布不可信 raw clip；上线验收时
   需要把“无 raw clip 但有失败原因”当作保护行为，而不是静默成功。

## 建议下一步

1. 复核并修复 `camera_rules.zone_id` 类型不匹配，跑通 8090 新建 zone + intrusion
   rule + runtime apply + 新 evidence 的完整 smoke。
2. 为算法开关增加显式 support matrix，区分 `configurable`、
   `runtime_detecting`、`evidence_enabled`。
3. 如果 crowd/fall/chasing 需要生产 evidence，扩展 event-worker pending task 和
   recording policy。
4. 如果 face/watchlist/live-search 需要 per-camera 控制，把 Savant/face-worker
   运行时逻辑接入 camera rule。
5. 补一条部署验收脚本：检查 8090、Replay job、sink alias、raw clip Range、
   `alarm_machine_time`、新 evidence bundle。

## 详细文档索引

- 当前主线：`docs/current_mainline_status.md`
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
