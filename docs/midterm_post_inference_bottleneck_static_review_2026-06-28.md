# Midterm 推理后链路静态瓶颈 Review - 2026-06-28

## 结论

本次只做静态分析，不启动服务、不读取 live 指标、不跑压测。结论是：当前推理后链路仍然有扩展瓶颈，但主要不在 Savant 推理热路径本身，而在 Redis 之后的单消费者、同步 DB/向量检索、证据素材化阶段。

这份记录用于等待 60 路压测结果期间固化待验证风险。后续修改应以压测 artifact 和 live 指标为排序依据，不在压测进行中改 shared runtime、compose、FPS/batch 配置或规则启用状态。

## 静态发现

### 1. face-worker 同步处理链路会随 face observation 线性放大

当前 `face-worker` 每条 face observation 的处理路径是：

```text
Redis security.face_observations
  -> 单条解析和 embedding 校验
  -> 单条插入 Postgres face_observations
  -> 同步 watchlist 规则解析
  -> 同步 pgvector gallery search
  -> 命中后同步写回 security.events
```

关键代码点：

- `services/face-worker/app/worker.py:568` 的 `_process_batch()` 内部仍按消息逐条处理；
- `services/face-worker/app/worker.py:596` 先 `_handle_observation()`，插入成功后才在同一循环里调用 `watchlist_emitter.emit_for_observation()`；
- `services/face-worker/app/vector_store.py:260` 的 `search_gallery()` 同步查询 gallery embedding；
- `db/migrations/005_face_observations.sql:39` 明确 `face_observations` 目前只有 btree indexes，ANN vector index deferred；
- `db/migrations/006_gallery_schema.sql:112` 的 gallery embedding 索引也是 person/is_active btree，不是向量 ANN 索引。

风险：

- 人脸导出量、图库人数、每人 embedding 数增长后，watchlist/gallery search 成本会线性放大；
- face-worker 作为单 consumer 时，DB insert、向量检索和事件写回共享同一处理 loop，任一环节慢都会拖住 observation 消费；
- 这会让 Savant 已经异步导出的 face observation 在 Redis/DB 之后堆积，而不是直接表现为推理热路径阻塞。

后续候选改法：

- gallery/person embedding 增加 pgvector ANN 索引，并用 `EXPLAIN ANALYZE` 固化查询计划；
- 将 observation 持久化与 watchlist/gallery match 解耦，至少让写入与匹配各自可扩容；
- 引入批量 insert、批量 match 或 per-source/person 限流；
- 增加 face-worker consumer 并发前，先确认 watchlist event 幂等、DB unique key 和 Redis pending 行为。

### 2. event-worker 录像请求去重是 O(N) Redis stream 扫描

当前 `RecordRequestPublisher.has_request()` 每次判断是否已有录像请求时，对 `security.record_requests` 做：

```text
XRANGE security.record_requests - +
```

关键代码点：

- `services/event-worker/app/record_request.py:323` 的 `has_request()` 每次全量扫描 stream；
- 同文件 `publish()` 使用 `maxlen=10000`；
- `infra/docker-compose.midterm.yml:608` 当前 midterm `EVENT_BATCH_SIZE=1`，且 compose 中 event-worker 是单 consumer。

风险：

- 每个可录像事件都会触发一次最多 10000 条 stream 的 JSON 解析和比较；
- 事件量上来时，这会成为 Redis/CPU 侧 O(N) 热点；
- 单 consumer + batch size 1 会放大该热点对事件消费延迟的影响。

后续候选改法：

- 用 Redis key/set 或 Postgres 唯一约束记录 `(source_event_id, strategy)` 幂等键，避免全 stream 扫描；
- 若仍保留 stream 侧判重，应限制扫描窗口或维护侧索引；
- 调整 `EVENT_BATCH_SIZE` 和 consumer 并发前，先补幂等测试和重复事件回放测试。

### 3. media-worker 证据素材化仍是单进程串行 finalizer

当前 media-worker 主循环是单进程轮询，依次扫描 sink 输出、探测媒体、裁剪/转码、decode 校验并写 evidence bundle。

关键代码点：

- `services/media-worker/app/worker.py:4304` 是单个 `while not shutdown_requested` 主循环；
- `services/media-worker/app/worker.py:4302` 创建 `_MaterializationGuard(cfg.materialization_max_active)`，但该 guard 是本进程内 active 计数保护，不是 worker pool；
- `services/media-worker/app/post_savant_evidence_bundle.py:393` 起 `ffmpeg` 做裁剪/转码，当前命令使用 `libx264`；
- `infra/docker-compose.midterm.yml:804` 当前默认 `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=4`，但静态代码未看到并行 finalizer 线程池或进程池。

风险：

- `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE` 会限制本进程同时进入物化的数量，但不会自动把单 loop 变成并行处理；
- 事件风暴下，ffprobe/ffmpeg/decode 校验会把 queue wait、evidence lifecycle 和 annotation TTL 压力放大；
- 如果前段推理健康但 evidence 迟迟不可播放，根因可能在素材化吞吐，而不是 Savant。

后续候选改法：

- 明确 media-worker 是 scale out 多进程、多 container，还是内部 worker pool；
- 将扫描、任务 claim、ffmpeg finalizer 和 DB 状态更新拆成可并行但幂等的阶段；
- 用压测报告里的 queue wait、ffmpeg child CPU、p95/p99 lifecycle 决定并发和 admission 参数；
- 先保留当前 admission/backpressure 策略，避免并发提升后把磁盘和 Replay sink 直接打满。

## 已缓解项

Savant 到 Redis 的 exporter 热路径已有隔离：

- `modules/savant_security/custom/services/redis_stream_writer.py:27` 使用有界异步 `AsyncRedisStreamWriter`；
- `enqueue()` 使用 `put_nowait()`，队列满时 drop 并计数，不长期阻塞 `process_frame`；
- 这说明当前“推理后链路”瓶颈排序不应优先回到 Savant Redis `XADD` 同步阻塞假设。

DB 队列查询也已有针对性索引：

- `db/migrations/018_events_table_performance_indexes.sql`
- `db/migrations/019_media_worker_events_queue_indexes.sql`
- `db/migrations/020_evidence_queue_playable_indexes.sql`

这些缓解项不能消除上面三个瓶颈，只是说明下一步应聚焦 face-worker、record request 幂等和 evidence materialization finalizer。

## 压测后判定口径

等 60 路压测完成后，建议用下面的指标决定修复顺序：

| 候选瓶颈 | 需要从压测或 live 指标确认的信号 |
| --- | --- |
| face-worker 同步 gallery/watchlist | `security.face_observations` lag / pending、face-worker CPU、gallery query p95、Postgres `face_observations` / `person_gallery_embeddings` query plan |
| record request 去重扫描 | event-worker CPU、`security.events` pending、`security.record_requests` stream 长度、`has_request()` 调用频率与耗时 |
| evidence materialization 串行 finalizer | evidence queue depth、queue wait p95/p99、ffmpeg child CPU、playable bundle p95、annotation missing ratio |
| Savant Redis exporter | exporter enqueue drops/write errors、Savant effective FPS、forwarder send failures；只有这些异常同时出现时才重新排到前面 |

## 协作边界

在并行 60 路压测结束前，本项只固化文档，不改代码、不 rebuild、不 recreate live 服务。后续动手时按阶段拆：

1. 先根据压测结果确认真实第一瓶颈；
2. 单独修 record request 幂等或 face-worker 向量索引，不和 media-worker 并发改造混做；
3. 每个阶段都补静态/单元测试，再用 targeted smoke 或压力复跑验证；
4. 若要调整 compose consumer 数、batch size、FPS 或 materialization 并发，必须在报告中记录 runtime epoch、enabled sources、规则集合和压测 artifact。
