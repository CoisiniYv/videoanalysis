# Midterm Progress Snapshot - 2026-06-11

本文档固化 2026-06-11 当前运行状态、算法齐全度、未实现项和下一步实现计划，
用于后续继续计划时做基线对比。

## 快照范围

- 快照时间：2026-06-11 约 16:19，Asia/Shanghai。
- 检查方式：只读运行态检查、API 查询、数据库只读聚合、代码/文档交叉核对。
- 当前主线：midterm。
- 当前入口：`infra/docker-compose.midterm.yml`。
- 客户入口：`http://0.0.0.0:8090/`。
- Compose project：`video-analytics-midterm`。
- 未做事项：未改运行代码、未重启容器、未清理或回滚工作树。

## 运行状态结论

当前 midterm 主线正在运行，基础部署面健康。8090、API、Replay、Savant、
Redis、workers 和 video-file-sink 都处于运行状态，Redis 和 Savant 有健康状态。

已验证：

- `docker compose -f infra/docker-compose.midterm.yml ps --format json`：
  midterm 相关容器均为 `running`。
- `docker ps --filter name=video-analytics-midterm`：
  `video-analytics-midterm-*` 关键容器均 `Up`。
- `bash scripts/runtime/doctor_midterm.sh`：
  `runtime_doctor_ok=True`。
- `bash scripts/smoke/current/check_midterm_deployment.sh`：
  `16 passed`，输出 `PASS_MIDTERM_DEPLOYMENT_CONTRACT`。
- `curl --noproxy '*' http://0.0.0.0:8090/health`：
  HTTP 200，`{"status":"ok","evidence_root":"/evidence","read_only":true}`。
- `video-file-sink` 在 Docker 网络中保留 `video-file-sink` alias，Replay job
  使用的 `dealer+connect:tcp://video-file-sink:6666` 解析前提成立。

当前启用摄像头：

| source_id | 名称 | 状态 | 当前导出规则 |
| --- | --- | --- | --- |
| `primary_rtsp` | Primary RTSP Camera | enabled | `behavior.intrusion` |
| `source_00000000-0000-4000-8000-781078565686` | lab | enabled | `behavior.intrusion` |

当前 8090 evidence list 返回：

- bundle 总数：`2683`。
- 最新 bundle 主要是 `intrusion`。
- 最新 ready bundle 包含 `raw_clip.mov`、生产 sidecar 和
  `alarm_machine_time`。

## 数据库运行计数

以下为通过运行中的 `video-analytics-midterm-api` 容器使用同一 `DATABASE_URL`
做的只读聚合。

最近 24 小时主要事件状态：

| event_type | algorithm_type | status | clip_status | count | 最新时间 |
| --- | --- | --- | --- | ---: | --- |
| `intrusion` | `behavior.intrusion` | `new` | `replay_job_created` | 1 | 2026-06-11 08:19:03 UTC |
| `intrusion` | `behavior.intrusion` | `suppressed` | `not_implemented` | 224 | 2026-06-11 08:18:52 UTC |
| `intrusion` | `behavior.intrusion` | `new` | `ready` | 192 | 2026-06-11 08:18:31 UTC |
| `intrusion` | `behavior.intrusion` | `new` | `skipped_by_poc_limit` | 29 | 2026-06-11 08:16:42 UTC |
| `intrusion` | `behavior.intrusion` | `new` | `failed` | 54 | 2026-06-11 08:15:40 UTC |
| `intrusion` | `behavior.intrusion` | `new` | `duration_guard_failed` | 24 | 2026-06-11 08:12:56 UTC |
| `watchlist_hit` | `face_intelligence` | `suppressed` | `not_implemented` | 24 | 2026-06-11 08:08:10 UTC |
| `watchlist_hit` | `face_intelligence` | `new` | `ready` | 3 | 2026-06-10 09:12:46 UTC |

全部历史聚合中，`intrusion` 和 `watchlist_hit` 都出现过 `ready` 证据：

- `intrusion` 历史 ready 数量超过当前 24 小时 ready 数量，包含旧
  `algorithm_type=intrusion` 和新 `algorithm_type=behavior.intrusion` 两类记录。
- `watchlist_hit` 历史有 `ready=142`，但当前 24 小时 only 3 条 ready，且
  8090 per-camera watchlist 开关还不是真正 runtime gate。

当前 evidence task 聚合：

| event_type | status | count | 最新时间 |
| --- | --- | ---: | --- |
| `intrusion` | `pending` | 10076 | 2026-06-11 08:19:03 UTC |
| `watchlist_hit` | `pending` | 596 | 2026-06-10 16:13:13 UTC |

说明：这里的 `pending` 表示 evidence task 初始/生命周期状态仍存在大量历史和运行中记录，
不能单独等同于最终 bundle 已 ready。最终可见证据状态需要以 `events.payload.media.clip_status`
和 8090 bundle API 为准。

## 算法齐全度矩阵

| 算法 | 配置/API 可见 | Savant/runtime 检测 | 默认 evidence | 当前结论 |
| --- | --- | --- | --- | --- |
| `behavior.intrusion` | 是 | 是 | 是 | 端到端已实现，当前生产基线 |
| `behavior.crowd_gathering` | 是 | 是 | 否 | 部分实现，事件能力存在但证据 parity 未完成 |
| `behavior.fall` | 是 | 是 | 否 | 部分实现，事件能力存在但证据 parity 未完成 |
| `behavior.chasing` | 是 | 是 | 否 | 部分实现，事件能力存在但证据 parity 未完成 |
| `behavior.loitering` | 是 | 否 | 否 | 未实现运行检测，当前会被规则注册表跳过 |
| `behavior.running` | 是 | 否 | 否 | 未实现运行检测，当前会被规则注册表跳过 |
| `behavior.wall_climb_suspicious` | 是 | 否 | 否 | 未实现运行检测，当前会被规则注册表跳过 |
| `face.observation` | 是 | 部分 | 否 | 人脸观测链路存在，但 per-camera 开关不是 runtime gate |
| `face.watchlist` | 是 | 部分 | 部分 | face-worker/env 控制，历史有 ready 证据，但 per-camera 开关未接管 |
| `face.live_search` | 是 | 否 | 否 | deferred/contract-only，当前不是可用实时搜索 |

## 已实现能力

### 8090 和运行时管理

- 8090 作为客户入口可用。
- 8090 代理 `/api/v1/*` 到内部 API。
- 8090 原生 bundle API 可读 evidence 列表、详情、sidecar、raw clip。
- 摄像头基础字段、启停、RTSP 参数、zone/rule 通过 API 持久化并可导出。
- runtime apply/restart 走受控顺序，避免 Replay routing identity 缓存旧连接。
- `video-file-sink` 网络 alias 已满足 Replay job 解析要求。

### 入侵检测和证据

`behavior.intrusion` 当前是唯一完整生产基线：

```text
8090/API rule
  -> cameras.midterm.yml
  -> Savant behavior rule
  -> Redis event
  -> event-worker record_request
  -> clip-worker Replay job
  -> video-file-sink
  -> media-worker evidence sidecar
  -> 8090 evidence viewer
```

当前已看到最新 `intrusion` 事件持续产生，record request、Replay job 和 ready
bundle 都在运行中出现。

### 人脸注册和 watchlist 基础

- 人脸注册是真实链路，依赖 YOLOv8 face ONNX 和 AdaFace ONNX。
- face-worker 正在运行并插入 face observation。
- face-worker 日志显示 watchlist target 已刷新：
  `demo:f4_3:reese`、`demo:f4_3:finch`。
- 历史数据库中 `watchlist_hit` 有 ready 证据。

## 未完整实现项

### 行为算法未齐全

缺失 runtime rule module：

- `behavior.loitering`
- `behavior.running`
- `behavior.wall_climb_suspicious`

部分实现但未默认证据化：

- `behavior.crowd_gathering`
- `behavior.fall`
- `behavior.chasing`

原因：

- 已注册的部分规则可以产生事件，但 event-worker 默认 evidence task/recording
  策略仍主要面向 `intrusion`、`watchlist_hit`、`live_search_hit` 和
  `face_intelligence`。
- `infra/env/midterm.env` 当前 `RECORDING_EVENT_TYPES=watchlist_hit,intrusion`。
- 其它 behavior event 默认不等同于入侵的 Replay evidence 生产能力。

### 人脸算法开关不是 per-camera runtime gate

- `face.observation` 可保存/导出，但 Savant face observation exporter 没有用
  camera rule enabled 状态做 gate。
- `face.watchlist` 可保存/导出，但实际匹配由 face-worker 环境变量和目标名单控制。
- `face.live_search` 是 deferred/contract-only，当前命令行 emitter 只允许
  `watchlist_hit`。

### 证据链路仍有失败保护状态

当前 evidence fail-closed 是刻意保护行为，但代表链路仍需继续提升稳定性：

- `duration_guard_failed`：time-domain crop 或窗口验证失败时不发布不可信 raw clip。
- `failed`：Replay/job/media-worker 某一步失败。
- `skipped_by_poc_limit`：clip-worker 并发/运行限制导致跳过。
- `generated_annotation_failed` 或 `generated_unverified`：旧 bundle 或部分链路的
  标注/验证状态未达到当前生产 sidecar 标准。

## 下一步实现计划

### P0：固化 support matrix

目标：让 8090/UI/API 明确区分算法状态，避免“能配置”被误认为“已生产可用”。

实现要点：

- 新增 API support matrix，字段至少包含：
  `configurable`、`runtime_detecting`、`evidence_enabled`、`per_camera_gate`、
  `status_reason`。
- 8090 算法卡片读取该矩阵，未生产可用的算法显示为“配置预留/部分可用/未接证据”。
- 文档和测试同步，防止后续回归。

### P1：补齐缺失行为规则

目标：让 `loitering`、`running`、`wall_climb_suspicious` 具备真实 runtime 检测。

实现要点：

- 在 `modules/savant_security/custom/rules/` 添加：
  `loitering.py`、`running.py`、`wall_climb.py`。
- 在 `custom/rules/__init__.py` 导入并注册。
- 补充纯规则测试和 runtime config loader 测试。
- 用导出的 `cameras.midterm.yml` 验证 unknown rule type 不再出现。

### P2：行为算法 evidence parity

目标：让 `crowd_gathering`、`fall`、`chasing` 以及新增行为规则能按需产出
Replay evidence。

实现要点：

- 扩展 event-worker evidence task 初始状态策略。
- 扩展 `RECORDING_EVENT_TYPES` 和 recording policy，避免只录
  `watchlist_hit,intrusion`。
- 确保每类事件 payload 都带齐：
  `source_id`、`camera_id`、`frame_uuid`、`keyframe_uuid`、
  `runtime_epoch_id`、`evidence_policy`。
- 扩展 media-worker sidecar event type 支持和 annotation summary。
- 增加每类算法的 smoke：事件产生、record request、Replay job、ready bundle。

### P3：人脸 per-camera runtime gate

目标：让 `face.observation`、`face.watchlist`、`face.live_search` 真正受 8090
per-camera 算法规则控制。

实现要点：

- Savant face observation exporter 读取 camera rule enabled 状态。
- face-worker 按 camera rule 过滤 watchlist 匹配。
- watchlist evidence policy 改为读取 camera rule，而不是只用 face-worker 默认值。
- 实现 live search 查询/目标状态、事件 `live_search_hit`、证据策略和 API 查询。
- 增加端到端 smoke：注册人脸、配置 per-camera watchlist/live_search、产生事件、
  生成证据。

### P4：证据稳定性验收

目标：降低 `failed`、`duration_guard_failed`、`skipped_by_poc_limit` 比例，并让失败原因可观测。

实现要点：

- 增加 evidence acceptance smoke：
  8090 health、camera export、runtime apply、Replay job、sink alias、raw clip Range、
  `alarm_machine_time`、sidecar ready、duration guard 状态。
- 为 clip-worker 并发限制和 post-Savant anchor 缺失增加运行统计。
- 对 `duration_guard_failed` 保留 fail-closed，但补足诊断入口和可读错误。

## 当前对比基线

后续计划或实现完成后，至少对比以下项目：

- 容器是否仍全部 `Up`，Redis/Savant 是否 `healthy`。
- `doctor_midterm.sh` 是否仍 `runtime_doctor_ok=True`。
- `check_midterm_deployment.sh` 是否仍 16 项通过。
- 8090 health 是否仍 HTTP 200。
- 当前启用摄像头和规则是否符合预期。
- 算法矩阵中每个算法的 `runtime_detecting` 和 `evidence_enabled` 是否有变化。
- 最近 24 小时各算法的 `ready/failed/duration_guard_failed/skipped` 比例是否改善。
- 新增算法是否产生真实事件和 ready bundle，而不是只出配置记录。
