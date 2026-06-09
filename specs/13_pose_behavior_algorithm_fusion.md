# 13_pose_behavior_algorithm_fusion.md

## 1. 目标

本规格固化 `pose_alg_code.zip` 中摔倒、追逐、人群聚集三类姿态/轨迹规则的融合计划、运行时契约和 harness 测试要求。

结论：

- `pose_alg_code.zip` 只能作为参考实现，不得原样覆盖现有 runtime。
- `fall` 是单 track 规则，可以先融合。
- `crowd_gathering` 和 `chasing` 是 frame-level multi-track 规则，必须先补运行时调用契约。
- 三类规则上线前必须修正事件 payload 合并、规则配置透传和 per-camera 阈值配置。

本规格只定义接入计划和测试合同，不承诺现场准确率。像素距离、像素速度和姿态阈值必须按摄像头视角标定。

## 2. 输入代码来源

当前参考包：

```text
pose_alg_code.zip
  fall.py
  chase.py
  crowd.py
  multitrack_base.py
  __init__.py
```

参考实现中的可复用部分：

- `fall.py` 的姿态投票、upright -> lying 转换检测、持续倒地抑制。
- `chase.py` 的 `track_kinematics()` 和 `detect_chase_pairs()` 纯几何判断。
- `crowd.py` 的邻近聚类、成员重叠维持 cluster id、迟滞双阈值状态机。

需要重写或适配的部分：

- env-only 阈值读取。
- 事件 payload 缺少统一 algorithm/rule/camera/zone 字段。
- `multitrack_base.py` 用隐藏缓冲适配 per-track loop，存在一帧延迟和 pending event drain 风险。
- 多 track 规则没有显式 frame-level runtime contract。

## 3. 当前主线约束

行为规则当前主循环是单 track 模式：

```text
runtime.store.update(observations)
for track in runtime.store.active_tracks:
  for rule in runtime.rules:
    event = rule.evaluate(track)
```

当前必须保持的主线约束：

- 行为规则仍位于 `modules/savant_security/custom/rules/`。
- 规则层必须是纯 Python，不得导入 Savant 或 DeepStream。
- `SecurityEvent` 是唯一行为事件输出合同。
- `event-worker`、`clip-worker`、`media-worker` 不实现视觉算法判断。
- disabled camera 和 disabled rule 不得进入实际 rule evaluation。
- face rules 不得进入 behavior rule registry。

当前需要先修的融合阻塞点：

- `RuleConfig` 需要携带原始 `rule_entry.config`，否则 per-rule 阈值无法传入规则。
- `_enrich_and_export()` 需要 merge 规则 payload，不能直接覆盖。
- multi-track 规则需要显式每帧调用，不建议依赖隐藏缓冲适配器作为长期方案。

## 4. 配置合同

### 4.1 配置来源优先级

规则阈值按以下顺序合并：

```text
implementation defaults
  < Algorithm Registry default_config
  < camera rule instance config
  < deployment env override
```

env override 只能作为部署级紧急开关或临时压测手段。长期调参必须使用 camera rule instance config，因为像素阈值依赖摄像头视角。

### 4.2 RuleConfig 扩展

运行时 `RuleConfig` 必须保留通用字段和原始配置：

```text
name
rule_type
zone
enabled
min_inside_ms
cooldown_s
severity
snapshot_required
clip_required
config
algorithm_id
```

其中：

- `name` 等于 `rule_id`。
- `rule_type` 是内部短名，例如 `fall`、`chasing`、`crowd_gathering`。
- `algorithm_id` 是外部 ID，例如 `behavior.fall`。
- `config` 是 API/export 原始 JSON config 的浅拷贝。

### 4.3 Canonical 外部配置字段

外部 API 和当前 midterm 运行配置使用以下 canonical 字段。

`behavior.fall`：

```yaml
config:
  zone_id: lobby
  min_down_ms: 1500
  cooldown_s: 60
  require_transition: true
  lying_aspect_ratio: 0.85
  upright_aspect_ratio: 0.55
  torso_horizontal_deg: 45.0
  head_hip_collapse_ratio: 0.22
  min_visible_keypoints: 0
  keypoint_threshold: 0.25
```

兼容别名：

- `min_on_ground_ms` -> `min_down_ms`
- `fall_transition_window_ms` -> `transition_window_ms`

`behavior.crowd_gathering`：

```yaml
config:
  zone_id: lobby
  min_person_count: 5
  exit_person_count: 3
  min_duration_s: 2
  eps_px: 180.0
  require_in_zone: true
  cooldown_s: 60
```

兼容别名：

- `count_high` -> `min_person_count`
- `count_low` -> `exit_person_count`
- `min_duration_ms` -> `min_duration_s`

`behavior.chasing`：

```yaml
config:
  zone_id: lobby
  min_speed_px_s: 120.0
  max_distance_px: 220.0
  min_cos_alignment: 0.80
  speed_ratio_tolerance: 0.60
  behind_cos_min: 0.50
  velocity_window_ms: 700
  min_pair_duration_s: 1.5
  cooldown_s: 30
```

兼容别名：

- `max_pair_distance_px` -> `max_distance_px`
- `min_duration_ms` -> `min_pair_duration_s`

## 5. 事件合同

所有新增规则输出 `SecurityEvent` 时必须包含统一字段：

```text
event_type
algorithm_type
camera_id
source_id
track_id
start_ts_ms
end_ts_ms
confidence
severity
zone
rule_name
snapshot_required
clip_required
payload
```

`payload` 必须至少包含：

```json
{
  "algorithm_id": "behavior.fall",
  "rule_id": "fall_lobby",
  "camera_id": "cam_001",
  "zone_id": "lobby"
}
```

`_enrich_and_export()` 只能追加通用字段，不得删除规则私有字段。通用 enrichment 字段包括：

- `person_bbox`
- `person_confidence`
- `visible_keypoint_count`
- `person_quality_gate`
- `media`
- `rule`

intrusion 专属字段如 `inside_ms` 只能由 intrusion 或明确适用的规则写入。

### 5.1 Fall payload

`fall` 事件 payload 必须包含：

```json
{
  "algorithm_id": "behavior.fall",
  "rule_id": "fall_lobby",
  "camera_id": "cam_001",
  "zone_id": "lobby",
  "track_id": 7,
  "on_ground_ms": 1300,
  "require_transition": true,
  "transition_window_ms": 1500,
  "posture": {
    "aspect_ratio": 0.94,
    "torso_angle_deg": 68.0,
    "head_hip_ratio": 0.12,
    "available_votes": 3,
    "is_lying": true,
    "is_upright_before": true
  }
}
```

### 5.2 Crowd payload

`crowd_gathering` 事件 payload 必须包含：

```json
{
  "algorithm_id": "behavior.crowd_gathering",
  "rule_id": "crowd_lobby",
  "camera_id": "cam_001",
  "zone_id": "lobby",
  "cluster_id": 3,
  "member_track_ids": [1, 2, 3, 4, 5],
  "person_count": 5,
  "duration_ms": 2100,
  "centroid": {"x": 320.0, "y": 460.0},
  "eps_px": 180.0
}
```

区域型 crowd 事件可以使用 `track_id=null` 或 `track_id=0`，但 `source_event_id` 必须稳定，建议包含 `camera_id:event_type:zone_id:cluster_id:start_ts_ms`。

### 5.3 Chasing payload

`chasing` 事件 payload 必须包含：

```json
{
  "algorithm_id": "behavior.chasing",
  "rule_id": "chasing_lobby",
  "camera_id": "cam_001",
  "zone_id": "lobby",
  "leader_track_id": 11,
  "follower_track_id": 12,
  "distance_px": 160.0,
  "alignment": 0.91,
  "leader_speed_px_s": 180.0,
  "follower_speed_px_s": 175.0,
  "duration_ms": 1600
}
```

`track_id` 顶层字段应使用 `follower_track_id`，便于按追逐发起者查事件。

## 6. 四阶段 goal 融合计划

一个 implementation goal 必须按本节四个阶段顺序执行。每个阶段都要先补 harness，再实现代码，再跑该阶段验收；不得跳过前置阶段直接接规则。

### 6.1 Stage 1: Runtime contract and harness foundation

目标：先把三类规则需要的公共合同补齐，避免后续规则事件和配置被 runtime 吃掉。

改动范围：

```text
modules/savant_security/custom/models/camera_config.py
modules/savant_security/custom/services/rule_runtime.py
modules/savant_security/custom/pyfuncs/behavior_rules.py
harness/tests/test_pose_rule_config_contract.py
harness/tests/test_pose_behavior_event_payload_contract.py
```

要求：

- 只读检查 `pose_alg_code.zip`，提取可复用逻辑，不原样覆盖现有 runtime。
- `RuleConfig` 保留 `config` 和 `algorithm_id`。
- `camera_entry_to_legacy_config()` 不丢原始 rule config。
- `_enrich_and_export()` merge `event.payload`，不得覆盖规则私有字段。
- enrichment 支持没有单一代表 track 的 frame-level event。
- Redis 仍只作为 `SecurityEvent` 的异步 stream buffer，不承担规则判断。

阶段验收：

```text
pytest -q harness/tests/test_pose_rule_config_contract.py
pytest -q harness/tests/test_pose_behavior_event_payload_contract.py
git diff --check -- \
  modules/savant_security/custom/models/camera_config.py \
  modules/savant_security/custom/services/rule_runtime.py \
  modules/savant_security/custom/pyfuncs/behavior_rules.py \
  harness/tests/test_pose_rule_config_contract.py \
  harness/tests/test_pose_behavior_event_payload_contract.py
```

### 6.2 Stage 2: Fall single-track rule

目标：先落地风险最低的单 track fall 规则，验证规则注册、配置读取、payload 和 cooldown 合同。

改动范围：

```text
modules/savant_security/custom/rules/fall.py
modules/savant_security/custom/rules/__init__.py
harness/tests/test_fall_rule.py
harness/tests/test_pose_behavior_rule_activation.py
```

要求：

- 复用 `fall.py` 的姿态投票、upright -> lying 转换检测、持续倒地抑制。
- 支持 camera rule config + env override。
- 支持 `min_visible_keypoints` 质量门。
- 默认保持 `require_transition=true`。
- keypoints 缺失时允许 bbox-only fallback，但 payload 必须标记 `pose_quality_mode=bbox_only`。
- 输出 payload 必须符合 5.1。

阶段验收：

```text
pytest -q harness/tests/test_fall_rule.py
pytest -q harness/tests/test_pose_behavior_rule_activation.py
pytest -q harness/tests/test_pose_behavior_event_payload_contract.py
git diff --check -- \
  modules/savant_security/custom/rules/fall.py \
  modules/savant_security/custom/rules/__init__.py \
  harness/tests/test_fall_rule.py \
  harness/tests/test_pose_behavior_rule_activation.py
```

### 6.3 Stage 3: Frame-level runtime and crowd_gathering

目标：补显式 frame-level rule 执行器，并用 crowd 规则验证 multi-track runtime 合同。

改动范围：

```text
modules/savant_security/custom/rules/base.py
modules/savant_security/custom/rules/crowd_gathering.py
modules/savant_security/custom/rules/__init__.py
modules/savant_security/custom/services/rule_runtime.py
modules/savant_security/custom/pyfuncs/behavior_rules.py
harness/tests/test_frame_behavior_rule_runtime.py
harness/tests/test_crowd_gathering_rule.py
harness/tests/test_pose_behavior_rule_activation.py
```

新增抽象：

```text
FrameBehaviorRule.evaluate_frame(frame_tracks, frame_ts_ms) -> list[SecurityEvent]
```

运行时处理顺序：

```text
runtime.store.update(observations)
active_tracks = runtime.store.active_tracks

for single_track_rule in runtime.single_track_rules:
  for track in active_tracks:
    evaluate(track)

for frame_rule in runtime.frame_rules:
  evaluate_frame(active_tracks, frame_ts_ms)
```

要求：

- single-track 和 frame-level rules 在 `SourceRuntime` 中分组，避免每帧重复判定。
- frame-level rule 每个 source 每帧只运行一次。
- 不使用隐藏 pending queue 作为长期方案。
- frame-level event enrichment 不依赖 `track.current_bbox` 必然存在。
- `crowd_gathering` 复用邻近聚类和迟滞状态机。
- `crowd_gathering` 支持 `zone_id` polygon 内过滤。
- payload 包含 cluster、成员、人数、centroid 和 duration，符合 5.2。

阶段验收：

```text
pytest -q harness/tests/test_frame_behavior_rule_runtime.py
pytest -q harness/tests/test_crowd_gathering_rule.py
pytest -q harness/tests/test_pose_behavior_rule_activation.py
pytest -q harness/tests/test_pose_behavior_event_payload_contract.py
git diff --check -- \
  modules/savant_security/custom/rules/base.py \
  modules/savant_security/custom/rules/crowd_gathering.py \
  modules/savant_security/custom/rules/__init__.py \
  modules/savant_security/custom/services/rule_runtime.py \
  modules/savant_security/custom/pyfuncs/behavior_rules.py \
  harness/tests/test_frame_behavior_rule_runtime.py \
  harness/tests/test_crowd_gathering_rule.py
```

### 6.4 Stage 4: Chasing and full validation

目标：最后接入追逐规则，并做完整目标测试、doctor、compose 和 diff 检查。

改动范围：

```text
modules/savant_security/custom/rules/chasing.py
modules/savant_security/custom/rules/__init__.py
harness/tests/test_chasing_rule.py
harness/tests/test_pose_behavior_rule_activation.py
```

要求：

- 复用 `track_kinematics()` 和 `detect_chase_pairs()`。
- 支持 zone scope。
- follower/leader 判定必须进入 payload。
- cooldown key 必须包含 `camera_id`、`zone_id`、leader/follower pair。
- 并肩快走、相向跑、距离过远、速度差过大都不得报警。
- payload 必须符合 5.3。

完整验收命令：

```text
pytest -q \
  harness/tests/test_pose_rule_config_contract.py \
  harness/tests/test_pose_behavior_event_payload_contract.py \
  harness/tests/test_frame_behavior_rule_runtime.py \
  harness/tests/test_fall_rule.py \
  harness/tests/test_crowd_gathering_rule.py \
  harness/tests/test_chasing_rule.py \
  harness/tests/test_pose_behavior_rule_activation.py

bash scripts/runtime/doctor_midterm.sh
docker compose -f infra/docker-compose.midterm.yml config
git diff --check
```

如果开启真实视频 smoke，只能验证事件链路可运行，不能把默认阈值下的检测结果当成生产准确率证明。

### 6.5 Single goal objective 模板

后续可以用一个 goal 执行完整融合，objective 建议写成：

```text
Implement specs/13_pose_behavior_algorithm_fusion.md end-to-end using the four-stage plan:
Stage 1 runtime contract and harness foundation,
Stage 2 fall single-track rule,
Stage 3 frame-level runtime plus crowd_gathering,
Stage 4 chasing plus full validation.

Preserve unrelated dirty worktree changes. Treat pose_alg_code.zip as a reference only, not as a wholesale patch. Keep rule code pure Python and inside the Savant behavior runtime; Redis remains only the SecurityEvent stream buffer. Add the harness tests named in the spec, make them pass, and do not mark the goal complete until the full Stage 4 acceptance commands pass or a concrete blocker is reported with exact failing commands.
```

## 7. Harness 测试设计

所有新增 harness 测试默认纯 Python，不依赖 GPU、视频文件、Redis、PostgreSQL 或 Docker。

### 7.1 `test_pose_rule_config_contract.py`

目标：防止 API/export 配置进入 runtime 时丢字段。

必须覆盖：

- `behavior.fall` 的 `min_down_ms`、`require_transition`、`min_visible_keypoints` 进入 `RuleConfig.config`。
- `behavior.crowd_gathering` 的 `min_person_count`、`exit_person_count`、`eps_px` 进入 `RuleConfig.config`。
- `behavior.chasing` 的 `min_pair_duration_s`、`max_distance_px`、`min_speed_px_s` 进入 `RuleConfig.config`。
- `algorithm_id` 映射为内部 `rule_type`。
- disabled behavior rule 不进入 runtime。
- face rule 不进入 behavior runtime。

### 7.2 `test_pose_behavior_event_payload_contract.py`

目标：防止 enrichment 覆盖规则 payload。

必须覆盖：

- 已有 payload 中的 `algorithm_id`、`rule_id`、`zone_id` 保留。
- fall 私有 `posture` 字段保留。
- crowd 私有 `member_track_ids`、`cluster_id` 保留。
- chasing 私有 `leader_track_id`、`follower_track_id` 保留。
- enrichment 追加 `media`、`person_bbox`、`person_quality_gate`。
- intrusion 的 `inside_ms` 不被错误添加到 crowd/chasing/fall。

### 7.3 `test_fall_rule.py`

目标：验证单 track fall 状态机。

必须覆盖：

- 站立 -> 快速躺倒 -> 持续超过 `min_down_ms` 产生 `fall`。
- 躺倒持续不足阈值不报警。
- 已经躺在画面里且 `require_transition=true` 不报警。
- `require_transition=false` 时持续躺倒可以报警。
- 短暂倒地后恢复站立不报警。
- 弯腰、蹲下、俯卧撑式横向 bbox 反例不报警。
- keypoints 缺失时允许 bbox-only fallback，但 payload 必须标记。
- `min_visible_keypoints` 不满足时不报警。
- cooldown 内不重复报警。

### 7.4 `test_crowd_gathering_rule.py`

目标：验证 frame-level crowd 聚类和迟滞。

必须覆盖：

- ROI 内 5 人邻近聚集持续超过阈值产生 `crowd_gathering`。
- 人数短暂达到阈值但未持续不报警。
- 人数低于 `min_person_count` 不报警。
- 达到 high 后跌到 high-1 但仍高于 `exit_person_count` 不清除计时。
- 跌到 `exit_person_count` 或更低清除 crowd 状态。
- ROI 外人员不计入。
- 两个分离 cluster 不合并。
- cluster 成员小幅变化时通过成员重叠保持同一 `cluster_id`。
- cooldown key 按 camera/zone/cluster 隔离。

### 7.5 `test_chasing_rule.py`

目标：验证追逐 pair 纯几何判定和持续状态。

必须覆盖：

- 两人高速、近距离、同向、前后排列并持续超过阈值产生 `chasing`。
- 并肩快走不报警。
- 相向奔跑不报警。
- 一快一慢速度差过大不报警。
- 距离超过 `max_distance_px` 不报警。
- 持续不足 `min_pair_duration_s` 不报警。
- follower/leader 判定稳定进入 payload。
- pair 中断后计时重置。
- cooldown key 按 camera/zone/pair 隔离。

### 7.6 `test_frame_behavior_rule_runtime.py`

目标：验证 multi-track runtime 执行点。

必须覆盖：

- frame-level rule 每帧只调用一次。
- 同一帧内多个 active tracks 不导致重复 detect。
- frame-level rule 可以返回多个事件且全部 export。
- 没有 active tracks 时不产生事件且状态按规则清理。
- source A 和 source B 的 frame-level 状态不串扰。
- 最后一帧不依赖下一帧才能 flush 事件。

### 7.7 `test_pose_behavior_rule_activation.py`

目标：验证算法激活链路。

必须覆盖：

- `behavior.fall` -> `fall`。
- `behavior.crowd_gathering` -> `crowd_gathering`。
- `behavior.chasing` -> `chasing`。
- 三类规则在 `custom.rules.REGISTRY.known_rule_types()` 中注册。
- unknown enabled behavior rule 在 runtime 防御性跳过并记录日志。
- camera A 启用 fall 不会让 camera B 产生 fall 事件。

## 8. 测试数据构造规范

测试应使用 synthetic `TrackState` 和 `PersonPoseObservation`，避免视频依赖。

基础 helper 建议：

```text
make_obs(track_id, ts_ms, bbox, keypoints=None, camera_id="cam_001")
make_track(track_id, observations)
make_frame_tracks(track_specs, ts_ms)
make_coco_keypoints(...)
```

bbox 统一使用：

```text
BBox(x, y, width, height)
foot_point = (x + width / 2, y + height)
```

姿态关键点使用 COCO17 名称，fall 至少构造：

```text
nose
left_shoulder
right_shoulder
left_hip
right_hip
```

测试不得使用随机数。必须使用固定 timestamp 和坐标，保证回归稳定。

## 9. 质量门和标定要求

### 9.1 Fall

fall 规则默认允许关键点缺失时退化到 bbox-only，但必须满足：

- payload 标记 `pose_quality_mode=bbox_only`。
- 可通过 `min_visible_keypoints` 禁用 bbox-only 报警。
- `require_transition` 默认开启。
- 现场部署前必须用真实摄像头样本验证弯腰、蹲下、坐下、俯卧撑反例。

### 9.2 Crowd 和 Chasing

crowd 和 chasing 的像素阈值必须按 camera 配置。

如果后续支持单应矩阵或地面坐标，规则输入应切换为 ground-plane 坐标：

```text
foot_point_px -> ground_point_m
speed_px_s -> speed_m_s
distance_px -> distance_m
```

在 ground-plane 坐标接入前，不允许把一组全局默认阈值当作跨摄像头生产配置。

## 10. 非目标

本融合阶段不做：

- 不训练新模型。
- 不修改 Savant model chain。
- 不修改 face-worker。
- 不修改 evidence viewer UI。
- 不做在线热加载。
- 不承诺 fall/chasing/crowd 的生产准确率。
- 不用视频 smoke 替代纯规则 harness。

## 11. 最小完成标准

可以认为融合完成的最低标准：

- 三类规则都已注册，并能从 `behavior.*` algorithm_id 实例化。
- `RuleConfig.config` 完整透传。
- `_enrich_and_export()` 保留规则 payload。
- fall 纯单元测试覆盖转换、持续、恢复抑制、bbox-only、关键点质量门。
- crowd 纯单元测试覆盖聚类、迟滞、zone、cluster identity。
- chasing 纯单元测试覆盖 pair 几何、leader/follower、持续、反例。
- frame-level runtime 测试证明 multi-track 规则每帧只跑一次且不丢事件。
- 目标 pytest、doctor、compose config、`git diff --check` 通过。
