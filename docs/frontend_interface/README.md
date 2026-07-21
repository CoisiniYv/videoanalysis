# 前端接口与交接文档

更新时间：2026-07-20

代码基线：当前分支 `feat/roi-adaface-redis-20260711`、HEAD `1eb4174` 及当前未提交
工作区。若 HEAD/工作区变化，先核对代码再沿用本文。

## 文档导航

| 文件 | 内容 |
| --- | --- |
| [01_architecture.md](01_architecture.md) | 8090 proxy、页面模块和完整链路控制 |
| [02_api_inventory.md](02_api_inventory.md) | 浏览器可访问的 API 清单 |
| [03_data_contracts.md](03_data_contracts.md) | 摄像头、人员、轨迹、evidence 与维护合同 |
| [04_views_and_modules.md](04_views_and_modules.md) | 页面/DOM/跨模块交互 |
| [05_development_and_validation.md](05_development_and_validation.md) | 开发与验证 |

仓库级架构先读 `docs/current_architecture.md`。

## 当前前端形态

```text
Browser :8090
  -> evidence-viewer
       -> /static/*       vanilla HTML/CSS/JS
       -> /api/v1/*       api:8000 proxy
       -> /media/*        API media proxy
       -> /api/bundles/*  legacy evidence compatibility
```

脚本固定顺序：

```text
operator.js -> trajectory.js -> evidence.js -> maintenance.js
```

无 React/Vue/TypeScript/bundler。`index.html` 是 DOM 合同，`style.css` 为全局作用域，
资源 query version 需要随改动刷新。

## 页面结构

一级导航：

- 配置：摄像头、人员与人脸库；
- 证据：告警证据、人员轨迹；
- 运维控制：启动与状态、高级维护。

内部 hash 仍为 `#cameras/#people/#trajectory/#evidence/#runtime/#maintenance`。

Runtime 页面当前包含：

- 实时延迟；
- 完整链路快速启动、摄像头选择和后台进度；
- 建议下一步；
- 高级 performance/topology；
- 摄像头性能、forwarder、evidence 和 container 状态。

## 重要边界

1. 浏览器只使用 8090 same-origin；
2. 8090 不代理 FastAPI `/docs`；
3. 新 evidence 使用 `/api/v1/evidence`，不依赖 `/api/bundles`；
4. 媒体 URL 使用 API 返回的 `/media/...`；
5. topology apply-async 刷新页面可继续显示，但 API 重启会中断执行；
6. 运行预设由服务端返回，前端不能复制另一套常量；
7. 完整启动时按 source_ids 批量启用，不能逐路触发当前单分支；
8. 当前没有统一鉴权/RBAC；
9. `/api/v1/ws/alerts` 后端存在，但 8090 未做 WebSocket upgrade proxy。

## 事实优先级

1. router/service/repository；
2. `index.html` 与 JavaScript 消费方式；
3. harness 合同测试；
4. 本目录文档。

接口或页面变化必须在同一提交更新本目录和
`docs/midterm_web_operator_guide.md`。
