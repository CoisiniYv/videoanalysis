---
type: performance-acceptance
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - performance
  - pressure
  - acceptance
---

# 性能与验收手册

## 当前有效基线

| 场景 | 结论 | 适用边界 |
| --- | --- | --- |
| 生产 T4 40 路、4 FPS | 2026-07-14 正式门禁通过 | 当前生产容量基线 |
| 同链路约 4 小时 | 无 evidence failed/expired/fallback | 波峰 queue wait 与 GPU 热余量有限 |
| 4090 60 路、8 FPS | 2026-07-14 当时 revision 通过 | 后续双时间域代码尚未同规模复跑 |
| 双时间域 40 路、4 FPS | 2026-07-15 通过 | 证明最新 rolling 时间域修复的 40 路闭环 |

不要再使用 6 月的 50/50 retained、单进程 pacer 或“Phase 2 未跑”作为当前主结论。

## 正式 profile

### T4 production

- 40 source，A/B 20/20；
- 4 FPS analysis；
- Pose/Face batch 4，ROI AdaFace 16；
- MPS 45/45/10；
- cooldown 60s；
- rolling prefill 25s、retention 600s；
- 当前 preset media max-active/remux/finalizer-process = 10/5/5；
- 5+5 evidence。

### 4090 local

- 60 source，A/B 30/30；
- 8 FPS analysis；
- Pose/Face batch 4，ROI AdaFace 16；
- 无 MPS；
- cooldown 30s；
- rolling prefill 25s、retention 600s；
- media 12/8/16；
- 5+5 evidence。

精确值以 runtime preset 代码和 artifact 中 observed container env 为准。

## 测试分层

### Ingress/forwarder

只证明 source/readability/queue/send。不能证明模型、ROI 或 evidence。

### Savant/ROI

证明 source visibility、effective FPS、Pose/Face output、ROI batch/pending。不能只看
GPU utilization 或配置值。

### Full evidence

完整预设要看：

```text
source -> Replay/raw fanout -> Savant/ROI/person/face
  -> events/tasks -> rolling coverage -> media scheduler/finalizer
  -> DB-backed evidence -> 8090
```

正常 full preset 不要求 clip-worker Replay job 数大于 0；相反，fallback/record request
不应成为主路径。

### Recovery soak

在正式吞吐基础上注入 source、event/person/face/media worker 和 API 重启，证明 durable
state 可以收敛且没有 duplicate bundle/job、stale lease 或残留容器。

## 核心门禁

### 输入与推理

- exact source count；
- RTSP/fixture identity；
- source restart/exited；
- A/B visibility；
- effective FPS；
- forwarder queue/full/drop/send failure；
- Pose/Face outputs；
- ROI published/batch occupancy/expired/pending。

### 人员与人脸

- person producer/persisted/source coverage/loss；
- person consumer lag/pending；
- face observation count/embedding norm/source coverage；
- gallery effective backend；
- watchlist event 与轨迹图片。

### Rolling 与 evidence

- segment source/epoch coverage；
- segment FPS，原始门槛不得被 analysis FPS 替代；
- segment index hit/miss/fallback/read pin；
- task status/phase/retry/lease；
- queue wait、oldest ready、WIP/lane depth；
- failed/expired/fallback/active after drain；
- physical bundle、playable event coverage；
- MOV duration/FPS/playability/HTTP Range；
- DB detail/timeline/annotation/bbox/person_context；
- cleanup/residual ownership。

### 时间域

- 原始 frame UUID/PTS 与 event/annotation 一致；
- mux PTS cadence 稳定；
- frame identity 到 mux window 映射可审计；
- 不允许 wallclock PTS 抖动造成短 clip/coverage false miss。

## 当前 T4 风险阈值

生产审计显示平均可持续，但同步波峰会把 permit/remux/finalizer 打满。持续观察：

- hourly active 是否回到 0；
- queue wait p95；
- oldest ready；
- failed/expired=0；
- GPU temperature/power cap/SM clock；
- DB index/publish latency；
- rolling maintenance lock wait。

40 路若持续低于 3.96 FPS，应先处理热/功耗，不直接扩容或放宽门禁。

## 60 路最新 revision 复验要求

- 600s sample + 120s drain；
- 正确 native-24 fixture hash；
- 60/60 source；
- analysis 8 FPS，raw evidence >=20 FPS；
- queue/full/send failure=0；
- person/face/ROI loss/pending=0；
- tasks/bundles 全收敛，无 failed/expired/fallback；
- 5+5、timeline/annotation/bbox/person context 全通过；
- 结果保留，不用清理掩盖失败；
- 至少两轮可比 + 一轮 restart soak。

## Qdrant 验收语义

历史 20k benchmark 是可选后端能力证据。当前默认 pgvector。如测试 Qdrant，报告必须
包含 effective backend、collection/alias、bootstrap/outbox lag、query/rerank p95/p99、
fallback/shadow mismatch；否则不能写“Qdrant authoritative run”。

## Artifact 必填

- run id、revision、dirty diff；
- hardware/driver/power/temperature；
- input path/hash/codec/GOP/bitrate；
- source count、FPS、batch、topology/MPS；
- preset 与 observed env；
- sampling/prefill/postfill/drain/cooldown；
- Redis/PG/runtime samples；
- evidence/trajectory/ROI/visual gates；
- logs、cleanup/preservation audit；
- failure/warning 分类和未声明项。

## 常见误读

- 分析 8 FPS 不等于 evidence 8 FPS；
- bundle 数小于 event 数可能来自 cooldown；
- watchlist image 不一定在视频列表；
- `security.record_requests=0` 在 full preset 是预期；
- materialization skipped/failed/expired 不能算 playable；
- 旧 60 路 pass 不自动证明新 revision；
- pressure publisher pass 不等于混合真实 RTSP pass。
