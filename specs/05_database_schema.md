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

## 8. face_observations

```sql
CREATE TABLE face_observations (
    id BIGSERIAL PRIMARY KEY,
    camera_id TEXT REFERENCES cameras(id),
    source_id TEXT,
    track_id TEXT,
    captured_at TIMESTAMPTZ NOT NULL,

    face_bbox JSONB,
    person_bbox JSONB,
    landmarks JSONB,

    quality REAL,
    embedding vector(512),
    model_name TEXT,
    model_version TEXT,

    matched_person_id BIGINT REFERENCES persons(id),
    match_score REAL,

    snapshot_path TEXT,
    event_id BIGINT,
    payload JSONB DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX face_observations_embedding_hnsw_idx
ON face_observations
USING hnsw (embedding vector_cosine_ops);

CREATE INDEX face_observations_camera_time_idx
ON face_observations(camera_id, captured_at DESC);

CREATE INDEX face_observations_person_time_idx
ON face_observations(matched_person_id, captured_at DESC);
```

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

建议后续为 `face_observations` 增加幂等字段：

```sql
ALTER TABLE face_observations
ADD COLUMN source_observation_id TEXT;

CREATE UNIQUE INDEX face_observations_source_observation_uidx
ON face_observations(source_observation_id)
WHERE source_observation_id IS NOT NULL;
```

这样 face-worker 重试时不会重复插入同一条人脸 observation。


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
