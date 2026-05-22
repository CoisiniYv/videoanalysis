# 00_project_goal.md

## 1. 项目名称

多路实时视频智能分析系统，目录名：`video-analytics`。

## 2. 项目背景

本项目面向校园或园区安防场景，需要在一台具备多线程 CPU 和 2 路 NVIDIA T4 GPU 的服务器上，实时分析约 60 路 RTSP 摄像头视频流，检测人员行为、人脸身份和重点事件，并将事件推送到报警大屏、后台管理系统和数据库。

系统必须优先满足工程落地：稳定运行、可调试、可扩展、可测试、可审计。

## 3. 业务目标

系统需要支持以下算法和业务能力：

| 编号 | 能力 | 目标说明 |
|---|---|---|
| 1 | 周界入侵 | 人员进入指定 ROI 后产生入侵事件。 |
| 2 | 翻墙 | 识别人员跨越墙体线或翻越区域的可疑行为。 |
| 3 | 徘徊 | 人员在指定区域长时间低速停留。 |
| 4 | 人群聚集 | 指定区域内人数超过阈值并持续一段时间。 |
| 5 | 奔跑/追逐 | 根据 track 速度、方向和多人关系判断奔跑和追逐。 |
| 6 | 摔倒 | 根据人体姿态、bbox 变化和静止时间判断摔倒。 |
| 7 | 重点人员布控 | 注册人员人脸后，实时命中并告警。 |
| 8 | 轨迹追踪 | 检测到清晰人脸后记录 camera_id、track_id、时间和截图。 |
| 9 | 一键找人 | 用户输入人名，系统实时检测该人出现的位置并告警。 |

## 4. 技术目标

### 4.1 实时视频目标

- 支持约 60 路 RTSP 视频流。
- 双 T4 GPU 分流处理。
- 每路主检测默认 5 到 8 FPS。
- 不追求每帧完整推理，优先保证实时性。
- 主 pipeline 不能被数据库、截图、告警推送等慢操作阻塞。

### 4.2 模型目标

主模型使用 YOLO26-pose，承担：

- 人体检测。
- person bbox 输出。
- 人体关键点输出。
- 行为规则基础特征。
- head/person ROI 生成。

人脸链路使用：

- SCRFD_2.5G 进行人脸检测。
- ArcFace 提取人脸 embedding。
- PostgreSQL + pgvector 进行向量检索。

### 4.3 后端目标

FastAPI 负责：

- 摄像头管理。
- ROI 和规则配置。
- 事件查询。
- 人员库管理。
- 重点人员布控。
- 一键找人任务。
- 告警大屏实时推送。
- 审计日志。

### 4.4 数据目标

PostgreSQL + pgvector 统一存储：

- 摄像头信息。
- ROI 配置。
- 事件。
- 轨迹。
- 人员。
- 人脸向量。
- 一键找人任务。
- 审计日志。

## 5. 非目标

第一版不做以下内容：

1. 不做云端多机大规模分布式调度。
2. 不直接上 Kubernetes。
3. 不使用 Qdrant 作为第一版向量库，但必须预留 VectorStore 接口。
4. 不保存 60 路全量视频。MVP 阶段只预留事件截图和事件前后短片段字段；后续优先通过 Savant Replay Service 生成事件片段。
5. 不承诺翻墙、追逐、摔倒第一版达到高准确率，先定义为可疑事件并通过现场数据迭代。
6. 不在 Savant 主进程内执行慢速同步业务逻辑。

## 6. MVP 范围

MVP 第一阶段必须完成：

1. Savant module 可启动。
2. 单路视频 YOLO26-pose 检测人和关键点。
3. nvtracker 输出 track_id。
4. 周界入侵、徘徊、人群聚集、摔倒初版规则。
5. Redis Streams 事件输出。
6. event-worker 入库。
7. FastAPI 查询事件。
8. 前端或简单大屏显示最新告警。

MVP 第二阶段完成：

1. SCRFD_2.5G 接入。
2. ArcFace 接入。
3. pgvector 人脸向量存储和检索。
4. 重点人员布控。
5. 一键找人。

## 7. 成功标准

### 功能成功标准

- 可以注册重点人员人脸。
- 视频中出现该人时产生告警。
- 周界入侵、徘徊、人群聚集、摔倒初版可产生事件。
- 告警事件可以在大屏中实时展示。
- 事件可以按摄像头、类型、时间查询。

### 工程成功标准

- 行为规则可以脱离 Savant 单元测试。
- 人脸向量检索可以用 pgvector 测试。
- Pipeline 有 smoke test。
- 30 路、60 路压测有指标记录。

### 性能成功标准

- 60 路降帧运行时，系统不出现无限排队。
- 主事件延迟应保持在可接受范围内，目标小于 2 秒，具体以压测为准。
- 过载时可降级，不应导致服务崩溃。

---

# 8. MVP 报警录像策略补充

第一版 MVP 不强制实现复杂报警片段录制。

MVP 阶段目标是先跑通：

```text
Savant 推理
  -> Redis Streams
  -> event-worker
  -> PostgreSQL
  -> FastAPI
  -> 大屏/事件查询
```

报警录像能力采用“预留结构、暂不实现复杂录制”的策略：

```text
1. events 表保留 snapshot_path / clip_path。
2. payload.media 保留 snapshot_status / clip_status / recording_strategy。
3. 预留 security.record_requests 和 security.media_ready。
4. MVP 阶段允许 snapshot_path / clip_path 为空。
5. 大屏可以显示“视频片段暂未生成”。
```

第一版不采用“同一路摄像头双路拉流 + 持续切片落盘”作为默认方案。

后续正式实现报警片段时，优先考虑：

```text
1. Savant Replay Service：作为与 Savant 官方文档一致的短时缓存和重推方案。
2. 学校已有 NVR / 视频平台回放接口：作为外部系统对接方案。
3. post_event_rtsp_demo：仅用于临时演示，不作为生产默认方案。
```

## 9. Savant Replay 录像策略补充

Savant 官方提供 Replay Service，用于保存最近一段视频流并在需要时按 REST API 创建 re-streaming job。Replay 可以作为中间节点保存最近 N 秒视频与 metadata，并在事件触发后把事件周围视频重推到下游 sink。

本项目后续报警片段录制默认采用：

```text
RTSP Source Adapter
  -> Savant Replay Service
  -> Savant Module 实时分析
  -> Redis security.events
  -> event-worker 入库和告警
  -> clip-worker 调 Replay REST API
  -> Video File Sink Adapter 保存片段
  -> media-worker 回写 events.clip_path
```

MVP 阶段仍然不阻塞主事件闭环；Replay 可作为后续独立阶段启用。
