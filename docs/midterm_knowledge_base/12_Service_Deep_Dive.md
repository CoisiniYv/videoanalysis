---
type: service-deep-dive
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - services
  - modules
  - runtime
---

# 服务深潜

## evidence-viewer / 8090

关键文件：`services/evidence-viewer/app/main.py`、`static/*.js`。

- 对外监听 8090；
- 提供 `/`、`/operator` 和静态页面；
- 白名单代理 `/api/v1/*`，代理 `/media/*`；
- 保留 `/api/bundles/*` 文件兼容接口；
- 不代理 FastAPI `/docs`，也未实现 WebSocket upgrade。

新功能应优先走 `/api/v1/evidence` 的 DB-backed 合同。

## api

关键模块：routers、`runtime_topology.py`、`runtime_topology_jobs.py`、
`runtime_latency.py`、runtime apply/performance/control。

- DB 配置与查询面；
- camera/ROI/rule export；
- 人脸注册；
- evidence list/detail；
- Docker runtime 编排；
- 40/60 路预设；
- 异步 apply/status；
- latency/overview/maintenance。

topology job 是进程内线程，不是持久队列。状态文件只恢复展示，不恢复执行。

## Replay、raw fanout、analysis-forwarder

- Replay A/B 接收全率 RTSP 并写 RocksDB；
- full preset 的 `replay-raw-fanout-a/b` 同时做 sampled Savant 输出和 raw PUB；
- rolling sink 从 raw PUB 取 sampler 之前的编码帧；
- 单分支由独立 `analysis-forwarder` 采样；
- Replay job API 仍供 clip-worker 兼容路径使用。

不要把 full preset 画成 `raw-fanout -> analysis-forwarder-a/b -> Savant`；当前代码在
full mode 直接把 raw fanout 容器配置成 sampler。

## Savant A/B

```text
PtsFpsGate
  -> yolo26_pose
  -> nvtracker
  -> behavior_rules (events + person observations)
  -> yolov8_face
  -> face_person_associator
  -> face_roi_exporter
  -> in-pipeline adaface/reid/exporters (single path)
  -> frame annotations / metrics
```

完整预设启用 ROI exporter、禁用 in-pipeline AdaFace input 和 face observation export，
把 embedding 移到独立 worker。单分支仍可使用 in-pipeline AdaFace。

## rolling-cache-sink

关键目录：`services/rolling-cache-sink/`。

- 替代 rolling 场景的旧 `video_files.py`；
- 每 source/session 一条 H.264 passthrough GStreamer pipeline；
- splitmux MOV fragment；
- staging 后同文件系统 rename 原子发布；
- `metadata.json` 是 fragment commit marker；
- 暴露 `/metrics`、`/healthz`、`/readyz`；
- 原始 PTS 保留，另建 `rolling_cache_mux_pts`。

retention/byte quota 由单 owner 维护，不能删除 active read pin。

## event-worker

- 消费 `security.events`；
- 持久化 event；
- source+algorithm cooldown；
- 创建 evidence task、alerts；
- 单分支发布 record request；
- full preset 在 prefill 前关闭 task 创建，开放后 suppress record request。

event-worker 不再消费高率人体轨迹，避免策略/DB 工作饿死 person stream。

## person-observation-worker

关键文件：`services/event-worker/app/person_worker.py`、`person_main.py`。

- 复用 event-worker image；
- 独立 consumer group/process；
- 先恢复 pending，再批量读新消息；
- transactional batch 写 `person_bbox_observations`；
- 默认 worker batch 500。

健康检查要单独看该 consumer 的 lag/pending，不能只看 event-worker。

## adaface-roi-worker

- 消费有 TTL 的 112x112 JPEG ROI；
- TensorRT batch 16；
- T4 preset 通过 MPS 10%，4090 preset 不使用 MPS；
- 发布 512 维 face observations；
- 暴露 metrics/health；
- 必须监控 ROI stream lag/pending、expired crop 和 batch occupancy。

## face-worker

- 持久化 face observation；
- 查询注册图库；
- exact match/rerank；
- 写 match_results、轨迹图片；
- 产生 `watchlist_hit` 回到 `security.events`。

当前默认后端是 pgvector。Qdrant 模块、outbox 和 sync worker 是可选能力，只有显式 env
和 profile 生效后才可描述为 authoritative。

## clip-worker

当前结构包含 contracts、request consumer、proof resolver、pure planner、admission
repository、Replay client/executor、request processor 和 coordinator。

- 负责单分支/兼容 record request；
- planner shadow 只比较纯结果；
- Coordinator V2 是默认 side-effect owner；
- Replay slot 使用 owner/token/generation；
- create 前持久化 plan hash/request/delivery；
- ACK 只在 durable outcome 后发生；
- 不恢复 rolling-owned lifecycle。

完整双分支正常运行时不会把它当主物化 worker。

## video-file-sink

- 接收 Replay job 输出；
- 写兼容 raw video/metadata；
- A/B/A–H 实例用于双分支兼容或 shard 压测；
- full rolling path中这些实例不是主 evidence 输出。

## media-worker

核心职责：

- Scheduler V2 非阻塞 dispatch；
- process-lifetime WorkBudget；
- image/remux/finalizer lanes 与 source caps；
- PostgreSQL pool；
- RollingSegmentIndex、row cache、read pins；
- materialization lease/heartbeat/fence；
- durable finalizer handoff；
- finalizer process pool；
- staging/atomic publish；
- bundle/artifact/timeline/overlay transaction；
- terminal state 与 durable cleanup recovery。

常见误判：

- finalizer worker 配置值不等于有效并发，shared WIP 仍是上限；
- queue wait 高不一定是 finalizer pool wait；
- legacy derivative 关闭不等于 DB annotation 关闭；
- segment discovery 慢、read-pin retry、event late arrival要与 remux CPU 分开诊断。

## PostgreSQL / Redis

PostgreSQL 提供状态、配置和索引权威。Redis 提供 delivery。健康必须联合判断：

- DB locks/pool/query/index/autovacuum；
- Redis XLEN/lag/pending/memory/eviction；
- lifecycle active/oldest/lease；
- source/epoch/session identity；
- 文件系统和 DB 是否原子收敛。
