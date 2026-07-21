---
type: index
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - midterm
  - knowledge-base
---

# Midterm 知识库索引

## 当前权威入口

- 仓库级架构：`docs/current_architecture.md`
- 当前实现/验证状态：`docs/current_mainline_status.md`
- 部署：`docs/midterm_deployment.md`
- 本次代码/文档同步审计：`docs/documentation_sync_audit_2026-07-20.md`

知识库专题页：

- [[01_System_Overview|系统总览]]
- [[02_Runtime_Data_Flow|运行时数据流]]
- [[03_Module_Map|模块地图]]
- [[04_Control_Plane_8090|8090 控制面]]
- [[05_Evidence_Chain|证据链]]
- [[06_Performance_Optimization_History|性能优化历史]]
- [[07_Deployment_Migration|部署与迁移]]
- [[08_Open_Risks_And_Next_Actions|剩余风险与下一步]]
- [[09_AI_Agent_Onboarding|AI agent 快速上手]]
- [[10_Glossary|术语表]]
- [[11_Data_Contracts_And_Storage|数据契约与存储]]
- [[12_Service_Deep_Dive|服务深潜]]
- [[13_Runtime_Control_Runbook|运行控制手册]]
- [[14_Performance_And_Acceptance_Playbook|性能与验收手册]]
- [[15_Design_Invariants_And_Decisions|设计不变量与决策]]
- [[16_Troubleshooting_Playbook|排障手册]]
- [[17_Testing_And_Change_Guide|测试与变更指南]]
- [[18_Artifact_And_Directory_Map|目录与 artifact 地图]]

## 当前一句话状态

系统已具备 8090 管理的单 GPU 双分支、ROI AdaFace、独立人体轨迹消费、
rolling-cache-first evidence、Scheduler V2 和 DB-backed 播放/标注。T4 40 路、4 FPS
是当前已验证生产基线；4090 60 路、8 FPS 在较早 revision 通过，但最新双时间域代码
仍需同 revision 复跑。

## 阅读规则

1. 当前代码/迁移优先；
2. `current_architecture` 和 `current_mainline_status` 优先于专题页；
3. 带日期报告只证明其 revision/profile/artifact；
4. archive 只作追溯；
5. `infra/env/midterm.env` 默认是 pgvector，Qdrant 是可选 profile；
6. 完整双分支主 evidence 路径是 rolling-cache，逐事件 Replay 是单分支兼容路径。
