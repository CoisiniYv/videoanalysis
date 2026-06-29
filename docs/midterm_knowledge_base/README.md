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

维护规则：

- 项目结构、运行方式或核心数据流变化时，优先更新本知识库，而不是只更新聊天记录。
- 新增性能压测、迁移策略或生产验收结论时，同步更新 [[06_Performance_Optimization_History|性能优化历史]]
  和 [[08_Open_Risks_And_Next_Actions|剩余风险与下一步]]。
- 不把运行时快照当作事实源。摄像头、规则、人员、人脸库和 evidence metadata 的事实源是 PostgreSQL。
- 本知识库是索引和解释层；细节和证据仍以源码、迁移、压测 artifact 和正式 docs/specs 为准。
