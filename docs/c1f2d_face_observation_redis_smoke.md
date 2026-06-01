# C1F.2d — Face Observation Redis Export Smoke

## 目标

验证从 RTSP 到 Redis 的完整 face observation 导出链路：

```text
RTSP
-> Source Adapter
-> Replay
-> Savant (module.c1f2d_face_observation_redis.yml)
-> YOLO26-pose full-frame primary
-> nvtracker
-> YOLOv8-Face full-frame primary
-> face-person association
-> AlignFace preprocessing
-> AdaFace embedding (512-d)
-> face_reid_gate (quality + throttle)
-> face_observation_exporter
-> Redis security.face_observations
```

## 与 C1F.2c 的关系

C1F.2c 已证明：
- YOLOv8-Face runtime: PASS
- AlignFace preprocessing runtime: PASS
- AdaFace model runtime: PASS
- embedding dim=512: PASS
- embedding norm valid: PASS

C1F.2d 在 C1F.2c 基础上增加：
- face_reid_gate runtime 接入
- face_observation_exporter runtime 接入
- Redis security.face_observations 写入验证
- 消息体 schema 验证

## 为什么复用已有 gate/exporter

face_reid_gate 和 face_observation_exporter 在项目早期已经实现：

- `custom/pyfuncs/face_reid_gate.py` — Savant pyfunc wrapper
- `custom/services/face_reid_gate.py` — pure Python gate logic
- `custom/pyfuncs/face_observation_exporter.py` — Savant pyfunc wrapper
- `custom/services/face_observation_exporter.py` — Redis XADD exporter
- `custom/models/face_events.py` — FaceObservationEventDraft schema

C1F.2a 已确认这些文件在配置层面存在且完整。C1F.2d 不从零实现，
而是将它们接回当前 C1 official RTSP + inline Replay + Savant runtime 链路。

## Trimmed Module 内容

`module.c1f2d_face_observation_redis.yml` 包含：

| 元素 | 类型 | 作用 |
|------|------|------|
| yolo26_pose | nvinfer@complex_model | person detection + 17 keypoints |
| tracker | nvtracker | person track_id |
| yolov8_face | nvinfer@complex_model | face detection + 5 landmarks (full-frame primary) |
| face_person_associator | pyfunc | link face to person track |
| adaface | nvinfer@attribute_model | 512-d embedding (input: yolov8_face.face) |
| face_reid_gate | pyfunc | quality gate + per-track throttle |
| face_observation_exporter | pyfunc | Redis XADD (gate-allowed only) |

不包含：
- behavior_rules
- same_frame_detection_debug
- adaface_runtime_debug
- face_embedding_debug
- face_debug
- production evidence / media pyfuncs

## Redis security.face_observations Schema

消息体由 `FaceObservationEventDraft.to_dict()` 生成：

| 字段 | 类型 | 说明 |
|------|------|------|
| schema_version | str | "1.0" |
| source_observation_id | str | deterministic id: face:{source_id}:{track_id}:{ts} |
| producer | str | "savant_security" |
| message_type | str | "face_observation" |
| camera_id | str | business camera_id |
| source_id | str | Savant source_id |
| track_id | str | person track_id (string) |
| timestamp_ms | int | frame timestamp |
| frame_num | int? | frame number |
| face_bbox | list? | [xc, yc, w, h] |
| landmarks | list? | 5-point landmarks |
| face_confidence | float | detector confidence |
| quality | float | gate quality score |
| detector_model | str | "yolov8_face" |
| embedding_model | str | "adaface" |
| embedding_dim | int | 512 |
| embedding | list | 512-d float vector |
| embedding_norm | float | L2 norm (~1.0) |
| reid_allowed | bool | true (only gate-allowed reach Redis) |
| reid_throttle_key | str | throttle key |
| association_score | float | face-person association score |
| association_method | str | association method |

Redis Stream flat fields:
- type, source_observation_id, camera_id, source_id, track_id,
  timestamp_ms, face_confidence, quality, embedding_model, embedding_dim
- data: full JSON above

## Gate Allowed 语义

face_reid_gate 的 7 条规则全部通过：

1. person_track_id > 0
2. face_confidence >= threshold (default 0.6)
3. face bbox min width/height >= threshold (default 40)
4. landmarks = 5 points (10 floats)
5. embedding_dim = 512
6. embedding norm in [0.90, 1.10]
7. no NaN in feature

加上 per-camera+track throttle (default 1000ms)。

## 禁止图片 bytes 的边界 (No Image Bytes)

Redis 消息体中不允许包含：
- JPEG / PNG / RAW 图片数据
- face crop bytes
- base64 编码的图片
- 任何形式的二进制像素数据

只允许：
- embedding vector (512 floats, ~2KB JSON)
- bbox coordinates
- landmarks coordinates
- metadata strings

## Smoke 验收标准

| 指标 | 要求 |
|------|------|
| valid_face_observations | > 0 |
| observations_with_track_id | > 0 |
| observations_with_embedding | > 0 |
| observations_with_embedding_dim_512 | > 0 |
| observations_with_valid_norm | > 0 |
| observations_with_gate_allowed | > 0 |
| observations_without_image_bytes | = total |
| sample_embedding_dim | 512 |
| sample_embedding_norm | 0.90 ~ 1.10 |

结果枚举：
- PASS — 所有指标满足
- PASS_WITH_SOURCE_CORRUPTION — RTSP 源有 decode error，但仍有有效 observation
- FAIL_NO_REDIS_MESSAGES — Redis 无新消息
- FAIL_NO_VALID_OBSERVATION — 有消息但无有效 observation
- FAIL_INVALID_EMBEDDING — embedding dim/norm 不合格
- FAIL_IMAGE_BYTES_IN_REDIS — 消息中包含图片 bytes
- FAIL_PIPELINE_STOPPED — Savant pipeline 提前停止

## 不验证范围

- face-worker DB ingest
- PostgreSQL face_observations
- gallery match / watchlist_hit / live_search_hit
- API / frontend
- production evidence clip
- timestamp-domain mapping
- frame_uuid / keyframe_uuid 对齐

## Source Corruption 处理口径

固定 RTSP 源 `rtsp://10.37.57.112:8554/live/1080movie` 不是 decode-clean。
Savant 日志中可能出现 `generated_corrupt` / `decode error` / `reference frame`。

如果 source corruption 存在但仍有有效 observation 导出，返回
`PASS_WITH_SOURCE_CORRUPTION`，不算 FAIL。

## 下一步

C1F.3 — face-worker ingest compatibility smoke：
- 验证 face-worker 能从 Redis security.face_observations 消费
- 验证 PostgreSQL face_observations 表写入
- 验证 pgvector embedding 存储
