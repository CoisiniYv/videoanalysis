# Midterm media-worker finalizer 平滑调度 8 FPS 压测报告

日期：2026-06-29

## 结论

本轮验证了 media-worker 第一阶段扩展模型：单进程 deadline-aware pacer +
CPU/ffmpeg thread limit。结论是：在不改变 evidence 存储方式、不启用多
media-worker 容器、不引入内部 worker pool 的前提下，60 路同卡双分支 8 FPS
证据链可以完成 retained-evidence 验收，并把 media-worker CPU 峰值从旧基线
约 1151% 降到约 98%。

本轮不是长期 soak，也不是真实 RTSP 混合输入证明。它证明的是当前 pressure
source 条件下，利用 300 秒 materialization deadline 做平滑调度是可行的。

## 压测配置

- Run ID：`pressure60_media_fullobs_8fps_20260629T092901Z`
- Artifact：`/data/video-analytics/artifacts/pressure60_media_fullobs_8fps_20260629T092901Z`
- 拓扑：同卡双分支 30+30，`--dual-shard-same-gpu --dual-shard-api --dual-shard-gpu 0`
- FPS：`8/1`
- Batch：`BATCH_SIZE=4`，`POSE_BATCH_SIZE=4`，`FACE_DETECTOR_BATCH_SIZE=4`，`FACE_EMBEDDING_BATCH_SIZE=16`
- `MAX_PARALLEL_STREAMS=32`
- 压力时长：120 秒
- drain：600 秒
- 保留证据：50 条

## 关键结果

- `PRESSURE_RUN_STATUS=passed`
- cleanup 前：429 events / 429 evidence_tasks / 55 bundles / 52 playable bundles
- cleanup 后：50 events / 50 tasks / 50 bundles / 50 playable bundles
- 8090 evidence proof：50/50 OK，`index_source=database`
- Redis `security.events` pending/lag：0/0
- Redis `security.face_observations` pending/lag：0/0
- Redis `security.record_requests` 最终 live pending：0
- Source containers：60 路，无退出、无重启、无 negative PTS
- Forwarder queue：0，send failures：0
- semantic outputs：pose/person/face observation 持续产出

## media-worker 指标

修正后的 pressure runner 会在 evidence drain 完成后重新抓取 worker logs，再生成
`downstream_observability_summary.json`。因此本轮 media 指标覆盖了最终 54 条
`media_event_finalized` 日志，而不是只覆盖压力阶段前半段。

| 指标 | 结果 |
| --- | ---: |
| media-worker CPU peak | 98.08% |
| finalized_count | 54 |
| retained playable | 50 |
| finalizer_failed_count | 0 |
| imageio_ffmpeg_fallback_count | 0 |
| throttle_paced_count | 54 |
| throttle_deadline_guard_count | 0 |
| queue_wait p50 | 141.488s |
| queue_wait p95 | 189.913s |
| queue_wait p99 | 193.068s |
| lifecycle p50 | 144.165s |
| lifecycle p95 | 192.325s |
| lifecycle p99 | 195.688s |
| deadline_slack min | 103.073s |
| finalization p95 | 3.853s |
| ffprobe p95 | 43ms |
| ffmpeg duration p95 | 0ms |

解读：

- CPU 峰值明显下降，说明 `MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT=4`、
  output-side x264 thread limit、`MEDIA_WORKER_FFMPEG_X264_PRESET=ultrafast` 和
  0.5 秒 post-finalize pacing 共同把物化阶段从瞬时吃满 CPU 改成较平滑的单进程调度。
- queue/lifecycle p95 上升到约 190 秒，但仍留有最小约 103 秒 deadline slack；
  这符合“用 300 秒 TTL 缓冲换 CPU 平滑”的设计。
- 本轮 retained evidence 没有触发 imageio fallback，也没有走常规 ffmpeg 转码路径；
  raw clip 可播放性和 DB-backed evidence index 均通过。

## face-worker 同步查询旁证

本轮 face-worker gallery 查询已被 pressure 报告采集：

- gallery query count：2201
- p95：1ms
- p99：1ms
- max：7ms
- watchlist emitted：233
- watchlist failed：0

当前小图库规模下，face-worker exact pgvector 查询不是本轮瓶颈。ANN index 仍应等代表性图库规模
EXPLAIN/压力数据后再决定。

## 已修正的报告缺口

本轮前一次压测发现 `downstream_observability_summary.json` 只统计到 17 条
`media_event_finalized`，原因是 pressure runner 在 evidence drain 前抓取了 worker logs。
已修正为：

1. 压力阶段结束后仍保留一次初始 diagnostics；
2. evidence drain 完成后重新执行 `capture_runtime_logs_since_start()`；
3. 重新计算 `diagnostics["log_summary"]`；
4. 再生成 downstream observability。

同时修正了 `imageio_ffmpeg_fallback_count` 的统计口径：从字符串出现次数改为解析
`imageio_ffmpeg_fallback_count=N` 后求和，避免把 `imageio_ffmpeg_fallback_count=0`
误报成 fallback。

## 当前判断

`PASS_POST_INFERENCE_SPEC3_MEDIA_FINALIZER_THROUGHPUT` 可以针对当前 pressure profile
视为通过：retained playable 未退化，media-worker CPU 峰值显著下降，queue/lifecycle
p95/p99 被明确量化且仍在 300 秒 deadline 内。

但不能发放最终 `PASS_POST_INFERENCE_60_STREAM_CLOSURE`，因为仍缺：

- 真实 RTSP 混合输入；
- 更长时间 8 FPS soak；
- 生产硬件，尤其 T4/弱卡 profile；
- 如果未来生产要求每个事件都 materialize，而不是只保留 admission 后的高价值证据，则还需要重新选择更强扩展模型。

