# Video Analytics Platform

多路实时视频分析系统，基于 Savant + NVIDIA DeepStream + TensorRT。

## 当前部署版本

项目机器部署使用中期项目版本，不使用 C1/C2/phase 代号入口。

```bash
docker compose -f infra/docker-compose.midterm.yml config
docker compose -f infra/docker-compose.midterm.yml up -d --build
```

当前部署入口：

| Purpose | File |
|---|---|
| Compose | `infra/docker-compose.midterm.yml` |
| Env | `infra/env/midterm.env` |
| Replay config | `modules/savant_replay/config.midterm.json` |
| Camera config | `modules/savant_security/config/cameras.midterm.yml` |
| Savant module | `modules/savant_security/module.yml` |

默认项目名和容器名前缀是 `video-analytics-midterm`，默认 source id 是
`primary_rtsp`。

## 当前链路

```text
RTSP source
  -> replay-service storage
  -> savant-security inference
  -> Redis / PostgreSQL workers
  -> clip-worker Replay job
  -> video-file-sink raw clip
  -> media-worker JSONL sidecar evidence
  -> evidence-viewer
```

证据包默认包含：

- `raw_clip.mov`
- `sink_metadata.json`
- `annotations.frame_cache.identity.jsonl`
- `summary.frame_cache.identity.json`
- `metadata.json`

## 默认端口

- Redis: `6396`
- Replay API: `8098`
- Evidence viewer: `8090`
- Optional local PostgreSQL profile: `5439`

Workers 默认使用宿主 PostgreSQL：

```text
postgresql://video:video@host.docker.internal:5432/video_analytics
```

## 当前运行参数

`infra/env/midterm.env` 当前固定了单路中期部署的主要阈值：

- `MAX_FPS_CONTROL=true`
- `MAX_FPS=8/1`
- `MIN_FPS=2/1`
- `POSE_INFER_INTERVAL=1`
- `POSE_CONFIDENCE_THRESHOLD=0.50`
- `POSE_KEYPOINT_THRESHOLD=0.35`
- `POSE_SELECTOR_CONFIDENCE_THRESHOLD=0.50`
- `POSE_SELECTOR_NMS_IOU_THRESHOLD=0.50`
- `POSE_MIN_WIDTH=60`
- `POSE_MIN_HEIGHT=100`
- `FACE_CONFIDENCE_THRESHOLD=0.50`
- `WATCHLIST_THRESHOLD=0.60`

## 文档

- 当前部署说明：`docs/midterm_deployment.md`
- Compose 清单：`docs/compose_inventory.md`
- 当前状态：`docs/current_mainline_status.md`
- 历史阶段/实验文档：`docs/`

历史 C1/C2/phase 入口已归档到：

- `infra/archive/phase-only/20260609/`
- `modules/savant_replay/archive/phase-only/20260609/`
- `modules/savant_security/config/archive/phase-only/20260609/`

这些归档文件只用于追溯和历史回归，不作为项目机器部署入口。
