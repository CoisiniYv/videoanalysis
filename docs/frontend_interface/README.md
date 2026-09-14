# 前端与 API 集成

本目录描述 Video Analytics Platform 的 Web 操作台、浏览器侧 API 使用方式以及主要数据契约。面向需要修改 8090 操作界面、接入业务 API 或理解前后端边界的开发者。

项目总体架构请先阅读 [`../current_architecture.md`](../current_architecture.md)。

## 前端架构

```text
Browser :8090
  |
  v
Evidence Viewer / Operator
  +-- /static/*       HTML / CSS / JavaScript
  +-- /api/v1/*       -> FastAPI service
  +-- /media/*        -> evidence media proxy
  +-- /api/bundles/*  legacy compatibility path
```

浏览器通过 8090 使用 same-origin API 和媒体路径。FastAPI 服务本身运行在内部 Compose 网络中，不要求浏览器直接访问其容器端口。

## 前端技术栈

当前操作台采用轻量的原生前端实现：

- HTML；
- CSS；
- Vanilla JavaScript；
- 无 React / Vue / TypeScript；
- 无独立前端 bundler。

主要脚本按以下顺序加载：

```text
operator.js -> trajectory.js -> evidence.js -> maintenance.js
```

`index.html` 定义页面 DOM 结构，`style.css` 使用全局样式。修改静态资源时应同步检查页面引用和资源版本参数。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [01_architecture.md](01_architecture.md) | 8090 proxy、页面模块与运行控制架构 |
| [02_api_inventory.md](02_api_inventory.md) | 浏览器侧 API 清单 |
| [03_data_contracts.md](03_data_contracts.md) | 摄像头、人员、轨迹与 evidence 数据契约 |
| [04_views_and_modules.md](04_views_and_modules.md) | 页面、DOM 和模块交互 |
| [05_development_and_validation.md](05_development_and_validation.md) | 开发与验证方式 |

## 页面模块

操作台主要包含以下业务区域：

- **配置**：摄像头、ROI、算法和规则；
- **人员与人脸**：人员资料与人脸图库；
- **证据**：事件证据、视频、快照和标注；
- **人员轨迹**：按人员查看跨时间观测记录；
- **启动与运行**：运行预设、摄像头选择、source/FPS/延迟与任务状态；
- **高级维护**：拓扑、性能配置、存储和诊断。

页面内部使用 hash 进行模块切换，包括：

```text
#cameras
#people
#trajectory
#evidence
#runtime
#maintenance
```

## API 集成约定

1. 浏览器业务请求优先使用 `/api/v1/*`；
2. Evidence 媒体地址使用 API 返回的 `/media/...` 路径；
3. 新 evidence 功能使用 `/api/v1/evidence`，`/api/bundles` 仅用于兼容旧数据；
4. 运行预设和运行时参数由服务端 API 提供，前端不维护第二套硬编码预设；
5. 批量启动完整链路时按 `source_ids` 提交选中的摄像头集合；
6. FastAPI 的 OpenAPI `/docs` 属于内部 API 服务，不由 8090 作为主要用户入口提供；
7. `/api/v1/ws/alerts` 如需在浏览器中使用，部署层必须同时具备 WebSocket upgrade 代理能力。

## 代码位置

前端静态页面和代理服务位于 `services/evidence-viewer/`，业务 API 位于 `services/api/`。涉及接口字段、运行时控制或 evidence 数据结构的修改，应同时检查：

```text
services/evidence-viewer/
services/api/
services/event-worker/
services/media-worker/
harness/tests/
```

API 契约和页面行为应由代码与自动化测试共同约束，本文档用于解释稳定的集成方式，而不是记录某个临时开发分支或工作区状态。

## 部署安全

8090 适合作为统一操作入口，但在非受信任网络中仍应置于受控网络或反向代理之后，并配置 TLS、访问控制和必要的审计策略。不要将内部 Redis、PostgreSQL、推理服务或调试端口直接暴露到公网。
