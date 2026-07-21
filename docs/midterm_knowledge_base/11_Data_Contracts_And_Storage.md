---
type: data-contracts
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - database
  - redis
  - evidence
  - storage
---

# 数据契约与存储

## 总体原则

- PostgreSQL 是配置、业务身份、事件、调度状态和 evidence 索引事实源；
- Redis Streams 提供有界 delivery，不是最终状态源；
- Replay 和 rolling 保存在线原始视频窗口；
- 文件系统保存最终视频/图片，数据库保存可查询索引和展示 metadata；
- generated YAML/JSON 是快照；
- 状态和媒体身份必须按 source、runtime epoch、stream session、frame UUID/PTS 隔离。

## PostgreSQL 表组

| 表组 | 代表表 | 主要 owner |
| --- | --- | --- |
| 摄像头配置 | `cameras`, `camera_zones`, `camera_rules` | API |
| 事件 | `events`, alerts/audit rows | event-worker / face-worker |
| 人员图库 | `persons`, `person_gallery_embeddings` | API registration |
| 图库同步 | `gallery_vector_sync_outbox` | API/face sync worker |
| 人脸观测 | `face_observations`, `match_results` | face-worker |
| 人体轨迹 | `person_bbox_observations` | person-observation-worker |
| 证据任务 | `evidence_tasks`, `evidence_event_links` | event/clip/media workers |
| 证据索引 | `evidence_bundles`, `evidence_artifacts`, `evidence_frame_timeline`, `evidence_overlay_segments` | media-worker |

## 摄像头身份

- `cameras.id`：DB camera id；
- `source_id`：跨 Replay/Savant/Redis/rolling/evidence 的稳定运行身份；
- `camera_name`：用户展示；
- `enabled`：是否加入当前运行；
- zone 的稳定引用是文本 `zone_id`，migration 016 处理历史类型兼容；
- camera/rule 保存后导出快照，但不应自动做全量 apply。

## Evidence materialization v2

权威词汇：`libs/evidence_lifecycle/contract.py`。

### 状态

```text
manifest_ready
materialization_pending
materializing
materialized
materialization_deferred
materialization_failed
materialization_expired
materialization_skipped
```

### Phase

```text
waiting_ready
waiting_coverage
image_running
remux_running
finalizer_pending
finalizing
terminal
manual_quarantine
```

### 关键字段族

- 身份：`event_id`, `source_event_id`, `source_id`, `runtime_epoch_id`；
- 时序：`materialization_ready_at`, `materialization_next_attempt_at`, deadline；
- 所有权：`materialization_owner`, lease owner/token/generation/expiry/heartbeat；
- 交接：`materialization_handoff`；
- 原因：retry/defer/failure/expired reason，互斥使用；
- Replay：slot owner/token/generation、create state、plan hash、request/delivery id；
- 清理：`cleanup_audit` 中 durable pending/result。

合同：

- ready time 是原始策略时间，retry 不得移动它；
- deferred 是终态，retryable reason 留在 pending；
- terminal 状态清空 lease/next-attempt；
- lease/fence 丢失后不得发布、提交或删除；
- rolling recovery 由 media-worker 唯一拥有；Replay recovery 由 clip-worker 唯一拥有；
- DB terminal commit 前不删除 source artifact。

## Migrations 029–032

| Migration | 作用 | 部署注意 |
| --- | --- | --- |
| 029 | lifecycle、owner、lease、handoff、历史分类/隔离 | ambiguous active row fail closed 到 quarantine |
| 030 | lifecycle claim/recovery indexes | 与 Scheduler V2 查询口径一致 |
| 031 | Replay slot/create fencing | 防止旧 clip worker 重复提交 |
| 032 | cleanup_pending、algorithm cooldown hot indexes | concurrent、autocommit、非压力窗口 |

不要仅部署新 worker 而漏 migration，也不要只应用 migration 后继续运行旧镜像而不做
兼容性检查。

## Redis Streams

| Stream | Producer | Consumer |
| --- | --- | --- |
| `security.events` | Savant behavior / face-worker | event-worker |
| `security.person_observations` | behavior rules | person-observation-worker |
| `security.face_rois` | Savant ROI exporter | adaface-roi-worker |
| `security.face_observations` | Savant single path / ROI worker | face-worker |
| `security.frame_annotations` | Savant | latency、单分支兼容 proof |
| `security.frame_annotations.<source>` | Savant full preset | media-worker source-scoped lookup |
| `security.record_requests` | event-worker normal path | clip-worker |
| `security.alerts` | event-worker | API WebSocket router |

Redis contract：

- consumer group pending/lag 必须观测；
- stream trimming 不能删除未消费高率轨迹；因此 person 消费已拆独立进程并提高 maxlen；
- record request 使用 `SET NX EX` 幂等键，不扫描全 stream；
- full preset suppress record request；
- source-scoped annotation 在 full preset 不允许静默 fallback global。

## 人脸向量

- PostgreSQL `person_gallery_embeddings` 是事实源，512 维；
- 当前 env 默认 `FACE_VECTOR_BACKEND=pgvector`；
- Qdrant profile、collection/alias、outbox 和 exact rerank 仍受支持；
- Qdrant 只在显式启用并观测 bootstrap/sync/fallback 后才是运行查询后端；
- 历史 Qdrant benchmark 不改变当前默认配置。

## Evidence 索引

`evidence_bundles` 是 list/detail 主入口；`evidence_artifacts` 保存媒体/manifest 逻辑，
timeline 与 overlay 是 8090 的帧级展示来源。

- DB 有 bundle 但媒体缺失：不可播放；
- 文件存在但 DB 无 bundle：主页面不可见；
- annotation degraded 必须显式标注，不能伪装 complete；
- image evidence 与 video evidence 可以在不同产品页面展示；
- covered child 必须可追到 parent/physical evidence。

## 时间域

| 时间/身份 | 用途 |
| --- | --- |
| `frame_uuid` / keyframe UUID | 视觉身份绑定 |
| 原始 `frame_pts` | 事件、轨迹、annotation、窗口映射 |
| `rolling_cache_mux_pts` | 稳定 MOV/segment/crop |
| `event_ts_ms` | 业务事件时间 |
| `created_at` | 入库/机器时间 |
| Redis stream id | delivery/诊断，不是媒体锚点 |

原始 PTS 与 mux PTS 的映射必须通过同一 source/session/epoch 和 frame identity 完成。

## 存储路径

| 路径 | 作用 |
| --- | --- |
| `/data/video-analytics/models` | 共享模型 |
| `/data/video-analytics/models-savant-b` | B 分支独立 engine cache |
| `/data/video-analytics/replay-midterm*` | Replay RocksDB |
| `/home/user/video-analytics-fast/rolling-cache` | 默认 rolling 在线窗口 |
| `/home/user/video-analytics-fast/rolling-cache-materialized` | 默认中间物化 |
| `/data/video-analytics/media/evidence` | 最终 evidence |
| `/home/user/video-analytics-fast/face_trajectory_cache` | 轨迹展示 cache |
| `/data/video-analytics/artifacts` | 压测/诊断证据 |

host 路径可被环境变量覆盖，以 effective Compose mount 为准。

## 改动检查表

- 是否有 migration/upgrade/idempotence 路径；
- 是否改变 status/phase/owner/reason；
- 是否改变 stream producer/consumer/maxlen/ACK；
- 是否改变 source/epoch/session/frame identity；
- 是否改变 raw/mux/business 时间域；
- 是否保持 DB-backed list/detail；
- 是否需要新索引和在线创建策略；
- 是否同步当前架构、部署和 API 文档。
