---
type: roadmap
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - risks
  - roadmap
---

# 剩余风险与下一步

## P0 真实 RTSP / 长时间 soak

当前 pressure source 证明不等于真实摄像头生产证明。下一步最重要：

- 混合真实 RTSP；
- 不同码率、分辨率、GOP、网络抖动；
- 断流重连；
- 8 FPS 长时间 soak，建议至少 2-4 小时起步。

验收：

- source 不退出、不重启；
- forwarder queue 不长期堆积；
- Savant effective FPS 达到所选 profile；
- pose/person/face observation 持续产出；
- Redis pending/lag 不增长；
- retained evidence 50/50 playable；
- lifecycle p95/p99 不超过 300s deadline；
- 8090 list/detail 可查 retained samples。

## P1 生产硬件 profile

仍需验证：

- 单 T4；
- 双 T4；
- 4090；
- 双 GPU；
- T4 + 3060 是否值得。

输出目标：

| 硬件 | 路数 | FPS | batch | topology | 证据延迟 | 结论 |
| --- | ---: | ---: | ---: | --- | --- | --- |

不要把 4090 pressure source 结果直接外推到 T4。

## P1 face-worker 同步链路

注册图库在线查询已经切到 Qdrant authoritative，并通过 60 路 8 FPS 压测和 20,000 向量 benchmark。
因此原来的“pgvector 大图库查询是否会线性放大”已经从主要风险降级。

当前剩余风险是：`face-worker` 仍在单 consumer loop 中同步完成 DB insert、规则解析、Qdrant 查询、
exact rerank、event publish 和 ACK。真实 RTSP 长时间运行或更高 face observation 速率下，仍需要确认
端到端 ACK/pending 是否稳定。

已证明：

- 60 路 8 FPS authoritative run 下 Qdrant query p95/p99 为 3ms/4ms；
- 5000 人 x 4 图，即 20,000 向量 gRPC benchmark all-search p95/p99 为 4.037ms/6.427ms；
- fallback count 为 0；
- `watchlist_hit` payload 和 8090 evidence 语义未改变。

下一步需要：

- 真实 RTSP soak 下记录 observation insert、rule resolution、Qdrant query、exact rerank、event publish、
  ACK p95/p99；
- 如果 Qdrant query 已达标但 `security.face_observations` pending/ACK 仍异常，再拆
  persistence/matching 队列；
- 如果生产图库增长到 50k/100k active embeddings，再追加同脚本 benchmark。

## P1 Savant 阶段级 latency

当前整体吞吐可观测，但模型链内部阶段不足。

需要阶段级指标：

- pose detector；
- tracker；
- behavior_rules；
- face detector；
- face_person_associator；
- AdaFace；
- face_reid_gate；
- pyfunc 后处理；
- DeepStream batch wait / queue wait。

目标是把“8 FPS 波动”定位到具体阶段，而不是只看整体 effective FPS。

## P2 media finalizer 扩展

当前不要继续大改。只有以下情况出现再升级：

- 真实 RTSP soak 下 lifecycle p95/p99 超 300s；
- materialization expired 增多；
- production 要求全事件 evidence，而不是 retained high-value evidence；
- CPU 已平滑但 queue 长期不下降。

候选路线：

- 内部 worker pool；
- 多 media-worker 容器 + DB-backed claim；
- 独立 finalizer service。

风险：

- 重复终态；
- 文件 cleanup 竞态；
- Replay sink 堆积；
- DB claim 锁竞争；
- 磁盘膨胀。

## P2 8090 生产化

生产前仍需：

- 鉴权；
- 角色权限；
- 更完整操作审计；
- WebSocket 实时告警；
- 运行状态可视化强化；
- 配置保存 / runtime apply 的 UI 防误操作。
