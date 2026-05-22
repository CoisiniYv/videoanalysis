# 02_savant_pipeline.md

## 1. Savant Module 目标

Savant module 是实时视频分析的核心。它将 RTSP 视频帧转化为结构化 metadata 和事件消息。

注意：按照 Savant 官方架构，RTSP 接入、文件落盘、Replay 重推等能力应优先由 source / sink / bridge adapter 和 Replay Service 承担，module 只保留实时推理与轻量 metadata 处理职责。

主 pipeline：

```text
YOLO26-pose
  -> nvtracker
  -> behavior_rules_pyfunc
  -> face_roi_selector_pyfunc
  -> SCRFD_2.5G
  -> ArcFace
  -> face_intelligence_pyfunc
  -> event_exporter
```

## 2. Pipeline 元素说明

### 2.1 YOLO26-pose

类型：primary model。

实现提示：如果 YOLO26-pose 的 converter 只注册 `person` object，并把 keypoints 作为 object attribute，则可以先按 detector 方式验证；如果 Savant metadata 表达不顺，或模型输出同时包含 bbox、class、confidence、keypoints 等复杂结果，应切换为 Savant complex model 方式实现。

职责：

- 检测 person。
- 输出 bbox。
- 输出 17 个人体 keypoints。
- 为行为规则和人脸 ROI 选择提供基础数据。

输入：整帧图像。

输出：

```text
PersonPoseObservation
  source_id
  camera_id
  frame_id
  timestamp_ms
  bbox
  confidence
  keypoints
  keypoint_confidence
```

### 2.2 nvtracker

类型：DeepStream nvtracker。

职责：

- 给 person 对象分配 track_id。
- 支持后续行为规则的时序判断。

推荐先测试：

```text
NvSORT
NvDCF_perf
NvDCF_accuracy
```

### 2.3 BehaviorRulesPyFunc

类型：Savant PyFunc。

职责：

- 从 frame metadata 读取 person bbox、keypoints、track_id。
- 更新 TrackState。
- 调用纯 Python 行为规则。
- 输出 SecurityEvent。

禁止：

- 直接在 PyFunc 中写复杂数据库逻辑。
- 直接同步请求 FastAPI。
- 直接执行慢向量检索。

### 2.4 FaceRoiSelectorPyFunc

职责：

- 根据 person bbox 和关键点估计 head ROI。
- 控制每个 track 的人脸尝试频率。
- 过滤太小、太远、质量明显不够的人。

策略：

```text
每个 track 每 1000ms 最多尝试一次 face detection
person 高度小于 min_person_height 时不尝试
已经成功识别的 track 在 cooldown 内不重复提取 ArcFace
```

### 2.5 SCRFD_2.5G

类型：secondary detector。

输入：head/person ROI。

职责：

- 检测人脸 bbox。
- 输出 face landmarks。
- 输出 face confidence。

输出进入 face quality filter。

### 2.6 ArcFace

类型：secondary classifier / attribute model / embedding model。

实现提示：ArcFace 输出 embedding，本质上应作为 face object 的 attribute 写入 metadata。可以先用 `nvinfer@classifier` 验证，最终以 converter 能否稳定输出 embedding attribute 为准。

输入：经过对齐的人脸 crop。

输出：512 维或模型指定维度 embedding。

### 2.7 FaceIntelligencePyFunc

职责：

- 生成 FaceObservation。
- 将人脸 observation 事件推送到 Redis Streams。
- 可选执行轻量阈值判断。

复杂检索建议交给 face-worker。

## 3. module.yml 骨架

```yaml
name: security_video_analytics

parameters:
  log_level: ${oc.env:LOGLEVEL, INFO}

  model_path: ${oc.env:MODEL_PATH, /models}
  download_path: ${oc.env:DOWNLOAD_PATH, /downloads}

  max_parallel_streams: ${oc.decode:${oc.env:MAX_PARALLEL_STREAMS, 32}}

  max_fps_control: true
  max_fps: ${oc.env:MAX_FPS, 8/1}
  min_fps: ${oc.env:MIN_FPS, 2/1}

  batch_size: ${oc.decode:${oc.env:BATCH_SIZE, 8}}
  max_same_source_frames: 1
  batched_push_timeout: 40000

  queue_maxsize: 100

pipeline:
  elements:
    - element: nvinfer@detector
      name: yolo26_pose
      model:
        format: onnx
        model_file: yolo26_pose.onnx
        batch_size: ${oc.decode:${oc.env:POSE_BATCH_SIZE, 8}}
        precision: fp16
        input:
          shape: [3, 640, 640]
        output:
          layer_names: [output]
          converter:
            module: custom.converters.yolo26_pose
            class_name: Yolo26PoseConverter
            kwargs:
              confidence_threshold: 0.35
              keypoint_threshold: 0.25
          objects:
            - class_id: 0
              label: person
              selector:
                kwargs:
                  confidence_threshold: 0.4
                  min_width: 20
                  min_height: 40

    - element: nvtracker
      properties:
        ll-lib-file: /opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so
        ll-config-file: /opt/savant/src/module/config/config_tracker_NvDCF_perf.yml

    - element: pyfunc
      module: custom.pyfuncs.behavior_rules
      class_name: BehaviorRulesPyFunc
      kwargs:
        camera_config_path: /opt/savant/src/module/config/cameras.yml

    - element: pyfunc
      module: custom.pyfuncs.face_roi_selector
      class_name: FaceRoiSelectorPyFunc
      kwargs:
        min_person_height: 80
        face_attempt_interval_ms: 1000

    - element: nvinfer@detector
      name: scrfd_face
      model:
        format: onnx
        model_file: scrfd_2.5g.onnx
        batch_size: ${oc.decode:${oc.env:FACE_BATCH_SIZE, 16}}
        precision: fp16
        input:
          object: yolo26_pose.person
          shape: [3, 320, 320]
        output:
          layer_names: [output]
          converter:
            module: custom.converters.scrfd
            class_name: ScrfdConverter
            kwargs:
              confidence_threshold: 0.5
              nms_iou_threshold: 0.4
          objects:
            - class_id: 0
              label: face
              selector:
                kwargs:
                  confidence_threshold: 0.6
                  min_width: 20
                  min_height: 20

    - element: nvinfer@classifier
      name: arcface
      model:
        format: onnx
        model_file: arcface.onnx
        batch_size: ${oc.decode:${oc.env:ARCFACE_BATCH_SIZE, 16}}
        precision: fp16
        input:
          object: scrfd_face.face
          shape: [3, 112, 112]
        output:
          layer_names: [embedding]
          converter:
            module: custom.converters.arcface
            class_name: ArcFaceEmbeddingConverter

    - element: pyfunc
      module: custom.pyfuncs.face_intelligence
      class_name: FaceIntelligencePyFunc
      kwargs:
        event_stream: security.events
        face_min_quality: 0.65
        watchlist_threshold: 0.75
        live_search_threshold: 0.75
```

## 4. Converter 要求

### 4.1 Yolo26PoseConverter

必须完成：

- 解析 YOLO26-pose ONNX 输出。
- 输出 person bbox。
- 输出关键点数组。
- 支持 batch 输出。
- 过滤低置信度对象和低置信度关键点。

不得假设 batch 恒为 1。

### 4.2 ScrfdConverter

必须完成：

- 解析 SCRFD_2.5G 输出。
- 输出 face bbox。
- 输出 landmarks。
- 支持 NMS。
- 坐标必须正确映射到父 ROI 或原图坐标。

### 4.3 ArcFaceEmbeddingConverter

必须完成：

- 读取 embedding tensor。
- 做 L2 normalization，如果模型输出未归一化。
- 输出统一 FaceEmbedding metadata。

## 5. 批处理策略

默认：

```text
YOLO26-pose batch_size: 8
SCRFD_2.5G batch_size: 16
ArcFace batch_size: 16
max_same_source_frames: 1
batched_push_timeout: 40000
```

调优时测试：

```text
batch_size: 4 / 8 / 16
pose_fps: 5 / 8 / 12
face_attempt_interval_ms: 1000 / 2000 / 3000
```

## 6. 摄像头配置

Savant 不得硬编码摄像头规则。所有规则从 `config/cameras.yml` 加载。

示例：

```yaml
cameras:
  cam_001:
    enabled: true
    gpu_id: 0
    source_id: site_a.gate.cam_001
    fps:
      pose_fps: 8
      face_fps: 1
    zones:
      perimeter:
        type: polygon
        points: [[100, 300], [900, 300], [900, 700], [100, 700]]
    rules:
      intrusion:
        enabled: true
        zone: perimeter
        min_inside_ms: 1000
        cooldown_s: 30
    face:
      enabled: true
      min_face_width: 24
      min_face_height: 24
      min_quality: 0.65
      attempt_interval_ms: 1000
```

## 7. 输出事件

Savant 输出到 Redis Streams 的消息必须至少包含：

```json
{
  "source_event_id": "cam_001:t_001:intrusion:1710000000000",
  "event_type": "intrusion",
  "camera_id": "cam_001",
  "source_id": "site_a.gate.cam_001",
  "track_id": "t_001",
  "start_ts_ms": 1710000000000,
  "end_ts_ms": 1710000001200,
  "confidence": 0.86,
  "severity": "medium",
  "snapshot_required": true,
  "clip_required": true,
  "payload": {
    "event_ts_ms": 1710000000000,
    "frame_uuid": null,
    "keyframe_uuid": null,
    "replay": {
      "source_id": "site_a.gate.cam_001",
      "pre_seconds": 5,
      "post_seconds": 5
    }
  }
}
```

## 8. Smoke Test 验收

Savant pipeline smoke test 必须验证：

1. module 可启动。
2. YOLO26-pose engine 可加载。
3. SCRFD engine 可加载。
4. ArcFace engine 可加载。
5. 测试视频可输出 person metadata。
6. 可输出至少一个行为事件。
7. 可输出至少一个 face_observation。

---

# 9. MVP 媒体录制边界

Savant MVP 阶段只负责输出结构化事件和人脸 observation。

Savant 输出事件时可以包含：

```json
{
  "snapshot_required": true,
  "clip_required": true
}
```

但 MVP 阶段不要求 Savant 生成真实截图或视频片段。

Savant 不应在第一阶段承担：

```text
1. 大文件写盘。
2. 报警前后视频剪辑。
3. NVR 回放下载。
4. 复杂媒体状态管理。
```

后续如果实现报警前后视频片段，应优先使用 Savant Replay Service，而不是自研 RecordingController。Replay 负责短时缓存和重推；clip-worker 负责调用 Replay REST API；Video File Sink Adapter 负责落盘；media-worker 负责整理业务路径和回写数据库。


## 10. Replay 相关 metadata 要求

为了支持后续“报警前 5 秒 + 后 N 秒”视频片段，Savant 输出事件时应尽量携带以下字段：

```json
{
  "source_id": "site_a.gate.cam_001",
  "event_ts_ms": 1710000000000,
  "frame_uuid": null,
  "keyframe_uuid": null,
  "replay": {
    "enabled": false,
    "pre_seconds": 5,
    "post_seconds": 5
  }
}
```

字段说明：

- `source_id`：必须稳定，需与 Savant source adapter / Replay 中的 source id 一致。
- `event_ts_ms`：事件锚点时间。
- `frame_uuid`：如果 Savant metadata 可取得，应记录。
- `keyframe_uuid`：如果能取得最近 keyframe，应记录；否则由 clip-worker 通过 Replay `/api/v1/keyframes/find` 查询。
- `replay.pre_seconds` / `replay.post_seconds`：默认 5 秒，可按事件类型或规则覆盖。

MVP 阶段这些字段可以为空或只保留 `source_id + event_ts_ms`，但 schema 应提前兼容。
