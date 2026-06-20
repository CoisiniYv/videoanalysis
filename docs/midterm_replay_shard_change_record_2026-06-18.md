# Replay 双分片录制最小闭环修改记录

日期：2026-06-18

## 背景

本次修改面向未来 60 路摄像头的 Replay 录制压力和证据路由正确性。当前没有 60 路真实摄像头，因此本次验收范围限定为：

- 60 个模拟 `source_id` 的双 Replay shard 分片计划；
- `source_id -> replay shard` 的一等路由能力；
- `runtime_apply` 动态源生成的 shard-aware 输出；
- `clip-worker` 按实际存储 shard 创建 Replay job；
- DB/event payload 能记录 shard identity、Replay job 请求和证据状态；
- 少量循环源验证实际 Replay 录制、job routing 和 sink 输出。

本次修改不能替代真实 60 路码流吞吐压测，也不能宣称 `PASS_PHASE3_DUAL_T4_60` 已完成。

## 主要修改

### 1. Replay shard 配置与解析

新增：

- `services/api/app/services/replay_shards.py`
- `services/clip-worker/app/replay_shards.py`
- `infra/config/replay-shards.midterm.json`

能力：

- 支持 `REPLAY_SHARDS_JSON` 和 `REPLAY_SHARDS_CONFIG_PATH`；
- shard 配置包含 `shard_id`、Replay API URL、Replay in-stream endpoint、Replay job sink URL；
- 默认未配置时保持单 Replay 兼容；
- 显式启用 shard 配置后，未知 `source_id` fail closed，不静默落到默认 Replay；
- `infra/config/replay-shards.midterm.json` 固化 60 个模拟源的 30/30 分片：
  - `source_00..source_29 -> replay-a`
  - `source_30..source_59 -> replay-b`

### 2. runtime_apply 动态源分片

修改：

- `services/api/app/services/runtime_apply.py`

能力：

- `_build_sources_doc()` 在启用 shard 配置时按 `source_id` 解析 shard；
- 每个 source 写入对应的 `zmq_endpoint`；
- 每个 source 附带 `replay_shard_id`；
- runtime apply 返回 `replay_shards` 摘要，便于确认当前运行计划。

这避免了未来所有 source 都写入单一 `replay-service:5555`。

### 3. clip-worker shard-aware Replay job routing

修改：

- `services/clip-worker/app/config.py`
- `services/clip-worker/app/worker.py`
- `services/clip-worker/app/repository.py`

能力：

- `Config` 新增 `replay_shards`；
- 每条 record request 按 `source_id` 解析 Replay shard；
- `ReplayClient` 按 `replay_api_url` 缓存；
- `create_job()` 使用该 shard 的 `replay_job_sink_url`；
- 未分配 source 直接写 failed，`evidence_reason="replay_shard_routing_failed"`；
- `update_clip_status()` 将 `replay_shard_id`、`replay_api_url`、`replay_job_sink_url` 和 Replay job request 写入 `events.payload.media`；
- queued、deferred、failed 路径保留 shard 诊断字段。

这使 DB/event payload 能回答两个关键问题：

- 该事件来自哪个 `source_id`；
- 该事件的 Replay job 是否发到了实际存储该 source 的 Replay shard。

### 4. midterm compose 双 Replay 表达

修改：

- `infra/docker-compose.midterm.yml`
- `scripts/runtime/video_file_sink_entrypoint.sh`

新增 compose profile：

- `dual-replay-shards`

新增服务：

- `replay-a`
- `replay-b`
- `video-file-sink-a`
- `video-file-sink-b`

关键配置：

- `replay-a` host API port：`8198`
- `replay-b` host API port：`8298`
- `replay-a` RocksDB：`/data/video-analytics/replay-midterm-a`
- `replay-b` RocksDB：`/data/video-analytics/replay-midterm-b`
- API 和 clip-worker 挂载 `infra/config`
- `REPLAY_SHARDS_CONFIG_PATH` 默认保持空值，避免误拒当前真实源；
- 显式设置 `REPLAY_SHARDS_CONFIG_PATH=/app/infra/config/replay-shards.midterm.json` 时启用双分片表。

`video_file_sink_entrypoint.sh` 增加：

- `VIDEO_FILE_SINK_EPOCH_ROOT`
- `VIDEO_FILE_SINK_REUSE_CURRENT_EPOCH=true`

用于让 per-shard sink 复用当前 runtime epoch，避免多个 sink 启动时互相覆盖 `.current_epoch.json`。

### 5. 验证脚本和测试

新增：

- `scripts/runtime/check_replay_shard_plan.py`
- `harness/tests/test_replay_shard_routing.py`

更新：

- `harness/tests/test_clip_worker_queue_safety.py`
- `harness/tests/test_midterm_deployment_contract.py`
- `harness/tests/archive/phase-only/20260610/test_c1i1f_person_bbox_observation_stream.py`

覆盖范围：

- shard 配置解析；
- 60 个模拟 `source_id` 唯一分片；
- 30/30 平衡；
- runtime apply 生成正确 `ZMQ_ENDPOINT`；
- clip-worker Replay job 发到正确 Replay API 和 sink；
- 未知 `source_id` fail closed；
- DB/event payload 保留 shard/job 诊断字段；
- midterm compose 能渲染双 Replay 和双 sink 服务。

### 6. 文档和规格更新

修改：

- `docs/midterm_lab_alarm_and_60_stream_readiness_findings_2026-06-16.md`
- `specs/16_dual_path_30x2_t4_production_optimization.md`

固化内容：

- 当前没有 60 路摄像头时的验证边界；
- 60 个模拟 `source_id` 的分片能力；
- 小规模循环源运行验证；
- Replay 录制分片、clip-worker shard-aware routing、DB shard identity 已形成最小闭环；
- 真实 60 路吞吐、Replay RocksDB 写入延迟、video-file-sink/media-worker burst capacity 仍需后续 staged pressure test；
- 未来添加摄像头可以一个一个添加，但每次添加都必须立即固化 shard assignment；
- 批量导入 60 路前，必须先生成并审查完整分片计划，再 apply。

## 已运行验证

### 静态和单元测试

```bash
python -m py_compile \
  services/api/app/services/replay_shards.py \
  services/api/app/services/runtime_apply.py \
  services/clip-worker/app/replay_shards.py \
  services/clip-worker/app/config.py \
  services/clip-worker/app/worker.py \
  services/clip-worker/app/repository.py \
  scripts/runtime/check_replay_shard_plan.py
```

结果：通过。

```bash
pytest -q \
  harness/tests/test_replay_shard_routing.py \
  harness/tests/test_clip_worker_queue_safety.py::test_replay_job_routes_to_source_shard \
  harness/tests/test_clip_worker_queue_safety.py::test_unknown_source_fails_before_replay_job \
  harness/tests/test_camera_runtime_apply_service.py \
  harness/tests/test_midterm_deployment_contract.py
```

结果：`38 passed in 0.40s`。

### 60 个模拟源分片计划

```bash
python scripts/runtime/check_replay_shard_plan.py
```

结果：

```json
{
  "status": "PASS_REPLAY_SHARD_PLAN",
  "source_count": 60,
  "shard_counts": {
    "replay-a": 30,
    "replay-b": 30
  },
  "max_imbalance": 0
}
```

### compose 双分片渲染

```bash
REPLAY_SHARDS_CONFIG_PATH=/app/infra/config/replay-shards.midterm.json \
docker compose --profile dual-replay-shards \
  -f infra/docker-compose.midterm.yml config
```

结果：通过。渲染结果包含：

- `replay-a`
- `replay-b`
- `video-file-sink-a`
- `video-file-sink-b`
- API/clip-worker 的 `REPLAY_SHARDS_CONFIG_PATH`
- `replay-a` host port `8198`
- `replay-b` host port `8298`

### diff 格式检查

```bash
git diff --check
```

结果：通过。

## 小规模循环源运行验证

在没有真实 60 路摄像头的条件下，已用两个循环视频 source 做最小运行闭环：

```text
source_00 -> replay-a -> video-file-sink-a
source_30 -> replay-b -> video-file-sink-b
```

验证结果：

- `source_00` 只在 `replay-a` 查到 keyframe；
- `source_30` 只在 `replay-b` 查到 keyframe；
- 一次性 clip-worker 从独立 Redis stream 消费 record request；
- Replay job 分别调用 `http://127.0.0.1:8198` 和 `http://127.0.0.1:8298`；
- DB `events.payload.media` 正确保存 shard 和 job 信息；
- 两个 shard 都产生实际 `video.mov` sink 输出。

样本：

```text
run=shard-e2e-10s-20260618T114545Z-e132ce10

source_00
  event=ffed933c-8007-45c1-8a4b-8ffc554fc10b
  shard=replay-a
  api=http://127.0.0.1:8198
  sink=dealer+connect:tcp://video-file-sink-a:6666
  video duration=8.000000s
  video size=3034304

source_30
  event=1f2b7a00-6d60-4a44-ba85-4321c6403223
  shard=replay-b
  api=http://127.0.0.1:8298
  sink=dealer+connect:tcp://video-file-sink-b:6666
  video duration=8.000000s
  video size=3034304
```

边界：该运行没有证明完整 post-Savant evidence ready。原因是循环源验证没有真实 Savant frame proof / event-centered metadata；media-worker 最终可能按现有生产证据守卫标记为 `duration_guard_failed`。本验证只证明 Replay 录制分片、Replay job routing、sink 输出和 DB shard identity。

## 当前结论

本次修改已经完成 60 路录制前置能力的最小闭环：

- 60 个模拟 source 能清晰分配到两个 Replay shard；
- runtime apply 能为每路 source 生成明确的 Replay 写入目标；
- clip-worker 能把 Replay job 发到存储该 source 的 shard；
- 未知 source 不会误写默认 Replay；
- DB/event payload 能保存 shard identity 和 Replay job request；
- 小规模循环源证明两个 shard 可以分别录制并输出 clip。

仍不能声明：

- 真实 60 路 RTSP 持续录制已经通过；
- 8fps 推理吞吐已经满足 60 路；
- Replay RocksDB 写入延迟在 60 路下可接受；
- video-file-sink/media-worker 能承受 60 路突发 evidence job；
- 完整 post-Savant evidence ready 已在双分片下通过。

## 后续计划

下一步应单独执行 staged runtime pressure test：

1. 10 路：验证 source attach、Replay write、Replay job latency、DB shard identity、clip 输出。
2. 30 路：验证单 shard 上限，测量 RocksDB、sink、media-worker 和 clip-worker latency。
3. 60 路：启用双 shard，验证 30/30 分片、跨 shard 隔离、重连故障注入和 evidence 完整性。

如果 8fps analysis 压力过大，可以把 analysis fps 降到 4fps；但 Replay 录制分片验证优先关注 full-rate 录制、source 区分和 DB 正确存储，不能用 analysis fps 降级掩盖 Replay 写入能力问题。
