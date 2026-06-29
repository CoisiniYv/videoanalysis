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

## P1 face-worker 大图库

当前小图库下 gallery query p95 很低，但生产图库会改变复杂度。

需要：

- 构造 100 / 500 / 1000 人图库；
- 每人多张 embedding；
- `EXPLAIN ANALYZE`；
- pressure 下 gallery query p95/p99；
- 验证 watchlist target-person filtering；
- 验证阈值正确性。

ANN index 只有在代表性图库证明 exact scan 成本真实存在后再加。若加 ANN，需要 exact rerank 保持阈值语义。

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
