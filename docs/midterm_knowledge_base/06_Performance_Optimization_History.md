---
type: performance-history
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - performance
  - optimization
---

# 性能优化历史

## 当前已证明能力

| Profile | 结论 |
| --- | --- |
| 60 路 3 FPS downstream evidence | 50/50 playable，8090 可查 |
| 60 路 16/1 高入口压力 | 证据链保住 50/50，但不是 16 FPS 推理证明 |
| 单 4090 同卡双分支 60 路 4 FPS | retained-evidence 通过 |
| 单 4090 同卡双分支 60 路 8 FPS | retained-evidence 通过 |
| media finalizer pacer 8 FPS | CPU peak 约 98%，lifecycle p95 约 192s |
| face-worker Qdrant cutover | 60 路 8 FPS fallback=0，20,000 向量 all-search p95/p99=4.037ms/6.427ms |

## 已完成优化

### Event-worker record request 去 O(N)

旧问题：

```text
XRANGE security.record_requests - +
```

每次发布 record request 前扫全 stream，事件量上来后成为 CPU/Redis 热点。

新设计：

- Redis `SET NX EX` 幂等键；
- `has_request()` 只做 `EXISTS`；
- `XADD` 失败释放 key；
- duplicate retry task 终态化为 `recording_policy_skipped:duplicate_record_request`。

### Downstream observability 固化

pressure runner 固定输出：

- Redis streams / consumer groups；
- PostgreSQL run summary / table stats / lifecycle；
- event-worker dedupe counters；
- face-worker watchlist/gallery counters；
- media-worker queue/lifecycle/ffprobe/ffmpeg/throttle；
- worker docker CPU；
- 8090 evidence proof。

### 8090 配置保存不误重启

算法/ROI 保存改为 config sync，不走 full runtime apply。

验收语义：

- `containers_restarted=[]`
- `source_containers_touched=[]`

### Replay shard / 双分支证据链

同卡双分支 30+30 支持：

- `replay-a` / `replay-b`；
- `video-file-sink-a` / `video-file-sink-b`；
- `analysis-forwarder-a` / `analysis-forwarder-b`；
- `savant-a` / `savant-b`；
- clip-worker 按 source 选择 replay shard。

pressure harness 校验 topology replay shard JSON 和 clip-worker observed JSON SHA256 一致。

### Media-worker 平滑调度

解决的问题：

- 8 FPS pressure 下 media-worker CPU 曾约 1151%；
- evidence 能完成，但瞬时 CPU 太猛。

当前模型：

- 单进程 deadline-aware pacer；
- high-priority event type 优先；
- deadline 早者优先；
- finalization 后 0.5s sleep；
- deadline guard 90s；
- CPU/native/ffmpeg thread limit；
- 不改变 evidence 存储方式；
- 不引入多容器 DB claim 竞态。

结果：

- media-worker CPU peak 约 98%；
- 50/50 retained playable；
- lifecycle p95 约 192s；
- deadline slack min 约 103s。

### ffmpeg / fallback 观测

已修正：

- 容器内 `ffmpeg` / `ffprobe` 存在；
- output-side x264 thread limit；
- pressure report 在 drain 后刷新 logs；
- fallback count 按 `imageio_ffmpeg_fallback_count=N` 数值求和。

### Face-worker Qdrant 注册图库查询

旧风险：

- 注册图库查询依赖 pgvector exact search；
- 生产图库扩展到数千人员、每人多图时，查询成本可能随图库规模放大；
- 8090 watchlist hit 需要保持阈值语义和 payload 不变。

当前结果：

- PostgreSQL 仍是 `persons` / `person_gallery_embeddings` 事实源；
- Qdrant 是 `face_gallery_current` 派生索引，可从 PostgreSQL rebuild；
- `FACE_VECTOR_BACKEND=qdrant` authoritative runtime 已通过；
- 60 路 8 FPS 压测中 Qdrant query p95/p99 为 3ms/4ms，fallback count 为 0；
- 5000 人 x 4 图，即 20,000 向量 gRPC benchmark all-search p95/p99 为 4.037ms/6.427ms。

剩余风险：

- `face-worker` 仍是单 consumer loop；
- 后续关注 observation insert、rule resolution、exact rerank、event publish 和 ACK latency；
- 如果 pending/ACK 异常，下一步是 persistence/matching 解耦，而不是继续优化 pgvector gallery scan。

## 重要误区

- `16/1` 高入口压力不是 16 FPS 推理能力证明。
- 8090 页面状态不等于运行时真实状态，必须看 DB、runtime overview、container 状态和 generated config。
- 8 FPS pressure source 通过不等于真实 RTSP 长时间生产通过。
- 当前 retained evidence 通过不等于所有事件都会生成完整 evidence。

## 下一步性能工作

优先级：

1. 真实 RTSP 8 FPS 长时间 soak；
2. T4 / 双 GPU / 生产硬件 profile；
3. face-worker 同步链路 ACK/匹配 p95，必要时拆 persistence/matching；
4. Savant 阶段级 latency；
5. 如果 lifecycle 超 300s，再评估 media finalizer worker pool / 多容器 claim。
