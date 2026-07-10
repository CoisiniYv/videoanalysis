# Phase 2 Module Review: services/event-worker

审查范围：`services/event-worker/`，并交叉核对当前 midterm compose/env、相关迁移、测试与文档。本文只做报告，不修改代码。

## 1. 模块职责

`services/event-worker` 是检测事件进入证据链路后的第一个持久化与分流服务。上游主要是 Savant/face-worker 写入 Redis stream `security.events`，另有 `security.person_observations` 人体框观测流；本模块把事件写入 PostgreSQL `events`，按证据策略创建 `evidence_tasks`，执行 alert cooldown、record_request admission/cooldown/去重，并在 Replay fallback 路径中向 `security.record_requests` 发布录制请求。入口在 `services/event-worker/main.py:28-54`，主循环在 `services/event-worker/app/worker.py:754-971`。

下游分两条路径：

- rolling-cache 路径：event-worker 只创建 `evidence_tasks`，在 `ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=true` 时不发布 per-event Replay 请求，代码路径见 `services/event-worker/app/worker.py:381-392`。
- Replay fallback 路径：event-worker 发布 `security.record_requests`，clip-worker/Replay/video-file-sink/media-worker 继续处理，发布点见 `services/event-worker/app/worker.py:481-485` 和 `services/event-worker/app/record_request.py:354-409`。

## 2. 关键文件与角色

| 文件 | 行数 | 角色 |
| --- | ---: | --- |
| `services/event-worker/main.py` | 58 | 进程入口，安装 SIGTERM/SIGINT 处理，加载配置，连接 Redis/PostgreSQL 并调用 `run_worker`，见 `services/event-worker/main.py:28-54`。 |
| `services/event-worker/app/config.py` | 115 | 环境变量加载与 `Config` dataclass。字段清单在 `services/event-worker/app/config.py:9-37`，默认值读取在 `services/event-worker/app/config.py:45-115`。 |
| `services/event-worker/app/worker.py` | 971 | 核心消费循环、事件解析、证据策略填充、runtime epoch 注入、alert/record_request/evidence_task 编排。超过 500 行，主要函数见下表。 |
| `services/event-worker/app/repository.py` | 1399 | PostgreSQL repository，负责 `events`、`person_bbox_observations`、`evidence_tasks`、`evidence_event_links` 写入和状态同步。超过 500 行，主要函数见下表。 |
| `services/event-worker/app/record_request.py` | 420 | 构造并发布 Replay 录制请求，包含 shard/runtime_epoch/post-Savant PTS metadata 透传，以及 Redis SET NX 去重。 |
| `services/event-worker/app/redis_consumer.py` | 148 | Redis Stream consumer group 封装，支持 `xreadgroup`、pending reclaim、`xack`。 |
| `services/event-worker/app/alert_policy.py` | 234 | 摄像头 alert cooldown 策略，支持 global/event_type/algorithm scope，并把 suppress 决策写回事件。 |
| `services/event-worker/app/alert_publisher.py` | 79 | 向 `security.alerts` 发布 alert。 |
| `services/event-worker/Dockerfile` | 15 | Python 3.12 slim 镜像，安装 requirements，复制 app/main 并执行 `python main.py`。 |
| `services/event-worker/requirements.txt` | 2 | 运行依赖 `redis>=5.0.0`、`psycopg[binary]>=3.0.0`。 |
| `services/event-worker/app/__init__.py` | 0 | 空包标记。 |

`worker.py` 主要函数/类：

| 符号 | 行号 | 说明 |
| --- | --- | --- |
| `RecordingPolicyState` | `services/event-worker/app/worker.py:51-55` | 进程内记录本轮已发布请求数与 cooldown 时间。 |
| `_parse_event` | `services/event-worker/app/worker.py:64-88` | 从 Redis entry 解析 `data` JSON 并校验 `source_event_id/event_type/camera_id`。 |
| `_parse_person_observation` | `services/event-worker/app/worker.py:91-134` | 解析人体框观测并过滤非 `accepted`。 |
| `_get_evidence_task_status` | `services/event-worker/app/worker.py:137-158` | 读取刚创建的 task 状态，失败时回退到初始状态策略。 |
| `_mark_recording_policy_skipped` | `services/event-worker/app/worker.py:161-184` | 把 record_request policy skip 写成 `materialization_skipped`。 |
| `_recording_cooldown_key` | `services/event-worker/app/worker.py:205-217` | 计算 record_request cooldown key。 |
| `_current_runtime_epoch_id` / `_apply_runtime_epoch` | `services/event-worker/app/worker.py:220-270` | 从 Redis runtime epoch key 读取当前 epoch，并写入 event/payload/media。 |
| `_handle_event` | `services/event-worker/app/worker.py:273-511` | 239 行超长核心函数：插入事件、alert policy、创建 evidence task、record_request gate、ACK。 |
| `_apply_default_evidence_policy` | `services/event-worker/app/worker.py:530-565` | 给 intrusion legacy 事件补默认证据策略。 |
| `_apply_recording_window` | `services/event-worker/app/worker.py:568-630` | 给 clip-required 事件补 `pre_seconds/post_seconds`。 |
| `_process_batch` | `services/event-worker/app/worker.py:633-687` | 顺序处理一批事件，解析失败直接 ACK。 |
| `_handle_person_observation` / `_process_person_observation_batch` | `services/event-worker/app/worker.py:690-735` | 写入人体框观测并 ACK。 |
| `run_worker` | `services/event-worker/app/worker.py:754-971` | 218 行超长主循环，按 person pending/new、event pending/new 的顺序轮询处理。 |

`repository.py` 主要函数/类：

| 符号 | 行号 | 说明 |
| --- | --- | --- |
| `_INSERT_SQL` / `_INSERT_PERSON_BBOX_OBSERVATION_SQL` | `services/event-worker/app/repository.py:15-110` | `events` 和 `person_bbox_observations` 的幂等插入 SQL。 |
| `EVIDENCE_TASK_STATUSES` / `_STATUS_TO_EVIDENCE_STATE` | `services/event-worker/app/repository.py:113-169` | evidence/task 状态枚举与 operator evidence_state 映射。 |
| env helper | `services/event-worker/app/repository.py:172-212` | repository 内部再次实现 env 解析。 |
| `_evidence_admission_decision` | `services/event-worker/app/repository.py:514-577` | 读取 admission 限额并用 count 判定是否允许创建可物化任务。 |
| `_materialization_ttl_metadata` | `services/event-worker/app/repository.py:591-604` | 按 event time 计算 replay/annotation/materialization 初始 deadline。 |
| `_materialization_ready_at` | `services/event-worker/app/repository.py:607-617` | 计算 `event_ts + post_seconds + segment_grace`。 |
| `_evidence_task_initial_status` | `services/event-worker/app/repository.py:648-664` | 根据 event_type/algorithm_type 选择 `materialization_pending/pending/not_implemented`。 |
| `EventRepository.insert_event` | `services/event-worker/app/repository.py:673-713` | 幂等插入 `events`。 |
| `EventRepository.create_evidence_task` | `services/event-worker/app/repository.py:741-943` | 203 行超长函数：coverage/admission/ready_at/runtime_epoch/audit 字段组装和 task upsert。 |
| `EventRepository.set_evidence_status` | `services/event-worker/app/repository.py:945-1060` | 116 行超长函数：同步 `events.payload.media` 与 `evidence_tasks` 状态。 |
| `get_last_unsuppressed_alert` | `services/event-worker/app/repository.py:1120-1179` | alert cooldown 查询。 |
| `mark_evidence_materialization_skipped` | `services/event-worker/app/repository.py:1273-1320` | 将任务标记为 `materialization_skipped`。 |
| `set_clip_status` | `services/event-worker/app/repository.py:1322-1399` | record_request 发布后把 clip/task 状态写成 pending 等。 |

## 3. 数据流

输入：

- Redis `security.events`：由 `EVENT_STREAM` 默认读取，配置默认见 `services/event-worker/app/config.py:48-50`，主循环读取 pending/new 见 `services/event-worker/app/worker.py:879-937`。
- Redis `security.person_observations`：默认开启，配置见 `services/event-worker/app/config.py:94-115`，主循环见 `services/event-worker/app/worker.py:833-877`。
- Redis runtime epoch key：默认 `video_analytics:midterm:runtime_epoch`，读取见 `services/event-worker/app/worker.py:220-239`。
- PostgreSQL `cameras.alert_policy`：alert policy 查询见 `services/event-worker/app/repository.py:1073-1094`。
- PostgreSQL `events/evidence_tasks`：用于去重、cooldown、clip status 和 admission 查询，典型查询见 `services/event-worker/app/repository.py:294-318`、`services/event-worker/app/repository.py:1120-1179`、`services/event-worker/app/repository.py:1246-1271`。

处理：

1. Redis entry -> `_parse_event` 校验 -> `_apply_default_evidence_policy` / `_apply_recording_window` / `_apply_runtime_epoch`，见 `services/event-worker/app/worker.py:301-303`。
2. `insert_event` 写入 `events`，`ON CONFLICT (source_event_id) DO NOTHING`，见 `services/event-worker/app/repository.py:15-73`。
3. 新事件执行 alert policy，必要时 `mark_event_suppressed` 写 `events.status='suppressed'`，见 `services/event-worker/app/worker.py:318-347` 和 `services/event-worker/app/repository.py:1206-1244`。
4. 需要证据的新事件调用 `create_evidence_task`，写入 `evidence_tasks`，见 `services/event-worker/app/worker.py:349-368` 和 `services/event-worker/app/repository.py:741-943`。
5. clip-required 事件进入 record_request gate，按 event_type/source/cooldown/duplicate/clip_status 决定跳过或发布，见 `services/event-worker/app/worker.py:370-500`。

输出：

- PostgreSQL `events`：插入、alert suppress、media/evidence 状态更新。
- PostgreSQL `person_bbox_observations`：人体框观测幂等插入，见 `services/event-worker/app/repository.py:83-110`、`services/event-worker/app/repository.py:715-739`。
- PostgreSQL `evidence_tasks`：创建、初始状态、runtime_epoch、ready_at、materialization audit，见 `services/event-worker/app/repository.py:816-900`。
- PostgreSQL `evidence_event_links`：coverage merge 打开时写 `covered_by` 链接，见 `services/event-worker/app/repository.py:390-423`、`services/event-worker/app/repository.py:902-917`。
- Redis `security.alerts`：alert publish，见 `services/event-worker/app/alert_publisher.py:30-79`。
- Redis `security.record_requests`：Replay fallback 录制请求，见 `services/event-worker/app/record_request.py:354-409`。
- 文件系统/API：本模块不直接读写证据文件，也不直接调用其他服务 HTTP API。

## 4. 并发与状态机模型

进程内模型：单进程单线程顺序循环。`run_worker` 每轮先处理 person pending/new，再处理 event pending/new，且 `_process_batch` 对 messages 做普通 `for` 顺序处理，见 `services/event-worker/app/worker.py:831-937` 和 `services/event-worker/app/worker.py:653-687`。没有线程池、asyncio 或多进程 worker。

跨进程模型：Redis consumer group 与 PostgreSQL 幂等写允许水平扩容，但当前 midterm compose 只配置一个 `event-worker` service/consumer name，见 `infra/docker-compose.midterm.yml:741-801`，其中 `CONSUMER_NAME=event-worker-midterm-1` 在 `infra/docker-compose.midterm.yml:785-786`。如果未来 scale 多个 event-worker，admission count 与状态更新需要重新评估并发安全。

状态转移，event-worker 负责的部分：

```text
Redis security.events
  -> events.status = new
  -> alert suppressed? events.status = suppressed
  -> evidence_tasks.status/materialization_status =
       materialization_pending | manifest_ready | pending | not_implemented
  -> admission denied: materialization_skipped
  -> coverage merge: materialization_deferred
  -> record_request published: pending
  -> recording policy skip: materialization_skipped
```

已确认原子/幂等点：

- `events` 插入使用 `ON CONFLICT (source_event_id) DO NOTHING`，见 `services/event-worker/app/repository.py:15-73`。
- `person_bbox_observations` 插入使用 `ON CONFLICT (source_observation_id) DO NOTHING`，见 `services/event-worker/app/repository.py:83-110`。
- `evidence_tasks` 创建使用 deterministic `task_id` 和 `ON CONFLICT (task_id) DO UPDATE SET updated_at = evidence_tasks.updated_at`，实际是幂等 no-op，不做状态覆盖，见 `services/event-worker/app/repository.py:752-760`、`services/event-worker/app/repository.py:859-900`。
- coverage parent window 更新先 `FOR UPDATE` 锁父任务，且 update 带 `materialization_status <> 'materialized'` 条件，见 `services/event-worker/app/repository.py:436-499`。
- Redis record_request 去重使用 `SET ... NX EX`，见 `services/event-worker/app/record_request.py:298-332`。

非原子/race 风险：

- admission 是先 `SELECT COUNT(*)` 再后续 insert task。计数在 `_evidence_admission_decision` 内执行，见 `services/event-worker/app/repository.py:514-577`，真正 insert 在 `services/event-worker/app/repository.py:859-900`。多 event-worker 并发时可能同时看到未超限并一起插入，导致短时超出 global/source/event_type active limit。
- `set_evidence_status` 更新 `evidence_tasks` 只有 `WHERE event_id = ...`，没有 `WHERE status IN (...)` 或 terminal-state 保护，见 `services/event-worker/app/repository.py:1040-1059`。
- `mark_evidence_materialization_skipped` 同样按 `event_id` 直接覆盖 task 到 `materialization_skipped`，见 `services/event-worker/app/repository.py:1308-1319`。
- `set_clip_status` 同样按 `event_id` 直接覆盖 task 状态，见 `services/event-worker/app/repository.py:1379-1398`。
- `_handle_event` 中 `rolling_cache_suppress_record_requests=true` 会 ACK 后返回，但不更新 task 状态，见 `services/event-worker/app/worker.py:381-392`。这符合 rolling-cache 路径预期，但要求 media-worker rolling materialization 同步开启，否则 pending task 没有 record_request fallback。

## 5. 配置项清单

`load_config()` 读取的配置：

| 配置 | 代码默认值 | 当前 midterm 覆盖/说明 |
| --- | --- | --- |
| `REDIS_URL` | `redis://redis:6379/0`，`services/event-worker/app/config.py:48` | compose 固定为同值，`infra/docker-compose.midterm.yml:752`。 |
| `EVENT_STREAM` | `security.events`，`services/event-worker/app/config.py:49` | compose 固定，`infra/docker-compose.midterm.yml:754`。 |
| `ALERT_STREAM` | `security.alerts`，`services/event-worker/app/config.py:50` | compose 固定，`infra/docker-compose.midterm.yml:755`。 |
| `RECORD_REQUEST_STREAM` | `security.record_requests`，`services/event-worker/app/config.py:51-53` | compose 固定，`infra/docker-compose.midterm.yml:756`。 |
| `RECORDING_ENABLED` | false，`services/event-worker/app/config.py:54-55` | compose 设 true，`infra/docker-compose.midterm.yml:757`。 |
| `DATABASE_URL` | postgres container URL，`services/event-worker/app/config.py:56-59` | compose 默认指向 host.docker.internal，`infra/docker-compose.midterm.yml:753`。 |
| `CONSUMER_GROUP` / `CONSUMER_NAME` | `event-workers` / `event-worker-1`，`services/event-worker/app/config.py:46`、`services/event-worker/app/config.py:60` | compose 设 `event-workers-midterm` / `event-worker-midterm-1`，`infra/docker-compose.midterm.yml:785-786`。 |
| `POLL_TIMEOUT_MS` / `EVENT_BATCH_SIZE` | 5000 / 10，`services/event-worker/app/config.py:62-63` | compose 设 2000 / 1，`infra/docker-compose.midterm.yml:793-794`。 |
| `DEFAULT_REPLAY_SOURCE_ID` | 空，`services/event-worker/app/config.py:64` | compose 设 `primary_rtsp`，`infra/docker-compose.midterm.yml:782`。 |
| `RECORDING_EVENT_TYPES` | 空 tuple，`services/event-worker/app/config.py:65` | env/compose 默认为 `watchlist_hit,intrusion`，`infra/env/midterm.env:102`、`infra/docker-compose.midterm.yml:759`。 |
| `RECORDING_SOURCE_ID` | 空，`services/event-worker/app/config.py:66` | compose 允许空，`infra/docker-compose.midterm.yml:760`。 |
| `RECORDING_MAX_REQUESTS_PER_RUN` | 0，`services/event-worker/app/config.py:67-69` | env/compose 默认为 0，`infra/env/midterm.env:103`、`infra/docker-compose.midterm.yml:761`。 |
| `RECORDING_COOLDOWN_SECONDS` | 0，`services/event-worker/app/config.py:70-72` | env/compose 默认为 30，`infra/env/midterm.env:104`、`infra/docker-compose.midterm.yml:762`。 |
| `RECORDING_COOLDOWN_SCOPE` | `event_type`，`services/event-worker/app/config.py:73-75` | env/compose 默认为 `algorithm`，`infra/env/midterm.env:105`、`infra/docker-compose.midterm.yml:763`。`_recording_cooldown_key` 不显式识别 `algorithm`，未知值落入 source:event_type 分支，见 `services/event-worker/app/worker.py:205-217`。 |
| `RECORDING_COOLDOWN_GRACE_MS` | 1000，`services/event-worker/app/config.py:76-78` | env/compose 默认为 1000，`infra/env/midterm.env:106`、`infra/docker-compose.midterm.yml:764`。 |
| `RECORDING_PRE_SECONDS` / `RECORDING_POST_SECONDS` | fallback 到 `DEFAULT_PRE_SECONDS/DEFAULT_POST_SECONDS`，默认 5/5，`services/event-worker/app/config.py:79-84` | env/compose 5/5，`infra/env/midterm.env:127-130`、`infra/docker-compose.midterm.yml:765-768`。 |
| `RECORD_REQUEST_DEDUPE_TTL_SECONDS` | 86400，`services/event-worker/app/config.py:85-87` | compose 未显式覆盖。 |
| `ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS` | false，`services/event-worker/app/config.py:88-93` | env/compose 默认为 false，`infra/env/midterm.env:184`、`infra/docker-compose.midterm.yml:778`。 |
| `PERSON_OBSERVATION_*` | stream/group/name/start/batch/enabled 默认见 `services/event-worker/app/config.py:94-115` | compose 开启并设置 midterm group/name，`infra/docker-compose.midterm.yml:787-792`。 |

repository/worker 额外直接读取的配置：

| 配置 | 默认值/用途 | 代码位置 |
| --- | --- | --- |
| `RUNTIME_EPOCH_REDIS_KEY` | 默认 `video_analytics:midterm:runtime_epoch`，读取当前 runtime epoch。 | `services/event-worker/app/worker.py:220-239` |
| `EVIDENCE_HIGH_PRIORITY_EVENT_TYPES` | 默认 `watchlist_hit,live_search_hit`。 | `services/event-worker/app/repository.py:215-226` |
| `EVIDENCE_MATERIALIZATION_DEFER_LOW_PRIORITY` | 默认 false，打开后低优先级初始 `manifest_ready`。 | `services/event-worker/app/repository.py:229-232` |
| `EVIDENCE_EVENT_COVERAGE_MERGE_ENABLED` | 默认 false。 | `services/event-worker/app/repository.py:277-291` |
| `EVIDENCE_EVENT_COVERAGE_EVENT_TYPES` | 默认 `intrusion,watchlist_hit,live_search_hit`。 | `services/event-worker/app/repository.py:281-287` |
| `EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS` | repository 默认 30 秒。 | `services/event-worker/app/repository.py:290-291` |
| `EVIDENCE_ADMISSION_MAX_ACTIVE_GLOBAL/PER_SOURCE/BY_EVENT_TYPE` | 默认 0/0/空 map，compose 设置 240/1/intrusion:40。 | `services/event-worker/app/repository.py:514-526`、`infra/docker-compose.midterm.yml:775-777` |
| `EVIDENCE_REPLAY_TTL_SECONDS` | 默认 300。 | `services/event-worker/app/repository.py:591-604` |
| `EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS` | 代码默认 120，midterm env/compose 600。 | `services/event-worker/app/repository.py:591-604`、`infra/env/midterm.env:111`、`infra/docker-compose.midterm.yml:773` |
| `EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS` | 默认 0。 | `services/event-worker/app/repository.py:607-617`、`infra/docker-compose.midterm.yml:774` |
| `EVIDENCE_MATERIALIZATION_POLICY` | 默认 `priority`。 | `services/event-worker/app/repository.py:759-760` |

配置不一致项：

- `infra/env/midterm.env` 当前 `EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS=60`，见 `infra/env/midterm.env:185-187`；compose 默认是 30，见 `infra/docker-compose.midterm.yml:779-781`；测试仍断言 env 文件应为 30，见 `harness/tests/test_midterm_deployment_contract.py:933-949`。这是明确的 env/compose/test 不一致。
- compose 还设置了 `RECORDING_STRATEGY=savant_replay`，见 `infra/docker-compose.midterm.yml:758`，但 `Config` 没有读取该字段，`record_request.py` 也硬编码 `strategy: savant_replay`，见 `services/event-worker/app/record_request.py:198-199`。

## 6. 错误处理

异常处理路径清点：

- Redis group 创建：只有非 BUSYGROUP 异常会抛出，见 `services/event-worker/app/redis_consumer.py:40-60`。
- Redis read/pending/claim 失败：记录 exception 后返回空列表，主循环继续，见 `services/event-worker/app/redis_consumer.py:70-80`、`services/event-worker/app/redis_consumer.py:96-127`。这会表现为短期无消费，除日志外没有状态指标。
- Redis ACK 失败：记录 exception 并返回 false，调用侧只打 error，见 `services/event-worker/app/redis_consumer.py:134-141`、`services/event-worker/app/worker.py:501-509`。消息可能重复处理。
- event JSON 解析失败或缺字段：`_process_batch` 直接 ACK，见 `services/event-worker/app/worker.py:64-88`、`services/event-worker/app/worker.py:655-659`。这是有意丢弃坏消息，但没有 dead-letter。
- DB `insert_event` 失败：`_handle_event` 返回 `(False, None)` 且不 ACK，见 `services/event-worker/app/worker.py:305-313`。该路径会留 pending 供重试。
- alert policy 失败：日志后默认 emit，见 `services/event-worker/app/worker.py:318-327`。风险是策略系统异常时会发出本应 suppress 的 alert/record path。
- alert publish 失败：日志后继续，最终 ACK，见 `services/event-worker/app/worker.py:336-347`、`services/event-worker/app/worker.py:501-509`。alert 可能丢失且没有重试状态。
- evidence_task 创建失败：日志后继续，最终 ACK；随后 record_request gate 会因为 `evidence_task_status=missing` 拒绝发布，但不会写 terminal status，见 `services/event-worker/app/worker.py:349-368`、`services/event-worker/app/worker.py:403-469`。这会留下已插入 event 但没有 task/record_request 的不一致。
- `_get_evidence_task_status` 查询失败：日志后回退到初始状态，见 `services/event-worker/app/worker.py:137-158`。如果真实任务已被其他路径改为非 recordable，可能错误发布 record_request。
- record_request 去重 reserve 失败：返回 false，publish 返回 None，见 `services/event-worker/app/record_request.py:298-332`、`services/event-worker/app/record_request.py:354-379`；调用侧不会抛错，最终 ACK，见 `services/event-worker/app/worker.py:481-500`。
- record_request xadd 失败：释放 dedupe key 并返回 None，见 `services/event-worker/app/record_request.py:389-409`；调用侧 ACK，任务可能仍是 pending 但没有后续触发。
- admission 查询异常：fail-open 允许创建 task，见 `services/event-worker/app/repository.py:529-569`。高压时这会削弱 admission/backpressure。
- worker 主循环兜底：任何未捕获异常记录后 sleep 1s，见 `services/event-worker/app/worker.py:957-959`。

## 7. 测试覆盖

已发现的直接覆盖：

- `harness/tests/test_event_worker_recording_policy.py` 直接导入 event-worker 模块，见 `harness/tests/test_event_worker_recording_policy.py:10-23`。覆盖记录窗口优先级、shard 字段、runtime_epoch/ready_at 静态存在、rolling suppress、runtime epoch override、cooldown skip、event_type/source scope 与 grace，见 `harness/tests/test_event_worker_recording_policy.py:137-171`、`harness/tests/test_event_worker_recording_policy.py:281-380`、`harness/tests/test_event_worker_recording_policy.py:383-666`。
- `harness/tests/test_record_request_idempotency.py` 覆盖 Redis SET NX 去重、`has_request` 用 EXISTS 而非 stream scan、xadd 失败释放 dedupe key，见 `harness/tests/test_record_request_idempotency.py:80-82`、`harness/tests/test_record_request_idempotency.py:103-144`。
- `harness/tests/test_alert_policy_scoped_cooldown.py` 覆盖 alert policy repository scope 与 event-worker 在 intrusion cooldown 中仍允许 watchlist 录制，测试函数名见 `harness/tests/test_alert_policy_scoped_cooldown.py:243-316`。
- `harness/tests/test_evidence_materialization_phase2plus.py` 覆盖 `_materialization_ready_at`、admission limit 与 materialization skipped 静态存在，见 `harness/tests/test_evidence_materialization_phase2plus.py:170-200`、`harness/tests/test_evidence_materialization_phase2plus.py:200-363`。
- `harness/tests/test_midterm_deployment_contract.py` 覆盖 rolling-cache 相关 env/compose wiring，见 `harness/tests/test_midterm_deployment_contract.py:933-953`。

需更正的测试归属：

- Phase 0 初步清单把 `harness/tests/test_event_repository.py` 归到 `services/event-worker`，见 `docs/code_review/00_inventory.md:239`。当前文件开头明确写的是 API repository 测试，并导入 `services/api` 的 `app.repositories.events.EventRepository`，见 `harness/tests/test_event_repository.py:1-17`，不应算 event-worker 覆盖。

明显缺口：

- 未发现真实 PostgreSQL 并发测试覆盖 `_evidence_admission_decision` count-then-insert race。
- 未发现对 `set_evidence_status`、`mark_evidence_materialization_skipped`、`set_clip_status` 的 CAS/terminal-state 并发回归测试。
- 未发现 Redis read/pending/ACK 失败的端到端恢复测试。
- 未发现 `evidence_task creation failed` 后 event 已插入但 message 被 ACK 的一致性测试。
- 未发现多 event-worker consumer group 并发或 pending reclaim `min_idle_ms=60000` 的配置化测试。
- `test_event_repository_persists_runtime_epoch_on_evidence_tasks` 和 `test_event_repository_persists_materialization_ready_at` 只是字符串存在检查，见 `harness/tests/test_event_worker_recording_policy.py:162-171`，不能证明 DB 写入和回查行为。

## 8. 代码质量

超长函数：

- `_handle_event` 239 行，混合 DB insert、alert、evidence task、recording policy、Redis publish、ACK，见 `services/event-worker/app/worker.py:273-511`。
- `run_worker` 218 行，混合 consumer 初始化、person/event pending/new 循环、统计与错误恢复，见 `services/event-worker/app/worker.py:754-971`。
- `EventRepository.create_evidence_task` 203 行，混合初始状态、TTL、coverage、admission、SQL params、audit、link upsert 和 status sync，见 `services/event-worker/app/repository.py:741-943`。
- `EventRepository.set_evidence_status` 116 行，混合 event payload 更新和 task 状态同步，见 `services/event-worker/app/repository.py:945-1060`。

重复/分散逻辑：

- env parsing 在 `config.py` 和 `repository.py` 各自实现，见 `services/event-worker/app/config.py:40-45` 与 `services/event-worker/app/repository.py:172-212`。
- evidence/task 状态映射在 event-worker 内部维护，后续 media/API 也会处理相同状态集合，`EVIDENCE_TASK_STATUSES` 和 `_STATUS_TO_EVIDENCE_STATE` 见 `services/event-worker/app/repository.py:113-169`。跨模块一致性需要在后续报告继续核对。
- `materialization_ready_at` 同时写入 `evidence_tasks` 字段和 `materialization_audit`/events payload，见 `services/event-worker/app/repository.py:839-849`、`services/event-worker/app/repository.py:991-994`。

死代码/可疑残留：

- `RedisStreamConsumer.trim()` 未发现调用点，定义在 `services/event-worker/app/redis_consumer.py:143-148`。
- `RecordingPolicyState.last_recorded_event_type` 写入和读取存在，但 `_handle_event` 读取后的 `last_recorded_event_type` 未参与判断，见 `services/event-worker/app/worker.py:52-55`、`services/event-worker/app/worker.py:424-426`、`services/event-worker/app/worker.py:492-494`。
- `RECORDING_STRATEGY` 在 compose 中设置但代码未读取，见 `infra/docker-compose.midterm.yml:758` 与 `services/event-worker/app/config.py:45-115`。

魔法数字/硬编码：

- Redis stream `maxlen=10000` 在 alert、record_request、trim 中重复硬编码，见 `services/event-worker/app/alert_publisher.py:70`、`services/event-worker/app/record_request.py:389-392`、`services/event-worker/app/redis_consumer.py:143-148`。
- pending reclaim `min_idle_ms=60000` 是 `read_pending` 默认值，`run_worker` 调用时没有配置入口，见 `services/event-worker/app/redis_consumer.py:92-94`、`services/event-worker/app/worker.py:879-880`。
- 主循环异常 sleep 1 秒、summary 60 秒均写死在 `services/event-worker/app/worker.py:943-959`。

TODO/FIXME/HACK：

- `rg -n "TODO|FIXME|HACK" services/event-worker/app services/event-worker/main.py services/event-worker/Dockerfile services/event-worker/requirements.txt` 未发现命中。

## 9. 与现有文档的一致性

一致项：

- 文档描述旧 per-event Replay 链路为 `event-worker -> security.record_requests -> clip-worker -> Replay job -> video-file-sink -> media-worker`，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:18-33`；event-worker 仍保留该 fallback 路径，代码见 `services/event-worker/app/worker.py:481-485` 和 `services/event-worker/app/record_request.py:354-409`。
- rolling-cache 计划要求 event-worker canary 时只创建 task、不发 per-event Replay 请求，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:1784-1787` 和 `specs/30_midterm_rolling_cache_evidence_plan.md:313-315`；代码实现为 `ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS` 分支，见 `services/event-worker/app/worker.py:381-392`。
- runtime epoch 一等字段文档要求见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:1942-1946`；migration 已添加字段和 active index，见 `db/migrations/024_evidence_task_runtime_epoch_barrier.sql:7-44`；event-worker 写入 task 字段，见 `services/event-worker/app/repository.py:833`、`services/event-worker/app/repository.py:868-888`。
- ready_at 文档要求 event-worker 创建 task 时写 `event_ts + post_seconds + segment_grace`，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:2148-2162`；代码实现见 `services/event-worker/app/repository.py:607-617`、`services/event-worker/app/repository.py:839-849`；migration 添加字段和索引见 `db/migrations/025_evidence_task_materialization_ready_at.sql:7-20`。
- `specs/30` 要求 rolling flags 默认关闭，见 `specs/30_midterm_rolling_cache_evidence_plan.md:255-271`；当前 env 中 rolling-cache 相关默认关闭，见 `infra/env/midterm.env:171-184`。

不一致/需要标注：

- `docs/current_mainline_status.md` 说当前高密度成功路径是 rolling-cache fast path，且 `security.record_requests` 为 0，见 `docs/current_mainline_status.md:49-80`；但 tracked midterm env/compose 默认仍是 `ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=false`，见 `infra/env/midterm.env:184`、`infra/docker-compose.midterm.yml:778`。这并非代码缺失，但说明“当前 mainline”依赖 pressure runner 或 profile override，默认 `docker compose` 环境不会自动走 record_requests=0。
- `docs/project_current_progress_summary.md` 既记录 rolling-cache/epoch barrier 已落地，见 `docs/project_current_progress_summary.md:73-129`，又在后续“证据生成与查看”中仍写当前采用 4 个 Replay/video-file-sink shard，见 `docs/project_current_progress_summary.md:185-195`。event-worker 代码同时支持两条路径，但文档表述存在新旧主路径混写。
- docs 中 pressure runner ready grace 写 event-worker 为 9 秒、media fallback 为 5 秒，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:2647-2650`；compose 默认 `EVIDENCE_MATERIALIZATION_READY_SEGMENT_GRACE_SECONDS=0`，见 `infra/docker-compose.midterm.yml:774`。这需要说明是压测 override 还是默认漂移。
- coverage window：compose/repository 默认 30 秒，见 `services/event-worker/app/repository.py:290-291`、`infra/docker-compose.midterm.yml:779-781`；env 当前是 60 秒，见 `infra/env/midterm.env:185-187`；deployment contract 测试仍期待 env 为 30，见 `harness/tests/test_midterm_deployment_contract.py:933-949`。

## 10. 已知问题回归检查

1. `REPLAY_TS_SYNC=false` 是否仍然默认 false：
   - 本模块不直接读取 `REPLAY_TS_SYNC`。`rg -n "REPLAY_TS_SYNC" services/event-worker` 无命中。
   - 相关文档说该开关已落地并生效，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:86-105`。
   - 结论：对 event-worker 不适用。需要在 `services/clip-worker` / `modules/savant_replay` 报告中继续核对真实默认值和绕过路径。

2. clip-worker proof wait 是否脱离单循环同步阻塞：
   - 本模块不做 proof wait。event-worker 的 `_process_batch` 仍是顺序循环，见 `services/event-worker/app/worker.py:653-687`，但这里处理的是事件入库和发布，不是 post-Savant frame proof。
   - 结论：对 event-worker 不适用；下一份 `services/clip-worker` 报告必须重点核对。

3. `evidence_tasks` slot/状态更新是否都走条件更新/CAS：
   - 不满足。`set_evidence_status`、`mark_evidence_materialization_skipped`、`set_clip_status` 都按 `event_id` 直接更新 evidence_tasks，没有 `WHERE status IN (...)` 或 terminal-state 条件，见 `services/event-worker/app/repository.py:1040-1059`、`services/event-worker/app/repository.py:1308-1319`、`services/event-worker/app/repository.py:1379-1398`。
   - admission 也是 count-then-insert，不是原子 slot reservation，见 `services/event-worker/app/repository.py:514-577` 和 `services/event-worker/app/repository.py:859-900`。
   - 结论：event-worker 仍残留非 CAS 状态写法，是本模块最高优先级风险。

4. `runtime_epoch_id` 是否是一等字段并有 barrier：
   - event-worker 会从 Redis 读取当前 epoch 并写入 event/payload/media，见 `services/event-worker/app/worker.py:220-270`；创建 task 时写 `runtime_epoch_id` 字段，见 `services/event-worker/app/repository.py:833`、`services/event-worker/app/repository.py:868-888`。
   - migration 已把 `evidence_tasks.runtime_epoch_id` 升为一等字段并建 active index，见 `db/migrations/024_evidence_task_runtime_epoch_barrier.sql:7-44`。
   - barrier 自身在 API/runtime_apply，不在 event-worker。相关文档指向 API 与 media-worker，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:1962-1975`。
   - 风险：`_current_runtime_epoch_id` Redis 查询失败会返回空字符串，见 `services/event-worker/app/worker.py:220-239`，这会让后续任务缺少当前 epoch。是否由 API/overview 监控 NULL 非终态任务，需要在 API 报告验证。

5. rolling-cache segment 边界是否严格对齐 GOP/keyframe：
   - event-worker 不生成 segment，不做 GOP/keyframe 裁切。它只透传 record_request keyframe/PTS metadata，见 `services/event-worker/app/record_request.py:37-77`、`services/event-worker/app/record_request.py:80-144`，以及 rolling suppress gate，见 `services/event-worker/app/worker.py:381-392`。
   - 结论：对 event-worker 不适用；应在 `services/media-worker`、rolling-cache sink 脚本和 `modules/savant_replay` 报告中核对。

6. `materialization_ready_at/not_before` 是否把自然等待和真实处理占用分开：
   - event-worker 负责写入 ready time，代码是 `event_at + post_seconds + segment_grace`，见 `services/event-worker/app/repository.py:607-617`，并持久化到 task 和 audit，见 `services/event-worker/app/repository.py:839-849`。
   - 初始 `materialization_deadline_at` 仍由 event time 计算，见 `services/event-worker/app/repository.py:591-604`。文档要求 claim 后由 media-worker 重置 processing deadline，见 `docs/midterm_evidence_pipeline_sharding_diagnosis_2026-07-03.md:2154-2160`。
   - 结论：event-worker 侧 ready_at 已实现；“worker 占用彻底分开”和“deadline 从 claim_time 算”需要在 media-worker 报告继续验证。

7. media-worker finalizer claim 是否还存在 claimed=true 误报：
   - 本模块不 claim media finalizer 任务。
   - 结论：对 event-worker 不适用；需要在 `services/media-worker` 报告核对。

## 11. 风险与建议

1. 高风险：event-worker 仍有非 CAS task 状态覆盖。
   - 影响：API epoch barrier 或 media-worker 已把任务推到终态后，event-worker 的 `set_clip_status`/skip/status sync 可能按 `event_id` 覆盖状态，造成“终态复活”或错误失败。
   - 建议：把 `services/event-worker/app/repository.py:1040-1059`、`services/event-worker/app/repository.py:1308-1319`、`services/event-worker/app/repository.py:1379-1398` 改成显式 allowed-from 状态条件，并对 terminal 状态加防复活保护；补真实 PostgreSQL 并发测试。

2. 高风险：evidence_task 创建或 record_request 发布失败后可能 ACK，导致事件已入库但证据链路无后续触发。
   - 影响：事件可见但 evidence task 缺失、task pending 无 record_request、或 alert 丢失，只靠日志排查。
   - 建议：对 `services/event-worker/app/worker.py:349-368` 和 `services/event-worker/app/worker.py:481-500` 增加明确失败状态、retry/dead-letter 或不 ACK 策略；至少把失败计数暴露到监控。

3. 中高风险：admission count-then-insert 不是 slot reservation。
   - 影响：单容器默认下风险较低，但 event-worker 水平扩容或 pending reclaim 并发时可能突破 `EVIDENCE_ADMISSION_MAX_ACTIVE_*` 限额。
   - 建议：用 DB 约束、advisory lock、slot 表或单条条件 update/insert 实现原子 admission。

4. 中风险：配置漂移会改变证据量和 cooldown 语义。
   - 影响：`EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS` 在 env/compose/test 中分别表现为 60/30/30；`RECORDING_COOLDOWN_SCOPE=algorithm` 在 record_request cooldown 中不是显式 scope，只是落入 fallback。
   - 建议：统一 env/compose/test/docs，给 record_request cooldown 增加明确的 `source_event_type` 或 `algorithm` 语义，并让测试覆盖实际 midterm 默认值。

5. 中风险：rolling suppress 强依赖 media-worker rolling materialization 同步开启。
   - 影响：`ROLLING_CACHE_SUPPRESS_RECORD_REQUESTS=true` 时 event-worker ACK 后不发 Replay request，也不终态化 task；若 media-worker rolling path 未启用或配置错误，任务会长期 pending。
   - 建议：增加启动/doctor 合同：suppress=true 必须同时满足 `ROLLING_CACHE_MATERIALIZATION_ENABLED=true` 或存在明确 fallback；也可在 event-worker 启动日志中输出强告警。
