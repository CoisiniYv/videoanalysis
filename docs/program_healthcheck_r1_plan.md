# Program Healthcheck R1 Plan

用途：这是一次全程序体检的执行计划，不是体检结果。执行本计划时只做审计、状态还原、风险归类和后续路线建议，最终报告写入 `docs/program_healthcheck_r1.md`。

## Goal Command Objective

可直接用于 goal 的目标文本：

```text
执行 docs/program_healthcheck_r1_plan.md 中的 Program Healthcheck R1。对 video-analytics / Savant 当前仓库做一次只审计、不重构、不修复的全程序体检，生成 docs/program_healthcheck_r1.md。除该报告和必要的只读辅助记录外，不修改业务代码、compose、module.yml、worker、API 或运行时配置。报告必须用证据区分 READY / PARTIAL / SPEC_ONLY / BROKEN / UNKNOWN，并给出 Top 20 风险与下一步开发顺序。
```

## Non-Negotiable Guardrails

- 只做全面审计、状态梳理、风险归类和后续计划建议。
- 不直接重构代码。
- 不新增业务功能。
- 不大规模修改 compose、`module.yml`、worker、API 或运行时配置。
- 不因为发现 bug 而顺手修核心链路。
- 默认只新增或更新 `docs/program_healthcheck_r1.md`。
- 如需生成辅助脚本，必须非常小、只读、可解释，并在报告中说明；优先不用脚本。
- 运行时操作必须偏只读：允许 `docker ps`、`docker compose ps`、`curl /health`、数据库只读查询；不要 `up/down/restart/rebuild/apply`，除非用户另行明确授权。
- 发现明显 bug 只记录：问题位置、影响范围、复现方式、建议修复阶段、是否阻断当前主线。
- 不要声称系统 READY，除非有实际代码、配置、测试或 smoke 证据。

## Starting Assumptions To Verify

这些是待验证假设，不是结论：

- 当前主线可能是 `infra/docker-compose.midterm.yml`、`infra/env/midterm.env`、`services/evidence-viewer`、`services/api`、`modules/savant_security`。
- 8090 可能是当前统一管理入口，并代理内部 API。
- 当前链路可能是 RTSP -> Replay -> analysis-forwarder/resampler -> Savant -> Redis -> workers -> PostgreSQL/evidence -> 8090。
- intrusion 和 watchlist 可能已有局部闭环；loitering、crowd、fall、running、chasing、wall_climb、live search 等需要逐项确认，不能凭文件名判定。
- 当前机器和生产目标不同：开发机 2x4090，目标双 T4；没有真实 60 路 RTSP 时不能做最终生产性能结论。

## Required Questions To Answer In The Report

最终 `docs/program_healthcheck_r1.md` 必须直接回答这些问题：

1. 当前程序到底由哪些服务组成。
2. 8090 端口对应哪个服务，以及它真实支持哪些管理能力。
3. 摄像头从添加到启动，再到进入 Replay、resampler、Savant 的真实路径。
4. 当前 Savant 推理链路真实启用了哪些模型和 PyFunc。
5. Redis Streams 现在真实使用了哪些 stream。
6. PostgreSQL 现在真实有哪些表，哪些表被实际写入。
7. `event-worker`、`face-worker`、`clip-worker`、`media-worker` 当前各自做到什么程度。
8. evidence 当前到底如何生成。
9. 哪些算法已经可用，哪些只是设计文档，哪些是半成品。
10. 当前测试、smoke、harness 能证明什么，不能证明什么。
11. 当前最大风险和技术债是什么。
12. 下一步最合理的开发顺序是什么。

## Execution Order

### 0. Baseline Discipline

1. 记录当前时间、分支、commit、dirty worktree。
2. 明确哪些文件是用户未提交改动，审计过程中不要清理或覆盖。
3. 所有实际运行命令都记录到最终报告 `## 18. Appendix: Commands Used`。
4. 先静态扫描，再做只读运行时验证；无法验证的项目标 `UNKNOWN`。

Baseline commands:

```bash
git status --short
git branch --show-current
git log --oneline -n 20
find . -maxdepth 3 -type f | sort | sed -n '1,240p'
```

### 1. Mainline And Compose Topology

目标：还原当前推荐启动面、服务网络、端口、volume、GPU 分配和历史 POC 边界。

Commands:

```bash
find . -iname '*compose*.yml' -o -iname '*compose*.yaml'
find . -maxdepth 4 -type f | grep -E 'Dockerfile|compose|env|Makefile|justfile'
```

必须判断：

- 当前推荐启动哪个 compose。
- 哪些 compose 是历史 POC、phase-only 或 legacy。
- 8090 暴露自哪个服务。
- Replay、resampler/analysis-forwarder、Savant shard、Redis、PostgreSQL、API、workers 是否在同一主线。
- volume 是否与 `/data/video-analytics` 对齐。
- GPU device 分配是否明确。

Report table:

| 服务 | 端口 | 镜像/构建路径 | 依赖 | 作用 | 当前状态判断 |
| --- | ---: | --- | --- | --- | --- |

### 2. 8090 Management Entry Audit

目标：确认 8090 是什么服务、页面和 API 是否一致、哪些操作真实生效、哪些只是壳。

Commands:

```bash
grep -R "8090" -n . --exclude-dir=.git --exclude-dir=__pycache__ || true
```

必须回答：

- 8090 是 FastAPI、Flask、Node、静态前端还是其他。
- 它暴露哪些页面和接口。
- 它是否真的能启动、停止程序；如果能，是操作 compose、容器、进程，还是只写配置。
- 摄像头管理是否落库，启停是否影响 Replay、adapter、Savant。
- 人脸管理是否接入 `person_gallery_embeddings`。
- evidence 管理是列表展示、详情查看、文件代理，还是能触发生成证据包。
- UI 调用的 API 与后端路由是否匹配。
- 是否存在危险 shell 执行、缺少校验、误删、误停等风险。

Report table:

| 页面/接口 | 当前能力 | 背后 API/函数 | 是否真实生效 | 问题 |
| --- | --- | --- | --- | --- |

### 3. API Inventory

目标：列出实际 API 路由，判断是否覆盖当前系统目标，以及 8090 是否真实调用。

Commands:

```bash
grep -R "@.*route\\|@router\\|FastAPI\\|APIRouter" -n services . | head -300
```

覆盖：

- `/health`、`/ready`
- cameras、zones、ROI
- rules、algorithms
- events、alerts
- persons、face upload、gallery
- watchlist、live-search
- evidence、media
- system start/stop/status

Report table:

| API | 方法 | 文件 | 当前实现 | 数据表/外部服务 | 缺口 |
| --- | --- | --- | --- | --- | --- |

### 4. Database Schema And Write Paths

目标：还原 PostgreSQL schema、实际写入者、读取者和幂等约束。

Commands:

```bash
find db services -type f | grep -E 'migration|schema|sql|models|database|dao|repository'
grep -R "CREATE TABLE\\|class .*Base\\|__tablename__\\|face_observations\\|events\\|cameras\\|person_gallery" -n db services . | head -500
```

必须覆盖：

- `cameras`
- `camera_zones`
- `camera_rules`
- `events`
- `record_requests`
- `media_ready`
- `persons`
- `person_gallery_embeddings`
- `face_observations`
- `match_results`
- `watchlist_rules`
- `live_search_jobs`
- `audit_logs`

重点判断：

- `events.source_event_id` 是否唯一且幂等。
- `face_observations.source_observation_id` 是否唯一且幂等。
- embedding 是否 `vector(512)`。
- `watchlist_hit` 是否作为 event 写入。
- evidence 的 `snapshot_path`、`clip_path`、`annotation_path`、`payload.media` 是否完整。
- 8090 管理操作是否真的写这些表。

Report table:

| 表 | 是否存在 | 谁写入 | 谁读取 | 是否主线需要 | 问题 |
| --- | --- | --- | --- | --- | --- |

### 5. Redis Streams And Message Contracts

目标：还原所有 Redis stream、生产者、消费者、schema、consumer group、ACK、retry 和幂等键。

Commands:

```bash
grep -R "security\\.events\\|security\\.face_observations\\|security\\.alerts\\|security\\.record_requests\\|security\\.media_ready\\|XADD\\|xadd\\|XREAD\\|xread" -n . --exclude-dir=.git | head -500
```

必须判断：

- 实际使用了哪些 stream。
- 每个 stream 的生产者和消费者。
- 消息 schema 是否稳定。
- 是否有 consumer group、ACK、pending/retry 策略。
- 是否有幂等键。
- 是否误传 image bytes、crop bytes、base64。
- 是否存在 `frame_annotations` 或 `identity_patches`；如果没有，只记录未来建议。

Report table:

| Stream | 生产者 | 消费者 | 消息内容 | 幂等键 | 风险 |
| --- | --- | --- | --- | --- | --- |

### 6. Savant / Replay / Resampler Flow

目标：还原视频从摄像头到证据的真实主线，排除 phase/POC 干扰。

Commands:

```bash
find modules infra services scripts -type f | grep -E 'module.yml|savant|replay|resampler|adapter|source|sink'
grep -R "Replay\\|replay\\|resampler\\|frame_uuid\\|keyframe_uuid\\|source_id\\|module.yml\\|nvinfer\\|pyfunc" -n modules services infra scripts | head -600
```

必须判断：

- RTSP source adapter 是否存在。
- Replay service 是否在主线 compose 中。
- resampler 是哪个服务、脚本或 adapter；降帧策略在哪里配置。
- Savant 当前主线 module 是哪个，是否还有多个 phase module 并存。
- 当前启用的 pose/person/keypoints、tracker、人脸检测、AdaFace、PyFunc。
- `face-worker` 是否没有跑 GPU embedding。
- `frame_uuid`、`keyframe_uuid`、`anchor_keyframe_uuid` 是否贯通。
- Replay job 是否按 `anchor_keyframe_uuid` 创建。
- `post_window_frame`、`start_window_frame` 是否只是 proof，不作为 Replay 锚点。

Report table:

| 链路节点 | 当前实现位置 | 输入 | 输出 | 是否主线 | 问题 |
| --- | --- | --- | --- | --- | --- |

### 7. Worker Audit

目标：逐个 worker 还原输入、输出、数据库写入、Redis 写入、完成度和风险。

覆盖：

- event-worker
- face-worker
- clip-worker
- media-worker
- evidence-viewer 或 8090 内置 evidence 管理服务

Report table:

| Worker | 输入 | 输出 | 数据库写入 | Redis 写入 | 当前完成度 | 风险 |
| --- | --- | --- | --- | --- | --- | --- |

Worker-specific checks:

- event-worker：消费 `security.events`、写 `events/alerts`、创建 `record_request`、保留 media 状态、幂等。
- face-worker：消费 `security.face_observations`、写 `face_observations`、pgvector gallery match、生成 `watchlist_hit` / `live_search_hit`、cooldown、不跑 GPU embedding。
- clip-worker：消费 `record_requests`、调用 Replay、使用正确 `anchor_keyframe_uuid`、处理 pre/post window、回写状态。
- media-worker：整理 `raw_clip`、生成 snapshot、`annotations.jsonl`、`summary.json`、viewer/index，检查 timestamp、frame_num、known_face、bbox 对齐风险。

### 8. Algorithm Capability Matrix

目标：按代码、配置、测试、文档和 smoke 证据给出能力状态。

状态只能使用：

- `READY`：功能闭环可用，有测试或 smoke 支撑。
- `PARTIAL`：部分可用，但缺关键环节。
- `SPEC_ONLY`：只有文档或设计。
- `BROKEN`：有实现但当前明显不可用。
- `UNKNOWN`：没有足够证据。

覆盖：

- intrusion
- loitering
- crowd_gathering
- running
- chasing
- fall
- wall_climb_suspicious / line crossing
- face_observation
- gallery_match
- watchlist_hit
- live_search_hit
- registered person history / 一键找人
- evidence annotation / known_face overlay

Report table:

| 能力 | 设计文档 | 代码实现 | 测试 | 端到端验证 | UI入口 | 当前状态 | 下一步 |
| --- | --- | --- | --- | --- | --- | --- | --- |

### 9. Evidence Pipeline Audit

目标：判断 evidence 是否能作为调试证据、是否能作为生产证据，以及缺失契约。

检查：

- `raw_clip` 来源。
- snapshot 来源。
- `annotations.jsonl` 来源。
- `summary.json` 来源。
- `metadata.json` 来源。
- viewer 页面来源。
- bbox 坐标格式。
- `frame_num` 是全局还是 clip-local。
- `frame_pts`、`timestamp_ms`、`created_at` 是否混用。
- `known_face=0` 的可能原因。
- `watchlist_hit` trigger face 是否能回到 annotation。
- event payload 是否保留 `person_id`、`source_observation_id`、face bbox、similarity。

Report table:

| 证据产物 | 当前是否生成 | 来源 | 对齐锚点 | 风险 |
| --- | --- | --- | --- | --- |

结论必须包含：

- 当前 evidence 是否能作为调试证据。
- 当前 evidence 是否能作为生产证据。
- 如果不能，缺什么。
- 是否建议采用 raw_clip + annotation JSON + viewer dynamic overlay 作为主线。
- 是否建议短 TTL frame-indexed metadata cache；只建议，不实现。

### 10. UI / Operator Experience Audit

目标：从代码层面审计 8090 页面质量，不做美化。

检查：

- 当前页面模块。
- 按钮是否真实接 API。
- 摄像头添加、人脸注册、evidence 查看流程是否清晰。
- 算法开关、ROI 绘制是否存在。
- 错误提示是否清楚。
- 危险误操作。
- 是否适合作为当前调试入口。
- 是否适合作为生产管理入口。

UI severity:

- `P0`：会破坏数据或运行状态。
- `P1`：核心流程不可用。
- `P2`：操作混乱但可绕过。
- `P3`：美观和体验问题。

Report table:

| 问题 | 严重程度 | 影响 | 建议阶段 |
| --- | --- | --- | --- |

### 11. Tests / Harness / Smoke Coverage

目标：判断现有测试能证明什么、不能证明什么，区分单测、集成、GPU smoke、历史验证。

Commands:

```bash
find harness scripts -type f | sort
grep -R "PASS_\\|PARTIAL_\\|pytest\\|smoke\\|watchlist\\|intrusion\\|evidence\\|replay" -n docs harness scripts services modules | head -600
```

必须判断：

- 当前是否有一键全量健康检查。
- 当前是否有 8090/API healthcheck。
- 当前是否有 evidence bundle 验证。
- 当前是否有算法 golden events。
- 哪些脚本依赖 GPU、真实 RTSP、运行中 compose、历史 artifact。

Report table:

| 测试/脚本 | 覆盖能力 | 是否需要 GPU | 是否需要真实 RTSP | 当前可信度 | 问题 |
| --- | --- | --- | --- | --- | --- |

### 12. Performance And Deployment Readiness

目标：只做结构性评估，不做 60 路生产结论。

检查：

- batch size 配置在哪里。
- pose、face detector、embedding batch 是否分开。
- max FPS / resampler 配置在哪里。
- 2x4090 结果能说明什么。
- 哪些必须等双 T4 + 真实 RTSP 才能确认。
- 没有 60 路 RTSP 时，是否可用 replay/file source 模拟多路。
- Redis lag、PostgreSQL latency、event latency、GPU metrics 是否采集。
- Prometheus / Grafana 是否接入。

Report table:

| 性能问题 | 当前可测 | 当前不可测 | 需要生产机验证 | 建议 |
| --- | --- | --- | --- | --- |

### 13. Risk Ranking

目标：输出 Top 20 风险，按优先级排序。

每个风险包含：

- 风险名称。
- 严重程度 `P0/P1/P2/P3`。
- 影响范围。
- 证据。
- 建议处理阶段。
- 是否阻断继续开发算法。
- 是否阻断演示。
- 是否阻断生产。

特别关注：

- 主线 compose 不清晰。
- 8090 与实际服务状态不一致。
- 摄像头启停不可靠。
- replay/resampler/savant 链路不清晰。
- 多 phase module 污染。
- evidence 对齐不可靠。
- `known_face=0`。
- bbox 坐标混乱。
- timestamp 时间域混乱。
- `watchlist_hit` 与 `face_observation` 语义混乱。
- 算法只有 intrusion/watchlist，其他未完成。
- UI 丑是否阻断当前阶段。
- 4090 性能误判 T4。
- 没有真实多路 RTSP 测试资源。

## Required Report File

生成：

```text
docs/program_healthcheck_r1.md
```

必须包含以下结构：

```markdown
# Program Healthcheck R1

## 1. Executive Summary
一句话说明当前系统真实状态。

## 2. Current Mainline Topology
用文本图画出当前真实链路。

## 3. Service Inventory
服务表。

## 4. 8090 Management Entry Audit
8090 管理入口审计。

## 5. API Inventory
API 表。

## 6. Database Inventory
数据库表和写入路径。

## 7. Redis Streams Inventory
Redis 消息流。

## 8. Savant / Replay / Resampler Flow
视频推理链路。

## 9. Worker Responsibilities and Actual Status
worker 状态。

## 10. Algorithm Capability Matrix
算法矩阵。

## 11. Evidence Pipeline Audit
证据链路状态。

## 12. UI / Operator Experience Audit
8090 页面和操作体验。

## 13. Tests / Harness / Smoke Coverage
测试覆盖。

## 14. Performance and Deployment Readiness
性能与部署风险。

## 15. Top Risks
Top 20 风险。

## 16. Recommended Next Development Order
下一步开发顺序。

## 17. What Not To Do Next
明确接下来不要做什么。

## 18. Appendix: Commands Used
记录实际运行过的命令。
```

## Recommended Next Development Order Template

最终报告中使用此格式，只建议，不实现：

```markdown
## Recommended Next Development Order

### R1.1 — Mainline Compose / Service Naming Freeze
目标：
验收：

### R1.2 — 8090 Management Entry Stabilization
目标：
验收：

### R1.3 — Camera Lifecycle End-to-End
目标：
验收：

### R1.4 — Evidence Contract Stabilization
目标：
验收：

### R1.5 — Missing Behavior Algorithms MVP
目标：
验收：

### R1.6 — Face / Watchlist / Live Search Completion
目标：
验收：

### R1.7 — UI Operator Workflow Improvement
目标：
验收：

### R1.8 — Performance Harness Without Production RTSP
目标：
验收：

### R1.9 — Final Dual T4 Validation Plan
目标：
验收：
```

## What Not To Do Next Template

最终报告中必须包含：

```markdown
## What Not To Do Next

- 不要现在直接做 60 路性能结论。
- 不要现在大改 batch size 作为最终结论。
- 不要继续开很多 phase module。
- 不要只美化 UI 而不修摄像头/证据/算法闭环。
- 不要把 8090 页面按钮当成真实能力，必须检查背后 API 和 worker。
- 不要把 face_observation 当成 watchlist_hit。
- 不要把 raw_clip 调试产物当成生产 evidence。
```

## Final Reply Requirements After Execution

执行完整体检后，回复必须给出：

1. 生成的报告路径。
2. 是否修改了除报告之外的文件。
3. 当前 worktree 状态摘要。
4. 当前系统一句话状态。
5. Top 5 阻断问题。
6. 推荐下一步第一件事。

## Acceptance Checklist

- `docs/program_healthcheck_r1.md` 已生成。
- 报告明确区分已提交代码、未提交改动、未跟踪文件。
- 报告没有把历史 debug、phase-only、POC 文件误认为主线。
- 每个 READY/PARTIAL/SPEC_ONLY/BROKEN/UNKNOWN 判断都有证据来源或说明缺证据。
- 8090 页面能力和背后 API/worker 状态已对齐检查。
- camera -> Replay -> resampler/analysis-forwarder -> Savant -> Redis -> workers -> DB/evidence -> 8090 路径已还原。
- Redis streams、PostgreSQL tables、worker 输入输出已表格化。
- evidence 是否调试可用、生产可用已有明确结论。
- 测试和 smoke 的证明边界已写清楚。
- 性能部分没有给出双 T4 / 60 路最终结论。
- Top 20 风险已排序，并标注阻断算法、演示、生产的影响。
- 下一步开发顺序已给出，且没有实施任何建议项。
