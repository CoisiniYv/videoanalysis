# 人脸轨迹与一键找人 UI 重构计划

日期：2026-07-08

## 背景

当前 8090 前端把 `watchlist_hit` / `live_search_hit` 放在证据查看语义里展示，容易让操作员以为人脸名单命中应当像入侵告警一样生成并播放一段证据视频。新的产品语义应当拆开：

- 入侵、行为、聚集等事件仍走证据页面，核心对象是可审计的视频或图片证据。
- 人脸名单命中和一键找人不再优先走证据视频，而是走人脸轨迹和实时找人结果。
- 人脸命中默认保存图片和结构化轨迹，不再把视频生成作为主路径。

## 当前基础

后端已经具备人脸轨迹查询雏形：

- `GET /api/v1/people/{person_id}/latest-location`
- `GET /api/v1/people/{person_id}/trajectory?limit=...`

轨迹查询已经合并两类来源：

- `face_observations`：从图库 embedding 反查该人员出现记录。
- `watchlist_hit` / `live_search_hit`：已产生的人脸命中事件。

前端也已有人员页：

- 人员库检索
- 人脸注册
- 当前人员详情
- 最近位置
- 最近轨迹
- `查找此人` 按钮

问题不是完全缺功能，而是信息架构和操作入口错位：证据页还在承担人脸命中浏览，一键找人也还像附属于人员详情的一次刷新动作。

## 目标语义

### 1. 证据页面

证据页面只承担“事件证据审查”：

- 默认展示入侵、行为、聚集等告警证据。
- 视频证据保留播放器、时间线、标注框。
- 图片证据可展示，但不把 `watchlist_hit` 解释成需要播放的视频证据。
- `watchlist_hit` / `live_search_hit` 如果仍保留在证据索引中，只作为审计记录或图片命中入口，默认不混在视频证据主列表里。

验收口径：

- 操作员打开证据页，不会把人脸名单命中理解为“缺视频的证据”。
- 人脸类条目显示为“人脸命中图片 / 轨迹记录”，并提供“查看此人轨迹”跳转。
- 默认筛选不再把 `watchlist_hit` 当作主证据类型展示。

### 2. 人脸轨迹页面

人员页升级为“人脸轨迹查询”主入口：

- 左侧：人员检索和人员库。
- 中间：注册、图库维护。
- 右侧：人员档案、最近出现位置、轨迹时间线。

轨迹时间线显示：

- 命中图片，优先 `annotated_frame_url`，其次 `full_frame_url`，最后 `face_crop_url`。
- 摄像头名称。
- 命中时间。
- 相似度。
- 来源：图库观察 / 名单命中 / 一键找人。
- 可选操作：打开摄像头、查看同一摄像头更多轨迹、跳到相关审计记录。

验收口径：

- 选择一个人员后，可以直接看到最近位置和历史轨迹。
- 人脸轨迹以图片为主，不要求生成视频。
- 相同人员在不同摄像头下的出现记录能按时间排序。

### 3. 一键找人窗口

一键找人应是独立的非证据窗口，不再塞在证据查看器里。

入口：

- 人员页右上角：“一键找人”。
- 选中人员后：“查找此人”打开找人窗口并自动带入人员。

窗口形态：

- 前端 modal 或独立 panel。
- 不使用证据详情播放器。
- 不创建 evidence bundle 作为默认副作用。

输入：

- 已登记人员。
- 后续可扩展为上传照片找人。
- 时间范围、摄像头范围、最低相似度。

输出：

- 最新适合摄像头。
- 最近命中图片列表。
- 摄像头、时间、相似度。
- 打开摄像头实时画面。
- 查看完整轨迹。

验收口径：

- 一键找人的结果是“搜索结果”，不是“证据列表”。
- 找人操作可以快速返回最新位置，不等待视频生成。
- 需要留存时，后续再提供显式“保存为证据/生成报告”按钮。

## API 设计

第一阶段尽量复用现有 API：

- `GET /api/v1/people/{person_id}/latest-location`
- `GET /api/v1/people/{person_id}/trajectory`

建议补齐查询参数：

- `camera_id`
- `source_id`
- `start_ts_ms`
- `end_ts_ms`
- `min_similarity`
- `limit`
- `offset`

建议新增一键找人语义 API：

```http
GET /api/v1/people/{person_id}/find
```

返回结构：

```json
{
  "person": {},
  "latest_location": {},
  "results": [],
  "query": {
    "camera_id": null,
    "start_ts_ms": null,
    "end_ts_ms": null,
    "min_similarity": 0.0,
    "limit": 20
  },
  "mode": "person_lookup"
}
```

这个 API 可以先调用 repository 的 `trajectory()`，但前端语义上不再叫 evidence。

后续可扩展：

```http
POST /api/v1/people/find-by-image
```

用于上传临时人脸图片检索，不要求先注册为人员。

## 前端改造计划

### 阶段 A：信息架构改名与默认展示

- 人员页标题从“人脸图库”调整为“人脸轨迹”。
- `查找此人` 改为打开“一键找人”窗口，而不是只刷新右侧卡片。
- 最近位置和轨迹默认在选择人员时加载，不要求用户再理解“证据查询”。
- 证据页默认隐藏或弱化 `watchlist_hit` / `live_search_hit`。
- 人脸类证据如果出现，按钮文案改为“查看轨迹”，不再突出“播放”。

### 阶段 B：一键找人窗口

- 在 `index.html` 增加独立 modal / panel。
- 在 `operator.js` 增加状态：
  - `findPersonDialogOpen`
  - `findPersonResults`
  - `findPersonQuery`
- 调用 `/people/{id}/latest-location` 和 `/people/{id}/trajectory`，后续切到 `/people/{id}/find`。
- 结果卡片只展示图片和定位信息。

### 阶段 C：查询能力和分页

- 后端 `PeopleRepository.trajectory()` 增加 camera/time 参数。
- 前端轨迹列表增加时间范围、摄像头筛选、相似度阈值。
- 轨迹使用分页或“加载更多”，避免一次拉太多图片。

### 阶段 D：证据页解耦

- 证据列表默认分类排除人脸搜索类事件。
- 增加“人脸命中”跳转入口到人员轨迹，而不是在证据详情里播放。
- 保留审计能力：如果用户要查某条人脸事件的原始记录，仍能从轨迹进入相关事件元数据。

## 测试计划

### 静态前端 contract test

- 人员页存在独立“一键找人”窗口。
- `find-selected-person` 不再是证据查看入口。
- 人脸轨迹调用 `/api/v1/people/{person_id}/trajectory` 或 `/find`。
- 证据页不再把 `watchlist_hit` 文案作为主视频证据。

### 后端 API test

- `trajectory` 支持 limit/offset。
- 增加 camera/time filter 后，返回结果正确。
- `find` API 返回 latest_location 和 results。
- 没有证据 bundle 的 face observation 也能出现在轨迹中。

### 人工验收

- 8090 人员页选择人员，可以看到最近位置和轨迹图片。
- 一键找人打开独立窗口，不进入证据详情。
- 证据页默认看到的是入侵/行为类证据。
- 人脸名单命中不再表现为“视频打不开”的问题。

## 实施顺序

1. 先改前端信息架构和文案，不改数据模型。
2. 增加一键找人 modal，复用现有 latest-location / trajectory API。
3. 调整证据页默认分类和人脸类条目的跳转行为。
4. 补静态 contract test。
5. 增加 `/people/{id}/find` 和 trajectory filter。
6. 补后端 API test。
7. 重启 `evidence-viewer` / `api`，用 8090 做人工验收。

## 非目标

- 第一阶段不重新设计人脸 embedding 存储。
- 第一阶段不要求人脸命中生成视频证据。
- 第一阶段不把一键找人结果自动固化成 evidence bundle。
- 第一阶段不改变入侵、行为类视频证据的生成链路。

## 最终验收定义

这次重构完成后，8090 上应当形成三条清晰路径：

- 告警证据：去证据页，审查视频/图片证据。
- 人脸轨迹：去人员页，按人查询历史出现。
- 一键找人：打开独立找人窗口，快速定位最新适合摄像头和图片命中。

这三条路径不能再互相冒充，尤其不能让 `watchlist_hit` 继续表现成一个必须播放视频的证据项。
