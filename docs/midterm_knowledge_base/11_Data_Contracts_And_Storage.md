---
type: data-contracts
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - database
  - redis
  - evidence
  - storage
---

# 数据契约与存储

本页把 midterm 栈的核心数据契约放在一起：PostgreSQL 表、Redis Streams、文件系统产物、
状态机和索引优化。它不是 schema 的替代品；权威细节仍以 `db/migrations/*.sql` 和服务代码为准。

## 总体原则

- PostgreSQL 是摄像头、规则、人员、图库和 evidence metadata 的事实源。
- Redis Streams 是运行时消息总线，不是长期事实源。
- Replay RocksDB 是全速视频存储和取证时间窗来源。
- 文件系统只长期保存视频和必要 artifact；证据列表、timeline、overlay、annotation metadata 以 DB-backed 语义查询。
- 8090 展示应优先读 DB-backed API，不应重新扫描整个 evidence 目录作为主路径。

## PostgreSQL 表组

| 表组 | 代表表 | 主要写入方 | 主要读取方 |
| --- | --- | --- | --- |
| 摄像头配置 | `cameras`, `camera_zones`, `camera_rules` | API / 8090 | config export, runtime apply, Savant rules |
| 事件 | `events` | event-worker, face-worker | 8090 event/evidence list, media-worker, clip-worker |
| 人员图库 | `persons`, `person_gallery_embeddings` | API / people registration | face-worker, 8090 |
| 人脸观测 | `face_observations`, `match_results` | face-worker | face search, evidence overlay, diagnostics |
| 人体观测 | `person_bbox_observations` | exporter / worker path | evidence overlay, analytics diagnostics |
| 证据任务 | `evidence_tasks` | event-worker, clip-worker, media-worker | clip-worker, media-worker, runtime guard |
| 证据索引 | `evidence_bundles`, `evidence_artifacts`, `evidence_frame_timeline`, `evidence_overlay_segments` | media-worker | 8090 evidence API |
| 审计 | `audit_logs`, storage maintenance audit | API / maintenance | 8090, ops audit |

## 摄像头配置表

`cameras` 描述 source 级别配置：

- `id` 是 DB 内部 camera id；
- `source_id` 是运行时视频源身份，必须稳定；
- `camera_name` 是 8090 应优先展示给用户的名称；
- `enabled` 表示是否应该导出并启动；
- RTSP URL、transport、动态 source 相关配置由 API 导出到 generated config。

`camera_zones` 描述 ROI / line / polygon：

- `zone_id` 是面向规则引用的稳定文本 ID；
- `zone_name` 是 8090 展示名；
- polygon/line 坐标通常是归一化画面坐标；
- 规则应引用 `zone_id`，而不是前端临时 label。

`camera_rules` 描述算法配置：

- `rule_id` 是规则稳定 ID；
- `algorithm_id` / `rule_type` 表示入侵、watchlist hit 等算法；
- `enabled` 控制该摄像头是否启用该规则；
- `config` 保存阈值、cooldown、ROI 选择、watchlist 目标等结构化参数。

重要不变量：

- 保存 ROI/算法规则只更新 DB 和导出运行快照，不应触发 full runtime apply。
- camera/rule 修改后要验收 `containers_restarted=[]` 或等效审计结果。
- 不要用 `cameras.midterm.yml` 手工判断最终配置真相，它只是 DB 导出的快照。

## 事件表

`events` 是告警和 evidence 的业务入口。典型字段语义：

- `id`：DB event UUID；
- `source_event_id`：来自 Savant/exporter 的事件 ID；
- `event_type` / `algorithm_type`：业务类型；
- `source_id` / `camera_id` / `camera_name`：源和展示身份；
- `event_ts_ms` / `start_ts_ms` / `created_at`：事件发生时间和入库时间；
- `status`：事件处理状态；
- payload/metadata：保留 exporter、规则、证据状态和 UI 展示字段。

事件表的热路径包括：

- 8090 最近事件/证据列表；
- event-worker source/type cooldown 和 duplicate 判断；
- media-worker 查找需要 snapshot/annotation/finalization 的事件；
- evidence API 按 camera/type/time 查询。

已加过的性能索引：

- `idx_events_created_at_desc`
- `idx_events_event_type_created_at_desc`
- `idx_events_source_type_created_at_desc_unsuppressed`
- `idx_events_camera_type_ts_desc_unsuppressed`
- media-worker queue partial indexes：`idx_events_media_*`

## 人脸和图库表

`persons` 保存人员实体。`person_gallery_embeddings` 保存注册图库：

- embedding 是 `vector(512)`；
- 一个人可以有多张注册图；
- 8090 注册人脸后，人员和图库都在 PostgreSQL，不在 `/data/video-analytics/downloads`。

`face_observations` 保存运行时检测到的人脸：

- `source_observation_id` 是 exporter 给出的稳定观测 ID；
- `embedding vector(512)` 用于 gallery/watchlist 查询；
- bbox、quality、track/person association 信息用于 evidence overlay 和诊断；
- 与 `match_results` 共同构成 gallery/watchlist 查询历史。

当前注意点：

- 小图库下 exact pgvector search 成本低，不代表生产大图库也低。
- 100 / 500 / 1000 人图库需要单独 `EXPLAIN ANALYZE` 和 p95/p99。
- 如果上 ANN index，仍要 exact rerank，避免阈值语义漂移。

## Evidence 任务表

`evidence_tasks` 是 event-worker、clip-worker、media-worker 之间的状态契约。

典型状态流：

```text
pending
  -> claimed / replay_job_created
  -> materializing / finalizing
  -> ready
  -> terminal failure / materialization_skipped / stale / expired
```

关键字段：

- `event_id`：关联 `events.id`；
- `source_event_id`：用于去重和跨流追踪；
- `source_id` / `event_type`：admission、并发和优先级使用；
- `status`：clip-worker 侧任务状态；
- `materialization_status`：media-worker 侧物化状态；
- `materialization_deadline_at`：300 秒窗口内的 deadline；
- `priority`：高价值事件优先，例如 watchlist hit。

已优化点：

- active/stale 状态会被 runtime guard 识别并终态收敛；
- pending record request 指向不存在 DB event/task 时，clip-worker 会清理并 `XACK`；
- `evidence_tasks` 有 pending/materialization priority 索引和 active source/type 索引。

## Evidence 索引表

`evidence_bundles` 是 8090 evidence list/detail 的主入口：

- event/camera/source 元数据；
- media 状态；
- raw clip 路径；
- created/event time；
- playable/degraded 信息。

`evidence_artifacts` 保存每个 evidence 的文件或逻辑 artifact：

- raw clip；
- snapshot；
- manifest；
- overlay/timeline metadata；
- 其他可审查输出。

`evidence_frame_timeline` 和 `evidence_overlay_segments` 保存帧级时间线和展示片段：

- 它们用于让 8090 展示“什么时候识别到人脸/人体/命中名单”；
- raw clip 可播放但 frame metadata 缺失时，bundle 可以 degraded，而不是失败。

## Redis Streams

| Stream | 写入方 | 消费方 | 语义 |
| --- | --- | --- | --- |
| `security.events` | Savant exporters, face-worker watchlist emitter | event-worker | 行为事件和 watchlist hit |
| `security.face_observations` | Savant face exporter | face-worker | 人脸观测、embedding、质量信息 |
| `security.person_observations` | Savant person exporter | worker/diagnostics | 人体框/轨迹观测 |
| `security.record_requests` | event-worker | clip-worker | 取证请求 |
| `security.frame_annotations` | Savant frame annotation exporter | clip/media evidence path | 帧级 overlay/timeline proof |

Redis 使用约定：

- consumer group lag/pending 是压测必须记录的指标；
- pending 不为 0 不一定是故障，要看是否持续增长、是否对应已不存在 DB task；
- `security.frame_annotations` 是有界近似保留，不要按 60 路线性放大到无限；
- record request 去重已经从全流 `XRANGE - +` 改为 Redis `SET NX EX` 幂等键。

## Record request 去重契约

旧问题：

```text
RecordRequestPublisher.has_request()
  -> XRANGE security.record_requests - +
```

在长跑或 60 路事件风暴下，这是 O(N) 热点。

当前契约：

- key 由 stream、`source_event_id`、strategy 计算；
- `publish()` 在 `XADD` 前 `SET NX EX`；
- `XADD` 失败释放 key，允许重试；
- `has_request()` 只做 Redis `EXISTS`；
- duplicate retry task 标记为 duplicate skip，不留下活跃 pending；
- TTL 默认以天为单位，覆盖 replay/evidence 任务生命周期。

验证入口：

- `harness/tests/test_record_request_idempotency.py`
- `harness/tests/test_event_worker_recording_policy.py`
- pressure report 中 event-worker dedupe counters。

## 文件系统目录

| 路径 | 作用 | 是否干净迁移 |
| --- | --- | --- |
| `/data/video-analytics/models` | 模型文件 | 需要迁移 |
| `/data/video-analytics/media` | evidence/raw clip/runtime media | 默认不迁历史 |
| `/data/video-analytics/artifacts` | 压测和诊断 artifact | 默认不迁历史 |
| `/data/video-analytics/downloads` | 临时下载/导入区 | 当前不作为迁移必需 |
| Replay RocksDB 路径 | 全速视频存储 | 默认不迁历史 |
| PostgreSQL data | 业务状态 | 干净迁移默认不带 |
| Redis data | 运行消息状态 | 干净迁移默认不带 |

干净迁移原则：

- 代码和模型迁移；
- 旧 evidence、Replay、Redis、PostgreSQL 不迁，除非目标明确是业务数据迁移；
- 人脸注册数据在 PostgreSQL，因此新机器需要重新注册或另做 DB 迁移。

## 状态和时间口径

不要混用这些时间：

- 摄像头画面 PTS；
- Savant frame timestamp；
- event `event_ts_ms`；
- event `created_at`；
- evidence task `created_at`；
- materialization deadline；
- media-worker finalization 完成时间。

排障时要明确是在问：

- 事件是否产生；
- record request 是否产生；
- Replay job 是否成功；
- raw clip 是否写出；
- DB-backed bundle 是否 ready；
- 8090 是否能查到；
- overlay/annotation 是否 complete。

## 数据契约改动检查表

修改数据契约时必须回答：

- 是否新增/修改 DB migration；
- 是否影响旧数据兼容；
- 是否影响 Redis stream payload；
- 是否影响 8090 list/detail；
- 是否影响 evidence task 状态机；
- 是否影响 runtime guard；
- 是否需要新增索引；
- 是否需要 pressure report 新增字段；
- 是否需要更新本知识库。
