# 08_performance_policy.md

## 1. 性能目标

系统目标是约 60 路 RTSP 视频实时分析，运行在双 NVIDIA T4 GPU 服务器上。

核心原则：

```text
实时系统处理最新帧，不追求处理每一帧。
```

当负载过大时，系统应降级而不是无限排队。

## 2. 默认 FPS 策略

```text
YOLO26-pose:    每路 5 到 8 FPS
YOLOv8-Face:    与 YOLO26-pose 同帧节奏运行 (full-frame primary)
face-person association: 与 YOLOv8-Face 同帧节奏运行 (PyFunc, 几何匹配)
AdaFace:        每个 track 每 1 秒最多尝试一次 (per-track throttle, 仅
                对 face_quality 合格的人脸执行)
```

> F1.1a 锁定: YOLOv8-Face **是 full-frame primary detector**, 不是
> per-person ROI secondary. 不要为每个 person 单独触发一次 face
> 推理.

不同算法对 FPS 的需求：

| 功能 | 推荐 FPS | 说明 |
|---|---:|---|
| 周界入侵 | 5-8 | 依赖 track 和 ROI. |
| 徘徊 | 2-5 | 长时间事件, 可低频. |
| 人群聚集 | 2-5 | 统计人数, 可低频. |
| 奔跑 | 8-12 | 速度事件, 需要稍高频. |
| 追逐 | 8-12 | 依赖多人关系和速度. |
| 摔倒 | 8-12 | 依赖姿态变化. |
| 人脸检测 (YOLOv8-Face) | 跟随 pose FPS | 全帧 primary, 与 pose 同步. |
| 人脸嵌入 (AdaFace) | ≤ 1 / track / s | 只对质量合格的人脸触发. |

## 3. Batch 策略

默认 (F1.1a 命名):

```text
YOLO26-pose                batch_size = 8     POSE_BATCH_SIZE
YOLOv8-Face                batch_size = 8     FACE_DETECTOR_BATCH_SIZE
AdaFace                    batch_size = 16    FACE_EMBEDDING_BATCH_SIZE
max_same_source_frames     = 1
batched_push_timeout       = 40000 us
```

压测时必须比较：

```text
batch_size = 4 / 8 / 16
```

> 兼容旧命名: F1.1b 实现阶段可继续读取 `FACE_BATCH_SIZE` /
> `ARCFACE_BATCH_SIZE` 作为兼容别名, 文档说明后续会移除. 新代码必须
> 使用新名字.

指标：

- 吞吐。
- 延迟 p50/p95/p99。
- GPU 利用率。
- 显存占用。
- 事件延迟。

## 4. 过载定义

出现以下任一情况，认为系统进入过载：

1. 事件延迟持续超过目标阈值。
2. Redis Streams lag 持续增长。
3. Savant 输出队列持续增长。
4. GPU 利用率长期接近 100%，且事件延迟变大。
5. 显存接近上限。
6. PostgreSQL 写入延迟显著升高。

## 5. 过载降级策略

按顺序执行：

1. 提高 AdaFace per-track throttle 间隔 (例如 1s -> 2s).
2. 增加 face_attempt_interval_ms.
3. 暂停非重点摄像头人脸链路 (跳过 face-person association 输出).
4. 降低 YOLOv8-Face 频率 (与 pose 解耦, 改为隔帧运行).
5. 降低 YOLO26-pose FPS.
6. 丢弃过期帧.
7. 保留行为主链路.
8. 输出 overload metrics 和日志.

绝对禁止：

- 无限扩大队列.
- 因数据库慢导致 Savant 主 pipeline 阻塞.
- 每路每帧都强制运行 AdaFace.
- 通过 Redis 传图片 bytes / face crop bytes / JPEG / PNG.
- 在 Savant 主 pipeline 内做 JPEG encode/decode roundtrip.
- 把 AdaFace 移到 CPU/Python face-worker 作为实时 embedding bottleneck.
- 把 YOLOv8-Face 改成 per-person ROI secondary detector (F1.1a 锁定的
  反面).

## 6. 性能指标

必须采集：

### 6.1 视频指标

```text
camera_online
camera_input_fps
camera_processed_fps
camera_dropped_frames
rtsp_reconnect_count
```

### 6.2 推理指标

```text
pose_inference_latency_ms
face_detector_inference_latency_ms      # YOLOv8-Face
adaface_inference_latency_ms
pose_batch_size_actual
face_detector_batch_size_actual
adaface_batch_size_actual
face_person_association_latency_ms       # PyFunc, 不涉及 GPU
```

### 6.3 GPU 指标

```text
gpu_utilization
gpu_memory_used
gpu_memory_total
gpu_temperature
```

### 6.4 事件指标

```text
events_total_by_type
event_latency_ms
watchlist_hits_total
live_search_hits_total
false_positive_count
```

### 6.5 队列指标

```text
redis_stream_length
redis_consumer_lag
worker_processing_latency_ms
worker_error_count
```

### 6.6 数据库指标

```text
postgres_write_latency_ms
postgres_query_latency_ms
pgvector_search_latency_ms
postgres_connections
```

## 7. 压测矩阵

### 7.1 路数矩阵

```text
1 路
4 路
8 路
16 路
30 路
60 路
```

### 7.2 功能矩阵

每个路数下分阶段测试：

```text
只解码
解码 + YOLO26-pose
+ nvtracker
+ 行为规则
+ YOLOv8-Face full-frame
+ face-person association + face_quality
+ AdaFace embedding
+ Redis Streams (face_observations 含 embedding)
+ face-worker pgvector 检索
+ PostgreSQL 入库
+ 告警 WebSocket
```

### 7.3 批大小矩阵

```text
pose_batch_size:          4, 8, 16
face_detector_batch_size: 4, 8, 16
face_embedding_batch_size: 8, 16
```

## 8. 验收目标

第一版目标：

```text
单路功能可跑通
30 路单 GPU 压测可得出瓶颈
60 路双 GPU 降帧运行不崩溃
事件队列不无限增长
GPU 显存不持续泄漏
```

建议目标值：

```text
行为事件延迟 p95 <= 2s
watchlist_hit 延迟 p95 <= 3s
Redis lag 不持续增长
PostgreSQL 写入 p95 <= 100ms
pgvector search p95 <= 300ms，具体取决于数据量
```

实际验收以现场压测结果为准。

## 9. Grafana 看板

至少包含：

1. GPU 总览。
2. 摄像头在线状态。
3. 每路 FPS。
4. 事件数量趋势。
5. 事件延迟。
6. Redis 队列积压。
7. PostgreSQL 延迟。
8. worker 错误数。
9. 人脸识别命中数。
10. 一键找人任务状态。

## 10. 性能调优优先级

1. 降低输入 FPS.
2. 使用更小 YOLO26-pose 模型.
3. 调整 batch size (pose / face detector / face embedding).
4. 提高 AdaFace per-track throttle 间隔.
5. 优化 converter 和后处理.
6. 优化数据库索引.
7. 使用 INT8, 前提是精度可接受.
8. 增加 GPU 或拆分服务器 (可选地启用 Savant module chaining via ZMQ).

## 10.1 Redis 消息体边界 (F1.1a 锁定)

Redis stream `security.face_observations` 的单条消息允许:

- 结构化 metadata (camera_id / source_id / track_id / timestamp_ms /
  person_bbox / face_bbox / landmarks / quality / model_name /
  model_version).
- **512 维 AdaFace embedding** (float32, 约 2 KB 主体).
- 可选: `snapshot_path` / `crop_path` 路径引用字段.

绝对不允许:

- crop image -> Redis (face crop bytes).
- full frame bytes / JPEG / PNG.
- JPEG encode/decode roundtrip 作为正常路径.
- CPU/Python 实时 embedding 作为 face-worker 的常规职责.

如果未来真的需要传输 face crop 给某个 offline 服务, 必须开辟独立通
道 (例如另一个 stream 或文件系统路径引用), 不得占用
`security.face_observations`.

## 10.2 NVDEC 容量 TODO (F1.2 runtime smoke 前必须回答)

下游推理 FPS 降到 3-5 不等于 RTSP 解码 FPS 也下降. 摄像头仍按其编
码 / 帧率推流, NVDEC 解码压力照旧. 必须在 F1.2 runtime smoke 之前
确认:

```text
TODO: 60 路摄像头实际编码格式 (H.264 vs H.265)
TODO: 摄像头源端实际推流帧率 (是否可配置 3-5fps 或锁定 10/25/30fps)
TODO: 摄像头 bitrate 区间 (影响 NVDEC 解码负载)
```

已知硬件约束 (单 T4):

| 编码 | 1080p30 摄像头数 (经验上限) |
|---|---|
| H.264 | 约 22 路 |
| H.265 | 约 44 路 |

已知部署风险:

- 30 路 / 卡 1080p30 H.264 已超过 T4 NVDEC 容量上限.
- 双卡平摊后 60 路 H.264 全部 1080p30 仍紧张, 必须确认实际帧率.

决策依赖项:

- 如摄像头支持 H.265: 大概率无需额外降采样.
- 如摄像头仅 H.264 30fps: 必须在源端 (RTSP profile) 或 nvstreammux
  做降采样.
- 降采样方式 (源端 RTSP profile vs nvstreammux frame-duration) 待
  F1.2 runtime 验证.

重要澄清:

- `nvstreammux` / `nvinfer interval` 降采样只能减轻下游推理压力, **不
  能减轻 NVDEC 解码本身**.
- 因此 **源端可配低 fps 是最优解**, 其次才是中间环节降采样.

---

# 11. 报警录像性能边界补充

MVP 阶段性能压测不把报警片段录制作为必验项。

MVP 性能关注：

```text
1. Savant 推理延迟。
2. Redis Streams lag。
3. event-worker 入库延迟。
4. PostgreSQL 写入延迟。
5. FastAPI 查询延迟。
6. GPU 显存和利用率。
```

报警录像正式实现后再新增指标：

```text
record_request_latency_ms
media_ready_latency_ms
clip_generation_latency_ms
clip_failure_count
recording_queue_lag
replay_job_create_latency_ms
replay_keyframe_lookup_latency_ms
replay_cache_ttl_seconds
replay_rocksdb_size_bytes
video_file_sink_write_latency_ms
video_file_sink_failure_count
external_nvr_download_latency_ms
```

第一版不采用持续切片落盘作为默认策略，避免在 MVP 性能测试中过早引入额外磁盘 IO 干扰。后续启用 Savant Replay 后，Replay 的 RocksDB 占用、TTL、写入延迟和 Video File Sink 落盘延迟必须单独纳入压测，不与 MVP 主推理验收混在一起。


## 12. Replay 性能边界

启用 Savant Replay 后，Replay 只应保存短时间缓存，不应演变为 NVR。

建议初始参数：

```text
REPLAY_CACHE_TTL_SECONDS = 60
DEFAULT_PRE_SECONDS = 5
DEFAULT_POST_SECONDS = 5
MAX_RECORD_SECONDS = 15
```

必须监控：

```text
replay_input_fps
replay_output_jobs_total
replay_active_jobs
replay_job_create_latency_ms
replay_keyframe_lookup_latency_ms
replay_rocksdb_size_bytes
video_file_sink_write_latency_ms
media_ready_latency_ms
clip_failure_count
```

过载策略：

```text
1. 限制同时运行的 Replay jobs。
2. 对低优先级事件不生成 clip，只保留事件。
3. 缩短 Replay TTL。
4. 降低 post_seconds 或最大片段长度。
5. 录像失败不得影响事件入库和告警推送。
```
