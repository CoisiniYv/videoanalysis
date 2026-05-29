# 03_behavior_rules.md

## 1. 行为规则设计目标

行为规则负责将 YOLO26-pose 和 nvtracker 产生的时序 metadata 转换为业务事件。

输入：

```text
PersonPoseObservation
TrackState
CameraConfig
ROI / line / thresholds
```

输出：

```text
SecurityEvent
```

所有行为规则必须可以脱离 Savant 单独测试。

## 2. 核心数据结构

### 2.1 BBox

```python
class BBox:
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]: ...

    @property
    def foot_point(self) -> tuple[float, float]: ...
```

### 2.2 Keypoint

```python
class Keypoint:
    name: str
    x: float
    y: float
    confidence: float
```

COCO 17 keypoints 建议统一命名：

```text
nose
left_eye
right_eye
left_ear
right_ear
left_shoulder
right_shoulder
left_elbow
right_elbow
left_wrist
right_wrist
left_hip
right_hip
left_knee
right_knee
left_ankle
right_ankle
```

### 2.3 PersonPoseObservation

```python
class PersonPoseObservation:
    source_id: str
    camera_id: str
    frame_id: int
    timestamp_ms: int
    bbox: BBox
    confidence: float
    keypoints: list[Keypoint]
    keypoint_confidence: float | None
    track_id: str | None
```

### 2.4 TrackState

```python
class TrackState:
    source_id: str
    camera_id: str
    track_id: str
    observations: list[PersonPoseObservation]
    first_seen_ms: int
    last_seen_ms: int
    current_bbox: BBox
    current_keypoints: list[Keypoint]
    avg_speed_px_s: float
    direction: tuple[float, float] | None
    inside_zones: set[str]
    flags: dict
```

### 2.5 SecurityEvent

```python
class SecurityEvent:
    event_type: str
    source_event_id: str
    source_id: str
    camera_id: str
    track_id: str | None
    person_id: int | None
    algorithm_type: str
    algorithm_version: str | None
    start_ts_ms: int
    end_ts_ms: int | None
    confidence: float
    severity: str
    snapshot_required: bool
    clip_required: bool
    evidence_policy: dict
    payload: dict
```

R3 locks this as the common event contract for all behavior algorithms and face
intelligence events. Algorithm-specific values must be placed in `payload`.

## 3. 通用规则机制

### 3.1 Cooldown

每个事件必须支持 cooldown，避免重复报警。

事件去重键建议：

```text
camera_id + event_type + track_id + zone_id
```

### 3.2 ROI

支持：

- polygon。
- line。
- direction line。

### 3.3 时间窗口

TrackState 应保留最近 N 秒 observations。

默认：

```text
short_window: 3s
medium_window: 10s
long_window: 60s
```

### 3.4 置信度

事件 confidence 来源可以组合：

```text
模型置信度
track 稳定性
规则持续时间
关键点可见性
ROI 命中程度
```

第一版可用规则评分，不强制机器学习评分。

## 4. 周界入侵

### 4.1 输入

- track。
- polygon ROI。
- `min_inside_ms`。
- `cooldown_s`。

### 4.2 判断逻辑

```text
person foot_point 进入 ROI
  + 连续停留 >= min_inside_ms
  + track 已确认
  -> intrusion
```

### 4.3 输出事件

```json
{
  "event_type": "intrusion",
  "severity": "medium",
  "snapshot_required": true,
  "clip_required": true,
  "payload": {
    "zone_id": "perimeter",
    "inside_ms": 1200
  }
}
```

## 5. 翻墙 / 翻越可疑

### 5.1 输入

- track。
- wall_line。
- bbox 序列。
- keypoints 序列。

### 5.2 判断逻辑

第一版定义为可疑事件：`wall_climb_suspicious`。

```text
track 从 wall_line 一侧移动到另一侧
  + bbox 中心点高度发生明显变化
  + 髋/膝/踝关键点支持跨越动作
  + 动作持续时间在合理范围内
  -> wall_climb_suspicious
```

### 5.3 注意事项

- 不要第一版承诺绝对翻墙确认。
- 每个摄像头的 wall_line 必须人工配置。
- 需要通过现场数据调整阈值。

## 6. 徘徊

### 6.1 输入

- track。
- polygon ROI。
- `min_duration_s`。
- `max_avg_speed_px_s`。
- `max_motion_range_px`，可选。

### 6.2 判断逻辑

```text
track 在 ROI 内持续 >= min_duration_s
  + avg_speed <= max_avg_speed_px_s
  + 不属于正常通行轨迹
  -> loitering
```

### 6.3 输出事件

```json
{
  "event_type": "loitering",
  "payload": {
    "zone_id": "perimeter",
    "duration_s": 35,
    "avg_speed_px_s": 22
  }
}
```

## 7. 人群聚集

### 7.1 输入

- ROI。
- 当前 person tracks。
- `min_person_count`。
- `min_duration_s`。

### 7.2 判断逻辑

```text
ROI 内 person_count >= min_person_count
  + 持续 >= min_duration_s
  -> crowd_gathering
```

### 7.3 注意事项

- 人群聚集是区域事件，不一定绑定单个 track_id。
- 事件 payload 应包含人数和 zone_id。

## 8. 奔跑

### 8.1 输入

- track bbox 序列。
- keypoints 序列。
- `min_speed_px_s`。
- `min_duration_ms`。

### 8.2 判断逻辑

```text
track speed >= min_speed_px_s
  + 持续 >= min_duration_ms
  + 可选：关键点步幅/摆臂支持
  -> running
```

### 8.3 注意事项

- 像素速度依赖摄像头视角，必须支持按摄像头配置。
- 后期可加入透视校正。

## 9. 追逐

### 9.1 输入

- 多个 running tracks。
- track A/B 方向。
- track A/B 距离变化。

### 9.2 判断逻辑

```text
A 和 B 都高速移动
  + A 持续接近 B
  + A/B 方向相似
  + 距离持续缩短
  -> chasing
```

### 9.3 MVP 策略

第一版先做 running，chasing 作为增强规则。

## 10. 摔倒

### 10.1 输入

- bbox 宽高比。
- 中心点高度变化。
- keypoints，尤其肩、髋、膝、踝。
- 静止时间。

### 10.2 判断逻辑

```text
人体高度快速下降
  + bbox 从竖直变横向
  + 肩-髋连线接近水平
  + 倒地后静止 >= min_static_s
  -> fall
```

### 10.3 输出事件

```json
{
  "event_type": "fall",
  "severity": "high",
  "snapshot_required": true,
  "clip_required": true,
  "payload": {
    "static_s": 3.2,
    "body_angle": 78.0,
    "height_drop_px": 120
  }
}
```

## 11. 测试要求

每个规则必须有以下类型测试：

- 正例。
- 反例。
- 阈值边界。
- cooldown。
- 多 track。
- track 消失后的状态清理。

示例测试文件：

```text
harness/tests/test_intrusion.py
harness/tests/test_loitering.py
harness/tests/test_crowd_gathering.py
harness/tests/test_running.py
harness/tests/test_fall.py
```
