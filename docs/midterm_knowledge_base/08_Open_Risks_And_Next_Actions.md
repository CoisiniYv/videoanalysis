---
type: roadmap
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - risks
  - roadmap
---

# 剩余风险与下一步

## P0：提交与部署一致性

当前工作区含未提交服务、migration、测试和文档。提交/部署前必须确认：

- migration 032 与依赖它的 worker 代码同时交付；
- 032 以 autocommit、`CREATE INDEX CONCURRENTLY`、非压力窗口执行；
- production compose/env/override 与 API preset 来自同一 revision；
- 部署后记录 image、migration hash、runtime preset 和 effective container env；
- 不把本机 dirty working tree 结论直接当作生产已部署事实。

## P0：最新 revision 的 60 路复验

4090 60 路、8 FPS 在 2026-07-14 的当时 revision 通过。之后 rolling-cache 引入
双时间域并只对 40 路重新正式验证，因此仍需：

- 当前 revision；
- 正确 native-24 fixture 身份；
- 60 路、30/30、8 FPS；
- 600s sampling + 120s drain；
- 5+5、原始 FPS、timeline、bbox、trajectory、ROI pending 全门禁；
- 两轮可比结果和一次 worker restart soak。

## P0/P1：真实 RTSP 与恢复

pressure publisher 不等于真实摄像头。需要覆盖：

- 混合分辨率、码率、GOP 和厂商；
- 网络抖动、断流、重连、PTS rollback；
- source/session/epoch 变化；
- rolling fragment、read pin、coverage retry；
- event/person/face/media worker SIGKILL/restart；
- API 在 topology apply 中重启。

验收不能只有容器 Up；必须看 source age、FPS、queue、Redis lag/pending、task 收敛、
playability、annotation 和 residual ownership。

## P1：T4 热/功耗与 evidence 波峰余量

当前生产基线 40 路、4 FPS 已通过，但审计看到：

- GPU 84–85°C；
- 70W software power cap；
- SM clock 明显低于理论上限；
- 同步事件波峰 queue wait p95 约 47 秒；
- media permit/remux/finalizer 会短时打满，随后清空。

下一步优先改善散热并持续观察 hourly active=0、oldest ready、queue wait p95 和
expired=0。不要在当前物理条件下直接把生产基线改成 60 路。

## P1：后台 topology job 持久性

当前 status 文件持久，但执行是 API 进程内 daemon thread。需要评估：

- durable job row/queue；
- owner/lease/heartbeat；
- API restart 后 resume 或确定性 rollback；
- stop/apply 并发互斥；
- 操作审计和重试幂等。

## P1：worker 恢复闭环

Media lifecycle 已有 lease/fence/handoff，Clip 有 Replay fencing，但还需运行证明：

- finalizer_pending SIGKILL；
- cleanup_pending 长时间恢复；
- person stream 在 event storm 下无 trimming loss；
- face-worker DB/query/publish/ACK p95/p99；
- rolling retention 与 read pin 竞态；
- API epoch barrier 与旧 worker 并发。

## P2：8090 生产化

- 鉴权与 RBAC；
- 高风险操作审计；
- WebSocket upgrade proxy；
- 自动回滚/故障演练；
- `midterm_health.sh` 与当前服务清单同步；
- 更明确显示当前向量后端、migration revision 和 deployed image digest。

## P2：可选 Qdrant

Qdrant 有历史 20k benchmark，但当前 env 默认 pgvector。如重新启用：

- 显式配置 profile 与 `FACE_VECTOR_BACKEND=qdrant`；
- 验证 bootstrap/outbox/reconcile；
- 记录 exact rerank、fallback、shadow mismatch 和 sync lag；
- 在目标图库规模与真实 face observation 速率下重跑；
- 不能用历史 benchmark 声称当前运行态已启用 Qdrant。
