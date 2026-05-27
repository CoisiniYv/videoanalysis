# 05_database_schema.md

## 1. 数据库选择

第一版使用 PostgreSQL + pgvector。

原因：

- 业务数据和向量数据强关联。
- 摄像头、人员、事件、轨迹、审计天然适合关系型数据库。
- MVP 阶段减少 Qdrant 独立服务和双写复杂度。
- 通过 VectorStore 接口预留后期切换 Qdrant 的能力。

## 2. 扩展

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

## 3. cameras

```sql
CREATE TABLE cameras (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rtsp_url TEXT NOT NULL,
    site_id TEXT,
    location TEXT,
    gpu_id INTEGER,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX cameras_site_idx ON cameras(site_id);
CREATE INDEX cameras_enabled_idx ON cameras(enabled);
```

## 4. camera_zones

```sql
CREATE TABLE camera_zones (
    id BIGSERIAL PRIMARY KEY,
    camera_id TEXT REFERENCES cameras(id) ON DELETE CASCADE,
    zone_name TEXT NOT NULL,
    zone_type TEXT NOT NULL,
    points JSONB NOT NULL,
    payload JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX camera_zones_camera_idx ON camera_zones(camera_id);
```

`zone_type` 可选：

```text
polygon
line
direction_line
```

## 5. camera_rules

```sql
CREATE TABLE camera_rules (
    id BIGSERIAL PRIMARY KEY,
    camera_id TEXT REFERENCES cameras(id) ON DELETE CASCADE,
    rule_type TEXT NOT NULL,
    enabled BOOLEAN DEFAULT true,
    config JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX camera_rules_camera_type_idx ON camera_rules(camera_id, rule_type);
```

## 6. persons

```sql
CREATE TABLE persons (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    external_person_id TEXT,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_by TEXT,
    updated_by TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX persons_external_person_id_idx
    ON persons(external_person_id) WHERE external_person_id IS NOT NULL;
CREATE INDEX persons_name_trgm_idx ON persons USING gin (name gin_trgm_ops);
CREATE INDEX persons_active_idx ON persons(is_active);
```

`persons` 只表示”已登记人员”，不用于临时上传查历史。

字段约定：

- `external_person_id` — 外部系统人员标识（如工号、badge ID）。部分唯一索引
  (`WHERE external_person_id IS NOT NULL`)，允许多行为 null 但不允许重复非 null 值。
- CLI enrollment (`enroll_gallery.py`) 支持 `--external-person-id`，如果已存在则复用。

## 7. person_gallery_embeddings

```sql
CREATE TABLE person_gallery_embeddings (
    id                      BIGSERIAL PRIMARY KEY,
    person_id               BIGINT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    source_type             TEXT NOT NULL DEFAULT 'manual_upload',
    source_image_path       TEXT,
    source_observation_id   TEXT REFERENCES face_observations(source_observation_id) ON DELETE SET NULL,
    embedding_model         TEXT NOT NULL DEFAULT 'adaface',
    model_version           TEXT,
    embedding_dim           INTEGER NOT NULL DEFAULT 512 CHECK (embedding_dim = 512),
    embedding               vector(512) NOT NULL,
    embedding_norm          DOUBLE PRECISION NOT NULL,
    quality                 DOUBLE PRECISION,
    face_bbox               JSONB,
    landmarks               JSONB,
    is_primary              BOOLEAN NOT NULL DEFAULT false,
    is_active               BOOLEAN NOT NULL DEFAULT true,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX person_gallery_person_active_idx
ON person_gallery_embeddings(person_id, is_active);

CREATE UNIQUE INDEX person_gallery_one_primary_idx
ON person_gallery_embeddings(person_id)
WHERE is_primary = true AND is_active = true;
```

字段约定：

- 只存长期登记向量；临时上传 query embedding 严禁入表
- `source_observation_id` — 来源溯源。当 gallery embedding 从 `face_observations` 注册时，
  FK 到 `face_observations(source_observation_id)`，ON DELETE SET NULL。
- `embedding_model` / `model_version` 必须与 observation 检索向量空间兼容
- 一个人允许多条 active gallery embeddings
- F3.3 先锁定表设计，不在 spec 中要求 ANN/HNSW 索引立刻存在

## 8. face_observations (F3.1 current write contract, F3.3 target retention state)

```sql
CREATE TABLE face_observations (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_observation_id   TEXT NOT NULL UNIQUE,          -- 幂等 key
    camera_id               TEXT NOT NULL,
    source_id               TEXT NOT NULL,
    track_id                TEXT NOT NULL,
    timestamp_ms            BIGINT NOT NULL,               -- 视频/source timeline, 非 wall-clock
    captured_at             TIMESTAMPTZ,                   -- 可选的 wall-clock 时间
    frame_num               INTEGER,

    person_bbox             JSONB,                         -- nullable (非 person 场景)
    face_bbox               JSONB NOT NULL,
    landmarks               JSONB NOT NULL,                -- 10 floats

    face_confidence         DOUBLE PRECISION NOT NULL,
    quality                 DOUBLE PRECISION NOT NULL,
    detector_model          TEXT NOT NULL DEFAULT 'yolov8_face',
    embedding_model         TEXT NOT NULL DEFAULT 'adaface',
    model_version           TEXT,                          -- e.g. adaface_ir101_webface4m

    embedding_dim           INTEGER NOT NULL DEFAULT 512 CHECK (embedding_dim = 512),
    embedding               vector(512),                   -- F3.1 为 NOT NULL; F3.3 retention 目标允许后续置空
    embedding_norm          DOUBLE PRECISION,

    reid_throttle_key       TEXT NOT NULL DEFAULT '',
    association_score       DOUBLE PRECISION,
    association_method      TEXT,

    camera_config_resolved  BOOLEAN NOT NULL DEFAULT false,
    snapshot_path           TEXT,
    crop_path               TEXT,
    nvr_reference           JSONB,

    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,   -- 不含 embedding
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- btree indexes only (approximate vector index deferred to F3.2+)
CREATE INDEX idx_face_obs_source_observation_id ON face_observations(source_observation_id);
CREATE INDEX idx_face_obs_camera_id ON face_observations(camera_id);
CREATE INDEX idx_face_obs_source_id ON face_observations(source_id);
CREATE INDEX idx_face_obs_track_id ON face_observations(track_id);
CREATE INDEX idx_face_obs_timestamp_ms ON face_observations(timestamp_ms);
```

### 8.1 字段约定 (F3.1 锁定)

- `source_observation_id TEXT NOT NULL UNIQUE` — 幂等键。face-worker 用
  `ON CONFLICT DO NOTHING` 按此字段去重。
- `embedding vector(512)` — F3.1 当前实现要求写入时 embedding 必须存在。
  F3.3 retention 设计要求未来允许定期置空，因此 schema 目标态必须允许 null。
- `embedding_dim INTEGER NOT NULL DEFAULT 512 CHECK (embedding_dim = 512)` —
  与 vector(512) 配套，DB 层预防维度漂移。
- `embedding_norm DOUBLE PRECISION` — 写入时 worker 仍验证范围 [0.90, 1.10]；
  retention 置空后允许为 null。
- `face_bbox JSONB NOT NULL`, `landmarks JSONB NOT NULL` — 人脸检测输出，
  以 JSONB 存储，避免 PostgreSQL 原生数组的维度刚性。
- `person_bbox JSONB` — 关联的 person bbox，可为 null。
- `captured_at TIMESTAMPTZ` — 可选的 wall-clock 时间。当前
  `timestamp_ms` 是视频/source timeline，不一定是 wall-clock。
  不要错误转换为 observed_at，除非有真实 wall-clock 来源。
- `model_name` 不再独立存在 → 拆为 `detector_model` + `embedding_model`
  + `model_version`。`detector_model` = `yolov8_face`,
  `embedding_model` = `adaface`。
- `matched_person_id`, `match_score` 不在 F3.1 表中；这些属于
  watchlist/live_search 命中表 (F4/F5)。
- `snapshot_path`, `crop_path` — F3.1 为 null，后续 phase 补齐。
- `nvr_reference JSONB` — F3.3 预留回放定位信息，结构见 §9.1。
- `payload JSONB` 不包含 embedding 向量 (避免 ~8KB 重复存储)。
- 无 HNSW / IVFFlat / ANN 索引 — F3.1 是纯持久化，不检索。
  F3.2 增加精确 cosine distance (`<=>`) 搜索，仍无 ANN 索引（deferred to F3.3）。
- `FaceVectorStore.search_similar_faces()` (F3.2) 提供只读相似搜索，
  位于 `services/face-worker/app/vector_store.py`。
  支持 `top_k` / `min_similarity` / `camera_scope` / `include_embedding`。
  默认不返回 512-d embedding。
- 无 FK 到 cameras/persons — MVP 阶段保持松耦合。

### 8.2 F3.1 Redis → PostgreSQL 字段映射

F2.4 Redis Stream `security.face_observations` → face-worker → DB:

| Redis JSON field | PostgreSQL column | Notes |
|---|---|---|
| `source_observation_id` | `source_observation_id` | 幂等 key, UNIQUE |
| `camera_id` | `camera_id` | TEXT, NOT NULL |
| `source_id` | `source_id` | TEXT, NOT NULL |
| `track_id` | `track_id` | TEXT, NOT NULL |
| `timestamp_ms` | `timestamp_ms` | BIGINT, source timeline |
| — | `captured_at` | TIMESTAMPTZ, 暂无来源, null |
| `frame_num` | `frame_num` | INTEGER |
| `person_bbox` | `person_bbox` | JSONB, nullable |
| `face_bbox` | `face_bbox` | JSONB, NOT NULL |
| `landmarks` | `landmarks` | JSONB, NOT NULL |
| `face_confidence` | `face_confidence` | DOUBLE PRECISION |
| `quality` | `quality` | DOUBLE PRECISION |
| `detector_model` | `detector_model` | TEXT, "yolov8_face" |
| `embedding_model` | `embedding_model` | TEXT, "adaface" |
| `model_version` | `model_version` | TEXT, nullable (Redis 已有) |
| `embedding_dim` | `embedding_dim` | INTEGER CHECK=512 |
| `embedding` | `embedding` | vector(512) NOT NULL |
| `embedding_norm` | `embedding_norm` | DOUBLE PRECISION, [0.90,1.10] |
| `reid_throttle_key` | `reid_throttle_key` | TEXT |
| `association_score` | `association_score` | DOUBLE PRECISION |
| `association_method` | `association_method` | TEXT |
| `snapshot_path` | `snapshot_path` | TEXT, nullable |
| `crop_path` | `crop_path` | TEXT, nullable |
| `nvr_reference` | `nvr_reference` | JSONB, nullable |
| `payload` | `payload` | JSONB, 不含 embedding |

Redis-only 字段 (不入 PostgreSQL): `schema_version`, `producer`,
`message_type`, `reid_allowed`。

### 8.3 F3.3 retention 目标

最终策略不是“整行和向量一起删”，而是分两层：

- `face_observations` 元数据行可保留更久
- `embedding` / `embedding_norm` 可先按 TTL 置空

因此：

- 写路径仍要求 observation 初次入库时 embedding 完整
- retention 路径允许后续把 observation embedding 置空
- 查询路径必须使用 `WHERE embedding IS NOT NULL`

## 9. match_results

```sql
CREATE TABLE match_results (
    id                          BIGSERIAL PRIMARY KEY,
    search_request_id           UUID NOT NULL,
    search_mode                 TEXT NOT NULL,

    query_person_id             BIGINT REFERENCES persons(id) ON DELETE SET NULL,
    query_gallery_embedding_id  BIGINT REFERENCES person_gallery_embeddings(id) ON DELETE SET NULL,
    query_embedding_model       TEXT NOT NULL DEFAULT 'adaface',
    similarity_threshold        DOUBLE PRECISION,
    time_from                   TIMESTAMPTZ,
    time_to                     TIMESTAMPTZ,
    camera_scope                JSONB,

    matched_observation_id      UUID NOT NULL REFERENCES face_observations(id) ON DELETE CASCADE,
    matched_source_observation_id TEXT REFERENCES face_observations(source_observation_id) ON DELETE SET NULL,
    matched_camera_id           TEXT NOT NULL,
    matched_source_id           TEXT NOT NULL,
    matched_track_id            TEXT NOT NULL,
    matched_captured_at         TIMESTAMPTZ,
    matched_timestamp_ms        BIGINT,

    rank                        INTEGER NOT NULL,
    similarity                  DOUBLE PRECISION NOT NULL,
    face_confidence             DOUBLE PRECISION,
    quality                     DOUBLE PRECISION,
    snapshot_path               TEXT,
    crop_path                   TEXT,
    nvr_reference               JSONB,

    expires_at                  TIMESTAMPTZ NOT NULL,
    payload                     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE(search_request_id, matched_observation_id)
);

CREATE INDEX match_results_request_idx
ON match_results(search_request_id, rank);

CREATE INDEX match_results_query_person_idx
ON match_results(query_person_id, created_at DESC);

CREATE INDEX match_results_observation_idx
ON match_results(matched_observation_id);

CREATE INDEX match_results_expires_idx
ON match_results(expires_at);
```

字段约定：

- `search_mode` 只允许：

```text
registered_person_history
temporary_face_history
```

- `match_results` 是短期派生结果，不是事实源表
- 不存 query embedding
- 不存临时上传原图
- `temporary_face_history` 模式下，`query_person_id` /
  `query_gallery_embedding_id` 允许为 null
- 通过 `expires_at` 驱动 TTL 清理

### 9.1 `nvr_reference`

`face_observations.nvr_reference` 与 `match_results.nvr_reference` 建议共用同一结构：

```json
{
  "recording_strategy": "external_nvr_replay",
  "provider": "hikvision",
  "site_id": "campus_a",
  "device_id": "nvr_01",
  "channel_id": "ch_12",
  "stream_id": "main",
  "captured_at": "2026-05-27T10:15:23Z",
  "pre_seconds": 10,
  "post_seconds": 10,
  "playback_uri": null,
  "recording_id": null,
  "opaque_ref": null,
  "vendor_payload": {}
}
```

说明：

- `captured_at` 是 wall-clock anchor
- `opaque_ref` 支持厂商私有 token / file id
- `vendor_payload` 允许扩展而不污染公共字段

## 10. events

```sql
CREATE TABLE events (
    id BIGSERIAL PRIMARY KEY,
    source_event_id TEXT UNIQUE,

    event_type TEXT NOT NULL,
    camera_id TEXT REFERENCES cameras(id),
    source_id TEXT,
    track_id TEXT,
    person_id BIGINT REFERENCES persons(id),

    severity TEXT,
    confidence REAL,

    start_ts TIMESTAMPTZ,
    end_ts TIMESTAMPTZ,

    snapshot_path TEXT,
    clip_path TEXT,

    status TEXT DEFAULT 'new',
    payload JSONB DEFAULT '{}'::jsonb,

    -- Replay / media generation helper fields. MVP can keep them null.
    event_ts_ms BIGINT,
    frame_uuid TEXT,
    keyframe_uuid TEXT,
    recording_strategy TEXT DEFAULT 'reserved',
    media_status TEXT DEFAULT 'not_implemented',

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX events_type_time_idx ON events(event_type, created_at DESC);
CREATE INDEX events_camera_time_idx ON events(camera_id, created_at DESC);
CREATE INDEX events_person_time_idx ON events(person_id, created_at DESC);
CREATE INDEX events_status_idx ON events(status);
```

`status` 可选：

```text
new
acknowledged
confirmed
false_positive
resolved
```

## 11. tracks

```sql
CREATE TABLE tracks (
    id BIGSERIAL PRIMARY KEY,
    camera_id TEXT REFERENCES cameras(id),
    source_id TEXT,
    track_id TEXT NOT NULL,

    first_seen_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,

    best_face_observation_id BIGINT,
    matched_person_id BIGINT REFERENCES persons(id),

    payload JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE(camera_id, track_id, first_seen_at)
);

CREATE INDEX tracks_camera_track_idx ON tracks(camera_id, track_id);
CREATE INDEX tracks_person_idx ON tracks(matched_person_id);
```

## 12. watchlist_rules

```sql
CREATE TABLE watchlist_rules (
    id BIGSERIAL PRIMARY KEY,
    person_id BIGINT REFERENCES persons(id) ON DELETE CASCADE,
    enabled BOOLEAN DEFAULT true,
    threshold REAL DEFAULT 0.75,
    cooldown_seconds INTEGER DEFAULT 60,
    camera_scope JSONB,
    created_by TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX watchlist_rules_person_idx ON watchlist_rules(person_id, enabled);
```

## 13. live_search_jobs

```sql
CREATE TABLE live_search_jobs (
    id BIGSERIAL PRIMARY KEY,
    person_id BIGINT REFERENCES persons(id),
    status TEXT DEFAULT 'active',
    threshold REAL DEFAULT 0.75,
    camera_scope JSONB,
    created_by TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ,
    last_hit_at TIMESTAMPTZ,
    payload JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX live_search_jobs_active_idx
ON live_search_jobs(status, expires_at);
```

## 14. audit_logs

```sql
CREATE TABLE audit_logs (
    id BIGSERIAL PRIMARY KEY,
    actor TEXT,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    payload JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX audit_logs_entity_idx ON audit_logs(entity_type, entity_id);
CREATE INDEX audit_logs_time_idx ON audit_logs(created_at DESC);
```

## 15. snapshots 和 clips

第一版可以只在 events / face_observations 中保存路径。

后期如需独立管理：

```sql
CREATE TABLE media_assets (
    id BIGSERIAL PRIMARY KEY,
    asset_type TEXT NOT NULL,
    path TEXT NOT NULL,
    camera_id TEXT REFERENCES cameras(id),
    event_id BIGINT REFERENCES events(id),
    captured_at TIMESTAMPTZ,
    payload JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);
```

## 16. pgvector 查询示例

### 16.1 查询重点人员库

```sql
SELECT
    p.id AS person_id,
    p.name,
    g.id AS embedding_id,
    1 - (g.embedding <=> :query_embedding) AS similarity
FROM person_gallery_embeddings g
JOIN persons p ON p.id = g.person_id
WHERE g.is_active = true
  AND p.is_active = true
ORDER BY g.embedding <=> :query_embedding
LIMIT 5;
```

### 16.2 查询历史出现记录

```sql
SELECT
    id,
    camera_id,
    track_id,
    captured_at,
    snapshot_path,
    1 - (embedding <=> :query_embedding) AS similarity
FROM face_observations
WHERE embedding IS NOT NULL
  AND captured_at BETWEEN :start_time AND :end_time
  AND quality >= :min_quality
ORDER BY embedding <=> :query_embedding
LIMIT 50;
```

## 17. 数据保留策略

F3.3 最终建议：

```text
events: 按项目要求保留，默认 180 天
person_gallery_embeddings: 长期保存，人工管理
face_observations embedding: 默认保留 30 天，之后允许置空
face_observations metadata row: 默认保留到 180 天，视合规要求
match_results(registered_person_history): 默认 7 天
match_results(temporary_face_history): 默认 24 小时
temporary uploaded query image / query embedding: 请求结束即删，或 TTL <= 24h
snapshots/clips: 默认 30 到 90 天
watchlist/persons: 人工管理，不自动删除
audit_logs: 至少 1 年
```

规则：

- observation 命中搜索后，不立即删除其 embedding
- 命中与否不影响 observation embedding TTL
- 只允许 retention job 统一做置空/删除
- 临时上传查询对象不得进入长期表

---

# 18. MVP 媒体状态与幂等补充

MVP 阶段保留 `events.snapshot_path` 和 `events.clip_path` 字段，但允许为空。

建议所有事件在 `payload.media` 中包含：

```json
{
  "media": {
    "snapshot_required": true,
    "clip_required": true,
    "snapshot_status": "not_implemented",
    "clip_status": "not_implemented",
    "recording_strategy": "reserved",
    "pre_seconds": 5,
    "post_seconds": 5
  }
}
```

后续可选状态：

```text
snapshot_status: not_required / not_implemented / pending / ready / failed
clip_status: not_required / not_implemented / pending / ready / failed
recording_strategy: none / reserved / savant_replay / external_nvr_replay / post_event_rtsp_demo / custom_in_pipeline_ring_buffer
```

`source_observation_id TEXT NOT NULL UNIQUE` 已在 F3.1 schema 中实现 (见 §8)。
face-worker 使用 `ON CONFLICT (source_observation_id) DO NOTHING` 实现幂等，
重试时不会重复插入同一条人脸 observation。无需额外 ALTER TABLE。


## 19. Replay 录像请求表，后续阶段

MVP 阶段可以只使用 Redis Stream `security.record_requests`，不必建表。后续如果需要可追踪、可重试、可审计的录像任务，建议增加：

```sql
CREATE TABLE record_requests (
    id BIGSERIAL PRIMARY KEY,
    request_id TEXT UNIQUE NOT NULL,
    event_id BIGINT REFERENCES events(id) ON DELETE CASCADE,

    camera_id TEXT REFERENCES cameras(id),
    source_id TEXT NOT NULL,
    event_ts_ms BIGINT NOT NULL,
    frame_uuid TEXT,
    keyframe_uuid TEXT,

    pre_seconds REAL DEFAULT 5,
    post_seconds REAL DEFAULT 5,
    strategy TEXT DEFAULT 'savant_replay',
    status TEXT DEFAULT 'pending',

    replay_job_id TEXT,
    sink_output_path TEXT,
    clip_path TEXT,
    snapshot_path TEXT,
    error_message TEXT,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX record_requests_event_idx ON record_requests(event_id);
CREATE INDEX record_requests_status_idx ON record_requests(status, created_at);
```

`status` 建议：

```text
pending
replay_job_created
sink_writing
ready
failed
cancelled
```

## 19. Replay 媒体状态建议

`events.payload.media` 建议统一为：

```json
{
  "media": {
    "snapshot_required": true,
    "clip_required": true,
    "snapshot_status": "not_implemented",
    "clip_status": "not_implemented",
    "recording_strategy": "reserved",
    "pre_seconds": 5,
    "post_seconds": 5,
    "source_id": "site_a.gate.cam_001",
    "event_ts_ms": 1710000000000,
    "frame_uuid": null,
    "keyframe_uuid": null,
    "replay_job_id": null,
    "sink_output_path": null
  }
}
```

正式启用 Replay 后：

```text
recording_strategy = savant_replay
clip_status: pending / replay_job_created / sink_writing / ready / failed
snapshot_status: pending / ready / failed / not_required
```
