# Current Mainline Status

更新时间：2026-07-22

## 当前基线

- 分支：`feat/roi-adaface-redis-20260711`；
- 产品 checkpoint：`fd39fdb`；exact-lease 修复：`2a57f20`；
- Candidate C 验证文档基线：`cb0595e`；本文是其后的 docs-only 结论增补；
- 部署入口：`scripts/midterm_start.sh`；
- Compose：`infra/docker-compose.midterm.yml`；
- 用户入口：`http://<host>:8090/operator`；
- 当前架构权威说明：`docs/current_architecture.md`。

user157 在 `cb0595e` 完成后工作区干净。下表的“已实现”表示代码/配置存在；“已验证”
只在有对应 revision、参数和 artifact 时成立。

## 实现状态

| 领域 | 当前实现 | 验证状态 |
| --- | --- | --- |
| 8090 管理入口 | UI、API/media 代理、摄像头/人员/evidence/维护/运行页 | 已实现 |
| 完整启动 | 摄像头批量选择、T4/4090 预设、异步 apply/status、五段进度 | 已实现；后台线程不跨 API 重启 |
| 双分支推理 | 单 GPU A/B，Replay/raw-fanout/Savant，自动或手动分片 | T4 40 路已验证；4090 60 路有早于最新双时间域改造的通过记录 |
| ROI AdaFace | Savant 导出 ROI，独立 TensorRT worker 批量 embedding | T4 40、历史 4090 60 均有验证 |
| 人体轨迹 | 独立 `person-observation-worker` 批量写 PostgreSQL；丢失 Redis group 后从 retained rows 自愈 | 40/60 压测报告均有覆盖；group 自愈与日志轮转已做代码/运行 smoke，仍缺 restart soak |
| rolling-cache | 自有 GStreamer sink、原子 fragment/manifest 发布、双时间域、分 catalog COW segment index、有界 I/O admission | 正确性通过；two/three/four-slot 300s retention 容量门均失败，refresh/pin 结构门打开 |
| evidence 固化 | Scheduler V2、image/remux/finalizer lanes、进程 finalizer、DB pool | exact-lease 正确性通过；Candidate B/C 的 60 路一小时容量门均失败 |
| 生命周期 | materialization v2、lease/fence/handoff、Replay create fencing | migrations 029–031；`2a57f20` exact-transfer 通过一小时正确性门 |
| 热路径索引 | cleanup recovery 与 algorithm cooldown concurrent indexes | migration 032 已提交；目标 DB 是否应用仍需单独核对 |
| Evidence UI | DB-backed list/detail/timeline/overlay；视频 HTTP Range | T4 审计通过；文件接口仅兼容 |
| 向量匹配 | pgvector 默认；Qdrant 可选且可由 PostgreSQL 重建 | Qdrant 有历史 benchmark，不是当前默认 profile |

## 当前完整双分支主链

```text
RTSP -> Replay A/B -> replay-raw-fanout A/B
  |-> sampled Savant A/B -> events/person/face ROI/source annotations
  `-> full-rate rolling-cache-sink A/B

events -> event-worker -> evidence_tasks
person stream -> person-observation-worker -> trajectories
face ROI -> adaface-roi-worker -> face-worker -> watchlist events
tasks + rolling segments -> media-worker -> DB-backed evidence -> 8090
```

完整预设 suppress `security.record_requests` 且关闭 Replay fallback。因此
`clip-worker -> Replay job -> video-file-sink` 是普通单分支和兼容链，不是完整预设主链。

## 当前运行预设

| 预设 | 当前代码参数 | 状态 |
| --- | --- | --- |
| `production_t4_40` | 40 路、20/20、4 FPS、batch 4、ROI 16、MPS 45/45/10、media 10/5/5 | 当前生产基线 |
| `local_4090_60` | 60 路、30/30、8 FPS、batch 4、ROI 16、无 MPS、media 12/8/16 | 输入/正确性已复验；Candidate B/C override 的一小时容量均失败，默认容量未获准 |

## 最近有效证据

1. `production_t4_pressure40_currentcode_2026-07-14.md`：T4 40 路、4 FPS、600s +
   120s drain 正式通过；
2. `production_runtime_evidence_audit_2026-07-15.md`：生产双分支约 4 小时稳定审计，
   evidence 无失败/过期/fallback，但同步波峰 queue wait p95 约 47 秒；
3. `local4090_pressure60_worker_regression_remediation_2026-07-13.md`：4090 60 路、
   8 FPS 在 7 月 14 日当时 revision 和正确 fixture 下通过；
4. `local_rolling_cache_dual_clock_remediation_2026-07-15.md`：后续双时间域实现通过
   40 路、4 FPS 正式门禁；
5. `media_worker_finalizer_admission_fenced_retry_2026-07-21.md`：`2a57f20`
   exact-lease 正确性通过；Candidate C 60 路、8 FPS 一小时只有
   4,742/5,781 materialized（82.03%），不能作为默认容量配置。

严格结论：当前代码可把 T4 40 路作为已验证基线；4090 60 路的输入稳定性、bundle
完整性与 exact-lease 正确性已在最新 revision 复验，但持续容量没有通过。B/C 两轮均在
约 300 秒 deadline 前形成 attempt=0 ready backlog，Phase 6 保持打开。

首个 two-slot I/O admission r300 诊断把 poll-gap p95 降到 1.736s，但仍形成
70.94s ready-to-remux p95；它同时暴露 person consumer `NOGROUP` 循环和约
168GB/85GB person/face Docker 日志。当前实现已增加 retained-row group 自愈和
50MB×3 日志轮转，受控删除 group 的 smoke 与 103 项相关测试通过。

防护生效后的 two-slot 受控复跑保留在
`pressure60_8p1_ioadm2_b10m_r300c_20260721T1740Z`；ready-to-remux/media queue/lifecycle
p95 仍为 105.45s/126.82s/127.21s。随后通过 artifact-audited override 只把 admission
改为 three-slot，结果保留在
`pressure60_8p1_ioadm3_b10m_r300_20260721T1809Z`。该轮 60/60、8.0527 FPS、969/969
正式任务 materialized、1,007/1,007 retained video 全部通过视频与 annotation 门，person
persistence 104,679/104,679，且 send/queue/raw loss、attempt=0 expiry、recovery、
claim-busy、duplicate、finalizer failure 与 residual 均为 0。

three-slot 把 ready/media/lifecycle p95 明显改善到 37.21s/57.80s/58.30s，poll-gap p95
1.360s，但仍未达到 5s/10s/30s，oldest-ready p95=44.21s，metadata visibility p95=9.43s。
实际 remux、DB claim、finalizer pool wait p95 仅 0.708s/0.383s/0.792s；主要等待仍在
index admission/refresh/pin。

最终 four-slot 简单候选保留在
`pressure60_8p1_ioadm4_b10m_r300_20260721T1837Z`。输入、969/969 正式任务、1,008
retained video、annotation、person persistence 和全部 fence/residual 继续通过，但 ready/media/
lifecycle p95 退化到 55.77s/76.73s/77.21s，oldest-ready p95=62.93s，metadata visibility
p95=10.46s。slot-wait 略降而 refresh/pin/lock-hold 上升，确认增加并发放大 I/O convoy。
不再测试 width 5；下一轮以 width 3 为对照做 refresh/pin publication 结构修复。

## 已知开放项

### P0/P1

- 保持 width 3，不再扩大 admission；先做单一 refresh/pin publication 结构修复并重跑
  日常 300s retention 短门；
- 只有结构修复后的 r300 输入、容量、correctness、annotation、visibility、residual 全通过，
  才运行 3,840s endurance 短门；两者未通过前不再跑一小时；
- 独立处理 finalizer p95 5.244s / process-pool wait p95 5.576s，禁止与 index/WIP/remux
  调整一次性混在同一候选；
- 完成真实混合 RTSP 的断流、重连和长 soak；
- 完成 event/person/face/media worker restart/recovery soak；
- 继续观察生产 T4 84–85°C、70W power cap 和 evidence 波峰排队。

### P2

- topology apply 改为跨 API 进程可恢复的持久作业；
- 修正 `midterm_health.sh` 对 legacy source 和新 person worker 的固定服务清单；
- 增加鉴权、RBAC 和更完整操作审计；
- 实现 8090 WebSocket upgrade 代理；
- 如启用 Qdrant，必须显式记录 profile/env、sync/outbox 和 fallback 状态。

## 文档规则

当前架构和状态只由本文件、`docs/current_architecture.md`、部署说明与当前代码共同决定。
带日期报告是证据，不是永久 current status；`archive/phase-only/` 不作为部署或设计入口。
