# 07_deployment.md

## 1. 部署目标

第一版采用 Docker Compose 部署在单台服务器上。

服务器配置假设：

```text
CPU: 多线程 CPU
GPU: 2 x NVIDIA T4
OS: Linux
Runtime: Docker + NVIDIA Container Toolkit
```

## 2. 服务列表

```text
savant-gpu0
savant-gpu1
rtsp-source-adapter，按实际 Savant adapter 方案配置，可多实例
replay-service，后续报警录像阶段启用
video-file-sink，后续报警录像阶段启用
redis
postgres
api
event-worker
clip-worker
media-worker，后续报警录像阶段启用
face-worker
prometheus
grafana
web
```

## 3. GPU 分配

```text
savant-gpu0 -> NVIDIA device 0
savant-gpu1 -> NVIDIA device 1
```

摄像头分配：

```text
GPU0: 约 25 到 30 路
GPU1: 约 25 到 30 路
```

实际分配由 cameras 配置决定。

## 4. 目录映射

宿主机建议目录：

```text
/data/video-analytics/
  models/
  downloads/
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

仓库目录：

```text
video-analytics/
  modules/
  services/
  db/
  infra/
  harness/
```

## 5. Docker Compose 骨架

```yaml
services:
  savant-gpu0:
    image: ghcr.io/insight-platform/savant-deepstream:latest
    runtime: nvidia
    privileged: true
    environment:
      - MODULE_NAME=security_gpu0
      - MODEL_PATH=/models
      - DOWNLOAD_PATH=/downloads
      - MAX_PARALLEL_STREAMS=32
      - MAX_FPS=8/1
      - POSE_BATCH_SIZE=8
      - FACE_BATCH_SIZE=16
      - ARCFACE_BATCH_SIZE=16
      - REDIS_URL=redis://redis:6379/0
      - EVENT_STREAM=security.events
    volumes:
      - ./modules/savant_security:/opt/savant/src/module
      - /data/video-analytics/models:/models
      - /data/video-analytics/downloads:/downloads
      - /data/video-analytics/media:/media
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["0"]
              capabilities: [gpu]

  savant-gpu1:
    image: ghcr.io/insight-platform/savant-deepstream:latest
    runtime: nvidia
    privileged: true
    environment:
      - MODULE_NAME=security_gpu1
      - MODEL_PATH=/models
      - DOWNLOAD_PATH=/downloads
      - MAX_PARALLEL_STREAMS=32
      - MAX_FPS=8/1
      - POSE_BATCH_SIZE=8
      - FACE_BATCH_SIZE=16
      - ARCFACE_BATCH_SIZE=16
      - REDIS_URL=redis://redis:6379/0
      - EVENT_STREAM=security.events
    volumes:
      - ./modules/savant_security:/opt/savant/src/module
      - /data/video-analytics/models:/models
      - /data/video-analytics/downloads:/downloads
      - /data/video-analytics/media:/media
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["1"]
              capabilities: [gpu]

  redis:
    image: redis:7
    command: redis-server --appendonly yes
    volumes:
      - /data/video-analytics/redis:/data

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      - POSTGRES_DB=video_analytics
      - POSTGRES_USER=video
      - POSTGRES_PASSWORD=video
    volumes:
      - /data/video-analytics/postgres:/var/lib/postgresql/data
      - ./db/migrations:/docker-entrypoint-initdb.d

  api:
    build: ./services/api
    environment:
      - DATABASE_URL=postgresql://video:video@postgres:5432/video_analytics
      - REDIS_URL=redis://redis:6379/0
      - MEDIA_ROOT=/media
    volumes:
      - /data/video-analytics/media:/media
    ports:
      - "8000:8000"
    depends_on:
      - postgres
      - redis

  event-worker:
    build: ./services/event-worker
    environment:
      - DATABASE_URL=postgresql://video:video@postgres:5432/video_analytics
      - REDIS_URL=redis://redis:6379/0
      - EVENT_STREAM=security.events
    volumes:
      - /data/video-analytics/media:/media
    depends_on:
      - postgres
      - redis

  clip-worker:
    build: ./services/clip-worker
    environment:
      - DATABASE_URL=postgresql://video:video@postgres:5432/video_analytics
      - REDIS_URL=redis://redis:6379/0
      - MEDIA_ROOT=/media
      - RECORDING_ENABLED=false
      - RECORDING_STRATEGY=reserved
      - REPLAY_API_URL=http://replay-service:8080
      - DEFAULT_PRE_SECONDS=5
      - DEFAULT_POST_SECONDS=5
    volumes:
      - /data/video-analytics/media:/media

  prometheus:
    image: prom/prometheus
    volumes:
      - ./infra/prometheus:/etc/prometheus
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana
    ports:
      - "3000:3000"
```

具体镜像 tag 后续必须固定，不允许生产使用 `latest`。

## 6. 环境变量

### 6.1 Savant

```text
MODULE_NAME
MODEL_PATH
DOWNLOAD_PATH
MAX_PARALLEL_STREAMS
MAX_FPS
MIN_FPS
POSE_BATCH_SIZE
FACE_BATCH_SIZE
ARCFACE_BATCH_SIZE
REDIS_URL
EVENT_STREAM
LOGLEVEL
```

### 6.2 API / Worker

```text
DATABASE_URL
REDIS_URL
MEDIA_ROOT
WATCHLIST_DEFAULT_THRESHOLD
LIVE_SEARCH_DEFAULT_THRESHOLD
```

## 7. 模型文件

放置位置：

```text
/data/video-analytics/models/
  yolo26_pose.onnx
  scrfd_2.5g.onnx
  arcface.onnx
```

TensorRT engine 可由 Savant/DeepStream 首次启动构建，也可以离线构建。

建议生产中离线构建并固定：

```text
yolo26_pose_b8_fp16.engine
scrfd_2.5g_b16_fp16.engine
arcface_b16_fp16.engine
```

## 8. 启动顺序

1. 启动 postgres。
2. 执行 db migrations。
3. 启动 redis。
4. 启动 api。
5. 启动 workers。
6. 启动 savant-gpu0/gpu1。
7. 启动 web。
8. 启动 prometheus/grafana。

## 9. 健康检查

必须提供：

```text
api /health
api /ready
worker heartbeat
savant module status
postgres connection check
redis connection check
```

## 10. 日志

日志要求：

- 所有服务输出结构化日志。
- 包含 request_id 或 event_id。
- Savant 事件输出要包含 source_id、camera_id、track_id。
- worker 错误要包含 Redis message id。

## 11. 备份

第一版至少备份：

- PostgreSQL。
- `/data/video-analytics/media`。
- cameras/rules 配置。
- model 文件和 engine 文件。

## 12. 生产注意事项

1. 固定 Savant / DeepStream / TensorRT 镜像版本。
2. 固定 NVIDIA driver 版本。
3. 固定模型版本。
4. 不要在生产使用 `latest`。
5. 不要把 RTSP 密码提交到 Git。
6. 使用 `.env` 或密钥管理保存敏感信息。
7. 对外 API 必须加认证。

---

# 13. MVP 录像部署策略补充

MVP 阶段默认不启动复杂录制服务。

默认启动服务：

```text
postgres
redis
api
event-worker
savant-gpu0
```

单路跑通后再启动：

```text
savant-gpu1
face-worker
prometheus
grafana
web
```

MVP 阶段可配置：

```env
RECORDING_ENABLED=false
RECORDING_STRATEGY=reserved
DEFAULT_PRE_SECONDS=5
DEFAULT_POST_SECONDS=5
```

不默认启用：

```text
segment-recorder
recording-worker
nvr-clip-worker
media-worker
replay-service
video-file-sink
```

后续如果实现报警片段录制，优先启用 `replay-service + video-file-sink + media-worker`。`nvr-clip-worker` 只作为学校已有 NVR / 视频平台的外部对接方案。不要默认增加独立双路拉流的 segment-recorder。


---

# 14. Replay 阶段部署补充

正式启用报警前后视频片段时，新增服务建议：

```text
replay-service
video-file-sink
media-worker
```

推荐环境变量：

```env
RECORDING_ENABLED=true
RECORDING_STRATEGY=savant_replay
REPLAY_API_URL=http://replay-service:8080
REPLAY_CACHE_TTL_SECONDS=60
DEFAULT_PRE_SECONDS=5
DEFAULT_POST_SECONDS=5
VIDEO_FILE_SINK_DIR=/media/replay-sink-output/%source_id-%chunk_idx
```

Replay 工作目录建议挂载到：

```text
/data/video-analytics/replay/rocksdb
```

Video File Sink 输出目录建议挂载到：

```text
/data/video-analytics/media/replay-sink-output
```

`media-worker` 负责把 Video File Sink 的输出目录整理成业务路径，例如：

```text
/data/video-analytics/media/clips/{event_id}.mp4
/data/video-analytics/media/snapshots/{event_id}.jpg
```

注意：Video File Sink Adapter 原生会写出 `metadata.json` 和 `video.{mov,webm}`，业务侧不要假设它会直接生成 `{event_id}.mp4`。
