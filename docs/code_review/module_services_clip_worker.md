# Phase 2 Module Review: services/clip-worker

审查范围：`services/clip-worker/`，并交叉核对当前 midterm compose/env、Replay shard 配置、相关迁移、测试与文档。本文只做报告，不修改代码。

## 1. 模块职责

`services/clip-worker` 是旧 per-event Replay fallback 路径的调度器。上游由 event-worker/face-worker 写入 Redis stream `security.record_requests`，clip-worker 消费后完成 Replay shard 路由、post-Savant frame proof wait、keyframe/anchor 校验、Replay job 创建，并把 `events.payload.media` 与 `evidence_tasks` 推进到 `waiting_proof`、`queued`、`replay_job_created`、`materialization_failed/materialization_expired` 等状态。入口在 `services/clip-worker/main.py:52-121`，核心循环在 `services/clip-worker/app/worker.py:3138-4275`。

下游包括：

- Replay REST API：`ReplayClient.find_keyframe()` 和 `ReplayClient.create_job()` 调用 `/api/v1/keyframes/find`、`/api/v1/job`，见 `services/clip-worker/app/replay_client.py:50-134`、`services/clip-worker/app/replay_client.py:136-307`。
- video-file-sink：Replay job payload 的 sink URL 来自 `REPLAY_JOB_SINK_URL` 或 shard 配置，payload 构造见 `services/clip-worker/app/replay_client.py:482-490`。
- media-worker：clip-worker 只创建 Replay job 与 DB slot，实际 sink 文件稳定、finalizer 和 bundle 落地由 media-worker 完成；slot release 函数在本模块提供，调用方在 media-worker，`release_replay_slot()` 见 `services/clip-worker/app/repository.py:810-913`。

## 2. 关键文件与角色

| 文件 | 行数 | 角色 |
| --- | ---: | --- |
| `services/clip-worker/main.py` | 121 | 进程入口。加载配置，按 `CLIP_WORKER_CONSUMER_COUNT` 启动多个 `multiprocessing.Process`，并负责异常退出后的全局 shutdown/terminate/kill，见 `services/clip-worker/main.py:52-117`。 |
| `services/clip-worker/app/config.py` | 330 | `Config` dataclass 与环境变量加载。字段定义见 `services/clip-worker/app/config.py:12-76`，读取逻辑见 `services/clip-worker/app/config.py:118-330`。 |
| `services/clip-worker/app/replay_client.py` | 502 | Replay HTTP client 与 Replay job payload 构造，包含 `REPLAY_TS_SYNC`、constant cadence、HTTP 400 fallback 和 delivery watchdog。 |
| `services/clip-worker/app/replay_shards.py` | 214 | Replay shard 配置解析与 `source_id -> shard` 路由。支持 `REPLAY_SHARDS_JSON`、`REPLAY_SHARDS_CONFIG_PATH` 和 implicit default shard，见 `services/clip-worker/app/replay_shards.py:87-127`。 |
| `services/clip-worker/app/repository.py` | 1463 | PostgreSQL 状态写入、Replay slot admission/release、deadline sweep 与 operator-visible media 状态同步。超过 500 行，主要函数见下表。 |
| `services/clip-worker/app/worker.py` | 4275 | 核心 worker。Redis consumer group、pending reclaim、proof wait、Replay admission、Replay job 创建、ACK/不 ACK 状态机都在此文件。超过 500 行，主要函数见下表。 |
| `services/clip-worker/Dockerfile` | 15 | Python 3.12 slim 镜像，安装 requirements，复制 app/main 并执行 `python main.py`。 |
| `services/clip-worker/requirements.txt` | 3 | 运行依赖 `httpx`、`redis`、`psycopg[binary]`。 |
| `services/clip-worker/app/__init__.py` | 0 | 空包标记。 |

`repository.py` 主要函数：

| 符号 | 行号 | 说明 |
| --- | --- | --- |
| `EVIDENCE_STATES` / `TERMINAL_EVIDENCE_STATES` | `services/clip-worker/app/repository.py:13-45` | 本模块自己的 evidence state 与终态枚举。 |
| `release_timed_out_replay_slots` | `services/clip-worker/app/repository.py:126-210` | 把过期 active Replay slot 标记为 `timeout` 并同步 events payload。 |
| `active_replay_slot_counts` | `services/clip-worker/app/repository.py:213-273` | 先释放超时 slot，再按 global/shard/source 统计 active slot。 |
| `try_acquire_replay_slot` | `services/clip-worker/app/repository.py:276-582` | 307 行核心函数；用 PostgreSQL advisory lock + `FOR UPDATE` + CTE 原子判定并预占 Replay slot。 |
| `record_replay_job_for_slot` | `services/clip-worker/app/repository.py:585-670` | 只在 `replay_slot_status='active'` 时把 `replay_job_id/resulting_stream_id` 写回 slot。 |
| `acquire_replay_slot` | `services/clip-worker/app/repository.py:673-808` | 旧式 slot persist 函数；当前 `worker.py` 未调用，只在测试中直接覆盖。 |
| `release_replay_slot` | `services/clip-worker/app/repository.py:810-913` | 只在 active slot 上释放，写 `released`、active age 和 events payload。 |
| `expire_materialization_deadlines` | `services/clip-worker/app/repository.py:916-996` | 周期性将 pending/deferred/replaying/finalizing 等过期任务置为 `materialization_expired`，并保护 rolling-cache materializing 与 covered-by 关系。 |
| `terminal_evidence_state` | `services/clip-worker/app/repository.py:1029-1078` | 处理 Redis record_request 前检查 event/task 是否已终态。 |
| `record_request_target_exists` | `services/clip-worker/app/repository.py:1081-1157` | 防 stale record_request：DB 中既无 event 也无 task 时允许 ACK 丢弃。 |
| `update_clip_status` | `services/clip-worker/app/repository.py:1159-1347` | 同步 `events.payload.media` 与 `evidence_tasks`，但 evidence_tasks update 只有 `WHERE event_id=...`，没有 CAS/终态保护。 |
| `update_evidence_media_result` | `services/clip-worker/app/repository.py:1349-1463` | 历史 midterm metadata/snapshot 写回函数；当前活动路径未调用。 |

`worker.py` 主要函数/类：

| 符号 | 行号 | 说明 |
| --- | --- | --- |
| `_proof_lookup_concurrency_gate` | `services/clip-worker/app/worker.py:64-76` | 进程内 `BoundedSemaphore`，只限制 frame annotation lookup 并发，不是 Replay job worker pool。 |
| `ClipGateDecision` / `ActiveReplayJob` / `ReplaySlotTiming` / `ReplayRoute` | `services/clip-worker/app/worker.py:79-117` | gate 决策、本地 fallback active job、slot 时间预算与 shard route 数据类。 |
| `FrameAnnotationAnchor` / `ReplayFrameDomainProofs` | `services/clip-worker/app/worker.py:119-146` | post-Savant frame proof 的 frame/keyframe/PTS 证据结构。 |
| `_replay_offset_seconds` / `_replay_duration_seconds` / `_effective_replay_slot_timing` | `services/clip-worker/app/worker.py:276-366` | 计算 Replay offset、duration 与 completion-aware slot timeout。 |
| `_read_frame_annotation_range_entries` | `services/clip-worker/app/worker.py:589-680` | bounded range scan + 进程内 TTL cache。 |
| `_frame_annotation_matches_domain` | `services/clip-worker/app/worker.py:744-760` | 按 `runtime_epoch_id` 和 `stream_session_id` 过滤 frame annotations。 |
| `_pending_delivery_counts` / `_claim_pending_entries` | `services/clip-worker/app/worker.py:1361-1456` | Redis pending 统计与 `XAUTOCLAIM`/fallback reclaim。 |
| `_redis_stream_group_diagnostics` | `services/clip-worker/app/worker.py:1505-1530` | 读取 pending/lag，但异常被 `pass` 静默吞掉。 |
| `_defer_clip_request` / `_defer_post_savant_frame_proof` / `_queue_clip_request` | `services/clip-worker/app/worker.py:1600-1833` | 延迟、waiting_proof、queued 状态写入；非终态路径通常不 ACK，依赖 pending reclaim。 |
| `_find_replay_frame_domain_proofs` | `services/clip-worker/app/worker.py:2049-2226` | 178 行核心 proof 查找，校验 start/post window、session/epoch、cross-session/truncated fallback。 |
| `_apply_replay_anchor_to_request` | `services/clip-worker/app/worker.py:2228-2414` | 187 行核心函数；把 proof 转成 Replay anchor、offset、duration 和 label 字段。 |
| `_prepare_post_savant_replay_request` | `services/clip-worker/app/worker.py:2452-2853` | 402 行核心 proof wait 函数；在消费循环内轮询 frame annotations、必要时查 keyframe，并可能 sleep。 |
| `_replay_job_labels` | `services/clip-worker/app/worker.py:2856-2922` | 将 proof/runtime/shard/session 信息写入 Replay labels。 |
| `_clip_gate_decision` | `services/clip-worker/app/worker.py:2940-3074` | 压力等级、run_once limit、event type quota、global/shard/source concurrency、per-camera cooldown gate。 |
| `connect_redis` | `services/clip-worker/app/worker.py:3101-3117` | Redis 连接，socket timeout 至少为 `max(10, poll_timeout+5)`。 |
| `_resolve_replay_route` | `services/clip-worker/app/worker.py:3120-3135` | 根据 `source_id` 解析 Replay shard 并复用 `ReplayClient`。 |
| `run_worker` | `services/clip-worker/app/worker.py:3138-4275` | 1138 行主循环；处理 Redis 消息、状态机、Replay slot、Replay job、ACK/不 ACK。 |

归档目录 `services/clip-worker/archive/phase-only/20260610/` 仍有历史实现文件，但当前入口和 imports 不引用；除文档一致性外，本报告不把它们当活跃代码。

## 3. 数据流

输入：

- Redis `security.record_requests`：默认由 `RECORD_REQUEST_STREAM` 指定，配置默认见 `services/clip-worker/app/config.py:125-128`，主循环 `XREADGROUP` 见 `services/clip-worker/app/worker.py:3222-3230`。
- Redis pending list：`_claim_pending_entries()` 用 `XAUTOCLAIM`/`XCLAIM` 回收未 ACK 的 waiting/queued 请求，见 `services/clip-worker/app/worker.py:1401-1491`。
- Redis `security.frame_annotations`：post-Savant proof 读取流名默认见 `services/clip-worker/app/config.py:247-249`，bounded range scan 见 `services/clip-worker/app/worker.py:548-680`。
- PostgreSQL `events`：读取终态与写 payload/media 状态，见 `services/clip-worker/app/repository.py:1029-1078`、`services/clip-worker/app/repository.py:1184-1238`。
- PostgreSQL `evidence_tasks`：slot admission/release、状态同步、deadline sweep，见 `services/clip-worker/app/repository.py:276-582`、`services/clip-worker/app/repository.py:810-996`。
- PostgreSQL `evidence_event_links`：deadline sweep 保护 covered-by parent/child，见 `services/clip-worker/app/repository.py:957-968`。
- Replay REST API：`/api/v1/keyframes/find` 与 `/api/v1/job`，见 `services/clip-worker/app/replay_client.py:99-134`、`services/clip-worker/app/replay_client.py:309-319`。

处理与输出：

```text
security.record_requests
  -> parse request / terminal target check / shard route
  -> optional schedule gate queue/defer
  -> optional post-Savant proof wait from security.frame_annotations
  -> atomic Replay slot admission in evidence_tasks
  -> Replay /api/v1/job with labels and sink URL
  -> events.payload.media + evidence_tasks status/slot updates
  -> XACK only for terminal/success/final failure; queued/waiting paths stay pending
```

文件系统：本模块不直接写 evidence 文件、raw clips、metadata 或 bundles；文件输出发生在 Replay/video-file-sink/media-worker。当前 active code 也不直接读写本地配置文件，除 `REPLAY_SHARDS_CONFIG_PATH` 可通过 `Path(...).read_text()` 读取 shard JSON，见 `services/clip-worker/app/replay_shards.py:97-102`。

## 4. 并发与状态机模型

进程模型：`main.py` 默认按 `CLIP_WORKER_CONSUMER_COUNT` 启动多个独立进程，当前代码默认 8，见 `services/clip-worker/app/config.py:145` 和 `services/clip-worker/main.py:62-87`。每个子进程有自己的 Redis/PostgreSQL 连接，异常退出后父进程请求全局 shutdown 并终止其它进程，见 `services/clip-worker/main.py:89-117`。

单进程内部模型：每个进程仍是顺序主循环。`run_worker()` 每轮先 reclaim pending，再 `XREADGROUP count=10` 读新消息，随后对 entries 做普通嵌套 `for` 顺序处理，见 `services/clip-worker/app/worker.py:3193-3239`。`_proof_lookup_concurrency_gate()` 是进程内 semaphore，只包住 frame annotation lookup，见 `services/clip-worker/app/worker.py:64-76`、`services/clip-worker/app/worker.py:2566-2570`，不是把一个 consumer 内的 record request 并行化。

状态转移：

```text
record_request Redis message
  -> stale/terminal: ACK, 不写新状态
  -> shard routing failed: failed/materialization_failed, ACK
  -> schedule hard/critical/quota terminal_defer: materialization_deferred, ACK
  -> concurrency gate denied: materialization_deferred/queued, 不 ACK
  -> cooldown defer: materialization_deferred, 不 ACK；预算耗尽后 failed, ACK
  -> post-Savant proof waiting: waiting_proof, 不 ACK
  -> proof retry budget exhausted: failed/materialization_failed, ACK
  -> atomic Replay slot active
  -> Replay job created: replay_job_created/materialization_pending, ACK
  -> Replay job create exception/None: materialization_failed, release slot, ACK
  -> timeout sweep: materialization_expired
```

已确认原子/CAS 边界：

- Replay slot active 预占使用 `pg_advisory_xact_lock`、`target ... FOR UPDATE`、active counts、decision 和 reserved CTE 在一个 SQL 中完成，见 `services/clip-worker/app/repository.py:330-496`。这是当前多进程并发下最关键的原子 admission。
- `try_acquire_replay_slot()` 明确把 `released/timeout` 视为 terminal slot，不重新写回 active，见 `services/clip-worker/app/repository.py:370-375`、`services/clip-worker/app/repository.py:446-449`。
- `record_replay_job_for_slot()` 只在 `replay_slot_status='active'` 时写 job id，见 `services/clip-worker/app/repository.py:607-638`。
- `release_replay_slot()` 只释放 active slot，见 `services/clip-worker/app/repository.py:834-870`。
- `release_timed_out_replay_slots()` 只处理 active 且 deadline 过期的 slot，见 `services/clip-worker/app/repository.py:135-170`。
- `expire_materialization_deadlines()` 带状态集合、deadline 条件、rolling-cache materializing 保护和 covered-by 保护，见 `services/clip-worker/app/repository.py:937-968`。

非原子/race 风险：

- `update_clip_status()` 先更新 `events`，然后只凭 `WHERE event_id = ...` 覆盖 `evidence_tasks.status/materialization_status`，没有 `WHERE materialization_status IN (...)` 或 terminal-state predicate，见 `services/clip-worker/app/repository.py:1184-1238`、`services/clip-worker/app/repository.py:1257-1318`。
- `update_evidence_media_result()` 同样按 `task_id/event_id` 覆盖 task，无状态 predicate，见 `services/clip-worker/app/repository.py:1425-1441`；当前只在归档代码中引用，但仍暴露在 active repository。
- 旧 `acquire_replay_slot()` 按 `event_id` 直接写 active slot，无 terminal/predicate 保护，见 `services/clip-worker/app/repository.py:714-768`；当前 `worker.py` 未导入/调用，`rg` 只发现测试和归档引用。
- `terminal_evidence_state()` 在主循环前置检查能减少旧 pending 重放，但它不是与后续 `update_clip_status()` 同一条 CAS SQL，见 `services/clip-worker/app/worker.py:3284-3297`。

proof wait 与 slot 占用：当前实现满足“waiting_proof/queued 不应消耗 Replay slot”的历史约束。`_defer_post_savant_frame_proof()` 只写 `waiting_proof` 并返回 True，不 ACK、不调用 slot admission，见 `services/clip-worker/app/worker.py:1717-1765`；真正 `try_acquire_replay_slot()` 在 proof/keyframe 准备后才调用，见 `services/clip-worker/app/worker.py:3906-3927`。但注意 schedule gate 会在 proof wait 前查询 active slot 并可能 queue，见 `services/clip-worker/app/worker.py:3378-3412`、`services/clip-worker/app/worker.py:3444-3469`；这不会占用 slot，但会把 proof wait 延后。

## 5. 配置项清单

`Config` 字段与 `load_config()` 读取项：

| 配置 | 代码默认值 | 当前 midterm 覆盖/说明 |
| --- | --- | --- |
| `REDIS_URL` | `redis://redis:6379/0`，`services/clip-worker/app/config.py:125` | compose 固定同值，`infra/docker-compose.midterm.yml:902`。 |
| `RECORD_REQUEST_STREAM` | `security.record_requests`，`services/clip-worker/app/config.py:126-128` | compose 固定，`infra/docker-compose.midterm.yml:904`。 |
| `REPLAY_API_URL` | `http://replay-service:8080`，`services/clip-worker/app/config.py:119` | compose 固定，`infra/docker-compose.midterm.yml:905`。 |
| `REPLAY_JOB_SINK_URL` | `dealer+connect:tcp://video-file-sink:6666`，`services/clip-worker/app/config.py:120-123` | compose 固定，`infra/docker-compose.midterm.yml:906`。 |
| `REPLAY_SHARDS_JSON` | 空时 fallback 到 path/default，`services/clip-worker/app/replay_shards.py:97-116` | compose 默认空，`infra/docker-compose.midterm.yml:907`。 |
| `REPLAY_SHARDS_CONFIG_PATH` | 空时 implicit default shard，`services/clip-worker/app/replay_shards.py:97-116` | compose 默认空但挂载 `../infra/config`，`infra/docker-compose.midterm.yml:908`、`infra/docker-compose.midterm.yml:968-970`。 |
| `REPLAY_IN_STREAM_ENDPOINT` | `dealer+connect:tcp://replay-service:5555`，`services/clip-worker/app/config.py:131-136` | compose 未显式设置。 |
| `DATABASE_URL` | postgres container URL，`services/clip-worker/app/config.py:139-142` | compose 默认 host.docker.internal，`infra/docker-compose.midterm.yml:903`。 |
| `CONSUMER_GROUP` / `CONSUMER_NAME` | `clip-workers` / `clip-worker-1`，`services/clip-worker/app/config.py:143-144` | compose 设 `clip-workers-midterm` / `clip-worker-midterm-1`，`infra/docker-compose.midterm.yml:909-910`。 |
| `CLIP_WORKER_CONSUMER_COUNT` | 8，`services/clip-worker/app/config.py:145` | env/compose 8，`infra/env/midterm.env:133`、`infra/docker-compose.midterm.yml:911`。 |
| `POLL_TIMEOUT_MS` | 5000，`services/clip-worker/app/config.py:146` | compose 固定 5000，`infra/docker-compose.midterm.yml:912`。 |
| `DEFAULT_PRE_SECONDS` / `DEFAULT_POST_SECONDS` | 5 / 5，`services/clip-worker/app/config.py:147-148` | env/compose 5/5，`infra/env/midterm.env:127-128`、`infra/docker-compose.midterm.yml:913-914`。 |
| `KEYFRAME_LOOKUP_WINDOW_S` | 10，`services/clip-worker/app/config.py:149` | compose default 15，`infra/docker-compose.midterm.yml:915`。 |
| `CLIP_WORKER_MAX_JOBS_PER_RUN` | 0，`services/clip-worker/app/config.py:150` | env/compose 100，`infra/env/midterm.env:131`、`infra/docker-compose.midterm.yml:916`；只有 `CLIP_WORKER_RUN_ONCE=true` 才生效，见 `services/clip-worker/app/worker.py:2931-2937`。 |
| `CLIP_WORKER_RUN_ONCE` | false，`services/clip-worker/app/config.py:151-152` | env/compose false，`infra/env/midterm.env:132`、`infra/docker-compose.midterm.yml:917`。 |
| `CLIP_WORKER_MAX_CONCURRENT_JOBS` | 0，`services/clip-worker/app/config.py:153-155` | env/compose 8，`infra/env/midterm.env:134`、`infra/docker-compose.midterm.yml:918`；实际 gate 用 `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY`，见 `services/clip-worker/app/worker.py:3024-3035`。 |
| `CLIP_WORKER_PENDING_CLAIM_MIN_IDLE_MS` | 5000，`services/clip-worker/app/config.py:156-158` | env/compose 5000，`infra/env/midterm.env:135`、`infra/docker-compose.midterm.yml:936`。 |
| `CLIP_WORKER_PENDING_CLAIM_COUNT` | 10，`services/clip-worker/app/config.py:159` | env/compose 10，`infra/env/midterm.env:136`、`infra/docker-compose.midterm.yml:937`。 |
| `CLIP_WORKER_PENDING_CLAIM_INTERVAL_S` | 5，`services/clip-worker/app/config.py:160-162` | env/compose 5，`infra/env/midterm.env:137`、`infra/docker-compose.midterm.yml:938`。 |
| `CLIP_WORKER_DEFERRED_RETRY_MAX_ATTEMPTS` | 12，`services/clip-worker/app/config.py:163-165` | env/compose 12，`infra/env/midterm.env:138`、`infra/docker-compose.midterm.yml:939`。 |
| `CLIP_WORKER_PER_CAMERA_COOLDOWN_SECONDS` | 0，`services/clip-worker/app/config.py:166-168` | env/compose 0，`infra/env/midterm.env:139`、`infra/docker-compose.midterm.yml:940`。 |
| `REPLAY_STOP_CONDITION_MODE` | `frame_count`，`services/clip-worker/app/config.py:169-171` | compose `ts_delta_sec`，`infra/docker-compose.midterm.yml:941`。 |
| `REPLAY_FPS` | 30，`services/clip-worker/app/config.py:172` | compose default 24，`infra/docker-compose.midterm.yml:942`。 |
| `REPLAY_DURATION_EXTRA_SLACK_S` | 0，`services/clip-worker/app/config.py:173-175` | compose default 5，`infra/docker-compose.midterm.yml:943`。 |
| `REPLAY_ANCHOR_STRATEGY` | `request_keyframe`，`services/clip-worker/app/config.py:176-178` | compose `event_keyframe`，`infra/docker-compose.midterm.yml:946`。 |
| `ALLOW_UNBOUNDED_KEYFRAME_FALLBACK` | false，`services/clip-worker/app/config.py:179-181` | compose 固定 false，`infra/docker-compose.midterm.yml:967`。 |
| `KEYFRAME_LOOKUP_RETRIES` / `KEYFRAME_LOOKUP_RETRY_SLEEP_S` | 0 / 1.0，`services/clip-worker/app/config.py:182-185` | compose 8 / 1.0，`infra/docker-compose.midterm.yml:947-948`。 |
| `POST_SAVANT_FRAME_PROOF_ATTEMPTS` | 1，`services/clip-worker/app/config.py:186-188` | env/compose 1，`infra/env/midterm.env:47`、`infra/docker-compose.midterm.yml:949`。 |
| `POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S` | fallback `KEYFRAME_LOOKUP_RETRY_SLEEP_S` 或 1.0，`services/clip-worker/app/config.py:189-194` | env/compose 1.0，`infra/env/midterm.env:49`、`infra/docker-compose.midterm.yml:950`。 |
| `POST_SAVANT_FRAME_PROOF_WAIT_BUDGET_S` | 12，`services/clip-worker/app/config.py:195-197` | env/compose 3，`infra/env/midterm.env:50`、`infra/docker-compose.midterm.yml:951`。 |
| `POST_SAVANT_FRAME_PROOF_POLL_INTERVAL_S` | fallback retry sleep / 0.5，`services/clip-worker/app/config.py:198-206` | env/compose 0.5，`infra/env/midterm.env:51`、`infra/docker-compose.midterm.yml:952`。 |
| `POST_SAVANT_FRAME_PROOF_FAST_PATH_BATCH_SIZE/LAG/PENDING` | 0/0/0，`services/clip-worker/app/config.py:207-218` | env/compose 0/0/0，`infra/env/midterm.env:52-54`、`infra/docker-compose.midterm.yml:953-955`。 |
| `CLIP_WORKER_FRAME_ANNOTATION_LOOKUP_CONCURRENCY` | 8，`services/clip-worker/app/config.py:219-222` | env/compose 8，`infra/env/midterm.env:55`、`infra/docker-compose.midterm.yml:956`。 |
| `CLIP_WORKER_FRAME_ANNOTATION_RANGE_CACHE_TTL_S/BUCKET_MS/MAX_ENTRIES` | 0.75 / 1000 / 64，`services/clip-worker/app/config.py:223-234` | env/compose 同值，`infra/env/midterm.env:56-58`、`infra/docker-compose.midterm.yml:957-959`。 |
| `POST_SAVANT_ALLOW_CROSS_SESSION_POST_WINDOW_PROOF` | true，`services/clip-worker/app/config.py:235-240` | env/compose true，`infra/env/midterm.env:59`、`infra/docker-compose.midterm.yml:960`。 |
| `POST_SAVANT_ALLOW_TRUNCATED_PRE_WINDOW_PROOF` | true，`services/clip-worker/app/config.py:241-246` | env/compose true，`infra/env/midterm.env:60`、`infra/docker-compose.midterm.yml:961`。 |
| `FRAME_ANNOTATION_STREAM` | `security.frame_annotations`，`services/clip-worker/app/config.py:247-249` | env/compose 同值，`infra/env/midterm.env:31`、`infra/docker-compose.midterm.yml:962`。 |
| `FRAME_ANNOTATION_ANCHOR_LOOKBACK_COUNT` / `PAGE_COUNT` | 20000 / 2000，`services/clip-worker/app/config.py:250-255` | env/compose 同值，`infra/env/midterm.env:61-62`、`infra/docker-compose.midterm.yml:963-964`。 |
| `FRAME_ANNOTATION_ANCHOR_WALL_CLOCK_SLACK_S` / `PTS_TOLERANCE_S` | 1.0 / 1.0，`services/clip-worker/app/config.py:256-260` | compose default 1.0/1.0，`infra/docker-compose.midterm.yml:965-966`。 |
| `EVIDENCE_MATERIALIZATION_POLICY` | `priority`，`services/clip-worker/app/config.py:262-264` | compose default `priority`，`infra/docker-compose.midterm.yml:919`。 |
| `EVIDENCE_HIGH_PRIORITY_EVENT_TYPES` | `watchlist_hit,live_search_hit`，`services/clip-worker/app/config.py:265-268` | env/compose 同值，`infra/env/midterm.env:108`、`infra/docker-compose.midterm.yml:920`。 |
| `EVIDENCE_MATERIALIZATION_DEFER_LOW_PRIORITY` | false，`services/clip-worker/app/config.py:269-271` | env/compose false，`infra/env/midterm.env:109`、`infra/docker-compose.midterm.yml:921`。 |
| `EVIDENCE_REPLAY_TTL_SECONDS` | 300，`services/clip-worker/app/config.py:272-274` | env/compose 300，`infra/env/midterm.env:110`、`infra/docker-compose.midterm.yml:922`。 |
| `EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS` | 120，`services/clip-worker/app/config.py:275-277` | env/compose 600，`infra/env/midterm.env:111`、`infra/docker-compose.midterm.yml:923`。 |
| `EVIDENCE_UNKNOWN_SOURCE_FAIL_CLOSED` | true，`services/clip-worker/app/config.py:278-280` | env/compose true，`infra/env/midterm.env:112`、`infra/docker-compose.midterm.yml:924`。 |
| `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY` | env value, fallback `CLIP_WORKER_MAX_CONCURRENT_JOBS`, fallback 4，`services/clip-worker/app/config.py:281-286` | env/compose 36，`infra/env/midterm.env:116`、`infra/docker-compose.midterm.yml:925`。 |
| `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SHARD` | 2，`services/clip-worker/app/config.py:287-289` | env/compose 9，`infra/env/midterm.env:117`、`infra/docker-compose.midterm.yml:926`。 |
| `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY_PER_SOURCE` | 1，`services/clip-worker/app/config.py:290-292` | env/compose 1，`infra/env/midterm.env:118`、`infra/docker-compose.midterm.yml:927`。 |
| `EVIDENCE_REPLAY_ACTIVE_SLOT_EXTRA_SECONDS` | 5，`services/clip-worker/app/config.py:293-296` | env/compose 0，`infra/env/midterm.env:119`、`infra/docker-compose.midterm.yml:928`。 |
| `MEDIA_POLL_INTERVAL_S` / `MIDTERM_SINK_STABILITY_CHECKS` | 5 / 2，`services/clip-worker/app/config.py:297-304` | env/compose 2 / 2，`infra/env/midterm.env:125-126`、`infra/docker-compose.midterm.yml:929-930`。 |
| `EVIDENCE_REPLAY_SLOT_SINK_STABILITY_BUDGET_S` | 60，`services/clip-worker/app/config.py:305-308` | env/compose 60，`infra/env/midterm.env:120`、`infra/docker-compose.midterm.yml:931`。 |
| `EVIDENCE_REPLAY_SLOT_FINALIZER_BUDGET_S` | 0，`services/clip-worker/app/config.py:309-312` | env/compose 0，`infra/env/midterm.env:121`、`infra/docker-compose.midterm.yml:932`。 |
| `EVIDENCE_REPLAY_SLOT_GRACE_S` | fallback `EVIDENCE_REPLAY_ACTIVE_SLOT_EXTRA_SECONDS`/5，`services/clip-worker/app/config.py:313-320` | env/compose 5，`infra/env/midterm.env:122`、`infra/docker-compose.midterm.yml:933`。 |
| `EVIDENCE_MATERIALIZATION_EVENT_TYPE_QUOTAS` | 空 map，`services/clip-worker/app/config.py:322-324` | env/compose 空，`infra/env/midterm.env:123`、`infra/docker-compose.midterm.yml:934`。 |
| `EVIDENCE_MATERIALIZATION_PRESSURE_LEVEL` | `normal`，`services/clip-worker/app/config.py:325-330` | env/compose normal，`infra/env/midterm.env:124`、`infra/docker-compose.midterm.yml:935`。 |

`ReplayClient` 额外直接读取：

| 配置 | 代码默认值 | 当前 midterm 覆盖/说明 |
| --- | --- | --- |
| `REPLAY_TS_SYNC` | false，`services/clip-worker/app/replay_client.py:164-166`、`services/clip-worker/app/replay_client.py:455-457` | env/compose false，`infra/env/midterm.env:227`、`infra/docker-compose.midterm.yml:945`。 |
| `REPLAY_FORCE_CONSTANT_CADENCE` | true，`services/clip-worker/app/replay_client.py:177`、`services/clip-worker/app/replay_client.py:450-454` | env/compose true，`infra/env/midterm.env:226`、`infra/docker-compose.midterm.yml:944`。 |

硬编码/配置化不足的参数：

- `XREADGROUP count=10` 写死在 `services/clip-worker/app/worker.py:3227-3230`。
- materialization deadline sweep 间隔 30 秒写死在 `services/clip-worker/app/worker.py:3195-3203`。
- summary log 间隔 60 秒写死在 `services/clip-worker/app/worker.py:4248-4262`。
- Redis socket connect timeout 5 秒、health check 30 秒写死在 `services/clip-worker/app/worker.py:3103-3110`。
- Replay `anchor_wait_duration` 1 秒写死在 `services/clip-worker/app/replay_client.py:489-490`。
- Replay sink options 5 秒 retry timeout、HWM 10000、inflight 100 写死在 `services/clip-worker/app/replay_client.py:376-384`。

## 6. 错误处理

异常处理清点：

- 子进程崩溃：`_run_consumer()` 打 exception，调用 `request_shutdown()` 并 re-raise，父进程会 terminate/kill 其它子进程，见 `services/clip-worker/main.py:27-50`、`services/clip-worker/main.py:89-117`。
- Redis group 创建：非 `BUSYGROUP` 重新抛出，见 `services/clip-worker/app/worker.py:3093-3098`。
- Redis 连接：`connect_redis()` 会 `ping()`，失败会抛出到子进程 crash 路径，见 `services/clip-worker/app/worker.py:3101-3117`。
- request JSON 解析失败：`_parse_request()` 返回 None，主循环直接 `XACK`，没有 dead-letter，见 `services/clip-worker/app/worker.py:3083-3090`、`services/clip-worker/app/worker.py:3240-3244`。
- pending inspect/claim 失败：打 warning 后返回空 pending，主循环继续读新消息，见 `services/clip-worker/app/worker.py:1368-1378`、`services/clip-worker/app/worker.py:1436-1444`、`services/clip-worker/app/worker.py:1482-1491`。
- `_redis_stream_group_diagnostics()` 对 `XPENDING` 和 `XINFO GROUPS` 异常完全 `pass`，没有日志和指标，见 `services/clip-worker/app/worker.py:1512-1529`。
- `ReplayClient.status()`、`find_keyframe()`、`create_job()` 一般异常都记录日志后返回 None，见 `services/clip-worker/app/replay_client.py:37-48`、`services/clip-worker/app/replay_client.py:129-134`、`services/clip-worker/app/replay_client.py:301-307`。调用侧会把 None 当作缺 keyframe 或创建失败推进状态。
- `ReplayClient.create_job()` 对 HTTP 4xx/5xx 走 fallback payload；fallback 仍失败后返回 None，见 `services/clip-worker/app/replay_client.py:184-300`。这避免直接 crash，但错误只落在 log/failed status。
- repository 函数普遍 `except Exception` 后返回 `0/None/False/{}`，见 `services/clip-worker/app/repository.py:208-210`、`services/clip-worker/app/repository.py:246-252`、`services/clip-worker/app/repository.py:500-502`、`services/clip-worker/app/repository.py:668-670`、`services/clip-worker/app/repository.py:994-996`、`services/clip-worker/app/repository.py:1344-1346`。
- `active_replay_slot_counts()` 失败会回退到进程本地 `active_jobs` 计数，见 `services/clip-worker/app/worker.py:3378-3390`。多进程下本地 fallback 看不到其它进程，可能短时低估 active slot；后续 atomic admission 仍会再次判定。
- `try_acquire_replay_slot()` 返回 None 时 `_queue_clip_request()` 不 ACK，依赖 pending reclaim 重试，见 `services/clip-worker/app/worker.py:3928-3959`。
- Replay job create 抛异常时会写 failed、release active slot、ACK，见 `services/clip-worker/app/worker.py:4053-4085`；`create_job()` 返回 None 时也 release slot 并 ACK，见 `services/clip-worker/app/worker.py:4220-4245`。
- 主循环兜底只打 exception 后 sleep 1 秒，没有重新创建 Redis/Postgres 连接，见 `services/clip-worker/app/worker.py:4271-4273`。如果连接对象进入坏状态，可能持续打日志和 sleep。

可能导致卡住或信号不足的路径：

- `_defer_clip_request()`、`_defer_post_savant_frame_proof()`、`_queue_clip_request()` 的非终态路径不 ACK，依赖 pending reclaim；如果 `CLIP_WORKER_PENDING_CLAIM_COUNT=0` 或 Redis claim 长期失败，message 会留在 PEL，见 `services/clip-worker/app/worker.py:1600-1675`、`services/clip-worker/app/worker.py:1717-1833`。
- `update_clip_status()` 失败返回 False，但多数调用方不检查返回值，仍可能 ACK message，例如成功创建 Replay job 后 `update_clip_status()` 返回值未被使用，见 `services/clip-worker/app/worker.py:4174-4245`。
- `_redis_stream_group_diagnostics()` 静默失败会让 fast-path proof wait 判断缺少 pending/lag 信号，见 `services/clip-worker/app/worker.py:3529-3559`。

## 7. 测试覆盖

已发现的直接覆盖：

- `harness/tests/test_clip_worker_queue_safety.py` 覆盖 queue/defer 不永久 skip、proof wait 不消耗 proof reads、fast path、terminal/stale pending ACK、shard routing/mismatch、unknown source fail、retry budget、waiting_proof diagnostics、single-shot reclaimed proof 等，典型用例见 `harness/tests/test_clip_worker_queue_safety.py:629-699`、`harness/tests/test_clip_worker_queue_safety.py:769-951`、`harness/tests/test_clip_worker_queue_safety.py:1107-1268`。
- `harness/tests/test_completion_aware_replay_admission.py` 覆盖 worker 调用 DB-backed slot、atomic admission denied 不创建 Replay job、terminal slot ACK 不 queue、create exception release slot、repository SQL 结构和 active predicate，见 `harness/tests/test_completion_aware_replay_admission.py:70-132`、`harness/tests/test_completion_aware_replay_admission.py:201-258`、`harness/tests/test_completion_aware_replay_admission.py:458-610`。
- `harness/tests/test_evidence_materialization_phase2plus.py` 覆盖 `_clip_gate_decision()` 的 per-shard/source quota、event type quota、pressure degrade，以及 deadline sweep 中 rolling/coverage 保护的 SQL 静态断言，见 `harness/tests/test_evidence_materialization_phase2plus.py:366-471`。
- `harness/tests/test_midterm_replay_cadence_payload.py` 覆盖 `REPLAY_TS_SYNC` 默认 false、显式 true 可 opt-in、constant cadence fallback 与 frame_count fallback，见 `harness/tests/test_midterm_replay_cadence_payload.py:53-70`、`harness/tests/test_midterm_replay_cadence_payload.py:73-134`。
- `harness/tests/test_midterm_replay_epoch_isolation.py` 覆盖 Replay labels/`runtime_epoch_id`、delivery duration、runtime_epoch filter 和 proof wait 独立配置，文件中的 clip-worker 相关测试函数见 `harness/tests/test_midterm_replay_epoch_isolation.py:90-233`。
- `harness/tests/test_midterm_stream_session_isolation.py` 覆盖 record_request/Replay labels 携带 stream session、frame annotation lookup 过滤 session、post-Savant request 要求 session，见 `harness/tests/test_midterm_stream_session_isolation.py:59-170`。
- `harness/tests/test_midterm_deployment_contract.py` 覆盖 clip-worker shard env、retention、`REPLAY_TS_SYNC=false`、constant cadence 与 queue safety 默认值，见 `harness/tests/test_midterm_deployment_contract.py:371-377`、`harness/tests/test_midterm_deployment_contract.py:773-877`。

明显缺口：

- 未发现真实 PostgreSQL 多进程并发测试去证明 `try_acquire_replay_slot()` 在真实隔离级别下不会超发；现有测试主要是 fake cursor 和 SQL 字符串断言。
- 未发现 `update_clip_status()` 不能覆盖 terminal/materialized/epoch-superseded task 的回归测试；当前代码也没有 CAS predicate。
- 未发现 Redis 真实 consumer group/Pending Entries List 长时间卡住、`XAUTOCLAIM` 不可用、Redis 连接断开后恢复的集成测试。
- 未发现 `update_clip_status()` 失败但 Replay job 已创建、随后 message 被 ACK 时的一致性测试。
- 未发现对 `CLIP_WORKER_MAX_CONCURRENT_JOBS` 与 `EVIDENCE_MATERIALIZATION_MAX_CONCURRENCY` 同时设置不同值时日志/行为一致性的测试。
- Replay API fallback 测试使用 monkeypatch `_submit_job_payload`，未发现真实 Replay service HTTP 400/timeout 的端到端测试。

## 8. 代码质量

超长函数：

| 函数 | 行号 | 主要风险 |
| --- | --- | --- |
| `run_worker` | `services/clip-worker/app/worker.py:3138-4275` | 1138 行，混合 Redis 消费、状态机、proof wait、admission、Replay job、ACK，局部变量和分支过多，难以保证所有 ACK/不 ACK 分支一致。 |
| `_prepare_post_savant_replay_request` | `services/clip-worker/app/worker.py:2452-2853` | 402 行，proof lookup、keyframe lookup、PTS 验证、sleep/wait 和 diagnostics 混在一起。 |
| `try_acquire_replay_slot` | `services/clip-worker/app/repository.py:276-582` | 307 行，SQL 正确性关键但难读；建议保留单 SQL，但拆出 result/diagnostic mapping。 |
| `update_clip_status` | `services/clip-worker/app/repository.py:1159-1347` | 189 行，同时更新 events 与 evidence_tasks，且没有状态 CAS。 |
| `_apply_replay_anchor_to_request` | `services/clip-worker/app/worker.py:2228-2414` | 187 行，PTS/offset/duration/label 写入集中，适合拆成纯计算和 label enrichment。 |
| `_find_replay_frame_domain_proofs` | `services/clip-worker/app/worker.py:2049-2226` | 178 行，start/post/cross-session/truncated 多策略混合。 |
| `ReplayClient.create_job` | `services/clip-worker/app/replay_client.py:136-307` | 172 行，primary/fallback payload 与 HTTP 错误处理交织。 |
| `acquire_replay_slot` | `services/clip-worker/app/repository.py:673-808` | 136 行旧路径，当前 worker 不用，但仍是非 CAS active slot writer。 |
| `_derive_start_window_frame_from_keyframe_reference` | `services/clip-worker/app/worker.py:1835-1940` | 106 行，frame annotation 反查逻辑与主 proof flow 重复。 |
| `_derive_truncated_start_window_frame` | `services/clip-worker/app/worker.py:1941-2047` | 107 行，与 start-window lookup 逻辑相近。 |

重复/分散逻辑：

- evidence state/status 映射在 clip-worker、event-worker、media-worker/API 中各自维护；clip-worker 的枚举见 `services/clip-worker/app/repository.py:13-89`。后续全局报告应和 `module_services_event_worker.md` 中的状态映射重复问题合并。
- env parsing helpers 在 clip-worker 内部重复实现 `_csv_env/_bool_env/_event_type_quotas_env`，见 `services/clip-worker/app/config.py:79-115`；event-worker repository 也有独立 env helper，见 `docs/code_review/module_services_event_worker.md`。
- Redis consumer/pending/ACK 逻辑是 clip-worker 自己实现；event-worker 有独立 `redis_consumer.py` 封装。两者错误处理语义不完全一致。
- `events.payload.media` JSONB 拼接 SQL 在 `update_clip_status()`、`release_*()`、`record_replay_job_for_slot()` 多处重复。

死代码/历史代码：

- `acquire_replay_slot()` 当前 active worker 未调用；`rg` 只发现测试和函数定义，见 `services/clip-worker/app/worker.py:23-33` 的 imports 只导入 `try_acquire_replay_slot/record_replay_job_for_slot/release_replay_slot`。
- `update_evidence_media_result()` 当前 active code 未调用；`rg` 只发现 `services/clip-worker/archive/phase-only/20260610/evidence_media_service.py` 和函数定义。

魔法数字：

- `XREADGROUP count=10`：`services/clip-worker/app/worker.py:3227-3230`。
- deadline sweep 30 秒：`services/clip-worker/app/worker.py:3195-3203`。
- summary log 60 秒：`services/clip-worker/app/worker.py:4248-4262`。
- `_frame_annotation_stream_bounds()` 对 post window fallback 直接用 `60_000` ms，对 end slack 至少 15 秒，见 `services/clip-worker/app/worker.py:535-541`。
- Replay sink retry/hwm/inflight 固定值：`services/clip-worker/app/replay_client.py:376-384`。
- Replay delivery watchdog最小 30 秒、额外 10 秒：`services/clip-worker/app/replay_client.py:386-402`。
- Replay anchor wait 1 秒：`services/clip-worker/app/replay_client.py:489-490`。

TODO/FIXME/HACK：`rg -n "TODO|FIXME|HACK" services/clip-worker` 未发现 active clip-worker 目录中的对应注释。

其它质量风险：

- `seen_requests` 是每个进程内的 set，主循环长期运行会持续增长；多进程之间也不共享，只能做本进程内去重，见 `services/clip-worker/app/worker.py:3150`、`services/clip-worker/app/worker.py:3246-3255`。
- startup log 打印 `max_concurrent_jobs=cfg.max_concurrent_jobs`，见 `services/clip-worker/app/worker.py:3154-3174`；真实 gate 使用 `cfg.evidence_materialization_max_concurrency`，见 `services/clip-worker/app/worker.py:3024-3035`，排查时容易误读。

## 9. 与现有文档的一致性

- `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md` 记录 `REPLAY_TS_SYNC=false` 已落地，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:86-88`。代码默认 false，compose/env 也为 false，见 `services/clip-worker/app/replay_client.py:164-166`、`services/clip-worker/app/replay_client.py:455-457`、`infra/env/midterm.env:226-227`、`infra/docker-compose.midterm.yml:944-945`。一致。
- 同一文档早期段落仍描述“clip-worker 单主循环逐条 proof wait”，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:117-124`。当前代码已经通过 `main.py` 多进程启动 8 consumer，见 `services/clip-worker/main.py:62-87`；文档后续 `14.2` 已更新为多进程修正，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:1139-1174`。结论：文档内部有历史段落，当前代码与后续修正段一致。
- 文档说 terminal slot 不再重新 queue，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:1110-1117`。代码在 `try_acquire_replay_slot()` 和 `run_worker()` 均实现，见 `services/clip-worker/app/repository.py:370-375`、`services/clip-worker/app/repository.py:446-449`、`services/clip-worker/app/worker.py:3971-3984`。一致。
- 文档说 shard mapping mismatch diagnostics/labels 已加入，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:1314-1331`。代码在 route diagnostics、phase diagnostics 和 Replay labels 中写入 mapping fields，见 `services/clip-worker/app/worker.py:3348-3407`、`services/clip-worker/app/worker.py:2856-2922`。一致。
- 文档与当前仓库默认 shard 配置存在差距：`docs/project_current_progress_summary.md` 称最终采纳 4 shard + global limit 36，见 `docs/project_current_progress_summary.md:258-278`；代码/env/compose 的 global limit 确认为 36，见 `infra/env/midterm.env:116`、`infra/docker-compose.midterm.yml:925`。但 `infra/config/replay-shards.midterm.json` 当前只列 `replay-a/replay-b` 两个 shard，见 `infra/config/replay-shards.midterm.json:1-80`，且 compose 默认 `REPLAY_SHARDS_CONFIG_PATH` 为空，见 `infra/docker-compose.midterm.yml:907-908`。这说明“4 shard”依赖运行时 `REPLAY_SHARDS_JSON` 或其它启动流程注入；从仓库默认文件本身不能证明 4 shard 已默认生效，需要 infra/runtime_apply 报告继续核对。
- `specs/30_midterm_rolling_cache_evidence_plan.md` 说 clip-worker 的 deadline sweep 不再杀 rolling-cache claimed task 和 coverage parent，见 `specs/30_midterm_rolling_cache_evidence_plan.md:417-425`。代码在 `expire_materialization_deadlines()` 中排除 rolling_cache materializing，并排除 `event_id` 或 `bundle_event_id` 被 covered-by 依赖的任务，见 `services/clip-worker/app/repository.py:950-968`。一致。
- `docs/current_mainline_status.md` 把 clip-worker 明确放在 fallback path，而 accepted path 是 rolling-cache，见 `docs/current_mainline_status.md:115-125`。当前 clip-worker 只消费 `record_requests` 并调 Replay，不参与 rolling-cache copy/materialization。与文档一致。
- `ready_at/not_before` 文档说自然等待从 worker 占用中剥离，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:2148-2162`、`docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:2637-2650`。该语义主要由 event-worker/media-worker 实现；clip-worker 只负责旧 Replay fallback 的 Redis pending/proof wait，仍按 `materialization_deadline_at` 做 TTL sweep，见 `services/clip-worker/app/repository.py:916-996`。本模块无法单独证明 ready_at 语义完整，需在 media-worker 报告继续核对。

## 10. 已知问题回归检查

- `REPLAY_TS_SYNC=false` 是否仍然默认生效：已确认。`ReplayClient.create_job()` 在没有显式 `ts_sync` 参数时用 `_env_bool("REPLAY_TS_SYNC", False)`，`build_job_payload()` 同样默认 false，见 `services/clip-worker/app/replay_client.py:164-166`、`services/clip-worker/app/replay_client.py:455-457`；midterm env/compose 也为 false，见 `infra/env/midterm.env:226-227`、`infra/docker-compose.midterm.yml:944-945`；测试覆盖见 `harness/tests/test_midterm_replay_cadence_payload.py:53-70`。存在显式 opt-in 路径 `ts_sync=True`，但 `run_worker()` 调 `create_job()` 未传该参数，见 `services/clip-worker/app/worker.py:4023-4052`。
- clip-worker proof wait 是否从单循环同步阻塞改成并发/分片：部分完成。进程级别已从单进程扩为 `CLIP_WORKER_CONSUMER_COUNT=8` 的多进程，见 `services/clip-worker/main.py:62-87`、`infra/env/midterm.env:133`；但每个 consumer 进程内仍是顺序循环，`_prepare_post_savant_replay_request()` 会在循环内 `time.sleep()` 等 proof，见 `services/clip-worker/app/worker.py:2843-2851`。因此不是进程内 worker pool；并发来自多进程 consumer。
- proof wait / queued 是否不消耗 Replay concurrency slot：已确认。waiting/queued 写状态不调用 slot admission，见 `services/clip-worker/app/worker.py:1717-1833`；slot 只在 proof/keyframe 准备后调用 `try_acquire_replay_slot()`，见 `services/clip-worker/app/worker.py:3906-3927`。测试 `test_post_savant_concurrency_queue_does_not_wait_for_proof` 也覆盖这一点，见 `harness/tests/test_clip_worker_queue_safety.py:663-699`。
- `evidence_tasks` slot/状态更新是否都走 CAS：未全部满足。slot admission/release/job attach 已有 active/terminal predicate，见 `services/clip-worker/app/repository.py:330-496`、`services/clip-worker/app/repository.py:607-638`、`services/clip-worker/app/repository.py:834-870`；但 `update_clip_status()` 和历史 `update_evidence_media_result()` 仍按 event/task 直接覆盖状态，见 `services/clip-worker/app/repository.py:1257-1318`、`services/clip-worker/app/repository.py:1425-1441`。这是本模块最明确的回归风险点。
- `runtime_epoch_id` 是否是一等字段并建索引、epoch barrier 是否真实存在：clip-worker 侧只负责携带和过滤，不负责 barrier。迁移 024 已将 `evidence_tasks.runtime_epoch_id` 提升为字段并建 active index，见 `db/migrations/024_evidence_task_runtime_epoch_barrier.sql:7-44`；clip-worker Replay labels 保留 `runtime_epoch_id`，见 `services/clip-worker/app/worker.py:2856-2922`，frame annotation lookup 按 epoch 过滤，见 `services/clip-worker/app/worker.py:744-760`。epoch barrier 实现在 API/runtime_apply，不在本模块；需要在 `services/api` 报告中继续核对。
- rolling-cache segment 边界是否严格对齐 GOP/keyframe：本模块不适用。clip-worker 的旧 Replay path 会用 frame annotation/keyframe proof 选择 Replay anchor，见 `services/clip-worker/app/worker.py:2049-2414`；rolling-cache segment/GOP 对齐属于 `modules/savant_security`、`modules/savant_replay`、`media-worker/rolling_cache.py` 范围。
- `materialization_ready_at/not_before` 是否把自然等待和真实处理 worker 占用分开，deadline 起点是否从 ready/claim 起算：本模块只间接相关。迁移 025/026/027 提供 ready_at 字段和索引，见 `db/migrations/025_evidence_task_materialization_ready_at.sql:1-19`、`db/migrations/026_evidence_task_ready_claim_order_idx.sql:1-14`、`db/migrations/027_evidence_task_ready_indexes_terminal_deferred.sql:1-24`；clip-worker 不 claim rolling-cache ready tasks，只做 Replay fallback 与 deadline sweep。它的 `expire_materialization_deadlines()` 仍按 `materialization_deadline_at <= now()` 扫描，见 `services/clip-worker/app/repository.py:937-949`。deadline 起点是否从 media-worker claim 后重置，需要在 `services/media-worker` 报告核对。
- media-worker finalizer claim 是否仍存在“claim 了不存在任务却返回 claimed=true”：本模块不适用。clip-worker 只提供 `release_replay_slot()` 被 media-worker 调用，见 `services/clip-worker/app/repository.py:810-913`；finalizer claim 逻辑在 `services/media-worker/app/worker.py`。

## 11. 风险与建议

1. 高优先级：`update_clip_status()` 非 CAS 可能覆盖终态/epoch barrier 结果。影响是旧 pending Redis message 或 late Replay result 可能把已 `materialized/materialization_expired/epoch_superseded_incomplete` 的 task 写回 pending/replay_job_created。建议把 task 状态写入改为显式 transition SQL，至少加 `WHERE materialization_status IN (...)` 和 terminal-state 排除，并为 terminal overwrite 加回归测试。

2. 高优先级：仓库默认 Replay shard 文件与“4 shard + global 36”文档不完全一致。影响是未通过 pressure/runtime_apply 注入 `REPLAY_SHARDS_JSON` 的启动方式可能落回 implicit single shard 或现有 2 shard 文件，吞吐与文档验收不一致。建议在 infra 报告中继续核对 day-to-day 启动路径，并让 compose 默认指向权威 shard config 或在 doctor 中强制诊断 effective shard count。

3. 中高优先级：queued/waiting_proof 依赖 Redis PEL reclaim，缺少独立 retry scheduler 和强监控。影响是 pending claim 配置错误、Redis claim 异常或 consumer 长期坏连接时，任务可停在 waiting/queued 而没有 ACK 或 dead-letter。建议增加 pending age/claim failure 指标、doctor 检查和超过 TTL 的显式 DB/Redis reconciler。

4. 中优先级：`run_worker()` 过长且每进程顺序 proof wait。影响是新增状态分支时容易漏 ACK/漏 release slot；单个 consumer 仍会被慢 proof wait 卡住。建议拆成纯状态机函数、Replay admission 函数、ACK policy 表，并评估 bounded worker pool 或 shard-local queue，避免在主消费循环中直接 sleep。

5. 中优先级：旧 repository 写函数仍保留。`acquire_replay_slot()` 和 `update_evidence_media_result()` 当前不在 active worker 路径，但都是非 CAS 写状态函数。建议删除或迁入 archive；若必须保留，加入 terminal predicate 并标注 deprecated，避免未来误用。
