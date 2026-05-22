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
YOLO26-pose: 每路 5 到 8 FPS
SCRFD_2.5G: 每个 track 每 1 秒最多尝试一次
ArcFace: 只对质量合格的人脸执行
```

不同算法对 FPS 的需求：

| 功能 | 推荐 FPS | 说明 |
|---|---:|---|
| 周界入侵 | 5-8 | 依赖 track 和 ROI。 |
| 徘徊 | 2-5 | 长时间事件，可低频。 |
| 人群聚集 | 2-5 | 统计人数，可低频。 |
| 奔跑 | 8-12 | 速度事件，需要稍高频。 |
| 追逐 | 8-12 | 依赖多人关系和速度。 |
| 摔倒 | 8-12 | 依赖姿态变化。 |
| 人脸识别 | 1 per track per second | 只对清晰人脸触发。 |

## 3. Batch 策略

默认：

```text
YOLO26-pose batch_size = 8
SCRFD_2.5G batch_size = 16
ArcFace batch_size = 16
max_same_source_frames = 1
batched_push_timeout = 40000us
```

压测时必须比较：

```text
batch_size = 4 / 8 / 16
```

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

1. 降低 SCRFD_2.5G 频率。
2. 增加 face_attempt_interval_ms。
3. 暂停非重点摄像头人脸链路。
4. 降低 ArcFace 重复识别频率。
5. 降低 YOLO26-pose FPS。
6. 丢弃过期帧。
7. 保留行为主链路。
8. 输出 overload metrics 和日志。

绝对禁止：

- 无限扩大队列。
- 因数据库慢导致 Savant 主 pipeline 阻塞。
- 每路每帧都跑 SCRFD 和 ArcFace。

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
face_inference_latency_ms
arcface_inference_latency_ms
pose_batch_size_actual
face_batch_size_actual
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
+ SCRFD_2.5G
+ ArcFace
+ Redis Streams
+ PostgreSQL 入库
+ 告警 WebSocket
```

### 7.3 批大小矩阵

```text
pose_batch_size: 4, 8, 16
face_batch_size: 8, 16
arcface_batch_size: 8, 16
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

1. 降低输入 FPS。
2. 使用更小 YOLO26-pose 模型。
3. 调整 batch size。
4. 限制 SCRFD/ArcFace 频率。
5. 优化 converter 和后处理。
6. 优化数据库索引。
7. 使用 INT8，前提是精度可接受。
8. 增加 GPU 或拆分服务器。

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
