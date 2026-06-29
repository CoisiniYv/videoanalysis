# Midterm 单卡双分支 60 路证据链 4 FPS / 8 FPS 压测报告

日期：2026-06-29

## 结论

本次按 8090 拓扑控制路径测试单卡双分支 30+30：

- 4 FPS：通过。60 路入口稳定，replay shard 证据链闭环，保留 50 条 playable evidence，50 条 annotation complete。
- 8 FPS：复测通过。60 路入口稳定，replay shard 证据链闭环，保留 50 条 playable evidence。该轮使用 batch=4 / pose batch=4 / face detector batch=4，说明 8 FPS 失败不能简单归因为 batch size=4 不可用。
- 8 FPS 前序失败：曾出现过入口通过但业务 observation 链路为 0 的失败样本。本报告保留该失败样本，作为后续压测必须检查 pose/person/face observation 语义输出的原因。

本次还修复了一个压测脚本缺口：`clip-worker` compose 段原本没有接收 `REPLAY_SHARDS_JSON`，导致脚本记录了 replay shard 注入成功，但容器实际仍使用默认 `video-file-sink:6666`。现在 compose 已补齐该环境变量，脚本在 recreate 后会用 `docker inspect` 校验实际容器环境，不生效会立即失败。

## Artifact

| 轮次 | Run ID | Artifact | 状态 |
| --- | --- | --- | --- |
| 4 FPS | `pressure60_8090topology_dual1gpu_evidence4fps_20260629T064345Z` | `/data/video-analytics/artifacts/pressure60_8090topology_dual1gpu_evidence4fps_20260629T064345Z` | passed |
| 8 FPS 前序失败 | `pressure60_8090topology_dual1gpu_evidence8fps_20260629T065029Z` | `/data/video-analytics/artifacts/pressure60_8090topology_dual1gpu_evidence8fps_20260629T065029Z` | interrupted after diagnosis |
| 8 FPS 复测 | `pressure60_8090topology_dual1gpu_evidence8fps_batch4_verify_20260629T070656Z` | `/data/video-analytics/artifacts/pressure60_8090topology_dual1gpu_evidence8fps_batch4_verify_20260629T070656Z` | passed |

## 4 FPS 结果

关键配置：

```bash
--dual-shard-same-gpu --dual-shard-api --dual-shard-gpu 0
--fps 4/1 --min-fps 2/1
--batch-size 4 --pose-batch-size 4 --face-detector-batch-size 4
--face-embedding-batch-size 16
--max-parallel-streams 32
--duration-s 120 --sample-interval-s 15
--drain-s 600 --keep-evidence 50
```

入口指标：

| 指标 | 结果 |
| --- | ---: |
| sample_count | 8 |
| stable_samples | 8 |
| max_forwarder_sources | 60 |
| max_savant_sources | 60 |
| max_forwarder_queue_depth | 0 |
| queue_full_samples | 0 |
| max_savant_send_failures_total | 0 |
| final_forwarder_frames_forwarded_total | 32,126 |
| target_forwarded_frames | 28,800 |
| forwarded / target | 1.1155 |

证据链结果：

| 指标 | 结果 |
| --- | ---: |
| events | 1,034 |
| evidence_tasks | 1,034 |
| evidence_bundles | 50 |
| playable_bundles | 50 |
| annotation complete | 50 |
| materialization_skipped | 877 |
| materializing before cleanup | 107 |
| materialized | 50 |

Replay shard 路由已经实际进入 `clip-worker`：

- `replay-a -> dealer+connect:tcp://video-file-sink-a:6666`
- `replay-b -> dealer+connect:tcp://video-file-sink-b:6666`
- `clip_worker_replay_shards_pressure.json` 中 expected / observed SHA256 一致。

下游状态：

- Redis consumer group pending 为 0。
- `security.record_requests` consumer group pending 为 0。
- media-worker 没有走 `imageio_ffmpeg` 常规 fallback；保留证据中 raw clip 可播放。
- media-worker 峰值 CPU 约 890%，clip-worker 峰值约 93%。证据链能收敛，但瓶颈已经明确在 media finalization / clip replay 调度侧。

## 8 FPS 复测结果

关键配置与 4 FPS 相同，仅 `--fps 8/1`，目标 forwarded frames 为 57,600。

入口指标：

| 指标 | 结果 |
| --- | ---: |
| sample_count | 8 |
| stable_samples | 8 |
| max_forwarder_sources | 60 |
| max_savant_sources | 60 |
| max_forwarder_queue_depth | 0 |
| queue_full_samples | 0 |
| max_savant_send_failures_total | 0 |
| final_forwarder_frames_forwarded_total | 58,162 |
| target_forwarded_frames | 57,600 |
| forwarded / target | 1.0098 |

Savant 语义输出：

| 指标 | 结果 |
| --- | ---: |
| final_savant_pose_objects_total | 8,189 |
| final_savant_person_observations_exported_total | 8,189 |
| final_savant_face_observations_exported_total | 2,605 |

证据链结果：

| 指标 | cleanup 前 | cleanup 后保留 |
| --- | ---: | ---: |
| events | 552 | 50 |
| evidence_tasks | 552 | 50 |
| evidence_bundles | 53 | 50 |
| playable_bundles | 52 | 50 |
| materialized | 52 | 50 |
| materialization_skipped | 434 | 0 |

运行和清理状态：

- `report.json` 状态为 `passed`，`failure_reasons=[]`，`warnings=[]`。
- `kept_50_evidence.csv` 保留 50 条 evidence，其中 37 条 `intrusion`，13 条 `watchlist_hit`。
- cleanup 后 PostgreSQL 只保留本轮 50 条 materialized evidence 相关记录。
- cleanup 后 Redis `XPENDING security.record_requests clip-workers-midterm` 为 0。
- `clip-worker` 的 `REPLAY_SHARDS_JSON` 已恢复为空，运行态回到默认 replay sink 配置。

本轮结论：8 FPS / batch=4 可以完成 60 路单卡双分支 retained-evidence 验收。前序 8 FPS 失败不是 batch size 配置没有传入，也不是 batch=4 天然不可用；更可能是当时运行态或模型链路局部没有稳定产出 pose/person observation。

## 8 FPS 前序失败样本

关键配置与 4 FPS 相同，仅 `--fps 8/1`，目标 forwarded frames 为 57,600。

入口指标：

| 指标 | 结果 |
| --- | ---: |
| sample_count | 8 |
| stable_samples | 8 |
| max_forwarder_sources | 60 |
| max_savant_sources | 60 |
| max_forwarder_queue_depth | 0 |
| queue_full_samples | 0 |
| max_savant_send_failures_total | 0 |
| final_forwarder_frames_forwarded_total | 59,940 |
| target_forwarded_frames | 57,600 |
| forwarded / target | 1.0406 |

入口链路本身没有背压；8 FPS 失败点在 Savant 内部业务 observation 输出：

| 对比项 | 4 FPS 后 3 个样本 | 8 FPS 后 3 个样本 |
| --- | ---: | ---: |
| frames_seen_total | 84,006 | 157,280 |
| pose_objects_total | 15,898 | 0 |
| face_objects_total | 32,610 | 3,483 |
| adaface_embeddings_total | 32,610 | 3,483 |
| person_observations_exported_total | 15,898 | 0 |
| face_observations_exported_total | 6,271 | 0 |

Savant 日志显示 8 FPS 下脸检测并非完全没有结果：

- `raw_face_detections=583/586`
- `detector_confidence_passed=583/586`
- `reid_allowed=0`
- `reid_rejected_by_other=583/586`
- `pose_objects_total=0`
- `person_observations_exported_total=0`

因此 8 FPS 这轮不是 Redis、PostgreSQL、clip-worker 或 media-worker 堵塞，而是 person/pose 没输出导致 face-person association 和 face observation export 全部断流。后续修复应优先定位 YOLO26-pose 在 8 FPS / 双分支 / batch4 下为什么无 person 输出。

## 压测脚本验收增强

本次同步修复了一个压测验收缺口：`run_midterm_pressure60.py` 现在会把每个 runtime sample 的 Savant 语义计数写入 `sample_summary.json`，并在 retained-evidence 压测中把以下情况判为失败：

- `final_savant_pose_objects_total <= 0`
- `final_savant_person_observations_exported_total <= 0`
- `final_savant_face_observations_exported_total <= 0`

该门槛只作用于 `keep_evidence > 0` 且不是 forwarder null-sink 的证据链压测；纯入口吞吐测试不受影响。

## 性能瓶颈判断

当前结论分两层：

1. 4 FPS 生产保守档：入口和证据链都通过。下游主要压力在 media-worker finalization 和 clip-worker replay 并发，系统靠 admission/backpressure 保留 50 条高价值证据，而不是为 1,034 个事件全量生成证据。
2. 8 FPS 高配档：本次复测入口吞吐和证据链都通过，但 media-worker 峰值 CPU 约 1178%，clip-worker 峰值约 93%，说明后推理证据链的下一阶段优化仍应聚焦 media finalization、clip replay 调度和 admission 可解释性。
3. 压测验收不能只看 `forwarded / target > 1`，必须同时检查 pose/person/face observation 是否持续产出。前序 8 FPS 失败样本已经证明入口通过不等于算法语义链通过。

下一步建议：

- 用同样的语义门槛继续跑更长时间 8 FPS 长跑，确认 pose/person 输出不会再次掉到 0。
- 继续优化 media-worker finalization p95、clip-worker replay 调度和 evidence lifecycle 分段指标。
- 4 FPS 档可以作为当前更保守的生产基线，8 FPS 档作为单卡 4090 高配目标继续验证真实 RTSP 混合输入。
