# Midterm 实验室告警与 60 路就绪性发现

日期：2026-06-16

本文固化实验室摄像头告警诊断、30/60 路生产就绪性评估中的只读运行时发现。
它是发现记录，不是实施计划。

## 1. 实验室摄像头告警行为

当前实验室摄像头并不是“完全没有告警”。在 2026-06-15 晚上的 live 检查中，
实验室摄像头产生了 intrusion 记录，并且最新一批 record-request 事件中的大多数
都成功生成了证据。

已观察到的实验室事件：

| 创建时间 CST | 事件状态 | 证据状态 | 备注 |
| --- | --- | --- | --- |
| 2026-06-15 21:11:44 | stored | ready | `/media/evidence/72c5986a-d184-47b7-9bfb-fbf9b643144b/raw_clip.mov` |
| 2026-06-15 21:12:14 | stored | ready | `/media/evidence/161f1fd1-437c-401e-87c6-17209a63208a/raw_clip.mov` |
| 2026-06-15 21:12:46 | stored | ready | `/media/evidence/49543594-8fed-4eaa-90c2-bf3fab2fcced/raw_clip.mov` |
| 2026-06-15 21:13:16 | stored | ready | `/media/evidence/38db0dbe-a611-4d7b-bb00-7b0a8a3abf27/raw_clip.mov` |
| 2026-06-15 21:13:32 | stored | not_implemented | 被 `camera_algorithm_cooldown` 抑制；预期不会创建 record request/evidence |
| 2026-06-15 21:14:14 | stored | ready | `/media/evidence/3531cd12-2394-4292-8f6a-37167b0a39a6/raw_clip.mov` |

用户可见的差距是：“人出现在实验室摄像头前”不等于证据会立刻出现在 8090。
运行链路如下：

```text
Savant intrusion event
  -> event-worker DB row / alert / record_request
  -> clip-worker waits for post-window frame proof
  -> Replay job writes a 10s evidence clip
  -> media-worker finalizes the bundle
  -> 8090 shows ready evidence
```

最新实验室证据通常在事件发生后几十秒才 ready，不是实时出现。已观察到的延迟来自
post-window proof 等待、Replay/media finalization，以及当时
`CLIP_WORKER_MAX_CONCURRENT_JOBS=1` 带来的竞争。

## 2. `min_inside_ms` 语义

`min_inside_ms` 表示：同一个被跟踪的人必须在配置的 intrusion 区域内连续停留的最短时间，
达到后才允许触发 intrusion 事件。

实现使用的是 person bbox 的脚点，不是 bbox 中心点：

```text
foot_point = (bbox.x + bbox.width / 2, bbox.y + bbox.height)
```

对每条 track，规则先检查最新脚点是否在 ROI polygon 内。如果在，就沿着同一条 track
向前回扫，找到最早的连续在区域内的 observation，并计算：

```text
inside_ms = latest_observation.timestamp_ms - first_inside_observation.timestamp_ms
```

只有满足以下条件时才会发出 intrusion 事件：

- 规则已启用；
- track 存在 observations；
- 最新脚点在区域内；
- `inside_ms >= min_inside_ms`；
- per-track/per-zone cooldown 允许发出事件。

当前两个运行时摄像头的差异：

| Source | `min_inside_ms` | 实际效果 |
| --- | ---: | --- |
| `primary_rtsp` | 1 | 只要检测到人在全画面区域内，几乎立即触发 |
| `source_00000000-0000-4000-8000-781078565686` (`lab`) | 1000 | 需要在实验室 ROI 内稳定跟踪约 1 秒 |

如果 pose detector 或 tracker 在短时间内丢人，连续在区域内的计时会重置。即使视频流健康，
实验室源也会比 primary movie source 显得没那么即时。

## 3. 实验室检测率发现

实验室源处于 active 状态，并且大约以 8fps 产生 annotation，但 person detection
并不是每帧都稳定出现。在 live 检查中，较短的新窗口能看到人被检测到；但更长窗口的比例
仍然显示检测比较稀疏：

```text
lab_last_2m  frames=960  frames_with_person=240  pct=25.00%
lab_last_5m  frames=2401 frames_with_person=863  pct=35.94%
lab_last_10m frames=4802 frames_with_person=863  pct=17.97%
```

同一轮调查中，累计运行时计数也显示 primary 与 lab 的检测量存在明显差距：

```text
primary_rtsp pose_objects_total ~= 37k
lab          pose_objects_total ~= 417
```

这意味着漏报或延迟的实验室告警不能只从 clip-worker 侧排查。需要区分两个问题：

1. Savant 是否产生了 intrusion 事件？
2. 如果产生了，证据链路是否让该事件可见并变成 ready？

在最新测试窗口中，这两个问题的答案大多是“是”。对更早的实验室测试来说，主要差距往往在
证据链路之前：检测/跟踪稀疏或 cooldown suppression 使 record-request 事件少于预期。

## 4. 证据链路状态

post-Savant evidence proof 修复改善了“事件存在但没有证据”的路径。修复后的新实验室事件
可以变成 `ready`。

剩余证据链路问题：

- 2026-06-17 的 clip-worker scheduling 修复把当前部署默认值提升到
  `CLIP_WORKER_MAX_CONCURRENT_JOBS=4`，并把 scheduling gate 移到 post-Savant proof
  等待之前。这消除了此前单并发串行化的失败模式，但还没有让 clip 生成具备 shard-aware
  能力。Replay API 路由和 sink 容量仍然是 60 路证据突发时的单栈瓶颈。
- 被 suppressed 的事件会被存储，但不会创建 record request，因为当前策略配置为
  `suppress_record_request=true`。
- 8090 必须区分 event records、suppressed records、queued/replaying evidence、
  ready evidence 和 failed evidence。只展示 ready evidence 会让延迟中的事件看起来像
  “没有告警”。
- media finalization 当前会记录 `ffprobe not found` 并回退到 `imageio_ffmpeg`；
  已观察运行中该回退可用，但生产诊断前应清理。

## 5. 当前 60 路就绪性判断

当前架构方向是正确的：

```text
RTSP source-adapter
  -> Replay full-rate storage
  -> analysis-forwarder sampled/drop-on-pressure path
  -> Savant inference
  -> Redis/PostgreSQL
  -> clip-worker Replay job
  -> video-file-sink
  -> media-worker evidence bundle
  -> 8090
```

这条链路把全帧率证据路径与采样后的分析路径分离。它能避免旧的 inline Replay-to-Savant
路径中，Savant 分析分支变慢或停滞时直接反压 source capture。

但是，当前 checkout 还不能宣称稳定支持 60 路运行。静态 readiness 检查显示 Phase 1
forwarder topology 已存在，但本地运行环境不是 30/60 路验证环境：

```text
phase1_forwarder_passed = true
enabled_rtsp_sources actual = 1, required >= 30
gpu_t4_count actual = 0, required >= 1
```

60 路至少需要两个生产分片：

```text
Shard A: 30 sources -> replay-a -> analysis-forwarder-a -> savant-a on T4 #0
Shard B: 30 sources -> replay-b -> analysis-forwarder-b -> savant-b on T4 #1
```

API/Redis/PostgreSQL/evidence UI 可以保持共享，但所有接触 Replay jobs 的 worker
都必须变成 shard-aware。

## 6. 生产前必须修复的 60 路差距

不能只通过给当前单分片栈继续添加摄像头来扩展到 60 路。已知差距：

- 没有 first-class 的 `source_id -> shard -> Replay/Savant` 路由映射；
- runtime source generation 仍需要支持 per-shard Replay endpoints；
- clip-worker 当前假设只有一个 `REPLAY_API_URL`；它必须把 job 路由到存储该 source 的
  Replay shard；
- `MAX_PARALLEL_STREAMS` 必须按每个 Savant module 的 30 路加余量进行设置和验证；
- analysis-forwarder 必须按 shard 部署，并暴露 per-source fairness/drop metrics；
- 未使用的 Savant H264/NVENC output 应该在 scale test 前禁用或改成 metadata-only；
- `video-file-sink`、clip-worker 和 media-worker 当前仍是证据突发时的单点瓶颈；
- Redis stream sizing 对 60 路过小，除非提高 retention 或对 streams 分片。

Redis sizing 风险尤其具体：

```text
60 streams * 8fps ~= 480 frame_annotation entries/s
FRAME_ANNOTATION_REDIS_MAXLEN=20000
20000 / 480 ~= 42s of frame annotations
```

如果 clip-worker/media-worker 的延迟超过这个窗口，frame proof lookup 可能会失败，
因为需要的 post-Savant annotation 已被裁剪。

## 7. 必需的 UI/可观测性改进

8090 应该把 event pipeline 显示为独立状态，而不是只展示 ready evidence。
至少需要按 source 展示：

- effective analysis fps；
- 最近窗口内 frames with person ratio；
- last frame age；
- last intrusion event time；
- last record_request time；
- latest evidence state and reason；
- suppressed/cooldown count；
- clip-worker queued/replaying/failed counts；
- source/container restart count and restart rate；
- forwarder drop rate and queue depth。

这是建立 operator trust 的必要条件。否则，一个健康但证据延迟或被 cooldown 抑制的流，
在界面上会看起来像“没有告警”。

## 8. 下一步执行指导

不要直接跳到 60 路。按以下顺序执行：

1. 强化 8090 event/evidence 可观测性，让实验室测试中事件记录能立即显示，即使证据仍在
   queued 或 replaying。
2. 提升或重设计 clip-worker/media-worker 并发，加入明确的 queueing 和 backpressure 限制。
3. 根据最坏证据延迟来设置 Redis streams 和 Replay TTL，而不是只按 5s pre/5s post clip 长度。
4. 先跑 10 路 soak，再跑 30 路 single-T4 shard test。
5. 只有 Phase 2 通过后，才实施双 shard source routing 和 60 路 Replay job routing。

scale test 的验收必须包含：

- source adapter restart count 保持不变；
- Replay 没有持续 EAGAIN/send-timeout storm；
- forwarder drop 有界且 per-source fair；
- Savant per-source fps 和 last-frame age 在目标范围内；
- Redis proof retention 超过已观察到的 queue/replay delay；
- evidence success rate 和 ready latency 是实测值，不是推断值。

## 9. 2026-06-17 补充：分片与 Replay 录制发现

2026-06-17 的验证保持同一结论：当前 runtime 对小规模部署具备有用的 backpressure
隔离能力，但还不是一个已验证的 60 路系统。

已验证的当前事实：

```text
pytest readiness/forwarder/clip-worker queue set: 53 passed
analysis-forwarder: healthy, queue_depth=0, send_failures=0
Savant: healthy, current windows around 8.1fps/source
Phase 2 readiness: failed as expected
  enabled_rtsp_sources actual=1, required>=30
  runtime_source_count actual=2, required>=30
  gpu_t4_count actual=0, required>=1
  local GPUs: NVIDIA GeForce RTX 4090, NVIDIA GeForce RTX 4090
runtime health issue: compose_source_not_running
```

`doctor_midterm.sh` 失败的唯一原因是它仍然期望 `MAX_FPS_CONTROL=true`；当前 mainline
有意保持 `MAX_FPS_CONTROL=false` 和 `INGRESS_FPS_GATE_ENABLED=true`。在把 doctor
作为 release gate 之前，应先修复这个陈旧期望。

### 9.1 必需的双分片形态

不要只通过添加 60 条 camera rows 来扩展当前单栈。目标形态是两个独立的录制/分析分片：

```text
Shard A: 30 sources -> replay-a -> analysis-forwarder-a -> savant-a -> GPU0
Shard B: 30 sources -> replay-b -> analysis-forwarder-b -> savant-b -> GPU1
```

每个 shard 至少需要：

- 自己的 Replay service 和 RocksDB path；
- 自己的 analysis-forwarder metrics endpoint；
- 自己的 Savant module，并绑定到目标 GPU；
- 自己的 source-adapter target endpoint；
- 最好也有自己的 video-file-sink，用于 Replay jobs。

Redis/PostgreSQL/API/8090 可以保持共享，但所有记录和运行时决策都必须携带足够的 identity，
以便按 shard 路由和排查。

### 9.2 摄像头添加工作流

API 应继续允许运维逐个创建摄像头，但 scale test 的 runtime activation 应该分批执行。

推荐模式：

```text
daily operation: add or edit one camera at a time
scale test: activate in batches such as 5 -> 10 -> 15 -> 30 per shard
production: never jump from 2 directly to 60 without staged soak data
```

shard assignment 必须在 camera/source 激活时确定；一旦某个 `source_id` 已经产生
events/evidence，就必须保持稳定。后续如果要把 source 移到别的 shard，必须有明确的
runtime epoch 或 migration record。

### 9.3 Analysis FPS 与 Replay 录制压力

如果 8fps 使 Savant/GPU/Redis annotation traffic 过载，可以降低 analysis FPS。
需要匹配设置：

```text
8fps target: ANALYSIS_FPS=8/1 and MAX_FPS=8/1
4fps target: ANALYSIS_FPS=4/1 and MAX_FPS=4/1
```

这只会降低采样后的分析路径压力。它不会显著降低 Replay 录制写入压力，因为 Replay
在 analysis sampling 之前就存储全帧率的 encoded H264 stream。

估算的 analysis load：

```text
60 streams at 8fps ~= 480 analysis frames/s total, 240 per shard
60 streams at 4fps ~= 240 analysis frames/s total, 120 per shard
```

按 6Mbps/source 估算 Replay write load：

```text
30 streams/shard ~= 180Mbps ~= 22MB/s per shard
60 streams total ~= 360Mbps ~= 44MB/s total
300s TTL working set ~= 6.75GB/shard before RocksDB amplification
```

这主要是 storage 和 Replay-sharding 问题，不是 analysis-FPS 问题。应使用 NVMe-backed
Replay paths，并测量 write latency、EAGAIN/send timeouts、RocksDB growth 和
Replay job success rate。

### 9.4 DB 与证据 identity 要求

只有 shard identity 成为 first-class，60 路才能清晰区分。events、record requests、
evidence tasks 和 bundle metadata 的最小 durable identity 是：

```text
camera_id
source_id
shard_id
runtime_epoch_id
stream_session_id
replay_job_id
```

2026-06-18 已落地 Replay 录制分片的最小闭环能力：

- `infra/config/replay-shards.midterm.json` 固化 60 个模拟 `source_id` 的 30/30
  `replay-a` / `replay-b` 分片计划；
- `runtime_apply.py` 在显式启用 shard config 时，会把每个 source 写成
  `replay_shard_id + zmq_endpoint`；
- clip-worker 在每条 record request 上解析
  `source_id -> shard_id -> replay_api_url + replay_job_sink_url`，并把
  `replay_shard_id`、Replay API、job sink 写入 event payload 的 `media` 区域；
- 未分配的 `source_id` 会失败并写入 `replay_shard_routing_failed` 诊断，不再静默落到
  单个默认 Replay。

当前 midterm compose 默认仍保持单 Replay 兼容；只有设置
`REPLAY_SHARDS_CONFIG_PATH=/app/infra/config/replay-shards.midterm.json` 时才启用显式
分片表。原因是现有少量真实源（例如 `primary_rtsp` 和实验室源）不属于未来 60 路模拟清单，
默认强制启用会误拒当前运行源。

### 9.5 修复后的当前 clip-worker 上限

clip-worker 已改善，但还不是 60 路最终吞吐形态：

- 当前部署默认值是 `CLIP_WORKER_MAX_CONCURRENT_JOBS=4`；
- scheduling/concurrency 在 post-Savant frame proof waiting 之前检查；
- concurrency-full requests 会变成 `queued`，而不是 permanent skips；
- retryable post-Savant proof misses 可以在有界 retry budget 内 defer；
- worker 已支持 shard-aware Replay API / job sink 路由，但真实吞吐上限仍取决于
  Replay job latency、video-file-sink throughput 和 media-worker finalization time。

对 Phase 2/3，可以先用一个共享 clip-worker 解析：

```text
source_id -> shard_id -> replay_api_url + replay_job_sink_url
```

后续如果单 worker 成为瓶颈，再拆成每个 Replay shard 一个 clip-worker。并发从每个 shard
4 个 active jobs 开始。只有在测量 Replay job latency、
video-file-sink throughput、media-worker finalization time 和 bursty events 下的
evidence ready latency 之后，才提高到 6 或 8。

### 9.6 当前没有 60 路摄像头时的验证边界

没有真实 60 路摄像头时，不能宣称 60 路真实吞吐已经通过。但可以、也已经应该验证：

- 60 个模拟 `source_id` 能唯一分配到两个 Replay shard，且 30/30 平衡；
- 每个 `source_id` 都能解析出唯一的 Replay API、Replay in-stream endpoint 和 Replay job sink；
- runtime source generation 会按 source 写入对应 `replay_shard_id` 和 `zmq_endpoint`；
- clip-worker 会把 Replay job 发到存储该 source 的 shard；
- 未知 source 不会落入默认 Replay；
- DB/event payload 能保存 shard identity、Replay job request 和证据状态。

这类验证证明“未来 60 路来了以后不会混源、不会误路由、DB 能分清楚”。它不能替代后续
10 -> 30 -> 60 的真实码流写盘/IO/Replay job latency 压测。

### 9.7 2026-06-18 小规模循环源运行验证

在没有 60 路真实摄像头的条件下，已用两个循环视频 source 做了最小运行闭环验证。
验证时临时启用：

```text
source_00 -> replay-a -> video-file-sink-a
source_30 -> replay-b -> video-file-sink-b
```

已证明：

- `source_00` 只在 `replay-a` 查到 keyframe，`source_30` 只在 `replay-b` 查到 keyframe；
- 一次性 clip-worker 从独立 Redis stream 消费 record request 后，分别调用
  `http://127.0.0.1:8198` 和 `http://127.0.0.1:8298` 创建 Replay job；
- DB `events.payload.media` 中正确保存：
  `replay_shard_id`、`replay_api_url`、`replay_job_sink_url`、`replay_job_request`；
- `replay_job_request.configuration.stored_stream_id` 分别保持为 `source_00` 和 `source_30`；
- `replay_job_request.sink.url` 分别是
  `dealer+connect:tcp://video-file-sink-a:6666` 和
  `dealer+connect:tcp://video-file-sink-b:6666`；
- 两个 shard 都产生了实际 sink 输出视频文件。

运行证据样本：

```text
run=shard-e2e-10s-20260618T114545Z-e132ce10
source_00 event=ffed933c-8007-45c1-8a4b-8ffc554fc10b
  shard=replay-a api=http://127.0.0.1:8198
  sink=dealer+connect:tcp://video-file-sink-a:6666
  sink video duration=8.000000s size=3034304
source_30 event=1f2b7a00-6d60-4a44-ba85-4321c6403223
  shard=replay-b api=http://127.0.0.1:8298
  sink=dealer+connect:tcp://video-file-sink-b:6666
  sink video duration=8.000000s size=3034304
```

本次运行没有证明完整 post-Savant evidence ready。原因是该验证使用手工循环源和直接
keyframe anchor，不包含真实 Savant frame proof / event-centered metadata；media-worker
能处理输出并写回状态，但最终按现有生产证据守卫标记为 `duration_guard_failed`。这说明
post-Savant 证据生成还有自己的验证前提，不能用这次循环源测试替代真实 Savant 事件验证。

因此，本次结论限定为：Replay 录制分片、clip-worker shard-aware job routing、实际 sink
输出、DB shard identity 和证据状态写回已经通过最小运行闭环；60 路真实吞吐和完整
post-Savant evidence ready 仍需后续 staged runtime test。
