---
type: evidence-note
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - evidence
  - replay
  - media-worker
---

# 证据链

## 设计目标

证据链目标是生成可审查、可播放、可追踪来源的 evidence。

当前语义：

- `raw_clip.mov` 是文件系统视频 artifact；
- manifest、timeline、overlay、annotation metadata 进入 PostgreSQL；
- 8090 evidence list/detail 以 DB-backed index 为主；
- raw clip 可播放但 annotation 缺失时，可以展示 degraded 状态，而不是把 evidence 判为完全失败。

## 状态流

```text
event created
  -> evidence_task pending
  -> record_request published
  -> clip-worker waits for proof / calls Replay
  -> replay_job_created
  -> video-file-sink output ready
  -> media-worker materializing
  -> materialized / materialization_failed / materialization_skipped
  -> evidence_bundles indexed
```

相关表：

- `events`
- `evidence_tasks`
- `evidence_bundles`
- `evidence_artifacts`
- `evidence_frame_timeline`
- `evidence_overlay_segments`

## Admission 与 backpressure

60 路事件风暴下，系统不是为每个事件全量生成 evidence。当前策略是通过 admission/backpressure
保留预算内高价值证据：

- global active budget；
- per-source active budget；
- per-event-type budget；
- clip-worker concurrency；
- per-shard concurrency；
- per-source replay concurrency；
- media-worker materialization backlog / deadline guard。

跳过的事件会以 `materialization_skipped` 等状态终态化，避免无限 pending。

## Clip-worker

输入：

- Redis `security.record_requests`
- PostgreSQL events/evidence_tasks
- Redis frame annotations / proof

输出：

- Replay job；
- evidence task 状态；
- replay shard diagnostics。

已优化：

- stale/缺失 DB 事件 pending record request 会 `XACK`；
- replay shard routing 支持 `REPLAY_SHARDS_JSON` / `REPLAY_SHARDS_CONFIG_PATH`；
- constant-cadence Replay 请求成为默认。

## Media-worker

输入：

- video-file-sink 输出目录；
- sink metadata；
- evidence task deadline；
- frame annotation / DB metadata。

输出：

- DB-backed evidence index；
- retained `raw_clip.mov`；
- terminal evidence state。

已优化：

- deadline-aware pacer；
- high-priority event type 优先；
- earliest deadline 优先；
- finalization 后 0.5s 平滑停顿；
- deadline guard 90s 内跳过停顿；
- `MEDIA_WORKER_MATERIALIZATION_CPU_THREAD_LIMIT=4`；
- ffmpeg output-side `-threads` 和 `ultrafast` preset；
- pressure report 在 drain 后刷新 logs；
- `imageio_ffmpeg_fallback_count` 按数值解析。

## 当前 evidence 延迟

在 `pressure60_media_fullobs_8fps_20260629T092901Z` 中：

- retained playable：50/50；
- media-worker CPU peak：98.08%；
- queue wait p95：约 189.9s；
- lifecycle p95：约 192.3s；
- lifecycle p99：约 195.7s；
- deadline slack min：约 103.1s；
- imageio fallback：0。

解释：

这是用 300 秒 materialization deadline 换 CPU 平滑。它不是告警触发延迟，而是事件创建到 evidence
可播放/入库完成的延迟。

## 何时需要更强 finalizer

暂时不建议直接上 worker pool 或多 media-worker 容器。只有出现以下情况再升级：

- 真实 RTSP / 长 soak 下 lifecycle p95/p99 接近或超过 300s；
- `materialization_expired` 增多；
- 生产目标从 retained high-value evidence 改为全事件 evidence；
- media-worker CPU 已平滑但队列持续增长。
