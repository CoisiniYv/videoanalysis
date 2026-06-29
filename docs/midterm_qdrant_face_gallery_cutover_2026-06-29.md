# midterm Qdrant 人脸图库切换与规模验收

日期：2026-06-29

## 结论

本次已经把 `face-worker` 在线图库检索切到 Qdrant authoritative 路径，并保留 PostgreSQL
作为人员和图库事实源。当前运行态为：

```text
FACE_VECTOR_BACKEND=qdrant
QDRANT_FALLBACK_TO_PGVECTOR=false
QDRANT_PREFER_GRPC=true
QDRANT_COLLECTION=face_gallery_current
QDRANT_BASE_COLLECTION=face_gallery_adaface_512_v1
```

最终验收口径：

```text
PASS_FACE_GALLERY_QDRANT_CUTOVER
```

需要注意：当前真实图库只有 3 条 active embedding，因此 60 路运行压测证明的是 Qdrant
authoritative 路径、fallback=0、8090 证据链和 worker 链路没有被破坏；未来“数千人员、每人几张
人脸图像”的查询压力，使用单独的 20,000 向量合成 benchmark 证明。

## 已完成修复

- `face-worker` 增加 `pgvector`、`shadow`、`qdrant`、`hybrid` 后端选择。
- Qdrant 作为派生索引，PostgreSQL `persons` / `person_gallery_embeddings` 仍是事实源。
- 增加 `gallery_vector_sync_outbox`，图库写入、删除、停用、人员姓名/外部 ID 更新会同事务写出
  Qdrant sync outbox。
- 增加 `sync_qdrant_gallery.py` 的 `bootstrap`、`drain-outbox`、`reconcile`、`rebuild`、`status`。
- Qdrant collection 固化为 `face_gallery_adaface_512_v1`，alias 为 `face_gallery_current`。
- 增加独立 `qdrant-sync-worker`，复用 face-worker 镜像常驻运行
  `sync_qdrant_gallery.py --mode run`。
- `sync_qdrant_gallery.py` 增加 `run` / `watch` 模式，循环 drain outbox。
- 修复崩溃后卡在 `processing` 的 outbox 行：超过
  `QDRANT_SYNC_PROCESSING_TIMEOUT_SECONDS` 后自动回到 `retry`。
- alias 创建后会验证 `face_gallery_current` 真实指向
  `face_gallery_adaface_512_v1`，不再只吞掉 `already exists`。
- 新增默认关闭的 batch 查询接口草案：`QDRANT_BATCH_QUERY_ENABLED=false`，
  `search_gallery_batch()` 可使用 Qdrant batch API 并合并 PostgreSQL exact
  rerank candidate fetch；主链路暂未接入 batch，所以线上语义不变。
- collection 默认启用 payload index、gRPC 查询、HNSW/optimizer 查询参数：
  - `QDRANT_PREFER_GRPC=true`
  - `QDRANT_INDEXING_THRESHOLD_KB=1000`
  - `QDRANT_FULL_SCAN_THRESHOLD_KB=1000`
  - `QDRANT_DEFAULT_SEGMENT_NUMBER=2`
  - `QDRANT_HNSW_M=16`
  - `QDRANT_HNSW_EF_CONSTRUCT=100`
- 修复 `face-worker` 在压测 source 下把 source_id-like 文本当 `camera_rules.camera_id uuid`
  查询的问题。现在会先把 observation 里的非 UUID `camera_id` 按 `cameras.source_id` 解析成真实
  camera UUID，再读取 per-camera watchlist 规则。
- 修复 Qdrant outbox 状态 SQL 中 aggregate `FILTER` 的位置错误。
- 压测脚本不再把 expected sampling gap 下的 `validate_seq_iq` 当作硬失败；只有 send failure、
  queue full、source 退出/重启、negative PTS 等真实入口异常才升级为失败。

## 60 路 Qdrant Authoritative 压测

报告：

```text
/data/video-analytics/artifacts/pressure60_qdrant_final_8fps_20260629T150103Z/report.json
docs/midterm_qdrant_pressure60_final_8fps_report_2026-06-29.md
```

配置：

- 60 routes；
- 8 FPS；
- 单 GPU 同卡双分支；
- batch size 4；
- 运行 300 秒，drain 600 秒；
- retained evidence 50；
- `FACE_VECTOR_BACKEND=qdrant`；
- `QDRANT_FALLBACK_TO_PGVECTOR=false`。

结果：

- status：`passed`
- failure reasons：`[]`
- warnings：`validate_seq_iq_expected_sampling_gap`
- Qdrant query：count 3283，p50 2ms，p95 4ms，p99 5ms，max 9ms
- exact rerank：count 3283，p50 1ms，p95 2ms，p99 3ms，max 8ms
- fallback count：0
- shadow mismatch count：0
- face-worker gallery latency：p50 3ms，p95 6ms，p99 8ms
- watchlist_hit emitted：630
- watchlist emit failed：0
- `security.face_observations` pending：0
- retained evidence：50/50
- 8090 evidence proof：50/50 OK，index source 为 `database`
- media finalization duration：p50 5.186s，p95 8.141s，p99 8.635s
- media lifecycle elapsed：p50 113.983s，p95 196.192s，p99 221.247s
- analysis-forwarder queue_full：0
- Savant send failures：0
- media finalizer failed：0，duplicate materialization：0，imageio fallback：0

压测后清理保留了 50 条可播放 evidence：

- events：50
- evidence tasks：50
- evidence bundles：50
- playable bundles：50
- retained event types：30 条 `watchlist_hit`，20 条 `intrusion`

## 20,000 向量规模 Benchmark

目的：覆盖“数千人员、每个人员几张人脸图像”的图库规模。本次使用：

- 5000 persons；
- 每人 4 张人脸；
- 共 20,000 个 512 维 AdaFace-like vectors；
- target sizes：2、20、200、all；
- 每档 300 次查询；
- candidate limit 20；
- `search_ef=128`；
- gRPC；
- 临时 collection，测试后自动删除。

报告：

```text
/data/video-analytics/artifacts/qdrant_scale/qdrant_gallery_scale_5000x4_rerun_20260629T150018Z.json
```

结果：

| target size | p50 | p95 | p99 | max | top1 self | missing self |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 0.857ms | 1.587ms | 4.863ms | 7.258ms | 1.0 | 0 |
| 20 | 1.199ms | 1.691ms | 2.846ms | 3.537ms | 1.0 | 0 |
| 200 | 1.503ms | 1.757ms | 2.386ms | 3.876ms | 1.0 | 0 |
| all | 1.981ms | 4.275ms | 6.801ms | 7.833ms | 1.0 | 0 |

验收阈值：

```text
max_p95_ms=75
max_p99_ms=150
status=passed
top1_self_hit_rate=1.0
top1_person_hit_rate=1.0
missing_self_count=0
```

这说明在 5000 人、每人 4 张图的规模下，Qdrant 查询本身不是当前 60 路的瓶颈。

## 当前运行态检查

Qdrant live collection：

- collection：`face_gallery_adaface_512_v1`
- alias：`face_gallery_current`
- PostgreSQL active gallery count：3
- Qdrant points：3
- outbox active：0
- Qdrant status：green
- update queue length：0

Redis live 检查：

- `security.face_observations / face-worker-midterm` pending：0
- `security.record_requests / clip-workers-midterm` pending：0

Rollback 检查：

- 只重启 `face-worker`，切到 `FACE_VECTOR_BACKEND=pgvector`，服务启动并初始化
  `backend=pgvector`。
- 再只重启 `face-worker`，切回 `FACE_VECTOR_BACKEND=qdrant`、
  `QDRANT_FALLBACK_TO_PGVECTOR=false`，服务启动并初始化 `backend=qdrant`。
- 过程中未 rebuild，未重启 Savant、Replay、clip-worker 或 media-worker。

## 仍需后续验证

- 当前 20,000 向量 benchmark 已覆盖“数千人员、每人几张图”。如果生产目标扩展到 50,000 或
  100,000+ active embeddings，需要追加同脚本 benchmark，并保持 p95/p99 SLA。
- 当前 Qdrant 仍在 `face-worker` 单 consumer loop 内同步查询。Qdrant 查询已足够快后，剩余
  face-worker 风险会回到 observation 入库、规则解析、event publish、ACK 延迟，以及是否需要拆分
  persistence/matching 队列。
- 本次没有改变 evidence 存储方式：metadata/json/timeline 仍在 PostgreSQL，视频仍在文件系统。
