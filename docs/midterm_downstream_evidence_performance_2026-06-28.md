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

## 验证

- `pytest -q harness/tests/test_midterm_pressure60_script.py`
- `pytest -q harness/tests/test_clip_worker_queue_safety.py harness/tests/test_evidence_materialization_phase2plus.py harness/tests/test_media_worker_perf_safety.py harness/tests/test_midterm_deployment_contract.py harness/tests/test_midterm_worker_indexes_static.py`
- `python -m py_compile` 覆盖 pressure script、clip-worker、event-worker、media-worker 相关文件
- `git diff --check`
- 运行态压测：`pressure60_3fps_playabledrain_20260628T100531Z`，结果 `PRESSURE_RUN_STATUS=passed`
