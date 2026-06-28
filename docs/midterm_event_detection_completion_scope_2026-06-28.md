# Midterm 事件检测补全阶段边界 - 2026-06-28

本文固化 2026-06-28 对当前算法支持状态的阶段性发现，并给出下一步补全事件检测代码时的并行工作边界。

核心目标：开始补齐事件检测能力，但不干扰正在进行的性能测试和 60 路/30 路扩展验证。

## 当前算法支持状态

当前 8090/API 的权威入口是：

```text
GET /api/v1/algorithms/support-matrix
```

当前矩阵含义：

| 状态 | 含义 |
| --- | --- |
| `production_ready` | 8090 配置会控制真实运行时检测、事件和证据链路 |
| `event_only` | 运行时可以检测/出事件，但默认证据链路还不是生产闭环 |
| `config_only` | 配置可见或可保存，但当前不是 per-camera 运行时 gate |
| `unsupported` | 当前规则模块未实现或未注册，运行时会跳过 |
| `deferred` | 产品契约存在，但实现明确延期 |

当前支持矩阵：

| 算法 | 当前状态 | 当前能力 | 主要缺口 |
| --- | --- | --- | --- |
| `behavior.intrusion` | `production_ready` | 入侵检测；per-camera rule gate；事件、record request、证据链路可闭环 | 保持回归稳定 |
| `face.watchlist` | `production_ready` | 名单命中；per-camera 目标名单/阈值；face-worker 运行时消费；证据链路可闭环 | 需要继续做端到端样本回归 |
| `behavior.crowd_gathering` | `event_only` | `custom.rules.crowd_gathering` 已注册，可基于多目标聚集出事件 | 非 intrusion 行为证据 materialization 仍未生产闭环 |
| `behavior.fall` | `event_only` | `custom.rules.fall` 已注册，可基于姿态/框形态出事件 | 需要标准样本、误报控制和证据闭环 |
| `behavior.chasing` | `event_only` | `custom.rules.chasing` 已注册，可基于多轨迹运动关系出事件 | 需要真实样本、阈值校准和证据闭环 |
| `face.observation` | `config_only` | 底层 YOLOv8-Face、AdaFace、ReID gate/exporter 已存在 | 不是 per-camera rule gate；由 pipeline/env 控制 |
| `behavior.loitering` | `event_only` | `custom.rules.loitering` 已注册，可基于 ROI 停留和低速出事件 | 非 intrusion 行为证据 materialization 仍未生产闭环 |
| `behavior.running` | `event_only` | `custom.rules.running` 已注册，可基于轨迹速度和持续时间出事件 | 非 intrusion 行为证据 materialization 仍未生产闭环 |
| `behavior.wall_climb_suspicious` | `unsupported` | 有算法 ID/配置契约，配置层要求 line zone | 缺 `custom.rules.wall_climb` 规则实现和注册 |
| `face.live_search` | `deferred` | 有产品/事件契约 | 缺 runtime 查询、目标选择、事件和证据链路 |

底层模型链已经存在：

```text
YOLO26-pose/person -> nvtracker -> behavior_rules
YOLOv8-Face -> face/person association -> AdaFace -> ReID gate -> face_observation_exporter
```

因此下一阶段主要不是“再接模型”，而是补齐业务规则、事件语义、证据链路、8090 gate 和验收样本。

## 补全阶段建议

### Phase E1: 先把已注册规则做稳

范围：

- `behavior.crowd_gathering`
- `behavior.fall`
- `behavior.chasing`

目标：

- 保持规则模块和 API support matrix 一致；
- 增加真实或半真实样本测试；
- 增加事件 payload、cooldown、zone、track/frame identity 的 contract 测试；
- 明确每个算法的误报控制参数；
- 先确认 event-only 行为稳定，再决定是否升级证据链路。

验收信号：

- 对应 `harness/tests/test_*_rule.py` 或集成测试稳定；
- Savant rule registry 能看到规则；
- 8090 support matrix 仍准确标注 `event_only`；
- 不改变性能压测使用的模型链、FPS、batch、compose 拓扑。

### Phase E2: 补 unsupported 规则模块

范围：

- `behavior.wall_climb_suspicious`

目标：

- 新增 `modules/savant_security/custom/rules/wall_climb.py`；
- 在 `modules/savant_security/custom/rules/__init__.py` 注册；
- 补 unit tests 和 runtime config export 校验；
- support matrix 从 `unsupported` 升到 `event_only`，除非证据链路也同时闭环。

注意：

- `wall_climb_suspicious` 应使用 line zone，不应复用 polygon zone 语义；
- `running` 和 `chasing` 都依赖轨迹速度，阈值应先按像素速度校准，不要直接宣称真实世界速度；
- `loitering` 应依赖持续时间和 ROI 内低速/停留状态，避免与 `intrusion` 语义重叠。

### Phase E3: 证据链路升级

范围：

- 将非 intrusion 行为从 `event_only` 升级到 `production_ready`。

目标：

- event-worker evidence policy 支持对应 event_type；
- clip-worker/media-worker 能为这些事件生成可追溯证据；
- evidence viewer 能正确显示事件类型、算法、区域、bbox/pose overlay；
- 8090 apply/restart 显示 `applied/skipped/unsupported` 与实际证据状态一致。

验收信号：

- 每个算法至少有一个端到端 evidence smoke；
- evidence bundle detail、timeline、overlay 与事件 payload 对齐；
- 旧的 intrusion/watchlist 证据闭环不回退。

### Phase E4: 人脸侧 gate 和 live search

范围：

- `face.observation`
- `face.live_search`

目标：

- 决定 `face.observation` 是否真的需要 per-camera gate；
- 实现 `face.live_search` runtime path，或继续保持 `deferred`；
- 不把 env fallback 误认为 per-camera 8090 配置生效。

## 与性能测试的并行边界

结论：只要事件检测补全工作保持在离线代码、单元测试和文档阶段，不会影响另一个 Codex 正在做的性能测试。会影响性能测试的是对共享 live runtime 的重启、apply、配置和规则启用。

### 不应干扰性能测试的工作

这些工作可以并行做：

- 新增或修改 `modules/savant_security/custom/rules/*.py`，但不重启 live Savant；
- 新增规则单元测试、API support-matrix 测试、runtime-config dry-run 测试；
- 更新文档和规格；
- 在 isolated fixture / synthetic track 数据上验证规则；
- 只运行本地 targeted pytest，不操作正在压测的容器和数据库规则。

### 会污染性能测试的工作

这些工作会改变 live runtime，压测期间应避免：

- `docker compose ... restart/up --force-recreate` 重启 `savant-security`、`analysis-forwarder`、`event-worker`、`face-worker`、`clip-worker`；
- 调用 `POST /api/v1/cameras/runtime/apply` 或 `restart`；
- 修改正在压测摄像头的 `camera_rules`，尤其是启用 `crowd_gathering`、`chasing` 这类多目标规则；
- 修改 8090 推理性能配置，例如 `ANALYSIS_FPS`、`MAX_FPS`、`POSE_INFER_INTERVAL`、`FACE_INFER_INTERVAL`、batch 参数；
- 修改 `infra/docker-compose.midterm.yml`、Savant module、模型路径、Replay/Forwarder 拓扑；
- 在压测数据源上启用更多证据 materialization，增加 Replay/clip/media IO。

### 性能风险说明

事件规则本身虽然不是 GPU 模型推理，但会消耗 CPU 和 Python pyfunc 时间。

- `intrusion` 是单轨迹规则，成本相对可控；
- `fall` 是单轨迹姿态规则，成本随 person track 数增加；
- `crowd_gathering` 是多目标聚类规则，拥挤画面成本更敏感；
- `chasing` 是多轨迹配对规则，最坏情况下接近 O(n^2)；
- 新增证据闭环会增加 Redis、PostgreSQL、Replay、clip-worker、media-worker 和磁盘 IO。

所以性能测试如果要保持可比性，必须固定算法开关和规则集合。事件检测补全代码合入后，也要在压测报告中记录 exact support matrix、enabled rules、runtime epoch 和性能配置。

## 推荐协作规则

在另一个 Codex 做性能测试期间，事件检测补全应遵守：

1. 不改性能测试正在使用的 compose、env、模型、FPS、batch、runtime performance config。
2. 不对 shared live stack 执行 runtime apply/restart。
3. 不启用新算法规则到正在压测的摄像头。
4. 代码验证先走 unit tests / dry-run export / synthetic fixtures。
5. 需要 live smoke 时，等性能测试结束，或使用单独摄像头、单独数据源、单独 runtime epoch，并在报告里标注不是性能基线。

如果必须在同一台机器上做 live smoke，应先暂停性能测试并记录：

```text
start time
runtime_epoch before
enabled camera_rules before
performance config before
containers restarted
runtime_epoch after
enabled camera_rules after
```

否则两个任务的结论会互相污染：性能测试无法证明固定拓扑性能，事件检测测试也无法证明新增规则在稳定基线下的行为。

## 当前安全起点

建议从以下最小安全切入点开始：

1. 为 `behavior.loitering` / `behavior.running` 补真实或半真实 fixture regression；
2. 为 `crowd_gathering`、`fall`、`chasing` 补 sample/fixture regression；
3. 暂缓 `behavior.wall_climb_suspicious`，直到 8090 line/墙体配置和 runtime zone 表达保真；
4. 更新 support matrix 只在规则注册和 runtime evidence 状态真实变化后进行。

这条路径不会要求重启 live runtime，也不会改变性能测试正在验证的模型链和运行参数。
