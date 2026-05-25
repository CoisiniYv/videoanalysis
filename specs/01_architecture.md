# 01_architecture.md

## 1. 总体架构

系统采用实时视频分析主干 + 异步事件处理 + 业务 API + 报警大屏的结构。

```text
60 RTSP cameras
  -> Savant RTSP Source Adapters
  -> Savant Replay Service，可选，后续用于短时缓存和重推
  -> Savant Module GPU0 / GPU1
  -> YOLO26-pose + nvtracker + behavior rules
  -> SCRFD_2.5G + ArcFace + face intelligence
  -> Redis Streams
  -> event-worker / clip-worker / face-worker / media-worker
  -> PostgreSQL + pgvector
  -> FastAPI
  -> Web dashboard / alarm screen / live search UI
```

## 2. 组件职责

### 2.0 Savant Source Adapter / Replay / Sink Adapter

Savant 官方架构中，module 通常不直接对接外部摄像头、文件和存储系统，而是通过 adapter 完成输入输出。

本项目建议拆成：

```text
RTSP Source Adapter
  -> Replay Service，后续启用，用于保留最近 30-60 秒视频与 metadata
  -> Savant Module
  -> JSON / Redis / Video File Sink Adapter，按阶段启用
```

MVP 阶段可以先不启用 Replay，只跑通 source adapter -> module -> Redis 事件输出。

后续报警录像阶段启用 Replay，用于“事件前 5 秒 + 事件后 N 秒”的片段生成。

### 2.1 Savant Module

Savant 是实时视频 AI 主干。每张 T4 GPU 建议运行一个 Savant module。

职责：

- 接收 source adapter 输入。
- 调用 YOLO26-pose TensorRT engine。
- 调用 DeepStream nvtracker。
- 执行轻量行为规则 PyFunc。
- 执行人脸 ROI 选择。
- 调用 SCRFD_2.5G 和 ArcFace。
- 生成结构化事件消息。
- 将事件写入 Redis Streams。

非职责：

- 不直接长期存储业务数据。
- 不做复杂向量检索和业务查询。
- 不做报警大屏逻辑。
- 不同步保存大文件。

### 2.2 Redis Streams

Redis Streams 是事件中转层。

作用：

- 解耦 Savant 和后端 worker。
- 防止数据库、截图、告警推送拖慢视频 pipeline。
- 支持多个 worker 消费不同类型事件。
- 支持削峰和短暂缓冲。

建议 stream：

```text
security.events
security.face_observations
security.clip_tasks
security.alerts
```

### 2.3 Workers

worker 负责慢操作和业务持久化。

#### event-worker

- 消费 `security.events`。
- 幂等写入 PostgreSQL。
- 维护事件状态。
- 生成告警消息。

#### clip-worker

MVP 阶段：

- 不直接录视频。
- 不拉第二路 RTSP。
- 可生成或排队 `record_request`，但默认不消费。

Replay 阶段：

- 根据事件中的 `source_id`、`event_ts_ms`、`frame_uuid` 或 `keyframe_uuid` 定位 Replay 数据。
- 调用 Replay REST API 创建 job。
- 设置 `offset.seconds = DEFAULT_PRE_SECONDS`。
- 设置 stop condition 覆盖事件后置时长。
- 将 Replay 输出送到 Video File Sink Adapter。
- 生成完成后回写 `snapshot_path` 和 `clip_path`。

#### face-worker

第一版可以可选。如果 ArcFace 在 Savant 内完成，face-worker 主要负责：

- face_observation 入库。
- pgvector 检索。
- watchlist 告警生成。
- live_search 命中生成。

### 2.4 PostgreSQL + pgvector

PostgreSQL 是权威业务数据库。pgvector 是第一版向量检索方案。

存储：

- cameras。
- camera_zones。
- events。
- tracks。
- persons。
- person_gallery_embeddings。
- face_observations。
- watchlist_rules。
- live_search_jobs。
- audit_logs。

### 2.5 FastAPI

FastAPI 是业务后端。

职责：

- 提供 REST API。
- 提供 WebSocket 实时告警推送。
- 管理摄像头、ROI、规则、人员库。
- 创建和停止一键找人任务。
- 查询事件、轨迹、人脸出现记录。
- 服务报警大屏和后台管理系统。

### 2.6 报警大屏

报警大屏是业务前端，不等同于 Grafana。

展示内容：

- 最新告警。
- 摄像头名称和位置。
- 抓拍图。
- 视频片段。
- 重点人员姓名和匹配分数。
- 一键找人命中位置。
- 处理状态。

### 2.7 Prometheus + Grafana

Prometheus + Grafana 是运维监控，不是业务报警大屏。

监控：

- GPU 使用率。
- GPU 显存。
- 每路 FPS。
- 事件延迟。
- Redis 队列积压。
- PostgreSQL 写入延迟。
- Savant module 存活状态。
- RTSP 掉线次数。

## 3. 逻辑数据流

### 3.1 行为事件流

```text
RTSP frame
  -> YOLO26-pose
  -> person bbox + keypoints
  -> nvtracker track_id
  -> BehaviorRulesPyFunc
  -> SecurityEvent
  -> Redis Streams
  -> event-worker
  -> PostgreSQL events
  -> FastAPI WebSocket
  -> alarm screen
```

### 3.2 人脸事件流

```text
person track
  -> head/person ROI
  -> SCRFD_2.5G face detection
  -> face quality filter
  -> ArcFace embedding
  -> face_observation event
  -> Redis Streams
  -> face-worker
  -> pgvector search
  -> watchlist_hit / live_search_hit / face_observed
  -> PostgreSQL
  -> FastAPI WebSocket
  -> alarm screen
```

### 3.3 一键找人流

```text
user inputs person name
  -> FastAPI searches persons
  -> FastAPI creates live_search_job
  -> face-worker loads active live_search_jobs
  -> realtime FaceObservation is matched
  -> live_search_hit event
  -> alarm screen shows camera and snapshot
```

## 4. GPU 分配

目标服务器有 2 张 NVIDIA T4。

推荐部署：

```text
savant-module-gpu0: cameras 1-30
savant-module-gpu1: cameras 31-60
```

实际摄像头分配需支持配置化，不要写死。

## 5. 服务通信

| 源 | 目标 | 通信方式 | 说明 |
|---|---|---|---|
| RTSP source adapter | Replay service / Savant module | ZeroMQ / Savant protocol | 视频输入。 |
| Replay service | Savant module / Video File Sink | ZeroMQ / REST job control | 后续用于短时缓存和重推。 |
| Savant module | Redis Streams | Redis client | 事件输出。 |
| worker | PostgreSQL | SQL | 事件和向量入库。 |
| FastAPI | PostgreSQL | SQL | 业务查询。 |
| FastAPI | 前端 | REST / WebSocket | 管理和实时告警。 |
| Prometheus | services | metrics scrape | 运维监控。 |

## 6. 架构约束

1. Savant 主流程不得等待业务 API。
2. Redis Streams 的消息必须具备幂等键，例如 `event_id` 或 `source_event_id`。
3. worker 必须允许重试。
4. PostgreSQL 是权威数据源。
5. pgvector 仅是第一版向量实现，必须通过 VectorStore 接口封装。
6. 所有告警必须可审计、可确认、可标记误报。

---

# 7. MVP 报警录像与媒体状态设计补充

MVP 阶段不把报警录像作为阻塞主链路的功能。

事件链路在 MVP 中应按以下方式执行：

```text
Savant -> security.events -> event-worker -> PostgreSQL events -> security.alerts -> FastAPI WebSocket -> alarm screen
```

event-worker 负责事件登记和告警派发，不负责录制视频。

MVP 阶段：

```text
snapshot_path = null
clip_path = null
payload.media.snapshot_status = not_implemented
payload.media.clip_status = not_implemented
payload.media.recording_strategy = reserved
```

报警大屏必须能在没有视频片段时展示事件。视频生成状态可以显示为“暂未生成”。

后续报警片段录制的优先级：

```text
第一优先：Savant Replay Service。它是官方提供的短时缓存、回放和重推服务，适合事件前后片段生成。
第二优先：学校 NVR / 视频平台回放接口。它是外部系统对接方案，适合现场已有成熟录像平台时使用。
第三优先：报警后从 RTSP 录制 10 秒，仅用于演示，不作为生产方案。
不推荐：自研同流压缩环形缓存，除非 Replay 无法满足要求。
```

不建议默认采用独立 segment-recorder 双路拉流和持续切片落盘方案。


---

# 8. Replay 报警录像目标架构

## 8.1 生产目标拓扑

后续启用报警录像时，建议采用以下链路：

```text
RTSP Source Adapter
  -> Replay Service
      - RocksDB 保存最近 30-60 秒视频与 metadata
      - data_expiration_ttl 控制缓存生命周期
  -> Savant Module
      - YOLO26-pose / nvtracker / behavior rules / face intelligence
  -> Redis security.events
  -> event-worker
  -> PostgreSQL events
  -> clip-worker
      - 调 Replay REST API 查找 keyframe 或创建 job
      - offset.seconds = 5
      - stop_condition 覆盖事件后 5 秒或总帧数
  -> Video File Sink Adapter
  -> media-worker
      - 整理 sink 输出目录
      - 回写 events.clip_path / snapshot_path / payload.media
```

事件必须至少保留 `source_id` 和 `event_ts_ms`。如果能从 Savant metadata 中取得 `frame_uuid` 或 `keyframe_uuid`，应一并写入 event payload，以便 Replay 精确定位。

## 8.2 Adapter-Driven Ingestion 原则

官方 Savant adapter 的思路是通过 adapter / ZeroMQ 和 module 通信：

- Adapter 是**独立容器**，负责 RTSP/文件/队列的输入。
- Module 通过 ZeroMQ / Savant protocol 接收 adapter 推送的帧和 metadata。
- Adapter 可以传递视频帧、帧级 metadata、对象和对象 attributes。
- Savant 默认 pipeline source 是 `zeromq_source_bin` / `ZMQ_SRC_ENDPOINT`，而不是 `uridecodebin`。
- Replay Service 作为 upstream/downstream 之间的中间单元，保留最近若干秒到 RocksDB，并通过 REST API 创建 restream job。

当前项目长期目标应回归 **adapter-driven ingestion**，而不是 Savant module 自己独立拉一路 RTSP、Replay 另拉一路 RTSP。

## 8.3 当前阶段已知问题（Phase 3F0 诊断发现）

当前 `infra/docker-compose.phase3b.yml` 使用的拓扑存在严重架构偏差：

```text
当前实际拓扑（错误）：
  testVideo/test.mp4 -> ffmpeg-source -> RTSP -> Savant (uridecodebin)
  testVideo/test.mp4 -> source-adapter -> ZMQ -> Replay Service
```

问题：
1. Savant 读 RTSP、Replay 读 source-adapter 的独立文件循环，导致 bbox 和 snapshot **不在同一帧 / 同一轮循环**。
2. Savant module 使用 `uridecodebin` 直接读 RTSP，偏离了官方 adapter-driven 模式。
3. Replay metadata.objects 始终为空（Replay 绕过 Savant，预期行为）。
4. 真实 bbox 来自 Redis event payload，不来自 replay metadata.json。
5. `frame_uuid` / `keyframe_uuid` 均为 `None`，无法做帧级精确对齐。

**详见 `docs/phase3f0_2_diagnosis_report.md` 和 `docs/phase3f0_3_topology_review.md`。**

## 8.4 临时改良方案（Phase 3F0.3a 提议，仅用于验证）

```text
test.mp4 -> ffmpeg-source -> RTSP server
       -> Savant (uridecodebin)
       -> source-adapter(rtsp.sh) -> Replay Service
```

该方案将 source-adapter 从 `video_loop.sh`（独立读文件）改为 `rtsp.sh`（消费同一 RTSP），消除双文件独立循环问题。

**重要限制：**
- 这是**临时改良方案，不是最终生产方案**。
- 仍然有双消费者问题（Savant + source-adapter 分别消费同一 RTSP），只能减少错位，不能保证帧级对齐。
- RTSP 消费者独立连接，连接时间差异可能造成亚秒级帧不同步。

## 8.5 未来生产方案评估

最终仍需要评估以下 single-ingestion 方案（见 `docs/phase3f0_3_topology_review.md` 方案 A）：

```text
方案 A（理想）：source-adapter -> Replay -> Savant
  - Savant 和 Replay 使用同一条帧流
  - 需要 Savant 支持 ZMQ source（当前不支持，需自研）
  - 匹配 specs/01_architecture.md 的目标架构

方案 B（改良）：source-adapter / bridge adapter tee 到 Replay 和 Savant
  - 单一 adapter 同时输出给 Replay 和 Savant
  - 需要 adapter 支持 tee/multicast 或中间 bridge
```

在 single-ingestion 或 timestamp-domain mapping 完成之前，bbox overlay 不应作为生产视觉验收通过。
