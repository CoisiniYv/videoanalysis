# Operator API 清单

更新时间：2026-07-20。基线为当前工作区；接口实现优先于本文。

本清单由当前 FastAPI `app.openapi()`、router decorators 和前端请求代码交叉提取。

符号：

- `使用`：当前 8090 JavaScript 直接调用；
- `间接`：当前页面通过另一个流程或返回 URL 使用；
- `可用`：后端存在，但当前页面没有直接调用；
- `兼容`：旧 evidence-viewer 文件型接口，新功能不应优先使用。

## 1. 通用约定

浏览器 base URL：当前页面 origin，例如 `http://host:8090`。

JSON API prefix：

```text
/api/v1
```

正常响应：

```json
{
  "data": {},
  "error": null,
  "request_id": "uuid"
}
```

错误响应通常为：

```json
{
  "data": null,
  "error": {
    "message": "...",
    "code": 400,
    "details": {}
  },
  "request_id": "uuid"
}
```

FastAPI 参数验证错误可能直接使用 `detail[]`，公共 `request()` 已兼容该形状。

## 2. 摄像头接口

Router：`services/api/app/routers/cameras.py`

| 状态 | Method | Path | 参数/Body | 用途 |
| --- | --- | --- | --- | --- |
| 使用 | GET | `/api/v1/cameras` | 无 | 摄像头列表和名称 lookup |
| 使用 | POST | `/api/v1/cameras` | `CameraCreate` JSON | 新增摄像头 |
| 使用 | GET | `/api/v1/cameras/{camera_id}` | path | 单摄像头详情 |
| 使用 | PUT | `/api/v1/cameras/{camera_id}` | `CameraUpdate` JSON | 修改摄像头 |
| 使用 | POST | `/api/v1/cameras/{camera_id}/enable` | 无 | 启用 |
| 使用 | POST | `/api/v1/cameras/{camera_id}/disable` | 无 | 停用 |
| 使用 | GET | `/api/v1/cameras/{camera_id}/config` | 无 | camera + zones + rules + alert policy |
| 使用 | GET | `/api/v1/cameras/{camera_id}/preview.jpg` | `max_width=1280`, `timeout_ms=3000`, `quality=85` | ROI 实时 JPEG 预览 |
| 使用 | POST | `/api/v1/cameras/{camera_id}/zones` | `ZoneCreate`; `bind_rules=false` | 新增区域/检测线 |
| 使用 | PUT | `/api/v1/cameras/{camera_id}/zones/{zone_id}` | `ZoneCreate`; `bind_rules=false` | 修改区域/检测线 |
| 使用 | DELETE | `/api/v1/cameras/{camera_id}/zones/{zone_id}` | 无 | 删除区域 |
| 使用 | GET | `/api/v1/cameras/{camera_id}/zones` | 无 | 区域列表 |
| 使用 | POST | `/api/v1/cameras/{camera_id}/rules` | `RuleCreate` | 新增基础规则 |
| 使用 | GET | `/api/v1/cameras/{camera_id}/rules` | 无 | 基础规则列表 |
| 可用 | GET | `/api/v1/cameras/{camera_id}/rules/{rule_id}` | 无 | 单规则 |
| 使用 | PUT | `/api/v1/cameras/{camera_id}/rules/{rule_id}` | `RuleUpdate` | 修改基础规则 |
| 使用 | DELETE | `/api/v1/cameras/{camera_id}/rules/{rule_id}` | 无 | 删除规则 |
| 可用 | POST | `/api/v1/cameras/{camera_id}/rules/{rule_id}/enable` | 无 | 启用基础规则 |
| 可用 | POST | `/api/v1/cameras/{camera_id}/rules/{rule_id}/disable` | 无 | 停用基础规则 |
| 可用 | GET | `/api/v1/cameras/{camera_id}/alert-policy` | 无 | 告警策略 |
| 可用 | PUT | `/api/v1/cameras/{camera_id}/alert-policy` | `AlertPolicy` | 保存告警策略 |
| 使用 | GET | `/api/v1/cameras/{camera_id}/runtime-config` | 无 | 当前摄像头生成配置预览 |
| 可用 | GET | `/api/v1/cameras/config/export` | `include_disabled=false` | YAML 导出，非 JSON envelope |
| 使用 | POST | `/api/v1/cameras/runtime/config/sync` | `include_disabled=true` | 同步配置，不做完整重启 |
| 使用 | POST | `/api/v1/cameras/runtime/sources/apply` | `include_disabled=true` | 应用 source containers |
| 使用 | POST | `/api/v1/cameras/runtime/apply` | `include_disabled=true`, `force=false` | 应用完整运行时 |
| 使用 | POST | `/api/v1/cameras/runtime/restart` | `include_disabled=true`, `force=false` | 受控重启 |
| 可用 | GET | `/api/v1/cameras/runtime/supervisor` | 无 | supervisor 状态 |
| 可用 | POST | `/api/v1/cameras/runtime/supervisor/recover` | 无 | 触发恢复 |

JPEG preview 不返回 JSON。响应包含：

```text
Content-Type: image/jpeg
Cache-Control: no-store
X-Camera-Preview-Width
X-Camera-Preview-Height
X-Camera-Source-Width
X-Camera-Source-Height
```

## 3. 算法与每摄像头规则

Router：`services/api/app/routers/algorithms.py`

| 状态 | Method | Path | Body | 用途 |
| --- | --- | --- | --- | --- |
| 使用 | GET | `/api/v1/algorithms` | 无 | 算法定义列表 |
| 使用 | GET | `/api/v1/algorithms/support-matrix` | 无 | UI 支持/运行状态矩阵 |
| 可用 | GET | `/api/v1/algorithms/{algorithm_id}` | 无 | 单算法定义 |
| 使用 | POST | `/api/v1/cameras/{camera_id}/algorithm-rules` | `AlgorithmRuleCreate` | 新增 Operator 算法规则 |
| 使用 | GET | `/api/v1/cameras/{camera_id}/algorithm-rules` | 无 | 算法规则列表 |
| 使用 | PUT | `/api/v1/cameras/{camera_id}/algorithm-rules/{rule_id}` | `AlgorithmRuleUpdate` | 修改算法规则 |
| 使用 | POST | `/api/v1/cameras/{camera_id}/algorithm-rules/{rule_id}/enable` | 无 | 启用 |
| 使用 | POST | `/api/v1/cameras/{camera_id}/algorithm-rules/{rule_id}/disable` | 无 | 停用 |

不要把 support matrix 的 `configurable=true` 等同于“运行时一定检测”。页面同时读取：

```text
status
configurable
per_camera_gate
runtime_detecting
event_enabled
evidence_enabled
production_ready
status_reason
requires_runtime_apply
```

## 4. 人员、人脸注册和轨迹

Router：`services/api/app/routers/people.py`

| 状态 | Method | Path | 参数/Body | 用途 |
| --- | --- | --- | --- | --- |
| 使用 | GET | `/api/v1/people` | `include_inactive=false`, `q`, `limit=50`, `offset=0` | 人员库和候选项 |
| 使用 | GET | `/api/v1/people/{person_id}` | path int | 人员详情和 gallery |
| 可用 | GET | `/api/v1/people/{person_id}/latest-location` | similarity/camera/source/time filters | 最新位置 |
| 使用 | GET | `/api/v1/people/{person_id}/trajectory` | similarity/camera/source/time, `limit=50`, `offset=0` | persisted 轨迹分页 |
| 可用 | GET | `/api/v1/people/{person_id}/find` | 同类 filters，`limit=20` | 包含 observation search 的一键找人 |
| 使用 | POST | `/api/v1/people/register-face` | multipart form | 新人员或追加人脸 |
| 使用 | POST | `/api/v1/people/register-faces` | multipart form，重复 `images` 字段 | 同一人员多图批量注册 |

轨迹页面当前请求：

```text
min_similarity=0.6
include_unregistered_sources=true
limit=50
offset=<page offset>
camera_id=<optional>
start_ts_ms=<optional epoch ms>
end_ts_ms=<optional epoch ms>
```

单图注册 `/register-face` form fields：

```text
image                       required file
external_person_id          required string
name                        required string
person_id                   optional int; append mode only
description                 optional string
is_primary                  bool, current UI false
quality_threshold           float 0..1, current UI 0.65
allow_multiple_faces        bool, current UI false
keep_crop                   bool, current UI true
operator                    optional, current UI "operator"
```

批量注册 `/register-faces` 复用上述 identity/quality fields，文件字段改为重复的
`images`。每一张有效图片生成一条 `person_gallery_embeddings`；接口返回每张图片的
`status`、`gallery_embedding_id` 或 `error_code/error_message`。处理结果可能是：

```text
REGISTERED   全部成功
PARTIAL      部分成功，HTTP 207，前端必须展示逐张失败原因
FAILED       没有图片成功，HTTP 207（身份或数据库全局错误除外）
```

支持扩展名：`.jpg`、`.jpeg`、`.png`、`.bmp`、`.webp`。默认单图最大上传 10 MiB；
批量默认最多 12 张、总计最多 50 MiB，可分别由 `FACE_UPLOAD_MAX_BYTES`、
`FACE_BATCH_UPLOAD_MAX_FILES`、`FACE_BATCH_UPLOAD_MAX_BYTES` 调整。

## 5. 证据接口

Router：`services/api/app/routers/evidence.py`

| 状态 | Method | Path | 参数 | 用途 |
| --- | --- | --- | --- | --- |
| 使用 | GET | `/api/v1/evidence/health` | 无 | 验证 DB index path |
| 可用 | GET | `/api/v1/evidence` | 与 bundles 相同 | bundle 列表短路径 alias |
| 使用 | GET | `/api/v1/evidence/bundles` | filters + limit/offset | 列表和分页 |
| 使用 | GET | `/api/v1/evidence/bundles/{event_id}` | path | manifest/detail |
| 使用 | GET | `/api/v1/evidence/bundles/{event_id}/annotations` | `include_records=true` | overlay records |
| 使用 | GET | `/api/v1/evidence/bundles/{event_id}/sink-metadata` | 无 | timeline/frame records |

列表 filters：

```text
event_type
event_category              default "evidence"
source_id
camera_id
event_id
person
clip_status
limit                       1..500, default 50
offset                      >=0
```

前端“摄像头”搜索输入当前发送为 `source_id`；后端 repository 同时匹配内部 source、
camera id、摄像头表名称及 payload camera name。不要在新 UI 中重新实现该匹配逻辑。

健康响应的 `data` 必须是：

```json
{"status": "ok", "index_source": "database"}
```

## 6. Event 接口

Router：`services/api/app/routers/events.py`

当前 8090 主页面不直接依赖这些接口，但 proxy 允许访问，适合事件详情或未来告警页：

| 状态 | Method | Path | 用途 |
| --- | --- | --- | --- |
| 可用 | GET | `/api/v1/events/recent?limit=50` | 最近事件 |
| 可用 | GET | `/api/v1/events` | 条件查询和分页 |
| 可用 | GET | `/api/v1/events/{event_id}` | UUID 或 source event id |
| 可用 | GET | `/api/v1/events/{event_id}/evidence` | event + tasks + evidence detail |
| 可用 | POST | `/api/v1/events/{event_id}/acknowledge` | 状态变更 |
| 可用 | POST | `/api/v1/events/{event_id}/confirm` | 状态变更 |
| 可用 | POST | `/api/v1/events/{event_id}/false-positive` | 状态变更 |
| 可用 | POST | `/api/v1/events/{event_id}/resolve` | 状态变更 |

事件 mutation body：

```json
{"operator": "", "comment": ""}
```

## 7. Runtime 接口

Router：`services/api/app/routers/runtime.py`

| 状态 | Method | Path | 参数/Body | 用途 |
| --- | --- | --- | --- | --- |
| 使用 | GET | `/api/v1/runtime/overview` | 无 | 总览、source、forwarder、evidence、container |
| 使用 | GET | `/api/v1/runtime/latency` | 无 | annotation、DB、A/B source/queue 的在线延迟摘要 |
| 使用 | GET | `/api/v1/runtime/control` | 无 | 控制状态 |
| 使用 | POST | `/api/v1/runtime/control/single/start` | 无 | 启动单路 |
| 使用 | POST | `/api/v1/runtime/control/single/stop` | 无 | 停止单路 |
| 使用 | POST | `/api/v1/runtime/control/single/restart` | `force=false` | 受控重启单路 |
| 使用 | POST | `/api/v1/runtime/control/dual/stop` | 无 | 停止双路扩展 |
| 使用 | GET | `/api/v1/runtime/performance-config` | 无 | 获取字段定义和值 |
| 使用 | PUT | `/api/v1/runtime/performance-config` | dynamic JSON dict | 保存，不应用 |
| 使用 | POST | `/api/v1/runtime/performance-config/apply` | `force=false` | 应用性能配置 |
| 使用 | GET | `/api/v1/runtime/topology-config` | 无 | 拓扑配置和值 |
| 使用 | PUT | `/api/v1/runtime/topology-config` | topology JSON | 保存，不应用 |
| 使用 | POST | `/api/v1/runtime/topology-config/apply` | 可选 camera selection body；`force=false` | 同步应用拓扑，高级兼容入口 |
| 使用 | POST | `/api/v1/runtime/topology-config/apply-async` | `{source_ids, disable_unselected}`；`force=false` | 快速启动的后台应用入口 |
| 使用 | GET | `/api/v1/runtime/topology-config/apply-status` | 无 | 恢复/轮询后台启动进度 |

`force=true` 会绕过 active evidence 保护语义，当前 UI 不主动发送 force；不要把它作为
普通重试按钮。

命名预设的快速启动顺序是：先 `PUT topology-config` 保存服务端 preset 和分片策略，
再 `POST apply-async` 原子应用所选 source。apply-status 的状态文件可跨浏览器刷新读取，
但执行线程不跨 API 进程重启存活。

`/runtime/latency` 当前返回 `status`、annotation media/write age、event/bundle DB lag、
active evidence 摘要、A/B queue/source age 和 10s/60s 阈值。它是在线 UI 信号，不是
pressure acceptance artifact。

## 8. 存储维护接口

Router：`services/api/app/routers/maintenance.py`

| 状态 | Method | Path | 用途 |
| --- | --- | --- | --- |
| 使用 | GET | `/api/v1/maintenance/storage/summary` | 容量、证据、人脸和 trash 统计 |
| 使用 | GET | `/api/v1/maintenance/execution-control` | 当前删除执行开关 |
| 使用 | PATCH | `/api/v1/maintenance/execution-control` | 修改执行开关 |
| 使用 | POST | `/api/v1/maintenance/evidence/delete-preview` | 证据删除预览 |
| 使用 | POST | `/api/v1/maintenance/evidence/delete` | 执行证据删除 |
| 使用 | POST | `/api/v1/maintenance/people/delete-preview` | 人员删除预览 |
| 使用 | POST | `/api/v1/maintenance/people/delete` | 执行人员删除 |
| 使用 | POST | `/api/v1/maintenance/people/gallery-delete-preview` | gallery 删除预览 |
| 使用 | POST | `/api/v1/maintenance/people/gallery-delete` | 执行 gallery 删除 |
| 可用 | POST | `/api/v1/maintenance/face-media/orphans-preview` | 孤儿媒体预览 |
| 可用 | POST | `/api/v1/maintenance/face-media/orphans-cleanup` | 孤儿媒体清理 |
| 可用 | GET | `/api/v1/maintenance/jobs/{job_id}` | job 分页详情 |

execute endpoint 前必须取得 preview 返回的 token/hash，详见数据合同。

## 9. WebSocket

API 代码声明：

```text
WS /api/v1/ws/alerts
```

消息来自 Redis `security.alerts`，并定期发送：

```json
{"message_type": "ping"}
```

当前前端没有创建 WebSocket，且 evidence-viewer 的通用 HTTP proxy 没有实现 WebSocket
upgrade 转发。因此不能假设 `ws://host:8090/api/v1/ws/alerts` 已可用。若新同事要做实时
告警，必须先增加并测试 8090 WebSocket proxy 或明确的受控入口。

## 10. Evidence Viewer 兼容接口

这些接口直接属于 `services/evidence-viewer/app/main.py`，不是内部 API proxy：

| 状态 | Method | Path | 说明 |
| --- | --- | --- | --- |
| 兼容 | GET | `/api/bundles` | 扫描 evidence 目录 |
| 兼容 | GET | `/api/bundles/{event_id}` | 文件 manifest |
| 兼容 | GET | `/api/bundles/{event_id}/annotations` | DB 优先/sidecar fallback 混合路径 |
| 兼容 | GET | `/api/bundles/{event_id}/sink-metadata` | metadata |
| 兼容 | GET | `/api/bundles/{event_id}/media/raw_clip` | 本地 FileResponse |
| 可用 | GET | `/health` | evidence-viewer 自身文件根健康 |

当前 `evidence.js` 不使用 `/api/bundles`。新增功能应保持 `/api/v1/evidence` 为唯一证据
查询合同，除非任务明确是维护兼容路径。

## 11. 不存在的 8090 路径

8090 当前不代理 FastAPI `/docs` 或 `/openapi.json`。交互式文档只在内部 `api:8000`
存在，不应写成客户/操作员入口。
