# 当前程序架构

更新时间：2026-07-22

适用范围：产品分支 `feat/roi-adaface-redis-20260711` 及容量修复分支
`codex/segment-index-concurrency-fix-20260721`；产品 checkpoint `fd39fdb`，
exact-lease 修复 `2a57f20`，当前容量结构 checkpoint `1ec97fc`。
本文描述“这个 revision 实际写成什么样”，不等同于任意机器都已部署同一份代码。

## 1. 文档与事实源

判断当前行为时按以下优先级：

1. 数据库迁移、当前服务代码和测试；
2. `infra/docker-compose.midterm.yml`、`infra/env/midterm.env` 及运行时 override；
3. 本文、`docs/current_mainline_status.md` 和 `docs/midterm_deployment.md`；
4. `specs/` 中标明完成状态的规格；
5. 带日期的压测、审计和 code-review 报告；
6. `archive/phase-only/` 仅作历史追溯。

带日期的报告只能证明其记录的 revision、参数、硬件和时间窗口，不能覆盖当前代码
合同。当前工作区有未提交实现时，提交版文档与工作区行为也必须分开表述。

## 2. 系统边界

Midterm 是一套 RTSP 视频分析、人员/人脸检索和证据固化系统。

- 浏览器唯一主入口：`http://<host>:8090/operator`；
- `evidence-viewer` 提供 8090 静态页面，并代理 `/api/v1/*` 和 `/media/*`；
- FastAPI `api:8000` 只在 Compose 网络内使用；
- PostgreSQL 是配置、事件、人员图库和 evidence 状态/索引事实源；
- Redis Streams 是运行消息总线，不是最终状态源；
- 文件系统保存 `raw_clip.mov`、图片及必要 artifact；列表、timeline、overlay 和
  annotation 以数据库索引为主。

## 3. 当前完整双分支链路

8090 的 `production_t4_40` 与 `local_4090_60` 都是 `full_evidence` 预设。两者均为
单 GPU、A/B 双分支；4090 预设不使用 CUDA MPS，但仍使用 ROI AdaFace 和
rolling-cache。

```text
RTSP cameras
  -> dynamic source adapters
  -> Replay A/B (full-rate RocksDB ingest)
  -> replay-raw-fanout A/B
       |-> sampled frames -> Savant A/B
       |     -> behavior events -----------------------> security.events
       |     -> person observations -------------------> security.person_observations
       |     -> face ROI crops ------------------------> security.face_rois
       |     `-> source-scoped frame annotations -----> security.frame_annotations.<source>
       |
       `-> full-rate encoded frames -> rolling-cache-sink A/B
                                      -> epoch/source segment catalog

security.person_observations -> person-observation-worker -> PostgreSQL trajectories
security.face_rois -> adaface-roi-worker -> security.face_observations
security.face_observations -> face-worker -> matches/watchlist_hit -> security.events
security.events -> event-worker -> events + evidence_tasks + alerts
evidence_tasks + rolling segments -> media-worker Scheduler V2
  -> image/remux lanes -> durable finalizer handoff -> finalizer processes
  -> raw_clip/image + DB-backed bundle/timeline/overlay
  -> 8090 evidence / trajectory views
```

完整预设中的关键语义：

- rolling-cache 是高密度 evidence 的主物化路径；
- `ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=true`，不会为每个事件发布 Replay job；
- `ROLLING_CACHE_FALLBACK_TO_REPLAY=false`，当前完整预设不会静默退回慢路径；
- 启动时先关闭 evidence task 创建，等待 source 收敛和 25 秒 rolling prefill，再以
  激活时间下界重新开放任务；
- 原始视频约 24 FPS，Savant 分析 cadence 为 4 FPS 或 8 FPS；两者不能混为一谈；
- Savant 原始 PTS/UUID 用于事件、轨迹和标注身份；`rolling_cache_mux_pts` 只用于
  稳定封装、segment 和视频裁剪。

## 4. 普通单分支与兼容路径

`scripts/midterm_start.sh` 先启动基础服务，并预创建但不启动 8090 管理的双分支
容器。普通单分支仍保留历史兼容链：

```text
RTSP -> Replay -> replay-raw-fanout -> analysis-forwarder -> savant-security
  -> event-worker -> security.record_requests -> clip-worker
  -> Replay job -> video-file-sink -> media-worker -> evidence
```

该路径默认 `ROLLING_CACHE_ENABLED=false`、Replay fallback 可用，并保留 in-pipeline
AdaFace。它是单分支/兼容/回退能力，不是 8090 两个完整双分支预设的主 evidence
数据流。

## 5. 运行预设

| 项目 | `production_t4_40` | `local_4090_60` |
| --- | ---: | ---: |
| 摄像头 | 40，A/B 20/20 | 60，A/B 30/30 |
| GPU | 单 T4，GPU0 | 单 4090，GPU0 |
| 分析 FPS | 4 | 8 |
| Pose / Face batch | 4 / 4 | 4 / 4 |
| ROI AdaFace batch | 16 | 16 |
| Face interval | 3 | 7 |
| CUDA MPS | 45% / 45% / 10% | 关闭 |
| cooldown | 60 秒 | 30 秒 |
| rolling retention | 600 秒 | 600 秒 |
| media max active | 10 | 12 |
| remux / finalizer process | 5 / 5 | 8 / 16 |

参数权威定义在
`services/api/app/services/runtime_topology.py::RUNTIME_PROFILE_PRESETS`。页面标签或
旧报告中的数字若与代码不同，以当前代码为准。

## 6. 8090 控制面

```text
Browser :8090
  -> evidence-viewer proxy
  -> FastAPI
  -> PostgreSQL config/state
  -> generated snapshots
  -> Docker Engine API
  -> dynamic sources and pre-created operator containers
```

普通操作员推荐流程：

1. 登记摄像头、ROI 和算法规则，不必逐路加入当前运行；
2. 在“选择摄像头并启动”中选择命名预设和精确路数；
3. 自动均分或手动指定 A/B；
4. 启动异步 topology apply；
5. 页面轮询持久化状态文件，展示预检、分支初始化、source 收敛、rolling 预热和
   evidence 开放进度。

后台任务由 API 进程内线程执行，状态原子写入
`/data/video-analytics/media/.runtime/topology_apply_status.json`。页面刷新不会丢失
进度，但 API 进程重启会中断任务并把状态标为失败；它不是跨进程的持久作业队列。

停止完整双分支时，系统会停止采集和推理、禁用当前摄像头，但保留 event/media 与
rolling sink 做有限收尾。不要把“停止采集”误解成立即杀死所有 evidence worker。

## 7. 服务职责

| 层 | 服务 | 当前职责 |
| --- | --- | --- |
| 入口 | `evidence-viewer` | 8090 UI、API/media 代理、旧文件接口兼容 |
| 控制 | `api` | 配置、人员注册、evidence 查询、运行编排、延迟/性能/拓扑 |
| 视频 | Replay / raw fanout / analysis-forwarder | 全率存储、原始分发、分析采样 |
| 推理 | `savant-security` / `savant-a/b` | Pose、tracker、规则、Face、ROI/annotation export |
| 人脸 | `adaface-roi-worker` | 完整预设中的异步 TensorRT AdaFace |
| 事件 | `event-worker` | 事件、cooldown、任务、告警；不再兼任高率轨迹消费 |
| 轨迹 | `person-observation-worker` | 独立批量持久化人体轨迹，避免事件策略阻塞 |
| 匹配 | `face-worker` | 人脸 observation、图库匹配、watchlist event、轨迹图片 |
| 缓存 | `rolling-cache-sink` | 每 source/session H.264 passthrough、原子 fragment 发布、健康指标 |
| 兼容取证 | `clip-worker` / `video-file-sink` | Replay job 协调、围栏 admission、兼容/回退输出 |
| 固化 | `media-worker` | Scheduler V2、segment index、租约/围栏、finalizer、DB 索引、清理 |

`person-observation-worker` 与 `face-worker` 是高率 Redis consumer。它们在
stream/group 被运行清理删除后会从 retained stream row 重新创建 group，而不是持续
输出 `NOGROUP` traceback；两者的 Docker `json-file` 日志均按 50MB、3 files 默认
轮转，避免消费故障把根分区写满。恢复从 stream id `0` 开始，依靠数据库幂等写入
收敛重复，不能用 `$` 跳过已发布 observation。

## 8. Evidence 生命周期与所有权

`libs/evidence_lifecycle/contract.py` 定义 canonical materialization v2：

```text
manifest_ready / materialization_pending
  -> materializing
       waiting_ready | waiting_coverage | image_running | remux_running
       -> finalizer_pending -> finalizing
  -> materialized | materialization_deferred | materialization_failed
     | materialization_expired | materialization_skipped
```

- PostgreSQL 保存 phase、owner、next attempt、lease owner/token/generation、handoff 和
  terminal reason；
- `materialization_deferred` 是终态，不再兼作可重试队列；
- rolling 任务只由 media-worker 恢复，Replay-owned 阶段由 clip-worker 管理；
- attempt 先写 staging，再校验 fence 并原子发布；清理失败进入 durable
  `cleanup_pending`；
- Replay create 使用 owner/token/generation 和 plan hash 防止旧进程重复提交；
- 迁移 029–031 建立生命周期与 Replay 围栏，迁移 032 增加 cleanup/cooldown 热路径
  索引；032 使用 `CREATE INDEX CONCURRENTLY`，必须在 autocommit 且非压力窗口执行。

## 9. 数据和存储

| 状态/数据 | 权威位置 |
| --- | --- |
| 摄像头、ROI、规则 | PostgreSQL；YAML 是导出快照 |
| 事件、任务、bundle、timeline、overlay | PostgreSQL |
| 人员和注册 embedding | PostgreSQL |
| 在线向量查询 | 默认 `pgvector`；Qdrant 是可选、可重建 profile |
| 运行消息 | Redis Streams |
| Replay 全率历史窗口 | `/data/video-analytics/replay-midterm*` |
| rolling 在线窗口 | 默认 `/home/user/video-analytics-fast/rolling-cache` |
| rolling 中间物化 | 默认 `/home/user/video-analytics-fast/rolling-cache-materialized` |
| 最终 evidence | `/data/video-analytics/media/evidence` |
| B 分支 engine cache | `/data/video-analytics/models-savant-b` |

Qdrant 支持和历史 benchmark 仍有效，但 `infra/env/midterm.env` 当前默认
`FACE_VECTOR_BACKEND=pgvector`，且 operator 预设不覆盖该值。只有显式启用 qdrant
profile/环境后，才能把运行态描述为 Qdrant authoritative。

## 10. 当前验证边界

- 生产 T4 40 路、4 FPS、双分支、ROI AdaFace、rolling-cache、5+5 evidence 已在
  2026-07-14 正式门禁通过，并在 2026-07-15 做过约 4 小时只读运行审计；
- `2a57f20` 的 4090 60 路、8 FPS Candidate C 一小时运行确认输入稳定、4,826 个
  retained bundle 完整且 exact-lease/finalizer admission 无 recovery/claim-busy/
  duplicate/residual；Phase 4 该正确性子门可关闭；
- 同一运行的容量门失败：5,781 个正式任务仅 4,742 materialized，1,039 个 attempt=0
  expired；Candidate C 的 82.03% 又低于 B 的 85.17%，两者都不能作为默认 60 路配置；
- B/C 对照定位出的 segment-index 热路径已完成分段计时、per-catalog COW 隔离、紧凑
  manifest/lazy native-row、mutation-driven reconcile 和 discovery-through-pin 有界 admission；
  filesystem mutation flock、read pin、identity fence 与 atomic rename 均保留；
- 完整 person-consumer 负载下 two/three/four-slot r300 均未过容量门。three-slot 已把
  ready-to-remux/media queue/lifecycle p95 从 105.45s/126.82s/127.21s 降到
  37.21s/57.80s/58.30s，并让 1,007/1,007 retained video 的 annotation 全通过，但仍高于
  5s/10s/30s 目标且 metadata visibility p95=9.43s；four-slot 又退化到
  55.77s/76.73s/77.21s。后续不再扩 admission，回到 refresh/pin publication 结构修复；
- 容量修复分支 `codex/segment-index-concurrency-fix-20260721` 的 `b7068b0` 已把 modern
  manifest read pin 缩到 source-window overlap 加两侧 guard；legacy/无效/无 overlap
  仍保守 pin 全 catalog。`segment_index_window_pin_smoke_20260721T192612Z` 在真实
  bind-mounted media-worker 中证明 10 个 segment 只 pin 5 个、实际 remux 3 个，双时间域、
  retention marker、identity fence 和日志指标均通过；
- bounded-pin width-three r300 已保留在
  `pressure60_8p1_pinwin_ioadm3_b10m_r300_20260721T192945Z`。输入、973/973 正式任务、
  1,010/1,010 retained video、annotation/person persistence、exact-lease 与 residual 全通过；
  pin-publication p95 从 1.934s 降到 0.073s，但 ready/media/lifecycle p95 仍为
  28.57s/48.69s/48.96s，metadata visibility p95=10.27s，所以 r300 严格容量门失败，
  r3840 仍不允许；
- 最新 `1ec97fc` 复用已知 immutable leaf membership，避免每次 parent 变化时重新 resolve/
  probe 全部已知 manifest；32+1 的测试只探测新 leaf。`0bc6c82` 同时修复 finalizer
  flattening 丢失 pinned-segment 等 count metric。真实容器 artifact
  `segment_index_refresh_pin_smoke_20260721T200925Z` 已证明 32→33 只 probe 新 manifest、
  finalizer 日志输出 pinned=5，并重验 legacy/identity/marker/retention 边界；
- 对应 width-three r300
  `pressure60_8p1_leafreuse_ioadm3_b10m_r300_20260721T2011Z` 的输入、972/972 正式任务、
  1,011 retained video、annotation/person persistence 与全部 fence/residual 通过，但
  ready/media/lifecycle p95 为 36.60s/57.03s/57.53s，metadata visibility p95=7.50s；
  正式窗口末仍有 79 active/39 ready，依赖 drain 才清零，因此容量门失败且 r3840 禁止；
- 该轮 5,847 个实际 segment 对应 12,122 次 `new_or_changed`，显示同一 catalog 的并发
  COW refresh 仍重复工作。下一结构门是 per-source/epoch singleflight 与完成时 refresh
  watermark；它尚未实现，不能写成容量已改善；
- Candidate C 使用 3,840s endurance retention、2,048-row cache；日常恢复配置是
  300s retention、256-row cache。两种 working set 必须分别验收，不能互相替代；
- 当前生产 T4 基线仍是 40 路，GPU 温度/功耗和同步事件波峰下的 evidence 排队余量
  有限；
- 尚未完成真实混合摄像头断流/恢复、worker restart soak、跨 API 重启的持久作业恢复、
  鉴权/RBAC 和 8090 WebSocket 透明代理。

对应证据：

- `docs/code_review/production_t4_pressure40_currentcode_2026-07-14.md`
- `docs/code_review/production_runtime_evidence_audit_2026-07-15.md`
- `docs/code_review/local4090_pressure60_worker_regression_remediation_2026-07-13.md`
- `docs/code_review/local_rolling_cache_dual_clock_remediation_2026-07-15.md`
- `docs/code_review/media_worker_finalizer_admission_fenced_retry_2026-07-21.md`
- `docs/code_review/media_worker_segment_index_capacity_fix_2026-07-21.md`

## 11. 变更同步要求

以下任一项变化都必须同时更新本文、`docs/current_mainline_status.md` 和对应专题文档：

- Compose service/profile 或启动/停止顺序；
- 8090 runtime preset、后台任务或 API；
- Redis stream、PostgreSQL migration、evidence 状态/所有权；
- rolling/Replay 主路径和 fallback 语义；
- 时间域、frame UUID/PTS 或视频 FPS 合同；
- 经过验证的容量结论或其适用边界。
