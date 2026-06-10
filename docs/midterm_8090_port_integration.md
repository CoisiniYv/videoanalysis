# Midterm 8090 端口功能与程序对接现状

更新时间：2026-06-10

## 总结

当前 midterm 运行栈里，`8090` 是客户侧唯一操作入口。它不是后端
`services/api` 本体，而是 `services/evidence-viewer` 对外发布的 FastAPI
服务：

```text
browser
  -> evidence-viewer:8090
      -> 8090 原生证据文件接口 /api/*
      -> 代理 /api/v1/* 到内部 api:8000
      -> 代理 /media/* 到内部 api:8000/media/*
```

`api:8000` 只在 compose 网络内 `expose`，不发布到宿主机。客户应访问：

```text
http://0.0.0.0:8090/
```

## 2026-06-10 实测状态

当前 8090 大部分操作实测可用，但不能判定为全链路完全正常。

已通过的 8090 操作：

- `/health` 和首页加载正常，页面包含摄像头、人员、证据、存储维护模块。
- 摄像头列表、摄像头创建/更新 smoke 链路正常。
- 区域创建和回读正常。
- 人员列表、人员详情、`/media/*` 图片代理正常。
- 完整人脸注册正常：使用真实 `reese.jpg`、YOLOv8 face ONNX、AdaFace ONNX
  注册 `operator:smoke:face` 成功。
- 告警证据列表、证据详情、生产 sidecar 标注、sink metadata 正常。
- raw clip 支持 Range 读取，返回 `206` 和 `video/quicktime`。
- 事件 API 可通过 8090 proxy 查询。
- 存储维护 summary、证据删除 preview、job detail、人员删除 preview、图库删除
  preview 正常。
- 删除执行返回 `403 storage maintenance execute disabled`，符合当前默认配置。
- `POST /api/v1/cameras/runtime/apply` 是受控运行时应用入口：导出配置后会
  停源、停 workers、重建 sink epoch、重启 Replay/Savant，再恢复源和 workers。
- `POST /api/v1/cameras/runtime/restart` 是 8090 上的一键受控重启入口，用于在
  管理端保持在线的情况下恢复推理/录像链路。

当前阻断项：

- 通过 8090 保存“绑定区域的算法规则”失败。测试接口：

  ```text
  POST /api/v1/cameras/{camera_id}/algorithm-rules
  ```

  使用 `zone_id=operator_smoke_perimeter` 创建 `behavior.intrusion` 规则时，
  内部 API 返回：

  ```text
  HTTP 500
  invalid input syntax for type uuid: "operator_smoke_perimeter"
  ```

  已确认数据库结构里 `camera_zones.zone_id` 是文本 ID，而 `camera_rules.zone_id`
  是 `uuid`。当前 API 将前端使用的文本区域 ID 直接写入 `camera_rules.zone_id`
  的 uuid 字段，导致规则保存失败。凡是需要绑定 polygon 区域或检测线的算法
  规则，都应视为当前未完全接通。

## 部署绑定

`infra/docker-compose.midterm.yml` 中的实际绑定：

- `evidence-viewer` 容器：`video-analytics-midterm-evidence-viewer`
- 宿主机端口：`8090:8090`
- 服务环境：
  - `EVIDENCE_ROOT=/evidence`
  - `EVIDENCE_VIEWER_HOST=0.0.0.0`
  - `EVIDENCE_VIEWER_PORT=8090`
  - `EVIDENCE_VIEWER_MAX_BUNDLES=200`
  - `OPERATOR_API_BASE_URL=http://api:8000`
- 证据目录挂载：
  - 宿主机 `/data/video-analytics/media/evidence`
  - 容器内 `/evidence`
  - 挂载方式 `ro`

内部 `api` 服务对接：

- `api` 容器：`video-analytics-midterm-api`
- 内部端口：`8000`
- 只通过 compose 网络暴露给 `evidence-viewer`
- 挂载 Docker socket，并默认开启 `CAMERA_RUNTIME_APPLY_ENABLED=true`

## 8090 路由职责

| 外部路径 | 实现位置 | 实际程序对接 | 状态 |
| --- | --- | --- | --- |
| `/` | `services/evidence-viewer/app/main.py` | 返回 `services/evidence-viewer/app/static/index.html` | 已接通 |
| `/static/*` | `services/evidence-viewer/app/main.py` | 8090 静态资源，加载 `operator.js`、`evidence.js`、`maintenance.js` | 已接通 |
| `/health` | `services/evidence-viewer/app/main.py` | 检查 `/evidence` 是否存在，返回 `read_only=true` | 已接通 |
| `/api/bundles` | `services/evidence-viewer/app/main.py` | 扫描文件证据 bundle，支持事件类型、摄像头、人员、录像状态等过滤 | 已接通 |
| `/api/bundles/{event_id}` | `services/evidence-viewer/app/main.py` | 返回单个证据 bundle manifest | 已接通 |
| `/api/bundles/{event_id}/annotations` | `services/evidence-viewer/app/main.py` | 读取生产 sidecar 标注；legacy/preview 仅显式调试使用 | 已接通 |
| `/api/bundles/{event_id}/sink-metadata` | `services/evidence-viewer/app/main.py` | 读取 `sink_metadata.json` | 已接通 |
| `/api/bundles/{event_id}/media/raw_clip` | `services/evidence-viewer/app/main.py` | 从证据 bundle 中发现并返回 `raw_clip.*` | 已接通 |
| `/api/v1/{path}` | `services/evidence-viewer/app/main.py` | HTTP 代理到 `http://api:8000/api/v1/{path}` | 已接通，限白名单前缀 |
| `/media/{path}` | `services/evidence-viewer/app/main.py` | HTTP 代理到 `http://api:8000/media/{path}`，用于人脸图片、截图等媒体访问 | 已接通 |
| `/api/v1/ws/alerts` | `services/api/app/routers/ws_alerts.py` | API 内部有 WebSocket 端点，但 8090 viewer 代理实现是普通 HTTP `urlopen` | 8090 未形成真正 WebSocket 透明代理 |

`/api/v1/{path}` 当前允许代理的第一段路径：

```text
cameras
algorithms
events
people
maintenance
ws
```

注意：`ws` 虽在白名单里，但 `evidence-viewer` 当前代理函数不是 WebSocket
升级代理。若需要浏览器从 8090 接收实时告警，需要单独实现 WebSocket 转发或
改成前端可访问的直连入口。

## 页面功能与实际后端

### 摄像头管理

页面入口：`8090 /` 的“摄像头管理”。

前端实现：`services/evidence-viewer/app/static/operator.js`

实际调用：

- `GET /api/v1/cameras`
- `POST /api/v1/cameras`
- `PUT /api/v1/cameras/{camera_id}`
- `POST /api/v1/cameras/{camera_id}/enable`
- `POST /api/v1/cameras/{camera_id}/disable`
- `GET /api/v1/cameras/{camera_id}/config`
- `POST/PUT/DELETE /api/v1/cameras/{camera_id}/zones...`
- `GET/POST/PUT /api/v1/cameras/{camera_id}/algorithm-rules...`

后端实现：

- `services/api/app/routers/cameras.py`
- `services/api/app/routers/algorithms.py`
- `services/api/app/repositories/cameras.py`

对接情况：

- 摄像头、区域、算法规则是真实写入数据库的 API，不是前端假数据。
- 算法定义来自 `services/api/app/algorithm_registry.py`。
- 行为算法规则会校验区域或检测线绑定，例如入侵类需要 polygon，翻越类需要
  line。
- 规则保存后页面会触发运行时应用。
- 已知问题：当前绑定区域/检测线的算法规则保存会因 `camera_rules.zone_id`
  类型不匹配失败，详见上方“2026-06-10 实测状态”。

### 运行时应用

页面按钮：“应用运行时”或“保存算法并应用运行时”。

实际调用：

```text
POST /api/v1/cameras/runtime/apply
POST /api/v1/cameras/runtime/restart
```

后端实现：

- `services/api/app/routers/cameras.py`
- `services/api/app/services/runtime_apply.py`

实际动作：

- 导出摄像头、区域、算法规则到
  `modules/savant_security/config/cameras.midterm.yml`
- 导出动态源配置到 `infra/generated/sources.generated.yml`
- 通过 Docker socket 停止 compose 固定源和 `video-analytics-source-*` 动态源
- 停止 event/face/clip/media workers
- 创建新的 runtime epoch 并重建 `video-analytics-midterm-video-file-sink`
- 重启 `video-analytics-midterm-replay-service`
- 重启 `video-analytics-midterm-savant`
- 对非 compose 固定源的 RTSP 摄像头，按 `source_id` 重建
  `video-analytics-source-{source_id}` gstreamer source-adapter 容器
- 先恢复 workers，最后恢复启用中的固定源和动态源

受控重启不重启：

- `video-analytics-midterm-evidence-viewer`，也就是 8090 管理端
- `video-analytics-midterm-api`，也就是 8090 proxy 后面的内部 API
- Redis/PostgreSQL

对接情况：

- 在 `infra/docker-compose.midterm.yml` 中，`CAMERA_RUNTIME_APPLY_ENABLED`
  默认是 `true`。
- 若单独运行 `services/api` 而不是通过 midterm compose，代码默认值是
  disabled，需要显式设置 `CAMERA_RUNTIME_APPLY_ENABLED=true`。

2026-06-10 发现旧的局部重启方式可能导致 Replay 缓存旧 ZeroMQ
`routing_id`，表现为 source-adapter 仍处理帧但 Replay 拒收帧、没有新的
evidence。该问题记录和诊断命令见
`docs/midterm_replay_routing_id_recovery.md`。

### 人员与人脸

页面入口：`8090 /` 的“人员与人脸”。

前端实现：`services/evidence-viewer/app/static/operator.js`

实际调用：

- `GET /api/v1/people`
- `GET /api/v1/people/{person_id}`
- `POST /api/v1/people/register-face`
- 图片预览通过 `/media/*` 代理读取

后端实现：

- `services/api/app/routers/people.py`
- `services/api/app/repositories/people.py`
- `libs/face_registration/image_face_registration.py`

对接情况：

- 人员列表和图库详情来自数据库。
- 人脸注册是真实上传图片并调用注册链路，不是 mock。
- 上传文件写入 `FACE_UPLOAD_ROOT`，默认在
  `/data/video-analytics/media/face_uploads`。
- 注册依赖真实模型路径：
  - `YOLOV8_FACE_ONNX`
  - `ADAFACE_ONNX`
  - `FACE_REGISTRATION_ONNX_PROVIDER`
- 注册结果写入人员和图库相关表，浏览器通过 `/media/*` 查看原图或裁剪图。

### 告警证据

页面入口：`8090 /` 的“告警证据”。

前端实现：`services/evidence-viewer/app/static/evidence.js`

实际调用：

- `GET /health`
- `GET /api/bundles?...`
- `GET /api/bundles/{event_id}`
- `GET /api/bundles/{event_id}/annotations?...`
- `GET /api/bundles/{event_id}/sink-metadata`
- `GET /api/bundles/{event_id}/media/raw_clip`

后端实现：`services/evidence-viewer/app/main.py`

对接情况：

- 证据浏览是 8090 viewer 的原生文件读取能力，不经过内部 API 数据库查询。
- 证据来源是 `/data/video-analytics/media/evidence/{event_id}/` 下的文件。
- 视频播放使用 bundle 内 `raw_clip.*`。
- 画面框、关键点、命中人脸等 overlay 使用 `annotations.frame_cache.identity.jsonl`
  生产 sidecar。
- `annotations.jsonl` legacy 文件被标记为 debug-only；默认自动模式要求生产
  sidecar ready。
- 页面分类包括名单布控、周界入侵、行为异常、聚集风险。

### 存储维护

页面入口：`8090 /` 的“存储维护”。

前端实现：`services/evidence-viewer/app/static/maintenance.js`

实际调用：

- `GET /api/v1/maintenance/storage/summary`
- `POST /api/v1/maintenance/evidence/delete-preview`
- `POST /api/v1/maintenance/evidence/delete`
- `POST /api/v1/maintenance/people/delete-preview`
- `POST /api/v1/maintenance/people/delete`
- `POST /api/v1/maintenance/people/gallery-delete-preview`
- `POST /api/v1/maintenance/people/gallery-delete`
- `POST /api/v1/maintenance/face-media/orphans-preview`
- `POST /api/v1/maintenance/face-media/orphans-cleanup`
- `GET /api/v1/maintenance/jobs/{job_id}`

后端实现：

- `services/api/app/routers/maintenance.py`
- `services/api/app/services/storage_maintenance.py`
- `services/api/app/repositories/maintenance.py`

对接情况：

- 统计和预览是真实后端能力。
- midterm 默认：
  - `STORAGE_MAINTENANCE_SUMMARY_ENABLED=true`
  - `STORAGE_MAINTENANCE_PREVIEW_ENABLED=true`
  - `STORAGE_MAINTENANCE_EXECUTE_ENABLED=false`
- 因此页面能看容量和生成删除预览，但执行删除默认会被后端 403 拦截。
- 若开启执行删除，需要同步考虑 8090 的网络访问范围、操作审计和权限控制。

### 事件 API

8090 代理允许 `/api/v1/events*`：

- `GET /api/v1/events/recent`
- `GET /api/v1/events`
- `GET /api/v1/events/{event_id}`
- `GET /api/v1/events/{event_id}/evidence`
- `POST /api/v1/events/{event_id}/acknowledge`
- `POST /api/v1/events/{event_id}/confirm`
- `POST /api/v1/events/{event_id}/false-positive`
- `POST /api/v1/events/{event_id}/resolve`

后端实现：`services/api/app/routers/events.py`

对接情况：

- 这些接口通过 8090 proxy 可被调用。
- 当前 8090 告警证据页面主要走文件证据接口 `/api/bundles*`，不是以
  `/api/v1/events*` 作为主查询入口。

## 非 8090 职责

- Replay API 不在 8090 上。midterm compose 将 Replay 服务发布到宿主机
  `8098:8080`。
- 内部 API 的 `/operator` 静态页存在于 `services/api`，但当前客户入口不是它；
  客户入口是 8090 的 `services/evidence-viewer` 静态页。
- 8090 当前没有看到登录、角色权限或审计前置网关。若开放到 LAN，尤其是开启
  存储维护执行能力时，需要额外权限边界。

## 快速验证

基础入口：

```bash
curl --noproxy '*' http://0.0.0.0:8090/health
curl --noproxy '*' http://0.0.0.0:8090/ | grep -q camera-form
```

当前 smoke：

```bash
bash scripts/smoke/current/check_operator_camera_and_face_registration.sh
```

该 smoke 会检查 8090 健康、操作台页面、摄像头 API、摄像头配置导出和人脸注册
入口。是否能完成真实人脸注册仍取决于数据库、模型文件和运行时环境。

## /data 目录记录

当前 midterm 运行栈对 `/data/video-analytics` 的实际依赖、已清理的历史目录、
以及仍因权限残留的旧 C2/C1 目录，记录在
`docs/midterm_data_directory_inventory.md`。8090 的证据根仍固定为
`/data/video-analytics/media/evidence`，存储维护 summary 通过 8090 proxy 读取
内部 API 的真实文件系统统计。

## 源码索引

- 8090 compose 入口：`infra/docker-compose.midterm.yml`
- 8090 服务配置：`services/evidence-viewer/app/config.py`
- 8090 路由与代理：`services/evidence-viewer/app/main.py`
- 8090 页面：`services/evidence-viewer/app/static/index.html`
- 摄像头/人员前端：`services/evidence-viewer/app/static/operator.js`
- 证据前端：`services/evidence-viewer/app/static/evidence.js`
- 维护前端：`services/evidence-viewer/app/static/maintenance.js`
- 内部 API 路由注册：`services/api/app/main.py`
- 摄像头 API：`services/api/app/routers/cameras.py`
- 算法 API：`services/api/app/routers/algorithms.py`
- 人员 API：`services/api/app/routers/people.py`
- 事件 API：`services/api/app/routers/events.py`
- 维护 API：`services/api/app/routers/maintenance.py`
- 运行时应用：`services/api/app/services/runtime_apply.py`
