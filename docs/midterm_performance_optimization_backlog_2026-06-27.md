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

最后一个 P1，也就是 frame annotation retention，原计划后续交给 8090 配置；
但 3 FPS 复跑失败后，为避免复跑时 annotation 过早过期，本轮先提高默认
retention。长期仍应再做成 8090 可配置项。

2026-06-27 60 路 `1080movie` / 单 Savant / 2 FPS 模拟压测后新增发现：

- `BATCH_SIZE=4` 能让单 Savant 以约 2 FPS / 路跑通 60 个 pressure source，
  Forwarder queue depth 保持 0，因此当前不应盲目升到 8；
- 代码侧优先瓶颈不是 batch size，而是：
  - evidence materialization 在事件风暴下跟不上；
  - Savant 日志出现大量 `validate_seq_iq` warning；复盘后确认这在
    analysis-forwarder 抽样拓扑下不能单独等同 source adapter 崩溃或证据输入丢帧。

后续复跑必须随机保留 50 个可查看事件/证据，避免再次清理到 8090 无样本可查。

2026-06-27 3 FPS 复跑失败后，先完成一轮代码侧修复，等待重新压测验证：

- 动态 source adapter 默认 `restart_policy=no`，不再 `unless-stopped` 自动复活；
- source adapter 默认 `EOS_ON_START=true`，重建/重连时先给 Savant 明确 source
  生命周期边界；
- 8090 performance apply 只重建真正消费 FPS / interval 参数的
  analysis-forwarder 和 Savant。source adapter 保持 full-rate 写入 Replay，
  不再下发误导性的 `MAX_FPS/MIN_FPS` env，因为 `rtsp.sh` 不消费这些参数，
  且证据链要求 Replay 保留全量输入；
- media-worker metadata 扫描默认上限从 2000 提高到 20000，并按 mtime 新文件优先，
  避免大目录中旧 metadata 抢占扫描窗口；
- frame annotation retention 默认从 `120s / 20000` 提高到 `600s / 200000`，
  `EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS` 同步提高到 600 秒。

这些修复只说明前置代码缺陷已处理，60 路 2 FPS / 3 FPS 是否通过仍以重新压测为准。

2026-06-28 运行态 review 后，新增当前必须先处理的阻塞项：

- `clip-worker` 正在反复 reclaim 一条已不存在 DB event/task 的
  `security.record_requests` pending 消息，造成 CPU 空转和 evidence guard 诊断污染；
- Savant 高频 `validate_seq_iq` 更像是 forwarder 抽样导致的 seq 缺口，而不是当前
  queue / send failure 背压，需要做语义对齐或日志降噪；
- media-worker 当前运行镜像仍缺 `ffmpeg` / `ffprobe`，源码 Dockerfile 已修但容器
  不是新镜像，必须作为镜像层问题单独处理；
- 仍有陈旧 `materializing` evidence task、frame annotation 常驻成本、observation
  表增长和 `runtime/overview` disabled compose source 误报等后续清理项。

因此下一轮复跑前，当前优先级调整为：先修 clip-worker stale pending，再处理
`validate_seq_iq` 抽样语义 / 降噪，再单独确认 media-worker 镜像二进制，最后回到
2 FPS / 60 路压测。

2026-06-28 本轮已开始修复：

- `clip-worker` 已新增 stale record request 保护：当 Redis 请求指向的
  PostgreSQL `events` / `evidence_tasks` 都不存在时，直接记录
  `clip_worker_acked_stale_request` 并 `XACK`，不再进入 Replay / proof 重试；
- 运行态已 recreate `clip-worker`（无 rebuild），旧 `primary_rtsp`
  pending 消息已被 ack，`XPENDING security.record_requests clip-workers-midterm`
  已从 1 降为 0；
- 压测脚本已调整 `validate_seq_iq` 验收语义：纯抽样导致的 seq gap 记录为
  warning，只有同时出现 send failure、queue 积压、source 退出/重启、负 PTS
  或 source 可见性失败时才作为 failure reason；
- 容器内确认 `VideoFrame.previous_frame_seq_id` 是只读字段，当前不能安全在
  analysis-forwarder 里重写 Savant seq，因此 Savant 日志源头降噪仍是后续项；
- 当时 media-worker 运行容器和本地 `video-analytics-midterm-media-worker:latest`
  镜像缺 `ffmpeg` / `ffprobe`。Dockerfile 已包含安装命令，但该问题必须通过
  rebuild / 正确镜像加载 / recreate 单独修复，不能靠普通 Python recreate 解决。

2026-06-28 下游证据链路修复后，60 路 3 FPS 已有一次通过结果：

- 成功 run：`pressure60_3fps_playabledrain_20260628T100531Z`
- Artifact：`/data/video-analytics/artifacts/pressure60_3fps_playabledrain_20260628T100531Z`
- 60 个 source 全部 active，source exited=0，restart=0，negative PTS=0；
- Forwarder send failures=0；
- 清理后保留 50 条 pressure evidence，50/50 raw clip playable；
- 压测脚本 drain 条件已从 `bundles >= keep_evidence` 修正为
  `playable_bundles >= keep_evidence`，避免 total bundle 够但 playable 不够时提前 cleanup；
- 本轮完整报告见
  `docs/midterm_downstream_evidence_performance_2026-06-28.md`。

仍需继续跟踪：最终 50 条中 26 条 annotation complete、24 条
`missing_frame_metadata`。这已不是之前“全部 missing”的失败模式，但 annotation
完整率仍是 Phase E 后续优化项。

2026-06-28 另有一次只读静态 review 固化了推理后链路的下一组待验证瓶颈：

- face-worker 仍是单 consumer 同步链路：单条 Postgres insert 后同步做
  watchlist / pgvector gallery search，且 face/gallery embedding 目前未建 ANN
  vector index；
- event-worker 的 `RecordRequestPublisher.has_request()` 仍对
  `security.record_requests` 做全量 `XRANGE - +` 去重，事件量上来后是 O(N)
  Redis/CPU 热点；
- media-worker 的 `MEDIA_WORKER_MATERIALIZATION_MAX_ACTIVE` 是本进程内 guard，
  当前不是 finalizer 线程池或进程池，ffprobe/ffmpeg/decode 校验仍主要受单进程
  轮询链路约束。

该 review 只固化静态发现，不替代 60 路压测结论。完整记录见
`docs/midterm_post_inference_bottleneck_static_review_2026-06-28.md`。

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
| DONE（2026-06-28 review 新增） | clip-worker 处理已不存在 DB event/task 的 Redis pending 请求 | 已修复：缺失 event/task 的 record request 会记录 `clip_worker_acked_stale_request` 并 `XACK`；运行态验证旧 `primary_rtsp` pending 已清零 | 不再每 5 秒 reclaim 旧消息，CPU、Redis、PostgreSQL 空转和 runtime 诊断污染解除 | Phase E | 已由 `harness/tests/test_clip_worker_queue_safety.py::test_stale_pending_entry_without_db_target_is_acked_without_replay` 固化；运行态 `XPENDING security.record_requests clip-workers-midterm` 为 0 |
| PARTIAL（2026-06-28 review 新增） | 抽样后 Savant `validate_seq_iq` 高频 WARN | 已修复压测误判：纯 sampling seq gap 进入 warning，叠加 send failure、queue/source 异常才进入 failure；容器内确认 `previous_frame_seq_id` 只读，不能安全重写 | 避免把预期抽样缺口误判为 60 路压测失败；但 Savant 日志源头降噪仍未彻底关闭 | Phase F | 已由 `harness/tests/test_midterm_pressure60_script.py` 固化 warning/failure 分类；后续仍需 Savant 日志等级、限频或协议层支持 |
| DONE（2026-06-28 review 新增，镜像层） | media-worker 运行容器缺 `ffmpeg` / `ffprobe` | 当前容器内已确认 `/usr/bin/ffmpeg` 和 `/usr/bin/ffprobe` 存在；60 路 3 FPS 成功 run 中未出现非零 `imageio_ffmpeg_fallback_count` | 证据物化不再因缺二进制进入 imageio fallback 探测路径 | Phase E / Deploy | 容器内 `which ffprobe`、`which ffmpeg` 有输出；`pressure60_3fps_playabledrain_20260628T100531Z` 保留 50/50 playable evidence |
| DONE / FOLLOW-UP（60 路复跑新增） | Evidence materialization 在事件风暴下跟不上 | 已处理 Redis 有界分页、admission budget、PostgreSQL 热路径索引、`generated_unverified` 降级语义和压测 drain 判定；`pressure60_3fps_playabledrain_20260628T100531Z` 达到 50/50 playable | 60 路 3 FPS 下可生成可播放证据；但 annotation 完整率仍需提高，最终 50 条中 26 complete、24 missing metadata | Phase E | 复跑已保留 50 条 playable evidence；后续验收应继续提高 annotation complete 比例并保留 p95/p99 evidence lifecycle 分段耗时 |
| PARTIAL（60 路复跑新增） | 抽样拓扑下的输入稳定性验收口径不清 | 代码侧已处理：动态 source 默认 `restart_policy=no`、`EOS_ON_START=true`、停用/删除动态 source 使用 `rm -f`；同时撤回 source FPS env 同步，明确 source adapter full-rate 写 Replay、analysis-forwarder/Savant 负责抽样与推理速度控制。3 FPS 失败报告中 `validate_seq_iq=51804`，但该指标会被 forwarder 抽样天然放大 | 继续把 `validate_seq_iq` 当唯一 P0 会误判 forwarder 设计内丢分析帧；真正要看 source restart、forwarder send failure、effective FPS、Replay/video-file-sink metadata 和证据 bundle | Phase F | 压测报告包含 forwarder `seen/forwarded/dropped/send_failures`、source restart、Replay/video-file-sink metadata、bundle 成功率；`validate_seq_iq` 仅作为辅助日志 |
| DONE（原 P1） | Frame annotation retention 对 60 路偏小 | 已提高默认值：`FRAME_ANNOTATION_REDIS_MAXLEN=200000`、`FRAME_ANNOTATION_TTL_SECONDS=600`，并同步 `EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS=600` | 60 路 3 FPS 下约 180 frame annotations/s，200000 长度约覆盖 18 分钟，先满足复跑验证窗口 | Phase E / 8090 | 已由部署契约固化；长期仍可再做 8090 可配置化 |
| P1（2026-06-28 review 新增） | 陈旧 `materializing` evidence task 未终态化 | 仍有 1 条 2026-06-26 创建、deadline 已过的 active task，事件 payload 仍是 `media_status=materializing` / `clip_status=replay_job_created` | 虽然 runtime guard 可把过期 active task 视为 stale，但它会污染运行态判断，说明终态收敛还有漏网路径 | Phase E | deadline 已过的 active materialization 能自动收敛为 terminal/stale；runtime overview 不再把旧任务当活动证据 |
| P1（2026-06-28 review 新增） | Redis frame annotation 常驻成本偏高 | `security.frame_annotations` 约 200002 条、约 307MB | 60 路时如果按源放大 maxlen 会快速膨胀；如果全局固定，需要确认取证窗口是否仍足够 | Phase E / 8090 | retention 由实际 p99 evidence lifecycle 反推；Redis 内存、跨源查询和证据命中窗口都在阈值内 |
| P1（2026-06-28 review 新增） | observation 表增长与 8090 查询成本需验证 | `person_bbox_observations`、`face_observations` 已约 1GB 级，部分事件索引读 tuple 很高 | 长跑后 8090 列表/证据查询可能成为数据库瓶颈 | Phase F | 对 8090 事件列表、证据详情、按 camera 查询补 `EXPLAIN ANALYZE`，必要时补索引或归档策略 |
| P2 | Forwarder 公平性还没有真实 30/60 路证明 | 已有 30 路离线 forwarder 压测通过，但不是完整 RTSP + Savant + Replay + Redis 链路 | 全局队列在两路或离线合成时健康，不代表 30 路真实抖动下每路都公平 | Phase F | 每路 forwarded fps、drop ratio、last-forwarded age 都在阈值内 |
| P2 | 观测指标仍需服务于长时间压测 | 现有性能观测规格已有方向，但生产压测还需要统一采集与留档 | 没有指标就无法区分 GPU 瓶颈、Redis 抖动、Replay/证据 IO、RTSP 输入问题 | Phase F | 每次压测产出固定 artifact，包含配置、指标、日志摘要、PASS/FAIL token |
| P2（2026-06-28 review 新增） | `runtime/overview` 把 disabled compose source 报为 not running | `primary_rtsp` 当前 disabled，但 overview 仍可出现 `compose_source_not_running`；supervisor dynamic source convergence 实际 healthy | 会误导 8090 运行态排查，让用户以为未启用摄像头也是故障 | 8090 / Runtime Health | disabled compose source 从健康问题中排除或降级为 info；active source 健康仍严格告警 |

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
`timeout=180s`、`max_backlog=200`。本轮为复跑先把 frame annotation retention
默认值提高到 `600s / 200000`，长期仍应纳入 8090 配置化。

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
