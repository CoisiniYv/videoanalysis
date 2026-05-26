# 02_savant_pipeline.md

## 1. Savant Module 目标

Savant module 是实时视频分析的核心。它将 RTSP 视频帧转化为结构化 metadata 和事件消息。

注意：按照 Savant 官方架构，RTSP 接入、文件落盘、Replay 重推等能力应优先由 source / sink / bridge adapter 和 Replay Service 承担，module 只保留实时推理与轻量 metadata 处理职责。

主 pipeline (锁定于 Phase F1.1a, 2026-05-26):

```text
YOLO26-pose
  -> nvtracker
  -> behavior_rules_pyfunc
  -> YOLOv8-Face                       (PRIMARY, FULL-FRAME, NOT secondary-on-ROI)
  -> face_roi_selector_pyfunc          (重定义: face-person association, 不裁剪 / 不触发推理)
  -> face_quality + per-track throttle
  -> adaface_preprocess_pyfunc         (5-point landmark alignment, 112x112)
  -> AdaFace                           (in-pipeline embedding, 输出 512 维)
  -> face_reid_gate_pyfunc             (quality gate + per-track throttle, F2.2)
  -> face_observation_exporter_pyfunc  (写入 security.face_observations, 包含 embedding, F2.3)
```

> 历史路线 `SCRFD_2.5G -> ArcFace` 已被 F1.1a 取代:
>
> - 第一版 detector 改为 **YOLOv8-Face full-frame primary**.
> - 第一版 embedder 改为 **AdaFace, 仍在 Savant module 内**.
> - SCRFD_2.5G 保留为 future detector candidate (可在 §2.5b 描述).
> - ArcFace 保留为 future embedding alternative (可在 §2.6 描述).
> - **不允许** 在任何后续 spec 中描述 "per-person ROI crop ->
>   YOLOv8-Face" 或 "YOLOv8-Face as secondary nvinfer on person ROI"
>   的链路. 60 路 / 2× T4 部署目标下, YOLOv8-Face 必须是单次全帧
>   primary 推理.
>
> 详见 `docs/phase_f1_1a_in_pipeline_face_architecture_lock.md`.

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

> **F1.1a 起的职责重定义**: 这个 PyFunc 不再为 secondary 人脸 detector
> 准备 ROI 截图. 在 YOLOv8-Face 已经作为 primary full-frame detector
> 直接输出 face object 之后, 它的工作变成 **face-person
> association** + 节流: 不裁剪图像, 不触发额外推理.
>
> 如后续阶段确认命名歧义明显, 可在 F1.2 之后将文件 / 类名重命名为
> `FacePersonAssociatorPyFunc`. F1.1a 不做重命名.

输入:

```text
persons:   [(track_id, person_bbox, keypoints), ...]   来自 YOLO26-pose + nvtracker
faces:     [(face_bbox, landmarks, confidence), ...]   来自 YOLOv8-Face
```

输出:

```text
associated_faces:
  (track_id_or_None, face_bbox, landmarks, confidence, quality_inputs, ...)
```

职责:

- 根据 person bbox 与关键点估计 head ROI (复用 F0
  `estimate_head_roi_from_person`).
- 计算每个 face bbox 与每个 person head ROI 的 IoU.
- 贪心匹配: 每个 face 取 IoU 最高的 person (高于 `min_iou` 阈值),
  并把该 person 的 `track_id` 继承给 face.
- 匹配不到 person 的 face, `track_id = None`, 仍然向下游传递 (后续
  仍可被 AdaFace 嵌入并入库, 但没有人体轨迹关联).
- 控制每个 track 的人脸尝试频率 (默认 1 Hz).
- 过滤明显过小 / 过远 / 质量不足的人脸 (具体在 face_quality 阶段
  做细判).

非职责:

- **不做图像裁剪**.
- **不触发额外推理** (例如不调用 secondary nvinfer).
- 不与 PostgreSQL 或 FastAPI 通信.

策略:

```text
每个 track 每 1000ms 最多产出一次 AdaFace embedding
person 高度小于 min_person_height 时跳过
已成功识别的 track 在 cooldown 内不重复 embedding
```

### 2.5 YOLOv8-Face

类型: **primary detector, full-frame**.

输入: 整帧图像 (与 YOLO26-pose 并列的 primary nvinfer).

输出:

- face bbox (xyxy).
- 5 个 face landmarks (eyes, nose, mouth corners).
- face confidence.

不要把 YOLOv8-Face 设计成 person ROI 上的 secondary detector. 第一
版 YOLOv8-Face 是全帧 primary, 与 YOLO26-pose 并列, 共享 nvinfer
batch 机制.

### 2.5b SCRFD_2.5G (future detector candidate)

SCRFD_2.5G 保留为未来 detector swap 候选, 不在第一版实现:

- 第一版 detector 已选 YOLOv8-Face full-frame.
- SCRFD 切换需重新评估 ONNX 输出张量形状, 实现 multi-FPN-scale
  anchor decode, 并通过新的 phase doc 推翻 F1.1a 才能落地.

### 2.6 AdaFace

类型: **in-pipeline embedding model**, 由 Savant module 推理, 不在
face-worker 或独立服务中运行.

实现提示: AdaFace 输出 embedding, 作为 face object 的 attribute 写入
metadata. 与 YOLOv8-Face 一样使用 TensorRT engine.

输入: 经过对齐的人脸 crop, 112×112.

输出: 模型固定维度 embedding (默认 512 维).

预处理注意事项 (必须在模型资产清单审核期间确认, 不允许猜测):

- 5-point landmark alignment 使用 YOLOv8-Face 输出的 5 个 landmarks.
- crop 尺寸固定为 112×112.
- 颜色通道顺序 (BGR vs RGB) 必须与实际模型导出一致.
- 像素归一化 (mean / std / 0-1 / -1..1) 必须与实际模型导出一致.
- 输出 embedding 的 L2 normalization 策略必须在模型资产审核中确认:
  - 若模型已内置 L2 归一化, converter 不再重复.
  - 若模型未归一化, converter 必须在写入 metadata 之前归一化.
- pgvector 假设 embedding 已归一化 (使用 cosine 距离). 不一致会导致
  watchlist / live_search 分数偏差, 必须用单元测试锁定.

### 2.6b ArcFace (future embedding alternative)

ArcFace 保留为未来 embedder swap 候选, 不在第一版实现:

- 第一版 embedder 已选 AdaFace in-pipeline.
- ArcFace 切换是 1 个 converter + 1 个 nvinfer 配置 + 模型资产更新
  即可完成的事情, 但必须有 phase doc 推翻 F1.1a 并解释为什么换.

### 2.7 face_reid_gate_pyfunc (F2.2)

职责:

- 评估人脸质量 (confidence, bbox size, landmarks, feature dim, L2 norm, NaN).
- 每 camera+track 限流 (默认 1000ms 间隔).
- 仅 `reid_allowed=true` 的 face 对象传递给下游.
- 不写 Redis, 不访问数据库.

### 2.8 face_observation_exporter_pyfunc (F2.3)

职责:

- 生成 `FaceObservationEventDraft` (含 embedding).
- 写入 Redis Stream `security.face_observations` (仅 `reid_allowed=true`).
- 不写 `security.events` (行为事件由 BehaviorRulesPyFunc 处理).
- 不做复杂检索, 不访问 PostgreSQL, 不调用 FastAPI.

Stream 体规则 (硬约束):

- 包含: camera_id, source_id, track_id, timestamp_ms, person_bbox,
  face_bbox, landmarks, quality, embedding, embedding_model,
  embedding_dim, reid_allowed, association_score, association_method.
- **不包含** JPEG / PNG / RAW / face crop bytes / base64.
- 幂等 key: `face:{source_id}:{track_id}:{timestamp_ms}[:{face_index}]`.

复杂检索 / 业务逻辑由 face-worker 完成.

## 3. module.yml 骨架

> 下方为 F1.1a 锁定后的 **示意骨架**, 不是当前 runtime 文件. 真实的
> module.yml 由后续 F1.1b / F2 实现阶段维护. 这里只用来对齐每个
> element 的角色与位置.

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
    # ----- primary: person + keypoints -----
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

    # ----- primary (full-frame): YOLOv8-Face -----
    # NOT a secondary nvinfer on person ROI — full-frame primary, F1.1a lock.
    - element: nvinfer@detector
      name: yolov8_face
      model:
        format: onnx
        model_file: yolov8_face.onnx
        batch_size: ${oc.decode:${oc.env:FACE_DETECTOR_BATCH_SIZE, 8}}
        precision: fp16
        input:
          shape: [3, 640, 640]
        output:
          layer_names: [output]
          converter:
            module: custom.converters.yolov8_face
            class_name: Yolov8FaceConverter
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

    # ----- face <-> person association (no inference, no crop) -----
    - element: pyfunc
      module: custom.pyfuncs.face_roi_selector
      class_name: FaceRoiSelectorPyFunc   # role redefined per F1.1a
      kwargs:
        min_person_height: 80
        face_attempt_interval_ms: 1000
        min_iou: 0.3

    # ----- AdaFace embedding, in-pipeline -----
    - element: nvinfer@classifier
      name: adaface
      model:
        format: onnx
        model_file: adaface.onnx
        batch_size: ${oc.decode:${oc.env:FACE_EMBEDDING_BATCH_SIZE, 16}}
        precision: fp16
        input:
          object: yolov8_face.face
          shape: [3, 112, 112]
        output:
          layer_names: [embedding]
          converter:
            module: custom.converters.adaface
            class_name: AdaFaceEmbeddingConverter

    # ----- F2.2 quality gate + per-track throttle -----
    - element: pyfunc
      module: custom.pyfuncs.face_reid_gate
      class_name: FaceReidGatePyFunc
      kwargs:
        log_every_n_frames: 30
        face_reid_min_confidence: 0.6
        face_reid_min_face_size: 40.0
        face_reid_min_interval_ms: 1000
        face_reid_norm_tolerance: 0.10

    # ----- F2.3 write reid_allowed=true observations to Redis -----
    - element: pyfunc
      module: custom.pyfuncs.face_observation_exporter
      class_name: FaceObservationExporterPyFunc
      kwargs:
        log_every_n_frames: 30
        producer: savant_security
```

### 3.1 环境变量命名

新命名 (F1.1a 起):

```text
FACE_DETECTOR_BATCH_SIZE        # 替代旧 FACE_BATCH_SIZE (YOLOv8-Face)
FACE_EMBEDDING_BATCH_SIZE       # 替代旧 ARCFACE_BATCH_SIZE (AdaFace)
```

兼容性: 旧 `FACE_BATCH_SIZE` / `ARCFACE_BATCH_SIZE` 在 F1.1b 实现阶
段保留为别名读取, 文档说明后续会移除. 新代码必须使用新名字.

## 4. Converter 要求

### 4.1 Yolo26PoseConverter

必须完成：

- 解析 YOLO26-pose ONNX 输出。
- 输出 person bbox。
- 输出关键点数组。
- 支持 batch 输出。
- 过滤低置信度对象和低置信度关键点。

不得假设 batch 恒为 1。

### 4.2 Yolov8FaceConverter

必须完成：

- 解析 YOLOv8-Face ONNX 输出 (单输出张量, 每行 `[x, y, w, h, conf,
  kp1_x, kp1_y, kp2_x, kp2_y, kp3_x, kp3_y, kp4_x, kp4_y, kp5_x,
  kp5_y]` 或等价 21 列布局, 以实际导出为准).
- 输出 face bbox.
- 输出 5 个 landmarks.
- 支持 batch 输出.
- 支持 NMS.
- 坐标必须在原始全帧坐标系中 (不是 ROI crop 坐标).

不得假设 batch 恒为 1.

### 4.3 AdaFaceEmbeddingConverter

必须完成：

- 读取 embedding tensor.
- 按模型导出确认是否需要 L2 normalization, 不一致时 converter 内做
  归一化, 与 §2.6 预处理要求保持一致.
- 输出统一 FaceEmbedding metadata, 写入 face object 的 attribute.

### 4.4 (future) ScrfdConverter

保留为未来 detector swap 实现, 不在第一版完成. 模型资产清单中 SCRFD
条目保留为 future candidate.

### 4.5 (future) ArcFaceEmbeddingConverter

保留为未来 embedder swap 实现. 与 §2.6b 一致.

## 5. 批处理策略

默认 (F1.1a 命名):

```text
YOLO26-pose batch_size:       8     (POSE_BATCH_SIZE)
YOLOv8-Face batch_size:       8     (FACE_DETECTOR_BATCH_SIZE)
AdaFace batch_size:           16    (FACE_EMBEDDING_BATCH_SIZE)
max_same_source_frames:       1
batched_push_timeout:         40000 us
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

Savant pipeline smoke test 按 F1 / F2 / F3 分阶段验收 (F1.1a 锁定):

**F1 (YOLOv8-Face + face-person association):**

1. module 可启动.
2. YOLO26-pose engine 可加载.
3. YOLOv8-Face engine 可加载.
4. 测试视频可输出 person metadata.
5. 测试视频可输出 face bbox + 5 landmarks.
6. face-person association 能为多数 face 继承 person 的 track_id
   (允许个别 face 因匹配不到 person 而 track_id=None).
7. face quality 字段存在并合理 (0.0 - 1.0).
8. 行为事件链路 (intrusion) 未受人脸链路影响.

**F2 (AdaFace embedding):**

9. AdaFace engine 可加载.
10. `security.face_observations` 携带 512 维 (或模型导出维度) embedding.
11. embedding L2 归一化状态符合 converter 设计 (有 / 无 normalization
    的策略明确, 与 pgvector 假设一致).
12. embedding metadata 不含图片 bytes.

**F3 (face-worker, 不在 Savant 内, 但顺带列出):**

13. face_observations 表可幂等入库.
14. pgvector 检索可触发 watchlist_hit / live_search_hit.
15. hit 事件回写 `security.events`.

不要把 F2 / F3 验收混入 F1 smoke. F1 smoke 不需要 embedding 也不需要
pgvector.

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
