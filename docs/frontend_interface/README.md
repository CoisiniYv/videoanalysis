# 前端接口与交接文档

更新时间：2026-07-12

代码基线：`04c5931`

适用范围：当前 midterm 8090 Operator Portal，包括摄像头、人员、人脸轨迹、证据、
运行时和存储维护页面，以及浏览器实际使用的 FastAPI 接口。

## 1. 文档原则

本目录是从当前代码提取的接口快照，不以旧设计文档代替实现。发生差异时按以下顺序
判断：

1. FastAPI router 和 schema；
2. repository/service 的实际返回字段；
3. `index.html` 和四个前端 JavaScript 模块的实际消费方式；
4. `harness/tests/` 中的当前合同测试；
5. 本目录文档。

修改接口或页面后，应在同一个提交中更新本目录。开始开发前先运行：

```bash
git rev-parse --short HEAD
git status --short
```

如果 HEAD 已不是 `04c5931`，先对照代码确认本目录是否需要刷新。

## 2. 文档导航

| 文件 | 内容 |
| --- | --- |
| [01_architecture.md](01_architecture.md) | 浏览器、8090 proxy、内部 API、媒体和前端模块边界 |
| [02_api_inventory.md](02_api_inventory.md) | Operator 可访问的 HTTP/WebSocket 接口清单及当前消费者 |
| [03_data_contracts.md](03_data_contracts.md) | 响应 envelope、摄像头、人员、轨迹、证据和维护数据合同 |
| [04_views_and_modules.md](04_views_and_modules.md) | 六个页面、DOM、状态、跨模块调用和交互流程 |
| [05_development_and_validation.md](05_development_and_validation.md) | 开发步骤、测试矩阵、重启方式、缓存和交接边界 |

## 3. 一分钟理解当前前端

```text
Browser :8090
  -> evidence-viewer FastAPI
       -> /static/*                 静态 HTML/CSS/JavaScript
       -> /api/v1/*                 反向代理到 api:8000/api/v1/*
       -> /media/*                  反向代理到 api:8000/media/*
       -> /api/bundles/*            旧的文件型 evidence-viewer 接口

Browser scripts, fixed load order:
  operator.js -> trajectory.js -> evidence.js -> maintenance.js
```

当前页面是无构建步骤的 vanilla JavaScript：

- 没有 React/Vue；
- 没有 TypeScript；
- 没有 npm bundler；
- `index.html` 提供固定 DOM；
- `style.css` 是全局样式；
- JavaScript 通过经典 `<script>` 顺序共享全局函数；
- CSS/JS 更新依靠 `?v=...` 查询参数手工刷新浏览器缓存。

## 4. 当前入口与权威路径

浏览器入口：

```text
http://<host>:8090/
http://<host>:8090/operator
```

当前前端使用的接口根路径：

```text
/api/v1
```

重要边界：

- 浏览器只访问 8090 same-origin 路径；
- `api:8000` 是 compose 内部地址，不是同事开发 UI 时应硬编码的浏览器地址；
- 新的证据页面必须使用 `/api/v1/evidence/*` 数据库索引；
- `/api/bundles/*` 是 evidence-viewer 中仍保留的文件扫描/兼容接口，不是新功能的
  首选数据源；
- 媒体 URL 使用 API 返回的 `/media/...`，不要在浏览器拼宿主机文件路径；
- 当前代码没有统一前端鉴权或 RBAC，不能把“接口可访问”理解为已有权限保护。

## 5. 代码入口

| 代码 | 职责 |
| --- | --- |
| `services/evidence-viewer/app/static/index.html` | 六个主页面及全部 DOM 锚点 |
| `services/evidence-viewer/app/static/style.css` | 主题、三栏布局、响应式和组件样式 |
| `services/evidence-viewer/app/static/operator.js` | 公共请求、导航、摄像头、人员和运行时 |
| `services/evidence-viewer/app/static/trajectory.js` | 独立人脸轨迹页面 |
| `services/evidence-viewer/app/static/evidence.js` | DB-backed 证据列表、播放和 overlay |
| `services/evidence-viewer/app/static/maintenance.js` | 存储统计与 preview/execute 删除流程 |
| `services/evidence-viewer/app/main.py` | 8090 静态服务、API/media proxy、旧文件接口 |
| `services/api/app/main.py` | 内部 FastAPI router 注册与 `/media` mount |
| `services/api/app/routers/` | `/api/v1/*` HTTP/WebSocket 接口 |
| `services/api/app/schemas/` | 请求和核心响应字段 |
| `services/api/app/repositories/` | PostgreSQL 查询和实际数据来源 |

## 6. 开发者先记住的五条规则

1. 页面显示摄像头时优先使用 `camera_name` 或摄像头查询得到的 `name`，不要把内部
   `source_id` / `camera_id` 当成默认用户标签。
2. 所有 JSON API 正常返回都优先按 `{data, error, request_id}` envelope 解析。
3. 证据只有在 API 给出可播放媒体 URL 时才播放；进行中或失败状态不能通过前端猜测
   文件路径绕过。
4. 轨迹页面使用 persisted trajectory，不执行全量向量搜索；分页依赖 `has_more`。
5. 删除必须经过 preview，再携带 `preview_id`、`confirm_token`、`candidate_hash` 和
   `reason` execute；前端不能提供直接删除捷径。

## 7. 本目录不覆盖的内容

- Worker 内部调度、lease、WIP 和 materialization 实现；
- Savant 模型链和算法推理代码；
- 新机器安装与完整部署操作；
- 历史 archive 页面；
- 尚未被当前页面使用的未来产品设计。

Worker 优化可以改变内部实现，但不得无协调地改变本目录记录的浏览器接口、状态字段、
媒体 URL 和证据 identity 合同。
