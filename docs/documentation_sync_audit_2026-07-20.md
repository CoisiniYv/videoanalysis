# 文档与当前代码同步审计

日期：2026-07-20

审计对象：当前工作区分支 `feat/roi-adaface-redis-20260711`，HEAD `1eb4174`，以及
当前未提交实现。

## 结论

仓库含有较完整的说明文档：审计前共有 332 个已跟踪的 Markdown/RST/TXT 文档类
文件，其中 281 个位于 `docs/`，163 个位于 archive 路径。问题不是“没有文档”，
而是文档数量多、历史报告与当前说明并存，核心入口没有持续随代码收敛。

审计前不能认为架构文档为最新。根 README、部署说明、知识库和 API 清单的最后主要
更新集中在 2026-06-20 至 2026-07-12，而当前服务、部署和 worker 生命周期已经在
7 月中旬继续演进。

## 发现与处理

| 级别 | 审计前问题 | 当前代码事实 | 本次处理 |
| --- | --- | --- | --- |
| P0 | 把 `clip-worker -> Replay job -> video-file-sink` 写成唯一主证据链 | 8090 完整预设使用 rolling-cache-first，record request 被抑制，Replay fallback 关闭 | 新增当前架构并改写入口、数据流和 evidence 文档 |
| P0 | 未说明 8090 完整预设、批量选源与后台启动 | 有 `production_t4_40`、`local_4090_60`、apply-async/status 和 5 段进度 | 更新部署、控制面、操作指南和 API 清单 |
| P0 | 模块图缺少独立轨迹 worker 和自有 rolling sink | `person-observation-worker` 与 `services/rolling-cache-sink/` 已存在 | 更新 Compose/模块/服务地图 |
| P1 | evidence 状态仍是旧 pending/replaying/finalizing 描述 | materialization v2、Scheduler V2、lease/fence/handoff 已由迁移 029–031 固化 | 更新数据契约和 evidence 生命周期 |
| P1 | 没有迁移 032 和 durable cleanup 热路径说明 | 新 migration 使用 concurrent indexes 支撑 cleanup/cooldown | 更新数据与部署注意事项 |
| P1 | 多处写“当前默认 Qdrant authoritative” | 当前 env 默认 `FACE_VECTOR_BACKEND=pgvector`，Qdrant 为可选 profile | 统一修正文档，保留历史 benchmark 的时间点含义 |
| P1 | 仍写 T4/4090 未验证或把旧 60 路结果当最新代码结论 | T4 40 路当前版本已验证；60 路曾通过，但双时间域改造后未同 revision 复跑 | 更新状态和风险边界 |
| P1 | 启动说明未包含双分支容器预创建、B 分支模型缓存和快速盘目录 | `midterm_start.sh` 已负责准备并 `--no-start` 预创建 | 更新部署和目录文档 |
| P2 | Runtime API 清单缺 latency 与 async apply | 当前 router 已提供三个新 GET/POST 接口 | 更新前端 API 清单 |
| P2 | QUICKSTART 仍要求逐路启用、写 8090 `/docs` | 推荐在完整链路选择器批量启用；8090 不代理 FastAPI `/docs` | 更新快速入门 |

## 当前文档权威入口

1. `docs/current_architecture.md`：当前工作区架构；
2. `docs/current_mainline_status.md`：实现、验证和开放项；
3. `docs/midterm_deployment.md`：部署与启动；
4. `docs/midterm_web_operator_guide.md`：操作员页面；
5. `docs/midterm_knowledge_base/00_Index.md`：专题知识库；
6. `docs/frontend_interface/README.md`：前端与 API 合同；
7. `docs/code_review/*.md`：带 revision/参数的时间点证据；
8. archive：仅追溯。

## 审计边界

- 本次只修改文档，没有修改程序、测试、Compose、脚本或迁移；
- 审计以静态代码、Git 差异、已有测试/压测报告交叉核对；当前环境没有 Docker
  命令，因此没有重新渲染 Compose 或启动运行态；
- 当前工作区本身包含大量未提交实现。本次文档描述的是该工作区，不代表这些变更已经
  提交、发布或部署到所有机器；
- 历史报告没有重写其当时结论，而是通过权威入口和适用范围防止其覆盖当前事实。

## 尚未由文档修复的实现风险

以下是代码/运维问题，不在“只改文档”的授权范围内：

- `scripts/midterm_health.sh` 的固定服务清单仍包含 legacy `source-adapter`，但未列出
  新的 `person-observation-worker`；健康检查结果需要结合 8090 runtime overview；
- topology apply 的执行线程不跨 API 进程存活；
- migration 032 是未提交文件且要求 autocommit/concurrent apply；
- 当前工作区需要在提交前把代码、迁移、测试和本次文档作为同一一致性单元复核。
