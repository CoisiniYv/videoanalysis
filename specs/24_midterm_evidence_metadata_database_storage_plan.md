# 24_midterm_evidence_metadata_database_storage_plan.md

Date: 2026-06-26

## 1. 目标

优化 8090 证据页面的列表和详情加载速度，减少 evidence bundle 目录中的小文件数量。

目标不是把所有证据都塞进 PostgreSQL，而是建立清晰分层：

- `raw_clip.mov` 继续放文件系统或后续对象存储。
- 8090 列表、筛选、分页所需字段全部来自 PostgreSQL。
- 当前小文件中的结构化内容逐步进入 PostgreSQL，避免 8090 为展示证据反复遍历目录和读取 JSON/JSONL。
- 文件系统只保留播放所需视频和必要的可恢复产物。

## 2. 当前问题

当前 lab 证据目录已清理到只剩当前主线格式，但每个 bundle 仍包含多份小文件：

```text
raw_clip.mov
annotations.frame_cache.identity.jsonl
sink_metadata.json
metadata.json
summary.json
summary.frame_cache.identity.json
video_crop_ffmpeg.log
```

这些文件不是旧标注链路残留。旧的 `annotations.jsonl` / `event_annotation.json` 逻辑已经从当前主线移除。

当前问题是：

1. 8090 证据列表如果依赖文件扫描，会随 bundle 数量增长变慢。
2. 每个 bundle 多个小文件会增加目录遍历、inode、备份和迁移成本。
3. `summary.json` 和 `summary.frame_cache.identity.json` 内容有重复。
4. `video_crop_ffmpeg.log` 对成功证据没有播放价值。
5. `annotations.frame_cache.identity.jsonl` 和 `sink_metadata.json` 当前对动态叠框仍有价值，不能直接删除。

## 3. 设计原则

1. 不把 `raw_clip.mov` 存进 PostgreSQL。
2. PostgreSQL 存索引、状态、小摘要、叠框元数据和帧时间轴。
3. 视频播放继续通过文件路径或对象存储 URI 提供 Range 读取。
4. 8090 列表严禁扫 evidence 目录。
5. 8090 详情优先读数据库，文件只作为兼容回退。
6. 写入过程必须幂等，以 `event_id` 和 `artifact_type` 为唯一键。
7. 保留一段迁移期：DB 新路径和旧文件路径并存，验证稳定后再减少落盘小文件。
8. 失败证据要保留足够诊断信息，成功证据默认不保留大段调试日志。

## 4. 不做什么

- 不把 `raw_clip.mov` 以 bytea 或 large object 存入 PostgreSQL。
- 不为了减少文件数量牺牲 8090 动态叠框准确性。
- 不在第一阶段删除 `sink_metadata.json` 和 `annotations.frame_cache.identity.jsonl`。
- 不继续恢复旧 `annotations.jsonl` / `event_annotation.json` 链路。
- 不让 8090 列表回退到全目录遍历作为默认路径。

## 5. 推荐目标架构

```text
events
  -> evidence_tasks
  -> evidence_bundles
  -> evidence_artifacts
  -> evidence_frame_timeline
  -> evidence_overlay_segments

文件系统：
  /data/video-analytics/media/evidence/{event_id}/raw_clip.mov

可选兼容期文件：
  annotations.frame_cache.identity.jsonl
  sink_metadata.json
  metadata.json
  summary.json
```

## 6. 数据库 Schema 草案

### 6.1 evidence_bundles

每个事件一行，承载 8090 列表和详情需要的主摘要。

```sql
CREATE TABLE evidence_bundles (
    event_id uuid PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    source_event_id text,
    camera_id text,
    source_id text,
    camera_name text,
    event_type text,
    event_created_at timestamptz,
    alarm_machine_time timestamptz,
    media_status text NOT NULL DEFAULT 'not_implemented',
    evidence_state text,
    evidence_reason text,
    raw_clip_uri text,
    raw_clip_size_bytes bigint,
    raw_clip_duration_seconds double precision,
    raw_clip_sha256 text,
    raw_clip_content_type text DEFAULT 'video/quicktime',
    annotation_status text,
    annotation_count integer,
    matched_objects integer,
    unknown_objects integer,
    visual_evidence_status text,
    frontend_overlay_required boolean DEFAULT true,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    materialization jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX evidence_bundles_source_created_idx
    ON evidence_bundles(source_id, event_created_at DESC);

CREATE INDEX evidence_bundles_camera_created_idx
    ON evidence_bundles(camera_id, event_created_at DESC);

CREATE INDEX evidence_bundles_type_created_idx
    ON evidence_bundles(event_type, event_created_at DESC);

CREATE INDEX evidence_bundles_media_status_idx
    ON evidence_bundles(media_status);
```

### 6.2 evidence_artifacts

记录所有证据产物的位置、大小、hash 和生命周期。`raw_clip` 只存 URI，不存二进制。

```sql
CREATE TABLE evidence_artifacts (
    id bigserial PRIMARY KEY,
    event_id uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    artifact_type text NOT NULL,
    uri text,
    storage_backend text NOT NULL DEFAULT 'filesystem',
    content_type text,
    compression text,
    size_bytes bigint,
    sha256 text,
    status text NOT NULL DEFAULT 'ready',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, artifact_type)
);

CREATE INDEX evidence_artifacts_type_idx
    ON evidence_artifacts(artifact_type);
```

推荐 `artifact_type`：

| artifact_type | 内容 | 是否建议长期文件落盘 |
| --- | --- | --- |
| `raw_clip` | `raw_clip.mov` URI | 是 |
| `overlay_annotations` | 原 `annotations.frame_cache.identity.jsonl` 内容 | 否，迁移后入库 |
| `sink_timeline` | 原 `sink_metadata.json` 帧时间轴 | 否，迁移后入库 |
| `bundle_summary` | 原 `summary.json` / sidecar summary 合并摘要 | 否 |
| `ffmpeg_log` | 失败时的 ffmpeg 日志 | 仅失败时保留 |

### 6.3 evidence_frame_timeline

把 `sink_metadata.json` 中 8090 叠框需要的字段结构化，避免详情页读取大 JSON 文件。

```sql
CREATE TABLE evidence_frame_timeline (
    event_id uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    clip_frame_index integer NOT NULL,
    frame_uuid text,
    frame_pts bigint,
    frame_dts bigint,
    duration_ns bigint,
    timestamp_ms bigint,
    width integer,
    height integer,
    source_id text,
    camera_id text,
    stream_session_id text,
    keyframe_uuid text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (event_id, clip_frame_index)
);

CREATE INDEX evidence_frame_timeline_uuid_idx
    ON evidence_frame_timeline(event_id, frame_uuid);

CREATE INDEX evidence_frame_timeline_pts_idx
    ON evidence_frame_timeline(event_id, frame_pts);
```

### 6.4 evidence_overlay_segments

把 `annotations.frame_cache.identity.jsonl` 中的可展示记录拆成按帧查询的 overlay。

```sql
CREATE TABLE evidence_overlay_segments (
    event_id uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    clip_frame_index integer NOT NULL,
    frame_uuid text,
    frame_pts bigint,
    t_ms integer,
    object_count integer NOT NULL DEFAULT 0,
    objects jsonb NOT NULL DEFAULT '[]'::jsonb,
    record jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, clip_frame_index)
);

CREATE INDEX evidence_overlay_segments_uuid_idx
    ON evidence_overlay_segments(event_id, frame_uuid);
```

## 7. 写入路径改造

### 7.1 media-worker 完成 materialization 后

当前 media-worker 写：

- `raw_clip.mov`
- `sink_metadata.json`
- `metadata.json`
- `summary.json`
- `annotations.frame_cache.identity.jsonl`
- `summary.frame_cache.identity.json`
- `video_crop_ffmpeg.log`

目标改为：

1. `raw_clip.mov` 继续写文件系统。
2. 计算 `raw_clip` 的 `size_bytes`、`sha256`、`duration_seconds`。
3. upsert `evidence_bundles`。
4. upsert `evidence_artifacts(raw_clip)`。
5. 解析并批量 upsert `evidence_frame_timeline`。
6. 解析并批量 upsert `evidence_overlay_segments`。
7. 合并 `summary.json` 和 sidecar summary，写入 `evidence_bundles.summary`。
8. 成功证据默认不保留 `video_crop_ffmpeg.log`；失败证据保留并登记为 `evidence_artifacts(ffmpeg_log)`。

### 7.2 幂等要求

- `evidence_bundles.event_id` 唯一。
- `evidence_artifacts(event_id, artifact_type)` 唯一。
- `evidence_frame_timeline(event_id, clip_frame_index)` 唯一。
- `evidence_overlay_segments(event_id, clip_frame_index)` 唯一。
- 重跑 media-worker 不应重复写记录。
- 如果旧文件存在，DB 写入成功后才允许删除小文件。

## 8. 8090/API 改造

### 8.1 列表接口

`/api/v1/evidence/bundles` 必须只查 PostgreSQL：

- 默认从 `evidence_bundles` 读。
- 兼容期如果 `evidence_bundles` 缺行，可退回当前 `events/evidence_tasks` 查询。
- 禁止列表路径读取 bundle 目录。

验收：

- 1000 个 bundle 下列表首屏 p95 小于 300ms。
- 10000 个 bundle 下分页查询 p95 小于 500ms。
- `source_id=lab`、`event_type`、时间范围过滤均走索引。

### 8.2 详情接口

`/api/bundles/{event_id}`：

- 优先从 `evidence_bundles` 和 `evidence_artifacts` 读。
- `raw_clip_url` 从 `raw_clip_uri` 派生。
- 不依赖 `metadata.json` / `summary.json`。

### 8.3 标注接口

`/api/bundles/{event_id}/annotations`：

- 优先从 `evidence_overlay_segments` 组装返回。
- 保持现有响应字段兼容：`records`、`annotations`、`annotation_source`、`production_ready`。
- 兼容期可回退读取 `annotations.frame_cache.identity.jsonl`。

### 8.4 sink metadata 接口

`/api/bundles/{event_id}/sink-metadata`：

- 优先从 `evidence_frame_timeline` 返回。
- 保持前端需要的字段：`pts/frame_pts`、`frame_uuid`、`clip_frame_index`、`width/height`。
- 兼容期可回退读取 `sink_metadata.json`。

## 9. 文件落盘策略

### 9.1 第一阶段保留

第一阶段仍保留：

- `raw_clip.mov`
- `annotations.frame_cache.identity.jsonl`
- `sink_metadata.json`
- `metadata.json`
- `summary.json`
- `summary.frame_cache.identity.json`

只删除成功证据的：

- `video_crop_ffmpeg.log`

### 9.2 第二阶段减少小文件

DB 读路径验证稳定后：

- 停止写 `summary.frame_cache.identity.json`，合并到 DB summary。
- 停止写 `metadata.json` 或只保留一个最小 manifest。
- `annotations.frame_cache.identity.jsonl` 改为可选 debug 输出。
- `sink_metadata.json` 改为可选 debug 输出。

### 9.3 第三阶段最终形态

每个成功证据目录只保留：

```text
raw_clip.mov
```

可选保留一个很小的恢复 manifest：

```text
manifest.json
```

其中只含：

- `event_id`
- `raw_clip_sha256`
- `raw_clip_size_bytes`
- `created_at`
- `db_materialization_version`

## 10. 迁移计划

### R24.1 — Schema 添加

目标：

- 添加 `evidence_bundles`、`evidence_artifacts`、`evidence_frame_timeline`、`evidence_overlay_segments`。

验收：

- migration 可重复运行。
- 现有测试不受影响。
- 表和索引存在。

### R24.2 — Backfill 当前 evidence

目标：

- 从当前 lab evidence 文件回填 DB。
- 不删除任何小文件。

验收：

- 当前 lab 的 214 个 bundle 均写入 `evidence_bundles`。
- `raw_clip` artifact 数与现有 `raw_clip.mov` 数一致。
- `overlay_segments` 数与当前 annotation 记录数一致。
- `frame_timeline` 数与当前 sink metadata 记录数一致。

### R24.3 — media-worker 双写

目标：

- 新生成证据继续写文件，同时双写 DB。

验收：

- 新 bundle 生成后 DB 行立即可查。
- 重跑同一事件不会重复插入。
- DB 写失败时文件路径仍可回退，但事件标记 `db_index_status=failed`。

### R24.4 — 8090 列表切 DB 主路径

目标：

- 8090 evidence 列表、筛选、分页只查数据库。

验收：

- 1000/10000 bundle 压测达标。
- 不访问 evidence 目录也能返回列表。
- `source_id=lab` 查询稳定。

### R24.5 — 8090 详情和叠框切 DB 主路径

目标：

- annotations 和 sink metadata 接口优先读 DB。

验收：

- 8090 播放和叠框效果与旧文件路径一致。
- 删除 `sink_metadata.json` 的测试副本后仍可叠框。
- 删除 `annotations.frame_cache.identity.jsonl` 的测试副本后仍可叠框。

### R24.6 — 成功证据停止写调试日志

目标：

- 成功证据不保留 `video_crop_ffmpeg.log`。
- 失败证据保留日志并登记 artifact。

验收：

- 成功 bundle 中无 `video_crop_ffmpeg.log`。
- 失败 bundle 可从 API 查询 ffmpeg log。

### R24.7 — 小文件落盘收敛

目标：

- DB 主路径稳定后，停止写重复 summary 和大 JSON/JSONL sidecar。

验收：

- 成功 bundle 目录只剩 `raw_clip.mov` 和可选 `manifest.json`。
- 8090 列表、详情、叠框不依赖小文件。
- 回滚开关可临时恢复 sidecar 文件输出。

## 11. 回滚策略

每一阶段必须有回滚开关：

| 开关 | 作用 |
| --- | --- |
| `EVIDENCE_DB_INDEX_WRITE_ENABLED` | 控制 media-worker 是否写 DB 索引 |
| `EVIDENCE_DB_INDEX_READ_ENABLED` | 控制 8090 是否优先读 DB 索引 |
| `EVIDENCE_DB_OVERLAY_READ_ENABLED` | 控制 annotations/sink-metadata 是否优先读 DB |
| `EVIDENCE_SIDECAR_FILE_WRITE_ENABLED` | 控制是否继续写 JSON/JSONL sidecar |
| `EVIDENCE_KEEP_SUCCESS_FFMPEG_LOG` | 控制成功证据是否保留 ffmpeg 日志 |

回滚顺序：

1. 先把 8090 读路径切回文件兼容路径。
2. 再保留 media-worker 双写。
3. 确认 DB 写入问题后修复。
4. 不因 DB 索引失败删除 `raw_clip.mov`。

## 12. 风险

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| DB 表过大 | 查询变慢、备份变慢 | overlay/timeline 分表或按月分区 |
| JSONB 过大 | 单行膨胀 | overlay 按帧拆行，不把所有记录塞一个 JSONB |
| DB 写入失败 | 新证据列表缺失 | 双写期保留文件回退和 `db_index_status` |
| 叠框不准 | 证据可信度下降 | R24.5 做逐帧对比测试 |
| 删除小文件过早 | 8090 回退失效 | 先 DB 读路径压测通过，再关文件输出 |
| raw_clip 入库诱惑 | DB/WAL 膨胀 | 明确禁止 `raw_clip` 二进制入库 |

## 13. 测试计划

必须新增或更新测试：

- migration schema 测试。
- backfill 幂等测试。
- media-worker DB 双写测试。
- 8090 evidence 列表不读文件测试。
- annotations DB 组装兼容测试。
- sink metadata DB 组装兼容测试。
- 删除 sidecar 文件后 8090 仍可叠框测试。
- 1000/10000 bundle 查询性能脚本。

## 14. 验收标准

完成后必须满足：

1. 8090 evidence 列表不遍历 evidence 目录。
2. `raw_clip.mov` 不进入 PostgreSQL。
3. 成功证据目录可收敛到只保留 `raw_clip.mov` 和可选小 manifest。
4. 8090 播放、动态叠框、筛选、删除入口仍可用。
5. 新生成证据 DB 索引写入幂等。
6. 旧文件路径仍可在回滚开关下恢复读取。
7. lab 当前证据可完整 backfill。
8. 没有恢复旧 `annotations.jsonl` / `event_annotation.json` 链路。

## 15. 推荐开发顺序

1. 先做 schema 和 backfill，只读不改现有生成链路。
2. 再做 media-worker 双写。
3. 再把 8090 列表切到 `evidence_bundles`。
4. 再把 annotations/sink metadata 接口切 DB 主路径。
5. 最后停止写成功证据的调试日志和重复 sidecar 文件。

不要先删文件。先证明 DB 路径完整，再收敛文件输出。

## 16. R24.1-R24.4 实施记录

执行日期：2026-06-26

本次已完成 R24.1-R24.4：

- 新增 migration：`db/migrations/017_evidence_database_artifacts.sql`。
- 新增表：`evidence_bundles`、`evidence_artifacts`、`evidence_frame_timeline`、`evidence_overlay_segments`。
- 新增 backfill：`scripts/maintenance/backfill_evidence_db_index.py`。
- 新增 media-worker DB 索引写入模块：`services/media-worker/app/evidence_db_index.py`。
- media-worker 在证据 materialization 成功后双写 DB 索引，默认由 `EVIDENCE_DB_INDEX_WRITE_ENABLED=true` 开启。
- media-worker 双写成功时写入 `events.payload.media.db_index_status=ready`，失败时写入 `failed` 和错误原因。
- 8090 evidence 列表主路径已切换为查询 `evidence_bundles`，不再依赖扫描 evidence 目录。
- `raw_clip.mov` 继续只保留在文件系统；PostgreSQL 只保存 URI、大小、状态、摘要和结构化 metadata，不写视频二进制。
- 现有小文件未删除，兼容回退仍保留。

当前 lab backfill 结果：

| 项 | 数量 |
| --- | ---: |
| `evidence_bundles` | 214 |
| `evidence_artifacts(raw_clip)` | 213 |
| `evidence_artifacts(bundle_summary)` | 214 |
| `evidence_artifacts(overlay_annotations)` | 214 |
| `evidence_artifacts(sink_timeline)` | 214 |
| `evidence_frame_timeline` | 65386 |
| `evidence_overlay_segments` | 14180 |

8090/API 验证：

- `/api/v1/evidence/health` 返回 `index_source=database`。
- `/api/v1/evidence/bundles?source_id=lab&limit=5` 返回 `total=213`，约 0.13 秒。
- `/api/v1/evidence/bundles?source_id=primary&limit=3` 返回 `total=0`。
- `evidence_bundles` 有 214 条而列表为 213 条，是因为当前列表只展示有 `raw_clip_uri` 的可播放证据，其中 1 条历史 bundle 没有 `raw_clip.mov`。

本次未做：

- 未执行 R24.5，annotations 和 sink metadata 详情接口仍保留文件兼容路径。
- 未删除任何现有小文件。
- 未停止写 `video_crop_ffmpeg.log`。
- 未做 1000/10000 bundle 性能压测。
