# 04_face_intelligence.md

## 1. 目标

人脸智能链路统一支撑：

1. 重点人员布控。
2. 轨迹追踪。
3. 一键找人。

底层统一流程 (锁定于 Phase F1.1a, 2026-05-26):

```text
全帧
  -> YOLO26-pose 输出 person + keypoints + track_id
  -> YOLOv8-Face full-frame primary detector (输出 face + 5 landmarks)
  -> face-person association (复用 FaceRoiSelectorPyFunc, IoU on head ROI)
  -> face quality filter
  -> AdaFace 5-point landmark alignment + 112x112 preprocess
  -> AdaFace 嵌入 (in-pipeline, Savant module 内)
  -> security.face_observations  (metadata + embedding, NO image bytes)
  -> face-worker (CPU 业务) -> PostgreSQL + pgvector
  -> watchlist / live_search / observation 存储
  -> business policy
```

> **历史 SCRFD_2.5G 第一版路线已替换**:
> 旧版描述将 SCRFD_2.5G 当作 secondary detector 跑在 person/head ROI
> 上, ArcFace 当作第一版 embedder. 这两条都被 Phase F1.1a 取代:
>
> - SCRFD_2.5G 保留为 future detector candidate (见 §15).
> - ArcFace 保留为 future embedding alternative (见 §15).
> - 第一版 detector 为 YOLOv8-Face full-frame primary.
> - 第一版 embedder 为 AdaFace, 仍在 Savant module 内.
> - 第一版 vector store 为 PostgreSQL + pgvector (HNSWLIB / Qdrant
>   不进入 MVP).
> - 重点人员布控 / 轨迹追踪 / 一键找人三个业务仍**复用同一条统一
>   Face Intelligence Pipeline**, 不会拆成三套独立链路.
>
> 详见 `docs/phase_f1_1a_in_pipeline_face_architecture_lock.md`.

## 2. 设计原则

1. 不把重点人员布控、轨迹追踪、一键找人写成三套算法。
2. 清晰人脸才进入 embedding 流程。
3. 同一 track 在 cooldown 内不重复提取 embedding。
4. PostgreSQL + pgvector 是第一版向量库。
5. 通过 FaceVectorStore 接口封装 pgvector，后期可切换 Qdrant。
6. 命中结果必须有阈值、质量分、截图、摄像头、时间、NVR 引用和审计信息。

## 3. Face Intelligence Pipeline

```text
YOLO26-pose person detection
  -> track_id (nvtracker)
  -> YOLOv8-Face full-frame primary detection
  -> face-person association (FaceRoiSelectorPyFunc, F1.1a 起的新职责)
  -> landmark and bbox validation
  -> face quality score
  -> AdaFace embedding (in-pipeline)
  -> FaceObservation (含 embedding) 写入 Redis security.face_observations
  -> face-worker (CPU): 入库 + pgvector 检索 + watchlist / live_search 判断
```

Pipeline 责任分界 (F1.1a 锁定):

- **Savant module**: detector + association + quality + AdaFace
  embedding + Redis publish. 不访问 PostgreSQL / FastAPI / pgvector.
- **face-worker**: CPU only. 消费带 embedding 的 observation, 入库,
  跑 pgvector 检索, 产出 watchlist_hit / live_search_hit.

## 4. Face-person association (FaceRoiSelectorPyFunc, F1.1a 重定义)

YOLOv8-Face 是 full-frame primary detector, 它独立输出 face bbox 与
5 个 landmarks; 不需要也不应该从 person ROI 截图后再跑 secondary
inference. 因此 `FaceRoiSelectorPyFunc` 从 F1.1a 起的职责变成 face-
person association:

1. 接收 YOLO26-pose 输出的 person object (含 track_id, keypoints).
2. 接收 YOLOv8-Face 输出的 face object (含 face bbox, landmarks).
3. 基于 head ROI (复用 F0 `estimate_head_roi_from_person` 估计的头部
   区域) 与 face bbox 计算 IoU.
4. 贪心匹配: 每个 face 取 IoU 最高的 person, 并把 person 的
   `track_id` 继承到 face object.
5. 匹配不到 person 的 face, `track_id = None`, 仍然向下游传递.

约束:

- 不做图像裁剪.
- 不触发额外推理.
- 不调用数据库.
- 不调用 FastAPI.
- 每个 track 默认 1 Hz throttle, 已成功识别的 track 在 cooldown 内
  不重复 embedding.

默认过滤:

```text
person_height >= 80 px
face_attempt_interval_ms >= 1000
同一 track 已成功识别后 cooldown
```

如后续命名歧义明显, 可在 F1.2 之后将该 PyFunc 重命名为
`FacePersonAssociatorPyFunc`. F1.1a 不做重命名.

## 5. YOLOv8-Face 输出

统一结构 (与历史 SCRFD_2.5G 输出兼容):

```python
class FaceDetection:
    source_id: str
    camera_id: str
    track_id: str | None       # 来自 face-person association, 可能为 None
    timestamp_ms: int
    face_bbox: BBox            # 全帧坐标
    landmarks: list[Point]     # 5 个 (eyes, nose, mouth corners)
    confidence: float
    person_bbox: BBox | None
    face_quality: float | None
    model_name: str            # "yolov8_face" 或后续替换的 detector 名
    model_version: str | None
```

`FaceDetection` 数据模型本身与 detector 无关, 由 F0 引入. SCRFD 切换
时只需更换 `model_name` / `model_version` 即可保持下游消费者不变.

## 6. Face Quality Filter

只有满足质量条件的人脸进入 embedding 流程。

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

### 6.b ReID Quality Gate (F2.2)

在 embedding 写入 Redis 之前，必须经过质量门控：

```text
YOLOv8-Face -> FacePersonAssociator -> AdaFace -> FaceReidGate -> Redis (future)
```

门控规则：

1. 必须有 person_track_id
2. face confidence >= 0.6
3. face bbox min width/height >= 40px
4. landmarks = 5 points
5. feature dim = 512
6. embedding L2 norm ~= 1.0 (tolerance ±0.10)
7. 无 NaN feature values

节流策略：每个 camera_id + person_track_id 最少间隔 1000ms。

配置环境变量：

- `FACE_REID_MIN_CONFIDENCE` (default 0.6)
- `FACE_REID_MIN_FACE_SIZE` (default 40)
- `FACE_REID_MIN_INTERVAL_MS` (default 1000)
- `FACE_REID_NORM_TOLERANCE` (default 0.10)

## 7. AdaFace Embedding (in-pipeline)

AdaFace 在 Savant module 内推理. 不在 face-worker, 也不在外部
Triton 服务.

AdaFace 输入：

```text
aligned face crop, 112x112  (基于 YOLOv8-Face 输出的 5 landmarks 对齐)
```

输出：

```python
class FaceEmbedding:
    face_observation_id: str
    embedding: list[float]
    dimension: int
    model_name: str            # "adaface"
    quality: float
```

要求：

- embedding 应 L2 normalize. 若模型已内置 normalize, converter 不重复;
  若未内置, converter 必须归一化.
- 颜色通道顺序 / 像素归一化策略必须与导出的 AdaFace ONNX 一致, 由模
  型资产清单 (`docs/model_assets_manifest.md`) 在 F2 启动前确认.
- 存入 PostgreSQL 的向量维度必须与 pgvector `vector(N)` 定义一致.
- 模型版本必须存储 (`model_name` + `model_version`), 方便后期升级和
  兼容.

### 7.b ArcFace (future embedding alternative)

ArcFace 保留为未来替换 AdaFace 的候选, 不在第一版实现. 替换路径:

1. 准备 ArcFace ONNX + 资产清单条目.
2. 实现 `custom.converters.arcface.ArcFaceEmbeddingConverter`.
3. 更新 module.yml 中 `nvinfer@classifier` 的 model 配置.
4. 发布 phase doc 推翻 F1.1a (说明为什么换).

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
    snapshot_path: str | None
    crop_path: str | None
    nvr_reference: dict | None
```

说明：

- `FaceObservation` 是事实源记录，不直接保存 `matched_person_id` / `match_score`。
- 搜索命中结果属于派生数据，应进入独立的 `match_results` 表。

## 9. 重点人员布控

### 9.1 流程

```text
管理员注册人员照片
  -> 提取 AdaFace embedding
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

F3.3 先锁定历史查询，不讨论实时告警或实时追踪事件。

### 11.1 模式 A: 已登记人员轨迹查询

```text
用户选择已登记 person
  -> 读取 person_gallery_embeddings
  -> 在 face_observations 中检索历史相似记录
  -> 结果写入 match_results
  -> 返回时间/摄像头/track/NVR 引用
```

约束：

- query 来源于 `persons` + `person_gallery_embeddings`
- 不产生新长期向量
- 不创建 `live_search_job`

### 11.2 模式 B: 临时上传人脸查历史

```text
用户临时上传一张人脸
  -> 提取临时 query embedding
  -> 在 face_observations 中检索历史相似记录
  -> 结果写入 match_results
  -> 返回时间/摄像头/track/NVR 引用
```

约束：

- 不创建 `person`
- 不写 `person_gallery_embeddings`
- 上传原图和 query embedding 不长期保存

### 11.3 `match_results`

`match_results` 是“一次历史搜索”的短期派生结果表，不是事实源表。

每条记录至少包含：

- `search_request_id`
- `search_mode`
- `query_person_id` 或临时查询上下文
- `matched_observation_id`
- `similarity`
- `rank`
- `snapshot_path`
- `nvr_reference`
- `expires_at`

### 11.4 与 F4/F5 的边界

- F3.3 只定义历史查询结果，不实现 `watchlist_hit`
- F3.3 只定义历史查询结果，不实现 `live_search_hit`
- 如未来需要实时模式，可在 F4/F5 另行引入 `watchlist_rules` /
  `live_search_jobs`

## 12. VectorStore 接口

```python
class FaceVectorStore:
    def add_gallery_embedding(self, person_id, embedding, metadata): ...
    def add_observation_embedding(self, observation_id, embedding, metadata): ...
    def search_gallery(self, embedding, threshold, limit, camera_scope=None): ...
    def search_observations(self, embedding, time_range=None, camera_scope=None, limit=50): ...
    def search_live_target(self, job_id, embedding): ...
```

第一版实现：`PgVectorFaceVectorStore` (PostgreSQL + pgvector).

**F3.2 已实现**: `FaceVectorStore.search_similar_faces()` — 精确 cosine
distance (`<=>`) 搜索，支持 `top_k` / `min_similarity` / `camera_scope` /
`include_embedding`。代码位于 `services/face-worker/app/vector_store.py`。

**F3.4 已实现**: `FaceVectorStore.search_gallery()` — 精确 cosine distance 搜索
`person_gallery_embeddings`，支持 `top_k` / `min_similarity` / `person_ids` /
`include_embedding`。自动排除 inactive person 和 inactive gallery embedding。
gallery embedding 的增删改由 `GalleryRepository` (`services/face-worker/app/gallery_repository.py`)
负责。

**F3.5 已实现**: Gallery Match Harness — 给定一个 `face_observation`，加载其
embedding，在 `person_gallery_embeddings` 中搜索 topK，结果写入 `match_results`。
代码位于 `services/face-worker/match_gallery.py` 和
`services/face-worker/app/match_repository.py`。
gallery_match 语义：
- `query_observation_id` / `query_source_observation_id`：输入的 face_observation
- `query_gallery_embedding_id`：匹配到的 gallery embedding
- `matched_observation_id`：NULL（无历史观测目标）
- 幂等键：`UNIQUE(search_request_id, query_gallery_embedding_id)`
- 不产生 `watchlist_hit` / `live_search_hit` / `security.events`

`add_observation_embedding` / `search_live_target` 留待 F4/F5。

**MVP 不实现** `HnswlibFaceVectorStore`, **MVP 不实现**
`QdrantFaceVectorStore`. 接口保留是为了未来切换, 不是因为现在要切换.

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

## 13.b 向量保留策略 (F3.3)

长期保存：

- `person_gallery_embeddings.embedding`
- `person_gallery_embeddings.embedding_norm`

短期保存：

- `face_observations.embedding`
- `face_observations.embedding_norm`

默认策略：

```text
person_gallery_embeddings: 长期保存，人工管理
face_observations embedding: 默认保留 30 天可检索
face_observations row metadata: 默认保留到 180 天
临时上传 query embedding: 请求结束即删除，或 TTL <= 24h
```

规则：

- observation 命中过搜索后，不立即删除其 embedding
- observation row 保留可长于 observation embedding 保留
- 临时上传图与临时 query embedding 不进入长期表

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
- 清理过期 `match_results`。
- 清理临时上传图和临时 query embedding。

## 15. Future alternatives (deferred)

下列方案在 F1.1a 阶段被锁定为 **未来候选**, 不进入第一版:

| 候选 | 用途 | 触发条件 |
|---|---|---|
| SCRFD_2.5G | future detector swap | 真实场景需要更高的小脸召回率, 且能解决 InsightFace 权重商用授权 |
| ArcFace | future embedding swap | 业务需要切换 backbone, 或 AdaFace 在现场指标不佳 |
| HNSWLIB | future in-process index | pgvector 在实际规模下检索延迟无法接受 |
| Qdrant | future external vector DB | 规模超出 pgvector 合理范围, 或与外部生态集成需要 |
| Savant module chaining via ZMQ | scaling option | 单 module 在双 T4 上 GPU 饱和, 检测 + 嵌入需拆模块运行 |

任何切换必须由一份新的 phase doc 推翻 F1.1a, 在 spec 中默默回退视为
违规.
