# 09_harness.md

## 1. Harness 目标

Harness 用于约束 Claude Code 和人工开发，确保算法、pipeline、数据库、向量检索和性能调整可验证。

测试分为：

1. 纯规则单元测试。
2. 人脸向量测试。
3. API 测试。
4. Savant pipeline smoke test。
5. 性能压测。
6. 回归 golden events。

## 2. 目录结构

```text
harness/
  tests/
    test_intrusion.py
    test_loitering.py
    test_crowd_gathering.py
    test_running.py
    test_chasing.py
    test_fall.py
    test_face_vector_store.py
    test_live_search.py
    test_api_events.py
    test_pipeline_smoke.py

  scenarios/
    intrusion/
    loitering/
    crowd_gathering/
    running/
    chasing/
    fall/
    face_match/
    live_search/

  golden_events/
    intrusion_basic.json
    loitering_basic.json
    crowd_basic.json
    fall_basic.json
    watchlist_hit_basic.json
    live_search_hit_basic.json

  run_perf_matrix.sh
```

## 3. 规则测试输入格式

行为规则测试不依赖视频和 GPU。

输入：

```json
{
  "camera_id": "cam_001",
  "source_id": "site_a.gate.cam_001",
  "track_id": "t_001",
  "observations": [
    {
      "timestamp_ms": 1000,
      "bbox": [100, 200, 60, 180],
      "confidence": 0.8,
      "keypoints": [
        {"name": "left_shoulder", "x": 120, "y": 230, "confidence": 0.8}
      ]
    }
  ],
  "camera_config": {
    "zones": {},
    "rules": {}
  }
}
```

输出：

```json
[
  {
    "event_type": "intrusion",
    "camera_id": "cam_001",
    "track_id": "t_001",
    "confidence": 0.86,
    "payload": {}
  }
]
```

## 4. 行为规则测试要求

每个规则至少测试：

- 正例。
- 反例。
- 阈值边界。
- cooldown。
- 多 track。
- track 消失状态清理。

### 4.1 周界入侵

测试：

```text
person 从 ROI 外进入 ROI，停留超过阈值 -> 产生 intrusion
person 短暂经过 ROI，不超过阈值 -> 不产生事件
cooldown 内重复进入 -> 不重复报警
```

### 4.2 徘徊

测试：

```text
ROI 内低速停留超过阈值 -> loitering
快速通过 -> 不报警
停留时间不足 -> 不报警
```

### 4.3 人群聚集

测试：

```text
ROI 内人数超过阈值并持续 -> crowd_gathering
人数短暂超过 -> 不报警
人数不足 -> 不报警
```

### 4.4 摔倒

测试：

```text
高度快速下降 + 横向姿态 + 静止 -> fall
弯腰捡东西 -> 不报警
坐下 -> 不报警或低置信度
```

## 5. 人脸向量测试

`test_face_vector_store.py` 必须验证：

1. 创建 person。
2. 插入 gallery embedding。
3. 插入 face_observation embedding。
4. 相同向量可检索命中。
5. 不同向量低于阈值。
6. camera/time filter 生效。
7. inactive gallery 不参与检索。

测试可使用伪造向量，不需要真实模型。

## 6. 一键找人测试

`test_live_search.py` 必须验证：

1. 创建 live_search_job。
2. job active 时可命中。
3. job expired 后不命中。
4. job stopped 后不命中。
5. cooldown 生效。
6. 命中后生成 `live_search_hit`。

## 7. API 测试

API 测试覆盖：

```text
POST /cameras
POST /cameras/{id}/zones
POST /cameras/{id}/rules
GET /events/recent
POST /persons
POST /persons/{id}/faces
POST /watchlist
POST /live-search
```

第一版上传照片接口可以用 mock embedding。

## 8. Pipeline Smoke Test

Smoke test 目标：验证 Savant pipeline 能跑通，不要求准确率。

输入：

```text
harness/scenarios/smoke/person_walk.mp4
harness/scenarios/face_match/face_sample.mp4
```

验证：

1. Savant module 启动成功。
2. YOLO26-pose engine 加载成功。
3. 输入视频产生 person metadata。
4. nvtracker 产生 track_id。
5. 行为 PyFunc 可运行。
6. Redis Streams 收到事件。
7. event-worker 可入库。

## 9. Golden Events

Golden file 示例：

```json
{
  "scenario": "intrusion_basic",
  "expected_events": [
    {
      "event_type": "intrusion",
      "camera_id": "cam_001",
      "track_id": "t_001"
    }
  ]
}
```

回归测试只校验关键字段，不强制完全相同的时间戳和置信度。

## 10. 性能压测脚本

`run_perf_matrix.sh` 应执行：

```text
1 路只解码
4 路 YOLO26-pose
8 路 YOLO26-pose + tracker
16 路 + 行为规则
30 路单 GPU
60 路双 GPU
```

输出：

```text
results/perf_YYYYMMDD_HHMMSS.json
```

记录：

- total_input_fps。
- total_processed_fps。
- gpu_utilization。
- gpu_memory_used。
- event_latency_p50/p95/p99。
- redis_lag。
- postgres_write_latency。

## 11. Claude Code 任务要求

每次让 Claude Code 实现功能时，任务必须包含：

1. 要改哪些文件。
2. 要新增哪些测试。
3. 验收命令。
4. 不允许修改哪些边界。
5. 事件 schema 是否变化。

示例：

```text
Task: Implement intrusion rule
Files:
  modules/savant_security/custom/rules/intrusion.py
  modules/savant_security/custom/pyfuncs/behavior_rules.py
  harness/tests/test_intrusion.py
Acceptance:
  pytest harness/tests/test_intrusion.py
Requirements:
  rule must be pure Python and not import Savant
```

## 12. CI 建议

第一版 CI：

```text
pytest harness/tests/test_intrusion.py
pytest harness/tests/test_loitering.py
pytest harness/tests/test_crowd_gathering.py
pytest harness/tests/test_fall.py
pytest harness/tests/test_face_vector_store.py
```

GPU pipeline smoke test 可手动或夜间执行。

---

# 13. MVP 媒体状态测试补充

MVP 阶段应增加 event-worker 媒体状态测试，不要求真实生成视频。

建议新增测试：

```text
harness/tests/test_event_worker_media_reserved.py
```

测试内容：

```text
1. 输入 snapshot_required=true / clip_required=true 的 SecurityEvent。
2. event-worker 能成功写入 events 表。
3. snapshot_path 为 null。
4. clip_path 为 null。
5. payload.media.snapshot_status = not_implemented。
6. payload.media.clip_status = not_implemented。
7. payload.media.recording_strategy = reserved。
8. FastAPI 查询事件时可以正常返回媒体状态。
```

后续实现报警录像后，再新增：

```text
harness/tests/test_record_request.py
harness/tests/test_media_ready.py
test_replay_record_request.py
test_replay_media_ready.py
```


## 14. Replay 录像测试补充

启用 Savant Replay 方案后，新增测试：

```text
harness/tests/test_replay_record_request.py
harness/tests/test_replay_media_ready.py
harness/tests/test_video_file_sink_output_parse.py
```

### 14.1 record request 测试

输入一个带 `clip_required=true` 的事件，验证：

```text
1. event-worker 能写入 events 表。
2. payload.media.recording_strategy = savant_replay 或 reserved。
3. RECORDING_ENABLED=true 时能生成 security.record_requests。
4. record_request 包含 source_id、event_ts_ms、pre_seconds、post_seconds。
5. 如果事件含 keyframe_uuid，record_request 原样携带。
```

### 14.2 Replay API mock 测试

使用 mock Replay API，验证 clip-worker：

```text
1. keyframe_uuid 缺失时调用 /api/v1/keyframes/find。
2. 创建 job 时设置 offset.seconds = 5。
3. 创建 job 时设置 stop_condition。
4. Replay job 创建失败时标记 clip_status=failed。
5. 成功时进入 sink_writing 或 ready 状态。
```

### 14.3 Video File Sink 输出解析测试

使用伪造 sink 输出目录：

```text
metadata.json
video.mov 或 video.webm
```

验证 media-worker 能把它登记或整理成业务 `clip_path`，并回写 `events.clip_path`。
