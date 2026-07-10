---
type: index
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - midterm
  - obsidian
  - knowledge-base
---

# Midterm 知识库索引

## 快速入口

- [[09_AI_Agent_Onboarding|AI agent 快速上手]]
- [[01_System_Overview|系统总览]]
- [[02_Runtime_Data_Flow|运行时数据流]]
- [[03_Module_Map|模块地图]]
- [[04_Control_Plane_8090|8090 控制面]]
- [[05_Evidence_Chain|证据链]]
- [[06_Performance_Optimization_History|性能优化历史]]
- [[07_Deployment_Migration|部署与迁移]]
- [[08_Open_Risks_And_Next_Actions|剩余风险与下一步]]
- [[10_Glossary|术语表]]
- [[11_Data_Contracts_And_Storage|数据契约与存储]]
- [[12_Service_Deep_Dive|服务深潜]]
- [[13_Runtime_Control_Runbook|运行控制手册]]
- [[14_Performance_And_Acceptance_Playbook|性能与验收手册]]
- [[15_Design_Invariants_And_Decisions|设计不变量与决策]]
- [[16_Troubleshooting_Playbook|排障手册]]
- [[17_Testing_And_Change_Guide|测试与变更指南]]
- [[18_Artifact_And_Directory_Map|目录与 artifact 地图]]
- Canonical pressure profile: `docs/midterm_pressure60_dual1gpu_profile_2026-07-09.md`

## 当前一句话结论

Midterm 栈已经具备 8090 管理、RTSP 接入、Replay 存储、Savant/DeepStream 推理、
Redis worker 后处理、DB-backed evidence 和离线迁移打包能力。当前最强证明是：

- 60 路 3 FPS 下游证据链通过；
- 单 4090 同卡双分支 60 路 4 FPS / 8 FPS retained-evidence pressure source 通过；
- 当前 60 路单卡双分支压测必须按
  `docs/midterm_pressure60_dual1gpu_profile_2026-07-09.md` 固定拓扑、batch、
  cooldown、duration、drain、`5:5/10:10/15:15` 证据窗口以及 DB-backed
  overlay/timeline visual gate；
- media-worker deadline-aware 平滑调度把 8 FPS pressure profile 下的 CPU 峰值控制到约 98%，
  evidence lifecycle p95 约 192 秒；
- face-worker 注册图库 Qdrant authoritative cutover 通过，20,000 向量 benchmark all-search p95/p99
  为 4.037ms/6.427ms；
- 仍缺真实 RTSP 混合输入、长时间 soak、生产硬件 profile、face-worker 同步链路 ACK/匹配延迟验证和
  Savant 阶段级 latency。

## 如何使用这个知识库

- 想快速接手项目：读 [[09_AI_Agent_Onboarding|AI agent 快速上手]]。
- 想理解整体结构：读 [[01_System_Overview|系统总览]]、[[02_Runtime_Data_Flow|运行时数据流]]、
  [[03_Module_Map|模块地图]]。
- 想改代码：先读 [[11_Data_Contracts_And_Storage|数据契约与存储]]、
  [[12_Service_Deep_Dive|服务深潜]] 和 [[17_Testing_And_Change_Guide|测试与变更指南]]。
- 想跑压测或判断是否生产可用：读 [[14_Performance_And_Acceptance_Playbook|性能与验收手册]]。
- 想排查线上问题：读 [[16_Troubleshooting_Playbook|排障手册]]。

## 事实源

- 代码入口：`scripts/midterm_start.sh`
- Compose：`infra/docker-compose.midterm.yml`
- Env：`infra/env/midterm.env`
- Savant module：`modules/savant_security/module.yml`
- 操作台：`http://127.0.0.1:8090/operator`
- 主技术报告：`docs/midterm_current_program_technical_analysis_2026-06-28.md`
- 后推理性能计划：`specs/26_midterm_post_inference_bottleneck_closure_plan.md`
- media finalizer 报告：`docs/midterm_media_finalizer_pacer_8fps_report_2026-06-29.md`
