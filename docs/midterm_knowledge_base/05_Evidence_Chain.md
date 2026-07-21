---
type: evidence-note
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - evidence
  - rolling-cache
  - media-worker
---

# 证据链

## 设计目标

- 视频 evidence 保留可播放 `raw_clip.mov`；
- watchlist 命中可生成图片 evidence/轨迹图片；
- bundle、artifact、timeline、overlay 和状态以 PostgreSQL 为主；
- 8090 不依赖全目录扫描；
- frame UUID/PTS、runtime epoch、stream session 和 source identity 可审计；
- 失败、过期、策略跳过和 covered alias 不能伪装成成功。

## 完整预设主路径

```text
Replay raw output
  -> rolling-cache-sink A/B
  -> epoch/source/session fragments

Savant event
  -> event-worker cooldown
  -> evidence_task (after rolling prefill gate)

media-worker
  -> wait segment coverage
  -> fenced claim/read pin
  -> image or remux
  -> durable finalizer handoff
  -> finalizer process
  -> atomic publish + DB index
  -> materialized
```

完整预设固定：

```text
ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=true
ROLLING_CACHE_MATERIALIZATION_ENABLED=true
ROLLING_CACHE_FALLBACK_TO_REPLAY=false
MEDIA_WORKER_SCHEDULER_V2_ENABLED=true
MEDIA_WORKER_SEGMENT_INDEX_ENABLED=true
```

因此正常完整链不占 Replay job slot，也不启动每事件 video-file-sink export。

## 单分支兼容路径

```text
record_request
  -> clip-worker proof/planner/admission
  -> fenced Replay create
  -> video-file-sink
  -> media-worker sink finalization
```

Clip Coordinator V2 仍是有效实现。它负责 Replay-owned 阶段，不得恢复 rolling-owned
任务；media-worker 不重新执行 Replay admission。

## Canonical lifecycle v2

状态：

```text
manifest_ready
materialization_pending
materializing
materialized
materialization_deferred
materialization_failed
materialization_expired
materialization_skipped
```

运行 phase：

```text
waiting_ready
waiting_coverage
image_running
remux_running
finalizer_pending
finalizing
terminal
manual_quarantine
```

关键合同：

- `materialization_deferred` 是 claim-terminal；
- retry 使用 pending、`materialization_next_attempt_at` 和 normalized reason；
- claim 使用 lease owner/token/generation；
- remux 到 finalizer 使用 durable handoff；
- stale owner 不能 publish、commit 或 cleanup；
- terminal commit 后 cleanup，失败时记录 `cleanup_pending` 供恢复；
- migration 029–031 是该状态/围栏的 schema 基础。

## 时间域和视频

```text
frame_uuid/keyframe_uuid      视觉身份
Savant frame_pts              事件、轨迹、annotation 关联
rolling_cache_mux_pts         MOV 封装、segment、裁剪
event_ts_ms/created_at        业务时间和诊断
```

RTSP wallclock PTS 可能抖动。rolling sink 不修改原始 Savant PTS，而是为编码媒体建立
稳定 mux cadence；media-worker 用 event frame UUID（必要时最近原始 PTS）映射到 mux
窗口。annotation/timeline 仍保存原始分析时间域。

证据视频应约为原始 24 FPS，bbox/timeline 约为 4/8 FPS 稀疏标注。不能把稀疏 bbox
误判为视频丢帧。

## 物理文件与数据库

最终 evidence 可能包含：

- `raw_clip.mov` 或 image artifact；
- `metadata.json`/manifest 等兼容文件；
- 可重建 sidecar（单分支/诊断时）；
- PostgreSQL `evidence_bundles`、`evidence_artifacts`、
  `evidence_frame_timeline`、`evidence_overlay_segments`。

完整预设关闭 legacy derivatives，但仍写 expanded DB rows。8090 的 list/detail、
timeline 和 annotation 必须优先来自数据库；filesystem fallback 只能是显式兼容状态。

## Cooldown、coverage 与成功口径

- event 总数可以高于 task/bundle 数，因为 source+algorithm cooldown 会抑制重复任务；
- covered alias 必须通过 `evidence_event_links`/parent 语义可追踪；
- `materialization_skipped`、failed、expired 都不是 playable success；
- watchlist 图片可能在人员轨迹页展示，不一定出现在视频 evidence 列表；
- 验收应分别统计 events、unsuppressed tasks、physical bundles、playable event coverage。

## 当前容量结论

- T4 40 路完整链已验证，4 小时运行无 failed/expired/fallback；
- 同步事件波峰下 queue wait 明显，当前 media max-active/remux/finalizer 仍需监控；
- 4090 60 路较早 revision 通过，最新双时间域实现需要 60 路复跑；
- 不允许通过关闭 proof、降低原始视频 FPS、删除失败结果或放宽 deadline 伪造通过。
