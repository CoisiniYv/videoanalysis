# Midterm 8090 端口功能与程序对接现状

更新时间：2026-06-11

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

## 2026-06-24 操作台一致性修正

本次复核发现 8090 的客户感知问题集中在三个读写边界：

- 摄像头管理页保存的是 `cameras.name`，但证据列表主要从事件 payload 或 bundle
  metadata 读取 `camera_name`。历史事件如果没有写入 `camera_name`，证据页会退回
  `source_id` / `camera_id`，导致 lab 摄像头名称和 evidence 名称不一致。
- 人脸注册页选中人员后会填充注册表单，容易把“新人员注册”和“追加到当前人员”
  混在一起。
- 存储维护的按时间范围删除依赖 bundle 事件时间。若 bundle 缺少
  `metadata.event.start_ts` / `created_at` / `alarm_machine_time` 等可信事件时间，
  不能用目录 mtime 当作删除范围依据，否则会产生误删或漏删。

修正策略：

- 数据库证据索引 `/api/v1/evidence/bundles` 回补 `cameras.name`，优先级仍为：
  event payload `camera_name`、media `camera_name`、payload `camera.name`、最后才是
  cameras 表名称。这样不需要迁移历史 event payload，也能让 8090 证据页显示管理端
  名称。
- 人脸注册前端分离“新人员”和“追加到当前人员”模式。选择人员只改变右侧图库和可追加
  目标；只有点击“追加到当前人员”才会带 hidden `person_id`。
- 范围删除预览只使用事件/报警时间。缺少可信事件时间的 bundle 会被预览为
  `missing_event_time_for_range` 跳过项，操作者可改用单条证据删除或先修复元数据。

迁移到新机器前，必须把以上三项作为 8090 sanity check：

```text
camera display name in /api/v1/evidence/bundles == cameras.name when event payload lacks camera_name
new face registration does not submit hidden person_id unless append mode is selected
time-range evidence delete preview does not use filesystem mtime as event time
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
- 告警证据列表和详情页显示“报警机器时间”。该时间来自 bundle
  `metadata.json` / `summary.json` 中的事件机器时间字段；新证据由
  media-worker 写入 `event.created_at` 和 `event.alarm_machine_time`。
  历史 intrusion bundle 若没有 `created_at`，8090 会仅在 `event_ts_ms` 或
  `source_event_id` 中的值看起来像 Unix epoch 毫秒时作为兼容 fallback。
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
| `/api/bundles` | `services/evidence-viewer/app/main.py` | 扫描文件证据 bundle，支持事件类型、摄像头、人员、录像状态等过滤，并返回 `alarm_machine_time` | 已接通 |
| `/api/bundles/{event_id}` | `services/evidence-viewer/app/main.py` | 返回单个证据 bundle manifest，并返回 `alarm_machine_time` | 已接通 |
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

2026-06-11 复核播放问题后确认：8090 播放不是前端根因。证据链路必须用
frame UUID 作为主对齐锚，并用 runtime epoch 隔离 frame cache。当前 runtime
apply/restart 会创建新 epoch、重建 sink，并清掉 Redis
`security.frame_annotations`；media-worker 也会按 `runtime_epoch_id` 过滤该
stream。Replay job 的 `offset.seconds` 仍保持正值 rewind 语义，不应改成负数。

实测恢复样本：

- `f208b550-6b34-44ff-a847-3219041349ea`：`clip_status=ready`，
  `raw_clip.mov` 10 秒、250 个 H.264 packet，8090 Range 读取返回 HTTP `206`。
- 8090 `/api/bundles?limit=1` 已能返回更新的 ready bundle
  `3acfac74-6c54-4045-820b-b659df3894da`。

如果 Replay sink 已生成 `video.mov`/`metadata.json`，但 8090 暂时还没出现新
bundle，先等 media-worker 完成 sink 稳定检查和长源文件探测；现场 f208 样本
从 Replay job 到 evidence ready 存在约 1 到 2 分钟延迟，这不等同于 8090 卡死。

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
- `annotations.jsonl` legacy 输出已从当前 8090 读取路径移除；默认自动模式只接受
  生产 sidecar ready。
- 页面分类包括名单布控、周界入侵、行为异常、聚集风险。
- 页面证据列表会在摄像头和录像状态之间显示 `报警 YYYY-MM-DD HH:mm:ss`；
  详情页“事件信息”中显示“报警机器时间”。
- `alarm_machine_time_source` 用于审计来源。优先级为
  `event.alarm_machine_time`、`event.created_at`、顶层 `metadata` 字段、
  `summary` 字段；再兼容 epoch 毫秒格式的 `event.event_ts_ms` /
  `event.timestamp_ms` / `event.source_event_id`。非 epoch 的视频内时间戳不
  会被当成机器时间，避免误显示 1970 年附近的时间。
- 新生成的 post-Savant Replay bundle 由 `services/media-worker/app/worker.py`
  将数据库 `events.created_at` 写入 `metadata.json` 的 `event.created_at`、
  `event.alarm_machine_time` 和 `event.alarm_machine_time_source`。
- 如果 8090 显示事件继续产生但没有新的 evidence bundle，先区分两类问题：
  `security.frame_annotations` 不刷新且 Replay 有 `mismatched routing_id` 时是
  Replay routing identity 卡住；`frame_annotations`、事件、Replay job 都刷新但
  sink epoch 目录无新文件时，检查 `video-file-sink` 容器是否保留
  `video-file-sink` 网络别名。8090 runtime apply/restart 重建 sink 时必须保留
  该别名，否则 Replay job 的
  `dealer+connect:tcp://video-file-sink:6666` 无法解析到 sink。
- evidence 目录出现还不等于 raw clip 已发布。8090 侧最终应看到
  `raw_clip_url` 非空；数据库事件 `payload.media.clip_status` 应为 `ready`，
  `payload.media.epoch_guard_status` 应为 `passed`。官方 `video-file-sink`
  metadata 不保留 Replay labels，所以 `sink_metadata_runtime_epoch_id` 缺失不能
  单独判定失败。

2026-06-11 追加播放故障记录：

- 症状：8090 告警证据页打开后不能播放，实际是页面默认选中了最新 bundle
  `83f0d677-9555-40d4-ae28-d2b70c4db523`。该 bundle 的 `raw_clip.mov`
  只有 184 字节，`ffprobe` 没有视频流；而较早的 `991704f0-...` 和
  `91175619-...` 都是约 10 秒 H.264，`GET .../media/raw_clip` Range 请求返回
  HTTP `206` 和 `video/quicktime`。
- 直接原因：post-Savant time-domain crop 的 ffmpeg 命令退出码为 0，但日志为
  `Output file is empty, nothing was encoded`，只留下 MOV 空壳。旧逻辑只检查
  文件存在且大小大于 0，后续读帧失败时没有完成 failure metadata，导致 8090
  把目录当作最新证据展示。
- 修复：`services/media-worker/app/post_savant_evidence_bundle.py` 在裁剪后调用
  `read_decoded_video_frame_count()`，读不到大于 0 的帧即删除产物并抛出
  `video_time_domain_crop_failed:decoded_frame_count_unavailable`。media-worker
  finalizer 会把 bundle 标为 `duration_guard_failed` /
  `time_domain_crop_failed`，`raw_clip_path` 置空，不再发布空视频。
- 8090 侧补强：`/api/bundles` 列表在存在 raw clip 时也返回 `raw_clip_url`；
  前端默认选择第一条 `raw_clip_available=true` 的证据，避免最新失败 bundle
  阻塞历史可播放证据。
- 运行验证：83f0 bundle 现在 `raw_clip_url=null`、`raw_clip_missing`、
  `clip_status=duration_guard_failed`；8090 `/#evidence` 浏览器验证默认选中
  `991704f0-...`，video src 指向
  `/api/bundles/991704f0-bec0-4d84-b670-da7bdd2e30d1/media/raw_clip`。

2026-06-11 追加根因修正：

- 8090 不能播放不是前端根因。前端只暴露了最新失败 bundle 被选中的表象；
  后端根因是 post-Savant Replay sink 的 metadata/video 时间轴在新 epoch 后仍
  出现重复 PTS 段，media-worker 旧裁剪逻辑把全文件第一个 PTS 当作连续秒表。
- 83f0 现场证据：
  - runtime epoch guard passed：
    `midterm-20260610T172541Z-949ea460` 在事件、Replay labels、sink path、
    当前 `.current_epoch.json` 中一致。
  - sink metadata PTS 不严格递增：row 46 从 `86404866666` 回落到
    `4848555555`，row 2800 从 `119671600000` 回落到 `1004522222`。
  - 源 `video.mov` 第一包 PTS 为 `118.667084`；旧命令按
    `requested_start_pts - first_metadata_pts` 得到错误 seek 点，ffmpeg 退出码
    0 但输出空壳 MOV。
- 固化后的后端规则：
  - frame identity 以 UUID 优先。若 sink metadata 中的 `uuid/frame_uuid` 能和
    `event_frame_uuid`、`start_window_frame_uuid`、`post_window_frame_uuid`
    匹配，优先选该 UUID 所在的连续 PTS 段。
  - 当前官方 Replay sink 不保证保留原始事件 `frame_uuid`，因此 UUID 缺失时不
    再把所有匹配 PTS 混在一起，而是选择最新的严格递增连续 PTS 段，并在
    `time_window` 记录 `frame_uuid_anchor_found=false`、
    `time_domain_selection_strategy=latest_contiguous_pts_segment`。
  - raw clip 裁剪使用该连续段的 `crop_segment_first_pts` 作为时间基准，并用
    `setpts=PTS-STARTPTS,trim=...` 的归一化 filter 裁剪，不再用输入前 `-ss`
    依赖容器 packet PTS 起点。
  - media-worker 读取 `security.frame_annotations` 时按事件/replay labels 的
    `runtime_epoch_id` 过滤；旧 epoch 或缺 epoch 的 frame annotation cache 不
    再参与 sidecar 对齐。
  - 8090 的受控 runtime apply/restart 创建新 epoch 时会删除
    `security.frame_annotations` Redis stream。该 stream 是 frame cache，不是
    事件或 evidence 审计数据；返回体和 `.current_epoch.json` 记录
    `redis_frame_cache_streams_reset` / `redis_frame_cache_reset_count`，旧
    evidence 文件仍保留在 `/data` 用于追溯。
  - 受控重启后若事件已产生但仍无新 evidence，检查 clip-worker 是否出现
    `missing_post_savant_frame_pts_window`。本次新事件 `72424794-...` 证明
    routing id 已恢复、Savant frame annotations 已带新 epoch，但 9 秒默认等待
    仍可能早于 post-window frame annotation 到达；post-Savant frame proof 已
    改为独立配置 `POST_SAVANT_FRAME_PROOF_ATTEMPTS=30` /
    `POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S=1.0`。

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
curl --noproxy '*' 'http://0.0.0.0:8090/api/bundles?limit=5' \
  | jq '.bundles[] | {event_id,event_type,alarm_machine_time,alarm_machine_time_source}'
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
- 8090 文件证据索引：`services/evidence-viewer/app/evidence_index.py`
- 8090 页面：`services/evidence-viewer/app/static/index.html`
- 摄像头/人员前端：`services/evidence-viewer/app/static/operator.js`
- 证据前端：`services/evidence-viewer/app/static/evidence.js`
- 新证据 metadata 写入：`services/media-worker/app/worker.py`
- 维护前端：`services/evidence-viewer/app/static/maintenance.js`
- 内部 API 路由注册：`services/api/app/main.py`
- 摄像头 API：`services/api/app/routers/cameras.py`
- 算法 API：`services/api/app/routers/algorithms.py`
- 人员 API：`services/api/app/routers/people.py`
- 事件 API：`services/api/app/routers/events.py`
- 维护 API：`services/api/app/routers/maintenance.py`
- 运行时应用：`services/api/app/services/runtime_apply.py`
