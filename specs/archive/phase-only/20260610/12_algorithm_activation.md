# 12_algorithm_activation.md

## 1. 目标

本规格固化“一般算法如何接入，并且按摄像头动态开关”的设计。

目标：

- 八个算法有统一的全局定义和命名。
- 每个摄像头可以独立启用或停用某些算法。
- 行为类算法进入 Savant 行为规则 runtime，不进入 face-worker。
- 人脸智能保持在 face pipeline 和 face-worker，不和行为规则混在一起。
- event-worker、clip-worker、media-worker 只处理事件、证据和媒体，不实现视觉算法判断。
- 新增算法时有稳定的 registry、API、导出、runtime builder 和测试路径。

本规格不定义具体算法判定逻辑。具体判定阈值和状态机仍放在
`03_behavior_rules.md` 和对应规则实现中。

## 2. 算法清单

系统第一版按八个算法家族管理：

| 算法家族 | 外部 algorithm_id | runtime 类型 | 运行位置 |
| --- | --- | --- | --- |
| 周界入侵 | `behavior.intrusion` | behavior rule | `modules/savant_security/custom/rules/` |
| 徘徊 | `behavior.loitering` | behavior rule | `modules/savant_security/custom/rules/` |
| 人群聚集 | `behavior.crowd_gathering` | behavior rule | `modules/savant_security/custom/rules/` |
| 奔跑 | `behavior.running` | behavior rule | `modules/savant_security/custom/rules/` |
| 追逐 | `behavior.chasing` | behavior rule | `modules/savant_security/custom/rules/` |
| 摔倒 | `behavior.fall` | behavior rule | `modules/savant_security/custom/rules/` |
| 翻越可疑 | `behavior.wall_climb_suspicious` | behavior rule | `modules/savant_security/custom/rules/` |
| 人脸智能 | `face_intelligence` | face intelligence | face pipeline + `services/face-worker/` |

`face_intelligence` 是算法家族，不拆成三个独立算法家族。操作层可以继续有：

```text
face.observation
face.watchlist
face.live_search
```

这些是人脸智能的功能实例或告警配置，不应该和七个行为算法并列成新的算法家族。

命名收敛要求：

- API、DB、导出配置的外部 ID 使用 `algorithm_id`。
- 行为规则内部可以使用短 `rule_type`，例如 `intrusion`、`fall`、`wall_climb`。
- `behavior.wall_climb_suspicious` 映射到内部 `wall_climb` 规则，输出事件仍可使用
  `wall_climb_suspicious`。
- `behavior.chasing` 必须进入 API allowlist 和 runtime export allowlist。

## 3. 三层模型

### 3.1 Algorithm Registry

Algorithm Registry 是全局能力声明，不保存摄像头状态。

每个算法定义必须包含：

```text
algorithm_id
display_name
category
input_requirements
supports_roi
supports_line
default_config
config_schema
default_evidence_policy
enabled
```

`enabled=false` 表示该算法家族当前产品不可用，不表示某个摄像头关闭该算法。

### 3.2 Camera Rule Instance

摄像头上的动态开关必须落在 rule instance 上。一个摄像头可以对同一个算法配置多个实例，例如不同 ROI 的两个入侵规则。

规则实例字段：

```text
rule_id
algorithm_id
enabled
zone_id
line_id
config
severity
cooldown_s
evidence_policy
```

约束：

- `algorithm_id` 必须来自 registry。
- `zone_id` / `line_id` 必须属于当前 camera。
- `enabled=false` 的规则不得进入 Savant `SourceRuntime.rules`。
- 修改 rule instance 只影响该 camera，不影响其他 camera。
- 行为算法的 event cooldown key 必须包含 `camera_id`、`event_type`、`track_id` 或区域键。

### 3.3 Runtime Builder

Runtime Builder 负责把数据库或 YAML 配置转换成运行时对象。

行为规则路径：

```text
camera_rules
  -> cameras.generated.yml
  -> CameraEntry.rules
  -> SourceRuntime.rules
  -> BehaviorRule.evaluate(track)
  -> SecurityEvent
```

人脸智能路径：

```text
camera_rules
  -> algorithm_runtime_config.json
  -> face-worker/watchlist/live-search config
  -> face observation / watchlist_hit / live_search_hit
```

Runtime Builder 的职责：

- 跳过 disabled camera。
- 跳过 disabled rule。
- 校验 algorithm_id、zone_id、line_id、config_schema。
- 将外部 `algorithm_id` 映射为内部 `rule_type`。
- 只实例化已注册的行为规则。
- 对未知但 enabled 的规则在导出阶段报错；runtime 层可以保留“记录并跳过”的防御行为，避免单个错误配置拖垮进程。

## 4. 配置格式

推荐的摄像头配置结构：

```yaml
cameras:
  cam_01:
    camera_id: cam_01
    source_id: cam_01
    enabled: true
    input:
      type: rtsp
      rtsp_url: rtsp://192.168.1.105:8554/live/1080movie
      rtsp_transport: tcp
    zones:
      lobby:
        zone_id: lobby
        zone_type: polygon
        coordinate_space: pixel
        points:
          - [100, 300]
          - [900, 300]
          - [900, 700]
          - [100, 700]
      wall_line:
        zone_id: wall_line
        zone_type: line
        coordinate_space: pixel
        points:
          - [150, 420]
          - [860, 420]
    rules:
      intrusion_lobby:
        rule_id: intrusion_lobby
        algorithm_id: behavior.intrusion
        enabled: true
        rule_kind: alert
        config:
          zone_id: lobby
          min_inside_ms: 1000
          cooldown_s: 30
        evidence_policy:
          snapshot_required: true
          clip_required: true
          pre_seconds: 5
          post_seconds: 10
      fall_lobby:
        rule_id: fall_lobby
        algorithm_id: behavior.fall
        enabled: false
        rule_kind: alert
        config:
          zone_id: lobby
          min_down_ms: 1500
          cooldown_s: 60
      wall_climb_main:
        rule_id: wall_climb_main
        algorithm_id: behavior.wall_climb_suspicious
        enabled: false
        rule_kind: alert
        config:
          line_id: wall_line
          min_crossing_ms: 500
          max_crossing_ms: 5000
          cooldown_s: 60
      watchlist_main:
        rule_id: watchlist_main
        algorithm_id: face.watchlist
        enabled: true
        rule_kind: alert
        config:
          threshold: 0.75
          cooldown_s: 60
```

导出到运行时后，`enabled=false` 的规则可以保留在配置文件中用于审计，但不得进入实际 rule evaluation 列表。

## 5. API 约定

API 保留 `06_api_design.md` 中的摄像头算法规则接口：

```text
GET  /api/v1/algorithms
GET  /api/v1/algorithms/{algorithm_id}
GET  /api/v1/cameras/{camera_id}/algorithm-rules
POST /api/v1/cameras/{camera_id}/algorithm-rules
PUT  /api/v1/cameras/{camera_id}/algorithm-rules/{rule_id}
POST /api/v1/cameras/{camera_id}/algorithm-rules/{rule_id}/enable
POST /api/v1/cameras/{camera_id}/algorithm-rules/{rule_id}/disable
```

创建或更新规则请求：

```json
{
  "rule_id": "loitering_lobby",
  "algorithm_id": "behavior.loitering",
  "enabled": true,
  "zone_id": "lobby",
  "line_id": null,
  "config": {
    "min_duration_s": 60,
    "max_avg_speed_px_s": 20,
    "cooldown_s": 60
  },
  "severity": "medium",
  "evidence_policy": {
    "snapshot_required": true,
    "clip_required": true,
    "pre_seconds": 5,
    "post_seconds": 10
  }
}
```

API 校验规则：

- `algorithm_id` 必须在 allowlist 中。
- 行为类算法必须属于 `behavior.*`。
- 人脸功能实例必须属于 `face.*`，并映射到 `face_intelligence` 家族。
- ROI 算法必须绑定 polygon zone。
- line 算法必须绑定 line 或 direction_line zone。
- 禁用规则时只修改 `enabled=false`，不删除历史配置。
- 删除规则需要单独审计日志，不作为常规开关路径。

## 6. 动态启用策略

### 6.1 短期

短期动态开关采用受控配置刷新：

```text
API 写 DB
  -> export cameras.generated.yml / algorithm_runtime_config.json
  -> 校验通过
  -> 重启或 recreate savant-security / face-worker
  -> 新 runtime 只加载 enabled rules
```

这一阶段实现简单、可审计，适合先把配置模型和算法接入路径跑通。

### 6.2 中期

中期在 Savant pyfunc 中支持文件热加载：

```text
watch config mtime
  -> parse and validate new config
  -> build new SourceRuntime map
  -> atomically swap runtime
  -> validation failed 时保留 last-good runtime
```

要求：

- reload 不能清空 last-good runtime。
- reload 成功和失败都要打结构化日志。
- 单 camera 配置错误不能影响其他 camera 的 last-good runtime。
- reload 后 disabled rule 不再产生新事件，但历史 cooldown 和已生成事件不回滚。

### 6.3 长期

长期可以用 Redis pub/sub 或 Redis Stream 做局部刷新：

```text
security.runtime_config_updates
  camera_id
  changed_rule_ids
  config_version
  generated_at
```

runtime 收到更新后只重建对应 camera 的 SourceRuntime。

## 7. 性能能力聚合

动态开关分两层：

1. 决策层开关：某规则是否评估、是否报警。
2. 模型能力开关：该 camera 是否需要跑某些模型。

第一阶段必须先保证决策层开关正确。模型链是否按 camera 裁剪可以后续优化。

Runtime export 应能按 enabled rules 聚合 camera capabilities：

| capability | 触发条件 |
| --- | --- |
| `needs_person_bbox` | 任一行为规则启用 |
| `needs_pose_keypoints` | `fall`、`wall_climb_suspicious` 或其他依赖姿态的规则启用 |
| `needs_track_velocity` | `running`、`chasing` 或其他依赖速度的规则启用 |
| `needs_multi_track_state` | `crowd_gathering`、`chasing` 启用 |
| `needs_face_detection` | 任一 `face.*` 规则启用 |
| `needs_face_embedding` | `face.watchlist` 或 `face.live_search` 启用 |
| `needs_face_reid` | `face.watchlist` 或跨帧身份聚合启用 |

后续如果要节省 GPU，可由 source controller 或 pipeline profile 根据 capabilities 选择轻量 pipeline。
在此之前，即使模型仍然运行，disabled rule 也不得输出事件。

## 8. 新增行为算法步骤

新增一个行为算法时按以下顺序落地：

1. 在 Algorithm Registry 增加算法定义。
2. 在 API schema 和 runtime export allowlist 增加 `algorithm_id`。
3. 定义 `config_schema`、默认阈值、默认证据策略。
4. 在 `modules/savant_security/custom/rules/` 增加纯 Python 规则。
5. 在 `custom/rules/__init__.py` 导入规则模块，触发 `@register_rule`。
6. 在 runtime builder 增加 `algorithm_id -> rule_type` 映射。
7. 增加单元测试：正例、反例、阈值边界、cooldown、disabled rule。
8. 增加 export/API contract 测试，确认启用和停用只影响目标 camera。

推荐实现顺序：

```text
loitering
running
crowd_gathering
fall
wall_climb_suspicious
chasing
```

原因是 `loitering` 和 `running` 只依赖单 track 状态，风险最低；`chasing` 依赖多 track 关系，最后实现。

## 9. 验收标准

配置和 API：

- `GET /api/v1/algorithms` 返回八个算法家族。
- API allowlist 包含 `behavior.chasing`。
- API 能创建、启用、停用单 camera 的 algorithm rule。
- 禁用规则不删除配置，重新启用后保留原 config。
- 错误的 `zone_id`、`line_id`、`algorithm_id` 在导出或 API 层失败。

Runtime：

- disabled camera 不进入 runtime。
- disabled rule 不进入 `SourceRuntime.rules`。
- camera A 启用某算法不会让 camera B 产生同类事件。
- enabled behavior rule 只在 Savant behavior runtime 评估。
- face watchlist/live search 不进入 behavior rule registry。
- runtime reload 失败时保留 last-good runtime。

证据和告警：

- 所有规则输出统一 `SecurityEvent`。
- `algorithm_id`、`rule_id`、`camera_id`、`zone_id` 或 `line_id` 必须进入事件 payload 或顶层字段。
- event-worker 只按事件合同创建告警和证据任务，不反向实现算法判断。

测试：

- 每个新增行为规则必须有纯 Python 单元测试。
- runtime export 必须有 camera/rule enablement contract 测试。
- operator/API 静态测试必须覆盖规则 enable/disable 基本流程。
- 热加载上线前必须有 last-good runtime 回退测试。

## 10. 当前代码收敛事项

以下是本规格要求的后续实现收敛点，不属于本规格文档本身的代码改动：

- API 和 runtime export allowlist 需要补齐 `behavior.chasing`。
- `wall_climb` 的 registry 名称和 `behavior.wall_climb_suspicious` 的外部 ID 需要显式映射。
- `rule_runtime.camera_entry_to_legacy_config()` 当前按 rule key 当作 `rule_type`，后续要改为按 `algorithm_id` 映射内部 `rule_type`。
- `custom/rules/__init__.py` 当前只导入 intrusion，新增规则必须在这里导入以完成注册。
- `algorithm_runtime_config.json` 后续应输出 camera capabilities，给模型级开关或 pipeline profile 使用。
