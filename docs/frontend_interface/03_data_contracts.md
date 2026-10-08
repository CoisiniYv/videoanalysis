# 前端数据合同

本文件记录当前 UI 实际依赖的字段和判定，不代表数据库全部列。

## 1. JSON Response Envelope

绝大多数 `/api/v1/*` 正常响应：

```ts
type ApiEnvelope<T> = {
  data: T;
  error: null;
  request_id: string;
};
```

错误：

```ts
type ApiErrorEnvelope = {
  data: null;
  error: {
    message: string;
    code: number;
    details?: unknown;
  };
  request_id: string | null;
};
```

例外：

- camera config export 返回 YAML；
- camera preview 返回 JPEG；
- `/media/*` 返回媒体 bytes；
- FastAPI validation 可能返回 `{detail: [...]}`；
- evidence-viewer `/api/bundles/*` 兼容接口不一定包 envelope。

## 2. 标识符合同

| 字段 | 含义 | 前端规则 |
| --- | --- | --- |
| `camera_id` / camera `id` | 摄像头数据库/配置 ID | 路由和精确过滤使用，不默认展示 |
| `source_id` | 流水线内部 source ID | 诊断和查询兼容，默认转成 camera name |
| `camera_name` / camera `name` | 可读摄像头名 | Operator 默认展示字段 |
| `event_id` | PostgreSQL event UUID | 证据详情、删除和内部 deep link |
| `source_event_id` | 上游幂等事件 ID | 兼容查找/诊断，不替代 event UUID |
| `person_id` | 系统人员整数 ID | 人员路由和轨迹查询 |
| `external_person_id` | 业务人员编号 | 可读搜索/注册字段 |
| `source_observation_id` | 人脸 observation 身份 | 轨迹去重和证据 identity 关联 |
| `frame_uuid` | 视觉帧身份 | overlay 首选匹配 |
| `frame_pts` | 媒体时间域位置 | overlay 次级匹配和窗口计算 |
| `runtime_epoch_id` | 运行代际 | 诊断/隔离；前端不得自行跨 epoch 合并 |
| `stream_session_id` | 单流会话 | timeline/annotation 隔离 |

用户界面展示优先级：

```text
camera_name -> camera lookup name -> source_id -> camera_id -> 未知摄像头
```

## 3. Camera Contracts

### 3.1 Camera create

```ts
type CameraCreate = {
  id: string;
  source_id: string;
  name: string;
  rtsp_url: string;
  site_id?: string | null;
  location?: string | null;
  gpu_id?: number;                 // default 0
  enabled?: boolean;               // default true
  input_type?: string;             // current UI "rtsp"
  rtsp_transport?: string;         // current UI "tcp"
  fps_policy?: Record<string, unknown>;
  alert_policy?: Record<string, unknown>;
};
```

当前 UI 新增 camera id 使用 UUID-compatible 值，`source_id` 从该 ID 生成。不要改回
可读名称充当内部 ID；可读内容放 `name`。

### 3.2 Camera response

```ts
type Camera = CameraCreate & {
  created_at?: string | null;
  updated_at?: string | null;
  runtime_source_apply?: {
    ok?: boolean;
    result?: unknown;
    error?: string;
    skipped?: string;
  };
};
```

create/update/enable/disable 可能附带 `runtime_source_apply`，页面必须分别展示“数据库
保存成功”和“运行源应用结果”，不能把两者合成一个隐含成功。

### 3.3 Zone

```ts
type Zone = {
  id?: string | number;
  camera_id?: string;
  zone_id: string;
  zone_name: string;
  zone_type: "polygon" | "line" | "direction_line" | string;
  coordinate_space: "pixel" | string;
  points: number[][];
  enabled: boolean;
  payload?: Record<string, unknown>;
};
```

当前 ROI editor：

- polygon 至少 3 点，最多 10 点；
- line 至少 2 点；
- 保存为 pixel coordinates；
- `bind_rules=true` 仅在保存最终 polygon ROI 时使用。

### 3.4 Algorithm rule

```ts
type EvidencePolicy = {
  snapshot_required: boolean;
  clip_required: boolean;
  pre_seconds: number;              // schema 0..300
  post_seconds: number;             // schema 0..300
  [key: string]: unknown;
};

type AlgorithmRule = {
  id?: number;
  rule_id: string;
  camera_id?: string;
  algorithm_id: string;
  algorithm_type?: string;
  family_algorithm_id?: string;
  enabled: boolean;
  zone_id?: string | null;
  line_id?: string | null;
  severity?: string;                // default medium
  config: Record<string, unknown>;
  evidence_policy: EvidencePolicy;
};
```

`config` 是 algorithm-specific。UI 必须以 `/algorithms` 的 `config_schema` 和现有
support matrix 为依据，不应把某算法字段推广为全局字段。

## 4. People And Gallery Contracts

### 4.1 People list

```ts
type PersonSummary = {
  person_id: number;
  name: string;
  external_person_id?: string | null;
  description?: string | null;
  is_active: boolean;
  active_gallery_count: number;
  primary_gallery_embedding_id?: number | null;
  primary_source_image_path?: string | null;
  primary_source_image_url?: string | null;
  primary_registered_crop_path?: string | null;
  primary_registered_crop_url?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

type PeopleList = {
  people: PersonSummary[];
  total: number;
  limit: number;
  offset: number;
};
```

人员列表缩略图优先：

```text
primary_registered_crop_url -> primary_source_image_url
```

### 4.2 Person detail and gallery

```ts
type PersonDetailResponse = {
  person: {
    person_id: number;
    name: string;
    external_person_id?: string | null;
    description?: string | null;
    is_active: boolean;
    payload: Record<string, unknown>;
    created_at?: string | null;
    updated_at?: string | null;
  };
  gallery: GalleryEmbedding[];
};

type GalleryEmbedding = {
  gallery_embedding_id: number;
  person_id: number;
  source_type: string;
  source_image_path?: string | null;
  source_image_url?: string | null;
  registered_crop_path?: string | null;
  registered_crop_url?: string | null;
  source_observation_id?: string | null;
  embedding_model: string;
  model_version?: string | null;
  embedding_dim: number;
  embedding_norm?: number | null;
  quality?: number | null;
  face_bbox?: unknown[] | null;
  landmarks?: unknown[] | null;
  is_primary: boolean;
  is_active: boolean;
  payload: Record<string, unknown>;
};
```

UI gallery 图片优先：

```text
registered_crop_url -> source_image_url
```

### 4.3 Batch face registration

```ts
type FaceRegistrationBatchItem = {
  index: number;
  filename: string;
  status: "REGISTERED" | "FAILED";
  gallery_embedding_id?: number | null;
  is_primary: boolean;
  quality?: number | null;
  error_code?: string | null;
  error_message?: string | null;
};

type FaceRegistrationBatchResult = {
  status: "REGISTERED" | "PARTIAL" | "FAILED";
  person_id?: number | null;
  person_reused: boolean;
  external_person_id?: string | null;
  name?: string | null;
  registered_count: number;
  failed_count: number;
  items: FaceRegistrationBatchItem[];
  warnings: string[];
};
```

`POST /api/v1/people/register-faces` 对部分成功返回 HTTP `207`；前端不能只按 HTTP
状态判断失败，必须渲染 `items` 中每张图片的结果。

## 5. Trajectory Contract

### 5.1 Response

```ts
type TrajectoryResponse = {
  person: Record<string, unknown>;
  trajectory: TrajectoryRow[];
  limit: number;
  offset: number;
  returned_count: number;
  has_more: boolean;
  mode: "persisted_trajectory";
};
```

API 内部读取 `limit + 1` 行来计算 `has_more`，但只返回 `limit` 行。前端下一页按钮
应只看 `has_more`，不以 `rows.length === limit` 自行推断；当前代码只在兼容旧响应时
使用长度 fallback。

### 5.2 Row

```ts
type TrajectoryRow = {
  event_id?: string | null;
  source_event_id?: string | null;
  event_type: string;
  person_id: number;
  person_name: string;
  external_person_id?: string | null;
  camera_id?: string | null;
  source_id?: string | null;
  camera_name?: string | null;
  event_ts_ms?: number | null;
  event_created_at?: string | null;
  similarity?: number | null;
  source_observation_id: string;
  observation_timestamp_ms?: number | null;
  face_bbox?: unknown;
  person_bbox?: unknown;
  face_crop_uri?: string | null;
  full_frame_uri?: string | null;
  annotated_frame_uri?: string | null;
  face_crop_url?: string | null;
  full_frame_url?: string | null;
  annotated_frame_url?: string | null;
  trajectory_thumbnail_url?: string | null;
  evidence_media_status?: string | null;
  evidence_summary?: Record<string, unknown>;
  trajectory_source: "watchlist_event" | "gallery_observation" | string;
  playback_kind: "image";
  result_mode: "latest_camera_hit";
};
```

列表图片优先：

```text
trajectory_thumbnail_url -> face_crop_url -> annotated_frame_url -> full_frame_url
```

详情图片优先：

```text
annotated_frame_url -> full_frame_url -> face_crop_url -> trajectory_thumbnail_url
```

详情加载失败时只尝试一次 thumbnail fallback。

数据来源：persisted endpoint 只从 `watchlist_hit` / `live_search_hit` 事件及其关联
observation/evidence artifact 读取，且按 `source_observation_id` 去重；不执行
`observation_matches` 向量搜索分支。

## 6. Evidence List Contract

```ts
type EvidenceBundleSummary = {
  event_id: string;
  source_event_id: string;
  event_type: string;
  source_id: string;
  camera_id: string;
  camera_name?: string | null;
  alarm_machine_time?: string | null;
  alarm_machine_time_source?: string | null;
  playback_kind: "video" | "image" | string;
  raw_clip_available: boolean;
  raw_clip_name?: string | null;
  raw_clip_url?: string | null;
  image_available: boolean;
  face_crop_url?: string | null;
  full_frame_url?: string | null;
  annotated_frame_url?: string | null;
  annotations_available: boolean;
  annotation_lines?: number | null;
  clip_status: string;
  media_status?: string | null;
  evidence_state?: string | null;
  evidence_reason?: string | null;
  materialization_status?: string | null;
  materialization_reason?: string | null;
  materialization_deadline_at?: string | null;
  visual_evidence_status?: string | null;
  frontend_overlay_required?: boolean | null;
  matched_objects?: number | null;
  unknown_objects?: number | null;
  person_id?: number | null;
  person_name?: string | null;
  external_person_id?: string | null;
  source_observation_id?: string | null;
  person_track_id?: string | null;
  warnings: string[];
  index_source: "database";
};
```

列表 wrapper：

```ts
type EvidenceBundleList = {
  bundles: EvidenceBundleSummary[];
  total: number;
  limit: number;
  offset: number;
  index_source: "database";
  warnings: string[];
};
```

## 7. Evidence Manifest And Playability

manifest 的前端关键字段：

```ts
type EvidenceManifest = {
  event_id: string;
  playback_kind: "video" | "image" | string;
  image_available: boolean;
  face_crop_url?: string | null;
  full_frame_url?: string | null;
  annotated_frame_url?: string | null;
  raw_clip_url?: string | null;
  raw_clip_name?: string | null;
  raw_clip_unavailable_reason?: string | null;
  annotations_url: string;
  sink_metadata_url: string;
  materialization_status?: string | null;
  materialization_reason?: string | null;
  metadata: {
    event: Record<string, unknown>;
    media: Record<string, unknown>;
    annotations: Record<string, unknown>;
  };
  summary: Record<string, unknown>;
  warnings: string[];
  index_source: "database";
};
```

前端不可通过 event id 拼 `/media/evidence/<id>/raw_clip.mov`。可播放依据是后端返回：

```text
video: playback_kind != image AND raw_clip_url != null
image: playback_kind == image AND one returned image URL exists
```

后端将以下状态判为 raw clip unavailable：

```text
duration_guard_failed
failed
manifest_ready
materialization_pending
materializing
materialization_deferred
materialization_failed
materialization_expired
materialization_skipped
media_deleted
media_expired
not_implemented
pending
queued
waiting_proof
replaying
finalizing
```

Worker 优化如新增状态，必须同步确认该状态是否可播放，并更新 API 与前端标签测试。

## 8. Annotation And Timeline Contracts

### 8.1 Annotations response

```ts
type EvidenceAnnotations = {
  event_id: string;
  count: number;
  raw_count: number;
  records: AnnotationRecord[];
  annotations: AnnotationRecord[];    // same records compatibility alias
  annotation_source: "database" | "filesystem";
  annotation_source_kind: string;
  fallback_used: boolean;
  fallback_reason?: string | null;
  production_ready?: boolean | null;
  timeline_domain?: string | null;
  warnings: string[];
  index_source: "database" | "filesystem";
};
```

`displayable=false` records 会被后端过滤。Overlay record 的核心字段不是单独 Pydantic
schema，而是 DB `evidence_overlay_segments.record` 原始 JSON。当前 renderer 依赖：

```text
clip_frame_index
frame_uuid / uuid
frame_pts / pts
t_ms / time_offset_ms
objects[]
displayable
```

object 常用字段：

```text
bbox / detection_box / box
label / class / object_type / role
track_id / person_id / external_person_id / name
confidence / similarity
landmarks
trajectory / points
style / color
displayable
```

### 8.2 Sink metadata response

```ts
type SinkMetadataResponse = {
  event_id: string;
  count: number;
  records: TimelineRecord[];
  warnings: string[];
  fallback_used: boolean;
  fallback_reason?: string | null;
  index_source: "database" | "filesystem";
};
```

timeline record 至少可能包含：

```text
clip_frame_index
frame_uuid
frame_pts / pts
frame_dts / dts
duration
timestamp_ms
width / height
source_id / camera_id
stream_session_id
keyframe_uuid
其他原 metadata 字段
```

Overlay 匹配规则必须继续以 final clip frame identity 为基础：frame UUID 优先，PTS 次级，
只在既有严格容差内使用 fallback。不要用 wall-clock 或宽泛 hold window 替代。

## 9. Evidence Status Labels

证据页面当前将以下状态映射为用户标签：

| State | Label |
| --- | --- |
| `pending` | 待处理 |
| `waiting_proof` | 生成中 |
| `queued` | 生成中 |
| `replaying` | 生成中 |
| `finalizing` | 生成中 |
| `image_pending` | 图片生成中 |
| `image_ready` | 图片就绪 |
| `image_missing` | 图片缺失 |
| `materialized` / `ready` | 可查看 |
| `failed` | 生成失败，附 reason |

Runtime 页面保留更细的 `等待帧证明`、`Replay 中`、`已跳过` 等标签。未知状态不会
导致异常，会回显原始字符串；但产品可读性仍要求新状态同步增加标签。

## 10. Maintenance Preview/Execute Contract

preview 通用关键字段：

```ts
type MaintenancePreview = {
  preview_id: string;
  confirm_token: string;
  candidate_hash?: string | null;
  preview_expires_at?: string | null;
  delete_mode?: "trash" | "delete" | string;
  candidate_count: number;
  deletable_count: number;
  skipped_count: number;
  estimated_bytes: number;
  skipped: Array<{target_id?: string; reason?: string}>;
};
```

execute body：

```ts
type MaintenanceExecute = {
  preview_id: string;
  confirm_token: string;
  candidate_hash?: string | null;
  reason: string;
  operator: string;
  delete_mode?: string;              // evidence/face media as applicable
};
```

以下任一变化都应要求重新 preview：

- preview 过期；
- candidate hash 变化；
- 文件 path/stat 变化；
- DB state 变化；
- active task 状态变化。

前端展示的 skipped reason 必须来自后端稳定 reason code，再映射为中文；不要从自由文本
判断是否允许删除。

## 11. Runtime Config Contracts

Performance GET 返回动态 `fields[]`，UI 按每个 field 的：

```text
key
kind
default
```

读取表单并构造 PUT body。不要在前端复制一套固定 performance schema。

Topology PUT 当前构造：

```ts
type RuntimeTopologyBody = {
  topology_mode: string;
  shard_strategy: string;
  streams_per_branch: number;
  branches: {
    a: Record<string, string | number>;
    b: Record<string, string | number>;
  };
  manual_assignments: Record<string, "a" | "b">;
};
```

每个 branch 当前可能包含：

```text
gpu_id
savant_batch_size
pose_batch_size
face_detector_batch_size
face_embedding_batch_size
max_parallel_streams
analysis_fps
analysis_min_fps
savant_max_fps
savant_min_fps
batched_push_timeout
```

保存与应用是两个动作；apply 可能因 active evidence tasks 被 409/503 guard 阻止，
`error.details` 会包含 `blocked`、`active_count` 和 task 摘要。
