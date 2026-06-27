# Midterm 性能优化问题清单与分阶段处理计划 - 2026-06-27

## 1. 边界

本文固化本次关于 30/60 路扩展、单 T4 运行、8090 性能控制和 Savant
推理链路的性能发现。

这些都是性能优化问题，但不是一次性全部完成的任务。后续必须按阶段拆分：

- 每个阶段单独提交、单独验证、单独回滚；
- 不把 Savant 热路径优化、Redis 写入改造、证据物化并发、批量参数调优混在同一个变更里；
- 不因为当前 2 路运行正常就宣称 30 路或 60 路已经具备生产能力；
- 不用 4090 或离线合成压力结果替代真实 T4、多路 RTSP、完整证据链路的验收结果。

本文初版只固化 backlog 和验收边界，不要求一次性完成全部优化。
2026-06-27 后续已先处理 Phase A 中两个原 P0：

- 单 shard `savant-security` 默认 `OUTPUT_FRAME` 已从 h264/nvenc 改为 `copy`；
- 生产 `modules/savant_security/module.yml` 已移除 `face_embedding_debug`、
  `same_frame_detection_debug`、`face_debug` 三个调试 PyFunc。

因此当前仍未关闭的 P0 只剩推理 FPS / resample 档位控制。该项仍按 Phase B
处理，可先由现场手工调低。

2026-06-27 继续处理前三个 P1：

- Redis exporter 已接入有界异步 Redis Stream writer；
- Savant batch / parallel-stream 参数已解除 compose 硬编码，改为 env 默认与覆盖；
- media-worker 证据物化默认从单并发改为保守有界并发。

同时 8090 运行控制面板已新增“推理性能”配置入口，可保存并应用 Forwarder
采样 FPS、Savant 入流 FPS、模型 infer interval 和 `BATCHED_PUSH_TIMEOUT`。
该入口会按配置差异重建 `analysis-forwarder` 和/或 `savant-security`，并复用
evidence restart guard，避免证据任务进行中时直接重建运行时容器。

最后一个 P1，也就是 frame annotation retention，不在本轮硬编码处理，后续仍应由
8090 运行控制面板配置。

2026-06-27 60 路 `1080movie` / 单 Savant / 2 FPS 模拟压测后新增发现：

- `BATCH_SIZE=4` 能让单 Savant 以约 2 FPS / 路跑通 60 个 pressure source，
  Forwarder queue depth 保持 0，因此当前不应盲目升到 8；
- 代码侧优先瓶颈不是 batch size，而是：
  - evidence materialization 在事件风暴下跟不上；
  - Savant ingress / source adapter 出现大量 `validate_seq_iq` warning。

后续复跑必须随机保留 50 个可查看事件/证据，避免再次清理到 8090 无样本可查。

## 2. 当前结论

### 2.1 单 T4 60 路不能按当前 8 FPS 直接扩展

当前默认设计里，单路有效分析帧率通常按 5..8 FPS 评估。60 路如果按 8 FPS
计算，总分析压力约为：

```text
60 * 8 = 480 analyzed frames/s
```

对一张 T4 来说，这个目标过激。单 T4 60 路只能作为节流运行目标重新起测，建议顺序是：

| 档位 | 单路分析 FPS | 总分析 FPS | 结论 |
| --- | ---: | ---: | --- |
| 起测档 | 3 FPS | 180 FPS | 单 T4 60 路的第一优先实测档位 |
| 保底档 | 2 FPS | 120 FPS | 3 FPS 不稳定时回退 |
| 上探档 | 4 FPS | 240 FPS | 只有 3 FPS 稳定并且证据链路无积压后再试 |
| 当前 8 FPS | 8 FPS | 480 FPS | 不应直接作为单 T4 60 路目标 |

因此，8090 端口当前配置 Savant 推理速度、`resample` / `MAX_FPS` /
`ANALYSIS_FPS` 的意义，是让现场按档位控制压力，而不是一次设置到 60 路满负载。

### 2.2 GPU 拆分要避免跨卡显存来回搬运

如果解码在 T4、推理在 3060，视频帧通常需要从 T4 显存转到主机内存，再进入
3060 显存。这个链路会引入额外拷贝、PCIe 压力和调度延迟。

更推荐的生产原则是：

- 同一个 Savant shard 内，解码和推理尽量放在同一张 GPU；
- 两张 T4 时，按 shard 拆分，让每张 T4 负责自己的解码和推理；
- 如果预算极限导致必须 T4 解码、3060 推理，需要把它当成降级方案压测，不能按零拷贝链路估算性能；
- 30/60 路容量判断以实测 `effective_fps`、GPU 利用率、队列积压和证据生成成功率为准。

## 3. 性能浪费与优化清单

| 状态 / 优先级 | 问题 | 当前证据 / 位置 | 为什么浪费性能 | 建议阶段 | 验收信号 |
| --- | --- | --- | --- | --- | --- |
| DONE（原 P0） | 单 shard Savant 未使用的 NVENC 输出 | 已修复：`infra/docker-compose.midterm.yml` 中 `savant-security`、`savant-a`、`savant-b` 的 `OUTPUT_FRAME` 均为 `copy` | Midterm 证据来自 Replay / video-file-sink，不依赖 Savant `5558` 输出视频；未使用的编码会消耗 GPU/管线资源 | Phase A | 已由 `harness/tests/test_midterm_deployment_contract.py::test_midterm_production_savant_hot_path_excludes_debug_and_unused_encoding` 固化 |
| DONE（原 P0） | Debug PyFunc 热路径成本 | 已修复：生产 `module.yml` 不再包含 `face_embedding_debug`、`same_frame_detection_debug`、`face_debug`；`SAME_FRAME_DEBUG_ENABLED` 默认 false | 每帧 Python 对象遍历、日志、JSONL 或调试输出都会放大多路延迟 | Phase A | 已由 `harness/tests/test_midterm_deployment_contract.py::test_midterm_production_savant_hot_path_excludes_debug_and_unused_encoding` 固化 |
| PARTIAL（原 P0） | 单 T4 60 路需要降分析 FPS | 8090 已支持保存/应用 `ANALYSIS_FPS`、`ANALYSIS_MIN_FPS`、`MAX_FPS`、`MIN_FPS`、模型 interval 和 `BATCHED_PUSH_TIMEOUT`；真实 T4 档位仍未压测 | 60 路 8 FPS 等于 480 analyzed frames/s，容易先压垮 GPU、Redis 或证据链路 | Phase B | 8090 已可下发档位并显示保存值/运行值/待应用；仍需真实 T4 3 FPS / 2 FPS / 4 FPS 压测报告 |
| DONE（原 P1） | Redis exporter 仍可能同步阻塞 Savant 热路径 | 已修复：`event_exporter`、`face_observation_exporter`、`person_observation_exporter`、`frame_annotation_exporter` 均使用 `AsyncRedisStreamWriter`；默认 `socket/connect timeout=50ms`，队列 `maxsize=1024` | Redis 抖动不再直接阻塞 Savant 热路径；队列满时按 drop-on-full 记录 | Phase C | 已由 `harness/tests/test_savant_redis_stream_writer.py` 和 `harness/tests/test_midterm_deployment_contract.py::test_midterm_savant_redis_exporters_are_async_and_bounded` 固化；仍需生产 Redis 故障注入压测 |
| PARTIAL（原 P1） | 批量与并发参数未针对 T4 调优 | 已处理硬编码：`BATCH_SIZE`、`POSE_BATCH_SIZE`、`FACE_DETECTOR_BATCH_SIZE`、`FACE_EMBEDDING_BATCH_SIZE`、`MAX_PARALLEL_STREAMS`、`BATCHED_PUSH_TIMEOUT` 已进入 `infra/env/midterm.env` 与 compose env 默认 | 现场可以先按 env 覆盖调参，不再改 compose；但最终 T4 operating point 仍必须实测 | Phase D | 已由 `harness/tests/test_midterm_deployment_contract.py::test_midterm_runtime_calibration_is_explicit` 固化；真实 T4 batch / latency / GPU 利用率结论仍待压测 |
| DONE（原 P1） | 证据物化仍是队列瓶颈 | 已修复默认值：`MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE=2`、`MEDIA_WORKER_MATERIALIZATION_TIMEOUT_S=180`、`MEDIA_WORKER_MATERIALIZATION_MAX_BACKLOG=200` | 不再默认单并发串行物化；同时保留 timeout/backlog guardrail，避免无限堆积 | Phase E | 已由 `harness/tests/test_midterm_deployment_contract.py::test_midterm_media_worker_materialization_defaults_are_bounded` 固化；仍需用 30/60 路压测验证 p99 evidence lifecycle |
| P0（60 路复跑新增） | Evidence materialization 在事件风暴下跟不上 | `docs/midterm_pressure60_1080movie_single_savant_report_2026-06-27.md`：60 路 / 2 FPS 下事件链路跑通，但大量任务进入 `materialization_skipped`、`materialization_expired`、`materialization_failed`，generated bundle 只有几十个量级 | 事件能产生但证据无法稳定跟上，8090 可复核样本不足，生产上会表现为告警有了但证据缺失或延迟过大 | Phase E | 复跑保留随机 50 个事件；p95/p99 evidence lifecycle 可解释；Replay job、annotation wait、ffmpeg materialization 均有分段耗时；50 个样本在 8090 可打开 |
| P0（60 路复跑新增） | Savant ingress / source adapter 序列跳变 | 同一报告记录 `validate_seq_iq` warning 20 分钟窗口约 8 万级，warning 指向 message loss 或 stream termination without EOS | 说明 60 路 2 FPS 虽然吞吐跑通，但输入链路稳定性不足；继续升 FPS 或 batch 可能掩盖丢帧/重连问题 | Phase F | 压测报告包含 `seq_warning_count_by_source`；同等 60 路 / 2 FPS 下 warning 显著下降，且 effective FPS、last-frame-age、事件数量不回退 |
| P1（保留给 8090） | Frame annotation retention 对 60 路偏小 | `FRAME_ANNOTATION_REDIS_MAXLEN=20000`、`FRAME_ANNOTATION_TTL_SECONDS=120`；60 路 8 FPS 时长度约只够 42 秒 | 检测事件存在，但证据生成时 bbox/proof annotation 可能已经被 trim 或 TTL 清理 | Phase E / 8090 | retention 按 p99 证据生命周期重新计算，并可在 8090 配置；30/60 路压测无 annotation 缺失 |
| P2 | Forwarder 公平性还没有真实 30/60 路证明 | 已有 30 路离线 forwarder 压测通过，但不是完整 RTSP + Savant + Replay + Redis 链路 | 全局队列在两路或离线合成时健康，不代表 30 路真实抖动下每路都公平 | Phase F | 每路 forwarded fps、drop ratio、last-forwarded age 都在阈值内 |
| P2 | 观测指标仍需服务于长时间压测 | 现有性能观测规格已有方向，但生产压测还需要统一采集与留档 | 没有指标就无法区分 GPU 瓶颈、Redis 抖动、Replay/证据 IO、RTSP 输入问题 | Phase F | 每次压测产出固定 artifact，包含配置、指标、日志摘要、PASS/FAIL token |

## 4. 分阶段执行建议

### Phase A - 低风险热路径清理

目标是先去掉确定无价值或只服务调试的成本。

范围：

- 关闭生产默认 `SAME_FRAME_DEBUG_ENABLED`；已完成；
- 从生产模块路径移除或 gate `face_debug`、`face_embedding_debug`；已完成；
- 确认没有 Savant `5558` 视频消费者后，将单 shard `OUTPUT_FRAME` 改为 `copy`
  或部署镜像支持的 metadata-only / disabled-output 方式；已完成，当前采用 `copy`。

Phase A 首次处理时没有同时做：

- Redis exporter 异步化，后续已作为原 P1 完成；
- batch size 调整，后续已解除硬编码，真实 T4 operating point 仍待压测；
- evidence materialization 并发调整，后续已改为保守有界默认值。

### Phase B - 8090 运行控制面板接管节流参数

目标是让现场能通过 8090 控制推理压力，而不是手工改 env 后重启整套系统。

当前状态：

- 已新增 API：
  - `GET /api/v1/runtime/performance-config`
  - `PUT /api/v1/runtime/performance-config`
  - `POST /api/v1/runtime/performance-config/apply`
- 已新增 8090 “运行控制 / 推理性能”面板，页面会展示保存值、运行值和待应用状态；
- 已支持保存并应用：
  - Forwarder：`FORWARDER_SAMPLER_ENABLED`、`ANALYSIS_FPS`、
    `ANALYSIS_MIN_FPS`
  - Savant：`INGRESS_FPS_GATE_ENABLED`、`MAX_FPS`、`MIN_FPS`、
    `POSE_INFER_INTERVAL`、`FACE_INFER_INTERVAL`、
    `FACE_EMBEDDING_INFER_INTERVAL`、`BATCHED_PUSH_TIMEOUT`
- 应用时按差异重建 `analysis-forwarder` 和/或 `savant-security`；
- Savant 重建后等待 ready；
- 应用前复用 evidence restart guard。存在进行中的证据任务时，默认阻止应用。

范围：

- 暴露并保存每个运行 profile 的 `ANALYSIS_FPS`、`MAX_FPS`、`MIN_FPS`；已完成；
- 暴露关键模型 interval，例如 pose、face detect、face embedding；已完成；
- 明确哪些参数需要受控重启 Savant shard，哪些参数可以动态生效；当前实现为重建
  forwarder/Savant 容器；
- 记录应用前后的配置快照；已通过保存值、运行值和 diff 返回。

未完成：

- 真实 T4 上的 2/3/4 FPS operating point；
- frame annotation retention 的 8090 配置入口；
- apply 后的长期 runtime 指标留档和 PASS token。

单 T4 60 路起测建议：

```text
ANALYSIS_FPS=3/1
MAX_FPS=3/1
MIN_FPS=1/1 或 2/1
```

如果证据链路积压或 GPU 超时，再降到 2 FPS。只有 3 FPS 连续稳定后，才上探
4 FPS。

### Phase C - Redis exporter 热路径隔离

目标是让 Redis 抖动不阻塞 Savant `process_frame`。

范围：

- 所有 Redis exporter 增加显式 connect/socket timeout；已完成；
- 将 `XADD` 放到有界异步队列或非阻塞 writer；已完成；
- 队列满时按可观测的 drop-on-full 策略处理；已完成；
- 区分正常压力下不应丢、故障注入下允许丢但必须标记 degraded。

验收重点不是“Redis 永远不丢”，而是 Redis 慢或短暂不可用时，推理链路不被拖死。

### Phase D - T4 batch / concurrency operating point

目标是在真实 T4 上找到稳定吞吐和延迟平衡点。

当前已完成的是解除硬编码：compose 不再把 `BATCH_SIZE`、`POSE_BATCH_SIZE`、
`FACE_DETECTOR_BATCH_SIZE`、`FACE_EMBEDDING_BATCH_SIZE` 写死，默认值统一放在
`infra/env/midterm.env`。这不是 T4 结论，不能替代真实压测。

范围：

- 验证 pose、face detector、face embedding 的 TensorRT engine 是否支持候选 batch；
- 分别测试 `BATCH_SIZE`、`POSE_BATCH_SIZE`、`FACE_DETECTOR_BATCH_SIZE`、
  `FACE_EMBEDDING_BATCH_SIZE`；
- 调整 `MAX_PARALLEL_STREAMS` 和 `BATCHED_PUSH_TIMEOUT`；
- 把最终参数写入 T4 operating point 文档。

禁止用 4090 结果直接替代 T4 结论。

### Phase E - 证据物化与 annotation retention

目标是检测成功后，证据生成不能被后处理队列和 Redis retention 拖垮。

当前已完成的是证据物化默认值调整：media-worker 默认 `max_active=2`、
`timeout=180s`、`max_backlog=200`。frame annotation retention 保留给 8090
配置化，不在本轮写死。

范围：

- 根据 p99 evidence lifecycle、Replay post-window、proof budget、headroom 反推
  annotation stream maxlen / TTL；
- 评估是否需要按 shard 拆分 frame annotation stream；
- 调整 `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE` 前先测 ffmpeg CPU/IO；
- 证据生成过载时必须有可见状态，而不是静默缺 bbox、缺 proof。

### Phase F - 30/60 路真实压测与留档

目标是最终证明每个 shard、每张 GPU、每个源在真实压力下都可解释。

范围：

- 30 路单 shard T4 实测；
- 60 路双 shard 或单 T4 降 FPS 实测；
- 每路 forwarded fps、drop ratio、last-frame-age；
- GPU decode/infer 使用率；
- Redis queue/write latency；
- evidence lifecycle p50/p95/p99；
- 8090 页面看到的配置与运行时真实配置一致。

压测 artifact 建议统一存放：

```text
/data/video-analytics/artifacts/perf/<run_id>/
```

## 5. 验收边界

以下说法在没有对应实测前都不能成立：

| 声明 | 必需证据 |
| --- | --- |
| 单 T4 可跑 30 路 | 真实 T4、30 路 RTSP 或等价压力、完整 Savant + Replay + Redis + evidence 链路、连续运行报告 |
| 单 T4 可跑 60 路 | 明确低 FPS 档位、完整链路压测、证据成功率和延迟达标 |
| 双 T4 可跑 60 路 | 两个 shard 都有独立 decode+infer 压测结果，且跨 shard 证据链路不互相拖垮 |
| Redis 不影响推理 | Redis 慢/断故障注入下，Savant FPS 和 source-adapter restart count 仍稳定 |
| 证据不会丢 | evidence lifecycle、annotation retention、materialization queue 三者同时达标 |

对应 PASS token 也必须谨慎使用：

- `PASS_PHASE2_SINGLE_T4_30`：只能由真实 T4 30 路完整链路压测产生；
- `PASS_PHASE3_DUAL_T4_60`：只能由双 shard / 双 T4 60 路完整链路压测产生；
- 单 T4 60 路如果采用 2-3 FPS 降级运行，应使用单独 token，不要冒充 8 FPS 生产满配。

## 6. 关联文档

- `specs/15_savant_performance_observability.md`
- `specs/16_dual_path_30x2_t4_production_optimization.md`
- `specs/21_replay_evidence_io_optimization_60_stream_production.md`
- `specs/22_midterm_60_stream_readiness_risk_closure_plan.md`
- `docs/program_healthcheck_r1.md`
- `docs/midterm_analysis_forwarder_30_stream_offline_pressure_2026-06-26.md`
