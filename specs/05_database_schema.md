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
    external_id TEXT,
    description TEXT,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX persons_name_trgm_idx ON persons USING gin (name gin_trgm_ops);
CREATE INDEX persons_external_id_idx ON persons(external_id);
```

## 7. person_gallery_embeddings

```sql
CREATE TABLE person_gallery_embeddings (
    id BIGSERIAL PRIMARY KEY,
    person_id BIGINT REFERENCES persons(id) ON DELETE CASCADE,
    embedding vector(512) NOT NULL,
    image_path TEXT,
    quality REAL,
    model_name TEXT,
    model_version TEXT,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX person_gallery_embedding_hnsw_idx
ON person_gallery_embeddings
USING hnsw (embedding vector_cosine_ops);

CREATE INDEX person_gallery_person_idx
ON person_gallery_embeddings(person_id, is_active);
```

## 8. face_observations (F3.1 final)

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
    embedding               vector(512) NOT NULL,          -- L2 normalized
    embedding_norm          DOUBLE PRECISION NOT NULL,

    reid_throttle_key       TEXT NOT NULL DEFAULT '',
    association_score       DOUBLE PRECISION,
    association_method      TEXT,

    camera_config_resolved  BOOLEAN NOT NULL DEFAULT false,
    snapshot_path           TEXT,
    crop_path               TEXT,

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
- `embedding vector(512) NOT NULL` — F3.1 要求 embedding 必须存在。
  若缺失或不合法 (维度错、norm 超范围)，worker 拒绝插入且不 ACK，
  留在 pending 中暴露问题。
- `embedding_dim INTEGER NOT NULL DEFAULT 512 CHECK (embedding_dim = 512)` —
  与 vector(512) 配套，DB 层预防维度漂移。
- `embedding_norm DOUBLE PRECISION NOT NULL` — worker 验证范围 [0.90, 1.10]。
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
- `payload JSONB` 不包含 embedding 向量 (避免 ~8KB 重复存储)。
- 无 HNSW / IVFFlat / ANN 索引 — F3.1 是纯持久化，不检索。
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
| `payload` | `payload` | JSONB, 不含 embedding |

Redis-only 字段 (不入 PostgreSQL): `schema_version`, `producer`,
`message_type`, `reid_allowed`。

## 9. events

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

## 10. tracks

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

## 11. watchlist_rules

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

## 12. live_search_jobs

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

## 13. audit_logs

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

## 14. snapshots 和 clips

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

## 15. pgvector 查询示例

### 15.1 查询重点人员库

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

### 15.2 查询历史出现记录

```sql
SELECT
    id,
    camera_id,
    track_id,
    captured_at,
    snapshot_path,
    1 - (embedding <=> :query_embedding) AS similarity
FROM face_observations
WHERE captured_at BETWEEN :start_time AND :end_time
  AND quality >= :min_quality
ORDER BY embedding <=> :query_embedding
LIMIT 50;
```

## 16. 数据保留策略

建议：

```text
events: 按项目要求保留，默认 180 天
face_observations: 默认 30 到 180 天，视合规要求
snapshots/clips: 默认 30 到 90 天
watchlist/persons: 人工管理，不自动删除
audit_logs: 至少 1 年
```

---

# 17. MVP 媒体状态与幂等补充

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


## 18. Replay 录像请求表，后续阶段

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
