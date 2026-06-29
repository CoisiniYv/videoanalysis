---
type: service-deep-dive
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - services
  - modules
  - runtime
---

# 服务深潜

本页按服务解释职责、输入输出、关键代码入口、常见故障和优化点。读完它，后续 agent 应能知道
“改哪个模块会影响哪条链路”。

## evidence-viewer / 8090

关键路径：

- `services/evidence-viewer/app/main.py`
- 8090 静态 UI 和 `/api/v1/*` 代理；
- `/media/*` 代理；
- 旧 `/api/bundles*` 文件扫描兼容接口。

职责：

- 用户唯一主入口；
- 摄像头管理页面；
- ROI / rule 编辑；
- 人员注册；
- evidence list/detail；
- runtime status/performance/topology 控制入口；
- storage maintenance UI。

设计要求：

- 对用户显示中文友好的 camera name、zone name、rule name；
- 不把 source id / camera id 当成主要展示；
- 保存配置和受控重启必须明确区分；
- evidence 页面应从 DB-backed evidence API 读取，不应依赖旧文件扫描语义。

常见坑：

- 8090 页面状态不是容器真实运行状态；
- 旧 evidence 文件兼容接口不能作为新证据链权威；
- 算法/ROI 保存如果误走 runtime apply，会影响取证和用户体验。

## api

关键路径：

- `services/api/app/main.py`
- `services/api/app/routers/cameras.py`
- `services/api/app/routers/runtime.py`
- `services/api/app/routers/people.py`
- `services/api/app/routers/evidence.py`
- `services/api/app/services/runtime_apply.py`
- `services/api/app/services/runtime_performance.py`
- `services/api/app/services/runtime_topology.py`
- `services/api/app/services/savant_supervisor.py`

职责：

- 业务 API；
- 摄像头/规则/ROI DB 写入；
- 导出 Savant camera YAML 和 source manifest；
- runtime apply / stop / restart；
- performance config 保存和应用；
- topology plan 保存和应用；
- people/face registration；
- evidence DB-backed 查询；
- storage maintenance。

输入输出：

```text
8090 UI
  -> API routers
  -> PostgreSQL
  -> generated config
  -> Docker compose / dynamic containers
```

关键边界：

- API 的 8000 端口只应在 compose 网络内使用；
- 用户入口是 8090；
- API 负责把 DB 状态导出成运行时快照，但 DB 才是 truth；
- runtime apply 必须走 evidence guard。

## replay-service

职责：

- 全速接入视频；
- RocksDB 存储；
- 为 analysis-forwarder 提供分析分支；
- 为 clip-worker 提供取证时间窗和 Replay job。

重要语义：

- Replay 是证据时间窗权威；
- analysis-forwarder 的采样不会影响 Replay 已存全速视频；
- 证据前后 5s/自定义时长来自 Replay job，而不是 forwarder 输出。

常见问题：

- replay job payload 和 Replay 版本不兼容会导致 fallback；
- constant-cadence 请求是 midterm 默认，避免旧 fallback 成常态；
- 多 replay shard 时，clip-worker 必须把 source 路由到正确 replay/video-file-sink 分支。

## source-adapter / dynamic source

职责：

- RTSP -> Replay；
- 静态 compose source 和动态 `video-analytics-source-*` 都存在；
- 根据 8090 DB 导出的 source manifest 启停。

关键配置：

- RTSP transport；
- wallclock timestamps；
- generated source manifest；
- enabled/disabled source 状态。

排障重点：

- source 容器是否 exited；
- negative PTS 是否增长；
- RTSP 服务端是否扛得住 60 路；
- `primary_rtsp` disabled 时，compose source exited 不一定是故障。

## analysis-forwarder

关键路径：

- `services/analysis-forwarder/app/main.py`
- `services/analysis-forwarder/app/sampler.py`

职责：

- 从 Replay 读取分析分支；
- 按 PTS/FPS 降采样；
- 用 bounded queue 保护进程；
- 写 Savant ZeroMQ source；
- 暴露 Prometheus metrics。

关键指标：

- seen frames；
- forwarded frames；
- dropped frames；
- send failures；
- queue depth；
- effective input/output FPS。

重要判断：

- queue 长期满且 send failures/阻塞，通常说明下游 Savant 消费不够快；
- null sink 通过但接 Savant 失败，说明 forwarder 自身不是主瓶颈；
- 16/1 入口压力不等于 16 FPS 推理通过。

## savant-security

关键路径：

- `modules/savant_security/module.yml`
- `modules/savant_security/pyfuncs/*`
- `modules/savant_security/config/cameras.midterm.yml`

主要阶段：

```text
PtsFpsGate
  -> yolo26_pose
  -> nvtracker
  -> behavior_rules
  -> yolov8_face
  -> face_person_associator
  -> adaface
  -> face_reid_gate
  -> exporters
```

职责：

- 执行 DeepStream/Savant 推理；
- 根据 camera rules 产生行为事件；
- 导出人脸、人形、frame annotation；
- 提供性能指标。

调优参数：

- `BATCH_SIZE`
- `POSE_BATCH_SIZE`
- `FACE_DETECTOR_BATCH_SIZE`
- `FACE_EMBEDDING_BATCH_SIZE`
- `MAX_PARALLEL_STREAMS`
- `MAX_FPS`
- `MIN_FPS`
- `FACE_INFER_INTERVAL`
- `FACE_EMBEDDING_INFER_INTERVAL`
- `FACE_REID_MIN_INTERVAL_MS`

未闭环风险：

- 缺少 pose/face/AdaFace/pyfunc 阶段级 latency；
- 只看整体 effective FPS 不足以判断卡在哪一段；
- 单路测试因为 `max_same_source_frames=1` 可能凑不满 batch，不能外推 60 路。

## event-worker

关键路径：

- `services/event-worker/app/worker.py`
- `services/event-worker/app/repository.py`
- `services/event-worker/app/record_request.py`

职责：

- 消费 `security.events`；
- 写 `events`；
- 执行 cooldown / suppression / admission；
- 创建 `evidence_tasks`；
- 发布 `security.record_requests`；
- 写 alerts。

优化点：

- record request 去重从 `XRANGE - +` 改成 Redis `SET NX EX`；
- duplicate/retry/reclaim 有 harness；
- admission 按 global/source/event_type 控制证据风暴；
- cooldown 按 source/event_type 分离，watchlist hit 不受 intrusion cooldown 影响。

常见问题：

- admission skip 大量出现不等于系统失败，要看 retained evidence 是否达标；
- 如果要求每个事件都生成完整证据，当前设计就需要重新扩容；
- duplicate task 不能留下长期 pending。

## face-worker

关键路径：

- `services/face-worker/app/worker.py`
- `services/face-worker/app/vector_store.py`
- `services/face-worker/app/watchlist_emitter.py`

职责：

- 消费 `security.face_observations`；
- 校验 embedding；
- 写 `face_observations`；
- 查询 gallery/watchlist；
- 产生 `watchlist_hit`；
- 写 `match_results`。

当前瓶颈风险：

- 单 consumer loop；
- DB insert 和 pgvector 查询同步执行；
- 生产大图库下 exact search 可能成为 p95/p99 热点；
- ANN/EXPLAIN/阈值正确性验证仍缺。

下一步优化应先观测：

- gallery query p95/p99；
- watchlist query p95/p99；
- insert latency；
- batch size；
- Redis lag；
- `EXPLAIN ANALYZE` 在 100/500/1000 人图库下的 plan。

## clip-worker

关键路径：

- `services/clip-worker/app/worker.py`
- `services/clip-worker/app/replay_shards.py`

职责：

- 消费 `security.record_requests`；
- claim / reclaim pending；
- 等待 post-Savant frame proof；
- 调 Replay job；
- 更新 `evidence_tasks`；
- 处理 stale/missing DB 事件；
- 根据 replay shard 路由到正确 Replay/video-file-sink。

关键配置：

- 全局并发；
- per-source 并发；
- per-shard 并发；
- replay shard JSON / config path；
- evidence 前后窗口；
- constant-cadence Replay payload。

常见问题：

- Redis pending 指向已不存在 DB event/task 时，必须 `XACK`，否则空转；
- proof 缺失会导致 replay job 长期无法建立；
- 双分支时 shard plan 不一致会让证据请求打到错误 Replay。

## video-file-sink

职责：

- 接收 Replay job 输出；
- 写 `raw_clip.mov` 和相关 sink metadata；
- 为 media-worker 提供扫描输入。

排障重点：

- sink 输出目录是否有新文件；
- replay epoch 是否匹配；
- 多分支时 source 是否落到正确 sink；
- raw clip 是否能 ffprobe/播放。

## media-worker

关键路径：

- `services/media-worker/app/worker.py`
- `services/media-worker/app/post_savant_evidence_bundle.py`
- `services/media-worker/app/post_savant_video_integrity.py`
- `services/media-worker/app/snapshot.py`

职责：

- 扫描 video-file-sink 输出；
- 校验 raw clip；
- 生成 snapshot / annotation / manifest；
- 写 DB-backed evidence bundle/artifacts/timeline/overlay；
- 终态化 `evidence_tasks`；
- 执行 cleanup。

优化点：

- ffmpeg/ffprobe 已进入镜像，避免 imageio fallback；
- deadline-aware pacer；
- CPU/native thread limit；
- ffmpeg output-side thread limit；
- priority + deadline ordering；
- pressure report drain 后刷新日志，避免只看压力前半段。

当前边界：

- 单进程轮询模型已经通过 8 FPS pressure profile；
- 不要过早上多容器 claim；
- 只有真实 RTSP soak 下 lifecycle p95/p99 超 300s 或要求全事件物化时，再升级 finalizer 模型。

## PostgreSQL

职责：

- 配置事实源；
- 业务事件事实源；
- 人脸图库和 pgvector 查询；
- evidence metadata 和查询索引；
- audit。

性能重点：

- `events` recent/list/cooldown 查询；
- `evidence_tasks` pending/materialization 队列；
- `evidence_bundles` playable recent；
- `face_observations` 和 `person_gallery_embeddings` vector query；
- autovacuum/dead tuples。

## Redis

职责：

- streaming message bus；
- consumer groups；
- bounded frame annotation；
- record request queue。

健康判断：

- `XLEN`；
- `XINFO GROUPS` lag；
- `XPENDING`；
- used memory / peak memory；
- rejected connections / evicted keys。

结论：

- Redis/PG 都在热路径，但最近一次调查没有证明“Redis 把 PG 写爆”是证据不完整主因；
- 证据完整率更依赖 admission、proof、Replay job、media finalization 和 annotation metadata 对齐。
