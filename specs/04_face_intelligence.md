# 04_face_intelligence.md

## 1. 目标

人脸智能链路统一支撑：

1. 重点人员布控。
2. 轨迹追踪。
3. 一键找人。

底层统一流程：

```text
person/head ROI
  -> SCRFD_2.5G
  -> face quality filter
  -> ArcFace embedding
  -> PostgreSQL + pgvector
  -> business policy
```

## 2. 设计原则

1. 不把重点人员布控、轨迹追踪、一键找人写成三套算法。
2. 清晰人脸才进入 ArcFace。
3. 同一 track 在 cooldown 内不重复提取 embedding。
4. PostgreSQL + pgvector 是第一版向量库。
5. 通过 FaceVectorStore 接口封装 pgvector，后期可切换 Qdrant。
6. 命中结果必须有阈值、质量分、截图、摄像头、时间和审计信息。

## 3. Face Intelligence Pipeline

```text
YOLO26-pose person detection
  -> track_id
  -> head ROI estimation
  -> SCRFD_2.5G face detection
  -> landmark and bbox validation
  -> face quality score
  -> ArcFace embedding
  -> FaceObservation
  -> gallery search / live search / observation storage
```

## 4. Head ROI 选择

优先级：

1. 如果 nose、eyes、ears 可信，基于关键点估计 head ROI。
2. 如果关键点不可信，基于 person bbox 上半部分估计 head ROI。
3. 如果 person 太小或遮挡严重，不做人脸检测。

默认过滤：

```text
person_height >= 80 px
face_attempt_interval_ms >= 1000
同一 track 已成功识别后 cooldown
```

## 5. SCRFD_2.5G 输出

统一结构：

```python
class FaceDetection:
    source_id: str
    camera_id: str
    track_id: str | None
    timestamp_ms: int
    face_bbox: BBox
    landmarks: list[Point] | None
    confidence: float
    person_bbox: BBox | None
    face_quality: float | None
```

## 6. Face Quality Filter

只有满足质量条件的人脸进入 ArcFace。

建议条件：

```text
face_confidence >= 0.6
face_width >= 24
face_height >= 24
landmarks 完整
模糊度通过
角度可接受
不严重遮挡
```

第一版可先用简化质量评分：

```text
quality = weighted(face_confidence, face_size, landmark_score)
```

## 7. ArcFace Embedding

ArcFace 输入：

```text
aligned face crop, 112x112
```

输出：

```python
class FaceEmbedding:
    face_observation_id: str
    embedding: list[float]
    dimension: int
    model_name: str
    quality: float
```

要求：

- embedding 应 L2 normalize。
- 存入 PostgreSQL 的向量维度必须与 pgvector 定义一致。
- 模型版本必须存储，方便后期升级和兼容。

## 8. FaceObservation

核心记录：

```python
class FaceObservation:
    id: str
    source_id: str
    camera_id: str
    track_id: str | None
    timestamp_ms: int
    person_bbox: BBox | None
    face_bbox: BBox
    landmarks: list[Point] | None
    quality: float
    embedding: list[float]
    matched_person_id: str | None
    match_score: float | None
    snapshot_path: str | None
    event_id: str | None
```

## 9. 重点人员布控

### 9.1 流程

```text
管理员注册人员照片
  -> 提取 ArcFace embedding
  -> 存 person_gallery_embeddings
  -> 创建 watchlist_rule
  -> 实时 FaceObservation 与 gallery 比对
  -> 超过 threshold
  -> watchlist_hit
```

### 9.2 事件

```json
{
  "event_type": "watchlist_hit",
  "person_id": "p_001",
  "person_name": "张三",
  "camera_id": "cam_012",
  "track_id": "t_889",
  "match_score": 0.84,
  "threshold": 0.75,
  "snapshot_path": "/data/events/...",
  "timestamp_ms": 1710000000000
}
```

### 9.3 必须支持

- enabled。
- threshold。
- cooldown_seconds。
- camera_scope。
- 人工确认。
- 误报标记。
- audit_log。

## 10. 轨迹追踪

轨迹追踪是被动记录。

```text
清晰人脸出现
  -> FaceObservation 入库
  -> 关联 camera_id / track_id / timestamp / snapshot
```

不一定告警。

用途：

- 查询某个人历史出现位置。
- 查询某个 track 的人脸证据。
- 查询陌生人是否重复出现。
- 为一键找人提供历史和实时基础。

## 11. 一键找人

### 11.1 流程

```text
用户输入人名
  -> FastAPI 查 persons
  -> 创建 live_search_job
  -> 实时 FaceObservation 与目标 embedding 比对
  -> 命中
  -> live_search_hit
  -> 大屏提示当前摄像头
```

### 11.2 live_search_job

```python
class LiveSearchJob:
    id: str
    person_id: str
    status: str
    threshold: float
    camera_scope: list[str] | None
    created_by: str
    created_at: datetime
    expires_at: datetime
    last_hit_at: datetime | None
```

### 11.3 事件

```json
{
  "event_type": "live_search_hit",
  "job_id": "job_123",
  "person_id": "p_001",
  "person_name": "张三",
  "camera_id": "cam_018",
  "location": "东门入口",
  "track_id": "t_391",
  "match_score": 0.85,
  "snapshot_path": "/data/events/..."
}
```

### 11.4 约束

- job 必须有 expires_at。
- job 可手动停止。
- job 命中后可继续监控或自动结束，由配置决定。
- 命中必须有 cooldown。

## 12. VectorStore 接口

```python
class FaceVectorStore:
    def add_gallery_embedding(self, person_id, embedding, metadata): ...
    def add_observation_embedding(self, observation_id, embedding, metadata): ...
    def search_gallery(self, embedding, threshold, limit, camera_scope=None): ...
    def search_observations(self, embedding, time_range=None, camera_scope=None, limit=50): ...
    def search_live_target(self, job_id, embedding): ...
```

第一版实现：`PgVectorFaceVectorStore`。

后期可实现：`QdrantFaceVectorStore`。

## 13. 阈值建议

第一版默认：

```text
face_min_quality: 0.65
watchlist_threshold: 0.75
live_search_threshold: 0.75
same_track_face_cooldown_s: 10
watchlist_alert_cooldown_s: 60
```

最终阈值必须通过现场数据校准。

## 14. 审计与合规

必须记录：

- 谁注册了人员。
- 注册照片来源。
- 何时创建布控。
- 何时命中。
- 谁确认或标记误报。
- 人脸数据删除记录。

必须支持：

- 删除人员。
- 停用人员。
- 删除 gallery embedding。
- 清理过期 face_observations。
