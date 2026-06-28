# Midterm 下游证据链 60 路 3 FPS 修复与压测报告 - 2026-06-28

## 结论

本轮目标是修复推理阶段之后的 Redis、PostgreSQL、clip-worker、media-worker
和 evidence 调度性能问题，不改变现有证据存储模型：`raw_clip.mov` 仍在文件系统，
metadata / annotation / index 仍走 PostgreSQL。

最终 60 路 3 FPS 压测通过：

- Run ID：`pressure60_3fps_playabledrain_20260628T100531Z`
- Artifact：`/data/video-analytics/artifacts/pressure60_3fps_playabledrain_20260628T100531Z`
- 60 个 source 全部 active，source exited=0，restart=0，negative PTS=0
- Forwarder send failures=0
- 保留 evidence：50
- Playable evidence：50/50
- 8090 evidence API 可按 pressure source 查到保留 bundle
- Redis `XPENDING security.record_requests clip-workers-midterm` 为 0
- media-worker 容器内 `ffmpeg` / `ffprobe` 存在；非零 `imageio_ffmpeg_fallback_count` 未出现

## 本轮修复

1. Redis frame annotation 查询改为有界分页扫描。
   - 避免 60 路共用 `security.frame_annotations` 时只读固定窗口导致同源 frame anchor 被其他 source 挤掉。
   - clip-worker proof 查找、keyframe pts 查找和 media-worker sidecar writer 都保留 source / camera / runtime epoch / session 过滤。
   - clip-worker 的 stream-id 查询上界允许 frame PTS 写入后最多约 15 秒 Redis stream-id 延迟，覆盖本轮实测的写入滞后。

2. 证据 admission 和 backpressure 加入全局、按 source、按 event type 预算。
   - 低价值事件在预算超限时进入 `materialization_skipped`，保留审计。
   - `watchlist_hit` / `live_search_hit` 可绕过 per-source 限制，但仍受全局限制。
   - 目标是让下游处理可完成的证据，而不是把数千条任务全部推给 clip/media worker 后过期。

3. PostgreSQL 增加证据队列和 playable bundle 热路径索引。
   - 新 migration：`db/migrations/020_evidence_queue_playable_indexes.sql`
   - 本机已验证索引 `valid/ready`。
   - 队列排序走 `idx_evidence_tasks_materialization_priority_created`。
   - source admission count 走 `idx_evidence_tasks_active_source_status` index-only scan。

4. raw clip 可播放但 annotation 缺失时改为降级成功语义。
   - `generated_unverified` 现在映射为 `materialized`，避免把可播放视频误报成完全失败。
   - 8090 可以展示“视频可播放、标注降级/缺失”的状态，而不是把 playable bundle 丢掉。

5. 修复压测脚本 drain 判定。
   - 失败 run `pressure60_3fps_downstream_20260628T095457Z` 暴露出脚本 bug：drain 以前用 `bundles >= keep_evidence` 提前结束。
   - 当时 DB 已有 59 个 bundle，但 playable 只有 42，脚本立刻 cleanup，删除了仍在 pending/materializing 的任务。
   - 现在改为 `playable_bundles >= keep_evidence` 才结束 drain。

## 压测结果

成功 run 清理前 DB 摘要：

- cameras：60
- events：3263
- evidence_tasks：3263
- evidence_bundles：57
- playable_bundles：50
- materialized：50
- materialization_skipped：2966
- materialization_pending：201
- materializing：22
- materialization_deferred：15
- materialization_failed：7
- materialization_expired：2

清理后保留：

- pressure events：50
- evidence_tasks：50
- evidence_bundles：50
- playable_bundles：50
- intrusion：26
- watchlist_hit：24

采样摘要：

- sample_count：11
- max_forwarder_sources：60
- max_savant_sources：60
- max_savant_send_failures_total：0
- max_queue_depth：1542，后续恢复为 0
- avg effective FPS 主要在约 3.0 到 4.6 FPS，部分窗口上冲到约 5 FPS

## 剩余风险

- annotation 完整率仍需继续优化：最终 50 条中 26 条 annotation complete，24 条 `missing_frame_metadata`。这已经不是之前“全部 missing”的失败模式，但生产目标应继续提高完整率。
- Savant `validate_seq_iq` 仍很高，本轮在抽样拓扑下作为 warning 处理；只有叠加 send failure、source 退出、queue 持续积压等信号时才判为失败。
- 当前通过的是单机当前环境 60 路 3 FPS 下游证据链验证，不等价于真实 T4 生产 60 路结论。
- 仍有历史 stale `materializing` task 的自动终态收敛需要后续单独处理，避免长期污染运行态判断。

## 补充：60 路 16 FPS 配置压测的解释

2026-06-28 后续又执行了一轮 60 路、`16/1` FPS 配置压测：

- Run ID：`pressure60_16p1_20260628T112109Z`
- Artifact：`/data/video-analytics/artifacts/pressure60_16p1_20260628T112109Z`
- `run_config.json`：`fps=16/1`、`min_fps=16/1`、`batch_size=4`、`max_parallel_streams=64`
- `performance_pressure_payload.json`：`analysis_fps=16/1`、`analysis_min_fps=16/1`、`savant_max_fps=16/1`、`savant_min_fps=16/1`
- 压测脚本在压力阶段通过 compose env 下发 `BATCH_SIZE=4`，并在 restore 阶段恢复原始配置

这轮压测的证据链路通过：

- `PRESSURE_RUN_STATUS=passed`
- cleanup 前：events=3080，tasks=3080，bundles=58，playable_bundles=53
- 最终保留：50 events、50 materialized tasks、50 bundles
- 保留样本：50/50 playable，50/50 annotation complete
- Redis `security.events` / `security.record_requests` pending 最终为 0

但是，这轮不能解释为“60 路已经完成 16 FPS 推理”。采样显示：

- `max_forwarder_sources=60`
- `max_savant_sources=60`
- `max_savant_send_failures_total=0`
- `avg_effective_fps_10s` 最低约 3.677，最高约 5.210，平均约 4.386
- `forwarder_queue_depth` 最高 2048，11 个采样点里有 8 个达到 2048
- source adapter 日志显示源侧可持续约 24 FPS 读取/推送，例如 `Processed 1000 frames, 23.9x FPS`
- 最后一个采样点里，forwarder 合计 seen 约 478104 帧、forwarded 约 94500 帧、dropped 约 381555 帧，转发比例约 19.8%
- analysis-forwarder 日志出现多次 `Resource temporarily unavailable`，说明 forwarder 到 Savant 的 ZeroMQ 写入存在背压；这些重试未累计为最终 send failure，但它们说明入口已经不能稳定按目标速率排空

因此，本轮 `16/1` 的准确含义是：使用 16 FPS 配置制造更高入口和事件压力，验证后段 replay / evidence / Redis / PostgreSQL / media-worker 是否还能保住 50 条完整证据。它不是 16 FPS 推理能力验收。

### 是否是 batch size 问题

不能把这次未达到 16 FPS 简单归因为 batch size。

已确认的事实是：压力阶段 `BATCH_SIZE=4` 已经下发，不是“忘记配置 batch size”。但 `BATCH_SIZE=4` 也不等于足以支撑 60 路 * 16 FPS。它只是一个可能影响 GPU 利用率和吞吐的参数。

本轮更直接的瓶颈证据是：

- source adapter 侧有足够输入帧，约 24 FPS；
- forwarder 侧大量 drop，且 queue 多次满到 2048；
- forwarder 到 Savant 写入出现 ZeroMQ backpressure；
- Savant 侧 effective FPS 最终只有约 4 FPS 级别。

也就是说，问题发生在 source adapter 之后、Savant 完成推理之前的入口/forwarder/Savant 处理边界。batch size 可能参与影响，但不是本轮证据能单独确认的根因。要判断 batch size 是否是主因，需要单独做前段 A/B 压测，例如固定 60 路输入，分别测试 `BATCH_SIZE=4/8/16/32`，同时记录 GPU utilization、模型 batch latency、forwarder queue、forwarded/seen 比例和 Savant per-source effective FPS。

### 60 路 16 FPS 解码 + 推理压测是否合理

分两种目标看：

1. 如果生产目标只是 8 FPS 或 3 FPS 推理，那么长期解码/推理 16 FPS 是冗余的。
   - 更合理的是把 source adapter、forwarder sampler、Savant FPS 配置对齐到生产目标。
   - 入口帧率可以略高于推理目标，但不应长期远高于推理目标，否则只会放大解码、队列、丢帧、日志和 annotation 写入成本。

2. 如果生产目标明确要求 60 路 * 16 FPS 推理，那么 16 FPS 压测是合理的，但它必须作为“前段推理吞吐验收”，不能只看证据链路是否保住 50 条。
   - 合格条件应包括：source adapter 不退出、negative PTS 为 0、forwarder queue 不持续满、ZeroMQ backpressure 不持续出现、forwarded/seen 比例接近目标采样率、Savant 每路 effective FPS 接近 16、GPU 利用率和显存稳定、证据链路仍能按策略产出。
   - 当前这轮没有满足这些前段条件，所以不能作为 60 路 16 FPS 推理通过证明。

当前结论：这轮证明了后段证据链路在高入口压力下可以保住 50 条完整证据；没有证明 60 路 16 FPS 推理吞吐。下一步如果要证明 16 FPS 推理，应把测试目标改为前段吞吐，并优先定位 forwarder / Savant 入口背压和 batch 参数组合。

## 验证

- `pytest -q harness/tests/test_midterm_pressure60_script.py`
- `pytest -q harness/tests/test_clip_worker_queue_safety.py harness/tests/test_evidence_materialization_phase2plus.py harness/tests/test_media_worker_perf_safety.py harness/tests/test_midterm_deployment_contract.py harness/tests/test_midterm_worker_indexes_static.py`
- `python -m py_compile` 覆盖 pressure script、clip-worker、event-worker、media-worker 相关文件
- `git diff --check`
- 运行态压测：`pressure60_3fps_playabledrain_20260628T100531Z`，结果 `PRESSURE_RUN_STATUS=passed`
