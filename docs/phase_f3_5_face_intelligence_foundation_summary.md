# Face Intelligence Foundation — F1 至 F3.5 阶段总结

本文档总结截至 F3.5 的人脸智能链路已完成能力、当前缺失能力、技术债和后续路线。

## 1. 已完成 Phases 和 Commits

### GPU Pipeline (Savant Module)

| Phase | 能力 | 关键 Commit |
|---|---|---|
| F0 | 人脸检测策略锁定：YOLOv8-Face full-frame primary | `4a41c62` 系列 |
| F1.1a | 架构锁定：YOLOv8-Face + AdaFace in-pipeline | `8759fe1` 系列 |
| F1.2 | YOLOv8-Face ONNX 验证 + Savant module config | `25a1c9d` 系列 |
| F1.3 | face-person association (head ROI + IoU) | `8759fe1` 系列 |
| F2.0 | AdaFace ONNX 验证 | `a667195` 系列 |
| F2.1 | AdaFace Savant runtime (5-pt alignment, 112x112) | `a667195` 系列 |
| F2.2 | face ReID gate (confidence, size, landmarks, quality) | `a667195` 系列 |
| F2.3 | face observation Redis exporter (security.face_observations) | `a667195` 系列 |

### CPU Worker + Database

| Phase | 能力 | 关键 Commit |
|---|---|---|
| F3.1 | face-worker: Redis → PostgreSQL (face_observations) | `bad8d2b` |
| F3.2 | FaceVectorStore: pgvector cosine distance search | `7d2bfb9` |
| F3.3 | gallery retention / match policy 设计文档 | `d6ba91e` |
| F3.4 | gallery schema (persons, person_gallery_embeddings, match_results) + enrollment CLI | `36f8ded`, `30df736` |
| F3.5 | gallery match harness (observation → gallery search → match_results) | `0e65bc4` |

### Migration 文件

```text
001_init.sql                                    — cameras, events 基础表
002_phase2e_events.sql                          — security.events
003_phase2h_audit_logs.sql                      — audit_logs
004_phase_c1_camera_config.sql                  — camera_config
005_phase_f3_1_face_observations.sql            — face_observations + pgvector
006_phase_f3_4_gallery_schema.sql               — persons, person_gallery_embeddings, match_results
007_phase_f3_5_match_results_gallery_semantics.sql — gallery_match 语义修复
```

## 2. 当前完整链路

```text
RTSP 视频流
  → Savant Source Adapter (GPU 解码)
  → YOLO26-pose (person + keypoints + track_id)
  → YOLOv8-Face full-frame primary (face bbox + 5 landmarks)
  → face-person association (head ROI IoU)
  → face quality filter (confidence, size, quality score)
  → face ReID gate (per-track throttle, feature validation)
  → AdaFace embedding (5-pt alignment → 112×112 → vector(512))
  → Redis Stream: security.face_observations
  → face-worker (CPU, consumer group)
  → PostgreSQL: face_observations (embedding vector(512))
  → FaceVectorStore.search_gallery() (pgvector cosine distance)
  → PostgreSQL: match_results (ranked topK results)
```

## 3. 当前数据表

### face_observations (F3.1)

实时人脸观测记录。每条记录代表 Savant pipeline 检测到的一个清晰人脸。

```text
主键: id (UUID)
幂等键: source_observation_id (TEXT UNIQUE)
向量: embedding vector(512) NOT NULL
关联: camera_id, source_id, track_id, timestamp_ms
元数据: face_bbox, landmarks, face_confidence, quality
来源: Savant GPU pipeline via Redis Stream
```

### persons (F3.4)

已注册人员主数据。

```text
主键: id (BIGSERIAL)
标识: name, external_person_id (可选唯一)
状态: is_active (soft delete)
```

### person_gallery_embeddings (F3.4)

已注册人员的长期 gallery 向量。

```text
主键: id (BIGSERIAL)
外键: person_id → persons(id) CASCADE
向量: embedding vector(512) NOT NULL
约束: 每个 active person 最多一个 is_primary=true
来源: enroll_gallery.py CLI 从 face_observations 注册
```

### match_results (F3.4/F3.5)

短期派生搜索结果。

```text
主键: id (BIGSERIAL)
gallery_match 幂等键: UNIQUE(search_request_id, query_gallery_embedding_id)
query 侧: query_observation_id, query_source_observation_id
gallery 侧: query_person_id, query_gallery_embedding_id
matched 侧: matched_observation_id (NULL for gallery_match)
排序: rank, similarity
TTL: expires_at
```

## 4. 当前没有完成的能力

| 能力 | 状态 | 说明 |
|---|---|---|
| watchlist_hit | 未实现 | 命中布控名单后产生告警事件 |
| live_search_hit | 未实现 | 一键找人命中后产生实时通知 |
| security.events 输出 | 未实现 | match 结果未写入 security.events 表 |
| REST API | 未实现 | enrollment / match / query 均为 CLI |
| retention deletion | 未实现 | 过期 match_results / face_observations 清理 |
| trajectory query | 未实现 | 按 person 查询历史出现轨迹 |
| NVR reference | 仅结构设计 | nvr_reference JSONB 字段存在但未接真实 NVR |
| ANN 索引 | 未实现 | pgvector 精确搜索，无 HNSW/IVFFlat |
| watchlist_rules | 未实现 | 布控规则配置表 |
| live_search_jobs | 未实现 | 实时搜索任务表 |

## 5. 四条后续路线

### A. Watchlist 实时告警 (F4)

```text
管理员注册 persons + gallery embeddings
  → 创建 watchlist_rules (threshold, camera_scope, cooldown)
  → face-worker 实时消费 face_observations
  → search_gallery() 比对 gallery
  → 命中 → security.events (watchlist_hit)
  → 告警推送 (WebSocket / API)
```

需要新增：watchlist_rules 表、watchlist_hit 事件类型、实时 gallery 比对逻辑、告警推送。

### B. 一键找人 — 临时上传 (F5)

```text
用户上传一张人脸图片
  → 提取 AdaFace embedding (临时，不入库)
  → search_similar_faces() 在 face_observations 中检索
  → 结果写入 match_results (search_mode=temporary_face_history)
  → 返回时间/摄像头/track/NVR 引用
```

需要新增：临时 embedding 提取 API、临时搜索 API、结果展示。

### C. 已注册人员轨迹查询 (F3.6 推荐下一步)

```text
用户选择已注册 person
  → 读取 person_gallery_embeddings
  → search_similar_faces() 在 face_observations 中检索历史
  → 结果写入 match_results (search_mode=registered_person_history)
  → 返回时间/摄像头/track/NVR 引用
```

需要新增：registered_person_history 模式、match_results 中 matched_observation_id 填充、轨迹聚合查询。

### D. Retention / Cleanup (F3.7)

```text
定时任务：
  → 清理过期 match_results (expires_at < now())
  → 清理过期 face_observations (embedding retention policy)
  → 清理 orphan gallery embeddings
```

需要新增：定时清理 worker 或 cron、retention policy 配置。

## 6. 推荐下一步：F3.6

**F3.6 — Registered Person Trajectory Query Harness**

理由：

1. F3.5 的 gallery_match 是"query observation → gallery"方向，已完成。
2. F3.6 的 registered_person_history 是"person → historical observations"方向，是 F3.5 的自然互补。
3. 两者共享 match_results 表和 MatchResultRepository，只需补充 observation-vs-observation 的 conflict target 和语义。
4. 完成后即可为 F4 (watchlist) 和 F5 (live search) 提供完整的 match 基础设施。
5. 不需要实时推理、不需要 Redis 告警推送、不需要 API，纯粹是 harness 阶段。

F3.6 范围：

- 使用已注册 person 的 gallery embedding
- 在 face_observations 中搜索历史相似记录
- 结果写入 match_results (search_mode=registered_person_history)
- matched_observation_id 填充为匹配到的历史 observation
- 幂等键: UNIQUE(search_request_id, matched_observation_id)
- CLI harness + 单元测试 + 集成测试
- 不做 API、不做实时告警、不做 security.events

## 7. 不要在下一步直接做 watchlist / live_search

原因：

1. watchlist_hit 需要实时消费链路 + 告警推送 + cooldown + camera_scope + audit，复杂度高。
2. live_search_hit 需要临时 embedding 提取 + 实时匹配 + 结果推送，依赖 API 层。
3. 两者都需要 F3.6 的 registered_person_history 作为基础验证。
4. 先完成 harness (F3.6)，再做实时链路 (F4/F5)，符合 CLAUDE.md 推荐开发顺序。

## 8. 当前技术债

| 项目 | 说明 | 建议 |
|---|---|---|
| modules/savant_phase1d stubs | 4 个 untracked `__init__.py` 文件，历史残留 | 确认无用后删除 |
| observation embedding retention | face_observations.embedding 永久保存，无过期清理 | F3.7 实现 retention policy |
| NVR reference | nvr_reference JSONB 字段已有结构设计，未接真实 NVR | 后续阶段按需接入 |
| face_vector_store 集成测试环境 | 不同环境 (CI / 本地 / Docker) 连接配置差异 | 统一 DATABASE_URL 注入方式 |
| match_results 双 UNIQUE 约束 | gallery_match 和 observation-vs-observation 两套幂等键共存 | F3.6 验证 observation-vs-observation 路径 |
| search_similar_faces vs search_gallery | 两个搜索方法分别查不同表，无统一 search 接口 | 后续可抽象统一 FaceSearchService |
