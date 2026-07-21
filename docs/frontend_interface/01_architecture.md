# 前端运行与模块架构

更新时间：2026-07-20

## 1. 部署拓扑

```text
Browser :8090
  -> evidence-viewer
       |-- /, /operator -> static/index.html
       |-- /static/*    -> static assets
       |-- /api/v1/*    -> api:8000/api/v1/*
       |-- /media/*     -> api:8000/media/*
       `-- /api/bundles/* -> local compatibility path

api:8000
  -> FastAPI routers/services/repositories
  -> PostgreSQL/Redis/Docker Engine
  -> /media StaticFiles
```

API 只在 Compose 网络 expose 8000。代理允许的 `/api/v1` 一级前缀为 cameras、
algorithms、evidence、events、people、maintenance、runtime、ws。普通 HTTP proxy 不会
把 WebSocket 自动升级。

API/media 的阻塞上游请求当前通过 worker thread 执行，避免占住 8090 asyncio event
loop；超时/连接失败分别返回 504/502 envelope。

## 2. 静态模块

| 文件 | 职责 |
| --- | --- |
| `index.html` | 六个 view、三级导航、全部 DOM anchor |
| `operator.js` | 公共 request/navigation、camera/people/runtime |
| `trajectory.js` | 人员轨迹分页与图片详情 |
| `evidence.js` | DB-backed evidence、video/image、overlay |
| `maintenance.js` | summary、preview、execute 删除 |
| `style.css` | 全局主题、布局、组件与响应式 |

固定加载顺序：`operator -> trajectory -> evidence -> maintenance`。当前不是 ES module，
跨文件只通过受控 `window.operatorXxx` 接口共享。

## 3. 导航与 view

一级 UI：配置、证据、运维控制。内部 view/hash：

| Hash | View | 模块 |
| --- | --- | --- |
| `#cameras` | 摄像头、ROI、规则 | operator |
| `#people` | 人员、人脸注册/图库 | operator |
| `#trajectory` | 人员轨迹 | trajectory |
| `#evidence` | 告警证据 | evidence |
| `#runtime` | 启动、状态、延迟、性能/拓扑 | operator |
| `#maintenance` | 存储维护 | maintenance |

离开 evidence 时暂停 overlay animation；当前 view 保存在 localStorage。

## 4. Runtime 前端架构

`loadRuntimeOverview()` 并行加载：

```text
/runtime/overview
/runtime/latency
/runtime/control
/runtime/performance-config
/runtime/topology-config
/runtime/topology-config/apply-status
```

Runtime quick start：

```text
server profile presets + cameras
  -> source selection
  -> balanced/manual assignment
  -> PUT topology-config
  -> POST apply-async {source_ids, disable_unselected}
  -> poll apply-status
  -> refresh overview/latency/topology
```

前端只允许精确选择 preset 需要的 40/60 路。任务 running 时禁止重复提交。进度分成
preflight、branches、sources、rolling、evidence；rolling prefill 显示剩余秒数。

状态文件可让刷新后的页面恢复同一 job 展示，但 API restart 使后台线程中断，页面应
展示 failed，而不是假装继续。

Runtime latency 页面每 5 秒静默刷新当前 view，并可手动刷新。

## 5. 公共接口

### `request(path, options)`

- JSON 默认 `Content-Type: application/json`；FormData 由浏览器设置 boundary；
- 非 2xx 或 `body.error` 抛 Error；
- 保留 `error.status` 和 `error.details`；
- 兼容 FastAPI `detail[]`；
- 二进制媒体/JPEG 不使用该 helper。

### 跨模块 window API

```text
window.operatorPeople
window.operatorTrajectory
window.operatorEvidence
window.operatorMaintenance
```

新增模块不要直接读取别的文件私有 state。

## 6. Evidence 与轨迹

```text
GET evidence/health
  -> GET evidence or evidence/bundles
  -> detail
  -> image URL, or annotations + sink metadata + raw clip URL
  -> currentTime/frame lookup -> canvas overlay
```

列表/详情、timeline/overlay 以 DB 为主。失败 reason 只在面向用户的安全提示中展示，
不能直接泄露内部路径/异常文本。

轨迹使用 persisted endpoint、limit/offset/has_more，不在每次翻页触发全量向量搜索。

## 7. 安全维护

所有删除：summary -> preview -> 展示候选/跳过/bytes/expiry -> reason + token/hash ->
execute -> 刷新来源页面。不得增加绕过 preview 的快捷入口。

## 8. 开发边界

- UI 样式变更不要顺手改变 frame UUID/PTS matching；
- runtime preset 数字来自后端，不在 HTML/JS 复制第二份权威值；
- async apply 错误必须保留 details；
- 资源 query version 与静态测试同步；
- 修改页面/API 后更新本目录、操作员指南和相应 harness 测试。
