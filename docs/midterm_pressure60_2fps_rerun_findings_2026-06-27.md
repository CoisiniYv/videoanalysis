# Midterm 60 路 2 FPS 复跑失败复盘 - 2026-06-27

## 结论

Run ID：`pressure60_2p1_20260627T145429Z`

Artifact：`/data/video-analytics/artifacts/pressure60_2p1_20260627T145429Z`

本轮 60 路 2 FPS 复跑未通过。最终生成 50 个 `evidence_bundles`，其中 43 个
具备 `raw_clip_uri` 和非零大小的可播放 raw clip，未达到“保留 50 个可在
8090 查看证据”的目标。

这次失败不是单纯 batch size 问题。失败链路分三段：

1. source adapter / RTSP 输入阶段出现负 PTS。
   - 多个 pressure source 容器日志出现：
     `OverflowError: -... not in range 0 to 18446744073709551615`。
   - 这是 Savant adapter `ffmpeg_src.py` 在把负 PTS 写入 GStreamer buffer
     时崩溃。
   - 压测使用同一个 `1080movie` RTSP 克隆 60 路，比真实 60 个独立摄像头更容易
     触发 looping / reconnect / negative PTS 问题，但仍说明当前压测入口不稳定。

2. analysis-forwarder 到 Savant ingress 早期发生背压。
   - 中段采样里 forwarder 已看到 60 路，但 queue depth 打满 255。
   - `savant_send_failures_total` 累计约 2313。
   - 后半段曾恢复到 60 路、平均约 2 FPS、queue 归零，但早期 ingress 背压已经使
     本轮不能作为稳定通过结果。

3. evidence materialization 仍未稳定达到 50 个可播放样本。
   - 清理前：363 events / 363 tasks / 50 bundles / 43 playable bundles。
   - 任务状态中有大量 `materialization_skipped`、`materialization_expired`、
     `materialization_deferred` 和 `materialization_failed`。
   - 运行中的 media-worker 镜像缺少 `ffprobe`，日志出现 `ffprobe not found`，
     只能走 `imageio_ffmpeg` fallback 探测时长，增加物化延迟。源码 Dockerfile
     已安装 `ffmpeg`，但当前运行镜像是旧构建；本轮没有执行镜像 rebuild，
     也不把 rebuild 作为普通代码修复的默认验证路径。

## 已修复

### 8090 evidence guard 误阻塞

修复位置：`services/api/app/services/runtime_apply.py`

旧逻辑会把已经终态的 evidence task 继续按 event payload 中残留的
`clip_status=pending` 视为重启阻塞。现在：

- `materialization_expired`
- `materialization_failed`
- `materialization_skipped`

这些终态 evidence 不再因为残留 `clip_status=pending` 阻塞受控重启。

对应测试：

- `harness/tests/test_camera_runtime_apply_service.py::test_evidence_guard_ignores_terminal_materialization_before_clip_pending`

### 压测脚本 DB 提交与恢复

新增脚本：`scripts/runtime/run_midterm_pressure60.py`

修复点：

- PostgreSQL 连接使用 `autocommit=True`，否则插入的 pressure cameras 对 8090 API
  不可见；
- 压测前会静默已有摄像头 source，并等待 evidence guard 清空；
- `KeyboardInterrupt` 会恢复原摄像头启停状态和性能配置；
- 结束后保留指定数量的 playable evidence，清理其余 pressure 数据；
- 压测报告会记录 sample summary、source container 状态、Savant / forwarder /
  media-worker 日志摘要；
- 通过条件不再只看 evidence 数量，还会检查 source exited、source restart、
  negative PTS、Savant send failures、`validate_seq_iq` 等入口稳定性指标。
- 新增可选 RTSP 规整转推模式，不需要 rebuild 镜像：
  - `--rtsp-republish-output-base` 指定每路转推输出的 RTSP base URL；
  - 脚本会用宿主机 `ffmpeg` 为每路 pressure camera 启动独立转推进程；
  - ffmpeg 命令包含 `+genpts`、`use_wallclock_as_timestamps` 和
    `avoid_negative_ts=make_zero`，用于验证 previous negative PTS 是否来自
    cloned / looping 压测源；
  - 转推进程日志会写入 `rtsp_republish/`，并纳入 failure reasons。

2 路 smoke 已验证脚本能把临时摄像头提交给 8090 runtime，并在结束后清理恢复。

### supervisor 不再默认反复拉起 stopped dynamic source

修复位置：

- `services/api/app/services/savant_supervisor.py`
- `infra/docker-compose.midterm.yml`

新增开关：

- `SAVANT_SUPERVISOR_AUTO_REPAIR_STOPPED_SOURCES=false`（默认）

效果：

- supervisor 仍会报告 missing / stopped / stale source 状态；
- 默认只把 missing / stale 拓扑问题交给自动 source convergence；
- stopped dynamic source 不再由后台自动反复启动，必须由 8090 的显式 source
  apply / 启停 / runtime restart 恢复；
- 避免不稳定压测源断开后被后台循环拉起，继续制造 `validate_seq_iq` 和 source
  lifecycle 噪声。

验证：

- 2 路 transcode RTSP 规整转推 smoke：
  `pressure_smoke_supervisor_norepair_20260627T154033Z`
- artifact：
  `/data/video-analytics/artifacts/pressure_smoke_supervisor_norepair_20260627T154033Z`
- 结果：
  - forwarder max sources：2；
  - Savant max sources：2；
  - forwarder queue max：0；
  - Savant send failures：0；
  - source adapter negative PTS：0；
  - 同一个退出 source adapter 日志中 `Starting the rtsp source adapter` 只出现 1 次；
  - 8090 supervisor 当前 `auto_repair_stopped_sources=false`。

本次 smoke 仍未通过 60 路目标门槛，原因是其中一路规整转推源约 36 秒后
`BrokenPipeError: Disconnected`，并且 Savant 日志仍有 `validate_seq_iq`。这说明
后台反复拉起问题已经修掉，但压测源本身还不够稳定，不能直接上 60 路结论。

## 当前未通过项

## 2026-06-28 运行态 review 新增结论

这次 review 没有改代码，只复核当前运行态和关键代码路径。结论是：2/3 FPS 复跑前，
还需要先清掉三个新的 P0，否则压测数据会被后台空转、日志噪声和证据物化镜像问题污染。

2026-06-28 本轮修复进展：

- `clip-worker` stale pending 已修复并在当前运行态验证：旧 `primary_rtsp`
  Redis pending 被识别为 `missing_db_event_and_evidence_task`，已 `XACK`，
  `XPENDING security.record_requests clip-workers-midterm` 为 0；
- `validate_seq_iq` 已先修压测判定口径：纯 analysis-forwarder 抽样造成的 seq
  gap 进入 warning，不再单独导致压测失败；若同时伴随 send failure、queue 积压、
  source 退出/重启、负 PTS 或 source 可见性失败，仍会作为 failure reason；
- media-worker 镜像问题已复核：当前容器和本地镜像仍缺 `ffmpeg` / `ffprobe`，
  Dockerfile 虽已安装，但必须通过 rebuild / 正确镜像加载 / recreate 单独处理。

### DONE：clip-worker 反复处理已不存在的 pending 请求

当前 `security.record_requests` 里仍有 1 条 Redis pending 消息，指向旧
`primary_rtsp` 事件 `0c9003a7-...`。PostgreSQL 中对应 `events` /
`evidence_tasks` 已不存在，但 `clip-worker` 仍会周期性 reclaim，并反复执行
post-Savant frame proof，日志出现 `clip_worker_pending_claimed` 和
`missing_post_savant_frame_proof`。

影响：

- 单个 `clip-worker` 进程出现约 40-80% CPU 的空转；
- Redis pending claim、PostgreSQL 查询和 evidence guard 诊断都会被旧消息污染；
- 需要在 `clip-worker` 侧识别“请求存在但 DB event/task 已清理”的情况，并把该
  Stream message 终止处理后 `XACK`，不能长期留在 PEL。

修复顺序：本项是当前第一优先级。

修复结果：

- `services/clip-worker/app/repository.py` 新增 `record_request_target_exists()`；
- `services/clip-worker/app/worker.py` 在 terminal evidence 检查之后、Replay 路由之前
  识别缺失 DB target 的孤儿请求并 `XACK`；
- 测试：
  `harness/tests/test_clip_worker_queue_safety.py::test_stale_pending_entry_without_db_target_is_acked_without_replay`；
- 运行态验证：recreate `clip-worker` 后，旧消息日志为
  `clip_worker_acked_stale_request ... reason=missing_db_event_and_evidence_task`，
  Redis pending 清零。

### PARTIAL：Savant `validate_seq_iq` 高频日志需要降噪或语义对齐

当前 Savant 约 481 条/分钟 `validate_seq_iq` WARN。forwarder 指标显示 queue
depth 为 0、send failures 为 0，因此这不是当前 ingress 背压，而更像是
analysis-forwarder 抽样后保留原始 seq_id，导致 Savant 看到 seq 不连续。

影响：

- 持续日志 I/O，会在 60 路时被放大；
- 容易把“预期抽样缺口”误判为 source adapter 丢帧或 Savant ingress 异常；
- 需要让 sampling 后的消息与 Savant seq 语义对齐，或对预期抽样缺口做降级 /
  限频，验收口径不能只看 WARN 数量。

本轮已完成：

- `scripts/runtime/run_midterm_pressure60.py` 新增 `pressure_warnings()`；
- `validate_seq_iq` 单独超阈值时输出
  `validate_seq_iq_expected_sampling_gap` warning；
- 只有叠加 forwarder send failure、queue 积压、source 退出/重启、负 PTS
  或 source 可见性失败时，才输出 `validate_seq_iq_exceeded` failure；
- 容器内确认 `VideoFrame.previous_frame_seq_id` 是只读字段，不能在
  analysis-forwarder 中做安全小改直接重写 seq。

未完成：

- Savant 日志源头的 WARN 降级、限频或协议层 seq 重写仍待后续处理。

### P0：media-worker 运行镜像仍缺 `ffmpeg` / `ffprobe`

源码 Dockerfile 已安装 `ffmpeg`，但当前运行容器里 `which ffprobe` /
`which ffmpeg` 没有输出。这是镜像层或容器复用问题，不是普通源码挂载即可修复。

影响：

- media-worker 会继续走 fallback scan / fallback probe；
- 事件风暴下 playable evidence bundle 生成可能继续落后；
- 修复需要明确 rebuild、加载正确镜像或重建容器。按当前约定，除非单独确认，
  普通代码修复不默认 rebuild。

### P1 / P2：后续清理项

- 仍有 1 条 2026-06-26 创建、deadline 已过的 `materializing` evidence task，
  需要终态化，避免污染运行态判断；
- `security.frame_annotations` 当前约 200002 条、约 307MB，60 路时需要继续验证
  retention、取证窗口和 Redis 内存之间的平衡；
- `person_bbox_observations` 与 `face_observations` 已是主要数据体量，后续需要用
  `EXPLAIN ANALYZE` 对 8090 事件 / 证据查询做针对性确认；
- 当前 live runtime 仍只有 1 个 active source，不是 60 路或双 4090 live proof；
- `runtime/overview` 的 `compose_source_not_running` 会把已 disabled 的
  `primary_rtsp` 报成问题，容易误导；后续应排除 disabled compose source 或降级为
  info。

### P0：source adapter negative PTS

需要优先处理。否则 60 路压测会出现 source 容器退出，即使后半段 Savant FPS 看起来
恢复，也不能算通过。

后续方向：

- 对 looping movie / cloned RTSP 压测源做 PTS 单调化或换成更稳定的源生成方式；
- 下一轮 2 FPS 复跑优先开启 `--rtsp-republish-output-base`，在不 rebuild 的前提下
  验证 source adapter negative PTS 是否消失；
- 当前 2 路规整转推 smoke 已消除 source adapter negative PTS overflow，但暴露出
  上游 RTSP / 转推源仍会断开，下一步需要先稳定压测源本身；
- 或在 source adapter 层显式处理负 PTS，避免 `ffmpeg_src.py` 因负 PTS 直接退出；
- 压测报告必须保留 source container 退出数和 negative PTS 日志计数。

### P0：Savant ingress 背压

60 路启动早期 forwarder queue 打满并出现大量 `WriterResultSendTimeout`。

后续方向：

- 继续确认 8090 下发的 `ANALYSIS_FPS` / `MAX_FPS` / resample 是否在最前可控点生效；
- 不应只依赖后段统计窗口判断 FPS；
- 压测通过必须满足 forwarder queue 不持续积压、`savant_send_failures_total` 不增长。

### P0：media-worker 运行态缺 `ffprobe`

源码 Dockerfile 已安装 `ffmpeg`，但当前运行镜像内 `which ffprobe` 为空。

后续方向：

- 普通代码修复优先通过挂载源码 + recreate 验证，不默认 rebuild；
- 如果最终确认必须依赖镜像内系统二进制，再单独作为镜像层变更处理，并明确执行
  rebuild / 镜像加载 / 容器 recreate；
- 验证容器内 `which ffprobe`、`which ffmpeg`，同时记录 fallback 耗时指标；
- 再复跑 2 FPS，观察 playable bundles 是否稳定达到 50。

## 当前恢复状态

复跑结束后已恢复：

- 8090 保存配置：`ANALYSIS_FPS=8/1`、`MAX_FPS=8/1`；
- `lab` 启用；
- `Primary RTSP Camera` 停用；
- pressure cameras：0；
- 当前保留本轮 43 个 playable pressure evidence，供 8090 查看。

## 下一步顺序

1. 先修 `clip-worker` stale Redis pending：请求指向的 DB event/task 已不存在时，
   终止处理并 `XACK`，避免后台长期空转。
2. 再处理 `validate_seq_iq`：区分真实丢帧、ingress 背压和 forwarder 抽样导致的
   预期 seq 缺口，避免 60 路时日志 I/O 放大。
3. 单独处理 media-worker 镜像缺 `ffmpeg` / `ffprobe`：如果必须 rebuild，要作为镜像层
   变更显式验证，不混入普通源码修复。
4. 继续修 source adapter negative PTS / cloned RTSP 压测源稳定性；当前不引入需要
   rebuild 的 patched source-adapter 镜像。下一轮用压测脚本的 RTSP 规整转推模式
   做 2 FPS 验证；必须先证明 2 路长时间不退出，再扩大到 60 路。
5. 修 Savant ingress 背压，让 60 路 2 FPS 下 forwarder queue 和 send failures 稳定。
6. 只有 2 FPS / 60 路同时满足入口稳定和 50 个 5s/5s playable evidence 后，才复跑
   3 FPS；3 FPS 通过后再上 4 FPS。
