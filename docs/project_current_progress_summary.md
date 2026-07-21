# Project Current Progress Summary

更新时间：2026-07-20

## 一句话结论

Midterm 已从 6 月底的单分支、逐事件 Replay 证据链演进为 8090 管理的单 GPU 双分支、
ROI AdaFace、rolling-cache-first 和 DB-backed evidence 架构。T4 40 路是当前已验证生产
基线；最新工作区的 4090 60 路仍需在双时间域改造后按同 revision 复跑。

## 当前已完成

### 管理与部署

- `scripts/midterm_start.sh` 是唯一整栈入口；
- 基础服务启动后，脚本预创建并停止保存 8090 管理的 A/B、MPS、ROI 和 rolling
  容器；
- 8090 提供摄像头优先的批量选择和后台完整链路启动；
- 页面可显示 runtime overview、端到端 latency、A/B source/FPS/queue 和 evidence
  状态；
- 停止完整链路会停止采集/推理并禁用摄像头，同时保留 evidence 收尾进程。

### 推理与人脸

- A/B Replay/raw-fanout/Savant 支持 balanced/manual 分片；
- T4 预设用 CUDA MPS 45%/45%/10%，4090 预设不使用 MPS；
- 两个完整预设都使用 `face_roi_exporter -> adaface-roi-worker -> face-worker`；
- 高率 `security.person_observations` 已拆到独立
  `person-observation-worker`，避免 event policy 阻塞轨迹持久化；
- PostgreSQL 是图库事实源，当前默认在线向量查询为 pgvector；Qdrant 保留可选能力。

### Evidence

- full-rate raw fanout 同时向 rolling sink 提供编码帧、向 Savant 提供采样帧；
- 自有 rolling sink 使用每 source/session GStreamer pipeline 和同文件系统原子目录发布；
- 双时间域保持 Savant PTS/UUID 的业务身份，同时用 mux PTS 稳定 MOV/segment；
- 完整预设在 rolling prefill 完成后才开放 evidence task；
- event-worker 在完整预设抑制 per-event Replay record request；
- media-worker 使用 Scheduler V2、bounded lanes、DB pool、segment index、lease/fence、
  finalizer processes 和 durable cleanup recovery；
- 8090 list/detail/timeline/annotation 以 PostgreSQL 为主，`raw_clip.mov`/图片保留在
  文件系统。

### 数据合同

- migrations 029/030：materialization lifecycle v2 与队列索引；
- migration 031：Replay slot/create fencing；
- migration 032：cleanup/cooldown 热路径 concurrent indexes；
- `libs/evidence_lifecycle/contract.py` 是 API、workers、报告共用的状态词汇权威。

## 已验证能力

| 时间 | 场景 | 结论 |
| --- | --- | --- |
| 2026-07-14 | 生产 T4，40 路，4 FPS，600s + 120s drain | 正式门禁通过 |
| 2026-07-15 | 同一生产链约 4 小时只读审计 | 无 evidence failed/expired/fallback；波峰排队有限 |
| 2026-07-14 | 本机 4090，60 路，8 FPS，正确 native-24 fixture | 当时 revision 正式门禁通过 |
| 2026-07-15 | 双时间域改造，本机 40 路，4 FPS | 当前改造正式门禁通过 |

容量结论必须绑定报告中的 revision、fixture、硬件、路数、FPS、窗口和 artifact。

## 仍未完成

1. 当前工作区版本的 4090 60 路、8 FPS 正式复跑；
2. 混合真实 RTSP、不同 GOP/码率、断流和重连长 soak；
3. worker/API 重启恢复和双分支自动回滚；
4. topology 后台任务跨 API 进程持久化；
5. T4 散热/功耗整改后的扩容复验；
6. 鉴权、RBAC、WebSocket 代理和生产审计强化；
7. 健康脚本的服务清单与当前 Compose 角色同步。

## 当前入口

- 架构：`docs/current_architecture.md`
- 状态：`docs/current_mainline_status.md`
- 部署：`docs/midterm_deployment.md`
- 操作：`docs/midterm_web_operator_guide.md`
- 知识网络：`docs/project_knowledge_network.md`
- 文档同步审计：`docs/documentation_sync_audit_2026-07-20.md`
