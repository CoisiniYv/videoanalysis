# Midterm 8090 算法控制与运行时应用现状

更新时间：2026-06-25

状态：P0-P1 已完成。本记录用于固化 8090 操作台算法配置能力的当前边界，
避免把“已保存到数据库”误认为“运行时已经生效”。

## 当前结论

`8090` 是当前 midterm 的客户侧操作入口。实际服务是
`services/evidence-viewer`，它对外暴露页面和证据接口，并把同源
`/api/v1/*` 代理到内部 `api:8000`。

本轮已经完成第一阶段：

- 8090 可以读取统一的算法支持矩阵；
- 8090 页面可以显示算法支持状态、状态原因和最新运行时应用状态；
- 运行时 apply/restart 返回本次实际涉及的 camera、source、rule、跳过规则、
  未支持规则和 `runtime_epoch`；
- 8090 可以查看选中摄像头的 dry-run 生成运行时配置；
- 未改变 `face-worker` 的 watchlist 匹配语义；
- 未把 `face.watchlist` 改成 DB/8090 驱动；
- `behavior.intrusion` 仍是当前生产可用的行为告警和证据基线。

## 2026-06-25 规则区域兼容修复

当前 8090 连接的是 `phase0-postgres`。这个库里 `camera_zones.zone_id` 已经是
TEXT，但 `camera_rules.zone_id` 仍是早期 UUID 列，所以把 `lab_full_frame` 这类
区域 ID 写进算法规则时会报：

```text
invalid input syntax for type uuid: "lab_full_frame"
```

已执行 `db/migrations/016_camera_rule_zone_id_text_compat.sql`，把
`camera_rules.zone_id` 迁成 TEXT，并保留对旧 UUID 规则的文本回填。现在
`/api/v1/cameras/{lab_camera_id}/algorithm-rules` 可以正常保存带
`zone_id=lab_full_frame` 的规则。

## 入口与边界

客户入口：

```text
http://127.0.0.1:8090/
http://127.0.0.1:8090/operator
```

调用关系：

```text
browser
  -> evidence-viewer:8090
      -> /static/* 操作台前端
      -> /api/bundles/* 证据文件接口
      -> /api/v1/* 代理到内部 api:8000
      -> /media/* 代理到内部 api:8000/media/*
```

内部 `api:8000` 不应作为客户入口暴露；8090 页面才是后续启停、管理摄像头、
配置算法、人脸注册、证据维护和受控运行时操作的统一入口。

## lab 摄像头 watchlist 现状

2026-06-25 的运行时审计结论是：`lab` 当前没有配置 per-camera
`face.watchlist` 规则。

已观察到的状态：

- `lab` 摄像头启用，并映射到自己的 `source_id`；
- `/api/v1/cameras/{lab_camera_id}/config` 只返回 `lab_intrusion_rule`；
- `camera_rules` 中只有 primary 和 lab 的 intrusion 规则；
- 生成的 `cameras.midterm.yml` 对 lab 只导出 `behavior.intrusion`；
- lab 有 face observations，说明人脸检测/观察路径不是完全缺失；
- lab 没有由 per-camera `face.watchlist` 规则证明出来的 watchlist 配置。

如果运行数据里看到 `watchlist_hit`，不能据此反推 8090 已经为某个摄像头启用了
watchlist。当前 watchlist 匹配仍由 `face-worker` 的 compose/env 控制，例如：

```text
WATCHLIST_MATCH_ENABLED=true
WATCHLIST_THRESHOLD=0.60
WATCHLIST_TARGET_EXTERNAL_PERSON_IDS=demo:midterm:reese,demo:midterm:finch
WATCHLIST_TARGET_NAMES=Reese,Finch
```

因此，`face.watchlist` 当前是“可配置/可导出，但不是 per-camera 运行时 gate”。
真正让 watchlist 从 8090/DB 驱动，需要后续 P3-P4 单独实现。

## 算法支持矩阵

新增的 API：

```text
GET /api/v1/algorithms/support-matrix
```

矩阵状态枚举：

| 状态 | 含义 |
| --- | --- |
| `production_ready` | 8090 配置会控制真实运行时检测、事件和证据链路 |
| `event_only` | 运行时可以检测/出事件，但默认证据链路不等同于生产 ready |
| `config_only` | 规则能保存/导出，但当前运行时不消费它作为 gate |
| `unsupported` | 当前算法未实现或未注册，不能作为有效运行时开关 |
| `deferred` | 产品契约存在，但实现明确延期 |

当前矩阵：

| 算法 | 状态 | 当前解释 |
| --- | --- | --- |
| `behavior.intrusion` | `production_ready` | 当前生产基线，Savant 消费规则，事件和证据链路可闭环 |
| `behavior.crowd_gathering` | `event_only` | 规则已注册，可出事件，但默认证据链路不是生产 ready |
| `behavior.fall` | `event_only` | 规则已注册，可出事件，但默认证据链路不是生产 ready |
| `behavior.chasing` | `event_only` | 规则已注册，可出事件，但默认证据链路不是生产 ready |
| `behavior.loitering` | `unsupported` | rule module 未注册，运行时会跳过 |
| `behavior.running` | `unsupported` | rule module 未注册，运行时会跳过 |
| `behavior.wall_climb_suspicious` | `unsupported` | rule module 未注册，运行时会跳过 |
| `face.observation` | `config_only` | 人脸观察由 pipeline/env 控制，未使用 per-camera rule gate |
| `face.watchlist` | `config_only` | watchlist 由 face-worker env 控制，未使用 per-camera camera_rules |
| `face.live_search` | `deferred` | `live_search_hit` 仍是契约/延期状态 |

## 8090 页面变化

8090 的算法卡片现在以 support matrix 为准展示：

- 支持状态 badge；
- 状态原因；
- 最新运行时 apply 状态；
- 跳过或未支持原因；
- 选中摄像头的生成运行时配置预览。

默认情况下，`unsupported` 和 `deferred` 算法不能保存或应用。需要显式调试时，
可以通过：

```text
?algorithm_debug=1
```

解除 UI 阻断，用于开发验证。这个调试开关不代表算法已经生产可用。

## 运行时 apply/restart 返回

运行时入口：

```text
POST /api/v1/cameras/runtime/apply
POST /api/v1/cameras/runtime/restart
```

返回体现在包含：

- `runtime_epoch_id`
- `runtime_epoch`
- `camera_ids`
- `source_ids`
- `applied_cameras`
- `configured_rules`
- `enabled_rules`
- `applied_rules`
- `skipped_rules`
- `unsupported_rules`
- `management_containers_preserved`

每条 rule 会带：

- `camera_id`
- `source_id`
- `rule_id`
- `algorithm_id`
- `support_status`
- `support_status_reason`
- `runtime_apply_state`
- `runtime_consumed`
- `runtime_skip_reason`

这让 8090 能区分：

```text
规则已保存
规则已进入生成配置
规则被运行时消费
规则因 config_only / unsupported / disabled 等原因被跳过
```

## 选中摄像头生成配置

新增的预览入口：

```text
GET /api/v1/cameras/{camera_id}/runtime-config
```

该接口使用 dry-run export，不写入最终运行时文件。返回内容包括：

- 选中摄像头的 `cameras.midterm.yml` 片段；
- 选中摄像头的 algorithm runtime config；
- export summary；
- apply plan。

用途是让 8090 页面可以直接展示“这台摄像头当前会生成什么运行时配置”，而不是只
显示数据库表单值。

## 本轮没有改变的内容

本轮只做 P0-P1：状态可见性和运行时应用反馈。

明确没有做：

- 没有把 `face.watchlist` 改成 DB/8090 驱动；
- 没有改变 `face-worker` 的 env-driven watchlist 匹配；
- 没有实现 watchlist target membership 的 8090 管理；
- 没有把 `face.live_search` 做成真实运行时能力；
- 没有把所有行为算法都升级成 production-ready；
- 没有把 API 侧 `/operator/static` 旧页面作为 8090 主入口。

8090 主入口仍是 `services/evidence-viewer/app/static/operator.js`。API 服务下的
`services/api/app/static/operator/*` 是内部 API 自带的旧静态页副本，不是
midterm compose 对外发布的 8090 页面。

## 验证记录

本轮已通过：

```text
pytest -q \
  harness/tests/test_algorithm_support_matrix.py \
  harness/tests/test_api_runtime_config_export.py \
  harness/tests/test_camera_runtime_apply_service.py \
  harness/tests/test_operator_face_registration_static.py \
  harness/tests/test_midterm_deployment_contract.py
```

结果：

```text
55 passed
```

静态语法检查：

```text
node --check services/evidence-viewer/app/static/operator.js
node --check services/api/app/static/operator/app.js
```

Compose 配置检查：

```text
docker compose -f infra/docker-compose.midterm.yml config
```

Diff 检查：

```text
git diff --check
```

以上均通过。

未跑 runtime smoke。原因是本轮目标是 P0-P1 的配置可见性和返回结构闭环，
不需要重启或扰动当前 midterm 运行栈；真实 runtime smoke 会涉及 8090 受控
apply/restart 和推理/录像链路重启，应在单独运行窗口执行。

## 关键文件

- `specs/23_midterm_8090_operator_algorithm_control_plane.md`
- `services/api/app/algorithm_registry.py`
- `services/api/app/schemas/algorithms.py`
- `services/api/app/routers/algorithms.py`
- `services/api/app/routers/cameras.py`
- `services/api/app/runtime_config_export.py`
- `services/api/app/services/runtime_apply.py`
- `services/evidence-viewer/app/static/index.html`
- `services/evidence-viewer/app/static/operator.js`
- `services/evidence-viewer/app/static/style.css`
- `harness/tests/test_algorithm_support_matrix.py`
- `harness/tests/test_api_runtime_config_export.py`
- `harness/tests/test_camera_runtime_apply_service.py`
- `harness/tests/test_operator_face_registration_static.py`

## 后续阶段建议

下一阶段不要和 P0-P1 混在一起。建议单独开 P3-P4 goal：

```text
实现 DB/runtime-config 驱动的 face.watchlist 控制，让 8090 可以按摄像头启停
watchlist，维护目标人员集合，并证明 lab 摄像头启用/停用 watchlist 会改变
watchlist_hit 事件和证据产出。
```

验收时至少要证明：

- 8090 为 lab 启用 `face.watchlist` 后，face-worker 使用 DB/runtime config
  作为规则来源；
- watchlist hit 事件能追溯到 8090 配置的 `rule_id`、threshold、target set；
- 停用 lab 的 watchlist 规则后，不再产生新的 lab watchlist hit；
- 其他摄像头不受 lab 开关影响。
