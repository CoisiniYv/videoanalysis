# Video Analytics Platform

多路实时视频分析与证据固化系统，基于 Savant、NVIDIA DeepStream、TensorRT、
Redis 和 PostgreSQL。

文档基线：2026-07-20。当前工作区架构见
[`docs/current_architecture.md`](docs/current_architecture.md)。

## 启动与用户入口

```bash
bash scripts/midterm_start.sh
```

启动脚本会检查 Docker/GPU、准备 `/data/video-analytics` 和快速盘目录、验证模型、
构建基础镜像、启动管理与普通单分支服务，并预创建但不启动由 8090 管理的双分支
容器。日常操作统一进入：

```text
http://127.0.0.1:8090/operator
```

内部 FastAPI 只监听 Compose 网络的 `api:8000`，不作为宿主机客户入口。

普通操作员建议：

1. 在 8090 登记摄像头并配置 ROI/算法；
2. 点击“选择摄像头并启动”；
3. 选择 T4 40 路或 4090 60 路完整预设；
4. 勾选精确路数并自动均分或手动指定 A/B；
5. 等待后台任务完成 source 收敛、rolling-cache 预热和 evidence 开放。

不要为了准备批量运行而逐路点击“加入当前运行”。

## 当前完整链路

```text
RTSP cameras
  -> Replay A/B
  -> replay-raw-fanout A/B
       |-> sampled frames -> Savant A/B -> events/person/face ROI/annotations
       `-> full-rate frames -> rolling-cache-sink A/B

person observations -> person-observation-worker -> PostgreSQL
face ROI -> adaface-roi-worker -> face-worker -> watchlist events
events -> event-worker -> evidence tasks
rolling segments + tasks -> media-worker Scheduler V2/finalizers
  -> raw_clip/image + DB-backed timeline/overlay
  -> 8090 evidence and trajectory views
```

完整预设是 rolling-cache-first：`clip-worker -> Replay job -> video-file-sink` 仍作为
普通单分支和兼容路径存在，但不是两个完整双分支预设的主 evidence 路径。原始证据
约 24 FPS，Savant 分析 cadence 为 4/8 FPS。

## 运行预设

| 预设 | 目标 | 关键参数 |
| --- | --- | --- |
| `production_t4_40` | 单 T4 40 路，A/B 20/20 | 4 FPS，batch 4/4，ROI AdaFace 16，CUDA MPS 45/45/10，rolling 600s |
| `local_4090_60` | 单 4090 60 路，A/B 30/30 | 8 FPS，batch 4/4，ROI AdaFace 16，不用 MPS，rolling 600s |

精确值以
`services/api/app/services/runtime_topology.py::RUNTIME_PROFILE_PRESETS` 为准。

## 当前部署文件

| 用途 | 文件 |
| --- | --- |
| Compose | `infra/docker-compose.midterm.yml` |
| Env 默认值 | `infra/env/midterm.env` |
| 存储挂载 | `infra/midterm-storage.override.yml` |
| 8090 双分支预创建 override | `infra/operator-dual-runtime.override.yml` |
| Replay 配置 | `modules/savant_replay/config.midterm*.json` |
| Camera 快照 | `modules/savant_security/config/cameras.midterm.yml` |
| Savant module | `modules/savant_security/module.yml` |

PostgreSQL 是摄像头、规则、人员、图库、事件和 evidence metadata 的事实源；YAML
和 topology JSON 是运行快照。当前 env 默认人脸向量后端是 `pgvector`；Qdrant 是
可选 profile，而不是未加配置时的默认运行态。

## 默认端口

- 8090：操作台、API/media 代理；
- 6396：Redis；
- 8098：基础 Replay API；
- 18080：基础 Savant metrics；
- 18081：基础 analysis-forwarder metrics；
- 18184：基础 raw-fanout metrics；
- 18180/18181、18185/18186：A/B Savant/raw-fanout 诊断端口；
- 18187：ROI AdaFace metrics；
- 5439：仅 `local-postgres` profile。

## 当前验证边界

- T4 40 路、4 FPS、完整 evidence 链已通过当前代码正式门禁，并有约 4 小时运行审计；
- 4090 60 路、8 FPS 曾在 2026-07-14 通过；随后 rolling-cache 双时间域改造只对
  40 路重新正式验证，当前工作区仍需同 revision 的 60 路复跑；
- 生产 T4 基线保持 40 路，GPU 热/功耗和 evidence 波峰余量有限；
- 真实混合 RTSP 断流恢复、worker restart soak、鉴权/RBAC 和跨 API 重启的后台任务
  恢复仍未闭环。

## 文档入口

- 当前架构：`docs/current_architecture.md`
- 当前状态：`docs/current_mainline_status.md`
- 部署说明：`docs/midterm_deployment.md`
- 8090 操作指南：`docs/midterm_web_operator_guide.md`
- 单卡双分支专项流程：
  `docs/midterm_8090_single_gpu_dual_branch_operator_runbook_2026-07-14.md`
- 知识库：`docs/midterm_knowledge_base/00_Index.md`
- 前端/API：`docs/frontend_interface/README.md`
- 本次文档同步审计：`docs/documentation_sync_audit_2026-07-20.md`
- 历史阶段材料：各目录的 `archive/phase-only/`

带日期的测试/审计报告只证明其记录的 revision 和参数，不应替代当前架构文档。
