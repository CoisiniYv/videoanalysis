# Midterm 最近 24 小时性能修复完成记录

日期：2026-06-12

关联目标文档：
`docs/repair_goal/midterm_recent_24h_findings_goal_2026-06-12.md`

## 总体结论

本次 goal 已完成。当前 `midterm` 主线已经把最近 24 小时 docs 与
`specs/15_savant_performance_observability.md` 中暴露的性能和可观测性问题落地到
代码、配置、测试和运行验收中。

验收时的 git 工作区干净，仅本地分支领先远端：

```text
## c2/post-savant-poc...origin/c2/post-savant-poc [领先 20]
```

10 分钟双源运行窗口内未观察到新的性能瓶颈，最终分类为：

```text
no bottleneck observed in the run window
```

## 本次固化的修复点

### 1. 多源运行态收敛

修复内容：

- `runtime apply` 能为 enabled 非 primary 摄像头创建动态
  `video-analytics-source-*` source-adapter。
- API/Savant supervisor 增加 desired-vs-actual source adapter 状态，能暴露
  missing、stopped、stale adapter。
- 双源验收中 `primary_rtsp` 与 `lab` 对应动态 adapter 同时运行。

验收结果：

```text
expected_source_adapters:
- video-analytics-midterm-source-adapter
- video-analytics-source-source_00000000-0000-4000-8000-781078565686

missing_adapters: []
stopped_adapters: []
stale_adapters: []
source_convergence_healthy: true
```

相关提交：

- `d5b01ab Add midterm source convergence path`

### 2. 证据列表和详情展示摄像头名称

修复内容：

- API evidence resolver 返回 `camera_name`。
- 8090 evidence-viewer 和 operator evidence 页面优先展示摄像头显示名，同时保留
  `source_id` 作为技术字段。
- evidence-viewer 增加 YAML 摄像头配置读取依赖。

验收样本：

```text
camera_name=Primary RTSP Camera
source_id=primary_rtsp
clip_status=ready
visual_evidence_status=verified

camera_name=lab
source_id=source_00000000-0000-4000-8000-781078565686
clip_status=ready
visual_evidence_status=verified
```

相关提交：

- `80796c6 Expose camera names in evidence views`
- `914dec3 Declare evidence viewer YAML dependency`

### 3. Savant 性能可观测性

修复内容：

- `modules/savant_security/module.yml` 打开 telemetry metrics。
- `infra/docker-compose.midterm.yml` 暴露 Savant metrics/webserver 端口。
- 增加 `scripts/smoke/current/check_savant_perf_observability.sh`，验证 metrics 或
  项目稳定别名中能看到 per-source frame flow。

验收结果：

```text
PASS_SAVANT_PERF_OBSERVABILITY_READY
frame_flow_source=primary_rtsp
frame_flow_source=source_00000000-0000-4000-8000-781078565686
metrics_head_has_frame_counter: true
```

相关提交：

- `2bb246f Wire Savant performance observability`

### 4. clip-worker 队列和 pending 恢复

修复内容：

- worker 启动后会处理 Redis Stream pending entries，不再只读取 `">"` 新消息。
- 单并发占用时不再把正常任务永久标记为 skipped，而是延后重试。
- 增加 pending/queue safety 测试，覆盖 crash-before-ack 和 busy slot 场景。

相关提交：

- `263a6e2 Harden clip worker queue recovery`

### 5. media-worker 扫描和 probe 降本

修复内容：

- 缩小 active epoch 扫描面，避免重启后反复全量 `rglob("metadata.json")`。
- 增加扫描、probe、snapshot 处理耗时指标。
- 降低 frame-cache sidecar 扫描默认值，并按 epoch/source/camera/session 过滤。
- `services/media-worker/Dockerfile` 固化 `ffmpeg/ffprobe` 安装，避免运行时落到
  `imageio_ffmpeg` fallback。

运行说明：

- 本次验收时当前容器内已确认 `/usr/bin/ffprobe` 可用。
- 永久修复已写入 Dockerfile；后续 clean rebuild 会带上该依赖。

相关提交：

- `7908b10 Reduce media worker scan and probe overhead`

### 6. worker 查询索引

修复内容：

- 新增 `db/migrations/014_worker_media_indexes.sql`。
- 为 evidence/media worker 常用 JSONB media 状态过滤增加表达式/部分索引。
- 增加静态测试防止迁移回退。

相关提交：

- `cd58154 Add worker media query indexes`

## 验证命令

静态和契约测试：

```bash
pytest -q harness/tests/test_midterm_deployment_contract.py
pytest -q harness/tests/test_camera_runtime_apply_service.py
pytest -q harness/tests/test_midterm_replay_evidence_duration_guard.py
pytest -q harness/tests/test_midterm_replay_epoch_isolation.py
pytest -q harness/tests/test_media_worker_perf_safety.py harness/tests/test_midterm_worker_indexes_static.py harness/tests/test_clip_worker_queue_safety.py
pytest -q harness/tests/test_evidence_viewer_alarm_machine_time.py harness/tests/test_api_event_evidence_camera_name.py harness/tests/test_midterm_deployment_contract.py
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.yml
git diff --check
```

运行态和 smoke：

```bash
bash scripts/runtime/doctor_midterm.sh
bash scripts/smoke/current/check_midterm_deployment.sh
bash scripts/smoke/current/check_savant_perf_observability.sh
curl --noproxy '*' -fsS -X POST http://127.0.0.1:8090/api/v1/cameras/runtime/sources/apply
```

关键通过标记：

```text
runtime_doctor_ok=True
PASS_MIDTERM_DEPLOYMENT_CONTRACT
PASS_SAVANT_PERF_OBSERVABILITY_READY
```

## 运行验收 artifact

目录：

```text
/data/video-analytics/artifacts/perf/midterm-perf-20260612T070006Z-cd58154
```

核心文件：

```text
run_config.json
savant_metrics_head.txt
docker_stats.jsonl
nvidia_smi.jsonl
redis_streams.jsonl
source_convergence.start.json
source_convergence.json
evidence_samples.json
summary.json
restart_counts.start.txt
restart_counts.end.txt
restart_counts.final.txt
compose_ps.txt
savant_last10m.log
media_worker_last10m.log
clip_worker_last10m.log
```

`summary.json` 关键结果：

```text
source_convergence_healthy: true
restart_count_changed_during_10min_window: false
metrics_head_has_frame_counter: true
evidence_bundle_count: 50
ready_verified_named_evidence_count: 46
fatal_log_markers: []
streammux_mentions: 0
bottleneck_classification: no bottleneck observed in the run window
```

## 已知验收限制

本次 clean image rebuild 被 Docker mirror/Aliyun 403 阻断，失败点在拉取
`python:3.12-slim-bookworm`。因此运行验收采用了当前本地镜像和容器修复路径：

- 用 `--no-build --force-recreate` 应用当前 compose/env。
- 在当前 `media-worker` 容器中现场安装 `ffmpeg` 以验证 `ffprobe` 可用。
- 对 evidence-viewer 运行镜像补入已提交源码和 `PyYAML`，并基于本地镜像修正
  entrypoint/CMD。

代码层永久修复已经提交：

- evidence-viewer 的 `PyYAML` 依赖已写入
  `services/evidence-viewer/requirements.txt`。
- media-worker 的 `ffmpeg/ffprobe` 依赖已写入
  `services/media-worker/Dockerfile`。

后续在 Docker mirror 恢复后，应执行一次完整 clean rebuild 复验。

## 后续建议

1. 在可访问基础镜像时重新执行
   `docker compose -f infra/docker-compose.midterm.yml up -d --build`。
2. 继续保留 `check_savant_perf_observability.sh` 作为每次性能验收的必跑 smoke。
3. 将双源 10-15 分钟 artifact 作为下一轮性能对比基线。
4. 若后续出现 backlog 或 clip 延迟，优先查看 `redis_streams.jsonl`、
   worker timing 指标和 `summary.json` 的 bottleneck classification。
