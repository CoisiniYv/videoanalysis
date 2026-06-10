# Phase F3.5 — Gallery Match Harness

## 目标

给定一个 `face_observation`（query），加载其 AdaFace embedding，在 `person_gallery_embeddings` 中搜索最相似的 gallery 条目，将 topK 结果写入 `match_results`。

## 范围

**实现：**
- `MatchResultRepository` — match_results 表的 CRUD
- `match_gallery.py` — CLI 工具，执行 gallery match 并写入结果
- 迁移 007 — 修复 match_results 语义以支持 gallery_match
- harness 测试（unit + integration）

**不实现：**
- `watchlist_hit` / `live_search_hit` 告警
- `security.events` 输出
- API endpoint
- retention deletion
- Savant pipeline / Redis producer 改动

## 语义

### gallery_match 模式

```text
query side:   face_observation (query_observation_id)
target side:  person_gallery_embeddings (query_gallery_embedding_id)
result:       match_results (topK ranked by similarity)
```

字段映射：

| match_results 字段 | gallery_match 含义 |
|---|---|
| search_request_id | 一次搜索的 UUID |
| search_mode | "gallery_match" |
| query_observation_id | 输入 face_observation 的 UUID |
| query_source_observation_id | 输入 face_observation 的 source_observation_id |
| query_person_id | 匹配到的 gallery 所属 person_id |
| query_gallery_embedding_id | 匹配到的 gallery embedding id |
| matched_observation_id | **NULL**（gallery match 无历史观测目标） |
| matched_camera_id / source_id / track_id | **NULL** |
| rank | 相似度排名（1 = 最相似） |
| similarity | cosine similarity (1 - cosine_distance) |
| expires_at | TTL 过期时间 |

### 与 observation-vs-observation 搜索的区别

`registered_person_history` / `temporary_face_history` 模式（未来实现）：

| 字段 | observation-vs-observation |
|---|---|
| query_observation_id | NULL（query 来自 person 或临时上传） |
| query_gallery_embedding_id | NULL 或 gallery embedding |
| matched_observation_id | **NOT NULL** — 匹配到的历史 face_observation |
| matched_camera_id / source_id / track_id | **NOT NULL** — 历史观测的上下文 |

## 幂等性

gallery_match 的幂等键为 `UNIQUE(search_request_id, query_gallery_embedding_id)`。

- 同一 search_request_id 下，不同的 gallery_embedding_id 可以插入多行（topK）
- 同一 search_request_id + 同一 gallery_embedding_id 重复插入被静默跳过

## 迁移 007

`db/migrations/007_phase_f3_5_match_results_gallery_semantics.sql`：

1. 新增 `query_observation_id UUID FK -> face_observations(id)`
2. 新增 `query_source_observation_id TEXT FK -> face_observations(source_observation_id)`
3. 新增 `UNIQUE(search_request_id, query_gallery_embedding_id)`
4. 放宽 `matched_observation_id` 为 NULLABLE
5. 放宽 `matched_camera_id` / `matched_source_id` / `matched_track_id` 为 NULLABLE
6. 不删除旧约束 `UNIQUE(search_request_id, matched_observation_id)`（供未来 observation-vs-observation 使用）

## CLI 用法

```bash
# 基本用法
python match_gallery.py --observation-id face:cam1:42:1000

# 自定义参数
python match_gallery.py --observation-id face:cam1:42:1000 \
    --top-k 5 --min-similarity 0.6

# 限定特定 person
python match_gallery.py --observation-id face:cam1:42:1000 \
    --person-ids 1,2,3

# 幂等重跑
python match_gallery.py --observation-id face:cam1:42:1000 \
    --search-request-id <uuid>

# dry-run（不写 DB）
python match_gallery.py --observation-id face:cam1:42:1000 --dry-run
```

## 测试

```bash
# unit tests (no DATABASE_URL)
pytest harness/tests/test_gallery_match.py -k "not integration"

# integration tests
DATABASE_URL=postgresql://... pytest harness/tests/test_gallery_match.py
```

测试覆盖：
- CLI 参数验证（9 tests）
- MatchResultRepository mocked 测试（15 tests）
- 集成测试：insert/query, idempotent, topK=3 persistence, independent requests, full flow, delete_expired, cleanup（8 tests）

## F3.5 与后续阶段的关系

- F3.5 只写 match_results，不产生 security.events
- F4（重点人员布控）将使用 watchlist_hit，基于 gallery match 结果触发告警
- F5（一键找人）将使用 live_search_hit，基于实时 gallery match 触发通知
- observation-vs-observation 搜索（registered_person_history / temporary_face_history）将在后续阶段实现
