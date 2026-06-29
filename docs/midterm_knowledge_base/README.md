---
type: knowledge-base-readme
project: video-analytics-midterm
updated: 2026-06-29
---

# Midterm Project Knowledge Base

这是本项目独立的 Obsidian 风格知识库，用于让人和 AI agent 快速理解
`video-analytics` midterm 栈的模块职责、数据流、运行控制、证据链、性能优化历史和剩余风险。

入口：

- [[00_Index|知识库索引]]
- [[09_AI_Agent_Onboarding|AI agent 快速上手]]
- [[12_Service_Deep_Dive|服务深潜]]
- [[14_Performance_And_Acceptance_Playbook|性能与验收手册]]
- [[16_Troubleshooting_Playbook|排障手册]]

维护规则：

- 项目结构、运行方式或核心数据流变化时，优先更新本知识库，而不是只更新聊天记录。
- 新增性能压测、迁移策略或生产验收结论时，同步更新 [[06_Performance_Optimization_History|性能优化历史]]
  和 [[08_Open_Risks_And_Next_Actions|剩余风险与下一步]]。
- 修改数据库 schema、Redis stream、worker 输入输出或 evidence 生命周期时，同步更新
  [[11_Data_Contracts_And_Storage|数据契约与存储]]。
- 修改 runtime apply、拓扑、性能参数或 8090 操作语义时，同步更新
  [[13_Runtime_Control_Runbook|运行控制手册]] 和 [[15_Design_Invariants_And_Decisions|设计不变量与决策]]。
- 不把运行时快照当作事实源。摄像头、规则、人员、人脸库和 evidence metadata 的事实源是 PostgreSQL。
- 本知识库是索引和解释层；细节和证据仍以源码、迁移、压测 artifact 和正式 docs/specs 为准。
