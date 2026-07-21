# Project Knowledge Network

更新时间：2026-07-20

本页解决“先读哪份文档、哪份是当前事实、哪份只是历史报告”。

## 当前权威主干

```text
README.md
  -> docs/current_architecture.md
       -> docs/current_mainline_status.md
       -> docs/midterm_deployment.md
       -> docs/compose_inventory.md
       -> docs/midterm_knowledge_base/00_Index.md
       -> docs/frontend_interface/README.md
```

| 问题 | 先读 |
| --- | --- |
| 系统现在怎么连接 | `docs/current_architecture.md` |
| 哪些已实现/已验证 | `docs/current_mainline_status.md` |
| 怎么部署、启动、停止 | `docs/midterm_deployment.md` |
| 哪些 service/profile 会启动 | `docs/compose_inventory.md` |
| 操作员怎么使用 8090 | `docs/midterm_web_operator_guide.md` |
| API/页面合同 | `docs/frontend_interface/README.md` |
| 数据库、Redis、状态机 | `docs/midterm_knowledge_base/11_Data_Contracts_And_Storage.md` |
| 服务职责 | `docs/midterm_knowledge_base/12_Service_Deep_Dive.md` |
| 当前风险 | `docs/midterm_knowledge_base/08_Open_Risks_And_Next_Actions.md` |

## 架构关系

```text
Control plane
8090 -> evidence-viewer -> api -> PostgreSQL/config -> Docker runtime

Full evidence data plane
RTSP -> Replay A/B -> raw fanout A/B
  |-> sampled Savant A/B -> Redis events/person/face ROI/annotations
  `-> full-rate rolling-cache A/B
Redis/DB tasks -> workers -> media finalization -> DB-backed evidence -> 8090

Compatibility plane
event-worker -> record_requests -> clip-worker -> Replay job -> video-file-sink
```

## 主题网络

### 8090 与运行控制

- `docs/midterm_knowledge_base/04_Control_Plane_8090.md`
- `docs/midterm_knowledge_base/13_Runtime_Control_Runbook.md`
- `docs/midterm_8090_single_gpu_dual_branch_operator_runbook_2026-07-14.md`
- `docs/frontend_interface/02_api_inventory.md`

### Evidence 与 worker 结构

- `docs/midterm_knowledge_base/05_Evidence_Chain.md`
- `docs/midterm_knowledge_base/15_Design_Invariants_And_Decisions.md`
- `specs/31_midterm_evidence_architecture_remediation_plan.md`
- `specs/33_midterm_media_worker_evidence_scheduler_remediation_plan.md`
- `specs/34_midterm_clip_media_worker_structural_remediation_plan.md`
- `docs/code_review/clip_media_phase*.md`

规格描述目标和阶段，code-review 报告描述当时 checkpoint；当前是否完成仍以代码、
迁移、测试和 `docs/current_mainline_status.md` 为准。

### 性能和验收

- `docs/code_review/production_t4_pressure40_currentcode_2026-07-14.md`
- `docs/code_review/production_runtime_evidence_audit_2026-07-15.md`
- `docs/code_review/local4090_pressure60_worker_regression_remediation_2026-07-13.md`
- `docs/code_review/local_rolling_cache_dual_clock_remediation_2026-07-15.md`
- `docs/midterm_knowledge_base/14_Performance_And_Acceptance_Playbook.md`

报告结论只能在相同 revision/profile/hardware/input 下复用。

### 部署、目录和迁移

- `docs/midterm_deployment.md`
- `docs/compose_inventory.md`
- `docs/midterm_knowledge_base/18_Artifact_And_Directory_Map.md`
- `docs/midterm_clean_machine_migration_2026-06-25.md`
- `docs/midterm_uos_clean_machine_migration_steps_2026-06-29.md`

### 前端和 API

- `docs/frontend_interface/01_architecture.md`
- `docs/frontend_interface/02_api_inventory.md`
- `docs/frontend_interface/03_data_contracts.md`
- `docs/frontend_interface/04_views_and_modules.md`
- `docs/frontend_interface/05_development_and_validation.md`

## 阅读规则

1. 当前代码/迁移优先于所有 prose；
2. 当前架构文档优先于带日期报告；
3. 带日期报告优先于 archive，但只能证明其当时范围；
4. `PASS_*` token 必须同时核对 revision、参数和未完成项；
5. “代码存在”不等于“运行验证完成”；
6. HEAD 与 dirty worktree 不同，必须明确说明审计对象；
7. 历史 Qdrant、Replay shard 或 pressure 结论不能覆盖当前默认 env/profile。

## 本次同步记录

代码与旧文档的差异、处理范围和剩余实现风险见
`docs/documentation_sync_audit_2026-07-20.md`。
