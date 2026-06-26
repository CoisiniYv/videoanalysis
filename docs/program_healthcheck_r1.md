# 程序体检 R1

审计日期：2026-06-26

审计范围：本次只做静态代码、配置、脚本和文档审计；没有重构代码，没有重启运行时，没有执行 Docker Compose 应用动作，没有修复业务链路，也没有重新跑端到端冒烟检查。

## 1. 执行摘要

当前系统结论是：**部分可用**。仓库现在的主线是 `infra/docker-compose.midterm.yml` 驱动的“先进入 Replay 缓存，再转发到 Savant 推理，再由 Redis、后台工作进程、PostgreSQL 和证据目录形成告警证据”的中期栈。8090 已经是事实上的统一管理入口，但摄像头生命周期、证据对齐、非入侵算法、实时找人、生产性能和权限控制都还没有闭环，不能宣称系统已经可生产上线。

本次开始审计时的仓库状态：

| 项目 | 当前值 |
| --- | --- |
| 当前分支 | `c2/post-savant-poc` |
| 最近提交 | `2bbf0e7 fix: avoid duplicate camera source apply` |
| 审计前已有的已修改文件 | `modules/savant_security/config/cameras.midterm.yml` |
| 审计前已有的未跟踪文件 | `docs/midterm_migration_performance_completeness_report_2026-06-26.md`、`docs/program_healthcheck_r1_plan.md` |
| 本次新增文件 | `docs/program_healthcheck_r1.md` |

一个重要发现：`modules/savant_security/config/cameras.midterm.yml` 里 `primary_rtsp` 和 `lab` 都是启用状态，但 `infra/generated/sources.generated.yml` 里 `primary_rtsp` 是停用状态、`lab` 是启用状态。这说明数据库、Savant 配置和实际视频源适配器生成配置之间可能已经漂移，8090 页面上的摄像头状态不能直接当成运行时真实状态。

## 2. 当前主线拓扑

当前真实主线可以还原为：

```text
8090 管理入口，也就是 evidence-viewer 服务
  -> 代理 /api/v1/* 到内部 api:8000
  -> 自己提供 /api/bundles/* 证据文件查看接口

PostgreSQL 中的 cameras / camera_rules
  -> API 导出并写入：
       modules/savant_security/config/cameras.midterm.yml
       infra/generated/sources.generated.yml
  -> 固定视频源适配器或动态 video-analytics-source-* 容器
  -> replay-service，写入 RocksDB 缓存
  -> analysis-forwarder，按 PTS 降帧并转发
  -> savant-security，加载 modules/savant_security/module.yml
       YOLO26-pose -> tracker -> 行为规则
       YOLOv8-Face -> 人脸-人员关联 -> AdaFace -> ReID 门控
       -> 导出行为事件、人脸观察、人员观察、帧标注
  -> Redis 消息流
  -> event-worker / face-worker / clip-worker / media-worker
  -> PostgreSQL events / evidence_tasks / face_observations / match_results
  -> Replay 任务 -> video-file-sink -> media-worker 证据包
  -> /data/video-analytics/media/evidence
  -> 8090 证据查看页面
```

主线边界如下：

| 类别 | 当前主线 | 历史或 POC 噪声 |
| --- | --- | --- |
| 编排文件 | `infra/docker-compose.midterm.yml` | `infra/archive/*`、`infra/archive/phase-only/*` |
| 环境变量文件 | `infra/env/midterm.env` | `infra/archive/phase-only` 下的历史环境变量文件 |
| Savant 模块 | `modules/savant_security/module.yml` | `modules/archive/phase-only/*`、`modules/savant_security/archive/module-variants/*` |
| Replay 配置 | `modules/savant_replay/config.midterm.json` | 归档 Replay 配置 |
| 8090 页面 | `services/evidence-viewer/app/static/*` | API 服务旧 `/operator/static` 页面已在本次清理中移除 |

## 3. 服务清单

| 服务 | 端口 | 镜像或构建路径 | 依赖 | 作用 | 当前判断 |
| --- | ---: | --- | --- | --- | --- |
| `redis` | 宿主机 `6396`，容器 `6379` | `redis:7-alpine` | 无 | Redis Streams 消息总线 | 主线服务 |
| `api` | compose 内部 `8000`，不直接暴露宿主机 | `services/api/Dockerfile.face-runtime` | Redis、PostgreSQL | FastAPI 业务 API | 主线服务，通过 8090 访问 |
| `postgres` | 配置档下宿主机 `5439`，容器 `5432` | `pgvector/pgvector:pg16` | 无 | 可选本地 PostgreSQL | 只是可选配置档；默认 URL 指向 `host.docker.internal:5432` |
| `replay-service` | 宿主机 `8098`，容器 `8080` | `ghcr.io/insight-platform/savant-replay-x86:v0.6.0` | 无 | 全帧缓存和 Replay 任务接口 | 主线服务 |
| `analysis-forwarder` | 宿主机 `18081` 指标端口 | `services/analysis-forwarder/Dockerfile` | Replay | Replay 到 Savant 的降帧转发器 | 主线服务 |
| `savant-security` | 宿主机 `18080` 指标端口 | `ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1` | Redis、forwarder | GPU 推理管线 | 主线服务，默认预留 1 张 GPU |
| `source-adapter` | 不暴露端口 | Savant GStreamer 适配器镜像 | Replay、forwarder、Savant | 固定 `primary_rtsp` 拉流入口 | 主线服务，但只覆盖固定源 |
| 动态视频源适配器 | 不暴露端口 | 同一个适配器镜像 | Docker socket、Compose 网络 | 8090/API 新增摄像头后的动态拉流容器 | 主线路径，但发现生成配置漂移 |
| `event-worker` | 无 | `services/event-worker/Dockerfile` | Redis、PostgreSQL | 消费事件、写事件表、发告警和录像请求 | 主线服务 |
| `face-worker` | 无 | `services/face-worker/Dockerfile` | Redis、PostgreSQL | 消费人脸观察、写库、做向量匹配、发名单命中 | 主线服务 |
| `clip-worker` | 无 | `services/clip-worker/Dockerfile` | Redis、Replay、video-file-sink、PostgreSQL | 消费录像请求并创建 Replay 任务 | 主线服务 |
| `video-file-sink` | 内部 `6666` | Savant 适配器镜像加自定义启动入口 | Redis | 接收 Replay 任务输出的视频和元数据 | 主线服务 |
| `media-worker` | 无 | `services/media-worker/Dockerfile` | video-file-sink、PostgreSQL | 扫描接收目录并生成证据包 | 主线服务 |
| `evidence-viewer` | 宿主机 `8090` | `services/evidence-viewer/Dockerfile` | API | 对外管理入口和证据查看器 | 主线入口 |
| `replay-a/b`、`savant-a/b`、`analysis-forwarder-a/b`、`video-file-sink-a/b` | 多个可配置端口 | 同类镜像或构建路径 | 配置档相关 | 双 Replay / 双 GPU 扩展 | 可选配置档，本次未证明生产可用 |

卷挂载基本围绕 `/data/video-analytics`：模型、下载、媒体、证据、Replay RocksDB、可选 PostgreSQL 数据都在这个目录树下。默认 `savant-security` 只按数量预留 GPU；可选 `savant-a/b` 有更明确的 GPU 设备配置。

## 4. 8090 管理入口审计

8090 对应的是 `services/evidence-viewer`，它是 FastAPI 服务。它不是内部 `services/api` 本体，而是公开页面、证据文件接口和 API 代理的组合。

| 页面或接口 | 当前能力 | 背后实现 | 是否真实生效 | 问题 |
| --- | --- | --- | --- | --- |
| `/`、`/operator` | 打开操作台页面 | evidence-viewer 静态文件 | 是 | 已保留为唯一页面入口 |
| `/health` | 检查 evidence 根目录 | evidence-viewer health | 是 | 只说明证据目录存在，不代表全系统健康 |
| `/api/v1/*` | 代理到内部 API | evidence-viewer 的白名单代理 | 大部分生效 | 普通 HTTP 代理，不等于可靠 WebSocket 代理 |
| `/api/bundles*` | 浏览证据文件、标注、原始视频 | evidence-viewer 原生文件接口 | 是 | 只能查看已有证据，不能生成证据 |
| `/media/*` | 代理 API 媒体文件 | evidence-viewer 媒体代理 | 是 | 依赖 API 的 media 挂载 |
| 摄像头管理 | 新增、修改、启停、查询 | cameras API 和 repository | 是 | 发现配置漂移，页面状态不一定等于实际拉流状态 |
| 区域和 ROI | JSON 方式维护区域 | camera_zones API | 是 | 不是成熟的可视化 ROI 绘制 |
| 算法规则 | 保存规则、开关、模板 | algorithms/cameras API | 部分生效 | 不支持的算法也可能显示为可配置 |
| 应用视频源 | 收敛视频源适配器容器 | `converge_camera_sources` | 是 | 会操作 Docker 容器 |
| 应用或重启运行时 | 写配置、重启 Replay、转发器、Savant 和后台工作进程 | `apply_camera_runtime` / `restart_camera_runtime` | 是 | 影响范围大，会中断运行 |
| 单路启动、停止、重启 | 操作一组硬编码容器 | `runtime_control.py` | 是 | 缺少生产级权限隔离 |
| 人脸注册 | 上传图片、离线检测和 AdaFace 特征向量、写入人员图库 | people API 和 `libs/face_registration` | 是 | 依赖模型和运行后端 |
| 证据索引 | 从数据库查询证据包 | `/api/v1/evidence/bundles` | 是 | 详情和视频仍走文件接口，数据库与文件可能不一致 |
| 存储维护 | 预览和执行删除 | maintenance API | 是 | `midterm.env` 中执行开关为 true，风险高 |

结论：8090 不是 UI 壳，它确实能写库、写运行时配置、启停容器、删除或标记存储内容。当前最大问题不是“不能用”，而是“权限过大且状态可信度不够”。

## 5. 接口清单

| 接口 | 方法 | 文件 | 当前实现 | 数据表或外部服务 | 缺口 |
| --- | --- | --- | --- | --- | --- |
| `/health` | GET | `services/api/app/main.py` | 固定返回健康 | 无 | 不检查依赖 |
| `/ready` | GET | `services/api/app/main.py` | 检查 PostgreSQL | PostgreSQL | 不检查 Redis、Replay、Savant |
| `/operator` | GET | `services/api/app/main.py` | API 自带操作台页面 | 静态文件 | 不是 8090 实际公开页面来源 |
| `/api/v1/cameras` | GET/POST | `routers/cameras.py` | 摄像头列表和新增 | `cameras` | 与运行时视频源状态可能漂移 |
| `/api/v1/cameras/{id}` | GET/PUT | `routers/cameras.py` | 摄像头详情和更新 | `cameras` | source_id 变更有安全约束，UI 隐藏 source_id |
| `/api/v1/cameras/{id}/enable|disable` | POST | `routers/cameras.py` | 启停摄像头 | `cameras`、运行时应用载荷 | 可能影响实际视频源适配器 |
| `/api/v1/cameras/{id}/zones` | 增删改查 | `routers/cameras.py` | 区域维护 | `camera_zones` | 缺少成熟 ROI 绘制 |
| `/api/v1/cameras/{id}/rules` | 增删改查/开关 | `routers/cameras.py` | 旧规则接口 | `camera_rules` | 可配置不支持算法 |
| `/api/v1/algorithms*` | GET/POST/PUT | `routers/algorithms.py` | 算法注册表、支持矩阵、规则接口 | `camera_rules`、注册表 | 注册表不是算法实现本身 |
| `/api/v1/events*` | GET/POST | `routers/events.py` | 事件查询和状态变更 | `events`、`evidence_tasks` | 只管理已产生事件 |
| `/api/v1/evidence/health` | GET | `routers/evidence.py` | 数据库证据索引健康 | PostgreSQL | 不验证视频文件可读 |
| `/api/v1/evidence/bundles` | GET | `routers/evidence.py` | 数据库证据列表 | `events`、`evidence_tasks`、`cameras` | 详情仍依赖文件接口 |
| `/api/v1/people` | GET | `routers/people.py` | 人员列表和搜索 | `persons`、图库表 | 没有完整一键找人工作流 |
| `/api/v1/people/{id}` | GET | `routers/people.py` | 人员详情和图库 | `persons`、`person_gallery_embeddings` | 轨迹/历史入口不完整 |
| `/api/v1/people/register-face` | POST | `routers/people.py` | 离线注册人脸 | 模型、`persons`、`person_gallery_embeddings` | 与实时 Savant embedding 是两条路径 |
| `/api/v1/maintenance/*` | GET/POST | `routers/maintenance.py` | 存储和人员/图库删除预览与执行 | 数据库、文件系统 | 需要鉴权和审计强化 |
| `/api/v1/runtime/overview` | GET | `routers/runtime.py` | 聚合运行状态 | Docker、指标接口、PostgreSQL | 只是部分可观测性 |
| `/api/v1/runtime/control*` | GET/POST | `routers/runtime.py` | 启停容器组 | Docker socket | 高风险操作 |
| `/api/v1/ws/alerts` | WebSocket | `routers/ws_alerts.py` | 从 Redis 推送告警 | `security.alerts` | 8090 代理 WebSocket 不确定 |

没有发现当前主线里存在完整的 `live_search_jobs` 接口和运行路径。

## 6. 数据库清单

当前数据库迁移主线是 `db/migrations/001_init.sql` 到 `016_camera_rule_zone_id_text_compat.sql`。`db/migrations/archive/phase-only` 下的是历史迁移，不应当算主线。

| 表 | 是否存在 | 谁写入 | 谁读取 | 主线是否需要 | 问题 |
| --- | --- | --- | --- | --- | --- |
| `cameras` | 是 | API 摄像头接口 | API、运行时导出和应用 | 是 | 发现与生成的视频源配置漂移 |
| `camera_zones` | 是 | API 区域接口 | API、Savant 配置导出 | 是 | 区域编辑体验弱 |
| `camera_rules` | 是 | API 规则接口 | API、Savant 配置、face-worker | 是 | 同时存支持、半支持、不支持规则 |
| `events` | 是 | event-worker | API、clip-worker、media-worker、maintenance | 是 | `source_event_id` 唯一且幂等，但 `payload.media` 契约较宽 |
| `evidence_tasks` | 是 | event-worker、clip-worker、media-worker | API、运行状态接口、maintenance | 是 | 实际承担录像和媒体状态，状态种类较多 |
| `record_requests` | 否 | Redis 消息流 | clip-worker | 概念上需要 | 没有 PostgreSQL 表，只是 `security.record_requests` 消息流 |
| `media_ready` | 否 | 无 | 无 | 未来可选 | 当前用 `events`、`evidence_tasks` 和文件表示 media 状态 |
| `persons` | 是 | 人员 API、注册脚本 | 人员 API、face-worker | 是 | 历史表结构兼容复杂 |
| `person_gallery_embeddings` | 是 | 人脸注册/图库录入 | face-worker、人员 API | 是 | 特征向量是 `vector(512)` |
| `face_observations` | 是 | face-worker | face-worker、标注/轨迹查询 | 是 | `source_observation_id` 唯一，特征向量是 `vector(512)` |
| `match_results` | 是 | 图库匹配相关代码 | 轨迹和匹配查询 | 是 | 实时找人没有闭环 |
| `watchlist_rules` | 否 | 无 | 无 | 当前不需要 | 当前名单规则放在 `camera_rules` |
| `live_search_jobs` | 未发现 | 无 | 无 | 未来需要 | 实时找人还是设计/契约层 |
| `audit_logs` | 是 | API 审计仓库 | API/维护接口 | 是 | 操作员身份和权限模型弱 |
| `person_bbox_observations` | 是 | event-worker | 轨迹和标注相关路径 | 有用 | 依赖 Savant 人员观察导出 |
| `maintenance_jobs`、`maintenance_job_items` | 是 | maintenance API | maintenance API | 有用 | 删除操作需要更强保护 |

关键判断：

- `events.source_event_id` 有唯一约束，event-worker 使用冲突忽略实现幂等。
- `face_observations.source_observation_id` 有唯一约束，face-worker 使用冲突忽略实现幂等。
- `face_observations.embedding` 和 `person_gallery_embeddings.embedding` 都是 `vector(512)`，也就是 512 维特征向量。
- `watchlist_hit` 是 face-worker 发到 `security.events` 的事件，再由 event-worker 写入 `events` 表。
- 证据路径字段分散在 `events` 顶层列和 `payload.media` 中，完整性取决于 clip-worker 和 media-worker 是否完成。

## 7. Redis 消息流清单

| 消息流 | 生产者 | 消费者 | 消息内容 | 幂等键 | 风险 |
| --- | --- | --- | --- | --- | --- |
| `security.events` | Savant 事件导出、face-worker 名单命中导出 | event-worker | SecurityEvent JSON 结构 | `source_event_id` | 多生产者，必须稳定载荷契约 |
| `security.face_observations` | Savant 人脸观察导出 | face-worker | 人脸元数据和 512 维特征向量，不含图片字节 | `source_observation_id` | 单摄像头 `face.observation` 规则不是权威运行门控 |
| `security.person_observations` | Savant 人员观察导出 | event-worker 可选消费者 | 人员框观察 | `source_observation_id` | 对轨迹/标注有用，但不是主告警流 |
| `security.alerts` | event-worker | API WebSocket | 告警消息 | `alert:{source_event_id}` | 8090 代理 WebSocket 不确定 |
| `security.record_requests` | event-worker | clip-worker | Replay 录像请求 | `source_event_id:strategy` | 没有数据库表，待处理和重试主要在消息流和 `evidence_tasks` 中表达 |
| `security.frame_annotations` | Savant 帧标注导出 | clip-worker、media-worker | 轻量帧元数据、目标框、关键点和关键点标记 | 帧/事件锚点字段 | 有长度和 TTL 限制，不是持久队列 |
| `security.media_ready` | 未发现 | 无 | 无 | 无 | 设计预期和当前实现不一致 |

没有发现主线把图片字节、裁剪图字节或 base64 图片通过 Redis 传输。人脸观察传的是特征向量和元数据；帧标注默认不传特征向量。

## 8. Savant、Replay 和降帧链路

| 链路节点 | 当前实现位置 | 输入 | 输出 | 是否主线 | 问题 |
| --- | --- | --- | --- | --- | --- |
| 摄像头配置 | API 和摄像头仓库 | PostgreSQL 摄像头数据 | `cameras.midterm.yml`、`sources.generated.yml` | 是 | 已发现生成的视频源配置漂移 |
| RTSP 视频源适配器 | 固定 `source-adapter` 和动态适配器创建逻辑 | RTSP 地址 | ZMQ 到 Replay | 是 | 固定源和动态源并存，复杂 |
| Replay | `modules/savant_replay/config.midterm.json` | 适配器帧 | RocksDB 缓存和输出流 | 是 | TTL 300 秒限制证据生成窗口 |
| 降帧转发 | `services/analysis-forwarder` | Replay 输出流 | 降帧后发给 Savant | 是 | 正常降帧容易被误判为链路卡顿 |
| Savant 输入源 | `zeromq_source_bin` + `PtsFpsGate` | 转发器输出帧 | Savant 帧 | 是 | 转发器和 Savant 都有 FPS 控制 |
| YOLO26-pose | `module.yml` | 视频帧 | 人员框和关键点 | 是 | 双 T4 性能未知 |
| tracker | DeepStream NvDCF | 人员检测 | `track_id` | 是 | 多路稳定性未证明 |
| 行为规则 | `custom.pyfuncs.behavior_rules` | 人员、轨迹和规则 | 行为事件和人员观察 | 是 | 只有注册过的规则会运行 |
| YOLOv8-Face | `module.yml` | 全帧 | 人脸框和特征点 | 是 | 模型路径依赖挂载和符号链接 |
| 人脸-人员关联 | `FacePersonAssociatorPyFunc` | 人脸和人员 | 人脸上附加 `person_track_id` | 是 | 关联质量需要视觉验证 |
| AdaFace | `nvinfer@attribute_model` | 对齐人脸裁剪图 | 512 维特征 | 是 | 在 Savant 内执行，不在 face-worker |
| ReID 门控 | `FaceReidGatePyFunc` | 人脸质量和特征向量 | 是否允许导出 | 是 | 1 秒控制是导出节流，不是模型推理周期 |
| 人脸观察导出 | `FaceObservationExporterPyFunc` | 允许导出的人脸 | `security.face_observations` | 是 | 主要由管线和环境变量控制，不完全由单摄像头规则控制 |
| 帧标注导出 | `FrameAnnotationExporterPyFunc` | 当前帧目标 | `security.frame_annotations` | 是 | 有界缓存，不是持久证据存储 |
| Replay 任务 | clip-worker | 录像请求和关键帧锚点 | video-file-sink 输出 | 是 | 锚点和证明帧对齐仍是高风险 |
| 证据生成 | media-worker | 接收目录视频和元数据 | 证据包 | 是 | `known_face`、`bbox`、时间戳仍需验收 |

当前 Savant 模块实际启用了：`replay_savant_frame_dump` 调试 pyfunc、`yolo26_pose`、`tracker`、`behavior_rules`、`yolov8_face`、`face_person_associator`、`adaface`、`face_reid_gate`、`face_observation_exporter`、`frame_annotation_exporter`、`savant_perf_metrics`、`face_embedding_debug`、`same_frame_detection_debug`、`face_debug`。

Replay 锚点判断：

- clip-worker 优先使用 `anchor_keyframe_uuid`，再用 `previous_keyframe_uuid`，再用 `keyframe_uuid`。
- `start_window_frame` 和 `post_window_frame` 应当作为窗口证明，不应被误当成 Replay 锚点本身。
- `ALLOW_UNBOUNDED_KEYFRAME_FALLBACK=false` 是正确方向，缺锚点时应该失败，而不是静默生成不可信证据。

## 9. 后台工作进程职责和当前状态

| 工作进程 | 输入 | 输出 | 数据库写入 | Redis 写入 | 当前完成度 | 风险 |
| --- | --- | --- | --- | --- | --- | --- |
| event-worker | `security.events`，可选 `security.person_observations` | events、evidence_tasks、alerts、record_requests | `events`、`evidence_tasks`、`person_bbox_observations` | `security.alerts`、`security.record_requests` | 部分可用 | 只有 intrusion、watchlist_hit、live_search_hit 会进入可物化证据状态，其他行为多为 `not_implemented` |
| face-worker | `security.face_observations` | 人脸观察入库、向量匹配、名单命中事件 | `face_observations`、match 相关表 | `security.events` | 部分可用 | 环境变量兜底可能掩盖单摄像头规则状态；实时找人未闭环 |
| clip-worker | `security.record_requests` | Replay 任务和证据状态更新 | `events`、`evidence_tasks` | 确认或延后处理录像请求 | 部分可用 | 证明帧、锚点和重试逻辑复杂，容易受 TTL 和缓存影响 |
| media-worker | video-file-sink 输出目录 | 证据包文件和数据库媒体状态 | `events`、`evidence_tasks` | 无主队列写入 | 部分可用 | 扫描式工作方式，旁路元数据文件和 `known_face` 对齐仍需验证 |
| evidence-viewer | 证据文件目录和 API 代理 | 8090 页面和文件接口 | 无直接写入 | 无 | 部分可用 | 文件视图和数据库索引可能不一致 |

重点结论：

- event-worker 是事件入库、证据任务创建、告警发布、录像请求发布的核心入口。
- face-worker 不跑 GPU 特征提取；特征向量已经由 Savant AdaFace 生成。
- clip-worker 调 Replay 接口，处理关键帧、证明帧、待处理、重试和延后。
- media-worker 不消费主 Redis 队列，而是扫描 video-file-sink 输出目录并生成证据包。

## 10. 算法能力矩阵

本报告使用中文状态：闭环可用、部分可用、仅设计、不可用、未知。本次没有重新运行端到端冒烟检查，所以没有把任何能力标成“闭环可用”。

| 能力 | 设计或 API | 代码实现 | 测试 | 端到端验证 | UI 入口 | 当前状态 | 证据 | 下一步 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 入侵 | 有 | Savant 规则和证据链路 | 有 | 有历史验证，本次未重跑 | 有 | 部分可用 | 注册表标为生产就绪，后台工作进程链路存在 | 重跑当前端到端冒烟检查 |
| 徘徊 | 有模板 | 未发现注册规则模块 | 未发现主线规则测试 | 无 | 有模板 | 不可用 | 支持矩阵标为 `unsupported` | 实现或从页面明确隐藏 |
| 聚集 | 有模板 | 有 `crowd_gathering.py` | 有单元测试 | 无证据闭环 | 有 | 部分可用 | 支持矩阵标为 `event_only` | 补证据端到端验证 |
| 奔跑 | 有模板 | 未发现注册规则模块 | 未发现主线规则测试 | 无 | 有模板 | 不可用 | 支持矩阵标为 `unsupported` | 实现或从页面隐藏 |
| 追逐 | 有模板 | 有 `chasing.py` | 有单元测试 | 无证据闭环 | 有 | 部分可用 | 支持矩阵标为 `event_only` | 验证实时事件和证据链路 |
| 跌倒 | 有模板 | 有 `fall.py` | 有单元测试 | 无证据闭环 | 有 | 部分可用 | 支持矩阵标为 `event_only` | 用标准样本视频验证 |
| 翻越/越线 | 有模板 | 未发现注册规则模块 | 未发现主线规则测试 | 无 | 有模板 | 不可用 | 支持矩阵标为 `unsupported` | 实现越线规则和画线页面 |
| 人脸观察 | 有 | Savant 导出器 | 有测试 | 本次未重跑 | 有 | 部分可用 | 注册表标为 `config_only` | 明确单摄像头门控，或改成全局管线设置 |
| 图库匹配 | 有 | pgvector 搜索路径 | 有测试 | 本次未重跑 | 间接入口 | 部分可用 | face-worker 路径存在 | 加确定性图库冒烟检查 |
| 名单命中 | 有 | face-worker 发 SecurityEvent | 有测试 | 有历史验证，本次未重跑 | 有 | 部分可用 | 数据库摄像头规则和环境变量兜底并存 | 重跑单摄像头名单端到端验证 |
| 实时找人 | 有契约 | 未发现完整运行路径 | 主要是契约层 | 无 | 模板/状态 | 仅设计 | 注册表标为 `deferred` | 设计并实现 `live_search_jobs` |
| 人员历史/一键找人 | 部分有 | 轨迹仓库和测试 | 有查询测试 | 无完整页面工作流 | 部分 | 部分可用 | 查询工具存在 | 做操作台工作流 |
| 证据标注和 `known_face` 叠框 | 有 | 帧缓存旁路元数据和查看器 | 有测试 | 有历史验证，本次未重跑 | 有 | 部分可用 | 旁路元数据文件和动态叠框存在 | 稳定身份、`bbox`、时间域契约 |

## 11. 证据链路审计

| 证据产物 | 当前是否生成 | 来源 | 对齐锚点 | 风险 |
| --- | --- | --- | --- | --- |
| `raw_clip.mov` | 成功收尾时生成 | Replay 任务经 video-file-sink 输出 | Replay 关键帧和请求 PTS 窗口 | 可能被时长、运行代次、完整性保护拒绝 |
| `sink_metadata.json` | 生成 | video-file-sink 原生元数据 | Replay 任务输出视频帧域 | 字段时间域需要一致 |
| `metadata.json` | 生成 | media-worker | 事件、Replay、运行代次 | 数据库和文件可能不一致 |
| `summary.json` | 生成 | media-worker | 证据包摘要 | 需要与 sidecar summary 保持一致 |
| `annotations.frame_cache.identity.jsonl` | 旁路元数据成功时生成 | `security.frame_annotations` 和事件锚点 | 帧 PTS、会话、运行代次、source_observation_id | Redis 缓存有长度和 TTL 限制 |
| `summary.frame_cache.identity.json` | 旁路元数据成功时生成 | media-worker 旁路文件写入器 | 生产时间线域 | 页面可信度依赖这个文件 |
| `annotations.jsonl` | 不再作为当前输出生成或读取 | 已移除旧链路 | 不适用 | 仅保留负向保护，不能当成生产证据 |
| 查看器页面 | 有 | 8090 静态页面 | 原始片段加动态叠框 | 只认生产 sidecar |

结论：

- 当前证据可以作为调试和集成证据。
- 对入侵和名单命中，如果证据包同时具备原始视频、元数据、生产旁路文件，可以作为阶段性演示证据。
- 当前还不能直接称为生产证据，因为本次没有重跑端到端验证，且 `known_face`、`bbox`、时间域、`frame_num`、`source_observation_id` 仍有风险。
- 建议继续以 `raw_clip` 加 JSON/JSONL 旁路文件加查看器动态叠框为主线，而不是生成带标注的视频文件。
- `security.frame_annotations` 已经相当于短 TTL 的按帧索引元数据缓存，但如果要用于生产证据，需要明确持久性、TTL、丢失策略和降级策略。

## 12. 页面和操作体验审计

当前 8090 页面模块包括：

- 摄像头：新增、修改、启停、应用视频源、算法控制、运行配置预览。
- 人员与人脸：人员列表、人脸注册、图库展示、删除入口。
- 运行状态：运行概览、视频源、转发器、证据和容器表，单路启停和重启，关闭双路扩展。
- 证据：按摄像头、人员、类别筛选，查看原始视频和动态叠框。
- 存储维护：空间摘要、删除预览和删除执行。

页面与接口的关系：

- 大部分按钮会调用真实 `/api/v1/*` 接口。
- 运行时按钮会真实操作 Docker 容器。
- 证据列表有数据库索引，详情和视频仍走文件接口。
- ROI 和部分规则配置仍偏 JSON/模板化，不是成熟操作台。
- evidence-viewer 和 API 里都有操作台静态页面，后续容易产生前端入口漂移。

| 问题 | 严重程度 | 影响 | 建议阶段 |
| --- | --- | --- | --- |
| 8090 能控制 Docker 和删除存储，但未看到生产级鉴权 | P0 | 误操作或未授权访问会破坏系统 | R1.2 |
| 摄像头状态配置漂移 | P1 | 页面启用状态和真实拉流不一致 | R1.3 |
| 运行时应用和重启影响范围大 | P1 | 可能中断推理和证据生成 | R1.2 |
| 不支持算法仍可见或可配置 | P1 | 操作员会误以为算法已生效 | R1.5/R1.7 |
| WebSocket 告警代理不确定 | P2 | 实时告警体验可能不稳定 | R1.2 |
| ROI 没有成熟绘制工具 | P2 | 摄像头配置容易出错 | R1.7 |
| 证据状态横跨数据库、文件、历史旁路文件 | P2 | 操作员可能过度信任调试证据 | R1.4 |
| 有两套操作台静态文件 | P2 | 修改可能落错位置 | R1.2，本次已移除 API 侧旧副本 |
| UI 美观和信息密度仍需改善 | P3 | 影响体验，但不是当前主阻断 | R1.7 |

结论：当前 8090 适合作为开发和调试入口，不适合作为生产管理入口。

## 13. 测试、验证脚本和冒烟覆盖

| 测试或脚本 | 覆盖能力 | 是否需要 GPU | 是否需要真实 RTSP | 当前可信度 | 问题 |
| --- | --- | --- | --- | --- | --- |
| `harness/tests/test_api_*` | API、摄像头、人员、证据、存储 | 多数不需要 | 不需要 | 静态/单元覆盖较好 | 不能证明实时运行状态 |
| 算法支持矩阵和规则结构测试 | 注册表和操作台契约 | 不需要 | 不需要 | 契约覆盖好 | 注册表不等于实现 |
| 入侵规则测试 | 入侵逻辑 | 多数不需要 | 不需要 | 单元覆盖好 | 不是 GPU E2E |
| 聚集、跌倒、追逐规则测试 | 非入侵规则逻辑 | 多数不需要 | 不需要 | 只到规则层 | 没有证据闭环 |
| 人脸相关测试 | 人脸观察、匹配、后台工作进程 | 多数不需要 | 不需要 | 单元覆盖好 | 没有当前实时摄像头证明 |
| clip-worker 和证据物化测试 | 录像请求和物化策略 | 多数不需要 | 不需要 | 逻辑覆盖好 | 不能证明真实 Replay 可用 |
| 中期部署契约 | 编排文件和部署静态契约 | 不需要 | 不需要 | 静态守护有效 | 不代表运行时健康 |
| `scripts/midterm_health.sh` | 8090/API/端口/Docker 健康 | 不一定 | 视运行态而定 | 运维检查有用 | 不是全量算法验收 |
| 当前冒烟脚本 | 操作台、Savant 指标、双 4090 路径 | 部分需要 | 部分需要 | 跑通时价值高 | 本次没有执行 |
| 归档测试和脚本 | 历史阶段验证 | 不定 | 不定 | 只能参考 | 不能当当前主线证明 |

当前没有发现一个能一次性证明“8090 + 摄像头生命周期 + Replay + Savant + 后台工作进程 + 证据包 + 算法标准事件”的全量健康检查。

## 14. 性能和部署就绪度

| 性能问题 | 当前可测 | 当前不可测 | 是否需要生产机验证 | 建议 |
| --- | --- | --- | --- | --- |
| 批大小 | 配置位置已知 | 最终最优值 | 需要双 T4 | 先冻结配置来源，后面再调参 |
| 降帧和 FPS | 转发器和 Savant 门控配置可查 | 60 路延迟和吞吐 | 需要 | 分开看输入 FPS、转发 FPS、模型间隔、导出节流 |
| 姿态、人脸和特征提取节奏 | 模型间隔和 ReID 导出节流可查 | 生产精度和延迟平衡 | 需要 | 不要用 2x4090 结果定最终值 |
| Redis 积压 | 消费组和待处理逻辑存在 | 长时间负载下积压 | 部分需要 | 增加积压和待处理指标 |
| PostgreSQL 延迟 | 索引和查询可静态检查 | 高负载写入延迟 | 需要 | 增加 DB 延迟指标 |
| GPU 指标 | Savant 和 forwarder 指标端口存在 | T4 利用率和 NVDEC 能力 | 需要 | 做分阶段双 T4 验证 |
| 60 路 RTSP | 可考虑文件或 Replay 模拟 | 真实摄像头和网络行为 | 需要 | 先做模拟多路验证脚本 |
| Prometheus/Grafana | 主编排文件未见完整接入 | 完整监控栈 | 可选 | 先稳定语义，再接大盘 |

结论：当前 2x4090 开发机结果不能推导最终双 T4、60 路 RTSP 的生产结论。

## 15. 前 20 项风险

| 序号 | 风险 | 等级 | 影响 | 证据 | 建议阶段 | 阻断算法开发 | 阻断演示 | 阻断生产 |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 8090 无生产级鉴权但能控制 Docker 和删除存储 | P0 | 误操作会破坏系统 | Docker socket 和维护删除接口存在 | R1.2 | 否 | 可能 | 是 |
| 2 | 摄像头配置和生成的视频源配置漂移 | P1 | 摄像头可能显示启用但不拉流 | `primary_rtsp` 两处状态不一致 | R1.3 | 是 | 是 | 是 |
| 3 | 主线编排文件和配置档命名仍复杂 | P1 | 容易跑错栈 | 多个 archive/profile 并存 | R1.1 | 否 | 可能 | 是 |
| 4 | 宿主机 PostgreSQL 和编排内 PostgreSQL 目标不清 | P1 | 后台工作进程可能连错库 | 默认指向 `host.docker.internal:5432` | R1.1 | 是 | 是 | 是 |
| 5 | 证据对齐脆弱 | P1 | 证据不能证明触发事件 | 帧、关键帧、PTS、会话关系复杂 | R1.4 | 否 | 是 | 是 |
| 6 | 帧标注消息流不是持久存储 | P1 | 证明帧或旁路元数据可能过期 | TTL 和最大长度配置存在 | R1.4 | 否 | 是 | 是 |
| 7 | `known_face` 可能为 0 | P1 | 名单命中缺视觉证明 | 旁路元数据依赖 `source_observation_id` | R1.4/R1.6 | 否 | 是 | 是 |
| 8 | 时间域和帧域容易混用 | P1 | 标注错位或片段错误 | 多种时间字段并存 | R1.4 | 否 | 是 | 是 |
| 9 | 不支持算法出现在页面和接口 | P1 | 操作员误判能力 | 支持矩阵显示 `unsupported` | R1.5/R1.7 | 是 | 可能 | 是 |
| 10 | 非入侵行为证据未闭环 | P1 | 聚集/跌倒/追逐不能生成生产证据 | 支持矩阵为 `event_only` | R1.5 | 是 | 可能 | 是 |
| 11 | 实时找人仅设计 | P1 | 一键找人能力不完整 | 未发现 `live_search_jobs` | R1.6 | 否 | 可能 | 是 |
| 12 | 名单环境变量兜底掩盖单摄像头规则 | P1 | 8090 状态无法解释实际命中 | face-worker 有环境变量兜底 | R1.6 | 否 | 是 | 是 |
| 13 | 两套操作台静态文件漂移 | P2 | 改页面容易改错 | 审计时 evidence-viewer 和 API 都有静态页面；本次已清理 API 侧旧副本 | R1.2 | 否 | 可能 | 可能 |
| 14 | 8090 WebSocket 告警代理不确定 | P2 | 实时告警可能不可用 | evidence-viewer 主要是 HTTP 代理 | R1.2 | 否 | 可能 | 可能 |
| 15 | media-worker 扫描和队列可能积压 | P2 | 证据生成延迟 | 扫描限制和最大活跃任务配置 | R1.4 | 否 | 可能 | 是 |
| 16 | `media_ready` 表或消息流不存在 | P2 | 架构预期不一致 | 主线未找到 | R1.1/R1.4 | 否 | 否 | 可能 |
| 17 | 无效特征向量留在待处理队列 | P2 | Redis 待处理量增长 | face-worker 故意不确认无效特征向量 | R1.6 | 否 | 可能 | 可能 |
| 18 | 2x4090 结果误导双 T4 | P1 | 生产容量判断错误 | 没有双 T4 和 60 路真实 RTSP | R1.8/R1.9 | 否 | 否 | 是 |
| 19 | 没有当前全量端到端健康门禁 | P1 | 回归可能逃过单测 | 测试多但没有一键全链路验收 | R1.8 | 是 | 是 | 是 |
| 20 | 阶段性和归档文件污染认知 | P2 | 概念验证代码被误当主线 | archive 文件很多 | R1.1 | 否 | 可能 | 可能 |

## 16. 建议下一步开发顺序

### R1.1 — 主线编排文件和服务命名冻结

目标：
冻结唯一推荐编排文件、配置档、服务名、数据库目标、宿主机端口、视频源适配器模式、Replay/Savant/后台工作进程主线边界。

验收：
有一份短文档明确启动、停止、健康检查和配置档用法；官方配置档的 `docker compose config` 通过；归档文件明确标为非主线；API 和后台工作进程指向同一个数据库。

### R1.2 — 8090 管理入口稳定化

目标：
让 8090 成为可信控制台，明确安全操作和危险操作，解决鉴权或本机使用假设，统一静态页面来源。

验收：
只有一套 8090 页面被认定为公开入口；运行时控制明确展示会影响哪些容器；删除和停机操作有开关、确认和审计；WebSocket 告警路径被验证或明确标为不支持。

### R1.3 — 摄像头生命周期端到端闭环

目标：
从新增、修改、启用、停用摄像头开始，一路验证数据库、导出配置、视频源适配器、Replay、转发器、Savant、Redis 事件和证据。

验收：
一条冒烟检查能证明启用摄像头同时出现在数据库、`cameras.midterm.yml`、`sources.generated.yml`、适配器容器状态、Replay 流量、Savant 指标和至少一个事件/证据路径中。

### R1.4 — 证据契约稳定化

目标：
定义并强制生产证据契约：原始视频、元数据、旁路元数据文件、摘要、身份链接、`bbox` 坐标系、帧域/时间域和查看器行为。

验收：
入侵和名单命中就绪证据包都能验证原始片段时长、锚点关键帧、事件居中窗口、旁路元数据时间域、身份绑定和数据库/文件一致性。

### R1.5 — 缺失行为算法最小可用版本

目标：
实现或明确隐藏徘徊、奔跑、翻越；补齐聚集、跌倒、追逐的证据能力对齐。

验收：
每个启用的行为算法都有标准样本单测、Savant 事件测试和证据物化冒烟检查；未实现能力在 8090 上明确不可用。

### R1.6 — 人脸、名单命中和实时找人完成

目标：
让人脸观察、名单命中、图库匹配、实时找人和人员历史语义变成权威路径，并和单摄像头控制一致。

验收：
单摄像头名单规则能真实控制匹配；环境变量兜底被禁用或显式标注；实时找人有数据库、接口和运行时路径；一键找人能返回摄像头、时间和证据链接。

### R1.7 — 8090 操作员工作流改进

目标：
在后端状态可信后，改进摄像头添加、ROI 绘制、算法控制、人脸注册、证据复核和运行状态展示。

验收：
操作员不需要编辑 JSON 就能完成核心任务；不支持能力被禁用或明确标识；错误提示能说明失败原因和当前状态。

### R1.8 — 无生产 RTSP 的性能验证脚本

目标：
使用 Replay 或文件源构建可重复的多路负载验证脚本，但不把它当最终生产结论。

验收：
验证脚本能报告每阶段 FPS、Redis 积压、数据库延迟、证据延迟、GPU 指标和失败原因。

### R1.9 — 最终双 T4 验证计划

目标：
只在目标双 T4 机器和接近生产的视频源条件下做最终验收。

验收：
输出 60 路、延迟、GPU/NVDEC、Redis 积压、数据库延迟、证据正确性、重启恢复和 8090 操作检查的通过/失败门槛。

## 17. 下一步不要做什么

- 不要现在直接做 60 路性能结论。
- 不要现在大改批大小作为最终结论。
- 不要继续开很多阶段性模块。
- 不要只美化 UI 而不修摄像头、证据、算法闭环。
- 不要把 8090 页面按钮当成真实能力，必须检查背后接口和后台工作进程。
- 不要把 `face_observation` 当成 `watchlist_hit`。
- 不要把 `raw_clip` 调试产物当成生产证据。
- 不要通过手工改运行时配置来掩盖当前生成视频源配置漂移，必须用摄像头生命周期验收测试闭环。
- 不要在 R1.1、R1.3、R1.4 稳定主线契约前扩大后台工作进程、API、模块的重构面。

## 18. 附录：本次运行过的命令

仓库状态和文件清单：

```bash
git status --short
git branch --show-current
git log --oneline -n 20
find . -maxdepth 3 -type f | sort | sed -n '1,240p'
find . -iname '*compose*.yml' -o -iname '*compose*.yaml'
find . -maxdepth 4 -type f | grep -E 'Dockerfile|compose|env|Makefile|justfile'
grep -R "8090" -n . --exclude-dir=.git --exclude-dir=__pycache__
```

API、数据库、Redis、Savant 和测试扫描：

```bash
rg -n "@.*route|@router|FastAPI|APIRouter" services . --glob '!*.pyc' --glob '!**/.git/**' --glob '!**/__pycache__/**'
find db services -type f | grep -E 'migration|schema|sql|models|database|dao|repository'
rg -n "CREATE TABLE|UNIQUE|vector\(512\)|source_event_id|source_observation_id|evidence_tasks|watchlist_rules|live_search_jobs|record_requests|media_ready" db/migrations services --glob '!**/archive/**' --glob '!**/__pycache__/**'
rg -n "security\.events|security\.face_observations|security\.alerts|security\.record_requests|security\.media_ready|security\.person_observations|security\.frame_annotations|XADD|xadd|XREAD|xread" . --glob '!**/.git/**'
find modules infra services scripts -type f | grep -E 'module.yml|savant|replay|resampler|adapter|source|sink'
rg -n "Replay|replay|resampler|frame_uuid|keyframe_uuid|source_id|module.yml|nvinfer|pyfunc|yolov8|yolo26|AdaFace|adaface|face_observation|frame_annotation" modules services infra scripts --glob '!**/archive/**' --glob '!**/__pycache__/**'
find harness scripts -type f | sort
rg -n "PASS_|PARTIAL_|pytest|smoke|watchlist|intrusion|evidence|replay" docs harness scripts services modules --glob '!**/.git/**' --glob '!**/__pycache__/**'
```

重点读取的文件：

```bash
sed -n '1,940p' infra/docker-compose.midterm.yml
sed -n '1,360p' modules/savant_security/module.yml
sed -n '1,340p' modules/savant_security/config/cameras.midterm.yml
sed -n '1,160p' infra/generated/sources.generated.yml
sed -n '1,180p' modules/savant_replay/config.midterm.json
sed -n '1,180p' infra/env/midterm.env
sed -n '1,340p' services/evidence-viewer/app/main.py
sed -n '1,180p' services/api/app/main.py
sed -n '1,520p' services/api/app/algorithm_registry.py
sed -n '1,260p' services/api/app/services/runtime_control.py
sed -n '1,620p' services/api/app/services/runtime_apply.py
sed -n '1,760p' services/event-worker/app/worker.py
sed -n '1,620p' services/event-worker/app/repository.py
sed -n '1,760p' services/face-worker/app/worker.py
sed -n '1,360p' services/clip-worker/app/worker.py
sed -n '2400,3120p' services/clip-worker/app/worker.py
sed -n '1,760p' services/media-worker/app/worker.py
rg -n "runtime|control|restart|apply|supervisor|storage|delete|maintenance|fetch\(|request\(" services/evidence-viewer/app/static --glob '!**/*.map'
```
