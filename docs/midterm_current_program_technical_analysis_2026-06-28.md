# Midterm 当前程序技术分析报告 - 2026-06-28

## 1. 报告范围

本报告基于当前 checkout 的代码、配置和 2026-06-28 已沉淀的压测文档进行静态技术分析。
未在本次报告编写过程中重启服务、读取 live runtime 指标或重新运行压测。

2026-06-29 同步状态：后续提交已经落地 8090 运行控制、性能配置、拓扑配置、
算法/ROI 配置同步、离线迁移打包，以及 media-worker finalizer 平滑调度相关改动。
当前未提交差异只应被视为运行时生成快照或操作者配置时，不能再把本报告最初列出的
runtime/frontend 文件当作待提交代码风险。当前已知仍可能出现 tracked diff 的文件是
`modules/savant_security/config/cameras.midterm.yml`，它来自 PostgreSQL 摄像头/规则状态导出的
Savant 配置快照。

本文不分析归档阶段文件作为当前部署入口。当前部署入口以 midterm 栈为准：

- 启动脚本：`scripts/midterm_start.sh`
- Compose：`infra/docker-compose.midterm.yml`
- Env：`infra/env/midterm.env`
- Savant module：`modules/savant_security/module.yml`
- 8090 操作台：`http://127.0.0.1:8090/operator`
- 干净迁移打包：`scripts/midterm_package_clean.sh`
- 干净迁移部署：`scripts/midterm_deploy_clean.sh`

## 2. 执行摘要

当前程序已经从早期单点实验演进为一套面向中期交付的实时视频分析系统。核心链路是：

```text
RTSP source
  -> Replay storage
  -> analysis-forwarder sampled branch
  -> Savant inference
  -> Redis streams
  -> event-worker / face-worker
  -> PostgreSQL / Qdrant / pgvector rollback
  -> clip-worker Replay job
  -> video-file-sink raw clip
  -> media-worker evidence indexing/finalization
  -> 8090 operator / evidence APIs
```

系统的产品面已经比较完整：8090 操作台覆盖摄像头配置、ROI/规则、人员和人脸库、证据浏览、
存储维护、运行时状态、性能配置和拓扑配置。后端 API 通过 evidence-viewer 代理暴露到 8090，
内部 API 服务继续只在 compose 网络内监听。

当前最强的运行证据来自 2026-06-28 到 2026-06-29 的压测文档：

- 60 路 3 FPS 下游证据链压测通过，保留 50 条 evidence，50/50 playable，8090 API 可查询。
- 60 路 16/1 配置压测证明高入口压力下证据链能保住样本，但没有证明 16 FPS 推理吞吐。
- 单 4090 同卡双分支 30+30 在 4 FPS 和 8 FPS 档位完成 retained-evidence 证据链验收。
- `pressure60_media_fullobs_8fps_20260629T092901Z` 证明第一阶段 media finalizer 平滑调度可在
  60 路同卡双分支 8 FPS profile 下保留 50/50 playable evidence，并把 media-worker CPU 峰值控制到
  约 98%。

当前主要技术风险不再是“是否能跑通一个告警证据”，而是扩展性和验收边界：

- face-worker 仍在单消费 loop 中同步做 DB insert、规则解析、Qdrant/pgvector 查询和 event publish；
  Qdrant 已解决注册图库向量检索扩展性，但尚未把 persistence 和 matching 解耦。
- 注册图库查询已有 Qdrant 派生索引；历史 `face_observations` 相似检索仍未迁移到 Qdrant，也不属于
  本次 cutover 范围。
- media-worker 仍是单进程轮询 finalizer，第一阶段 deadline-aware pacer 已通过 pressure profile
  验证；如果生产目标变成“所有事件全量物化”，仍需要重新评估 worker pool / 多容器 claim / 独立
  finalizer service。
- 8090 runtime topology 已能写双分支计划，pressure harness 已证明 replay shard 输出和 clip-worker
  读取路径一致；仍缺真实 RTSP 混合输入和长时间 soak。

2026-06-29 后续已修复并应从开放风险降级为回归验证的点：

- event-worker record request 去重已从 `XRANGE security.record_requests - +` 全流扫描改为
  Redis `SET NX EX` 幂等键；重复 record request 会终态跳过新建 retry task。
- pressure60 脚本已新增 `downstream_observability_summary.json`，固定 Redis、PostgreSQL、
  event-worker、face-worker、media-worker 和 8090 retained evidence proof 的报告结构。
- 8090 摄像头算法/ROI 保存不再通过 full runtime apply 重启推理链路；前端保存规则后调用
  `/api/v1/cameras/runtime/config/sync`，只同步 Savant/adapter 配置快照。
- `clip-worker` 对 stale/缺失 DB 事件的 pending record request 会执行终态清理和 `XACK`，
  不应继续长期 reclaim 空转。
- materializing/replay_job_created/finalizing 等 active evidence 状态已纳入 stale 终态收敛检查，
  旧任务不应无限停留在 8090 的“生成中”状态。
- `REPLAY_FORCE_CONSTANT_CADENCE=true` 已作为 midterm 默认行为，Replay 400 后 fallback
  不应再作为常态取证路径；后续继续观察 evidence lifecycle p95/p99，而不是重复排查同一 fallback。
- media-worker 已加入 deadline-aware pacer、CPU/native thread limit、ffmpeg output-side thread limit
  和 pressure 报告 drain 后日志刷新；`imageio_ffmpeg_fallback_count` 已按数值解析，避免把
  `imageio_ffmpeg_fallback_count=0` 误报为 fallback。
- clean-machine 迁移脚本支持 `--include-images` 离线打包 Docker 镜像，部署脚本可自动加载
  `images.tar` 并以 `--no-build` 启动，UOS 迁移步骤已固化。
- face-worker 在线注册人脸图库检索已完成 Qdrant authoritative 切换。PostgreSQL 仍是人员和
  `person_gallery_embeddings` 事实源，Qdrant 是可重建派生索引；60 路 8 FPS Qdrant authoritative
  压测通过，fallback count 为 0，20,000 向量 gRPC benchmark all-search p95/p99 为
  4.037ms/6.427ms。

## 3. 当前部署边界

### 3.1 默认部署形态

默认 compose 项目名是 `video-analytics-midterm`。默认服务包括：

| 层级 | 服务 | 职责 |
| --- | --- | --- |
| 存储/队列 | `redis` | Redis Streams、告警流、frame annotation stream |
| API | `api` | 内部 FastAPI，业务 API、运行时控制、Savant supervisor |
| 前端入口 | `evidence-viewer` | 8090 静态 UI、API/media 代理、旧 evidence 文件兼容接口 |
| 视频回放 | `replay-service` | 全速接入、RocksDB 存储、Replay job source |
| 入口采样 | `analysis-forwarder` | 从 Replay 取分析分支，按 PTS/FPS 采样并写 Savant |
| 推理 | `savant-security` | DeepStream/Savant 模型链、规则和 Redis exporter |
| 源接入 | `source-adapter` 和动态 `video-analytics-source-*` | RTSP 到 Replay |
| 事件处理 | `event-worker` | Redis event 入库、告警、录像请求发布 |
| 人脸处理 | `face-worker` | face observation 入库、Qdrant/pgvector gallery-watchlist 匹配、watchlist hit 事件 |
| 取证调度 | `clip-worker` | 消费 record requests，调用 Replay，写 evidence task 状态 |
| 原始视频 | `video-file-sink` | Replay job 输出 `raw_clip.mov` 相关 sink 文件 |
| 证据最终化 | `media-worker` | 扫描 sink 输出、校验 raw clip、写 DB-backed evidence 索引 |

默认 PostgreSQL 使用宿主机 `host.docker.internal:5432`，compose 内置 `postgres` 仅在
`local-postgres` profile 下启用并映射到主机 `5439`。

### 3.2 默认端口

| 端口 | 服务 |
| --- | --- |
| 6396 | Redis |
| 8090 | 8090 操作台 / evidence-viewer |
| 8098 | Replay API |
| 18080 | Savant metrics |
| 18081 | analysis-forwarder metrics |
| 5439 | 可选 local PostgreSQL profile |

API 服务的 8000 端口只在 compose 网络内暴露，8090 通过 `/api/v1/*` 代理访问。

### 3.3 当前关键运行参数

`infra/env/midterm.env` 和 compose 当前默认运行参数主要是：

| 参数 | 当前值 | 含义 |
| --- | --- | --- |
| `ANALYSIS_FPS` | `8/1` | forwarder 默认采样上限 |
| `MAX_FPS` | `8/1` | Savant PTS gate 默认上限 |
| `MIN_FPS` | `2/1` | Savant PTS gate 最小目标 |
| `BATCH_SIZE` | `1` | 默认 Savant pipeline batch |
| `MAX_PARALLEL_STREAMS` | `4` | 默认 Savant 并行流上限 |
| `FACE_INFER_INTERVAL` | `2` | 人脸检测 interval |
| `FACE_EMBEDDING_INFER_INTERVAL` | `2` | AdaFace embedding interval |
| `FACE_REID_MIN_INTERVAL_MS` | `1000` | 同 track ReID/export 节流 |
| `FRAME_ANNOTATION_REDIS_MAXLEN` | `200000` | frame annotation stream 近似保留上限 |
| `FRAME_ANNOTATION_TTL_SECONDS` | `600` | annotation 消息语义 TTL，不是 Redis EXPIRE |
| `EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL` | `240` | evidence admission 全局活跃预算 |
| `EVIDENCE_ADMISSION_MAX_ACTIVE_PER_SOURCE` | `2` | evidence admission 单 source 活跃预算 |
| `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY` | `8` | clip-worker 物化并发预算 |
| `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD` | `4` | 单 Replay shard 并发预算 |
| `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE` | `1` | 单 source 并发预算 |
| `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE` | `4` | media-worker 本进程 materialization guard |
| `MEDIA_WORKER_MATERIALIZATION_THROTTLE_SLEEP_S` | `0.5` | media-worker 每条 evidence 完成后的平滑停顿 |
| `MEDIA_WORKER_MATERIALIZATION_THROTTLE_DEADLINE_GUARD_S` | `90` | deadline 剩余时间低于该值时跳过停顿 |
| `MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT` | `4` | media-worker native/ffmpeg 线程上限 |
| `MEDIA_WORKER_FFMPEG_X264_PRESET` | `ultrafast` | post-Savant fallback 转码预设 |
| `REPLAY_FORCE_CONSTANT_CADENCE` | `true` | clip-worker 默认使用 Replay constant-cadence 请求，避免旧 Replay payload 兼容 fallback 成为常态路径 |

注意：`STORAGE_MAINTENANCE_EXECUTE_ENABLED=true` 写在 env 中；compose API 服务有 false 默认值，
但 env 渲染时会覆盖。8090 存储删除能力因此是强能力，需要执行控制、确认 token 和审计配合。

## 4. 核心推理链路

### 4.1 Source -> Replay -> Forwarder

RTSP 源默认由 Savant 官方 GStreamer adapter 接入 Replay。当前运行时也支持通过 8090/API
动态创建 `video-analytics-source-*` 容器。`RTSP_TRANSPORT` 默认已对齐为：

```text
tcp,use_wallclock_as_timestamps=1,fflags=+genpts
```

`replay-service` 是证据链的全速权威存储，analysis-forwarder 只负责分析分支采样。forwarder 当前实现包括：

- `ZeroMQSource` 读取 Replay 输出；
- PTS/FPS 采样器 `AnalysisFrameSampler`；
- bounded drop queue，默认 `FORWARDER_QUEUE_MAX_SIZE=2048`；
- `BlockingWriter` 写 Savant；
- Prometheus metrics：seen、forwarded、dropped、send failures、queue depth；
- `FORWARDER_OUT_ENDPOINT=null://...` 诊断模式，用于隔离 forwarder 自身能力。

这使前端性能定位可以拆成两步：先测 Replay/forwarder/null sink，再接回 Savant 测真实推理消费。

### 4.2 Savant/DeepStream module

`modules/savant_security/module.yml` 当前模型链包括：

```text
zeromq_source_bin
  -> PtsFpsGate
  -> optional replay_savant_frame_dump
  -> yolo26_pose
  -> nvtracker
  -> behavior_rules
  -> yolov8_face
  -> face_person_associator
  -> adaface
  -> face_reid_gate
  -> face_observation_exporter
  -> frame_annotation_exporter
  -> savant_perf_metrics
```

主要能力：

- 姿态检测：YOLO26 pose，按阈值过滤人形框和关键点。
- 跟踪：`nvtracker` 给 person 分配 track id。
- 行为规则：`behavior_rules` 从摄像头规则配置中执行入侵等行为检测。
- 人脸检测：YOLOv8-Face。
- 人脸和 person 关联：将 face 绑定到同帧 person/track。
- AdaFace embedding：生成 512 维向量。
- ReID gate：按时间、置信度、脸尺寸和 embedding norm 节流导出。
- Redis exporters：导出 `security.events`、`security.face_observations`、`security.person_observations`
  和 `security.frame_annotations`。
- 性能指标：暴露 Savant effective FPS、annotation export 等 runtime metrics。

Redis exporter 使用有界异步 writer；正常情况下 Redis 写入抖动不应直接阻塞 Savant `process_frame`。

### 4.3 Redis -> workers

推理结果进入 Redis Streams 后，由 worker 承接：

- `event-worker` 消费 `security.events`，写 PostgreSQL `events`，发告警和 record request。
- `face-worker` 消费 `security.face_observations`，写 `face_observations`，同步进行 gallery/watchlist 匹配。
- `clip-worker` 消费 `security.record_requests`，对 Replay 发起取证 job，更新 `evidence_tasks`。
- `media-worker` 扫描 video-file-sink 输出，生成或校验证据，并将 evidence metadata、timeline、overlay 写入 PostgreSQL。

## 5. API 与 8090 控制面

### 5.1 API 结构

内部 API 是 FastAPI 应用，包含：

- `/health`、`/ready`
- `/api/v1/events/*`
- `/api/v1/evidence/*`
- `/api/v1/cameras/*`
- `/api/v1/algorithms/*`
- `/api/v1/people/*`
- `/api/v1/maintenance/*`
- `/api/v1/runtime/*`
- `/api/v1/ws/alerts`

8090 evidence-viewer 代理 `/api/v1/{path}` 到内部 API，并代理 `/media/{path}` 到内部 API 的
media mount。这个设计让用户入口集中在 8090，而不是直接暴露 API 8000。

### 5.2 摄像头和算法规则

摄像头、ROI、规则等以 PostgreSQL 为 source of truth。API 可以导出 Savant runtime 配置：

- `modules/savant_security/config/cameras.midterm.yml`
- `infra/generated/sources.generated.yml`

这些 YAML 是运行时快照，不应单独当作人工配置源。8090 的摄像头页面能编辑 RTSP、启停摄像头、
配置 ROI 和规则，并可执行 runtime apply/restart。

### 5.3 人员和人脸库

人员、人脸注册和图库都在 `/api/v1/people` 下。API 镜像基于 face-worker 基础镜像构建，
复用 ONNX Runtime/OpenCV/Numpy 层来支持人脸注册。图库 embedding 存储在
`person_gallery_embeddings.embedding vector(512)`。

当前 watchlist 规则有两层：

- Savant/摄像头规则层生成算法事件；
- face-worker 通过 env 和图库做全局 watchlist/gallery matching。

报告使用当前代码观察：face-worker 仍会对每条新插入 observation 同步调用 watchlist emitter。
2026-06-29 后，在线图库检索已支持 `pgvector`、`shadow`、`qdrant`、`hybrid` 后端；当前
Qdrant authoritative 运行态为 `FACE_VECTOR_BACKEND=qdrant`、
`QDRANT_FALLBACK_TO_PGVECTOR=false`、`QDRANT_PREFER_GRPC=true`。这修复的是图库向量查询
扩展性，不等价于已经拆分 face observation 入库和 matching 队列。

### 5.4 Evidence API

当前 evidence list/detail 语义已经 DB-backed：

- `evidence_bundles` 保存证据概览、状态、路径、camera/source 元数据；
- `evidence_artifacts` 保存 artifact 元数据；
- `evidence_frame_timeline` 保存 timeline；
- `evidence_overlay_segments` 保存 overlay；
- `raw_clip.mov` 仍是文件系统视频 artifact。

8090 仍保留旧 `/api/bundles*` 文件扫描兼容接口，但当前主语义应优先使用 `/api/v1/evidence/*`。

### 5.5 Runtime control、performance 和 topology

当前 runtime API 包括：

- `/api/v1/runtime/overview`
- `/api/v1/runtime/control`
- `/api/v1/runtime/performance-config` GET/PUT/apply
- `/api/v1/runtime/topology-config` GET/PUT/apply
- `/api/v1/runtime/control/single/start|stop|restart`
- `/api/v1/runtime/control/dual/stop`
- `/api/v1/cameras/runtime/config/sync`

`runtime_performance.py` 可以保存并应用 forwarder/Savant 性能参数，应用时重建相关容器并等待
Savant ready。

摄像头算法、ROI、规则保存与 runtime apply 是不同语义。算法/ROI 只是更新 DB-backed camera
rule，再导出 Savant/adapter 配置快照；它不应重启 Savant、Replay、source adapter、clip-worker
或 media-worker。需要启停摄像头、改变 RTSP 源、切换性能参数或切换拓扑时，才进入受控 runtime
apply/restart 路径，并应继续受 evidence guard 保护。

`runtime_topology.py` 是当前工作树新增能力，支持：

- `auto`
- `single`
- `dual_same_gpu`
- `dual_dual_gpu`

它会根据摄像头生成分支 plan，支持 balanced、gpu_id、manual 分片策略。dual apply 会：

1. 检查 evidence restart guard；
2. 停止 source adapters、单路 forwarder、单路 Savant、compose source；
3. 创建新的 runtime epoch；
4. 写 Savant camera config、source manifest 和 replay shard plan；
5. 重建 branch Savant/forwarder；
6. 启动 branch Replay/video-file-sink；
7. 按 plan 创建动态 source adapter。

需要注意的风险：topology 写出的 shard plan 默认路径是
`/data/video-analytics/media/.runtime/replay_shards.topology.json`，而 clip-worker 读取
`REPLAY_SHARDS_JSON` 或 `REPLAY_SHARDS_CONFIG_PATH`。当前 compose 中
`REPLAY_SHARDS_CONFIG_PATH` 默认空，因此双分支证据链验收必须证明 clip-worker 实际读取了
topology 写出的 shard 文件。

## 6. 数据模型

### 6.1 摄像头/规则

核心表：

- `cameras`
- `camera_zones`
- `camera_rules`

迁移 010/012/016 给 operator schema 和 rule/zone ID 做兼容，`cameras.source_id` 有唯一索引。
当前运行配置从 DB 导出到 YAML，YAML 不是最终事实源。

### 6.2 事件和告警

核心表：

- `events`
- `audit_logs`

事件表按 event type、source/camera、时间和状态建索引。018 增加了面向 recent/list 查询的性能索引。
事件入库由 event-worker 和 face-worker 共同产生，watchlist hit 可由 face-worker 写回
`security.events` 后再进入 event-worker 处理。

### 6.3 人脸和图库

核心表：

- `face_observations.embedding vector(512)`
- `persons`
- `person_gallery_embeddings.embedding vector(512)`
- `match_results`
- `person_bbox_observations`

当前 btree/person-active 索引用于过滤和关联。2026-06-29 后，
`person_gallery_embeddings` 的在线注册图库查询已经有 Qdrant 派生索引和 pgvector rollback path；
Qdrant 只服务注册图库，不改变 PostgreSQL 事实源。`face_observations.embedding` 历史相似检索仍
未迁移到 Qdrant，后续如要做历史人脸搜索，需要单独设计 retention、相机/时间过滤和删除一致性。

### 6.4 Evidence

核心表：

- `evidence_tasks`
- `evidence_bundles`
- `evidence_artifacts`
- `evidence_frame_timeline`
- `evidence_overlay_segments`

015 增加 materialization state；017 将 evidence artifact/timeline/overlay 写入 DB；
019/020 增加 media queue 和 playable bundle 热路径索引。当前 evidence admission/backpressure
可以把低价值事件标记为 `materialization_skipped`，避免无限制堆积。

## 7. 证据链语义

当前证据链的目标不是生成带烧录标注的视频，而是保留可播放 `raw_clip.mov`，并将 metadata、
annotation、timeline、overlay 写入 DB 或 sidecar/DB 索引供 8090 展示。

关键语义：

- Replay 是证据时间窗的来源。
- clip-worker 负责从 record request 到 Replay job 的调度。
- video-file-sink 写 raw clip 和 sink metadata。
- media-worker 校验 raw clip 可播放性、生成 evidence bundle/index。
- raw clip 可播放但 annotation 缺失时可以降级成功，而不是把视频证据误判成完全失败。
- 成功 evidence bundle 只需要保留 `raw_clip.mov`，metadata/annotation 以 DB-backed 语义服务。

当前 60 路 3 FPS 压测中，50/50 retained evidence playable；annotation complete 是 26/50，
剩余 24 条为 `missing_frame_metadata`，说明可播放性和标注完整性应分开验收。

## 8. 性能证据和当前能力边界

### 8.1 下游证据链

`pressure60_3fps_playabledrain_20260628T100531Z` 证明：

- 60 个 source active；
- source exited=0、restart=0、negative PTS=0；
- forwarder send failures=0；
- 保留 evidence 50；
- playable evidence 50/50；
- 8090 evidence API 可按 pressure source 查到保留 bundle；
- `XPENDING security.record_requests clip-workers-midterm` 最终为 0。

这说明当前后段 evidence admission、clip-worker、media-worker、DB 索引和 8090 查询可以支撑
所选 60 路 3 FPS 压力 profile。但它不等于真实 T4 生产 60 路结论，也不等于 60 路 8/16 FPS
推理吞吐已经闭环。

### 8.2 16/1 配置压测

`pressure60_16p1_20260628T112109Z` 证明高入口压力下证据链仍保住 50 条证据，且最终
50/50 playable、50/50 annotation complete。

但它不是 16 FPS 推理通过证明，原因是：

- Savant effective FPS 平均约 4.386；
- forwarder queue 多次达到 2048；
- forwarded/seen 比例约 19.8%；
- forwarder 到 Savant 出现 ZeroMQ backpressure；
- source adapter 侧有足够输入帧，瓶颈在 source adapter 之后、Savant 完成推理之前。

### 8.3 前端推理入口

forwarder null sink 结果：

- 60 路 4 FPS：passed，max queue 0，send failures 0；
- 60 路 8 FPS：passed，max queue 1，send failures 0；
- 60 路 16 FPS：passed，但 forwarded/target 约 0.72。

接回单 Savant：

- 60 路 4 FPS、`BATCH_SIZE=4`：passed；
- 60 路 8 FPS、`BATCH_SIZE=4/8/16`：queue full，failed；
- 60 路 8 FPS、`BATCH_SIZE=32`：脚本门槛 passed，但 effective FPS 平均约 5.82，
  不能宣称稳定达到 8 FPS。

同卡双分支 30+30：

- 4 FPS、`BATCH_SIZE=4`：passed，queue 0，send failures 0；
- 8 FPS、`BATCH_SIZE=4`：passed，max queue 771，send failures 0，avg effective FPS 7.41。

后续同卡双分支 retained-evidence 证据链也已完成：

- 4 FPS：`pressure60_8090topology_dual1gpu_evidence4fps_20260629T064345Z`，50/50 playable，
  annotation complete 50/50；
- 8 FPS：`pressure60_8090topology_dual1gpu_evidence8fps_batch4_verify_20260629T070656Z`，
  50/50 playable；
- media finalizer 平滑调度复测：`pressure60_media_fullobs_8fps_20260629T092901Z`，50/50
  retained playable，media-worker CPU 峰值约 98%，queue wait p95 约 189.9 秒，lifecycle
  p95 约 192.3 秒。

这说明单 4090 同卡双分支 8 FPS 已经不只是前端入口证明，而是有 pressure source 下的
Replay/clip/media/evidence 闭环证明。但它仍不是长时间 soak、真实 RTSP 混合输入或生产硬件承诺。

### 8.4 当前建议 operating point

基于现有证据：

- 稳妥生产基线：60 路 4 FPS，当前已有同卡双分支证据链通过证据，仍需真实 RTSP / 长时间 soak。
- 4090 优化档：60 路 8 FPS，优先使用同卡双分支；pressure source 下 retained-evidence 已通过，
  但真实 RTSP、长时间 soak 和现场生产硬件仍需单独验收。
- 16 FPS：只能作为极限观察，不应作为默认生产目标。
- T4/弱卡生产结论：当前证据不足，必须按 T4 / 30-per-shard 计划单独验收。

## 9. 主要代码级风险

### 9.1 Record request 去重已移除 O(N) 扫描

`event-worker` 在发布 record request 前调用：

```text
record_publisher.has_request(source_event_id, "savant_replay")
```

旧实现中 `RecordRequestPublisher.has_request()` 会对 `security.record_requests` 做：

```text
XRANGE security.record_requests - +
```

本次已改为 Redis `SET NX EX` 幂等键：

- key 由 `(stream, source_event_id, strategy)` 计算 SHA-256；
- `publish()` 在 `XADD` 前占用 key，`XADD` 失败会释放 key，允许后续重试；
- `has_request()` 只做 Redis `EXISTS`，不再扫描 stream；
- `RECORD_REQUEST_DEDUPE_TTL_SECONDS` 默认 86400 秒；
- duplicate retry task 会标记为
  `recording_policy_skipped:duplicate_record_request`，避免留下 pending 任务。

后续风险已经从代码热点降级为压测回归项：需要在下一次 60 路压力 artifact 中确认 event-worker
CPU 和 duplicate count 没有异常。

### 9.2 face-worker 同步匹配路径

`face-worker` 当前在 `_process_batch()` 中逐条处理 face observation：

1. 解析 observation；
2. 校验 embedding；
3. 插入 PostgreSQL；
4. 对新插入 observation 同步调用 `watchlist_emitter.emit_for_observation()`。

注册图库检索已经切到 Qdrant authoritative，并保留 pgvector rollback/exact rerank path。20,000
向量 gRPC benchmark 显示 Qdrant 查询本身已不是“数千人员、每人几张脸”场景的主要瓶颈。剩余风险
变成：单 consumer loop 仍把 DB insert、规则解析、Qdrant 查询、exact rerank、event publish 和 ACK
串在一起；face observation 速率继续增加时，应该优先考虑拆分 persistence/matching 队列，而不是再
优化 pgvector gallery scan。

### 9.3 media-worker finalizer 扩展模型

`media-worker` 有 `_MaterializationGuard(max_active)`，但主流程仍是一个进程内的轮询 loop，
调用 `_process_sink_output(...)`。该 guard 能限制本进程进入物化的活跃数，但不等于真正的
多 worker finalizer pool。

2026-06-29 后续状态：当前选择的第一阶段扩展模型不是直接提高并发，而是单进程
deadline-aware pacer：

- 高优先级事件类型优先，默认 `watchlist_hit,live_search_hit`；
- 同优先级按 `evidence_tasks.materialization_deadline_at` 更早者优先；
- 每轮最多启动 `MEDIA_WORKER_MATERIALIZATION_MAX_PER_POLL` 个 finalization，默认 0 表示不按轮硬限量；
- 每条完成并完成 DB/index/cleanup 更新后，按
  `MEDIA_WORKER_MATERIALIZATION_THROTTLE_SLEEP_S` 短暂停顿，默认 0.5 秒；
- deadline 剩余时间小于 `MEDIA_WORKER_MATERIALIZATION_THROTTLE_DEADLINE_GUARD_S`
  时跳过 sleep，并允许越过 per-poll 限制，默认 90 秒；
- `MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT` 默认 4，用于限制 native math env、
  OpenCV 线程和 post-Savant ffmpeg `-threads`。

这个模型利用 300 秒 Replay/evidence deadline 余量平滑 CPU 峰值，不改变 evidence 存储方式，
也不引入多 media-worker 容器的 DB claim 竞态。2026-06-29 的
`pressure60_media_fullobs_8fps_20260629T092901Z` 已经完成第一阶段吞吐证明：
50/50 retained playable，media-worker CPU 峰值约 98%，queue wait p95 约 189.9 秒，
lifecycle p95 约 192.3 秒，最小 deadline slack 约 103.1 秒。该结论固化在
`docs/midterm_media_finalizer_pacer_8fps_report_2026-06-29.md`。

已拒绝的调参结果：`MAX_PER_POLL=1` / `THROTTLE_SLEEP_S=2` 在 8 FPS / 60 路
retained-evidence 复测中把采样到的 media-worker CPU 峰值从约 1151% 降到约 992%，
但处理过慢，运行中只 materialize 32 条，且出现多条 `materialization_expired`。因此默认改为
保守 sleep + 线程限制，不再用每轮 1 条作为生产默认。

如果后续仍需提升证据吞吐，才继续评估更强并发模型：

- 多 media-worker 容器 + DB-backed claim；
- 单进程内部 worker pool；
- 独立 finalizer service。

否则会有重复终态、磁盘膨胀、Replay sink 堆积和 cleanup 竞态风险。

### 9.4 双拓扑和 replay shard 接线风险

当前代码已经支持 topology plan 和 replay shard routing。默认 compose 中 clip-worker 的
`REPLAY_SHARDS_CONFIG_PATH` 可以为空，但 pressure harness 已通过 `REPLAY_SHARDS_JSON`
把 8090 topology apply 写出的 shard plan 注入 clip-worker，并校验 observed SHA256 和 topology
文件 SHA256 一致。

如果目标部署采用同卡双分支或双卡双分支，后续必须在真实 RTSP / 长时间 soak / 生产硬件 profile 中
继续记录：

- topology config；
- runtime epoch；
- topology 写出的 replay shard 文件路径和 hash；
- clip-worker 读取的 replay shard 输入路径；
- retained evidence 的 branch/shard 分布；
- 8090 list/detail 查询证明。

### 9.5 观测指标状态

当前 pressure 报告已经覆盖 forwarder、Savant、source 状态、保留 evidence、playable 数、
Redis pending 和 8090 查询证明。本次新增：

- `downstream_observability_summary.json`；
- Redis `XLEN` / `XINFO GROUPS`；
- PostgreSQL run summary、`pg_stat_user_tables` 和 evidence task lifecycle 聚合；
- event-worker record request dedupe counters；
- face-worker watchlist emitted/failed slots；
- media-worker finalization、ffprobe、ffmpeg duration distribution；
- media-worker deadline-aware pacing count、throttle reason、throttle sleep 和
  deadline slack distribution；
- worker docker CPU summary；
- retained evidence 的 8090 `/api/v1/evidence/bundles/{event_id}` 查询 proof。

仍缺或仍是 `not_enough_data`：

- face-worker 代表性图库规模下的 gallery query p95 / EXPLAIN；
- event-worker record-request dedupe latency；
- PostgreSQL hot query plan/stat deltas；
- media-worker queue wait / lifecycle / throttle 指标已在
  `pressure60_media_fullobs_8fps_20260629T092901Z` 中完成 pressure profile 级证明；
- ffmpeg CPU 已通过 thread limit 后的压力结果验证峰值改善；本轮 retained evidence
  没有触发常规 ffmpeg 转码，`imageio_ffmpeg_fallback_count=0`；
- Savant 模型阶段级 latency，例如 pose、face、AdaFace、pyfunc 后处理耗时。

这些缺口需要后续在对应 worker 内加 timer 或 EXPLAIN harness；当前报告结构已经固定，缺数据会显式
显示为 `not_enough_data`，不再静默遗漏。

## 10. 运维和迁移风险

### 10.1 运行态 source of truth

PostgreSQL 是摄像头、规则、人员、图库和 evidence metadata 的事实源。`cameras.midterm.yml` 和
`sources.generated.yml` 是生成的运行时快照。只看 YAML diff 不能证明人工配置漂移。

### 10.2 数据目录

`/data/video-analytics` 承载模型、media、Replay RocksDB、artifact、可选 PostgreSQL 数据等运行数据。
干净迁移应携带代码和模型，通常不应把 live Redis/PostgreSQL/Replay/evidence/person 状态直接带到新机器，
除非迁移目标明确需要保留业务数据并配套校验。

当前 clean-machine 迁移脚本支持两种包：

- 在线/半在线包：`repo.tgz` + `models.tgz`，目标机按 compose build/pull；
- 离线包：增加 `--include-images` 后生成 `images.tar` 和 `image_list.txt`，部署脚本自动
  `docker load`，并在启动时使用 `--no-build`。

UOS 目标机仍必须先具备 Docker Engine、Docker Compose v2、NVIDIA driver 和 NVIDIA Container
Toolkit。应用包不迁移旧 PostgreSQL、Redis、Replay RocksDB、证据媒体、人脸库或历史 artifact；
新机器需要在 8090 重新注册人员/人脸，除非另做业务数据迁移方案。

### 10.3 8090 强操作

8090 当前不仅是浏览器 UI，也是运行控制面：

- 摄像头源 apply/restart；
- runtime performance apply；
- topology apply；
- 单路/双路容器启停；
- storage maintenance delete。

这些操作都可能影响正在生成的证据。生产使用时必须保留 evidence guard、确认 token、审计日志和操作前健康摘要。

### 10.4 默认 DB 依赖

默认 worker/API 指向宿主 PostgreSQL。部署诊断时，worker 重启循环可能是宿主 DB 未启动或端口不可达，
不一定是 worker 镜像损坏。

## 11. 建议路线图

### P0 - 固化验收边界

- 将“60 路 3 FPS 下游证据链通过”“60 路 16/1 不是 16 FPS 推理证明”“同卡双分支 8 FPS
  pressure source 证据链通过但不是生产 soak 证明”
  作为文档和验收口径固定下来。
- 对所有 runtime/topology 压测 artifact 记录 dirty diff、runtime epoch、source count、规则集、FPS/batch、
  forwarder/Savant/worker/DB 指标。

### P1 - 去除 O(N) 热点

- 已完成：替换 `RecordRequestPublisher.has_request()` 全 stream 扫描。
- 已完成：pressure60 downstream observability schema 和静态测试。
- 已完成：注册图库在线查询 Qdrant cutover，60 路 authoritative 压测 fallback=0，20,000 向量
  gRPC benchmark all-search p95/p99 为 4.037ms/6.427ms。
- 后续仅对历史 `face_observations` 相似检索补 `EXPLAIN ANALYZE` / Qdrant 方案；注册图库
  pgvector ANN 已不是本阶段首要路线。
- 已补 harness：`RecordRequestPublisher` 幂等单测、event-worker duplicate/reclaim 测试、
  合成 Redis stream 验证不再调用 `XRANGE - +`。

### P2 - 证明 media finalizer 扩展模型

- 已选择第一阶段扩展模型：单进程 deadline-aware pacer + CPU/ffmpeg thread limit。
- 已用 60 路同卡双分支 8 FPS retained-evidence 压测证明第一阶段模型：
  media-worker CPU 峰值约 98%，50/50 retained playable。
- 当前保留单进程模型，不立即上内部 worker pool 或多容器 DB claim。
- 后续只有在真实 RTSP / 长时间 soak 中 queue/lifecycle p95 超出 300 秒 deadline，或生产要求全量事件
  物化时，再升级为内部 worker pool、多 media-worker 容器 DB claim 或独立 finalizer service。
- 仍需补充更细的 claim、proof、ffprobe/ffmpeg、decode、DB terminal update 阶段指标和终态幂等
  回归测试。

### P3 - 双分支证据链闭环

- 已完成同卡双分支 retained-evidence 端到端压力测试，并通过 8090 list/detail 证明保留样本可查询。
- pressure harness 已校验 topology replay shard JSON 和 clip-worker `REPLAY_SHARDS_JSON` SHA256 一致。
- 下一步不是重复证明同一 pressure source profile，而是做真实 RTSP 混合输入、长时间 soak 和双 GPU /
  T4 profile 验收。
- 仍需保留 replay shard parser/routing、topology apply dry-run、clip-worker retained evidence shard
  diagnostics 作为回归测试。

### P4 - Savant 阶段级指标

- 补 pose、face detector、AdaFace、pyfunc 后处理、DeepStream queue/batch wait 的低频指标。
- 在 8 FPS 优化中用阶段指标判断是调 interval、减少 annotation 输出、提高 batch，还是继续拆 shard。

### P5 - 8090 和受控重启回归

- 把算法/ROI 保存和 runtime apply 的区别写入 operator 验收项。
- 每次修改 camera rule/zone 后，验收 `containers_restarted=[]` 和 `source_containers_touched=[]`。
- 每次修改性能参数或拓扑参数后，验收 evidence guard、审计日志、受影响容器列表和 8090 状态提示。

## 12. 结论

当前程序已经具备中期交付所需的完整产品面和端到端证据链：8090 能完成配置、人员库、证据复核和运行控制；
Savant pipeline 覆盖姿态、人脸、行为规则和人脸识别；Replay + clip/media worker 能生成可审查证据；
DB-backed evidence 语义已经替代单纯 sidecar 读取。

但当前还不能把“压测通过”扩大解释为所有生产目标完成。准确边界是：

- 60 路 3 FPS 下游证据链已经有通过证据；
- 60 路 4 FPS 同卡双分支已完成 pressure source 证据链验收，可作为当前保守生产目标继续做真实 RTSP /
  长时间 soak；
- 60 路 8 FPS 同卡双分支已完成 pressure source retained-evidence 验收；media-worker 平滑调度将
  evidence lifecycle p95 明确控制在约 192 秒；
- 注册图库 Qdrant authoritative cutover 已完成当前 scale gate：60 路 8 FPS 压测 Qdrant p95/p99
  为 3ms/4ms、fallback=0；20,000 向量 benchmark all-search p95/p99 为 4.037ms/6.427ms；
- 16 FPS 不应作为当前默认生产承诺；
- T4/弱卡 60 路仍需要按单独计划验收。

下一阶段应避免继续扩大配置面，而应优先做真实 RTSP 混合输入、长时间 soak、生产硬件 profile、
face-worker persistence/matching 解耦可行性，以及 Savant 阶段级 latency 指标。只有这些闭环后，
`PASS_POST_INFERENCE_60_STREAM_CLOSURE` 或更高层的生产 readiness 才有足够证据支撑。

## 13. 参考依据

- `README.md`
- `README_MIDTERM.md`
- `infra/docker-compose.midterm.yml`
- `infra/env/midterm.env`
- `modules/savant_security/module.yml`
- `services/analysis-forwarder/app/main.py`
- `services/api/app/main.py`
- `services/api/app/routers/runtime.py`
- `services/api/app/services/runtime_topology.py`
- `services/evidence-viewer/app/main.py`
- `services/event-worker/app/worker.py`
- `services/event-worker/app/record_request.py`
- `services/face-worker/app/worker.py`
- `services/face-worker/app/vector_store.py`
- `services/clip-worker/app/replay_shards.py`
- `services/media-worker/app/worker.py`
- `scripts/midterm_package_clean.sh`
- `scripts/midterm_deploy_clean.sh`
- `db/migrations/*.sql`
- `docs/midterm_downstream_evidence_performance_2026-06-28.md`
- `docs/midterm_frontend_inference_performance_2026-06-28.md`
- `docs/midterm_dual1gpu_evidence_chain_4fps_8fps_report_2026-06-29.md`
- `docs/midterm_media_finalizer_pacer_8fps_report_2026-06-29.md`
- `docs/midterm_uos_clean_machine_migration_steps_2026-06-29.md`
- `docs/midterm_post_inference_bottleneck_static_review_2026-06-28.md`
- `specs/26_midterm_post_inference_bottleneck_closure_plan.md`
