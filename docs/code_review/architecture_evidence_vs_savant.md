# Architecture Review: Evidence Pipeline vs Official Savant Pattern

范围：当前工作树 `/home/user/video-analytics`。本文只做架构审查和对照分析，不修改源码/测试。官方参考以 Savant/Savant-RS 当前公开文档为准：

- Savant adapter/ZeroMQ 模型：<https://savant-ai.io/docs/latest/savant_101/10_adapters.html>
- Savant-RS Replay service：<https://insight-platform.github.io/savant-rs/services/replay/index.html>
- Replay re-streaming jobs：<https://insight-platform.github.io/savant-rs/services/replay/3_jobs.html>
- Replay REST API：<https://insight-platform.github.io/savant-rs/services/replay/4_api.html>

## 1. 结论

当前默认 midterm 证据链路的底层媒体获取方式和 Savant 官方推荐大体一致：视频源通过 adapter/ZeroMQ 进入 Replay，Replay 作为 sidecar/intermediary 保存可回放媒体，事件触发后用 Replay REST job 把片段推到 video-file-sink，再由应用层打包 evidence。这个骨架可在 `infra/docker-compose.midterm.yml:3-10`、`modules/savant_replay/config.midterm.json:19-47`、`services/clip-worker/app/replay_client.py:405-490` 中确认。

真正有出入的是 evidence 的产品层定义：官方 Savant 文档只定义 adapter、Replay、job、sink 等媒体/元数据传输能力，并不定义 `evidence_tasks/evidence_bundles` 这种业务证据状态机。当前项目把 evidence 设计成跨 Redis、PostgreSQL、Replay、video-file-sink、media-worker 的应用层工作流，这不是错误，但不能再简单称为“纯 Savant 官方方式”。

最大架构偏差有三处：

1. raw clip 与标注 sidecar 不来自同一条 Savant 输出流。raw clip 来自 Replay 存储回放，标注来自 post-Savant `security.frame_annotations` Redis 侧通道，media-worker 再按 PTS/frame_uuid 对齐。这解释了为什么代码里需要大量 frame-domain proof、epoch guard、sink window guard。
2. Savant 模块内 PyFunc 直接写 Redis 事件/帧标注。官方通常建议模块通过 adapters 与外部系统解耦，直接交互“可行但不是通常路径”。当前实现用 bounded async writer 降低阻塞风险，但如果 Redis 队列满会丢弃，这与 evidence proof 的可靠性要求存在张力。
3. rolling-cache 是自研高吞吐替代路径，不是 Savant Replay 官方语义。当前默认 env 仍关闭 rolling-cache，但代码和 compose 已有可选路径；打开后需要把它明确标成 custom path，而不是“官方 Replay path”。

## 2. 官方 Savant 模式要点

官方 adapter 文档的核心思想是：Savant module 主要跑视觉 pipeline，外部视频源、输出目标、数据库/队列等由独立 adapter 处理；通信使用 Savant adapter protocol + ZeroMQ，支持 video frame、stream info、frame metadata、object hierarchy 等数据。

对证据回放最相关的是 Replay 文档：

- Replay 是面向非线性视频分析的存储服务，可缓存多路视频并按 TTL 淘汰，也可通过 REST API re-stream 到 Savant sink 或 module。
- Replay job 是按需创建的动态 source，包含 sink、anchor frame、offset、configuration、stop condition、attributes。
- Replay 使用 frame/keyframe UUID 导航；anchor frame 是 keyframe UUID，offset 支持回看历史。
- Replay 支持 `ts_sync=true/false`，即按时间同步发送或尽快发送。
- Replay 明确不内置 job concurrency limit，也不持久化 running jobs；并发控制、恢复和状态持久化要由应用自己实现。
- video file sink adapter 会把 video 和 `metadata.json` 写入目录，`CHUNK_SIZE=0` 表示只按 EOS 分段。

因此，“官方方式”更像是一个媒体基础设施合同：

```text
source adapter -> Replay/storage -> Savant module/adapters
event or user trigger -> Replay REST job(anchor keyframe + offset + stop condition)
Replay job -> Savant sink/module, commonly video-file-sink
application layer -> business evidence package/index/UI
```

官方没有要求业务证据一定在 Savant module 内生成，也没有提供 evidence bundle schema。

## 3. 当前默认 Evidence 链路

当前 compose 自己把顺序写成“RTSP source -> Replay storage -> Savant inference -> Redis/Postgres event -> Replay job -> video-file-sink -> media-worker evidence bundle”，见 `infra/docker-compose.midterm.yml:3-10`。

实际拓扑：

1. `source-adapter` 把 RTSP 推给 Replay：`dealer+connect:tcp://replay-service:5555`，见 `infra/docker-compose.midterm.yml:716-729`。
2. Replay `out_stream` 先推给 `replay-raw-fanout:5557`，见 `modules/savant_replay/config.midterm.json:19-40`。
3. `replay-raw-fanout` 不采样，先把 raw branch 发到 `pub+bind:tcp://0.0.0.0:5560` 供 rolling-cache 使用，再把完整流转给 `analysis-forwarder:5557`。
4. `analysis-forwarder` 采样后推给 `savant-security`；compose 默认关闭它自己的 raw branch，避免 rolling-cache 再接到推理前向器之后，见 `infra/docker-compose.midterm.yml` 与 `services/analysis-forwarder/app/main.py`。
5. `savant-security` 用 `zeromq_source_bin` 收输入，并执行 YOLO/nvtracker/face/AdaFace/rules/Redis export，见 `modules/savant_security/module.yml:44-60`、`modules/savant_security/module.yml:74-223`。
6. frame annotation exporter 把轻量 frame/object metadata 写入 `security.frame_annotations`，见 `modules/savant_security/module.yml:259-286`；实际 writer 是 bounded async Redis Stream writer，满队列会 drop，见 `modules/savant_security/custom/services/redis_stream_writer.py:27-33`、`modules/savant_security/custom/services/redis_stream_writer.py:103-127`。
7. event-worker 写 `events/evidence_tasks`，并在 recordable 时发布 `security.record_requests`，见 `services/event-worker/app/worker.py:349-392`、`services/event-worker/app/worker.py:481-485`、`services/event-worker/app/repository.py:741-943`。
8. record_request 携带 `frame_uuid/keyframe_uuid/previous_keyframe_uuid/runtime_epoch_id/stream_session_id` 等锚点，见 `services/event-worker/app/record_request.py:180-219`。
8. clip-worker 先从 post-Savant frame annotations 找 proof，再调用 Replay `/api/v1/keyframes/find` 和 `/api/v1/job`，见 `services/clip-worker/app/worker.py:2452-2853`、`services/clip-worker/app/worker.py:3906-4245`、`services/clip-worker/app/replay_client.py:50-180`、`services/clip-worker/app/replay_client.py:309-490`。
9. Replay job 直接输出到 `video-file-sink:6666`，见 `infra/docker-compose.midterm.yml:893-977`；video-file-sink 写 `video.mov/metadata.json`，entrypoint 还生成 runtime epoch 目录，见 `infra/docker-compose.midterm.yml:979-990`、`scripts/runtime/video_file_sink_entrypoint.sh:4-89`。
10. media-worker 扫描 sink 输出、等待稳定、finalize bundle、写 DB evidence index，见 `services/media-worker/app/worker.py:4355-5455`、`services/media-worker/app/evidence_db_index.py:23-36`、`services/media-worker/app/evidence_db_index.py:65-194`。

## 4. 一致的地方

### 4.1 Replay 作为媒体权威是对的

当前把 Replay 放在 RTSP source 和 Savant 推理之前，符合“先存储原始流，再按事件回放”的证据需求。`modules/savant_replay/config.midterm.json:41-47` 配置 RocksDB TTL，`infra/docker-compose.midterm.yml:136-147` 使用官方 replay image 和持久化 RocksDB 目录。

### 4.2 Replay job payload 基本按官方合同构造

`ReplayClient.build_job_payload()` 明确设置了：

- `sink.url`
- `configuration.ts_sync`
- `stored_stream_id/resulting_stream_id`
- `max_idle_duration/max_delivery_duration`
- `labels`
- `stop_condition`
- `anchor_keyframe`
- `offset`

对应代码在 `services/clip-worker/app/replay_client.py:405-490`。这和官方 Replay job 的组成一致。

### 4.3 官方把 concurrency/persistence 留给应用，当前 DB slot 正是在补这个洞

官方文档说明 Replay job 独立、并发限制和 running job 持久化要应用自己做。当前 clip-worker 的 `try_acquire_replay_slot()` 用 PostgreSQL advisory lock + `FOR UPDATE` + active counts 原子预占 slot，见 `services/clip-worker/app/repository.py:276-582`；media-worker 在 sink video 稳定后释放 slot，见 `services/media-worker/app/worker.py:2209-2311`。这属于合理的产品层补齐。

### 4.4 video-file-sink 的用法基本正统

当前 Replay job sink 指向 `dealer+connect:tcp://video-file-sink:6666`，video-file-sink 用 `router+bind` 接收，见 `infra/docker-compose.midterm.yml:905-906`、`infra/docker-compose.midterm.yml:979-990`。这符合 Replay job sink 是 connect-type socket、目标是预先存在的 sink service 的官方描述。

## 5. 有出入的地方

### 5.1 Evidence 不是单一 Savant stream 的直接产物

官方 video-file-sink 可以接收一个 Savant 输出流，并同时写视频和 frame metadata。当前 evidence raw clip 来自 Replay 存储回放，而标注来自 Savant 之前已经处理过的 `security.frame_annotations` Redis stream。media-worker 在 sink metadata 没有 objects 时走 frame-cache sidecar 路径，见 `services/media-worker/app/worker.py:3924-4147`。

这不是错，但它是“跨域合成 evidence”：原始视频域、post-Savant metadata 域、PostgreSQL 事件域要重新对齐。对应复杂度已经显现在 clip-worker proof wait 和 media-worker guard 里，见 `services/clip-worker/app/worker.py:2452-2853`、`services/media-worker/app/worker.py:4192-4278`。

影响：

- evidence 质量依赖 PTS/frame_uuid/runtime_epoch/stream_session_id 的一致性，而不只是 Replay job 成功。
- raw clip 可包含没有推理标注的帧，尤其 `analysis-forwarder` 和 Savant ingress gate 都会采样。
- 这个设计更适合“原始可回放媒体 + 稀疏可信标注”，不适合默认承诺“每帧都有完整 overlay”。

### 5.2 Savant hot path 直接写 Redis 是实用偏离

官方建议 module 把外部 IO 交给 adapters，但当前 frame annotation、face/person observation、event export 都在 Savant PyFunc/service 内直接写 Redis。`AsyncRedisStreamWriter` 的设计明确是为了不阻塞 `process_frame`，队列满会 drop，见 `modules/savant_security/custom/services/redis_stream_writer.py:27-33`、`modules/savant_security/custom/services/redis_stream_writer.py:122-127`。

这对推理稳定性是友好的，但对 evidence proof 是风险：如果 `security.frame_annotations` 在压力下丢帧，clip-worker 可能等不到 proof 或只能走 fallback/失败。换句话说，当前是“推理优先、证据 proof best-effort”的 Savant 内部侧通道，不是官方 adapter 图上的可靠 sink。

### 5.3 `POST_SAVANT_FAST_RAW_CLIP_ENABLED=true` 会放松精确窗口语义

当前 env 默认打开 `POST_SAVANT_FAST_RAW_CLIP_ENABLED=true`，见 `infra/env/midterm.env:169`。media-worker 在 fast raw clip 模式下会放松 duration/window guard，见 `services/media-worker/app/worker.py:4226-4231`。

这和 Replay 官方的 `anchor + offset + stop_condition` 精确回放语义不是完全同一件事。它可能是性能必要的工程折中，但报告/产品语义应明确：fast raw clip 更接近“Replay/GOP 窗口原始片段”，不是严格裁剪到事件时间窗的官方 Replay job 输出。

### 5.4 rolling-cache 是自研替代路径

当前默认 `infra/env/midterm.env:171-184` 关闭 rolling-cache；但 compose 已有 `rolling-cache-sink` profile，代码也已有 materializer。rolling-cache-sink 订阅 `replay-raw-fanout:5560` 的 Replay 后、analysis-forwarder/resampler 前 raw branch 写分段文件，见 `infra/docker-compose.midterm.yml`、`scripts/runtime/rolling_cache_sink_entrypoint.sh:4-51`。media-worker 再把 rolling segments remux/copy 成 sink-like 目录并进入同一 finalizer，见 `services/media-worker/app/rolling_cache.py:104-280`、`services/media-worker/app/worker.py:5927-6713`。

这条路可以解决高并发 Replay job 压力，但它已经脱离官方 Replay service 的 keyframe/job/REST 合同。它应被标成 `custom rolling-cache evidence path`，并单独验证：

- retention 是否等价 Replay TTL；
- segment 边界/keyframe 是否可保证可解码；
- backpressure/磁盘上限是否有可靠治理；
- 与 event frame PTS 的覆盖关系是否可审计；
- 和 Replay fallback 的优先级是否清晰。

### 5.5 当前主要用 sink 文件稳定作为 Replay completion 信号

官方建议如需避免 concurrent jobs 可通过 REST API 查 job status；当前实现主要靠 DB slot + video-file-sink 文件出现/稳定来释放 slot。media-worker 在 sink video stable 后释放 slot，见 `services/media-worker/app/worker.py:2209-2311`、`services/media-worker/app/worker.py:4521-4587`。

这在现有工程里合理，但故障语义会混在一起：Replay job 慢、sink 慢、文件系统慢、metadata 不完整，都会表现为 materialization 延迟或过期。后续如果继续压测 60 路，建议把 Replay job status 轮询作为诊断信号补进来，而不是替换现有 sink-stable 机制。

## 6. 建议

1. 文档上把证据链路分成两层：`Savant/Replay media substrate` 和 `video-analytics evidence product layer`。前者大体官方，后者是自研。
2. 默认路径继续称为 `post_savant_replay`，但说明它是“Replay raw clip + post-Savant frame annotation sidecar”的合成证据，不是单条 Savant 输出流直接落盘。
3. 如果未来要更贴近官方 Savant 方式，有两个方向：让 Replay job 回放到 Savant module 再进 video-file-sink，或者让正常 Savant 输出直接进入 video-file-sink/metadata sink 做环形缓存。两者都会提高 metadata/video 同源性，但会增加 GPU/IO 成本。
4. 如果继续推进 rolling-cache，把它作为独立 ADR/模块审查对象，验收标准不能只复用 Replay path 的成功条件。
5. 对 `security.frame_annotations` 增加丢弃率、queue depth、stream lag 与 proof failure 的联动指标；否则 evidence 失败时很难区分 Replay 问题和 Savant hot-path metadata drop。
6. 保留 DB slot admission。它不是对官方的偏离，而是官方明确留给应用处理的并发/持久化职责。

## 7. 当前风险分级

| 风险 | 等级 | 依据 | 建议动作 |
| --- | --- | --- | --- |
| raw clip 与 annotation sidecar 分域合成，导致证据质量依赖 PTS/frame_uuid/session/epoch guard | 高 | `services/clip-worker/app/worker.py:2452-2853`、`services/media-worker/app/worker.py:4192-4278` | 把“稀疏可信标注”作为产品语义写清；继续强化 proof/guard 指标 |
| Savant PyFunc Redis exporter drop-on-full 与 evidence proof 可靠性冲突 | 中高 | `modules/savant_security/custom/services/redis_stream_writer.py:103-127` | 暴露 drop metrics，并把 proof miss 与 exporter drop 关联 |
| fast raw clip 放松精确窗口语义 | 中 | `infra/env/midterm.env:169`、`services/media-worker/app/worker.py:4226-4231` | 在 UI/API summary 中区分 exact crop 与 fast raw/GOP window |
| rolling-cache 被误认为官方 Replay path | 中 | `infra/env/midterm.env:171-184`、`services/media-worker/app/worker.py:5927-6713` | 单独 ADR 和验收矩阵 |
| Replay job completion 主要由 sink 文件稳定推断 | 中 | `services/media-worker/app/worker.py:2209-2311`、`services/media-worker/app/worker.py:4521-4587` | 增加 Replay job status 诊断，不必替换现有机制 |
