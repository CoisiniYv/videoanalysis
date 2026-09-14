# Video Analytics 技术参考手册

本目录汇总 Video Analytics Platform 的系统结构、运行数据流、控制面、证据链、数据契约、性能调优与排障资料。它适合需要深入理解服务职责和运行机制的开发者、部署人员与维护人员。

如果你第一次接触项目，建议先阅读仓库根目录 [`README.md`](../../README.md) 和 [`../README.md`](../README.md)。

## 推荐阅读顺序

| 文档 | 说明 |
| --- | --- |
| [00_Index.md](00_Index.md) | 技术参考总索引 |
| [01_System_Overview.md](01_System_Overview.md) | 系统能力与组件概览 |
| [02_Runtime_Data_Flow.md](02_Runtime_Data_Flow.md) | 视频、事件、人脸与证据的数据流 |
| [03_Module_Map.md](03_Module_Map.md) | 模块与服务职责 |
| [04_Control_Plane_8090.md](04_Control_Plane_8090.md) | 8090 控制面 |
| [05_Evidence_Chain.md](05_Evidence_Chain.md) | Evidence 生命周期与媒体生成 |
| [10_Glossary.md](10_Glossary.md) | 术语表 |
| [11_Data_Contracts_And_Storage.md](11_Data_Contracts_And_Storage.md) | PostgreSQL、Redis 与媒体存储契约 |
| [12_Service_Deep_Dive.md](12_Service_Deep_Dive.md) | 核心服务深入说明 |
| [13_Runtime_Control_Runbook.md](13_Runtime_Control_Runbook.md) | 运行控制与操作流程 |
| [14_Performance_And_Acceptance_Playbook.md](14_Performance_And_Acceptance_Playbook.md) | 性能测试与验收方法 |
| [15_Design_Invariants_And_Decisions.md](15_Design_Invariants_And_Decisions.md) | 关键设计约束 |
| [16_Troubleshooting_Playbook.md](16_Troubleshooting_Playbook.md) | 排障方法 |
| [17_Testing_And_Change_Guide.md](17_Testing_And_Change_Guide.md) | 测试与变更检查 |
| [18_Artifact_And_Directory_Map.md](18_Artifact_And_Directory_Map.md) | 运行产物和目录说明 |

## 系统事实源

为了避免文档与运行状态混淆，项目采用以下事实源约定：

- **PostgreSQL**：摄像头、规则、人员、人脸库、事件和 evidence metadata 的持久化事实源；
- **Redis**：异步消息、stream 和运行时任务传递；
- **Runtime YAML / JSON**：由持久化配置生成或用于表达当前运行快照；
- **源码与数据库迁移**：接口、生命周期和 schema 的最终技术定义；
- **自动化测试**：关键合同与回归行为的可执行验证。

完整双分支运行时以 rolling cache 作为主要 evidence 媒体来源；Replay job 路径保留用于兼容或特定运行模式。默认人脸向量后端使用 pgvector，Qdrant 可通过对应部署 profile 使用。

## 工程历史资料

本目录中部分文档记录性能优化、迁移和历史设计背景，例如：

- `06_Performance_Optimization_History.md`；
- `07_Deployment_Migration.md`；
- `08_Open_Risks_And_Next_Actions.md`。

这些内容用于解释系统演进，不应替代当前架构、部署配置、接口代码或运行预设。带日期的压测结果也只代表对应硬件、输入和代码版本下的测量结果。

仓库中可能还保留面向特定开发流程的历史 onboarding/协作材料；它们不属于运行或部署所需文档，也不作为本技术手册的主要入口。

## 文档维护原则

- 架构、数据契约或运行方式发生变化时，同步更新对应稳定文档；
- 性能数据必须注明测试环境、输入规模和运行参数；
- 历史诊断结论保留上下文，但不覆盖当前代码与配置；
- 对外说明避免依赖临时分支、某个工作区状态或未提交修改；
- 涉及 schema、Redis stream、worker 输入输出或 evidence 生命周期的变化，应同步更新数据契约和相关测试。
