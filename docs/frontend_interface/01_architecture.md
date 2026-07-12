# 前端运行与模块架构

## 1. 部署拓扑

当前 8090 页面由 `evidence-viewer` 服务提供。浏览器不直接连接内部 API：

```text
Browser
  |
  | HTTP :8090
  v
evidence-viewer
  |-- GET /, /operator ----------> static/index.html
  |-- GET /static/* -------------> static assets
  |-- /api/v1/{path} ------------> http://api:8000/api/v1/{path}
  |-- /media/{path} -------------> http://api:8000/media/{path}
  `-- /api/bundles/* ------------> local read-only evidence compatibility path

api:8000
  |-- FastAPI routers
  |-- PostgreSQL repositories
  |-- /media StaticFiles
  `-- runtime/maintenance services
```

代码依据：

- `services/evidence-viewer/app/main.py`
- `services/evidence-viewer/app/config.py`
- `services/api/app/main.py`
- `infra/docker-compose.midterm.yml`

compose 中只有 `8090:8090` 是 Operator 浏览器入口；API 使用 `expose: 8000`，由
evidence-viewer 通过 compose 网络代理。

## 2. 8090 Proxy 合同

`evidence-viewer` 只代理以下 `/api/v1` 一级前缀：

```text
cameras
algorithms
evidence
events
people
maintenance
runtime
ws
```

其他一级路径返回 404 `operator api route not proxied`。

HTTP 方法允许：

```text
GET POST PUT PATCH DELETE OPTIONS
```

媒体代理只允许：

```text
GET HEAD OPTIONS
```

代理会移除 hop-by-hop headers 和 `content-length`，保留其余请求/响应 headers；默认
proxy timeout 来自 `OPERATOR_PROXY_TIMEOUT_SECONDS`，代码默认 900 秒。

上游连接失败返回：

```json
{
  "data": null,
  "error": {
    "message": "operator api unavailable: ...",
    "code": 502
  },
  "request_id": null
}
```

timeout 使用 504。

## 3. 静态资源和加载顺序

`index.html` 固定按以下顺序加载经典脚本：

```text
1. operator.js
2. trajectory.js
3. evidence.js
4. maintenance.js
```

顺序是运行合同，不可随意交换：

- `operator.js` 定义 `request()`、`showError()`、`setStatus()`、
  `activateTopView()`、`openMaintenanceWithRequest()`；
- `trajectory.js` 使用这些公共函数并导出 `window.operatorTrajectory`；
- `evidence.js` 可调用 `window.operatorTrajectory.openForPerson()`，并导出
  `window.operatorEvidence`；
- `maintenance.js` 使用公共 `request()`，执行后可调用 `operatorEvidence.reload()` 或
  `loadPeople()`，并导出 `window.operatorMaintenance`。

当前不是 ES module，顶层名字共享在同一页面 global environment 中。新增顶层变量时
必须避免重名；如果继续扩展页面，优先采用 `window.operatorXxx` 小型公共接口，而不是
让模块直接读写另一个文件的内部 state。

## 4. 文件职责与复杂度

当前代码行数快照：

| 文件 | 行数 | 主要职责 |
| --- | ---: | --- |
| `operator.js` | 3,701 | 公共 shell、摄像头、人员、规则、ROI、运行时 |
| `evidence.js` | 2,047 | 证据检索、播放、帧对齐和 canvas overlay |
| `maintenance.js` | 663 | 存储统计和安全删除工作流 |
| `trajectory.js` | 468 | persisted 人脸轨迹独立页面 |
| `index.html` | 838 | DOM 与表单合同 |
| `style.css` | 2,207 | 全局主题、布局、组件和响应式 |
| `app/main.py` | 651 | proxy 和 evidence compatibility endpoints |
| `app/evidence_index.py` | 910 | 文件型 evidence 索引/标注兼容逻辑 |

复杂度热点：

1. `operator.js` 是最大的共享状态和 DOM 模块；修改公共请求、导航或选中对象逻辑时，
   必须跑所有 Operator 静态测试。
2. `evidence.js` 包含视频时间轴、frame UUID/PTS 匹配和 overlay 生命周期；UI 整理不应
   顺手改变对齐算法。
3. `style.css` 是全局作用域；新 class 应使用 view 前缀，避免影响其他页面。
4. `evidence-viewer/app/main.py` 同时保留 proxy 和旧文件型接口，修改路由顺序时要防止
   catch-all route 捕获具体 endpoint。

## 5. 六个主页面

`operator.js` 的 `TOP_VIEWS` 是主页面白名单：

| Hash | DOM root | 负责模块 | 进入页面时的动作 |
| --- | --- | --- | --- |
| `#cameras` | `camera-view` | `operator.js` | 初始加载摄像头、算法、规则和配置 |
| `#people` | `people-view` | `operator.js` | `loadPeople()` |
| `#trajectory` | `trajectory-view` | `trajectory.js` | `operatorTrajectory.init()` |
| `#evidence` | `evidence-view` | `evidence.js` | `operatorEvidence.init()` 并启动 overlay loop |
| `#runtime` | `runtime-view` | `operator.js` | `loadRuntimeOverview()` |
| `#maintenance` | `maintenance-view` | `maintenance.js` | `operatorMaintenance.init()` |

离开 evidence 页面时调用 `operatorEvidence.pause()`，停止 animation frame loop。

页面 hash 通过 `history.replaceState()` 更新，不触发传统页面跳转。最后访问页面保存在
`localStorage["operator-active-view"]`。

## 6. 公共前端接口

### 6.1 `request(path, options)`

定义在 `operator.js`。行为：

- `FormData` 不设置 `Content-Type`，由浏览器生成 multipart boundary；
- 其他 body 默认 `Content-Type: application/json`；
- 响应必须是 JSON；
- HTTP 非 2xx 或 `body.error` 均抛出 `Error`；
- FastAPI validation `detail[]` 会被拼成可读消息；
- `error.status` 保存 HTTP status；
- `error.details` 保存 backend error details；
- 成功返回 `body.data ?? body`。

二进制 JPEG 和媒体不能用此函数，应直接使用 URL/fetch/image/video。

### 6.2 `window.operatorPeople`

```text
openPersonById(personId, options)
```

打开人员详情；`options.openTrajectory=true` 时继续进入轨迹页。

### 6.3 `window.operatorTrajectory`

```text
init()
openForPerson(personId, options)
reload()
```

`options` 当前识别 `startTime`、`endTime`、`cameraId`。

### 6.4 `window.operatorEvidence`

```text
init()
pause()
reload()
```

### 6.5 `window.operatorMaintenance`

```text
init()
previewEvidenceDelete()
openDeleteDialog(request)
prepareDelete(request)       # openDeleteDialog alias
loadMaintenanceSummary()
```

## 7. 关键数据流

### 7.1 摄像头配置

```text
GET cameras/config
  -> edit camera / ROI / algorithm rule
  -> POST/PUT API
  -> POST cameras/runtime/config/sync
  -> refresh generated runtime config
```

保存普通规则和 ROI 默认只同步运行配置，不自动做整链路 restart。显式“应用视频源”或
“受控重启运行时”才调用对应 runtime endpoint。

### 7.2 人脸轨迹

```text
people list + cameras list
  -> person/camera/time filters
  -> GET people/{id}/trajectory?limit=50&offset=N
  -> inline list and image detail
  -> next page only when has_more=true
```

轨迹页面固定 `min_similarity=0.6`、`include_unregistered_sources=true`。API 的
`trajectory` endpoint 设置 `include_observation_search=False`，即只使用 persisted hit，
不在每次翻页时执行全量 gallery observation 向量搜索。

### 7.3 证据播放

```text
GET evidence/health
  -> GET evidence/bundles
  -> GET manifest
  -> image evidence: render returned image URL
  -> video evidence: GET annotations + sink-metadata + raw_clip_url
  -> video currentTime -> frame lookup -> canvas overlay
```

证据 list/detail 使用数据库索引。annotations 优先数据库 overlay rows；sink metadata
优先数据库 timeline rows。后端只在 DB rows 为空时提供受控 filesystem artifact fallback。

### 7.4 安全删除

```text
summary
  -> preview
  -> operator reviews candidate/skipped/bytes/expiry
  -> execute with preview token/hash/reason
  -> refresh evidence or people page
```

preview 与 execute 不可合并。

## 8. 主题和响应式

- 主题存在 `document.documentElement.dataset.theme`；
- localStorage key：`operator-theme`；
- CSS 断点：1260px、860px、540px；
- desktop 页面多为三栏 grid；
- tablet 会把 detail pane 移到下一行；
- mobile 变为单列。

`internal-debug` 和 `internal-config` 默认隐藏，是避免把内部 ID/诊断直接暴露给客户的
展示合同；不要为了调试永久移除这些 class。
