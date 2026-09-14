# Documentation

这里是 Video Analytics Platform 的文档入口。仓库包含稳定使用文档，也保留了大量设计、压测、诊断和迁移记录；如果你的目标是部署、使用或理解当前系统，请优先从本页列出的稳定文档开始。

## 使用者入口

| 文档 | 适合谁 | 内容 |
| --- | --- | --- |
| [`../README.md`](../README.md) | 所有人 | 项目介绍、架构与快速开始 |
| [`../README_MIDTERM.md`](../README_MIDTERM.md) | 部署/操作人员 | 启动、摄像头配置、运行预设与停止 |
| [`midterm_web_operator_guide.md`](midterm_web_operator_guide.md) | 操作人员 | 8090 Web 操作台详细使用方式 |
| [`midterm_quick_reference.md`](midterm_quick_reference.md) | 运维人员 | 常用命令与排障速查 |
| [`midterm_deployment.md`](midterm_deployment.md) | 部署人员 | Compose、存储、profile 与环境配置 |

## 架构与开发

| 文档 | 内容 |
| --- | --- |
| [`current_architecture.md`](current_architecture.md) | 当前系统组件、数据流和运行形态 |
| [`frontend_interface/README.md`](frontend_interface/README.md) | Web 前端、8090 proxy 与 API 集成 |
| [`midterm_knowledge_base/README.md`](midterm_knowledge_base/README.md) | 服务、证据链、数据契约和排障的技术参考 |
| [`compose_inventory.md`](compose_inventory.md) | Compose 文件和运行形态索引 |

## 文档分类

### 稳定文档

稳定文档描述当前项目的使用方式、架构和接口，应随着代码和配置变化持续维护。根 README、本页、部署文档、操作指南和接口文档属于这一类。

### 工程记录

仓库中还保留以下类型的资料：

- 文件名中带日期的压测、验证、迁移与诊断报告；
- `code_review/` 下的专项审查记录；
- `repair_goal/`、`runtime_stability_fix/`、`replay_evidence_fix/` 等历史修复过程资料；
- `archive/` 与各目录中的 `archive/phase-only/` 历史配置和阶段性产物。

这些文件保留了工程决策的上下文，适合追溯问题或研究历史实现，但**不应作为当前部署说明、API 合同或容量承诺**。

## 如何判断当前行为

当文档之间出现差异时，建议按以下顺序核对：

1. 当前 `main` 分支源码与数据库迁移；
2. 当前 Compose、环境变量和 runtime profile；
3. 自动化合同/回归测试；
4. `current_architecture.md`、部署与操作稳定文档；
5. 带日期的历史报告和设计记录。

对于性能数据，必须同时确认 GPU、视频分辨率、码率、分析 FPS、摄像头数量、事件负载和对应代码版本，不能直接把历史压测数字视为其他环境的性能保证。

## 常用运行入口

```bash
# Start
bash scripts/midterm_start.sh

# Health / diagnostics
bash scripts/midterm_health.sh
bash scripts/runtime/doctor_midterm.sh

# Stop
bash scripts/midterm_stop.sh
```

Web 操作台：

```text
http://127.0.0.1:8090/operator
```

生产或非受信任网络部署时，请通过受控网络或反向代理保护 8090，并避免直接暴露内部数据库、Redis、推理与调试端口。
