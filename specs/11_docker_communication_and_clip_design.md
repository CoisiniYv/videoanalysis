# Docker Compose 容器通信、事件流与 Savant Replay 报警录像设计 v0.3

建议保存路径：

```text
video-analytics/specs/11_docker_communication_and_clip_design.md
```

---

# 1. 文档目标

本文档统一说明本项目中 Docker、Docker Compose、容器通信、Redis Streams、event-worker、face-worker、PostgreSQL、媒体文件字段、告警大屏推送，以及基于 Savant Replay Service 的报警前后视频片段录制方案。

当前最终决策：

```text
MVP 阶段先跑通完整事件闭环，不实现复杂报警片段录制。
录像相关能力先保留字段、状态、任务流和接口。
跑通 MVP 后，优先用 Savant Replay Service 实现报警前 5 秒 + 后 5 秒片段。
```

原因：

```text
1. 事件检测、入库、查询、告警是第一优先级。
2. Savant 官方已经提供 Replay Service，可用于短时缓存、回放和重推。
3. 不希望 MVP 被录像功能拖慢。
4. 不采用“同一路摄像头双路拉流 + 持续切片落盘”作为默认正式方案。
5. 不优先自研 DeepStream / GStreamer 环形缓存，除非 Replay 无法满足要求。
```

---

# 2. Docker 基础概念

## 2.1 Image，镜像

镜像是静态模板，可以理解为“程序运行环境安装包”。

示例：

```text
redis:7
pgvector/pgvector:pg16
ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1
ghcr.io/insight-platform/savant-adapters-gstreamer:latest
```

镜像本身不运行，必须启动后才会变成容器。

## 2.2 Container，容器

容器是镜像运行起来后的实例。

```text
image
  -> docker run / docker compose up
    -> container
```

本项目中：

```text
redis 容器负责 Redis Streams
postgres 容器负责 PostgreSQL + pgvector
api 容器负责 FastAPI
event-worker 容器负责事件入库和告警派发
face-worker 容器负责人脸向量检索和命中事件生成
savant-gpu0 / savant-gpu1 容器负责 GPU 推理
replay-service 容器后续负责短时缓存和重推
video-file-sink 容器后续负责 Replay 输出落盘
media-worker 容器后续负责整理媒体路径和回写数据库
```

## 2.3 一个 image 可以启动多个 container

两个 Savant 容器可以使用同一个镜像，但绑定不同 GPU、处理不同摄像头分组。

```text
savant-gpu0 -> GPU 0
savant-gpu1 -> GPU 1
```

## 2.4 Volume / Bind Mount，挂载目录

数据库、模型文件、Replay RocksDB、截图、短视频和日志必须挂载到宿主机硬盘。

推荐宿主机目录：

```text
/data/video-analytics/
  models/
  downloads/
  engines/
    rtx4090/
    t4/
  media/
    snapshots/
    clips/
    replay-sink-output/
  replay/
    rocksdb/
  postgres/
  redis/
  logs/
```

---

# 3. 容器通信原则

## 3.1 通过服务名通信

Docker Compose 会为同一个项目创建内部网络。容器之间通过服务名通信，不需要固定 IP。

```text
api -> postgres:
postgresql://video:video@postgres:5432/video_analytics

api -> redis:
redis://redis:6379/0

savant-gpu0 -> redis:
redis://redis:6379/0

clip-worker -> replay-service:
http://replay-service:8080
```

## 3.2 localhost 注意事项

容器内的 `localhost` 是容器自己，不是宿主机，也不是其他容器。

错误：

```text
api 容器使用 localhost:5432 连接 PostgreSQL
```

正确：

```text
api 容器使用 postgres:5432 连接 PostgreSQL
```

---

# 4. Savant 官方组件边界

Savant 官方架构中，module 通常不直接对接外部摄像头、文件系统和外部存储，而是通过 adapter 完成输入输出。

本项目建议：

```text
RTSP Source Adapter
  -> Replay Service，后续启用
  -> Savant Module
  -> Redis / JSON metadata / Video File Sink，按阶段启用
```

组件职责：

```text
Source Adapter:
  负责 RTSP / 文件 / 队列输入。

Savant Module:
  负责实时推理、tracker、PyFunc、metadata、事件生成。

Replay Service:
  负责保存最近 N 秒视频流与 metadata，并在事件触发后重推。

Video File Sink Adapter:
  负责把 Replay 重推出来的视频写成 metadata.json + video.{mov,webm}。

media-worker:
  负责把 sink 输出整理成业务 clip_path / snapshot_path。
```

---

# 5. 服务职责划分

## 5.1 savant-gpu0 / savant-gpu1

职责：

```text
1. 接收 adapter 输入。
2. 执行 YOLO26-pose 推理。
3. 使用 nvtracker 生成 track_id。
4. 执行轻量行为规则。
5. 估计 person/head ROI。
6. 执行 SCRFD_2.5G 人脸检测。
7. 执行 ArcFace embedding 提取。
8. 将行为事件或人脸 observation 写入 Redis Streams。
```

边界：

```text
Savant 不直接写 PostgreSQL。
Savant 不直接调用 FastAPI。
Savant 不在 MVP 阶段处理复杂报警录像。
Savant 不执行慢速 pgvector 检索。
Savant 不直接生成最终业务 clip_path。
```

## 5.2 redis

职责：

```text
1. 接收 Savant 输出的事件。
2. 接收 Savant 输出的人脸 observation。
3. 缓冲短时间突发消息。
4. 提供 worker consumer group 消费。
5. 解耦实时推理和后端慢操作。
```

使用 Redis Streams，而不是普通 list 或 pub/sub。

## 5.3 postgres

职责：

```text
1. 存储 cameras、camera_zones、camera_rules。
2. 存储 events、tracks。
3. 存储 persons、person_gallery_embeddings、face_observations。
4. 存储 watchlist_rules、live_search_jobs。
5. 存储 audit_logs。
6. 存储 snapshot_path、clip_path 和 media 状态。
7. 后续可存储 record_requests。
```

PostgreSQL + pgvector 是第一版权威业务数据库。

## 5.4 api

FastAPI 是业务入口。

职责：

```text
1. 摄像头管理。
2. ROI 和规则配置。
3. 事件查询。
4. 人员库管理。
5. 重点人员布控管理。
6. 一键找人任务管理。
7. 轨迹查询。
8. WebSocket 实时告警推送。
9. 系统健康检查。
```

FastAPI 不处在实时推理链路中间。

## 5.5 event-worker

`event-worker` 是事件处理后台服务。

职责：

```text
1. 消费 security.events。
2. 校验 SecurityEvent。
3. 幂等写入 PostgreSQL events 表。
4. 写入 payload.media reserved / pending / not_implemented 状态。
5. 根据事件写入 security.alerts。
6. RECORDING_ENABLED=true 时写入 security.record_requests。
7. ACK Redis 消息。
```

重要边界：

```text
event-worker 不录视频。
event-worker 不拉 RTSP。
event-worker 不等待视频生成。
event-worker 不处理大文件。
```

一句话：

```text
event-worker 是报警事件的“登记员 + 派单员”，不是录像工人。
```

## 5.6 face-worker

职责：

```text
1. 消费 security.face_observations。
2. 写入 face_observations 表。
3. 使用 pgvector 检索 person_gallery_embeddings。
4. 判断 watchlist_hit。
5. 判断 live_search_hit。
6. 将命中事件写回 security.events。
7. 可选写入 security.record_requests。
8. ACK Redis 消息。
```

如果 ArcFace 已在 Savant 内完成，face-worker 默认不需要 GPU。

## 5.7 clip-worker，Replay 阶段

MVP 阶段 clip-worker 可以不启动，或只处理空逻辑。

Replay 阶段职责：

```text
1. 消费 security.record_requests。
2. 读取 event_id、source_id、event_ts_ms、pre_seconds、post_seconds。
3. 如果 keyframe_uuid 缺失，调用 Replay /api/v1/keyframes/find。
4. 调用 Replay REST API 创建 re-streaming job。
5. 设置 offset.seconds = pre_seconds。
6. 设置 stop_condition 覆盖 post_seconds 或最大帧数。
7. 将 Replay 输出送到 Video File Sink Adapter。
8. 更新 record_request 状态。
```

## 5.8 video-file-sink，Replay 阶段

职责：

```text
1. 接收 Replay 重推的视频和 metadata。
2. 写出 metadata.json。
3. 写出 video.mov 或 video.webm。
4. 按 DIR_LOCATION 模板组织输出目录。
```

注意：业务系统不应假设 Video File Sink 直接生成 `{event_id}.mp4`。应由 media-worker 整理最终路径。

## 5.9 media-worker，Replay 阶段

职责：

```text
1. 监听 Replay job / sink 输出完成状态。
2. 找到 metadata.json 和 video.{mov,webm}。
3. 整理或转存为 /media/clips/{event_id}.mp4 或保留原始格式。
4. 可选生成 snapshot。
5. 回写 events.clip_path、events.snapshot_path、payload.media。
6. 写入 security.media_ready。
```

---

# 6. Redis Streams 设计

## 6.1 MVP 必须使用的 stream

```text
security.events
security.face_observations
security.alerts
```

## 6.2 预留 / Replay 阶段 stream

```text
security.record_requests
security.media_ready
```

MVP 阶段可以不写 `security.record_requests`；也可以写但不消费。

## 6.3 security.events

用途：行为事件、重点人员命中事件、一键找人命中事件。

生产者：

```text
savant-gpu0
savant-gpu1
face-worker
```

消费者：

```text
event-worker
```

## 6.4 security.face_observations

用途：清晰人脸 observation，包括 bbox、landmarks、quality、embedding、camera_id、track_id。

生产者：

```text
savant-gpu0
savant-gpu1
```

消费者：

```text
face-worker
```

## 6.5 security.alerts

用途：实时告警推送。

生产者：

```text
event-worker
face-worker，可选
```

消费者：

```text
api / WebSocket alert service
web dashboard
alarm screen
```

## 6.6 security.record_requests

用途：请求生成报警片段。

生产者：

```text
event-worker
face-worker
```

消费者，Replay 阶段：

```text
clip-worker
```

MVP 阶段：

```text
默认不消费。
默认在 events.payload.media 中标记 reserved / not_implemented。
```

## 6.7 security.media_ready

用途：媒体生成完成后通知数据库更新。

生产者，Replay 阶段：

```text
media-worker
```

消费者：

```text
event-worker 或 api 内部订阅服务
```

---

# 7. 行为事件数据流

```text
RTSP frame
  -> RTSP Source Adapter
  -> Replay Service，可选，后续启用
  -> savant-gpu0 / savant-gpu1
  -> YOLO26-pose
  -> person bbox + keypoints
  -> nvtracker track_id
  -> BehaviorRulesPyFunc
  -> SecurityEvent
  -> Redis security.events
  -> event-worker
  -> PostgreSQL events
  -> Redis security.alerts
  -> FastAPI WebSocket
  -> alarm screen
```

MVP 阶段，事件可以没有截图和视频，但必须包含 media 状态。

---

# 8. 行为事件消息结构

Savant 写入 `security.events`：

```json
{
  "schema_version": "1.0",
  "source_event_id": "gpu0:cam_001:t_889:intrusion:1710000000000",
  "producer": "savant-gpu0",
  "gpu_id": 0,

  "event_type": "intrusion",
  "camera_id": "cam_001",
  "source_id": "site_a.gate.cam_001",
  "track_id": "t_889",

  "start_ts_ms": 1710000000000,
  "end_ts_ms": 1710000001200,
  "event_ts_ms": 1710000000000,

  "frame_uuid": null,
  "keyframe_uuid": null,

  "confidence": 0.86,
  "severity": "medium",

  "snapshot_required": true,
  "clip_required": true,

  "payload": {
    "zone_id": "perimeter",
    "inside_ms": 1200,
    "bbox": [100, 200, 60, 180],
    "media": {
      "snapshot_required": true,
      "clip_required": true,
      "snapshot_status": "not_implemented",
      "clip_status": "not_implemented",
      "recording_strategy": "reserved",
      "pre_seconds": 5,
      "post_seconds": 5,
      "source_id": "site_a.gate.cam_001",
      "event_ts_ms": 1710000000000,
      "frame_uuid": null,
      "keyframe_uuid": null
    }
  }
}
```

`source_event_id` 格式：

```text
{producer}:{camera_id}:{track_id}:{event_type}:{start_ts_ms}
```

用途：

```text
1. 防止重复入库。
2. 支持 worker 重试。
3. 支持 Redis pending 消息重新消费。
4. 支持 PostgreSQL ON CONFLICT DO NOTHING。
```

---

# 9. 人脸 observation 数据流

```text
person track
  -> head/person ROI
  -> SCRFD_2.5G
  -> face quality filter
  -> ArcFace embedding
  -> Redis security.face_observations
  -> face-worker
  -> PostgreSQL face_observations
  -> pgvector search
  -> watchlist_hit / live_search_hit
  -> Redis security.events
  -> event-worker
  -> PostgreSQL events
  -> alarm screen
```

人脸命中事件也同样只要求 MVP 入库和告警，视频片段先预留。

---

# 10. event-worker 工作细节

## 10.1 处理循环

```text
while True:
    从 Redis security.events 读取消息
    校验消息格式
    检查 source_event_id 是否已存在
    写入 PostgreSQL events 表
    写入 payload.media reserved / pending / not_implemented 状态
    如需告警，写入 security.alerts
    如 RECORDING_ENABLED=true，可写入 security.record_requests
    成功后 ACK Redis 消息
```

## 10.2 MVP 写入 events 表

示例：

```text
events.id = 1001
source_event_id = gpu0:cam_001:t_889:intrusion:1710000000000
event_type = intrusion
camera_id = cam_001
track_id = t_889
status = new
snapshot_path = null
clip_path = null
payload.media.snapshot_status = not_implemented
payload.media.clip_status = not_implemented
payload.media.recording_strategy = reserved
```

## 10.3 PostgreSQL 幂等写入

```sql
INSERT INTO events (
    source_event_id,
    event_type,
    camera_id,
    source_id,
    track_id,
    person_id,
    severity,
    confidence,
    start_ts,
    end_ts,
    status,
    payload
)
VALUES (...)
ON CONFLICT (source_event_id) DO NOTHING;
```

## 10.4 ACK 策略

```text
1. 数据库写入成功后 ACK。
2. alert 写入成功后 ACK。
3. 如果启用了 record_request，record_request 写入成功后 ACK。
4. 中途失败不 ACK，等待重试。
```

---

# 11. MVP 录像功能保留策略

## 11.1 当前决策

```text
MVP 阶段不实现复杂报警录像。
MVP 只保留录像字段、record_request 流、media 状态和后续扩展接口。
报警事件先做到可检测、可入库、可查询、可告警。
```

## 11.2 MVP 阶段必须保留的字段

`events` 表：

```text
snapshot_path
clip_path
payload
```

`payload.media`：

```json
{
  "media": {
    "snapshot_required": true,
    "clip_required": true,
    "snapshot_status": "not_implemented",
    "clip_status": "not_implemented",
    "recording_strategy": "reserved",
    "pre_seconds": 5,
    "post_seconds": 5,
    "source_id": "site_a.gate.cam_001",
    "event_ts_ms": 1710000000000,
    "frame_uuid": null,
    "keyframe_uuid": null
  }
}
```

## 11.3 大屏展示策略

MVP 阶段：

```text
事件必须立即展示。
截图/视频可以显示“暂未生成”或“功能预留”。
clip_path 可以为空。
snapshot_path 可以为空。
```

大屏文案建议：

```text
视频片段：暂未生成
截图：暂未生成
```

---

# 12. Savant Replay 正式录像方案

## 12.1 目标

实现：

```text
事件前 5 秒 + 事件后 5 秒视频片段
```

但不引入：

```text
1. 第二路 RTSP 拉流。
2. 60 路持续切片落盘。
3. 自研 DeepStream/GStreamer 环形缓存。
```

## 12.2 Replay 工作流

```text
RTSP Source Adapter
  -> Replay Service
      - RocksDB 保存最近 30-60 秒视频与 metadata
      - TTL 自动清理
  -> Savant Module
      - 实时分析并产生事件
  -> Redis security.events
  -> event-worker
      - 写入 events
      - 生成 record_request
  -> clip-worker
      - 查找 keyframe
      - 创建 Replay job
      - offset.seconds = 5
      - stop_condition = 事件后 5 秒或最大帧数
  -> Video File Sink Adapter
      - 输出 metadata.json + video.{mov,webm}
  -> media-worker
      - 整理 clip_path
      - 回写 PostgreSQL
```

## 12.3 Replay keyframe 定位

事件里建议携带：

```text
source_id
event_ts_ms
frame_uuid，可选
keyframe_uuid，可选
```

如果事件已经携带 `keyframe_uuid`，clip-worker 可直接创建 Replay job。

如果没有，clip-worker 应通过 Replay API 查找附近 keyframe：

```text
POST /api/v1/keyframes/find
```

请求需要包含：

```json
{
  "source_id": "site_a.gate.cam_001",
  "from": 1710000000,
  "to": 1710000001,
  "limit": 1
}
```

具体时间窗口可按事件时间向前后扩展。

## 12.4 Replay job 请求结构，示意

```json
{
  "sink": {
    "url": "pub+connect:tcp://video-file-sink:6666"
  },
  "configuration": {
    "send_metadata_only": false,
    "labels": {
      "event_id": "1001",
      "camera_id": "cam_001",
      "source_id": "site_a.gate.cam_001"
    }
  },
  "stop_condition": {
    "frame_count": 150
  },
  "anchor_keyframe": "018f76e3-a0b9-7f67-8f76-ab0402fda78e",
  "anchor_wait_duration": {
    "secs": 1,
    "nanos": 0
  },
  "offset": {
    "seconds": 5.0
  },
  "attributes": []
}
```

说明：

```text
offset.seconds = 从 anchor keyframe 向前回退指定秒数，但实际会从可解码 keyframe 开始。
stop_condition = 控制片段结束条件，可用 frame_count 或 last_frame。
labels = 方便 media-worker 关联 event_id。
```

## 12.5 record_request 结构

```json
{
  "schema_version": "1.0",
  "request_id": "record:events:1001",
  "event_id": 1001,
  "source_event_id": "gpu0:cam_001:t_889:intrusion:1710000000000",
  "camera_id": "cam_001",
  "source_id": "site_a.gate.cam_001",
  "event_ts_ms": 1710000000000,
  "frame_uuid": null,
  "keyframe_uuid": null,
  "pre_seconds": 5,
  "post_seconds": 5,
  "strategy": "savant_replay",
  "status": "pending"
}
```

---

# 13. 不推荐作为正式默认方案

## 13.1 独立 segment-recorder 持续拉第二路 RTSP

不推荐默认采用：

```text
独立 segment-recorder 持续拉第二路 RTSP。
持续把每路视频切片落盘。
```

原因：

```text
1. 同一路摄像头可能被拉两次。
2. 60 路时 RTSP 连接数翻倍。
3. 磁盘持续写入压力增加。
4. 故障面扩大。
5. MVP 复杂度过高。
```

## 13.2 自研 in-pipeline ring buffer

`custom_in_pipeline_ring_buffer` 只作为兜底方案，不作为默认方案。

原因：

```text
1. Savant 官方已有 Replay Service。
2. 自研 GStreamer / DeepStream packet ring buffer 实现复杂。
3. 需要自己处理 keyframe、GOP、封装、时间戳和失败恢复。
```

---

# 14. 外部 NVR / 视频平台方案

如果现场已有 NVR 或视频平台，并且提供可靠回放下载 API，可以作为外部对接方案：

```text
AI 系统只负责识别事件；
事件发生后，根据 camera_id + event_ts 从 NVR 下载前后视频。
```

优点：

```text
不双路拉摄像头。
不在 AI 服务器持续保存视频缓存。
不占用大量本地磁盘 IO。
录像可靠性高。
符合学校已有安防架构。
```

缺点：

```text
需要对接 NVR / 视频平台接口。
需要维护 camera_id 到 NVR channel 的映射。
不同厂商协议差异大。
```

本项目将其命名为：

```text
external_nvr_replay
```

---

# 15. recording_strategy 枚举

建议统一支持：

```text
none:
  不生成视频，只保留事件。

reserved:
  MVP 预留结构，但不实现。

savant_replay:
  使用 Savant Replay Service 生成事件前后片段，推荐正式方案。

external_nvr_replay:
  从学校 NVR / 视频平台下载事件前后视频。

post_event_rtsp_demo:
  报警后从 RTSP 录制 10 秒，仅临时演示。

custom_in_pipeline_ring_buffer:
  自研同流压缩环形缓存，仅作为无法使用 Replay 时的兜底方案。
```

---

# 16. 环境变量建议

MVP 默认：

```env
RECORDING_ENABLED=false
RECORDING_STRATEGY=reserved
DEFAULT_PRE_SECONDS=5
DEFAULT_POST_SECONDS=5
```

启用 Savant Replay：

```env
RECORDING_ENABLED=true
RECORDING_STRATEGY=savant_replay
REPLAY_API_URL=http://replay-service:8080
REPLAY_CACHE_TTL_SECONDS=60
DEFAULT_PRE_SECONDS=5
DEFAULT_POST_SECONDS=5
MAX_RECORD_SECONDS=15
VIDEO_FILE_SINK_DIR=/media/replay-sink-output/%source_id-%chunk_idx
```

启用外部 NVR：

```env
RECORDING_ENABLED=true
RECORDING_STRATEGY=external_nvr_replay
DEFAULT_PRE_SECONDS=5
DEFAULT_POST_SECONDS=5
NVR_API_BASE_URL=http://nvr.example.local
```

---

# 17. Docker Compose 设计骨架，MVP 版

```yaml
services:
  redis:
    image: redis:7
    command: redis-server --appendonly yes
    volumes:
      - /data/video-analytics/redis:/data
    networks:
      - video-analytics-net
    ports:
      - "6379:6379"

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: video_analytics
      POSTGRES_USER: video
      POSTGRES_PASSWORD: video
    volumes:
      - /data/video-analytics/postgres:/var/lib/postgresql/data
      - ./db/migrations:/docker-entrypoint-initdb.d
    networks:
      - video-analytics-net
    ports:
      - "5432:5432"

  api:
    build: ./services/api
    environment:
      DATABASE_URL: postgresql://video:video@postgres:5432/video_analytics
      REDIS_URL: redis://redis:6379/0
      MEDIA_ROOT: /media
    volumes:
      - ./services/api:/app
      - /data/video-analytics/media:/media
    ports:
      - "8000:8000"
    depends_on:
      - postgres
      - redis
    networks:
      - video-analytics-net

  event-worker:
    build: ./services/event-worker
    environment:
      DATABASE_URL: postgresql://video:video@postgres:5432/video_analytics
      REDIS_URL: redis://redis:6379/0
      EVENT_STREAM: security.events
      ALERT_STREAM: security.alerts
      RECORD_REQUEST_STREAM: security.record_requests
      RECORDING_ENABLED: "false"
      RECORDING_STRATEGY: reserved
      DEFAULT_PRE_SECONDS: "5"
      DEFAULT_POST_SECONDS: "5"
      CONSUMER_GROUP: event-workers
      CONSUMER_NAME: event-worker-1
    volumes:
      - ./services/event-worker:/app
      - /data/video-analytics/media:/media
    depends_on:
      - postgres
      - redis
    networks:
      - video-analytics-net

  face-worker:
    build: ./services/face-worker
    environment:
      DATABASE_URL: postgresql://video:video@postgres:5432/video_analytics
      REDIS_URL: redis://redis:6379/0
      FACE_STREAM: security.face_observations
      EVENT_STREAM: security.events
      RECORD_REQUEST_STREAM: security.record_requests
      RECORDING_ENABLED: "false"
      CONSUMER_GROUP: face-workers
      CONSUMER_NAME: face-worker-1
      WATCHLIST_DEFAULT_THRESHOLD: "0.75"
      LIVE_SEARCH_DEFAULT_THRESHOLD: "0.75"
    volumes:
      - ./services/face-worker:/app
      - /data/video-analytics/media:/media
    depends_on:
      - postgres
      - redis
    networks:
      - video-analytics-net

  savant-gpu0:
    image: ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1
    runtime: nvidia
    privileged: true
    environment:
      NVIDIA_VISIBLE_DEVICES: "0"
      MODULE_NAME: security_gpu0
      MODEL_PATH: /models
      DOWNLOAD_PATH: /downloads
      MAX_PARALLEL_STREAMS: "16"
      MAX_FPS: 8/1
      POSE_BATCH_SIZE: "8"
      FACE_BATCH_SIZE: "16"
      ARCFACE_BATCH_SIZE: "16"
      REDIS_URL: redis://redis:6379/0
      EVENT_STREAM: security.events
      FACE_STREAM: security.face_observations
    volumes:
      - ./modules/savant_security:/opt/savant/src/module
      - /data/video-analytics/models:/models
      - /data/video-analytics/downloads:/downloads
      - /data/video-analytics/media:/media
    depends_on:
      - redis
    networks:
      - video-analytics-net

networks:
  video-analytics-net:
    driver: bridge
```

MVP 先启动单 GPU：

```bash
docker compose -f infra/docker-compose.dev.yml up -d postgres redis api event-worker savant-gpu0
```

---

# 18. Docker Compose 设计骨架，Replay 扩展版

Replay 阶段新增服务，具体镜像、命令和端口需要以所选 Savant / savant-rs 版本的官方示例为准，以下只表示职责边界：

```yaml
services:
  replay-service:
    image: ghcr.io/insight-platform/savant-rs:latest
    environment:
      REPLAY_CACHE_TTL_SECONDS: "60"
    volumes:
      - /data/video-analytics/replay/rocksdb:/replay/rocksdb
    networks:
      - video-analytics-net

  video-file-sink:
    image: ghcr.io/insight-platform/savant-adapters-gstreamer:latest
    environment:
      DIR_LOCATION: /media/replay-sink-output/%source_id-%chunk_idx
      CHUNK_SIZE: "0"
      METADATA_JSON_FORMAT: native
    volumes:
      - /data/video-analytics/media:/media
    networks:
      - video-analytics-net

  clip-worker:
    build: ./services/clip-worker
    environment:
      DATABASE_URL: postgresql://video:video@postgres:5432/video_analytics
      REDIS_URL: redis://redis:6379/0
      RECORD_REQUEST_STREAM: security.record_requests
      MEDIA_READY_STREAM: security.media_ready
      RECORDING_ENABLED: "true"
      RECORDING_STRATEGY: savant_replay
      REPLAY_API_URL: http://replay-service:8080
      DEFAULT_PRE_SECONDS: "5"
      DEFAULT_POST_SECONDS: "5"
      MAX_RECORD_SECONDS: "15"
    volumes:
      - ./services/clip-worker:/app
      - /data/video-analytics/media:/media
    networks:
      - video-analytics-net

  media-worker:
    build: ./services/media-worker
    environment:
      DATABASE_URL: postgresql://video:video@postgres:5432/video_analytics
      REDIS_URL: redis://redis:6379/0
      MEDIA_READY_STREAM: security.media_ready
      MEDIA_ROOT: /media
    volumes:
      - ./services/media-worker:/app
      - /data/video-analytics/media:/media
    networks:
      - video-analytics-net
```

---

# 19. MVP 验收标准

MVP 不验收真实视频片段生成，验收以下内容：

```text
1. Savant module 可启动。
2. 单路视频 YOLO26-pose 能检测人和关键点。
3. nvtracker 能输出 track_id。
4. 行为规则能产生 intrusion / loitering / crowd_gathering / fall 初版事件。
5. Savant 能写入 security.events。
6. event-worker 能消费 security.events。
7. event-worker 能把事件写入 PostgreSQL events 表。
8. FastAPI 能查询最近事件。
9. security.alerts 能供大屏或 WebSocket 使用。
10. events 表包含 snapshot_path / clip_path 字段，但允许为空。
11. payload.media 标记 recording reserved / not_implemented。
12. 事件消息已兼容 source_id、event_ts_ms、frame_uuid、keyframe_uuid 字段。
```

---

# 20. Replay 报警录像验收标准

后续单独做报警录像时，再验收：

```text
1. record_requests 能生成。
2. clip-worker 能调用 Replay keyframes/find。
3. clip-worker 能创建 Replay job。
4. Replay job 使用 offset.seconds = 5。
5. 可生成报警前 5 秒 + 后 5 秒片段。
6. Video File Sink 能写出 metadata.json + video 文件。
7. media-worker 能整理为业务 clip_path。
8. events.clip_path / snapshot_path 能被更新。
9. 大屏能播放事件视频。
10. 录像失败不影响事件本身告警。
```

---

# 21. 故障处理策略

## 21.1 Redis 短暂不可用

Savant 侧：

```text
1. 不无限阻塞。
2. 本地有界队列短暂缓冲。
3. 队列满时丢弃低优先级事件或输出 overload log。
4. 高优先级事件尽量保留。
```

## 21.2 PostgreSQL 短暂不可用

worker 侧：

```text
1. 不 ACK Redis 消息。
2. 等 PostgreSQL 恢复后重试。
3. 通过 source_event_id 保证重复消费不重复入库。
```

## 21.3 Replay 不可用

```text
1. 不影响 events 入库。
2. 不影响 security.alerts 推送。
3. record_request 标记 failed 或保持 pending 等待重试。
4. payload.media.clip_status 标记 failed。
5. 大屏仍显示事件，只是视频不可用。
```

## 21.4 Video File Sink 失败

```text
1. Replay job 可重试。
2. media_status 标记 failed。
3. 记录 sink_output_path 和错误信息。
4. 不删除原始事件。
```

---

# 22. 安全边界

开发环境可以暴露：

```text
api: 8000
postgres: 5432
redis: 6379
grafana: 3000
prometheus: 9090
```

生产建议：

```text
1. 只暴露 api / web / grafana 管理端口。
2. PostgreSQL 不对外暴露。
3. Redis 不对外暴露。
4. Replay REST API 不对外暴露，只允许内部网络访问。
5. RTSP 密码不写入 Git。
6. 使用 .env 或密钥管理。
7. FastAPI 对外接口必须加认证。
```

---

# 23. 生产 Single-Ingestion 约束与当前 Phase 3F0 临时拓扑

本节是 Phase 3F0 诊断后新增的架构边界文档，后续任何涉及视频拓扑的实现必须遵守。

## 23.1 当前错误拓扑（Phase 3B compose 实际状态）

```
testVideo/test.mp4
  -> ffmpeg-source -> RTSP -> Savant (uridecodebin)
  -> source-adapter(video_loop.sh) -> ZMQ -> Replay Service
```

问题：
- 两个独立文件循环，不同步。
- Savant 的 bbox 来自循环 A 的帧，Replay clip/snapshot 来自循环 B 的帧。
- bbox 绘制位置正确但帧内容不匹配 — 视觉上看起来"bbox 不准"，实际是**帧内容不对**。

## 23.2 临时改良拓扑（Phase 3F0.3a 提议，未实施）

```
test.mp4 -> ffmpeg-source -> RTSP server
       -> Savant (uridecodebin)
       -> source-adapter(rtsp.sh) -> Replay Service
```

- 将 source-adapter 从 `video_loop.sh`（读文件）改为 `rtsp.sh`（消费同一 RTSP）。
- 消除双文件独立循环，Savant 和 source-adapter 都消费同一个 RTSP 流。
- **仍然是双消费者**：只能用于 MVP 验证，不能声称帧级对齐。
- **该拓扑仍然有双消费者问题，只能用于 MVP 验证。**

## 23.3 生产目标拓扑（Single-Ingestion）

```
RTSP camera -> Savant Source Adapter -> Replay Service -> Savant Module
                                                       -> video-file-sink
```

- 每路摄像头只接入一次（single-ingestion）。
- Adapter 是唯一入口，Savant 和 Replay 消费同一条帧流。
- 详见 `specs/01_architecture.md` Section 8 和 `docs/production_ingestion_topology_policy.md`。

## 23.4 关键架构事实

| 事实 | 来源 | 影响 |
|---|---|---|
| video-file-sink metadata.objects=[] 在当前 Replay-bypass-Savant 架构下是预期现象 | Phase 3F0 诊断 | bbox 不能从 metadata.json 获取 |
| 真实 bbox 当前来自 Redis event payload | `behavior_event_export_probe.py` | bbox 由 Savant 写入，与 replay metadata 无关 |
| 当前 `from_ns`/`to_ns` 被显式丢弃，keyframe lookup 为 unbounded | `replay_client.py:68-72` | 无法按 event_ts_ms 精确定位 keyframe |
| `frame_uuid` / `keyframe_uuid` 硬编码为 `None` | `behavior_event_export_probe.py:242-243` | 无法做帧级精确对齐 |
| Savant 当前使用 `uridecodebin` 直读 RTSP，偏离官方 adapter-driven 模式 | `module.yml` | 需工程化回归官方 ZMQ source 模式 |

## 23.5 对齐条件

如果需要生产级 bbox/snapshot 精确对齐，必须满足：

1. **Single-ingestion 拓扑** — Savant 和 Replay 消费同一条帧流。
2. **Timestamp-domain mapping** — Savant NTP/PTS ↔ Replay pipeline-relative 时间戳映射。
3. **帧级标识** — `frame_uuid` / `keyframe_uuid` / `frame_num` / `pts` 等字段在 event payload 中正确填充。

不能只依赖 `payload.bbox`（坐标正确但帧内容可能不对）。

## 23.6 禁止拓扑

```
禁止：同一路摄像头 -> Savant (独立 RTSP 拉流)
     同一路摄像头 -> Replay (独立 RTSP 拉流)
```

这是"双路拉流"的变体，无论使用的是 RTSP 还是文件循环，都禁止作为生产方案。

---

# 24. 最终总结

MVP 阶段：

```text
Savant 只做实时推理和事件输出；
Redis Streams 做容器间事件总线；
event-worker 做事件入库和告警派发；
PostgreSQL + pgvector 做权威数据源；
FastAPI 做业务入口和大屏推送；
报警录像只保留字段、状态和接口，不实现复杂录制。
```

后续阶段：

```text
优先使用 Savant Replay Service 生成事件前后视频；
如果学校 NVR / 视频平台接口成熟，可以作为 external_nvr_replay 方案；
不把“双路拉流 + 持续切片落盘”作为默认正式方案；
不优先自研 GStreamer 环形缓存。
```

一句话：

```text
先让系统会报警、能入库、能查询；
再用 Savant Replay 让系统能回放报警前后视频。
```
