# midterm Qdrant 人脸图库替换 baseline

日期：2026-06-29

## 结论

本次 baseline 说明：当前 live 小图库不是 `face-worker` 的主要延迟来源。

当前 PostgreSQL 中只有：

- active persons：3；
- total persons：5；
- active gallery embeddings：3；
- total gallery embeddings：5；
- active `face.watchlist` 规则：2。

在这种小规模、且规则明确指定 `target_person_ids` 的场景下，现有
pgvector exact 查询非常快：

```text
pgvector exact target-filtered gallery query:
  samples: 200
  p50: 0.167 ms
  p95: 0.198 ms
  p99: 0.301 ms
  max: 0.603 ms
```

因此，Qdrant 本次接入不是因为当前 3 条图库数据已经把 PostgreSQL
打满，而是为了生产规模下的高基数名单、全员名单、多人图库扩张和
60 路压力下的可观测切换能力。小名单 exact 查询仍然应该保留为
hybrid 路由的一部分。

## 当前规则目标

当前 8090/数据库中 active watchlist 规则：

- `primary_rtsp / Primary RTSP Camera`
  - `target_person_ids`: `[5, 6]`
  - `target_names`: `["Reese", "Finch"]`
  - `target_external_person_ids`: `["demo:f4_3:reese", "demo:f4_3:finch"]`
  - `min_similarity`: `0.75`
- `source_00000000-0000-4000-8000-781078565686 / lab`
  - `target_person_ids`: `[8]`
  - `target_names`: `["zr"]`
  - `target_external_person_ids`: `["3"]`
  - `min_similarity`: `0.75`

这也验证了空 target list 不能被静默扩展为全员搜索，否则会改变
8090 的 per-camera watchlist 语义。

## 当前 Redis 状态

采样时间点：

- `security.face_observations` length：666；
- `security.face_observations / face-worker-midterm` pending：0；
- `security.events` length：498；
- `security.events / event-workers-midterm` pending：0。

这说明本次 baseline 时不是 Redis consumer pending 堵塞。

## pgvector EXPLAIN 摘要

代表性 target-filtered 查询计划使用：

- `person_gallery_person_active_idx`；
- `persons_pkey`；
- execution time 约 `0.027 ms`；
- shared hit 约 8 blocks。

该计划对当前小 target list 是合理的，因此初始 hybrid 策略应该保持：

```text
FACE_VECTOR_SMALL_TARGET_THRESHOLD=5
```

即 target 人数小于等于 5 时优先走 pgvector exact；超过阈值、全员名单或
生产高基数名单时走 Qdrant 候选检索 + PostgreSQL exact rerank。

## 对 Qdrant 切换的影响

本次切换必须保持这些约束：

- PostgreSQL `persons` / `person_gallery_embeddings` 仍是事实源；
- Qdrant 只是派生索引，可从 PostgreSQL 重建；
- 默认 `FACE_VECTOR_BACKEND=pgvector`，不启动 Qdrant 时当前行为不变；
- `shadow` 模式只记录 Qdrant parity，不发 Qdrant 事件；
- `qdrant` authoritative 压测最终 `fallback_count` 必须为 0；
- `watchlist_hit` payload、`events.source_event_id`、8090 证据列表/详情语义不变。

当前代码已经按这个方向加入：

- `PgvectorGallerySearchBackend`；
- `QdrantGallerySearchBackend`；
- `ShadowGallerySearchBackend`；
- `HybridGallerySearchBackend`；
- `gallery_vector_sync_outbox`；
- `sync_qdrant_gallery.py` 的 `bootstrap` / `drain-outbox` / `reconcile` / `rebuild` / `status`。

## 后续验收

Qdrant authoritative 前还需要：

1. Qdrant 镜像和 `qdrant-client` 依赖可用后，只重建/重启 `face-worker`。
2. 执行 migration 021 后，运行：

```bash
python sync_qdrant_gallery.py --mode bootstrap
python sync_qdrant_gallery.py --mode reconcile
python sync_qdrant_gallery.py --mode drain-outbox
```

3. 切 `FACE_VECTOR_BACKEND=shadow`，确认 top1/person/threshold parity。
4. 再切 `FACE_VECTOR_BACKEND=qdrant`，跑 60 路压力测试。
5. 最终报告必须包含：
   - Qdrant query p95/p99；
   - exact rerank p95/p99；
   - outbox lag；
   - fallback count；
   - Redis pending；
   - face-worker latency；
   - retained evidence playable；
   - 8090 list/detail proof。

最终通过条件：

```text
PASS_FACE_GALLERY_QDRANT_CUTOVER
```
