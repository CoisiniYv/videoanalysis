# 页面与前端模块合同

## 1. App Shell

文件：`static/index.html` + `static/operator.js`

App shell 固定包含：

- 顶部六个 tabs；
- API URL 展示；
- theme toggle；
- 全局 status/error/success；
- camera、enabled camera、people、gallery、evidence 汇总卡片。

导航规则：

```text
hash > localStorage last view > cameras
```

合法 hash：

```text
#cameras #people #trajectory #evidence #runtime #maintenance
```

缓存键：

| Key | 内容 |
| --- | --- |
| `operator-theme` | `light` / `dark` |
| `operator-active-view` | 当前主页面 |
| `operator-evidence-count` | shell 上次成功读取的证据数量 |
| `operator-evidence-view-state` | 证据 filters/category/page/selection |

## 2. 摄像头页面

DOM root：`camera-view`

模块：`operator.js`

### 2.1 区域

```text
camera-list
camera-detail
rules
  -> tab-zones
  -> tab-rules
```

### 2.2 核心状态

```text
cameras
selectedCameraId
algorithms
algorithmSupportMatrix
currentZones
currentRules
selectedRuntimeConfig
lastRuntimeApplyResult
roiState
```

### 2.3 加载顺序

```text
loadAlgorithms()
loadWatchlistTargetPeople()
GET /cameras
select first/current camera
GET /cameras/{id}/config
GET /cameras/{id}/algorithm-rules
GET /cameras/{id}/runtime-config
render camera/zones/rules/quick controls
```

### 2.4 保存语义

- 新增/编辑 camera 后，后端可能同步 source containers，并把结果放在
  `runtime_source_apply`；
- 保存 ROI 或规则后调用 `runtime/config/sync`，不默认全链路重启；
- “应用视频源”调用 `runtime/sources/apply`；
- “保存并应用”规则仍先持久化规则，再同步 runtime config；
- “受控重启运行时”是高影响显式动作，必须保留确认对话框和 backend guard 错误展示。

### 2.5 ROI editor

画面 URL：

```text
/api/v1/cameras/{camera_id}/preview.jpg?max_width=1280&timeout_ms=3000&quality=85
```

canvas 点位从显示尺寸换算回 source pixel coordinates。`X-Camera-Source-Width/Height`
或图片 natural size 决定原图维度。不要把 CSS display coordinates 直接保存为 ROI。

### 2.6 快捷算法控件

当前客户页快捷算法列表：

```text
behavior.intrusion
behavior.loitering
behavior.running
behavior.crowd_gathering
behavior.fall
behavior.chasing
face.watchlist
```

`behavior.wall_climb_suspicious`、`face.live_search` 当前不作为快捷卡片显示。规则是否可
保存还要结合 support matrix，不能只看前端模板是否存在。

## 3. 人员管理页面

DOM root：`people-view`

模块：`operator.js`

### 3.1 页面结构

```text
people-list
registration-detail
people-detail
```

### 3.2 核心状态

```text
people
selectedPersonId
selectedPerson
selectedPersonRequestId
pendingOpenPersonId
faceRegistrationMode
```

`selectedPersonRequestId` 防止快速切换人员时较慢旧请求覆盖当前页面。

### 3.3 注册模式

两种显式模式：

- `new`：清空 hidden `person_id`，按人员编号和姓名创建；
- `append`：要求已选人员，并提交其 `person_id`。

如果用户修改了已选择人员对应的 `external_person_id`，
`prepareFaceRegistrationFormData()` 会删除 `person_id`，防止把新编号误追加到旧人员。

### 3.4 图片选择

- 人员列表：registered crop 优先；
- gallery：`registered_crop_url` 优先，随后 `source_image_url`；
- 图片 `<img>` 直接使用 `/media/...` URL；
- UI 不读取服务器文件路径。

### 3.5 跳转轨迹

“查看轨迹”调用：

```js
window.operatorTrajectory.openForPerson(selectedPersonId)
```

不再打开 modal，也不在 people 页面内嵌旧轨迹列表。

## 4. 人脸轨迹页面

DOM root：`trajectory-view`

模块：`trajectory.js`

### 4.1 页面结构

```text
trajectory-filter-pane
trajectory-list-pane
trajectory-detail-pane
```

筛选：

- 系统人员 ID 或 external person id；
- camera id；
- datetime-local start/end；
- 固定 similarity 0.6；
- persisted hits；
- 每页 50 条。

### 4.2 初始化

`init()` 只绑定一次 events，并并行加载：

```text
GET /api/v1/people?limit=200
GET /api/v1/cameras
```

人员 datalist 同时接受 system ID 和 external ID。最终 API path 必须使用解析后的整数
`person_id`。

### 4.3 查询状态

```text
rows
personId
offset
hasMore
selectedIndex
requestId
```

`requestId` 防止旧分页请求覆盖新查询。查询期间禁用 search/refresh；previous/next 的
可用性分别由 `offset > 0` 和 `hasMore` 决定。

### 4.4 图片展示

列表使用 lazy thumbnail。详情在页面右栏 inline 显示，不使用 modal：

```text
annotated frame -> full frame -> face crop -> thumbnail
```

主图加载失败后最多回退一次 thumbnail，再显示错误空态。

## 5. 证据管理页面

DOM root：`evidence-view`

模块：`evidence.js`

### 5.1 页面结构

```text
evidence-filters
evidence-list-pane
evidence-detail-pane
  -> video + canvas
  -> imageEvidence
  -> overlay controls
  -> event/identity/diagnostic details
```

### 5.2 分类

```text
all
perimeter
behavior
crowd
identity
```

事件映射：

| Category | Event types |
| --- | --- |
| identity | `watchlist_hit`, `live_search_hit` |
| perimeter | `intrusion`, `wall_climb_suspicious` |
| behavior | `loitering`, `running`, `fall` |
| crowd | `crowd_gathering` |
| all | 其他 |

“all”向后端发送 `event_category=evidence`，客户端再排除 identity；identity 必须通过
单独分类进入。人员搜索有值且当前为 all 时，UI 自动切到 identity。

### 5.3 请求和 selection

列表：固定每页 50。选择 bundle 时递增 `selectionRequestId`，manifest/annotation/
timeline 的旧响应只有在 request id 和 event id 仍匹配时才可落入 state。

默认选择：当前页第一个 `raw_clip_available=true` 的 bundle，否则选择第一项以展示
生成中/失败状态。

### 5.4 Image/video 分支

```text
playback_kind=image
  -> 不请求 annotations/sink metadata
  -> inline render full/annotated/crop image

playback_kind!=image
  -> Promise.all(annotations, sink metadata)
  -> set video raw_clip_url
  -> build frame lookup
  -> requestAnimationFrame overlay loop
```

### 5.5 Overlay 边界

`evidence.js` 的 overlay 不是普通装饰组件，而是视觉证据对齐逻辑。修改时必须保留：

- source video dimensions；
- frame UUID/PTS/timeline lookup；
- per-role display window；
- `displayable` filter；
- bbox normalization；
- matched/unknown/person/behavior role；
- stale request/animation cancellation；
- hidden image evidence 时停止无意义 canvas 绘制。

若目标只是改布局或颜色，不要同时修改 frame matching、hold window 或 object visibility。

### 5.6 从证据跳转轨迹

identity bundle 只有存在 `person_id` 时才显示/执行“查看此人轨迹”。调用
`operatorTrajectory.openForPerson(person_id)`，而不是使用姓名或 external id 作为 URL
path。

## 6. Runtime 页面

DOM root：`runtime-view`

模块：`operator.js`

### 6.1 页面结构

```text
runtime-overview-pane
runtime-performance-pane
runtime-topology-pane
runtime-source-pane
runtime-forwarder-pane
runtime-evidence-pane
runtime-container-pane
```

### 6.2 数据并发加载

`loadRuntimeOverview()` 并行请求：

```text
/runtime/overview
/runtime/control
/runtime/performance-config
/runtime/topology-config
```

任一配置子请求失败时应展示具体错误，不得把整个 overview 的旧值清空后伪装健康。

### 6.3 Save 和 Apply

Performance/Topology 都是：

```text
PUT save
  -> optional confirm
  -> POST apply
  -> refresh overview
```

如果用户取消 apply，保存仍已发生，页面必须显示“已保存，未应用”。

Runtime guard 返回 active evidence details 时，`apiErrorMessage()` 会翻译为“仍有 N 个
证据任务在生成中”。新实现不得吞掉 `error.details`。

## 7. 存储维护页面

DOM root：`maintenance-view`

模块：`maintenance.js`

### 7.1 状态

```text
preview
facePreview
facePreviewKind
activeDelete
executeEnabled
executeControlEnabled
```

### 7.2 删除入口

删除可以从三个页面进入：

- evidence detail -> `kind=evidence`；
- people detail -> `kind=person`；
- gallery item -> `kind=gallery`。

均通过 `openMaintenanceWithRequest()` / `operatorMaintenance.openDeleteDialog()` 进入同一
preview UI。

### 7.3 必须保留的安全交互

1. 获取 summary/execution control；
2. preview；
3. 展示候选、跳过、预计 bytes、过期时间；
4. 要求 reason；
5. execute；
6. refresh originating page。

`allow_stale_pending_tasks` 只通过显式“重新预览待处理任务”路径启用，不能默认 true。

## 8. CSS 与 DOM 命名

新页面/组件建议使用前缀：

```text
camera-
people-
trajectory-
evidence-
runtime-
maintenance-
```

需要跨模块的元素 ID 必须同时更新：

1. `index.html`；
2. JavaScript DOM lookup；
3. CSS selector；
4. 对应 static contract test。

不要删除 `hidden`、`internal-debug` 或 `internal-config` 仅为了让开发信息可见；应使用
浏览器 devtools 或单独 debug mode。

## 9. 模块扩展建议

当前代码允许继续做小型 vanilla JS 修改，但新增完整页面时应：

1. 新建独立 `xxx.js`；
2. 暴露最小 `window.operatorXxx`；
3. 由 `operator.js::activateTopView()` 管理 init/pause；
4. 不直接写另一个模块的 state；
5. 使用公共 `request()`；
6. 使用 request sequence id 防止 stale response；
7. 在 `index.html` 中保持明确 script load order；
8. 增加 static/API contract tests。

如果要引入 framework/bundler，必须另立迁移计划；不能让一部分页面依赖 build、另一部分
继续直接加载源码而没有统一发布和缓存策略。
