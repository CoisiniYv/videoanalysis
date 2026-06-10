# Storage Maintenance and Delete Plan

Date: 2026-06-10

Status: implementation contract.

## 1. 目标

当前 8090 操作台已经包含摄像头管理、人员与人脸、告警证据三个客户入口。下一步需要补齐一个受控的存储维护能力，解决以下问题：

1. 告警证据数量过多，需要支持按时间段删除、按筛选条件批量删除、手动多选删除。
2. 磁盘空间需要可视化和维护策略，支持按保留天数、最大占用、最小剩余空间做清理预览和执行。
3. 人员与人脸图库也需要删除能力，支持人员批量停用、图库照片批量停用、按登记时间清理，以及安全的图片文件清理。
4. 所有破坏性操作必须先预览，再确认执行，并留下审计记录。
5. 客户仍然只访问一个入口：`http://0.0.0.0:8090/`。

本计划同时作为第一版实现契约。第一版证据删除只删除磁盘媒体并保留业务记录；
删除后默认不会自动补录或重新生成证据。除非后续单独实现 restore/regenerate
能力，确认弹窗、执行结果和 job 详情都必须明确提示：

```text
删除后不会自动重新生成证据
```

## 2. 当前实现调查结论

### 2.1 8090 入口

当前 midterm 操作台由 `services/evidence-viewer` 对外提供 8090 端口：

```text
http://0.0.0.0:8090/
```

`services/evidence-viewer/app/main.py` 已经把同源 `/api/v1/*` 请求代理到内部 API：

```text
browser -> evidence-viewer:8090 -> api:8000
```

内部 API 没有 host 端口暴露，客户不需要也不应该直接访问 8000。

当前代理不是无条件透传。`services/evidence-viewer/app/main.py` 通过
`OPERATOR_PROXY_ALLOWED_PREFIXES` 限制允许的前缀，目前只有：

```text
cameras
algorithms
events
people
ws
```

因此维护 API 实施的第一步必须把 `maintenance` 加入这个 allowlist，并增加
静态/合约测试。否则 8090 页面调用 `/api/v1/maintenance/*` 会被 viewer 直接
拒绝，前端功能无法使用。

### 2.2 证据查看现状

证据接口目前在 `services/evidence-viewer` 内部实现：

```text
GET /api/bundles
GET /api/bundles/{event_id}
GET /api/bundles/{event_id}/annotations
GET /api/bundles/{event_id}/sink-metadata
GET /api/bundles/{event_id}/media/raw_clip
```

这些接口是文件型只读读取。`/health` 返回 `read_only: true`，compose 中 evidence-viewer 当前挂载：

```yaml
/data/video-analytics/media/evidence:/evidence:ro
```

这意味着直接在 evidence-viewer 里执行删除，需要把挂载改成 `rw`。不建议第一版这样做。

当前 midterm 证据目录必须固定为 flat bundle 布局：

```text
host:      /data/video-analytics/media/evidence/{event_id}/
viewer:    /evidence/{event_id}/
api:       /data/video-analytics/media/evidence/{event_id}/
producer:  media-worker EVIDENCE_OUTPUT_DIR=/media/evidence, output_dir={event_id}
```

第一版删除逻辑只扫描 `MEDIA_ROOT/evidence` 的第一层合法 bundle 目录。不要同时
模糊支持 `MEDIA_ROOT/evidence/events/{event_id}`，也不要把 `events/` 当成
event bundle。旧版 `events/{event_id}` 布局如果需要迁移，应单独做 legacy
迁移/清理任务，不混进 midterm 删除核心路径。

### 2.3 API 服务写权限

`infra/docker-compose.midterm.yml` 中 API 服务已有媒体目录读写权限：

```yaml
MEDIA_ROOT=/data/video-analytics/media
FACE_UPLOAD_ROOT=/data/video-analytics/media/face_uploads
FACE_REGISTRATION_ROOT=/data/video-analytics/media/face_registration
/data/video-analytics/media:/data/video-analytics/media:rw
```

所以删除和维护能力更适合放在内部 API 服务，由 8090 代理转发。这样：

1. 8090 仍是唯一客户入口。
2. 不需要把 evidence-viewer 的只读证据挂载改成读写。
3. API 可以同时处理文件和 PostgreSQL 标记。
4. 不涉及 ORT、AdaFace、YOLOv8-Face runtime 的重新下载或模型构建。

### 2.4 人脸库现状

当前人员与人脸接口在 `services/api/app/routers/people.py`：

```text
GET  /api/v1/people
GET  /api/v1/people/{person_id}
POST /api/v1/people/register-face
```

当前 repository 是读侧为主：

```text
services/api/app/repositories/people.py
```

人员和图库表已经有软删除基础字段：

```text
persons.is_active
person_gallery_embeddings.is_active
person_gallery_embeddings.is_primary
```

图库表同时保存图片路径：

```text
person_gallery_embeddings.source_image_path
person_gallery_embeddings.payload->>'registered_crop_path'
```

因此人脸删除的第一版应该默认做软删除，物理图片清理必须单独做引用检查。

### 2.5 既有媒体策略约束

`docs/media_output_directory_policy.md` 已经规定：

1. 清理策略优先删除 debug sink 和过期媒体。
2. 清理不能默认删除业务 DB 行。
3. 媒体文件删除后，业务记录应保留，并标记为 `media_expired` 或 `media_deleted`。
4. `events`、`face_observations`、`match_results`、`persons`、gallery records 默认不应被物理删除。

本计划沿用这个原则。

## 3. 总体架构选择

### 3.1 推荐方案

新增内部 API router：

```text
services/api/app/routers/maintenance.py
prefix: /api/v1/maintenance
```

8090 页面通过同源代理访问：

```text
http://0.0.0.0:8090/api/v1/maintenance/...
```

实现要求：同时修改 evidence-viewer 的代理 allowlist，把 `maintenance` 加入
`OPERATOR_PROXY_ALLOWED_PREFIXES`，并验证 8090 能转发
`/api/v1/maintenance/storage/summary`。这是 Midterm 的阻塞项。

证据浏览仍使用现有 8090 原生接口：

```text
/api/bundles
```

证据删除、存储统计、保留策略、人脸删除统一使用内部 API：

```text
/api/v1/maintenance/storage/summary
/api/v1/maintenance/evidence/...
/api/v1/maintenance/people/...
/api/v1/maintenance/face-media/...
```

### 3.2 为什么不直接改 evidence-viewer

不推荐把删除写进 evidence-viewer 的原因：

1. evidence-viewer 当前设计就是只读证据查看服务。
2. 当前 compose 对 evidence-viewer 使用 `:ro` 挂载，这是一个好的安全边界。
3. 删除证据后还需要更新 PostgreSQL 中 `events` / `evidence_tasks` 的媒体状态，API 已经有数据库连接。
4. API 已经持有 `/data/video-analytics/media:rw`，不需要增加新的写挂载。
5. 避免把客户页面服务变成直接文件删除服务。

### 3.3 对 8090 入口的影响

不会新增客户端口。用户仍通过：

```text
http://0.0.0.0:8090/
```

页面新增“存储维护”入口，内部调用 `/api/v1/maintenance/*`。API 仍然只在 compose 网络内暴露 8000。

## 4. 安全模型

### 4.1 默认先预览，再执行

所有删除类接口必须拆成两步：

1. `preview`: 只计算将要删除的对象、数量、大小、时间范围、跳过原因，绝不删除。
2. `execute`: 必须携带 preview 返回的 `preview_id` 和 `confirm_token`，并附带操作原因。

execute 不能按筛选条件重新计算候选集。preview 必须冻结候选快照，execute 只能
处理该快照里的 item。每个 item 至少保存：

```text
target_id
target_type
absolute_path
relative_path
resolved_path
size_bytes
mtime_ns
content_fingerprint
db_event_id
db_task_id
media_status_at_preview
eligibility_status
skip_reason
```

`content_fingerprint` 第一版可以使用 `size_bytes + mtime_ns + relative_path`
组合；对 `metadata.json` 可额外记录 sha256。execute 前必须逐项重校验：

1. 路径 resolve 后仍在允许根目录内。
2. 当前 size/mtime 与 preview 快照一致。
3. DB 状态没有从可删变成 pending/processing。
4. bundle 没有进入保护窗口或出现 lock 文件。
5. preview 没有过期。

任一重校验失败，该 item 必须跳过并记录 `stale_preview_item`、
`path_changed`、`db_state_changed` 或 `active_write_guard`，不能删除。

preview token 要有过期时间，建议 15 分钟：

```text
preview_expires_at = created_at + interval '15 minutes'
```

confirm token 必须和 `preview_id`、候选 hash、过期时间绑定。候选 hash 由冻结
item 快照排序后计算，execute 时用于确认“预览 A、执行 A”，防止“预览 A、
执行 B”。

前端必须显示：

```text
将删除的证据数量
预计释放空间
最早/最新事件时间
按类型和摄像头聚合的影响范围
跳过项数量和原因
删除模式：移入回收站 / 永久删除
```

前端确认弹窗也必须显示 preview 过期时间。preview 过期后，执行按钮禁用，用户
必须重新预览。

### 4.2 功能开关

后端维护能力按读、预览、执行三层开关控制：

```text
STORAGE_MAINTENANCE_SUMMARY_ENABLED=true
STORAGE_MAINTENANCE_PREVIEW_ENABLED=true
STORAGE_MAINTENANCE_EXECUTE_ENABLED=false
```

建议行为：

1. `STORAGE_MAINTENANCE_SUMMARY_ENABLED=true`: 允许只读容量统计和维护页面基础概览。
2. `STORAGE_MAINTENANCE_PREVIEW_ENABLED=true`: 允许生成 preview 和冻结候选快照。
3. `STORAGE_MAINTENANCE_EXECUTE_ENABLED=false`: 禁止真实删除，即使已有 preview。
4. 只有 execute enabled 时才允许移动到回收站或永久删除。

Midterm 默认组合必须是：

```text
STORAGE_MAINTENANCE_SUMMARY_ENABLED=true
STORAGE_MAINTENANCE_PREVIEW_ENABLED=true
STORAGE_MAINTENANCE_EXECUTE_ENABLED=false
```

这样页面能展示容量概览，也能做 preview，但不会执行任何删除。测试环境和 compose
env 应显式设置这三个值，避免实现后出现“页面存在但 preview 被默认禁用”的歧义。
如果部署方希望隐藏容量信息，可以单独关闭 `STORAGE_MAINTENANCE_SUMMARY_ENABLED`；
它不应被 execute 开关隐式关闭。

如果当前部署没有登录系统，至少要增加一个操作令牌：

```text
STORAGE_MAINTENANCE_OPERATOR_TOKEN=...
```

execute 类请求需要 `X-Operator-Token`。如果后续引入真实登录，再替换为角色权限。

### 4.3 路径安全

证据删除只允许删除证据根目录下的 event bundle：

```text
MEDIA_ROOT/evidence
```

midterm 第一版只支持：

```text
MEDIA_ROOT/evidence/{event_id}/
```

其中 `MEDIA_ROOT=/data/video-analytics/media`。`MEDIA_ROOT/evidence/events`
不是第一版扫描根，不允许作为同级兼容路径参与自动删除。

必须复用或等价实现 evidence-viewer 已有的安全规则：

1. `event_id` 只能是单段路径。
2. 禁止 `/`、`\`。
3. 禁止 `.` 和 `..` 作为完整路径段；不要禁止合法 event_id 中的普通点号。
4. 安全字符集以 evidence-viewer 的 `SAFE_EVENT_ID_RE` 为准：
   `^[A-Za-z0-9_.:-]+$`，避免和现有 event_id 规则冲突。
5. `resolve()` 后必须仍在 evidence root 内。
6. 禁止跟随 symlink 删除 evidence root 外部文件。

人脸图片删除只允许在以下目录内：

```text
MEDIA_ROOT/face_uploads
MEDIA_ROOT/face_registration
```

不能根据用户传入的任意路径删除文件。只能从 DB 中取路径，resolve 后校验在允许根目录内。

### 4.4 不删除运行中的输出

证据清理必须跳过可能还在写入的目录：

1. bundle 修改时间距离当前小于保护窗口，例如 10 分钟。
2. DB 中 `evidence_tasks.status` 仍为 `pending`、`claimed`、`processing` 或类似未完成状态。
3. bundle 中存在未来可加的 `.lock` 文件。
4. 当前 API 无法判断状态时，默认跳过最近修改的 bundle。

### 4.5 回收站与永久删除

需要区分两个动作：

1. 移入回收站：更安全，但不会释放磁盘空间。
2. 永久删除：释放磁盘空间，但不可恢复。

推荐回收站目录：

```text
MEDIA_ROOT/.trash/evidence/{job_id}/{event_id}/
MEDIA_ROOT/.trash/face/{job_id}/...
```

手动删除默认使用回收站。磁盘空间维护策略则先清空过期回收站；如果仍然低于空间目标，再对符合策略的证据执行永久删除。

## 5. 证据删除功能设计

### 5.1 删除范围

证据删除第一版默认只删除媒体文件和证据 bundle，不删除业务事件记录。

默认保留：

```text
events
evidence_tasks
face_observations
match_results
persons
person_gallery_embeddings
```

删除文件后，DB 中对应事件需要标记：

```text
events.media_status = 'media_deleted' 或 'media_expired'
events.payload.maintenance.deleted_at
events.payload.maintenance.deleted_by
events.payload.maintenance.delete_reason
events.payload.maintenance.delete_job_id
```

`evidence_tasks.status` 第一版不能写 `media_deleted`。当前 runtime 已经使用
`evidence_tasks.status` 判断 evidence task 生命周期和 record request gate，
现有状态主要是 `pending`、`not_implemented`、`ready`、`partial`、`failed`
等媒体任务状态。删除状态不应混入这个字段，除非先单独扩展状态机、枚举、展示语义
和 event-worker 测试。

第一版 task 侧只记录删除元数据，不改变原有 `status`：

```text
evidence_tasks.error_message = 'deleted_by_storage_maintenance:<job_id>'
evidence_tasks.metadata_path 保持原值或置入 result_payload 快照
evidence_tasks.output_root 保持原值或置入 result_payload 快照
maintenance_job_items.item_payload.evidence_task_status_before_delete
maintenance_job_items.item_payload.evidence_task_delete_marker
```

如果后续确实需要 `evidence_tasks.status='media_deleted'`，必须作为单独 migration
和 runtime 状态机变更实现，并覆盖 event-worker `get_evidence_task_status()`
调用路径，确认不会破坏“是否已有 evidence task / record_request gate”的判断。

如果某个 bundle 没有对应 DB 记录，也允许删除文件，但审计中标记为 `db_record_missing`。

### 5.1.1 删除后的列表语义

当前“告警证据”列表来自 file-based `/api/bundles`，它只扫描磁盘上的 bundle。
因此 bundle 被删除后，它会从现有证据列表消失，即使 `events` 和
`evidence_tasks` 仍在 DB 中保留。

第一版产品语义固定为：

1. `/api/bundles` 仍然只展示“当前磁盘上有可查看媒体的证据”。
2. 已删除媒体的历史事件不出现在默认“告警证据”列表。
3. “存储维护”页面提供维护历史/job 详情，可查看哪些 event 被删除。
4. DB 中事件通过 `media_status=media_deleted` 或 `media_expired` 保留审计。
5. 如果后续要在“告警证据”列表显示 `media_deleted` 历史行，需要另开一个
   DB-backed evidence list API，不能假装 file-based `/api/bundles` 能显示已删
   bundle。

也就是说，第一版删除后的客户可见结果是“录像证据从证据页移除”，不是“仍在证据页
显示一条不可播放记录”。该语义必须在确认弹窗中明确展示。

### 5.1.2 文件和 DB 的一致性策略

文件系统删除和 DB 更新无法天然组成一个事务。execute 必须以逐项幂等任务处理，
每个 item 有独立状态，不能只记录 job 总状态。

推荐逐项状态：

```text
planned
skipped
fs_move_started
fs_moved_to_trash
fs_delete_started
fs_deleted
db_update_started
db_updated
completed
failed_fs
failed_db
compensation_required
```

执行顺序：

1. 在 DB 中写入 item `planned`。
2. 对 item 做 preview 快照重校验。
3. 对 trash 模式，先把 bundle 原子 move/rename 到 `.trash`；跨文件系统时使用
   copy + fsync + rename + 校验 + 删除源目录。
4. 文件操作成功后，再更新 `events` / `evidence_tasks` 的 media 状态。
5. 最后写 item `completed`。

失败处理：

1. 文件移动成功、DB 更新失败：item 标记 `failed_db`，保留 trash 路径，下次
   execute 同一个 item 时只重试 DB 更新，不再重复移动文件。
2. DB 已标记、文件删除失败：item 标记 `failed_fs`，保留原路径和错误信息，下次
   可重试文件操作；如果 DB 已经是 media_deleted，重试不得报冲突。
3. item 已经 completed：重复执行必须返回 already_completed，不做二次删除。
4. 源 bundle 已不存在但 DB 已 media_deleted：视为 completed，并记录
   `source_missing_already_marked`。
5. 源 bundle 已不存在但 DB 未标记：标记 `compensation_required`，不得静默成功。

`maintenance_jobs.result_payload` 只存聚合结果；逐项明细应进入
`maintenance_job_items` 表或 JSONL 审计文件。第一版如果不建 item 表，也必须在
JSONL 中逐项记录，且 job 查询能返回失败/待补偿摘要。

### 5.2 时间段删除

支持按时间范围选择证据：

```text
time_from
time_to
timezone
```

时间来源优先级：

1. `metadata.json` 中的事件时间，例如 `event.start_ts`、`event.created_at`、`created_at`。
2. PostgreSQL `events.start_ts` / `events.created_at`。
3. `summary.json` 中的时间字段。
4. bundle 目录 mtime 作为 fallback，并在 preview 中标记 `time_source=mtime_fallback`。

前端用中文本地时间展示，后端接口接收带 timezone 的 ISO-8601 时间，并统一转成 UTC 比较。

### 5.3 批量删除

支持三种批量来源：

1. 页面多选的 `event_ids`。
2. 当前筛选条件对应的全部结果。
3. 时间范围和保留策略自动选出的候选项。

不能只删除前端当前页已加载的 200 条。preview 必须由后端按条件重新扫描完整候选集，并返回实际命中数量。

preview 完成后，execute 不再重新按这些筛选条件扫描。筛选条件只用于生成冻结
候选快照；真正删除的对象只来自该快照。

### 5.4 筛选维度

证据删除 preview 支持：

```text
event_ids
time_from / time_to
older_than_days
event_type
event_category
camera_id
source_id
clip_status
visual_evidence_status
has_raw_clip
```

告警分类沿用当前 8090 前端分类：

```text
identity: watchlist_hit, live_search_hit
perimeter: intrusion, wall_climb_suspicious
behavior: loitering, running, fall
crowd: crowd_gathering
```

### 5.5 API 草案

```text
GET /api/v1/maintenance/storage/summary
```

summary 是只读接口，默认应该可用。它只返回容量和分类统计，不生成 preview，不写
`maintenance_jobs`，不删除文件。

统计口径必须拆开，不能把 `media_root.used_bytes` 和 `evidence.total_bytes`
理解成同一个值：

1. `media_root`: 文件系统挂载点整体容量，来自 `statvfs(MEDIA_ROOT)`。
2. `evidence`: 当前 midterm flat bundle 根 `MEDIA_ROOT/evidence/{event_id}` 的证据占用，不包含 `.trash`。
3. `trash`: `MEDIA_ROOT/.trash` 占用，包含 evidence 和 face 回收站。
4. `face_media`: `FACE_UPLOAD_ROOT` 和 `FACE_REGISTRATION_ROOT` 占用。
5. `debug_sinks`: 已知 debug/临时输出，例如 `MEDIA_ROOT/replay-sink-output`、`MEDIA_ROOT/debug`、`MEDIA_ROOT/midterm-snapshots`。
6. `other_media`: `MEDIA_ROOT` 下不属于上述分类的剩余占用，便于发现未知大文件。

返回：

```json
{
  "media_root": {
    "total_bytes": 0,
    "used_bytes": 0,
    "free_bytes": 0,
    "free_percent": 0
  },
  "evidence": {
    "bundle_count": 0,
    "total_bytes": 0,
    "root": "MEDIA_ROOT/evidence",
    "layout": "flat_bundle",
    "oldest_event_time": null,
    "newest_event_time": null
  },
  "face_media": {
    "upload_bytes": 0,
    "registration_bytes": 0,
    "referenced_file_count": 0,
    "orphan_file_count": 0
  },
  "trash": {
    "bytes": 0,
    "item_count": 0
  },
  "debug_sinks": {
    "total_bytes": 0,
    "roots": {
      "replay_sink_output": 0,
      "debug": 0,
      "midterm_snapshots": 0
    }
  },
  "other_media": {
    "bytes": 0
  }
}
```

```text
POST /api/v1/maintenance/evidence/delete-preview
```

请求：

```json
{
  "event_ids": ["..."],
  "time_from": "2026-06-01T00:00:00+08:00",
  "time_to": "2026-06-10T23:59:59+08:00",
  "event_category": "identity",
  "camera_id": "camera-1",
  "delete_mode": "trash",
  "max_items": 1000
}
```

返回：

```json
{
  "preview_id": "...",
  "confirm_token": "...",
  "candidate_count": 120,
  "deletable_count": 118,
  "skipped_count": 2,
  "estimated_bytes": 987654321,
  "oldest_event_time": "...",
  "newest_event_time": "...",
  "groups": {
    "event_type": {},
    "camera_id": {}
  },
  "skipped": [
    {"event_id": "...", "reason": "task_pending"}
  ]
}
```

```text
POST /api/v1/maintenance/evidence/delete
```

请求：

```json
{
  "preview_id": "...",
  "confirm_token": "...",
  "delete_mode": "trash",
  "reason": "客户确认清理 6 月 1 日前证据",
  "operator": "operator"
}
```

```text
POST /api/v1/maintenance/evidence/retention-preview
POST /api/v1/maintenance/evidence/retention-run
GET  /api/v1/maintenance/jobs/{job_id}
```

`GET /api/v1/maintenance/jobs/{job_id}` 最小响应：

```json
{
  "job": {
    "job_id": "...",
    "job_type": "evidence_delete",
    "target_type": "evidence",
    "status": "completed",
    "delete_mode": "trash",
    "requested_by": "operator",
    "reason": "...",
    "created_at": "...",
    "started_at": "...",
    "finished_at": "...",
    "preview_expires_at": "...",
    "candidate_hash": "...",
    "summary": {
      "candidate_count": 120,
      "completed_count": 118,
      "skipped_count": 2,
      "failed_count": 0,
      "compensation_required_count": 0,
      "estimated_bytes": 987654321,
      "deleted_bytes": 987654321,
      "freed_bytes": 0,
      "trash_bytes_added": 987654321
    },
    "status_counts": {
      "completed": 118,
      "skipped": 2,
      "failed_fs": 0,
      "failed_db": 0,
      "compensation_required": 0
    }
  },
  "items": {
    "limit": 50,
    "offset": 0,
    "total": 120,
    "records": [
      {
        "item_id": 1,
        "target_type": "evidence",
        "target_id": "...",
        "status": "completed",
        "size_bytes": 123,
        "skip_reason": null,
        "error_message": null,
        "trash_path": null,
        "completed_at": "..."
      }
    ]
  },
  "attention_items": {
    "failed": [],
    "compensation_required": []
  }
}
```

items 必须支持分页参数：

```text
limit
offset
status
```

前端默认展示聚合和 `attention_items`，需要排查时再展开分页 items。

### 5.6 证据 UI

在 8090 页面新增“存储维护”导航项，中文页面，不展示内部路径。

证据清理页包含：

1. 顶部容量概览：媒体总空间、可用空间、证据占用、回收站占用。
2. 时间段筛选：开始时间、结束时间、快速选择“7 天前”“30 天前”“自定义”。
3. 告警分类：全部、名单布控、周界入侵、行为异常、聚集风险。
4. 摄像头筛选：按摄像头名称或 ID。
5. 删除模式：移入回收站、永久删除。
6. 预览按钮：先计算，不删除。
7. 预览结果弹窗：显示影响数量、大小、跳过原因。
8. 二次确认：输入确认文本或使用 preview token 才能执行。
9. 执行结果：显示 job 状态、成功数量、失败数量、释放空间。

在“告警证据”页增加快捷能力：

1. 每条证据左侧复选框。
2. 当前页全选。
3. “删除选中”按钮。
4. 跳转到存储维护页并带入选中的 event IDs。

## 6. 磁盘空间维护策略

### 6.1 策略参数

建议环境变量：

```text
STORAGE_RETENTION_DAYS=7
STORAGE_MAX_EVIDENCE_GB=500
STORAGE_MIN_FREE_PERCENT=15
STORAGE_DELETE_OLDEST_FIRST=true
STORAGE_TRASH_RETENTION_DAYS=7
STORAGE_MAINTENANCE_MAX_ITEMS_PER_RUN=1000
STORAGE_MAINTENANCE_MAX_BYTES_PER_RUN=107374182400
```

默认不启用自动定时删除：

```text
STORAGE_MAINTENANCE_SCHEDULE_ENABLED=false
```

第一版只做手动触发：

```text
preview -> execute
```

### 6.2 清理顺序

磁盘空间维护按以下顺序执行：

1. 清理过期回收站。
2. 清理 debug sink 输出，前提是路径明确且属于 debug 目录。
3. 清理超过保留天数的 evidence bundle。
4. 如果仍超过最大占用或低于最小剩余空间，按最旧优先继续清理 evidence。
5. 人脸图库图片不参与自动证据清理，只在人员/人脸维护中处理。

### 6.3 保留规则

默认跳过：

1. 最近 N 天证据。
2. 正在生成或最近修改的证据。
3. 被标记为重要或锁定的证据。
4. 无法确认路径安全的证据。
5. 无法计算大小或读取元数据失败且不在显式 event_ids 中的证据。

重要证据标记第一版可以不做 UI，但接口设计预留：

```text
events.payload.maintenance.pinned=true
```

### 6.4 释放空间计算

preview 返回 `estimated_bytes`。execute 后返回：

```text
deleted_bytes
trash_bytes_added
freed_bytes
```

注意：

1. 移入回收站不会真正释放空间，`freed_bytes=0`。
2. 永久删除才会释放空间。
3. 磁盘压力维护应该先 purge 过期回收站，再永久删除符合策略的旧证据。

## 7. 人员与人脸删除功能设计

### 7.1 删除语义

这里的人脸删除默认指“人员库和已登记图库”，不是历史识别观察数据。

默认不删除：

```text
face_observations
match_results
events
evidence_tasks
```

这样可以保留历史证据追溯。删除人员或图库后，只影响未来匹配。

### 7.2 人员删除

人员删除第一版做软删除：

```sql
UPDATE persons
SET is_active = false,
    updated_at = now(),
    updated_by = :operator,
    payload = jsonb_set(...)
WHERE id = :person_id;
```

同时停用该人员全部 active gallery：

```sql
UPDATE person_gallery_embeddings
SET is_active = false,
    is_primary = false,
    updated_at = now(),
    payload = jsonb_set(...)
WHERE person_id = :person_id
  AND is_active = true;
```

软删除后：

1. 默认人员列表不再显示该人员。
2. 未来 gallery match 不再使用该人员图库。
3. 历史证据仍保留原 match 结果和事件 payload。
4. 如果 UI 选择“显示停用人员”，仍可查看审计和历史信息。

### 7.3 图库照片删除

图库照片删除第一版也做软删除：

```sql
UPDATE person_gallery_embeddings
SET is_active = false,
    is_primary = false,
    updated_at = now(),
    payload = jsonb_set(...)
WHERE id = ANY(:gallery_embedding_ids);
```

如果删除的是 primary 图，第一版不自动提升其他图片为 primary，避免隐式改变客户布控语义。UI 提示“该人员暂无主图”，后续可由客户重新设置或重新注册。

### 7.4 按时间段清理人脸

支持按登记时间清理：

```text
created_from
created_to
older_than_days
```

适用对象：

1. 人员创建时间 `persons.created_at`。
2. 图库照片登记时间 `person_gallery_embeddings.created_at`。
3. 已停用人员或已停用图库照片的物理文件清理。

第一版建议只允许自动清理“已停用”的图库图片，避免误删仍在使用的布控照片。

### 7.5 图片物理清理

物理图片清理必须单独 preview：

```text
POST /api/v1/maintenance/face-media/orphans-preview
POST /api/v1/maintenance/face-media/orphans-cleanup
```

可删除图片必须满足：

1. 文件在 `FACE_UPLOAD_ROOT` 或 `FACE_REGISTRATION_ROOT` 内。
2. 默认没有任何 DB row 引用该路径，包括 active 和 inactive gallery row。
3. 没有其他人员或图库 row 引用相同路径。
4. 文件不是 symlink，或 symlink resolve 后仍在允许根目录内。

如果路径仍被任何 DB row 引用，默认跳过，并返回 `still_referenced`。

inactive 引用策略固定为：

1. 默认策略最保守：只要任何 `person_gallery_embeddings` row 的
   `source_image_path` 或 `payload.registered_crop_path` 引用该文件，不物理删除。
2. 只有显式请求 `allow_inactive_reference_cleanup=true`，并且该 row 已 inactive
   超过保留期，才允许把文件列为可清理候选。
3. 即使允许 inactive cleanup，也必须先把引用该文件的 inactive row 快照写入
   preview，并在 execute 前重校验这些 row 仍然 inactive。
4. active row 引用永远阻止物理删除，没有 override。

### 7.6 人脸 API 草案

```text
POST /api/v1/maintenance/people/delete-preview
POST /api/v1/maintenance/people/delete
```

请求：

```json
{
  "person_ids": [1, 2, 3],
  "external_person_ids": ["demo:001"],
  "include_gallery": true,
  "created_from": null,
  "created_to": null
}
```

```text
POST /api/v1/maintenance/people/gallery-delete-preview
POST /api/v1/maintenance/people/gallery-delete
```

请求：

```json
{
  "gallery_embedding_ids": [10, 11],
  "person_ids": [1],
  "created_to": "2026-06-01T00:00:00+08:00",
  "only_inactive": false
}
```

```text
POST /api/v1/maintenance/face-media/orphans-preview
POST /api/v1/maintenance/face-media/orphans-cleanup
```

请求：

```json
{
  "older_than_days": 7,
  "allow_inactive_reference_cleanup": false,
  "inactive_reference_retention_days": 30,
  "delete_mode": "trash"
}
```

### 7.7 人脸 UI

在“人员与人脸”页面增加：

1. 人员列表复选框。
2. 图库照片复选框。
3. “停用选中人员”。
4. “停用选中照片”。
5. “显示停用人员”开关。
6. “清理未引用图片”入口，跳转到存储维护页。

在“存储维护”页新增“人脸清理”tab：

1. 按登记时间筛选。
2. 按人员状态筛选：有效、停用、全部。
3. 按照片状态筛选：有效、停用、未引用。
4. 预览停用或物理清理影响。
5. 二次确认执行。

客户默认不看到 DB 字段、payload、内部路径。高级详情可以折叠显示 event ID、person ID、gallery ID，便于运维排查。

## 8. 数据库与审计设计

### 8.1 新增 migration

建议新增：

```text
db/migrations/013_storage_maintenance_audit.sql
```

### 8.2 维护任务表

新增 `maintenance_jobs`：

```sql
CREATE TABLE IF NOT EXISTS maintenance_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT,
    reason TEXT,
    delete_mode TEXT,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    preview_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    candidate_hash TEXT,
    preview_expires_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

索引：

```sql
CREATE INDEX IF NOT EXISTS maintenance_jobs_type_created_idx
    ON maintenance_jobs(job_type, created_at DESC);

CREATE INDEX IF NOT EXISTS maintenance_jobs_status_idx
    ON maintenance_jobs(status);
```

同时新增逐项明细表 `maintenance_job_items`，用于冻结 preview 快照、执行重校验、
幂等重试和失败补偿：

```sql
CREATE TABLE IF NOT EXISTS maintenance_job_items (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES maintenance_jobs(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    status TEXT NOT NULL,
    absolute_path TEXT,
    relative_path TEXT,
    resolved_path TEXT,
    size_bytes BIGINT,
    mtime_ns BIGINT,
    content_fingerprint TEXT,
    db_event_id UUID,
    db_task_id TEXT,
    media_status_at_preview TEXT,
    eligibility_status TEXT NOT NULL,
    skip_reason TEXT,
    trash_path TEXT,
    error_message TEXT,
    item_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (job_id, target_type, target_id)
);
```

索引：

```sql
CREATE INDEX IF NOT EXISTS maintenance_job_items_job_status_idx
    ON maintenance_job_items(job_id, status);

CREATE INDEX IF NOT EXISTS maintenance_job_items_target_idx
    ON maintenance_job_items(target_type, target_id);
```

preview 创建 `maintenance_jobs` 和 `maintenance_job_items`，execute 只能读取该
job 的 item 快照，不能重新扫描筛选条件。

`candidate_hash` 和 `preview_expires_at` 必须是 `maintenance_jobs` 的显式列，
不能只藏在 JSON payload 里。execute 必须直接读取并校验这两个字段：

1. 请求携带的 confirm token 必须绑定 `job_id`、`candidate_hash` 和
   `preview_expires_at`。
2. 当前时间超过 `preview_expires_at` 时拒绝执行。
3. 重新计算 item 快照 hash 与 `candidate_hash` 不一致时拒绝执行。

### 8.3 删除标记字段

可以选择加显式列：

```sql
ALTER TABLE persons
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT,
    ADD COLUMN IF NOT EXISTS delete_reason TEXT;

ALTER TABLE person_gallery_embeddings
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT,
    ADD COLUMN IF NOT EXISTS delete_reason TEXT;
```

如果希望 migration 更小，也可以先写入 `payload.maintenance`。但显式列更利于查询、筛选和审计。

### 8.4 审计报告文件

每次执行删除还应写一份 JSONL 报告：

```text
/data/video-analytics/artifacts/maintenance/{job_id}.jsonl
```

每行记录一个对象：

```json
{
  "job_id": "...",
  "target_type": "evidence",
  "target_id": "...",
  "action": "trash",
  "bytes": 123,
  "status": "deleted",
  "reason": null
}
```

这样即使前端或数据库查询异常，也能用文件审计实际删除结果。

## 9. 后端实现拆分

### 9.1 新增模块

建议新增：

```text
services/api/app/routers/maintenance.py
services/api/app/services/storage_maintenance.py
services/api/app/repositories/maintenance.py
services/api/app/schemas/maintenance.py
```

职责：

1. router: FastAPI endpoint、权限开关、请求响应。
2. service: 证据扫描、路径安全、大小计算、删除执行、回收站处理。
3. repository: maintenance_jobs、events、evidence_tasks、persons、gallery 更新。
4. schemas: preview/delete/summary 请求响应模型。

### 9.2 复用路径安全

可以从 `services/evidence-viewer/app/evidence_index.py` 复制或抽出以下规则：

```text
SAFE_EVENT_ID_RE
safe_bundle_dir
safe_child_path
resolve_root
```

如果抽公共库，建议放到：

```text
libs/media_maintenance/path_safety.py
```

第一版为降低改动面，可以在 API service 内实现等价函数，并用测试覆盖 traversal、symlink、unsafe id。

### 9.3 大批量任务处理

第一版可以同步执行，但必须限制单次规模：

```text
max_items
max_bytes
request_timeout
```

如果 preview 命中超过限制，返回：

```text
too_many_candidates
```

让用户缩小时间段或使用 retention 策略分批执行。

后续如果需要真正异步，可以把 `maintenance_jobs` 扩展为后台任务，由 API 返回 job id 并轮询状态。

## 10. 前端实现拆分

### 10.1 页面结构

`services/evidence-viewer/app/static/index.html`：

1. 左侧导航新增“存储维护”。
2. 新增 `maintenance-view`。
3. 证据列表和人员列表增加选择框。
4. 新增确认弹窗或确认面板。

`services/evidence-viewer/app/static/operator.js`：

1. 管理人员、人脸选择状态。
2. 调用 `/api/v1/maintenance/people/*`。
3. 与 summary card 同步人数和图库数量。

`services/evidence-viewer/app/static/evidence.js`：

1. 管理证据多选状态。
2. 将选中 event_ids 传给维护页。
3. 删除成功后刷新 `/api/bundles`。

建议新增：

```text
services/evidence-viewer/app/static/maintenance.js
```

职责：

1. 加载容量统计。
2. 证据清理 preview/execute。
3. 人脸清理 preview/execute。
4. job 查询与结果展示。

### 10.2 客户展示原则

页面展示中文业务字段：

```text
证据数量
预计释放空间
告警类型
摄像头
时间范围
人员姓名
人员编号
图库照片
删除原因
```

默认不展示：

```text
MEDIA_ROOT
FACE_UPLOAD_ROOT
FACE_REGISTRATION_ROOT
payload JSON
embedding vector
内部容器地址
```

## 11. 测试计划

### 11.1 单元测试

新增测试：

```text
harness/tests/test_storage_maintenance_path_safety.py
harness/tests/test_storage_maintenance_evidence_preview.py
harness/tests/test_storage_maintenance_face_preview.py
```

覆盖：

1. event_id traversal 被拒绝。
2. symlink escape 被拒绝。
3. 只扫描合法 bundle。
4. 当前 midterm 只扫描 `MEDIA_ROOT/evidence/{event_id}` 第一层 bundle。
5. `MEDIA_ROOT/evidence/events` 不会被当作兼容根自动扫描。
6. 时间范围筛选正确。
7. 大小统计正确。
8. 最近修改和 pending task 被跳过。
9. preview 快照保存 size、mtime、fingerprint、候选 hash。
10. execute 前 size/mtime 变化会跳过 item。
11. execute 不按筛选条件重新扫描候选。
12. 人脸图片只允许清理允许根目录内文件。
13. 任何 DB row 引用的图片默认不会被物理删除。
14. inactive 引用只有显式允许且超过保留期才可进入候选。

### 11.2 API 合约测试

新增：

```text
harness/tests/test_api_storage_maintenance.py
harness/tests/test_api_people_delete_maintenance.py
```

覆盖：

1. execute disabled 时只允许 preview。
2. preview 不改 DB、不删文件。
3. confirm_token 错误时拒绝执行。
4. preview 过期时拒绝 execute。
5. candidate hash 不匹配时拒绝 execute。
6. stale preview item 被跳过并记录原因。
7. evidence delete 后文件进入 trash 或被永久删除。
8. events.media_status 被标记为 media_deleted 或 media_expired。
9. evidence_tasks.status 保持原状态，删除信息写入 task 元数据或 maintenance item。
10. 文件成功移动但 DB 更新失败时 item 可重试。
11. DB 已标记但文件删除失败时 item 可重试。
12. completed item 重复执行返回 already_completed。
13. person delete 后 `persons.is_active=false`。
14. gallery delete 后 `person_gallery_embeddings.is_active=false` 且 `is_primary=false`。
15. orphan cleanup 跳过仍引用文件。

### 11.3 前端静态测试

更新或新增：

```text
harness/tests/test_operator_storage_maintenance_static.py
```

覆盖：

1. 8090 页面存在“存储维护”导航。
2. 页面没有暴露 8000 host 地址。
3. evidence-viewer 代理 allowlist 包含 `maintenance`。
4. 删除按钮必须走 preview 流程。
5. preview 过期后执行按钮禁用。
6. 中文文案存在。
7. `maintenance.js` 语法检查通过。

### 11.4 手工 smoke

建议新增：

```text
scripts/smoke/current/check_storage_maintenance_preview.sh
scripts/smoke/current/check_storage_maintenance_delete_tmp.sh
```

第一条只跑 preview，不删真实数据。

第二条使用临时 media root 或临时 bundle，验证：

1. 创建测试证据 bundle。
2. preview 命中。
3. execute 移入 trash。
4. 再次列表不出现。
5. 审计 job 可查询。

### 11.5 验证命令

实施完成后建议跑：

```bash
python -m pytest \
  harness/tests/test_storage_maintenance_path_safety.py \
  harness/tests/test_storage_maintenance_evidence_preview.py \
  harness/tests/test_storage_maintenance_face_preview.py \
  harness/tests/test_api_storage_maintenance.py \
  harness/tests/test_api_people_delete_maintenance.py \
  harness/tests/test_operator_storage_maintenance_static.py \
  -q

python -m py_compile \
  services/api/app/routers/maintenance.py \
  services/api/app/services/storage_maintenance.py \
  services/api/app/repositories/maintenance.py \
  services/api/app/schemas/maintenance.py

node --check services/evidence-viewer/app/static/operator.js
node --check services/evidence-viewer/app/static/evidence.js
node --check services/evidence-viewer/app/static/maintenance.js

docker compose -f infra/docker-compose.midterm.yml config
git diff --check
```

真实删除 smoke 必须只对测试 bundle 或用户明确指定的 event ids 执行。

## 12. 实施阶段

### Step 0: 文档确认

当前阶段。确认：

1. 删除 API 放在内部 API，通过 8090 代理。
2. evidence-viewer 继续只读。
3. evidence-viewer proxy allowlist 必须新增 `maintenance`。
4. midterm 证据根固定为 `MEDIA_ROOT/evidence/{event_id}`。
5. 不把 `MEDIA_ROOT/evidence/events` 混入第一版扫描根。
6. 证据删除默认不删 DB 业务行。
7. 删除后的证据默认从 file-based 证据列表消失。
8. preview 必须冻结候选快照，execute 只能处理快照 item。
9. execute 必须有过期、candidate hash、mtime/size 重校验。
10. 文件和 DB 更新必须逐项幂等。
11. `events.media_status` 可以使用 `media_deleted` / `media_expired`。
12. `evidence_tasks.status` 第一版不写 `media_deleted`。
13. 人脸删除默认软删除。
14. 物理清理必须 preview 和引用检查。

### Midterm: 只读统计和 preview

默认开关组合：

```text
STORAGE_MAINTENANCE_SUMMARY_ENABLED=true
STORAGE_MAINTENANCE_PREVIEW_ENABLED=true
STORAGE_MAINTENANCE_EXECUTE_ENABLED=false
```

实现：

1. evidence-viewer proxy allowlist 放行 `maintenance`。
2. `/api/v1/maintenance/storage/summary`
3. maintenance_jobs / maintenance_job_items preview 写入。
4. evidence delete preview，冻结候选快照。
5. people/gallery delete preview。
6. orphan media preview。
7. 前端“存储维护”只读页面。

不执行任何删除。

### Step 2: 证据删除执行

实现：

1. evidence delete execute。
2. trash/permanent 两种模式。
3. `events.media_status` 标记 `media_deleted` / `media_expired`。
4. `evidence_tasks.status` 保持原状态，删除信息写入元数据和 maintenance item。
5. maintenance_jobs 审计。
6. JSONL 报告。
7. UI 证据批量选择和删除确认。

### Step 3: 磁盘维护策略

实现：

1. retention preview。
2. retention manual run。
3. purge expired trash。
4. max evidence bytes / min free percent 计算。
5. oldest first 清理。

第一版不启用自动定时任务，只提供手动运行。

### Step 4: 人员与图库软删除

实现：

1. 批量停用人员。
2. 批量停用图库照片。
3. 人员页面复选框。
4. 显示停用人员开关。
5. 删除后未来 match 不再使用 inactive gallery。

### Step 5: 人脸图片物理清理

实现：

1. orphan preview。
2. orphan cleanup。
3. 引用检查。
4. trash/permanent 模式。
5. UI 显示预计释放空间。

### Step 6: 自动维护预留

后续如需要，再启用：

1. 定时任务。
2. 每日凌晨清理。
3. 空间低于阈值自动清理。
4. 告警通知。

自动维护不建议第一版默认开启。

## 13. 验收标准

功能验收：

1. 客户只访问 `http://0.0.0.0:8090/`。
2. 页面能看到存储容量、证据占用、人脸图片占用、回收站占用。
3. 能按时间段预览证据删除，返回数量、大小、跳过原因。
4. 能批量选择证据并删除。
5. 删除证据后，业务事件记录仍保留，媒体状态被标记。
6. 能按保留策略预览磁盘清理。
7. 能批量停用人员和图库照片。
8. 停用图库后，未来识别不再使用该 gallery row。
9. 能预览和清理未引用的人脸图片。
10. 所有执行动作都有 `maintenance_jobs` 和 JSONL 审计。

安全验收：

1. 未开启 execute env 时不能删除。
2. confirm_token 错误不能删除。
3. 路径穿越不能删除。
4. symlink escape 不能删除。
5. 正在生成或最近修改的证据被跳过。
6. 被 DB 引用的人脸图片不会被物理删除。
7. UI 不展示内部 8000 地址和内部根路径。

回归验收：

1. 摄像头管理仍正常。
2. 人脸注册仍正常。
3. 告警证据播放仍正常。
4. `/api/bundles` 只读查看接口仍正常。
5. 不需要重新下载 ORT。
6. 不需要把 API host 端口暴露给客户。

## 14. 风险与待确认问题

### 14.1 没有登录系统时的删除权限

当前 8090 是 LAN 可访问入口。如果没有登录和角色权限，直接开放删除会有风险。建议至少使用：

```text
STORAGE_MAINTENANCE_OPERATOR_TOKEN
```

后续再接入真实用户体系。

### 14.2 证据目录布局不能混用

历史文档提到过：

```text
MEDIA_ROOT/evidence/events/{event_id}
```

但当前 midterm compose 和 media-worker 产出已经固定为：

```text
MEDIA_ROOT/evidence/{event_id}
```

当前 evidence-viewer 也按 `EVIDENCE_ROOT` 第一层目录扫描。第一版维护服务必须以
这个 flat bundle 布局为唯一删除根，不做双布局兼容。旧 `events/{event_id}`
如果后续需要处理，应单独设计 legacy migration/cleanup，不进入本功能第一版。

### 14.3 回收站不释放空间

客户如果目标是“马上释放磁盘”，移入回收站不够。磁盘维护必须提供永久删除模式，并先做审计报告。

### 14.4 是否允许彻底删除历史业务记录

本计划默认不删除 `events`、`face_observations`、`match_results`。如果后续有合规要求需要彻底擦除历史记录，应单独设计“数据擦除”功能，不能混入普通磁盘维护。

### 14.5 人员删除是否保留历史身份显示

默认保留历史证据和 match 结果，所以旧证据里仍可能显示当时命中的人员信息。这是审计友好的行为。如果业务要求“人员删除后旧证据也匿名化”，需要单独设计历史匿名化。

## 15. 推荐下一步

建议按以下顺序开始实现：

1. 先做 Midterm，只读 summary 和 preview，让用户确认筛选结果可信。
2. 再做 Phase 2，只对测试 bundle 验证 evidence delete。
3. 然后接入 8090 中文 UI。
4. 最后做人脸软删除和 orphan media cleanup。

第一轮实现不启用自动定时清理，不对真实证据执行永久删除，除非用户明确给出 event ids 和确认范围。
