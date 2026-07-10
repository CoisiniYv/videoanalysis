# Phase 1 Module Boundaries

状态：基于 `docs/code_review/00_inventory.md` 的当前工作树清点提出模块边界；按原要求，等待确认后再进入 Phase 2，不合并模块报告、不跳过模块。

## 拟确认模块清单
| 顺序 | 模块 | Phase 2 报告文件 | 一句话职责 | 优先级/来源 |
| --- | --- | --- | --- | --- |
| 1 | services/event-worker | module_services_event_worker.md | 从 Redis/Savant 事件流消费检测事件，执行 admission、cooldown、record_request 发布与 evidence_task 创建前置逻辑。 | 核心优先 |
| 2 | services/clip-worker | module_services_clip_worker.md | 处理旧 per-event Replay 录制路径、proof wait、Replay job 调度、分片路由和 evidence task 状态推进。 | 核心优先 |
| 3 | services/media-worker | module_services_media_worker.md | 消费 Replay/rolling-cache 输出，完成 finalizer、rolling-cache materialization、bundle 落库/索引与证据文件整理。 | 核心优先 |
| 4 | services/api | module_services_api.md | 提供 8090/API 控制面，包括 runtime_apply、runtime epoch/restart、camera/rule 配置、events/evidence bundles API。 | 核心优先 |
| 5 | services/analysis-forwarder | module_services_analysis_forwarder.md | 转发/分流 Savant 分析输出，支撑多路/双分片拓扑中的后处理输入链路。 | Phase 0 实际发现，建议纳入 |
| 6 | services/face-worker | module_services_face_worker.md | 处理 face_observations、图库注册/匹配、watchlist/gallery match、Qdrant/pgvector 存储与同步。 | Phase 0 实际发现，建议纳入 |
| 7 | modules/savant_security | module_modules_savant_security.md | Savant 推理主模块与自定义 filters/pyfuncs/services/rules，包含 pts_fps_gate、frame_annotation_exporter、行为规则与 face/person observation export。 | 按用户建议核心后展开 |
| 8 | modules/savant_replay | module_modules_savant_replay.md | Replay 服务配置与 replay-a/b/c/d/e/f/g/h 分片配置，定义旧 Replay 录制路径的输入输出。 | 按用户建议核心后展开 |
| 9 | services/evidence-viewer | module_services_evidence_viewer.md | 8090 前端/轻量服务，负责 evidence/operator/storage maintenance 页面、标注渲染、continuous annotation 展示与播放状态。 | 按用户建议后续展开 |
| 10 | infra | module_infra.md | compose/env/config/generated runtime source 配置，定义服务拓扑、运行参数、端口、卷和分片。 | 配置审查对象 |
| 11 | db/migrations | module_db_migrations.md | PostgreSQL schema/index 迁移，覆盖 events/evidence_tasks/evidence_bundles/cameras/face/gallery/runtime epoch 等持久化合同。 | 数据合同审查对象 |
| 12 | scripts/runtime and scripts/ops | module_scripts_runtime_ops.md | 压测、doctor、启动/停止/迁移/打包/分析工具，包括 run_midterm_pressure60.py 与 rolling_cache sink entrypoint。 | 运行工具审查对象 |
| 13 | libs/evidence_metadata and libs/face_registration | module_libs_shared.md | 共享 evidence metadata 校验与人脸注册图片处理库，被服务和测试复用。 | Phase 0 实际发现，建议作为小型共享模块 |
| 14 | harness/tests | module_harness_tests.md | 测试套件本身，评估覆盖率、时序/并发回归用例和静态合同测试质量。 | 最后审查 |

## 边界说明
- `services/event-worker`、`services/clip-worker`、`services/media-worker`、`services/api` 是证据生成链路的核心审查优先级，Phase 2 先按此顺序展开。
- `services/api` 不拆成多个报告，但报告内部会单独覆盖 `runtime_apply`、runtime restart/epoch、evidence bundles API、camera/rule 控制面；这样可以避免把共享 repository/schema/config 上下文割裂。
- `services/analysis-forwarder` 与 `services/face-worker` 是 Phase 0 实际发现的活跃服务；它们不在原始预期模块示例中，但属于当前运行拓扑和事件/证据链路的一部分，建议纳入 Phase 2。
- `modules/savant_security` 作为一个模块报告，但内部会按 `filters/`、`pyfuncs/`、`services/`、`rules/`、`models/`、`adapters/` 分节，避免遗漏 `pts_fps_gate`、`frame_annotation_exporter`、Redis exporter、行为规则和 face/person observation export。
- `modules/savant_replay` 保持独立报告，重点覆盖 `REPLAY_TS_SYNC`、分片配置、GOP/keyframe/constant cadence 相关配置是否与 worker 代码一致。
- `services/evidence-viewer` 保持独立报告，覆盖 Flask 入口、数据库/文件索引、静态前端 `operator.js`/`evidence.js`/`maintenance.js`、播放状态与标注渲染。
- `infra` 覆盖 `infra/docker-compose.midterm.yml`、`infra/env/midterm.env`、`infra/config/*.json` 和 `infra/generated/`；归档 compose 只在历史一致性需要时引用，不作为当前运行入口。
- `db/migrations` 单独审查 schema/index/CAS/epoch/materialization_ready_at 数据合同，避免在各 worker 报告中重复完整迁移清单。
- `scripts/runtime and scripts/ops` 合并审查 `scripts/runtime/`、`scripts/tools/`、`scripts/maintenance/` 和 midterm start/stop/deploy/package 脚本；重点看压测 harness 与运行控制是否掩盖真实缺陷。
- `libs/evidence_metadata and libs/face_registration` 代码量较小且共享性质相近，建议合并为一个共享库报告；如果你希望更细，可拆成两个报告。
- `harness/tests` 最后作为独立模块审查，不仅列覆盖面，也评估测试是否真正覆盖并发/时序/状态机边界。

## 不单独出 Phase 2 报告的内容
- `services/source-adapter`、`services/savant-watchdog`：当前目录存在但 Phase 0 未发现活跃源文件；若后续发现 compose/runtime 仍依赖外部镜像或挂载文件，将在 `infra` 或相关服务报告中引用。
- `archive/` 与 `*/archive/phase-only/`：作为历史文档/历史配置清点保留；除非用于文档一致性核对，不作为当前代码质量审查主体。
- `testVideo/`：样例/本地媒体资产，不作为代码模块审查主体。
- `infra/generated/`、`services/*/infra/generated/`：作为生成快照/运行态配置证据审查，不直接等同源代码真实意图。

## Phase 2 报告统一模板
每个模块报告固定包含目标要求的 11 项：模块职责、关键文件与角色、数据流、并发与状态机模型、配置项清单、错误处理、测试覆盖、代码质量、与现有文档一致性、已知问题回归检查、风险与建议。涉及是否存在某问题的结论必须附路径和行号；未确认处明确写 `未确认/需要进一步验证`。

## 建议执行顺序
1. `services/event-worker`
2. `services/clip-worker`
3. `services/media-worker`
4. `services/api`
5. `modules/savant_security`
6. `modules/savant_replay`
7. `services/analysis-forwarder`
8. `services/face-worker`
9. `infra`
10. `db/migrations`
11. `services/evidence-viewer`
12. `scripts/runtime and scripts/ops`
13. `libs/evidence_metadata and libs/face_registration`
14. `harness/tests`
