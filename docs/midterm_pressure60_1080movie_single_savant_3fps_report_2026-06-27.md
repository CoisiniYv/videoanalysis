# Midterm 60 路 1080movie 单 Savant 3 FPS 压测报告

日期：2026-06-27  
Run ID：`pressure60_3fps_20260627T125357Z`  
Artifact：`/data/video-analytics/artifacts/pressure60_3fps_20260627T125357Z`

## 结论

本轮 60 路 `1080movie` 单 Savant、`BATCH_SIZE=4`、目标 3 FPS 压测没有达到稳定生产态。前两轮 8090 runtime metrics 接近 3 FPS，但中间采样掉到 0.1 FPS，最后一轮又冲到 4.49 FPS，说明运行链路存在明显抖动。Savant 日志中出现 `validate_seq_iq` 51804 次，60 路压测 source 基本都有大量序列跳变，符合“源适配器/Ingress 消息丢失或无 EOS 终止”的瓶颈判断。

证据链路方面，本轮最终没有生成任何 `evidence_bundles`。压测停止后的第一次统计为 461 个事件、466 个 evidence task，其中 222 expired、110 pending、134 skipped；worker drain 到清理前 DB 累计达到 1014 个事件/任务，但 bundle 仍为 0。结论是 3 FPS/60 路下不仅输入链路不稳定，证据物化链路也没有在事件风暴下完成可播放证据生成。

## 压测配置

- RTSP 输入：`rtsp://192.168.1.105:8554/live/1080movie`
- 临时摄像头：60 路，`source_id` 前缀 `pressure60_3fps_20260627T125357Z_`
- Savant/forwarder：单路径，`BATCH_SIZE=4`，`MAX_PARALLEL_STREAMS=64`
- 节流：`MAX_FPS=3/1`，`MIN_FPS=1/1`，`ANALYSIS_FPS=3/1`，`ANALYSIS_MIN_FPS=1/1`
- 证据策略：`pre_seconds=5`，`post_seconds=5`，`clip_required=true`，`snapshot_required=false`
- 人脸名单：Reese、Finch

## 采样结果

`batch4_fps3_sample_summary.txt` 重建后的 7 轮采样如下：

| sample | active sources | avg effective fps 10s | min | max |
|---|---:|---:|---:|---:|
| runtime_0 | 48 | 2.96 | 0.10 | 3.80 |
| runtime_1 | 48 | 2.96 | 0.10 | 3.80 |
| runtime_2 | 49 | 0.10 | 0.10 | 0.10 |
| runtime_3 | 49 | 0.10 | 0.10 | 0.10 |
| runtime_4 | 49 | 0.10 | 0.10 | 0.10 |
| runtime_5 | 49 | 0.10 | 0.10 | 0.10 |
| runtime_6 | 60 | 4.49 | 2.00 | 5.80 |

GPU 采样文件的瞬时 `nvidia-smi` 大多没有捕捉到 GPU 忙时：7 个样本 GPU0 平均/峰值利用率都是 0%，但清理前最终快照显示 GPU0 为 44%，decoder 13%，显存 4857 MiB。这里更可信的是 Savant runtime metrics 和 `validate_seq_iq` 日志，而不是低频 `nvidia-smi` 瞬时采样。

## 事件与证据

压测停止后第一次统计：

- `intrusion`: 120
- `watchlist_hit`: 341
- watchlist 命中：Finch 116，Reese 228
- evidence task：222 `materialization_expired`，110 `materialization_pending`，134 `materialization_skipped`
- evidence bundle：0

清理前 DB 累计：

- pressure cameras：60
- zones：60
- rules：120
- events：1014
- evidence_tasks：1014
- evidence_bundles：0

按要求保留了 50 个随机事件样本：

- CSV：`/data/video-analytics/artifacts/pressure60_3fps_20260627T125357Z/kept_50_events.csv`
- ID 列表：`/data/video-analytics/artifacts/pressure60_3fps_20260627T125357Z/kept_50_event_ids.txt`
- 保留分布：14 个 `intrusion`，36 个 `watchlist_hit`
- 保留 watchlist：Finch 12，Reese 24
- 50 个样本全部保留 `pre_seconds=5`、`post_seconds=5`
- 因没有生成 bundle，50 个保留任务已标记为终态 `materialization_expired`，避免继续阻塞后续受控重启

## 已确认的缺陷

1. `validate_seq_iq` 仍是 P0 输入链路问题。
   - 本轮计数：51804 次。
   - 单 source 最高：`pressure60_3fps_20260627T125357Z_18` 为 951 次。
   - 说明 60 路 3 FPS 下源适配器/Ingress 序列稳定性不足，且 source 容器有重启/重连抖动。

2. 证据物化仍是 P0 事件风暴问题。
   - 1014 个事件/任务没有生成任何 `evidence_bundles`。
   - media worker 日志显示 replay sink 扫描没有解析到 metadata 文件。
   - 3 FPS 压测下，仅靠当前 evidence worker/media worker 链路无法保证 5s/5s 证据落盘。

3. 配置 guardrail：压测初始插入 `zone_type=roi` 会导致 Savant 配置解析失败。
   - 已在本次运行中修正为 `zone_type=polygon`。
   - Savant 当前接受的区域类型应使用 `polygon`、`line`、`direction_line`。

## 清理与恢复

已清理：

- 删除 60 个临时 source 容器，最终剩余 0。
- 删除 Redis 中本 run 的 `security.events`、`security.face_observations`、`security.person_observations`、`security.frame_annotations`、`security.record_requests`、`security.alerts` 条目，最终剩余 0。
- 删除非保留压测 events/tasks，删除全部压测 cameras/zones/rules/observations。
- 清理错误恢复窗口内误生成的 3 条 `primary_rtsp` 测试事件。

最终保留：

- pressure events：50
- pressure evidence_tasks：50
- pressure evidence_bundles：0
- pressure cameras：0
- pressure face/person observations：0

运行态已恢复：

- `lab`：启用
- `primary_rtsp`：停用
- Savant：`BATCH_SIZE=1`，`MAX_PARALLEL_STREAMS=4`
- Forwarder：`ANALYSIS_FPS=8/1`，`ANALYSIS_MIN_FPS=2/1`
- 8090 runtime 最终只有 lab 动态 source 在运行；`compose_source_not_running` 是 primary disabled 时的既有提示，不影响当前 lab 动态源运行。

## 下一步建议

3 FPS/60 路不建议继续作为当前单 4090 主机的稳定目标。下一步应优先修两个瓶颈，而不是继续上调 batch size：

1. 修 `validate_seq_iq`/source adapter 重连与 EOS 处理，确认 60 路 source 不再频繁序列跳变。
2. 修 evidence materialization 在事件风暴下的调度、deadline、replay metadata 生产与扫描链路，先保证 2 FPS/60 路能稳定生成 5s/5s bundle。
3. 修完上述两项后，再重新跑 2 FPS 对照，再跑 3 FPS，避免用不稳定输入链路误判模型 batch size。
