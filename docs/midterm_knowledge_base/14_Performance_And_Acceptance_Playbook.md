---
type: performance-acceptance
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - performance
  - pressure
  - acceptance
---

# 性能与验收手册

本页把当前性能结论、压测口径、指标含义和验收门槛固化下来，避免后续把不同测试混在一起解释。

## 当前已证明的能力

| 场景 | 结论 | 限制 |
| --- | --- | --- |
| 60 路 3 FPS downstream evidence | 50/50 retained playable，8090 可查 | pressure source，不是真实 RTSP soak |
| 60 路 4 FPS 同卡双分支 | retained evidence 证据链通过 | 仍需真实 RTSP / 长时间 |
| 60 路 8 FPS 同卡双分支 | retained evidence 通过，media lifecycle p95 约 192s | pressure source，非生产硬件承诺 |
| 60 路 16/1 高入口压力 | 证据链保住样本 | 不是 16 FPS 推理证明 |
| media finalizer pacer 8 FPS | CPU peak 约 98%，50/50 playable | 单进程模型仍需真实 soak 观察 |

## Canonical 60 路单卡双分支 Profile

当前 60 路单卡双分支压测口径以
`docs/midterm_pressure60_dual1gpu_profile_2026-07-09.md` 为准。

固定项：

- `--dual-shard-same-gpu --dual-shard-gpu 0 --dual-shard-source-mode balanced`；
- 60 路拆成 `30+30`，不得退回单 Savant branch；
- `--batch-size 4 --pose-batch-size 4 --face-detector-batch-size 4 --face-embedding-batch-size 16`；
- `--duration-s 400 --drain-s 120 --pressure-algorithm-cooldown-s 60`；
- `--evidence-shard-count 4 --rolling-cache-evidence`；
- evidence window 固定为 `--evidence-policy-groups 5:5,10:10,15:15`
  和 `--evidence-group-size 20`；
- `--rolling-cache-prefill-s 25`，保证 15 秒前录窗口有缓存预热；
- 8090 visual gate 必须检查 DB-backed timeline/overlay rows、bbox、人员框和轨迹；
- 8 FPS stress profile 使用 `--fps 8/1`；
- 单 T4 生产探测 profile 使用同一套拓扑和 batch，仅把 `--fps` 改为 `4/1`。

优先使用包装脚本，避免漏掉同卡双分支或 cooldown：

```bash
bash scripts/runtime/run_pressure60_dual1gpu_profile.sh 8fps-stress
bash scripts/runtime/run_pressure60_dual1gpu_profile.sh 4fps-t4
```

## 不同测试的含义

### Forwarder null sink

目的：

- 隔离 analysis-forwarder；
- 证明 Replay -> forwarder -> null 写入能力；
- 不测 Savant 推理。

通过不代表：

- Savant 能消费同样 FPS；
- evidence 链路完成；
- GPU 足够。

### 接 Savant 单分支

目的：

- 测 forwarder 到 Savant 的真实消费；
- 观察 queue、send failures、effective FPS；
- 判断单 Savant branch 是否够。

常见结论：

- 4 FPS 可通过；
- 8 FPS 单分支容易 queue full；
- batch 调大不一定线性提升；
- effective FPS 是核心指标，不只是脚本门槛。

### 同卡双分支

目的：

- 把 60 路拆成 30+30；
- 同一张 GPU 上跑两个 Savant/forwarder branch；
- 降低单 branch 队列和 batch wait 压力。

当前结论：

- 4 FPS 和 8 FPS pressure source retained evidence 都有通过证据；
- 这是当前 4090 生产候选方向；
- 仍需真实 RTSP 混合输入和长时间 soak。

### 下游 evidence pressure

目的：

- 不只看推理，还看 event-worker、clip-worker、Replay job、video-file-sink、media-worker、8090；
- 验证 retained evidence 是否可播放、可查、可审计。

判断成功：

- source exited/restart/negative PTS 为 0；
- forwarder send failures 为 0；
- Redis pending/lag 不持续增长；
- retained evidence 达标；
- playable 50/50；
- media lifecycle p95/p99 在 deadline 内；
- 8090 list/detail proof OK。
- 视频证据只能是前后 `5s/10s/15s` 三组窗口，且实际长度接近
  `10s/20s/30s`。
- 标注不能只靠 filesystem fallback；DB overlay/timeline rows、人员框、bbox
  和轨迹都要可见。

## 指标解释

| 指标 | 含义 | 风险信号 |
| --- | --- | --- |
| source exited | source adapter 是否退出 | 大于 0 要查 RTSP/adapter |
| negative PTS | 时间戳异常 | 增长说明输入/PTS 有问题 |
| forwarder queue | forwarder 到 Savant 的等待 | 长期满说明下游消费不足 |
| send failures | ZeroMQ 写失败 | 非 0 需要排查 Savant/网络 |
| Savant effective FPS | 实际完成推理输出 FPS | 低于目标说明模型链或 batch 不够 |
| Redis pending | consumer group 未 ack | 持续增长说明 worker 卡住 |
| evidence retained | admission 后保留样本 | 太低说明 admission/事件风暴 |
| playable | raw clip 可播放 | 低于目标说明 Replay/media 链路 |
| annotation complete | overlay metadata 完整 | 可低于 playable，但要明确 degraded |
| lifecycle p95 | evidence 从任务到完成 | 超 300s 会触发 deadline 风险 |
| queue wait p95 | media-worker 等待物化时间 | 长期增长说明 finalizer 不够 |
| deadline slack | 距 deadline 剩余 | 接近 0 说明 pacer 太慢 |

## 60 路 8 FPS 验收门槛

推荐最小门槛：

- 60 source active；
- 4/8 FPS profile 明确记录；
- batch/max parallel streams 明确记录；
- runtime epoch 明确记录；
- dirty diff 明确记录；
- source exited=0；
- negative PTS=0；
- forwarder send failures=0；
- queue 不长期满；
- Savant effective FPS 接近目标；
- event-worker 无 O(N) scan；
- face-worker Redis lag 不增长；
- clip-worker pending 最终为 0；
- media-worker lifecycle p95/p99 < 300s；
- retained evidence 50/50 playable；
- 8090 proof OK。

## 真实 RTSP soak 验收

pressure source 通过后，下一步要做真实 RTSP：

- 至少混合真实摄像头和本地推流；
- 不同码率、分辨率、GOP；
- 网络抖动；
- 断流重连；
- 2-4 小时起步；
- 有条件再跑过夜。

需要额外记录：

- 每路 RTSP 输入 FPS；
- 解码失败；
- source reconnect；
- camera enabled/disabled 变更；
- evidence 生成期间是否发生 runtime apply；
- 磁盘增长；
- PostgreSQL table/index size；
- Redis memory peak。

## 生产硬件 profile

不得从 4090 直接外推到 T4。

建议矩阵：

| 硬件 | 拓扑 | 目标 |
| --- | --- | --- |
| 单 4090 | dual_same_gpu 30+30 | 60 路 8 FPS 候选 |
| 单 T4 | single 或 dual_same_gpu | 验证能否 60 路低 FPS |
| 双 T4 | dual_dual_gpu | 每卡解码+推理一组 |
| T4+3060 | 特殊拆分 | 只在预算极限时评估，注意跨 GPU 拷贝 |
| 双 4090 | dual_dual_gpu | 更高 FPS 或更高冗余 |

每个 profile 输出：

- 路数；
- FPS；
- batch；
- topology；
- GPU memory；
- GPU utilization；
- CPU peak；
- evidence latency；
- playable rate；
- annotation rate；
- 结论。

## face-worker 验收

当前已完成的注册图库查询验收：

- 60 路 8 FPS Qdrant authoritative pressure run；
- fallback count 为 0；
- Qdrant query p95/p99 为 3ms/4ms；
- exact rerank p95/p99 为 1ms/2ms；
- 8090 retained evidence proof 为 50/50；
- 5000 人 x 4 张图，即 20,000 向量 gRPC benchmark all-search p95/p99 为 4.037ms/6.427ms。

继续验收的端到端指标：

指标：

- insert p95/p99；
- gallery query p95/p99；
- exact rerank p95/p99；
- rule resolution p95/p99；
- event publish p95/p99；
- observation ACK p95/p99；
- Redis lag；
- emitted watchlist hit；
- false positive / false negative；
- threshold correctness；
- target-person filtering correctness。

下一步只有在真实 RTSP 或更高 face observation 速率下出现 ACK/pending 问题时，再考虑：

- persistence/matching 解耦；
- 独立 `security.face_match_requests`；
- 多 matcher worker；
- per-camera/person cache；
- 低质量 observation skip 策略。

Qdrant cutover 已完成门槛：

- PostgreSQL `person_gallery_embeddings` 仍是事实源；
- Qdrant collection 可以从 PostgreSQL bootstrap/reconcile；
- final authoritative run fallback count 为 0；
- Qdrant query p95/p99、outbox lag、shadow mismatch、fallback count 进入压力报告；
- `watchlist_hit` payload 和 8090 evidence 查询语义不变。

后续图库规模门槛：

- 当前已覆盖 20,000 active embeddings；
- 如果生产达到 50,000 / 100,000 active embeddings，需要复跑
  `services/face-worker/benchmark_qdrant_gallery_scale.py` 并固化 p95/p99。

## media-worker 验收

当前默认不继续大改。

需要继续记录：

- claim time；
- raw clip proof time；
- ffprobe time；
- decode/integrity time；
- ffmpeg/fallback time；
- DB terminal update time；
- cleanup time；
- lifecycle p95/p99；
- deadline slack。

升级 finalizer 的条件：

- lifecycle p95/p99 超 300s；
- materialization_expired 增长；
- retained evidence 低于目标；
- production 要求全事件物化；
- CPU 已平滑但 backlog 不下降。

## 压测报告必须包含

每次 pressure artifact 至少包含：

- run id；
- git commit；
- dirty diff summary；
- runtime epoch；
- source count；
- source generation method；
- FPS/batch/topology；
- Redis summary；
- PostgreSQL summary；
- worker CPU summary；
- forwarder/Savant metrics；
- retained evidence sample；
- 8090 proof；
- failure reason classification。

## 常见误读

- `16/1` 是入口配置，不是推理证明。
- 50/50 playable 不等于 50/50 annotation complete。
- `materialization_skipped` 大量存在不一定失败，它可能是 admission 保护。
- forwarder queue 背压不一定是 forwarder 慢，常常是 Savant 消费不足。
- 单路 batch 测试不能代表 60 路 batch。
- pressure source 不是生产 RTSP soak。
