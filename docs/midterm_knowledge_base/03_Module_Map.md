---
type: module-map
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - modules
  - services
---

# 模块地图

## Compose 与配置

| 模块 | 关键文件 | 职责 |
| --- | --- | --- |
| compose | `infra/docker-compose.midterm.yml` | midterm 服务编排、profiles、env wiring |
| env | `infra/env/midterm.env` | 默认 FPS、batch、worker、evidence、storage 参数 |
| start | `scripts/midterm_start.sh` | 启动入口 |
| package | `scripts/midterm_package_clean.sh` | 干净迁移打包，支持 `--include-images` |
| deploy | `scripts/midterm_deploy_clean.sh` | 干净迁移部署，支持加载 `images.tar` |

## 入口和控制

| 服务 | 关键文件 | 输入 | 输出 |
| --- | --- | --- | --- |
| `evidence-viewer` | `services/evidence-viewer/app/main.py` | 浏览器请求 | 8090 UI、API/media 代理 |
| `api` | `services/api/app/main.py` | 8090/API 请求 | DB 更新、runtime apply、evidence API |
| runtime topology | `services/api/app/services/runtime_topology.py` | topology config | source plan、replay shard plan、branch services |
| runtime apply | `services/api/app/services/runtime_apply.py` | camera DB state | Savant camera YAML、source manifest |

详见 [[04_Control_Plane_8090|8090 控制面]]。

## 视频与推理

| 服务 | 关键文件 | 职责 |
| --- | --- | --- |
| `replay-service` | compose image | 全速 RTSP 存储、取证时间窗 |
| `source-adapter` / dynamic source | Savant adapter image + generated sources | RTSP 到 Replay |
| `analysis-forwarder` | `services/analysis-forwarder/app/main.py` | 从 Replay 读取分析分支、PTS/FPS 采样、写 Savant |
| `savant-security` | `modules/savant_security/module.yml` | DeepStream/Savant 模型链、规则、Redis export |

Savant pipeline 主要阶段：

```text
PtsFpsGate
  -> yolo26_pose
  -> nvtracker
  -> behavior_rules
  -> yolov8_face
  -> face_person_associator
  -> adaface
  -> face_reid_gate
  -> face_observation_exporter
  -> frame_annotation_exporter
```

## Redis workers

| 服务 | 关键文件 | 输入 stream | 输出 |
| --- | --- | --- | --- |
| `event-worker` | `services/event-worker/app/worker.py` | `security.events` | `events`、alerts、evidence tasks、record requests |
| record publisher | `services/event-worker/app/record_request.py` | event id | Redis `security.record_requests` |
| `face-worker` | `services/face-worker/app/worker.py` | `security.face_observations` | `face_observations`、watchlist/gallery events |
| gallery search selector | `services/face-worker/app/gallery_search.py` | embedding + target persons | `pgvector` / `shadow` / `qdrant` / `hybrid` 后端选择 |
| Qdrant gallery store | `services/face-worker/app/qdrant_gallery_store.py` | embedding | Qdrant candidate search + PostgreSQL exact rerank |
| pgvector rollback store | `services/face-worker/app/vector_store.py` | embedding | pgvector exact search / rollback / historical observation search |
| Qdrant sync | `services/face-worker/sync_qdrant_gallery.py` | PostgreSQL gallery rows / outbox | bootstrap、drain、reconcile、status |
| `clip-worker` | `services/clip-worker/app/worker.py` | `security.record_requests` | Replay jobs、evidence task updates |
| replay shards | `services/clip-worker/app/replay_shards.py` | shard map | per-source Replay/video-file-sink routing |

## Evidence finalization

| 服务 | 关键文件 | 职责 |
| --- | --- | --- |
| `video-file-sink` | compose image | 接收 Replay job 输出 raw clip |
| `media-worker` | `services/media-worker/app/worker.py` | 扫描 sink 输出、校验、索引 evidence |
| post-Savant bundle | `services/media-worker/app/post_savant_evidence_bundle.py` | 生成/校验证据 bundle |
| integrity | `services/media-worker/app/post_savant_video_integrity.py` | raw clip decode/integrity 检查 |
| snapshot | `services/media-worker/app/snapshot.py` | 截图/探测辅助 |

## 压测与观测

| 脚本 | 职责 |
| --- | --- |
| `scripts/runtime/run_midterm_pressure60.py` | 60 路压测、topology apply、evidence drain、downstream observability |
| `scripts/runtime/report_evidence_materialization_phase0.py` | evidence materialization 报告 |

压测报告要看：

- `sample_summary.json`
- `downstream_observability_summary.json`
- `db_summary_before_cleanup.json`
- `kept_50_evidence.csv`
- worker logs since start
