# 34_midterm_clip_media_worker_structural_remediation_plan.md

Date: 2026-07-12

Status: 已完成设计，尚未执行

## 0. 执行摘要

本计划修补当前 `clip-worker` 和 `media-worker` 的结构性问题，但不推翻已经验证的
Replay、rolling-cache、证据状态、帧身份和 8090 数据合同。

当前问题不是两个 worker 的业务方向错误，而是实现边界已经失控：

- `services/clip-worker/app/worker.py` 为 4,275 行，`run_worker()` 单函数约
  1,138 行；
- `services/media-worker/app/worker.py` 为 8,589 行，
  `_process_sink_output()` 单函数约 1,104 行；
- Redis 消费、证明查找、Replay 调度、数据库状态迁移、媒体探测、ffmpeg、证据
  发布、索引和清理分散在少数超长控制流中；
- `clip-worker` 和 `media-worker` 共同修改同一证据生命周期，但所有权边界主要
  隐含在分支和状态字符串中；
- 当前测试对具体函数覆盖较多，但缺少能够证明“抽取前后外部行为完全相同”的
  协调器级 characterization tests。

修补采用渐进式 strangler migration，不做一次性重写：

1. 冻结输入、状态迁移、DB 写入、Redis ACK 和外部调用结果；
2. 先统一两个 worker 的生命周期所有权和状态转换入口；
3. 把 `clip-worker` 拆成 request consumer、proof resolver、planner、admission、
   Replay client 和短协调器；
4. 按 Spec 33 执行 `media-worker` 的租约、WIP、三 lane scheduler、finalizer、
   connection pool 和 segment index；
5. 再把 sink discovery、artifact publish、DB index、cleanup 从巨型函数中抽出；
6. 通过固定输入差分、故障注入、重启恢复和两轮同口径压力验收后删除旧分支。

第一阶段仍保持两个现有容器和现有 Redis/PostgreSQL/Replay 拓扑。模块化不等于
立即增加微服务；只有单进程有界调度通过后，运行数据证明存在独立扩缩容需求，
才另立方案评估进程或服务拆分。

## 1. Decision And Authority

### 1.1 决策

执行一次行为保持、状态合同优先的内部重构：

```text
clip-worker
  Redis request delivery
    -> validate/dedupe
    -> frame proof
    -> pure Replay plan
    -> atomic admission
    -> Replay API create
    -> durable state + ACK

media-worker
  short scheduler tick
    -> claim/recover
    -> remux/image lane
    -> durable handoff
    -> finalizer lane
    -> atomic evidence publish/index/terminal state
```

每个箭头必须有显式输入、输出、错误分类、幂等键和状态迁移。协调器负责顺序，不
直接实现 SQL、Redis 命令、HTTP payload 细节、ffmpeg 或文件发布。

### 1.2 与已有规格的关系

本计划是两个 worker 的结构治理总计划，不替代以下权威规格：

- Spec 17 继续定义 proof wait、Replay queue、证据状态可见性和帧对齐合同；
- Spec 29 继续定义 Replay 延迟分段和性能指标；
- Spec 31 继续定义 rolling-cache、Replay fallback 和证据架构产品边界；
- Spec 33 是 `media-worker` 调度、lease、WIP、finalizer handoff、连接池和
  segment index 的实现权威。

出现冲突时采用以下优先级：

```text
当前数据库迁移和代码实际合同
  > Spec 33 的 scheduler/state/lease 细则
  > Spec 17 的 Clip/Replay/overlay 合同
  > 本计划的模块边界和执行顺序
```

本计划新增的核心内容是：

1. `clip-worker` 的结构拆分方案；
2. 两个 worker 之间的阶段所有权和交接矩阵；
3. 行为保持式迁移、差分验证和旧代码退场门槛；
4. 防止重构后再次长回巨型 worker 的结构门禁。

## 2. Current Source Baseline

本节只记录 2026-07-12 当前代码事实，不把历史文档结论当作完成证据。

### 2.1 Clip Worker

当前生产模块规模：

| File | Lines |
| --- | ---: |
| `app/worker.py` | 4,275 |
| `app/repository.py` | 1,463 |
| `app/replay_client.py` | 502 |
| `app/config.py` | 330 |
| `app/replay_shards.py` | 214 |

`run_worker()` 同时承担：

- Redis consumer group 创建、读取、pending reclaim 和 ACK；
- 请求 JSON 解析、重复/终态判断；
- frame annotation 查询、缓存和 proof polling；
- keyframe、PTS、session、epoch 和截断窗口推导；
- quota、priority、cooldown 和 Replay shard 路由；
- PostgreSQL Replay admission slot 获取与释放；
- Replay API job 创建；
- evidence/event 状态、诊断和错误原因更新；
- shutdown、run-once 和周期性 deadline expiry。

当前可靠性机制必须保留：

- `source_event_id` / `event_id` 幂等；
- Redis pending/reclaim 恢复；
- proof wait 不占 Replay slot；
- PostgreSQL advisory lock + row lock 的原子 admission；
- global/shard/source 三层并发上限；
- completion-aware Replay slot；
- `runtime_epoch_id`、`stream_session_id` 和 `frame_uuid` 隔离；
- Replay 创建失败释放 slot，成功后由 sink/finalizer 完成路径收口。

### 2.2 Media Worker

当前生产模块规模：

| File | Lines |
| --- | ---: |
| `app/worker.py` | 8,589 |
| `app/rolling_cache.py` | 663 |
| `app/evidence_db_index.py` | 624 |
| `app/config.py` | 239 |

`worker.py` 当前同时承担：

- replay sink 目录扫描、稳定性判断和 processed-state 文件；
- rolling-cache task 查询、coverage 判断和 remux；
- image evidence、snapshot 和 annotation 补全；
- ffprobe/ffmpeg 调用、duration/window guard；
- bundle、timeline、overlay 和 sidecar 生成；
- PostgreSQL evidence bundle/artifact/index upsert；
- evidence task、event payload 和 Replay slot 状态收口；
- 文件原子替换、prune、storage guard 和失败 quarantine；
- executor、并发 guard、poll、shutdown 和恢复。

当前可靠性机制必须保留：

- raw clip 与 DB 元数据分层；
- `(event_id, clip_frame_index)` timeline/overlay 幂等 upsert；
- `frame_uuid` 优先、PTS 次级的帧对齐；
- runtime epoch/source 隔离；
- duration、window、playability 和 annotation guards；
- bundle/artifact/timeline/overlay 最终一致收口；
- 成功证据只清理可重建 sidecar，失败证据保留诊断；
- 重复扫描和重启后不会生成第二个逻辑证据包。

### 2.3 当前结构风险

| Risk | Consequence |
| --- | --- |
| 超长协调函数 | 任一局部改动可能改变 ACK、slot、状态或清理顺序 |
| I/O 与决策混合 | 无法对 planner/state machine 做纯单元测试 |
| 多处直接状态写入 | 同一状态在 task、event payload、bundle 中可能漂移 |
| 宽泛异常边界 | 暂态、永久错误和代码缺陷容易被同类处理 |
| 隐式资源所有权 | permit、lease、DB connection、future、文件清理由不同分支释放 |
| 双 worker 共管生命周期 | rolling expiry、Replay slot、finalizing/ready 所有权容易重叠 |
| 巨型兼容分支 | feature flag 能切换路径，但旧路径难以真正删除 |

### 2.4 根因判断

根因不是 Python、Redis 或 PostgreSQL 本身，也不是简单的“线程数不够”。当前结构是
多轮可靠性修复持续落入原主循环后的累积结果：每次修复单独看都合理，但缺少稳定的
job boundary、状态 transition owner 和 infrastructure port，最终让协调函数同时承担
业务决策、资源所有权和外部副作用。

因此只做以下动作不能关闭问题：

- 只把超长函数机械切成若干 `_helper()`；
- 只增加 executor 或 consumer 数；
- 只把代码移动到更多文件但继续互相调用 private helper；
- 只增加 feature flag 而永久保留两套路径；
- 只增加重试以掩盖 ACK、lease 或 publish 顺序不清；
- 只以测试数量或行数下降证明架构已修复。

真正的关闭条件是：副作用所有权唯一、状态迁移可条件验证、资源生命周期有界、重启
可从 durable state 恢复，并且协调器只负责显式阶段编排。

## 3. Scope

### 3.1 In Scope

本计划负责：

1. `clip-worker` 内部模块边界、短协调器和可恢复 ACK 合同；
2. `media-worker` 在 Spec 33 基础上的模块抽取和旧巨型函数退场；
3. evidence lifecycle、Replay slot 和 materialization phase 的写入所有权；
4. DB、Redis、Replay HTTP、filesystem/subprocess 的 adapter 边界；
5. 配置解析、启动校验、资源生命周期和 shutdown 顺序；
6. characterization、contract、fault-injection、restart 和 runtime 验收；
7. 临时 V2 flags、灰度、回滚和最终删除规则；
8. 防止 `worker.py` 再次膨胀的静态结构门禁。

### 3.2 Non-Goals

本计划不做：

- 不改变 Savant 模型链、采样 FPS、检测阈值或行为规则；
- 不改变 8090 产品交互、可读摄像头名或 watchlist image-first 语义；
- 不改变 5+5 视频窗口、frame UUID 主锚点或 raw clip 格式；
- 不把 PostgreSQL 队列替换为 Kafka/Celery/RabbitMQ；
- 不将 Redis 变成最终状态源；
- 不新增第二次 RTSP 拉流；
- 不把两个 worker 合并成一个进程；
- 第一阶段不新增 `proof-service`、`finalizer-service` 等容器；
- 不借重构顺手调整容量、grace、重试次数或业务优先级；
- 不把 Spec 33 尚未实现的 Replay fallback 描述成可用回滚路径；
- 不在本计划中处理 API 鉴权、Docker Socket 或全仓依赖锁定问题。

## 4. Non-Negotiable Contracts

### 4.1 External Compatibility

重构期间以下外部形状保持兼容：

- Redis stream 名、consumer group、核心 message fields；
- Replay API URL、job payload 和 sink endpoint；
- `events`、`evidence_tasks`、`evidence_bundles` 等现有列语义；
- 8090/API 可见 `evidence_state`、`evidence_reason` 和 playable 判定；
- evidence 目录和 `raw_clip.mov` 约定；
- 当前 compose service 名、容器名和启动入口。

允许 Spec 33 按其迁移计划增加 lease/phase/retry 字段，但不得为了模块拆分修改业务
结果或删除兼容字段。

### 4.2 PostgreSQL Is State Authority

PostgreSQL 是以下状态的唯一调度权威：

- evidence/materialization 当前状态；
- Replay slot 当前 owner/status/deadline；
- durable finalizer handoff；
- terminal bundle/index 结果。

Redis 提供 delivery 和 frame/event 数据流，不承担最终任务状态。内存集合、future、
semaphore 和本地 processed-state 只可作为有界运行时辅助，不能成为重启后唯一真相。

### 4.3 Identity And Time Domains

任何抽取都不得弱化以下身份层级：

```text
event identity:       event_id + source_event_id
runtime isolation:    source_id + runtime_epoch_id
stream isolation:     stream_session_id
visual identity:      frame_uuid / keyframe_uuid
window calculation:   frame_pts
business time:        event_ts_ms / created_at
```

`event_ts_ms`、Redis stream id 和 wall clock 不能替代 frame UUID/PTS 进行媒体锚定。
跨 epoch fallback 继续 fail closed。

### 4.4 Side Effects Must Be Ordered

任何协调器状态转换都必须遵循：

```text
validate input
  -> obtain execution capacity
  -> acquire fenced durable ownership
  -> perform bounded external work
  -> persist durable outcome with CAS/idempotency
  -> ACK or publish canonical artifact
  -> release resource in outermost finally
```

具体 publish/ACK 顺序由下文阶段合同定义。不得用“最终会补偿”掩盖一个未持久化
就 ACK、未验 fence 就覆盖文件、或提前释放 permit 的分支。

### 4.5 Error Taxonomy

两个 worker 使用相同错误类别，不再让异常文本决定重试：

| Class | Examples | Required outcome |
| --- | --- | --- |
| invalid input | missing source/epoch, invalid window | terminal/quarantine + ACK |
| duplicate/terminal | target already ready/failed | idempotent no-op + ACK |
| not ready | proof/coverage not yet visible | durable waiting + retry time |
| capacity | Replay slot/lane/permit unavailable | queued/pending; no retry burn |
| transient dependency | Redis/DB/Replay temporary failure | no ACK or durable retry |
| permanent external | Replay rejects valid request permanently | terminal reason + ACK |
| stale owner | lease/fence lost | no publish/commit/cleanup; recoverable exit |
| code defect/unknown | unexpected exception | log traceback, no unsafe ACK, alert |

稳定 reason code 与诊断文本分开存储。代码不得解析自由文本来选择下一状态。

### 4.6 No Dual Side-Effect Writers

灰度可以对纯 planner 同时运行 legacy/V2 并比较结果，但不能同时：

- 创建两个 Replay job；
- claim 同一个 evidence task；
- 写两份 canonical bundle；
- 更新两次 terminal state；
- ACK 同一个 delivery 的两条执行路径。

每次请求只允许一个 side-effect owner。Shadow mode 只能执行无副作用的解析、规划
和结果 diff。

## 5. Lifecycle Ownership Matrix

### 5.1 Stage Ownership

| Stage | Authoritative owner | Durable proof of completion |
| --- | --- | --- |
| event/task creation | event-worker | event + evidence task row |
| record request delivery | Redis/event-worker | stream entry + dedupe key |
| proof waiting | clip-worker | waiting phase/reason/next attempt |
| Replay admission | clip-worker repository | active fenced replay slot |
| Replay job creation | clip-worker | job id + resulting stream persisted |
| sink video stability | media-worker | immutable file identity/handoff |
| Replay slot release after success | media-worker | conditional released transition |
| rolling remux/image extraction | media-worker | fenced attempt/handoff |
| bundle/final index | media-worker | DB bundle/artifact/timeline/overlay rows |
| evidence terminal projection | media-worker | task CAS + event projection |
| timed-out Replay slot recovery | clip-worker repository | conditional timeout transition |
| rolling lease/deadline recovery | media-worker only | Spec 33 recovery transition |

`clip-worker` 的 deadline sweep 在 Spec 33 Phase 1 后只处理 Replay-owned phases，
不得过期 rolling materialization；`media-worker` 不重新进行 Replay admission。

### 5.2 Redis ACK Matrix

目标 ACK 合同：

| Outcome | DB/external state before ACK | ACK? |
| --- | --- | --- |
| malformed/invalid request | stable terminal/quarantine reason | yes |
| duplicate terminal target | existing terminal row verified | yes |
| proof not ready | durable retry/wait state and retry policy | only if DB-driven rescheduler exists; otherwise leave pending |
| capacity unavailable | durable queued/pending state | only if DB-driven rescheduler exists; otherwise leave pending |
| Replay job created | job id/resulting stream + replaying state persisted | yes |
| Replay permanent failure | slot released + terminal reason persisted | yes |
| DB/Redis/Replay transient failure | no unsafe terminal state | no |
| unexpected exception | diagnostic emitted, ownership converged | no by default |

Phase 0 必须记录当前每个分支的实际 ACK 行为；Phase 1 明确 DB-driven retry 是否已
完整存在。在证明存在之前，不得把 pending delivery 提前 ACK。

### 5.3 Replay-to-Media Handoff

成功 Replay job 的 handoff 至少包含：

```text
event_id / source_event_id
request_id / replay_job_id
source_id / camera_id
runtime_epoch_id / stream_session_id
resulting_stream_id / sink instance
requested and effective PTS window
frame/keyframe anchors
slot owner/token/deadline
```

`media-worker` 只能在 sink identity 与 handoff 匹配且 file stability 已证明后释放
completion-aware slot。模糊目录名、当前 epoch 猜测或只按 source 匹配均不能作为
成功交接。

## 6. Target Internal Architecture

### 6.1 Clip Worker

目标模块：

```text
services/clip-worker/app/
  contracts.py
  request_consumer.py
  proof_resolver.py
  replay_planner.py
  replay_admission_repository.py
  evidence_state_repository.py
  replay_client.py
  coordinator.py
  worker.py                  # composition root / compatibility facade only
```

职责：

#### `contracts.py`

- frozen dataclasses/typed enums for request、proof、plan、admission 和 outcome；
- normalized reason codes；
- 不导入 Redis、psycopg、httpx 或运行时 config。

#### `request_consumer.py`

- consumer group ensure/read/reclaim/ACK；
- 把 Redis bytes 转成 delivery envelope；
- 不解释 Replay policy，不写 PostgreSQL。

#### `proof_resolver.py`

- frame annotation 查询、bounded cache 和并发 gate；
- UUID/keyframe/PTS/session proof；
- 返回 `ReadyProof`、`NotReady` 或稳定 invalid outcome；
- 不创建 Replay job，不持有 Replay slot。

现有 `_find_*proof*`、`_derive_*window*` 等函数先原样移动并由 characterization tests
保护，再小步纯化；本阶段不重写 frame-domain 算法。

#### `replay_planner.py`

- 输入 request + proof + immutable config snapshot；
- 纯函数生成 shard-independent Replay plan、duration、anchor 和 labels；
- 同一输入必须稳定序列化为同一 plan hash；
- 不读 Redis/DB，不发 HTTP。

#### `replay_admission_repository.py`

- 原子 global/shard/source slot 获取；
- timeout/release/record job；
- owner/token/CAS；
- 只处理 Replay-owned phases。

#### `evidence_state_repository.py`

- target existence、terminal check、状态/诊断 projection；
- 每个 transition 是有 expected-state 条件的命名方法；
- 调用方不能拼装任意状态字符串或 SQL。

#### `replay_client.py`

- Replay HTTP transport、timeouts、response validation；
- payload builder 逐步移到 planner；
- transport error 与 permanent response error 分型。

#### `coordinator.py`

- `process_one(delivery)` 只编排上述组件；
- 每个分支返回显式 `ProcessingOutcome`，由单一 ACK policy 决定 ACK；
- 不包含 SQL、Redis command、HTTP payload 或 proof 扫描实现；
- `tick()` 只负责 read/reclaim、调用 `process_one()` 和周期 recovery。

### 6.2 Media Worker

Spec 33 指定的下列模块保持权威：

```text
materialization_scheduler.py
evidence_finalizer.py
segment_index.py
```

为完成巨型 `worker.py` 退场，再补充：

```text
services/media-worker/app/
  materialization_contracts.py
  sink_discovery.py
  media_probe.py              # 可先封装现有 probe functions
  evidence_job.py
  evidence_repository.py
  artifact_publisher.py
  cleanup_service.py
  worker.py                   # composition root / compatibility facade only
```

边界如下：

- `sink_discovery` 只发现候选、建立稳定 file identity，不做 ffprobe/finalize；
- `evidence_job` 把一个 fenced job 编排为 probe、assemble、publish、index、commit；
- `artifact_publisher` 只管理 attempt staging、fence check、atomic rename；
- `evidence_repository` 统一 task/event/bundle/index 的 transaction 和 CAS；
- `cleanup_service` 只在 terminal commit 后幂等清理，失败进入 `cleanup_pending`；
- `worker.py` 创建 config、scheduler、pool、index、executors，管理 signal/shutdown。

现有 `frame_cache_sidecar_writer.py`、`post_savant_evidence_bundle.py`、
`evidence_db_index.py` 等算法/构建模块不为了目录美观强行重写。先由新的 job boundary
调用，只有指标或测试证明内部职责冲突时再拆。

### 6.3 Dependency Rules

```text
composition root
  -> coordinator/scheduler
      -> domain contracts + ports
          <- Redis/DB/HTTP/filesystem/subprocess adapters
```

静态门禁必须证明：

- planner/contracts 不导入 `redis`、`psycopg`、`httpx`、`subprocess`；
- coordinator 不包含 SQL 字符串和直接 `xack/xreadgroup`；
- media scheduler tick 不调用 ffmpeg/ffprobe、sidecar build 或 future wait；
- repository 不读取任意进程全局 config；
- adapter 不自行决定产品状态迁移；
- 禁止 `from worker import _private_helper` 成为新模块依赖方式。

## 7. Implementation Phases

### Phase 0 - Freeze Behavior And Establish Safety Net

不改变运行行为。

1. 记录当前 revision、镜像、compose config、migration hash 和有效环境变量；
2. 为 `clip-worker` 每个 ACK/slot/status 分支建立 characterization table；
3. 固化一组 request/proof/Replay payload golden fixtures，覆盖：
   - normal full window；
   - truncated pre-window；
   - cross-session post proof；
   - missing proof；
   - duplicate target；
   - priority/non-priority capacity full；
   - Replay API transient/permanent failure；
4. 为 `media-worker` 固化 raw sink、rolling video、watchlist image、duplicate、
   corrupt input、late annotation 和 epoch mismatch fixtures；
5. 记录 DB statements/row projections、artifact hashes、timeline/overlay counts；
6. 加入每次 request/job 的 correlation fields：event、request、attempt、lease、
   Replay job、runtime epoch；
7. 给现有 `run_worker()`、`_process_sink_output()` 和 finalizer 建 branch inventory，
   所有早退分支必须列出 resource/ACK/state outcome。

Phase 0 只允许加测试、观测和无行为影响的 adapter seam。

Acceptance token:

```text
PASS_CLIP_MEDIA_LEGACY_BEHAVIOR_BASELINE_FROZEN
```

### Phase 1 - Unify Lifecycle And Transition Ownership

本阶段与 Spec 33 Phase 1 同步执行，先修状态合同再引入异步结构。

1. 建立 lifecycle transition matrix 和 normalized reason codes；
2. 将 `update_clip_status()`、media task/event 更新收拢为命名 transition methods；
3. 每个 transition 使用 expected state/owner/token 的条件更新；
4. 区分 business deadline、Replay slot deadline 和 materialization lease；
5. clip deadline recovery 排除 rolling-owned phases；
6. media recovery 成为 rolling lease/deadline 的唯一 owner；
7. 明确 Redis ACK policy，并对所有分支加入契约测试；
8. 修复 Spec 33 定义的 deferred/pending/ready/lease 冲突；
9. API/report/pressure analyzer 使用同一合法状态集合进行 contract validation；
10. 迁移和代码必须可以先后部署，禁止新代码依赖尚未应用的非空字段而直接崩溃。

Acceptance token:

```text
PASS_CLIP_MEDIA_LIFECYCLE_OWNERSHIP_UNIFIED
```

### Phase 2 - Extract Clip Pure Decisions

不改变 Redis/DB/Replay side effects。

1. 新建 typed contracts；
2. 把 request normalization、gate decision、window/anchor plan 抽成纯函数；
3. 把 proof resolver 作为一个有界 port 抽出，先搬迁后优化；
4. 以 legacy path 为 oracle 对全部 golden fixtures 做 plan/payload/label diff；
5. 相同输入的 V2 plan hash 必须稳定；
6. legacy 和 V2 结果不一致时 fail test，不以兼容 fallback 静默选一个；
7. 纯 planner shadow 可以在 canary 记录 diff，但不得发第二个 Replay job。

Acceptance token:

```text
PASS_CLIP_WORKER_PURE_PLAN_PARITY
```

### Phase 3 - Introduce Clip Coordinator V2

1. 抽出 Redis consumer/reclaim/ACK adapter；
2. 拆分 Replay admission repository 与 evidence state repository；
3. 创建 `ClipCoordinator.process_one()` 和显式 `ProcessingOutcome`；
4. 单一 ACK policy 根据 outcome 决策，不允许业务分支散布 `xack()`；
5. Replay slot acquire/release 使用 owner/token 上下文管理边界；
6. Replay create 成功必须先持久化 job/handoff/replaying，再 ACK；
7. 对 DB commit 前后、Replay response 前后、ACK 前后注入崩溃；
8. pending reclaim 后必须收敛为零或一个 Replay job，不得重复；
9. 主循环只做 read/reclaim/process/recovery/shutdown；
10. 临时使用 `CLIP_WORKER_COORDINATOR_V2_ENABLED` 做单路径 canary。

V2 默认关闭，完成一源、二源和重启恢复后才切为默认开启。关闭 V2 只能切换
coordinator，数据库状态合同和幂等 repository 不能回退。

Acceptance token:

```text
PASS_CLIP_WORKER_COORDINATOR_V2_RECOVERABLE
```

### Phase 4 - Execute Media Scheduler Contract

本阶段不重新设计，严格执行 Spec 33 Phase 2-5：

1. 抽出完整 `finalize_one()`；
2. 引入 shared end-to-end WIP、fenced lease 和 durable handoff；
3. 引入长期有界 image/remux/finalizer lanes；
4. 引入有界 PostgreSQL pool；
5. 保证 scheduler tick 不运行 media work 或等待 future；
6. 引入 source/epoch `RollingSegmentIndex`；
7. 完整执行 staging -> fence -> atomic publish -> DB terminal commit -> cleanup；
8. 实施 Spec 33 的 shutdown、recovery、capacity 和 performance gates。

如果本计划与 Spec 33 在字段、迁移、permit 或 lease 语义上不同，以 Spec 33 为准，
并回写修订本计划，不得实现两套 scheduler 合同。

Acceptance tokens 直接复用 Spec 33 Phase 2-5，不另造同义 token。

### Phase 5 - Extract Remaining Media Responsibilities

Scheduler V2 正确后再处理代码结构，避免同时改调度和媒体算法。

1. 抽出 sink candidate discovery/stability identity；
2. 将单任务路径封装为 `EvidenceJob`，输入为 immutable job contract；
3. 抽出 artifact staging/publish 和 cleanup；
4. 将散落的 task/event/bundle/index SQL 收敛到 repository transaction；
5. 现有 bundle、timeline、overlay 和 sidecar builders 作为纯/准纯组件调用；
6. 删除 `_process_sink_output()` 对 batch finalizer 的递归调用；
7. snapshot/annotation/image evidence 全部进入有界 lane；
8. duplicate、terminal、invalid、lost lease 和 cleanup failure 都返回 typed outcome；
9. 对 legacy/V2 固定输入比较 artifact hash、DB rows 和 terminal projection；
10. 确认抽取不改变帧数、duration guard、bbox/timeline 对齐和 playable 判定。

Acceptance token:

```text
PASS_MEDIA_WORKER_JOB_BOUNDARIES_PARITY
```

### Phase 6 - Cross-Worker Recovery And Shutdown Soak

1. clip 在 Replay create 后、DB persist 前崩溃；
2. clip 在 DB persist 后、ACK 前崩溃；
3. media 在 sink stable 后、slot release 前崩溃；
4. media 在 remux 后、handoff persist 前后崩溃；
5. media 在 canonical rename 前后和 DB terminal commit 前后崩溃；
6. DB/Redis/Replay 分别短暂不可用并恢复；
7. runtime epoch 在 waiting proof、replaying、finalizing 阶段切换；
8. SIGTERM 在每个阶段触发，必须在 compose grace 内收敛；
9. 所有场景最终满足：零重复 job、零重复 bundle、零悬挂 permit、零无 owner
   active lease、Redis pending 可解释；
10. 8090 最终状态与 DB 权威状态一致。

Acceptance token:

```text
PASS_CLIP_MEDIA_CRASH_RECOVERY_AND_SHUTDOWN
```

### Phase 7 - Remove Legacy Paths And Enforce Structure

只有完成两轮同口径压力通过和一次 restart soak 后才执行。

1. 删除 legacy clip coordinator 和 V2 flag；
2. 按 Spec 33 删除 media legacy scheduler、假 fallback 和临时 flags；
3. 删除旧 direct state writes、散布 ACK、重复 expiry 和 per-batch executor；
4. `worker.py` 只保留 composition、lifecycle 和兼容 import facade；
5. 新增 AST/static architecture tests；
6. 禁止新业务逻辑进入 composition root；
7. 更新旧规格 implementation status 和新的权威模块路径；
8. 记录 legacy 删除 commit、最终镜像和回滚镜像。

结构完成门槛：

- `clip-worker/app/worker.py` 目标不超过 900 行；
- `media-worker/app/worker.py` 目标不超过 1,200 行；
- coordinator/scheduler 单函数目标不超过 150 行；
- 新增或修改的 orchestration function 不超过 200 行；
- 超出目标必须在 review 中给出单一职责证明和后续 issue，不能通过压缩格式过门禁；
- 行数只是回归警报，最终以依赖方向、side-effect ownership 和测试为准。

Acceptance token:

```text
PASS_CLIP_MEDIA_LEGACY_ORCHESTRATORS_REMOVED
```

## 8. Test Matrix

### 8.1 Clip Unit And Contract Tests

- request bytes/JSON normalization；
- every ACK outcome；
- pending reclaim and delivery count；
- proof ready/not-ready/invalid/timeout；
- UUID/session/epoch isolation；
- full/truncated/cross-session window plan parity；
- priority/quota/cooldown decisions；
- shard mapping and stable plan hash；
- atomic admission under N concurrent consumers；
- Replay transient/permanent/invalid response；
- crash before/after Replay persist and ACK；
- slot timeout/release idempotency；
- unexpected exception never unsafe-ACKs。

至少更新并运行：

```text
harness/tests/test_clip_worker_queue_safety.py
harness/tests/test_completion_aware_replay_admission.py
harness/tests/test_midterm_replay_epoch_isolation.py
harness/tests/test_midterm_replay_cadence_payload.py
harness/tests/test_midterm_replay_duration_tuning.py
harness/tests/test_replay_shard_routing.py
```

新增建议：

```text
harness/tests/test_clip_worker_coordinator_contract.py
harness/tests/test_clip_worker_ack_recovery.py
harness/tests/test_clip_worker_plan_parity.py
```

### 8.2 Media Unit And Contract Tests

除 Spec 33 的完整矩阵外，增加：

- sink discovery 不做 probe/finalize；
- one immutable job contract -> one terminal outcome；
- artifact publish fence and atomicity；
- DB bundle/index/task/event transaction convergence；
- cleanup failure does not reverse ready；
- legacy/V2 fixture artifact/row parity；
- timeline/overlay row counts and hashes；
- no scheduler-thread media/subprocess work；
- no direct terminal state write outside repository；
- composition root resource close order。

至少更新并运行：

```text
harness/tests/test_evidence_materialization_phase0.py
harness/tests/test_evidence_materialization_phase2plus.py
harness/tests/test_media_worker_perf_safety.py
harness/tests/test_rolling_cache_materialization.py
harness/tests/test_evidence_db_index.py
harness/tests/test_midterm_worker_indexes_static.py
```

新增建议：

```text
harness/tests/test_media_worker_job_contract.py
harness/tests/test_media_worker_artifact_publish_recovery.py
harness/tests/test_worker_architecture_boundaries.py
```

### 8.3 Cross-Worker Integration Tests

- event -> request -> Replay -> sink -> final bundle happy path；
- image-only path never creates Replay job；
- clip ACK/reclaim exactly-once-effect；
- completion-aware slot released by matching sink only；
- rolling and Replay path do not both materialize one event；
- epoch switch fences both workers；
- terminal state and 8090 playable projection agree；
- maintenance/restart leaves no unexplained active tasks。

### 8.4 Static Architecture Tests

AST/import tests检查：

- pure modules 禁止 infrastructure imports；
- coordinators 禁止 SQL、Redis command 和 subprocess；
- direct `xack()` 只存在 consumer adapter；
- direct materialization terminal SQL 只存在指定 repository；
- no new import from `worker.py` private helpers；
- no per-batch executor construction；
- worker composition roots satisfy size/function thresholds；
- migration/status sets agree across clip/media/API/report tooling。

## 9. Validation Ladder

每一级保留 artifact、日志、DB/Redis snapshot 和有效配置：

1. pure/golden fixture parity；
2. deterministic unit + fault injection；
3. one-source Replay video + rolling video + image-only；
4. two-source mixed path and priority fairness；
5. eight-source canary with clip/media restart；
6. fixed-input 60-source comparison；
7. live 60-source comparison；
8. restart/recovery soak。

行为保持阶段要求与 baseline：

- 创建任务数按事件类型完全一致；
- Replay job payload canonical diff 为零；
- artifact playable/duration/frame count 无回归；
- timeline/overlay identity and counts 无非预期差异；
- terminal status/reason 分类无非预期差异；
- Redis pending/lag 在 drain 后归零或全部有明确 durable retry；
- p95 latency 不得恶化超过 10%。

调度性能提升阶段采用 Spec 33 的更严格 release/final closure gates，不用本节较宽的
行为保持阈值替代。

## 10. Go / No-Go Gates

### 10.1 Correctness

全部必须满足：

- duplicate Replay jobs = 0；
- duplicate canonical bundles = 0；
- terminal state contradictions = 0；
- stale owner publish/commit/cleanup = 0；
- cross-epoch proof/materialization = 0；
- ACK without durable outcome = 0；
- active slot/lease/permit after drain = 0；
- playable retained evidence checks pass；
- image/video evidence type 不因重构改变；
- 8090 visible state 与数据库权威状态一致。

### 10.2 Structural

- Clip side-effect branches 只能通过 coordinator ports；
- ACK 决策集中在一个 policy；
- Media scheduler tick 无阻塞媒体工作；
- DB 状态迁移集中并带 expected state/fence；
- executor/queue/connection 均有界并可观测；
- worker composition root 不再承载业务算法；
- architecture tests 防止依赖反转和体积回长。

### 10.3 Operational

- 启动日志报告 requested/effective flags、state contract version 和容量；
- readiness 能区分 DB/Redis/Replay/schema/recovery 问题；
- SIGTERM 在 grace 内完成或留下可恢复 durable state；
- 每个失败可用 event/request/job/attempt/lease/epoch 关联；
- rollback canary 能生成一份完整、可播放、无重复的证据。

任何数据丢失、重复 job/bundle、fence 失效、不可收敛 drain、错误 ACK 或跨 epoch
证据均立即 no-go，不允许用吞吐提升抵消。

### 10.4 Minimum Verification Commands

每个适用 phase 至少执行：

```bash
python -m compileall -q \
  services/clip-worker/app \
  services/media-worker/app

pytest -q \
  harness/tests/test_clip_worker_queue_safety.py \
  harness/tests/test_completion_aware_replay_admission.py \
  harness/tests/test_midterm_replay_epoch_isolation.py \
  harness/tests/test_replay_shard_routing.py

pytest -q \
  harness/tests/test_evidence_materialization_phase0.py \
  harness/tests/test_evidence_materialization_phase2plus.py \
  harness/tests/test_media_worker_perf_safety.py \
  harness/tests/test_rolling_cache_materialization.py \
  harness/tests/test_evidence_db_index.py \
  harness/tests/test_midterm_worker_indexes_static.py

docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
python -m pytest -q harness/tests/test_midterm_deployment_contract.py
git diff --check
```

纯代码变更通过静态/单元测试后，按影响范围 recreate，不默认 rebuild：

```bash
docker compose -f infra/docker-compose.midterm.yml up -d \
  --no-build --force-recreate --no-deps clip-worker media-worker
```

如果本 phase 修改 Dockerfile、requirements、base image 或 connection-pool dependency，
必须先 build 并记录 image digest，再 recreate。不得用隐藏 local override 代替 tracked
compose 行为。

## 11. Rollout Flags And Deployment

允许的临时 flags：

```text
CLIP_WORKER_COORDINATOR_V2_ENABLED
MEDIA_WORKER_SCHEDULER_V2_ENABLED
MEDIA_WORKER_DB_POOL_ENABLED
MEDIA_WORKER_SEGMENT_INDEX_ENABLED
```

规则：

1. 所有 flag 必须暴露 requested/effective 值；
2. 非法组合启动失败，不静默降级；
3. state contract/repository 幂等修复不是可回滚 flag；
4. shadow 仅限纯 planner，不允许双 side effects；
5. 每个 flag 有 owner、删除 phase 和最晚删除条件；
6. Phase 7 后生产代码不得保留 legacy coordinator/scheduler。

部署顺序：

1. 测试/观测 seams；
2. additive schema + compatible repositories；
3. Clip pure planner/proof modules；
4. Clip Coordinator V2 canary；
5. Spec 33 Media Scheduler V2；
6. remaining Media extraction；
7. cross-worker soak；
8. legacy deletion。

纯 Python 且当前 compose 已挂载源码的阶段，验证时 recreate/restart 受影响服务，不
rebuild。Dockerfile、requirements、base image 或 Spec 33 connection-pool dependency
变化时才 rebuild，并记录 image digest。

## 12. Rollback

### 12.1 Phase-Local Rollback

- pure extraction/parity 阶段：切回 legacy coordinator，保留测试和 compatible types；
- Clip V2：停止新读取，drain active Replay slots，关闭 V2，recreate clip-worker；
- Media V2：按 Spec 33 停止 claim，把 work 收敛到 terminal 或 durable
  `finalizer_pending`，再关闭 V2；
- segment index：可单独关闭并退回有界 compatibility scan；
- DB pool：V2 scheduler 依赖时不可单独关闭；
- additive schema 和规范化状态不回滚，旧镜像必须先证明 schema-compatible。

### 12.2 Coordinated Runtime Rollback

1. 暂停新 evidence admission；
2. 记录 Redis pending、active slots、leases、handoffs 和 task distribution；
3. drain 或逐项解释所有 active work；
4. 保存失败 artifact、日志、DB snapshot 和 image digest；
5. 部署记录的前一 schema-compatible image；
6. recreate 受影响 worker，不删除 Redis stream、DB row 或媒体目录；
7. 单一 canary 证明 request、Replay、sink、bundle、slot release 和 8090；
8. 确认无 duplicate 后恢复 admission。

不得使用 `ROLLING_CACHE_FALLBACK_TO_REPLAY=true` 作为通用回滚，因为当前没有
完整生产 transition 消费该开关。

## 13. Commit Discipline

每个 phase 分为可 review、可回滚 commit。建议序列：

1. `test: freeze clip and media orchestration behavior`
2. `refactor: centralize evidence lifecycle transitions`
3. `refactor: extract clip proof and replay planning`
4. `refactor: add recoverable clip coordinator`
5. 按 Spec 33 的独立 media scheduler commits
6. `refactor: extract media evidence job boundaries`
7. `test: prove clip media crash recovery`
8. `refactor: remove legacy worker orchestrators`

不得在一个 commit 中同时包含：

- schema normalization 与 coordinator concurrency；
- proof 算法改变与模块搬迁；
- scheduler 引入与容量调优；
- artifact publish 改变与 cleanup policy 改变；
- legacy 删除与新功能。

每个 commit 至少运行对应 targeted tests、`compileall` 和 `git diff --check`。涉及
compose/config 的 commit 还必须运行 compose config 和 deployment contract。

## 14. Definition Of Done

本计划完成必须同时具备：

```text
PASS_CLIP_MEDIA_LEGACY_BEHAVIOR_BASELINE_FROZEN
PASS_CLIP_MEDIA_LIFECYCLE_OWNERSHIP_UNIFIED
PASS_CLIP_WORKER_PURE_PLAN_PARITY
PASS_CLIP_WORKER_COORDINATOR_V2_RECOVERABLE
all required Spec 33 acceptance tokens
PASS_MEDIA_WORKER_JOB_BOUNDARIES_PARITY
PASS_CLIP_MEDIA_CRASH_RECOVERY_AND_SHUTDOWN
PASS_CLIP_MEDIA_LEGACY_ORCHESTRATORS_REMOVED
PASS_MIDTERM_CLIP_MEDIA_WORKER_STRUCTURAL_REMEDIATION
```

并且：

- 两轮同口径 60-source run 通过 Spec 33 final closure gates；
- 一轮 restart/recovery soak 通过；
- 新旧固定输入 parity artifact 可审计；
- legacy coordinator/scheduler 和临时 flags 已删除；
- 当前源码、迁移、镜像和有效 config 已记录；
- 目标 specs 更新 implementation status、commit 和验证证据；
- 没有以 TODO、永久 compatibility branch 或隐藏 override 代替未完成工作。

代码变短、测试通过或单次 canary 成功都不能单独宣称本计划完成。

## 15. Remaining Risks

即使本计划完成，以下风险仍需单独管理：

1. `clip-worker` 和 `media-worker` 仍共享 PostgreSQL 生命周期，schema rollout 顺序
   和连接容量仍是系统级风险；
2. Replay、video-file-sink 和 filesystem 之间无法获得真正的跨系统 ACID，只能依靠
   fencing、immutable identity、idempotent upsert 和 recovery 收敛；
3. ffmpeg/ffprobe 的 subprocess 资源占用仍可能造成 CPU/IO 尾延迟，Spec 33 容量验收
   通过后仍需持续监控；
4. Redis Streams 是至少一次 delivery，外部 Replay API 若不支持稳定 idempotency key，
   极端 crash window 仍需用查询/对账闭合；
5. 结构拆分不会自动解决 upstream forwarder queue、Savant send failure 或模型吞吐；
6. 现有 evidence 状态同时投影到 task、event payload 和 bundle，完全消除兼容投影需要
   另行 API/data migration；
7. 第一阶段保持单进程，如果长期指标证明 finalizer 需要独立扩缩容，必须另立服务拆分
   方案，不能直接把本计划的内部 queue 当成分布式协议；
8. 依赖版本锁定、API 鉴权和 Docker Socket 权限不在本计划内，仍是全系统生产化风险。

这些风险不得用来扩大当前实施范围，但最终验收报告必须逐项标明：已缓解、外部计划
承接或仍接受，不能在结构重构完成后隐去。

## 16. Future Execution Prompt

```text
Execute specs/34_midterm_clip_media_worker_structural_remediation_plan.md
phase by phase while treating Spec 33 as the authority for media-worker state,
lease, WIP, scheduler, finalizer, connection-pool and segment-index semantics.

Start from Phase 0. Preserve unrelated worktree changes. Do not rewrite media
algorithms or frame-domain proof logic during extraction. Freeze Redis ACK,
Replay payload, DB transition, artifact and 8090 outcomes before moving code.

Unify lifecycle ownership before adding asynchronous paths. Extract Clip pure
planning first, then introduce one side-effect owner through Coordinator V2.
Replay creation must be persisted before ACK, and reclaim must converge to one
effective job. Execute Spec 33 before extracting the remaining media job
boundaries. No scheduler tick may run media work or wait for futures.

Use feature flags only for one-path canary; shadow mode is pure and cannot
create a second job or bundle. Validate each phase with targeted tests,
fault injection, service recreate, DB/Redis/artifact evidence and the defined
runtime ladder. Do not remove legacy paths until two comparable pressure runs
and one restart soak pass. Finish by deleting flags and enforcing architecture
boundaries with static tests.
```
