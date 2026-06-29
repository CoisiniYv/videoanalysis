---
type: data-flow-note
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - data-flow
  - runtime
---

# 运行时数据流

## 摄像头配置流

```text
8090 camera page
  -> API /api/v1/cameras/*
  -> PostgreSQL cameras / camera_zones / camera_rules
  -> runtime config sync
  -> cameras.midterm.yml / sources.generated.yml
  -> source adapters / Savant module
```

关键点：

- 摄像头、ROI、算法规则保存不应触发 full runtime apply。
- 保存规则后调用 `/api/v1/cameras/runtime/config/sync`，只同步运行时快照。
- 启停摄像头、变更 RTSP、性能参数或拓扑，才进入受控 runtime apply/restart。

参见 [[04_Control_Plane_8090|8090 控制面]]。

## 视频与推理流

```text
RTSP
  -> source-adapter / dynamic video-analytics-source-*
  -> replay-service RocksDB
  -> analysis-forwarder
  -> Savant ZeroMQ source
  -> yolo26_pose -> nvtracker -> behavior_rules
  -> yolov8_face -> face_person_associator -> adaface
  -> Redis exporters
```

Replay 是全速存储和取证时间窗来源；analysis-forwarder 是分析分支采样器，不是证据权威。

关键队列/参数：

- `ANALYSIS_FPS`
- `MAX_FPS`
- `MIN_FPS`
- `BATCH_SIZE`
- `POSE_BATCH_SIZE`
- `FACE_DETECTOR_BATCH_SIZE`
- `FACE_EMBEDDING_BATCH_SIZE`
- `MAX_PARALLEL_STREAMS`
- `FORWARDER_QUEUE_MAX_SIZE`

## Redis worker 流

```text
security.events
  -> event-worker
  -> events / evidence_tasks / alerts / record_requests

security.face_observations
  -> face-worker
  -> face_observations
  -> gallery/watchlist pgvector query
  -> watchlist_hit event

security.record_requests
  -> clip-worker
  -> Replay job
  -> video-file-sink

security.frame_annotations
  -> clip/media evidence proof and overlay/timeline
```

优化过的点：

- `event-worker` record request 去重从全 stream `XRANGE` 改为 Redis `SET NX EX` 幂等键。
- `clip-worker` 对 stale/缺失 DB 事件的 pending record request 会终态清理并 `XACK`。
- pressure report 固定 Redis、PG、worker、media 和 8090 proof 观测结构。

## Evidence 流

```text
event-worker creates evidence task
  -> clip-worker consumes record request
  -> Replay creates clip job
  -> video-file-sink writes raw clip output
  -> media-worker scans sink output
  -> validates raw clip
  -> writes DB-backed evidence index
  -> 8090 evidence list/detail
```

Evidence 目标不是烧录标注视频，而是：

- 文件系统保留 `raw_clip.mov`；
- PostgreSQL 保存 manifest、timeline、overlay、annotations metadata；
- 8090 从 DB 查询 list/detail，必要时展示 degraded annotation 状态。

详见 [[05_Evidence_Chain|证据链]]。

## Face registration 流

```text
8090 people page
  -> API /api/v1/people
  -> face crop / embedding
  -> persons
  -> person_gallery_embeddings
```

干净迁移不迁旧 PostgreSQL，所以人员和人脸库需要在新机器 8090 重新注册，除非另做业务数据迁移。
