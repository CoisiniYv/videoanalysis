# Phase F3.3 — Gallery / Retention / Match Policy Design

Date: 2026-05-27
Status: design complete
Scope: schema and policy only

## 1. In Scope

- 设计 `persons` / `person_gallery_embeddings` 目标表结构
- 设计 `match_results` 目标表结构
- 设计 `face_observations.embedding` retention 策略
- 设计 `nvr_reference` 字段
- 明确一键寻人的两种模式

## 2. Out of Scope

- 不实现 `watchlist_hit`
- 不实现 `live_search_hit`
- 不写 `security.events`
- 不改 Savant pipeline
- 不建真实 API
- 不写本 phase migration

## 3. Final Decisions

1. `persons` 只表示“已登记人员”，临时上传查历史不会创建 `person`。
2. `person_gallery_embeddings` 只存长期登记向量，不存临时查询向量。
3. 两种一键寻人都统一检索 `face_observations`，结果统一落 `match_results`。
4. `match_results` 是短期派生表，不存 query embedding，不存上传原图。
5. `face_observations` 行可比 embedding 保留更久；embedding 到期后允许置空。
6. observation 是否命中过搜索，不影响其 embedding retention；不做“匹配后立即删 observation embedding”。
7. `nvr_reference` 采用 `JSONB`，保存 vendor-neutral 播放定位信息，并允许 vendor 扩展。

## 4. Two Search Modes

### 4.1 已登记人员轨迹查询

流程：

```text
选择已登记 person
  -> 读取该 person 的 active gallery embeddings
  -> 在 face_observations 上做历史相似检索
  -> 结果写入 match_results
  -> 返回轨迹/时间/摄像头/NVR 引用
```

特点：

- query 来源是 `persons` + `person_gallery_embeddings`
- 不产生新长期向量
- 可重复执行，结果可重算

### 4.2 临时上传人脸查历史

流程：

```text
临时上传一张人脸
  -> 提取临时 query embedding
  -> 在 face_observations 上做历史相似检索
  -> 结果写入 match_results
  -> 上传图和 query embedding 立即删除或只留极短 TTL
```

特点：

- 不创建 `person`
- 不写入 `person_gallery_embeddings`
- 不长期保存上传图
- 不长期保存 query embedding

## 5. `persons` Design

目标：只存“被人工登记并允许长期管理”的人员主数据。

```sql
CREATE TABLE persons (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    external_id TEXT UNIQUE,
    description TEXT,
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_by  TEXT,
    updated_by  TEXT,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX persons_name_trgm_idx ON persons USING gin (name gin_trgm_ops);
CREATE INDEX persons_external_id_idx ON persons(external_id);
CREATE INDEX persons_active_idx ON persons(is_active);
```

约束：

- 仅人工登记对象进入该表
- 禁止把临时上传查询对象写成 `person`
- 删除/停用 `person` 时，其 gallery embeddings 一并清理或停用

## 6. `person_gallery_embeddings` Design

目标：存放“长期登记、人工管理、可反复检索”的 gallery 向量。

```sql
CREATE TABLE person_gallery_embeddings (
    id                  BIGSERIAL PRIMARY KEY,
    person_id           BIGINT NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    source_type         TEXT NOT NULL DEFAULT 'manual_upload',
    source_image_path   TEXT,
    embedding_model     TEXT NOT NULL DEFAULT 'adaface',
    model_version       TEXT,
    embedding_dim       INTEGER NOT NULL DEFAULT 512 CHECK (embedding_dim = 512),
    embedding           vector(512) NOT NULL,
    embedding_norm      DOUBLE PRECISION NOT NULL,
    quality             DOUBLE PRECISION,
    face_bbox           JSONB,
    landmarks           JSONB,
    is_primary          BOOLEAN NOT NULL DEFAULT false,
    is_active           BOOLEAN NOT NULL DEFAULT true,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX person_gallery_person_active_idx
ON person_gallery_embeddings(person_id, is_active);

CREATE UNIQUE INDEX person_gallery_one_primary_idx
ON person_gallery_embeddings(person_id)
WHERE is_primary = true AND is_active = true;
```

约束：

- gallery embedding 是长期向量
- `embedding_model` 必须与历史 observation 检索所用向量空间兼容
- 一个 person 可有多条 active gallery embeddings
- 临时上传 query embedding 严禁写入此表
- 本 phase 不强制设计 ANN/HNSW 索引，先锁定数据模型和生命周期

`source_type` 建议值：

```text
manual_upload
snapshot_extract
imported_archive
```

## 7. `match_results` Design

目标：记录“一次历史搜索”的候选结果。它是**短期派生数据**，不是事实源表。

```sql
CREATE TABLE match_results (
    id                      BIGSERIAL PRIMARY KEY,
    search_request_id       UUID NOT NULL,
    search_mode             TEXT NOT NULL,

    query_person_id         BIGINT REFERENCES persons(id) ON DELETE SET NULL,
    query_gallery_embedding_id BIGINT REFERENCES person_gallery_embeddings(id) ON DELETE SET NULL,
    query_embedding_model   TEXT NOT NULL DEFAULT 'adaface',
    similarity_threshold    DOUBLE PRECISION,
    time_from               TIMESTAMPTZ,
    time_to                 TIMESTAMPTZ,
    camera_scope            JSONB,

    matched_observation_id  UUID NOT NULL,
    matched_camera_id       TEXT NOT NULL,
    matched_source_id       TEXT NOT NULL,
    matched_track_id        TEXT NOT NULL,
    matched_captured_at     TIMESTAMPTZ,
    matched_timestamp_ms    BIGINT,

    rank                    INTEGER NOT NULL,
    similarity              DOUBLE PRECISION NOT NULL,
    face_confidence         DOUBLE PRECISION,
    quality                 DOUBLE PRECISION,
    snapshot_path           TEXT,
    crop_path               TEXT,
    nvr_reference           JSONB,

    expires_at              TIMESTAMPTZ NOT NULL,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

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

`search_mode` 只允许两类：

```text
registered_person_history
temporary_face_history
```

设计原则：

- `search_request_id` 把一次搜索的所有候选行归为一组
- `matched_observation_id` 指向事实源 observation
- 不存 query embedding
- 不存临时上传原图路径
- `nvr_reference` 冗余一份播放定位信息，避免结果展示时必须二次 join
- `expires_at` 驱动结果表 TTL 清理

默认 TTL 建议：

- `registered_person_history`: 7 天
- `temporary_face_history`: 24 小时

如果未来需要异步搜索任务，可再增加 `search_requests` 表；本 phase 不需要。

## 8. `nvr_reference` Field Design

`nvr_reference` 建议同时出现在：

- `face_observations`
- `match_results`

字段类型统一为 `JSONB`。

推荐结构：

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

- `recording_strategy` 复用现有枚举口径，如 `reserved` / `external_nvr_replay`
- `captured_at` 是 wall-clock anchor；没有它就很难稳定回放
- `opaque_ref` 留给厂商私有 token / file id / playback handle
- `vendor_payload` 允许厂商扩展，不污染顶层公共字段

本 phase 只设计字段，不要求上游立即填充。

## 9. Vector Retention Policy

### 9.1 长期保存

- `person_gallery_embeddings.embedding`
- `person_gallery_embeddings.embedding_norm`
- 登记人员原始参考图路径（如保留）

策略：

- 由人工生命周期管理
- 删除 person / 删除 gallery 时才删除
- 命中搜索不影响其保留时长

### 9.2 短期保存

- `face_observations.embedding`
- `face_observations.embedding_norm`

建议默认策略：

```text
0-30 天: 可检索，保留完整 observation embedding
30-180 天: 保留 observation 元数据，但 embedding / embedding_norm 置空
180 天后: 整行 observation 可按项目合规要求删除
```

说明：

- 30 天是默认值，不是硬编码法律值
- 项目可按合规要求收紧到 7 天 / 14 天，或放宽到 90 天
- 关键点是“row retention”与“vector retention”分离

### 9.3 匹配后不立即删除

最终决定：

- observation embedding **不会**因为“这次搜索命中了”就立刻删除
- 是否命中不改变 observation embedding 的保留时钟
- 删除/置空只由统一 retention job 决定

理由：

- 同一 observation 可能被后续其他 person / 其他临时查询再次命中
- “命中过就删”会导致检索行为依赖历史访问，结果不可重复
- 行为审计和结果复盘会变得不可解释

### 9.4 匹配后立即删除/置空的对象

仅以下对象应在匹配完成后立即删除或极短 TTL：

- 临时上传原图
- 临时上传得到的 query embedding
- 临时上传过程中的中间 crop / debug 文件

策略建议：

```text
同步查询: 只在内存中保留，请求结束即销毁
异步查询: 放临时对象存储或 Redis，TTL <= 24h
数据库: 不落长期表
```

## 10. Implications For Existing F3.1 Schema

当前 `db/migrations/005_phase_f3_1_face_observations.sql` 中：

- `embedding vector(512) NOT NULL`
- `embedding_norm DOUBLE PRECISION NOT NULL`

而 F3.3 retention 目标要求：

- 允许 observation 行保留，但 embedding 被置空

因此后续真正落地 retention 前，必须先做 migration：

1. 放宽 `face_observations.embedding` 为 nullable
2. 放宽 `face_observations.embedding_norm` 为 nullable
3. 保持查询侧 `WHERE embedding IS NOT NULL`

这次只做设计，不执行 migration。

## 11. Recommended Next Implementation Order

1. 先补 schema migration：`persons` / `person_gallery_embeddings` / `match_results`
2. 再补 `face_observations` nullable embedding migration
3. 再补历史查询 read path
4. 最后补 retention worker / cron job
