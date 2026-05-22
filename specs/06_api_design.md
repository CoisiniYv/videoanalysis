# 06_api_design.md

## 1. API 目标

FastAPI 是业务系统入口，服务于：

- 报警大屏。
- 管理后台。
- 摄像头和 ROI 配置。
- 人员库。
- 重点人员布控。
- 一键找人。
- 事件查询和处理。
- WebSocket 实时告警。

## 2. 通用约定

### 2.1 URL 前缀

```text
/api/v1
```

### 2.2 返回结构

```json
{
  "data": {},
  "error": null,
  "request_id": "..."
}
```

错误：

```json
{
  "data": null,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "invalid camera_id"
  },
  "request_id": "..."
}
```

### 2.3 时间格式

API 使用 ISO 8601 时间。

### 2.4 权限

第一版可简化，后期需要：

- admin。
- operator。
- viewer。
- auditor。

## 3. 摄像头 API

### 3.1 创建摄像头

```http
POST /api/v1/cameras
```

请求：

```json
{
  "id": "cam_001",
  "name": "东门入口",
  "rtsp_url": "rtsp://...",
  "site_id": "site_a",
  "location": "东门",
  "gpu_id": 0,
  "enabled": true
}
```

### 3.2 查询摄像头列表

```http
GET /api/v1/cameras
```

### 3.3 更新摄像头

```http
PUT /api/v1/cameras/{camera_id}
```

### 3.4 启用/停用摄像头

```http
POST /api/v1/cameras/{camera_id}/enable
POST /api/v1/cameras/{camera_id}/disable
```

## 4. ROI 和规则配置 API

### 4.1 创建区域

```http
POST /api/v1/cameras/{camera_id}/zones
```

请求：

```json
{
  "zone_name": "perimeter",
  "zone_type": "polygon",
  "points": [[100, 300], [900, 300], [900, 700], [100, 700]]
}
```

### 4.2 创建规则

```http
POST /api/v1/cameras/{camera_id}/rules
```

请求：

```json
{
  "rule_type": "intrusion",
  "enabled": true,
  "config": {
    "zone": "perimeter",
    "min_inside_ms": 1000,
    "cooldown_s": 30
  }
}
```

### 4.3 获取摄像头完整配置

```http
GET /api/v1/cameras/{camera_id}/config
```

返回需可导出为 Savant `cameras.yml`。

## 5. 事件 API

### 5.1 最近事件

```http
GET /api/v1/events/recent?limit=50
```

### 5.2 条件查询事件

```http
GET /api/v1/events?event_type=intrusion&camera_id=cam_001&start=...&end=...
```

### 5.3 事件详情

```http
GET /api/v1/events/{event_id}
```

### 5.4 事件处理

```http
POST /api/v1/events/{event_id}/acknowledge
POST /api/v1/events/{event_id}/confirm
POST /api/v1/events/{event_id}/false-positive
POST /api/v1/events/{event_id}/resolve
```

请求：

```json
{
  "operator": "user_001",
  "comment": "已确认"
}
```

## 6. 人员库 API

### 6.1 创建人员

```http
POST /api/v1/persons
```

请求：

```json
{
  "name": "张三",
  "external_id": "student_001",
  "description": "测试人员"
}
```

### 6.2 上传人脸照片

```http
POST /api/v1/persons/{person_id}/faces
Content-Type: multipart/form-data
```

后端流程：

```text
保存图片
  -> SCRFD 检测人脸
  -> ArcFace 提取 embedding
  -> 写入 person_gallery_embeddings
  -> 返回 embedding_id 和质量分
```

### 6.3 查询人员

```http
GET /api/v1/persons?name=张三
```

### 6.4 删除/停用人员

```http
POST /api/v1/persons/{person_id}/disable
DELETE /api/v1/persons/{person_id}
```

删除必须写审计日志。

## 7. 重点人员布控 API

### 7.1 创建布控规则

```http
POST /api/v1/watchlist
```

请求：

```json
{
  "person_id": 1,
  "enabled": true,
  "threshold": 0.75,
  "cooldown_seconds": 60,
  "camera_scope": ["cam_001", "cam_002"]
}
```

### 7.2 查询布控规则

```http
GET /api/v1/watchlist
```

### 7.3 启用/停用布控

```http
POST /api/v1/watchlist/{rule_id}/enable
POST /api/v1/watchlist/{rule_id}/disable
```

## 8. 一键找人 API

### 8.1 创建实时搜索任务

```http
POST /api/v1/live-search
```

请求：

```json
{
  "person_id": 1,
  "threshold": 0.75,
  "camera_scope": null,
  "expires_in_minutes": 60
}
```

返回：

```json
{
  "job_id": 123,
  "status": "active",
  "person_id": 1,
  "expires_at": "2026-05-20T12:00:00+09:00"
}
```

### 8.2 查询任务状态

```http
GET /api/v1/live-search/{job_id}
```

### 8.3 停止任务

```http
POST /api/v1/live-search/{job_id}/stop
```

### 8.4 查询命中记录

```http
GET /api/v1/live-search/{job_id}/hits
```

## 9. 轨迹 API

### 9.1 查询人员出现记录

```http
GET /api/v1/persons/{person_id}/appearances?start=...&end=...
```

返回：

```json
[
  {
    "camera_id": "cam_001",
    "location": "东门入口",
    "captured_at": "2026-05-20T10:30:00+09:00",
    "snapshot_path": "/data/events/...",
    "match_score": 0.84
  }
]
```

### 9.2 查询 track

```http
GET /api/v1/tracks?camera_id=cam_001&track_id=t_001
```

## 10. WebSocket 实时告警

### 10.1 连接

```text
WS /api/v1/ws/alerts
```

### 10.2 消息格式

```json
{
  "message_type": "alert",
  "event": {
    "event_id": 1001,
    "event_type": "watchlist_hit",
    "camera_id": "cam_012",
    "location": "东门入口",
    "person_name": "张三",
    "match_score": 0.84,
    "snapshot_url": "/media/snapshots/...",
    "created_at": "2026-05-20T10:30:00+09:00"
  }
}
```

### 10.3 大屏应该订阅

- `intrusion`。
- `fall`。
- `watchlist_hit`。
- `live_search_hit`。
- `wall_climb_suspicious`。

## 11. 监控 API

FastAPI 可以提供简化健康检查：

```http
GET /health
GET /ready
GET /api/v1/system/status
```

但 GPU/FPS/队列指标主要由 Prometheus + Grafana 负责。

## 12. API 模块目录

```text
services/api/app/
  main.py
  routers/
    cameras.py
    zones.py
    rules.py
    events.py
    persons.py
    watchlist.py
    live_search.py
    tracks.py
    health.py
  repositories/
    cameras.py
    events.py
    persons.py
    vectors.py
  schemas/
    camera.py
    event.py
    person.py
    face.py
    live_search.py
  services/
    face_registration.py
    watchlist.py
    live_search.py
    alert_ws.py
```

---

# 13. 事件媒体字段与 MVP 展示补充

MVP 阶段事件接口必须兼容没有截图和视频的事件。

`GET /api/v1/events/recent` 和 `GET /api/v1/events/{event_id}` 建议返回：

```json
{
  "event_id": 1001,
  "event_type": "intrusion",
  "camera_id": "cam_001",
  "snapshot_url": null,
  "clip_url": null,
  "media": {
    "snapshot_status": "not_implemented",
    "clip_status": "not_implemented",
    "recording_strategy": "reserved"
  }
}
```

前端和报警大屏必须支持：

```text
1. 有事件但无视频。
2. 视频生成中。
3. 视频生成失败。
4. 视频 ready 后补充显示。
```

MVP 阶段显示文案建议：

```text
视频片段：暂未生成
截图：暂未生成
```
