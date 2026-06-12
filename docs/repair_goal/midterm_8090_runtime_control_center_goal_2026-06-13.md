# Midterm 8090 运行控制中心实施计划

日期：2026-06-13

## 当前基线

- 分支：`c2/post-savant-poc`
- 基线提交：`722d2c7 Document midterm alarm frequency findings`
- 开始前要求：`git status --short` 必须为空
- 现状判断：当前 midterm 运行态可用，本任务必须以“不破坏现有运行能力”为最高约束

## 目标

将 `8090` evidence-viewer/operator 门户扩展为整套视频分析程序的可视化控制管理界面，覆盖运行状态、性能统计、watchdog/supervisor 状态和受控操作入口。

边界要求：

- 8090 前端只做展示和操作入口。
- API 作为唯一受控后端，负责状态聚合和白名单动作执行。
- 前端不得直接访问 Docker socket、Savant 内部端口或宿主机命令。
- Savant `/metrics`、Docker inspect、watchdog/supervisor、事件库和摄像头配置只作为后端数据源。
- 所有危险动作必须保留二次确认、可审计结果和失败提示。

## 必须保持不回退

- 当前摄像头 CRUD、启停、算法开关、录制设置、证据查看能力不得回退。
- 当前 source-only runtime convergence 不得退回到普通摄像头变更触发完整 Savant/Replay/worker 重启。
- 当前 `va_savant_*` 指标导出不得破坏。
- 当前报警、证据、录像、worker 链路不得因门户改造被重构或改变语义。
- 如需新增运行控制能力，优先复用既有 API/service，不做大范围重写。

## 分阶段计划

### 阶段 1：只读运行总览

新增后端聚合接口，例如 `GET /api/v1/runtime/overview`，先只读聚合：

- API、Savant、Redis、Postgres、event-worker、media-worker、clip-worker 状态。
- Savant metrics 中的 per-source 指标：
  - `va_savant_frames_seen_total`
  - `va_savant_frame_annotations_exported_total`
  - `va_savant_effective_fps`
  - `va_savant_last_frame_age_seconds`
  - pose/person 相关计数
  - face/AdaFace 相关计数
- 每路 source adapter 容器状态、restart count、started_at。
- watchdog/supervisor snapshot。
- 最近报警时间、最近 1/5/15 分钟报警数、最长无报警窗口。

阶段 1 验收：

- 8090 出现“运行控制中心”只读面板。
- 能看到每路摄像头 FPS、last frame age、报警频率、容器重启次数。
- 后端指标解析有单测。
- 前端缺失指标时显示降级状态，不导致页面崩溃。

### 阶段 2：接入已有受控操作

在 8090 运行控制中心显式接入既有安全操作：

- 应用摄像头源：`POST /api/v1/cameras/runtime/sources/apply`
- 受控重启运行时：`POST /api/v1/cameras/runtime/restart`
- 查看 supervisor：`GET /api/v1/cameras/runtime/supervisor`
- 触发 supervisor recover：`POST /api/v1/cameras/runtime/supervisor/recover`

阶段 2 验收：

- 所有危险按钮都有二次确认。
- 操作成功、失败、部分成功都有清晰反馈。
- 8090 页面保持在线，不因 runtime restart 刷崩。
- 不新增前端直连 Docker 的能力。

### 阶段 3：卡顿和无报警检测

在 overview 中增加状态判定：

- `last_frame_age_seconds` 超阈值标记为卡顿或断流。
- annotation age 超阈值标记为 annotation stall。
- 最近 1/5/15 分钟报警为 0 时展示无报警窗口。
- 区分“有帧但无报警”和“无帧导致无报警”。
- 展示 ZeroMQ backpressure/send timeout 的最近日志摘要或计数。

阶段 3 验收：

- 能解释摄像头报警频率不一致时，是画面内容、检测阶段、帧率、卡顿还是 backpressure 导致。
- 能看到 watchdog 是否记录过恢复或重启。

### 阶段 4：细粒度控制扩展

在只读能力稳定后，再评估新增：

- 单路 source adapter 受控重启。
- 单 worker 受控重启。
- 拉取当前运行配置快照。
- 导出运行诊断包。

阶段 4 约束：

- 每个动作必须是 API 白名单能力。
- 每个动作必须有审计记录。
- 不允许通过前端传任意容器名或任意 shell 命令。
- 默认先实现 source adapter 级别控制，worker/Savant 级控制必须更谨慎。

## 验证和提交纪律

每个修改批次必须满足：

1. 修改前确认 `git status --short`，不得混入无关改动。
2. 只做一个可验证主题，例如“后端 metrics parser”或“8090 只读面板”。
3. 修改后先执行对应验证。
4. 验证通过后立即 commit。
5. commit message 必须能看出本批次目标。
6. 不允许把未验证代码留在工作区继续叠下一批修改。

最低验证集合：

- 文档或静态改动：`git diff --check`
- 后端逻辑：对应 targeted `pytest`
- 前端静态逻辑：对应静态/单元测试，至少检查按钮、文案、API route 绑定
- compose/env 相关：`docker compose -f infra/docker-compose.midterm.yml config`
- Savant metrics 相关：`scripts/smoke/current/check_savant_perf_observability.sh`
- 运行态可用时：补充 doctor/deployment smoke，并只读检查最近 Savant 日志无 fatal traceback

如果运行态不可用，必须在 completion 文档里说明未跑哪些 runtime 验证和原因，不能写成已验证。

## 建议 commit 拆分

1. `Document 8090 runtime control center goal`
2. `Add runtime overview metrics parser`
3. `Add runtime overview API`
4. `Add 8090 runtime control center panel`
5. `Wire supervisor status and recover controls`
6. `Add alarm cadence and stall indicators`
7. `Document 8090 runtime control center completion`

## 可复制 goal 命令

```text
goal: 实现 8090 作为整套视频分析程序的可视化控制管理门户。当前基线为 c2/post-savant-poc 分支 722d2c7，当前运行态可用，必须保持现有摄像头接入、报警、证据、录像、source-only convergence、Savant va_savant_* 指标不回退。请按 docs/repair_goal/midterm_8090_runtime_control_center_goal_2026-06-13.md 执行：先实现只读 runtime overview 聚合和 8090 运行控制中心展示，再接入已有受控操作，包括应用摄像头源、受控重启运行时、supervisor 状态和 recover，最后再评估单路 source adapter/worker 控制。8090 前端不得直接控制 Docker 或执行宿主机命令，所有控制动作必须通过 API 白名单和二次确认。每个修改批次都必须先运行对应验证，验证通过后立即 commit；不要把未验证改动叠加到下一批；不要混入无关重构；如果 runtime 验证不可用，必须在文档里明确说明未验证项和原因。完成后把实现、验证结果、剩余风险写入 docs/repair_goal/ 下新的中文 completion 文档，并提交。
```
