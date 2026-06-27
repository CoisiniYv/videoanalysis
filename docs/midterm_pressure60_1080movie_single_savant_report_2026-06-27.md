# Midterm 60 路 1080movie 单 Savant 压测报告 - 2026-06-27

## 1. 结论

本次完成了一次 60 路模拟推理压测：

- 输入源：同一个 `1080movie` RTSP，复制成 60 个临时摄像头；
- 拓扑：单路 `video-analytics-midterm-savant`，不是双 shard；
- 采样档位：Forwarder / Savant 均从 2 FPS 档起测；
- batch 调整：`BATCH_SIZE=4`，`MAX_PARALLEL_STREAMS=64`；
- 模型 batch：`POSE_BATCH_SIZE=1`、`FACE_DETECTOR_BATCH_SIZE=1`、
  `FACE_EMBEDDING_BATCH_SIZE=16`；
- 规则：每路启用 `behavior.intrusion` 和 `face.watchlist`；
- 人脸名单：Reese、Finch；
- 结果：60 路在 2 FPS 档位下可持续进入单 Savant 推理链路，Forwarder 队列保持
  0，watchlist 和 intrusion 均有事件产生。

这不是 T4 生产验收。当前主机是 RTX 4090 环境，不能用本结果替代真实 T4 30/60
路压测结论。

## 2. 压测配置

运行标识：

```text
RUN_ID=pressure60_20260627T083744Z
runtime_epoch=midterm-20260627T083939Z-b130f9b4
artifact_dir=/data/video-analytics/artifacts/pressure60_20260627T083744Z
```

视频源：

```text
rtsp://192.168.1.105:8554/live/1080movie
```

运行参数：

| 参数 | 值 |
| --- | --- |
| 临时摄像头数 | 60 |
| Savant shard | single `video-analytics-midterm-savant` |
| `ANALYSIS_FPS` | `2/1` |
| `ANALYSIS_MIN_FPS` | `1/1` |
| `MAX_FPS` | `2/1` |
| `MIN_FPS` | `1/1` |
| `BATCH_SIZE` | `4` |
| `MAX_PARALLEL_STREAMS` | `64` |
| `POSE_BATCH_SIZE` | `1` |
| `FACE_DETECTOR_BATCH_SIZE` | `1` |
| `FACE_EMBEDDING_BATCH_SIZE` | `16` |
| `BATCHED_PUSH_TIMEOUT` | `40000` |

`BATCH_SIZE=4` 是本次选择的 muxer / pipeline batch 档位。姿态和 YOLOv8-Face
仍保持 batch 1，因为当前 engine/部署默认仍按单 batch 使用；AdaFace 继续使用
batch 16。

## 3. 推理表现

3 分钟采样结果保存在：

```text
/data/video-analytics/artifacts/pressure60_20260627T083744Z/samples_batch4_fps2/
```

采样汇总：

| 指标 | 结果 |
| --- | ---: |
| 采样点 | 7 |
| pressure sources | 60 |
| 平均每路 effective FPS | 2.08 |
| 采样窗口最小 FPS | 0.10 |
| 采样窗口最大 FPS | 2.40 |
| warmup 后典型 FPS | 2.0 - 2.3 |
| 最大 last-frame age | 9.95s |
| warmup 后最大 last-frame age | 0.79s |
| Forwarder source rows | 60 |
| Forwarder queue depth | 0 |

运行时最终快照显示：

```text
va_savant_sources_active=60
health issue=compose_source_not_running
```

`compose_source_not_running` 来自 `primary_rtsp` 被故意保持停用，不代表 60 个
pressure 动态源失败。

GPU 采样：

| GPU | 角色 | 采样结果 |
| --- | --- | --- |
| GPU0 RTX 4090 | 单 Savant 实际使用 | spot sample 平均 GPU util 约 9.7%，最大 32%；最终快照 GPU 45%，decoder 55%，显存约 4.9 GiB |
| GPU1 RTX 4090 | 未参与本次单 shard 推理 | 基本空闲 |

Savant 日志中出现大量 `validate_seq_iq` warning，20 分钟窗口计数约 8 万级。
这些 warning 表示多源压测下存在 seq id 跳变 / message loss / 非 EOS 终止迹象。
因此本次结果只能说明 2 FPS 档位的单 Savant 吞吐可跑通，不能说明输入链路已经
达到生产稳定性。

## 4. 事件与证据结果

停止前稳定快照记录：

| 事件类型 | 数量 |
| --- | ---: |
| `intrusion` | 8184 |
| `watchlist_hit` | 152 |

watchlist 命中分布：

| 人员 | 数量 |
| --- | ---: |
| Reese | 99 |
| Finch | 53 |

证据任务状态快照：

| 状态 | 数量 |
| --- | ---: |
| `materialization_skipped` | 7404 |
| `materialization_expired` | 763 |
| `materialization_failed` | 60 |
| `materialization_deferred` | 55 |
| `materialization_pending` | 53 |
| `materializing` | 1 |

bundle 快照：

```text
bundles=60
```

后续 Redis / worker 残留队列继续落库了一小段尾部数据。最终清理时实际删除：

| 项 | 数量 |
| --- | ---: |
| pressure events | 8913 |
| pressure evidence_tasks | 8913 |
| pressure evidence_bundles | 64 |
| pressure face_observations | 25151 |
| pressure person_bbox_observations | 52970 |
| pressure camera_rules | 120 |
| pressure camera_zones | 60 |
| pressure cameras | 60 |

证据链路结论：

- `intrusion` 和 `watchlist_hit` 都能触发；
- 2 FPS / 60 路下事件量远高于证据物化能力；
- 大量 `materialization_skipped`、`expired`、`failed` 说明证据生成链路仍是 30/60
  路生产化必须继续优化的瓶颈；
- 本次不能声明“60 路证据全部可稳定生成”。

## 5. 清理与恢复

已清理：

- 删除 60 个临时 pressure 摄像头；
- 删除 120 条临时规则和 60 个 full-frame zone；
- 删除 pressure 前缀的 events、evidence_tasks、evidence_bundles、
  face_observations、person_bbox_observations；
- 删除 64 个本次 pressure evidence bundle 目录；
- 删除本次 pressure runtime epoch 目录：
  `/data/video-analytics/media/replay-sink-output/midterm/epochs/midterm-20260627T083939Z-b130f9b4`；
- 从 Redis streams 中按 `pressure60_20260627T083744Z` 前缀删除残留的
  `security.events`、`security.face_observations`、`security.person_observations`
  记录；
- 恢复 Savant / Forwarder 默认性能参数。

恢复后验证：

| 项 | 结果 |
| --- | ---: |
| pressure cameras | 0 |
| pressure events | 0 |
| pressure evidence_tasks | 0 |
| pressure evidence_bundles | 0 |
| pressure face_observations | 0 |
| pressure person_bbox_observations | 0 |
| pressure source containers | 0 |
| 当前摄像头总数 | 2 |
| 当前启用摄像头 | 1 |

当前摄像头状态：

| 摄像头 | 状态 |
| --- | --- |
| `primary_rtsp` | disabled |
| `lab` | enabled |

恢复后的性能参数：

| 参数 | 值 |
| --- | --- |
| `BATCH_SIZE` | `1` |
| `MAX_PARALLEL_STREAMS` | `4` |
| `MAX_FPS` | `8/1` |
| `MIN_FPS` | `2/1` |
| `ANALYSIS_FPS` | `8/1` |
| `ANALYSIS_MIN_FPS` | `2/1` |

磁盘占用对比：

| 路径 | 清理前 | 清理后 |
| --- | ---: | ---: |
| `/data/video-analytics/media/evidence` | 约 1.8 GiB | 约 1.6 GiB |
| `/data/video-analytics/media/replay-sink-output/midterm` | 约 49 MiB | 约 37 MiB |

## 6. 后续建议

1. 单 T4 60 路不要从 8 FPS 起跑，应继续使用 2 FPS / 3 FPS / 4 FPS 阶梯实测。
2. 真实 T4 上需要重新测 `BATCH_SIZE`、`MAX_PARALLEL_STREAMS`、模型 interval 和
   GPU decode/infer 利用率，不能沿用 4090 结论。
3. 证据链路需要继续优化 materialization 并发、annotation retention、Replay TTL
   和存储 IO，否则事件能产生但证据无法稳定跟上。
4. `validate_seq_iq` warning 需要单独定位。若 30/60 路生产目标要求少丢帧，应检查
   source adapter、Savant ingress、队列和重采样策略。

本次可作为“单 Savant / 60 路 / 2 FPS / 1080movie / 事件链路可跑通”的模拟结果，
不能作为 T4 生产 ready 证明。

## 7. 下一次复跑保留样本要求

本次已按原要求清理所有 pressure 事件和证据，因此当前 8090 上没有可回放的
pressure evidence。下一次 60 路复跑必须改变清理策略：

- 在清理前从本次 `pressure60_<run_id>` 产生的 ready / generated bundle 中随机保留
  50 个事件；
- 如果 generated bundle 不足 50 个，则优先保留全部 generated bundle，再用
  `materialization_failed` / `materialization_expired` 事件补足到 50 个；
- 保留清单写入：

```text
/data/video-analytics/artifacts/<run_id>/kept_50_events.csv
```

清单字段至少包含：

```text
event_id,source_id,camera_id,event_type,matched_person,evidence_state,raw_clip_uri,created_at
```

清理规则：

- 保留这 50 个事件、对应 `evidence_tasks`、`evidence_bundles` 和
  `/data/video-analytics/media/evidence/<event_id>/`；
- 删除其余 pressure 事件、证据、observations、临时摄像头、临时规则和 Replay epoch；
- 清理后在报告中记录：
  - kept event count；
  - deleted event count；
  - deleted bundle count；
  - kept evidence disk usage；
  - 8090 可打开的 5 个抽样 bundle URL。

这样既能释放大部分磁盘空间，也能保留足够样本用于 8090 人工查看。

## 8. 本次暴露的两个代码修复瓶颈

### 8.1 Evidence materialization 高并发瓶颈

现象：

- 60 路 / 2 FPS 下事件链路可以产生大量 `intrusion` 和 `watchlist_hit`；
- 证据任务大量进入 `materialization_skipped`、`materialization_expired`、
  `materialization_failed`；
- generated bundle 只有几十个量级，远低于事件量。

这不是单纯调大 batch size 可以解决的问题。需要作为代码修复项继续处理：

- 重新设计 evidence materialization 的 admission policy，避免事件风暴下无意义排队；
- 将每源 / 每事件类型的证据 quota 与 cooldown 变成可观测、可调的策略；
- 记录每个被跳过或过期任务的明确原因，并在 8090 上可过滤；
- 对 Replay job、annotation wait、ffmpeg materialization 分别计时，定位 p95/p99；
- 用保留 50 个样本验证证据质量，而不是只看事件数量。

### 8.2 Savant ingress / source adapter 序列跳变瓶颈

现象：

- Savant 日志出现大量 `validate_seq_iq` warning；
- warning 文本指向 message loss 或 stream termination without EOS；
- 这说明 60 路模拟在 2 FPS 下虽然吞吐能跑通，但输入链路还不能视为生产稳定。

需要作为代码修复项继续处理：

- 按 source 统计 `validate_seq_iq` 次数，纳入 8090 runtime overview 或压测报告；
- 关联 source adapter restart、RTSP reconnect、Savant last-frame-age 和 seq warning；
- 检查动态 source adapter 停启、重采样丢帧、EOS 处理和 Savant ingress 队列；
- 压测报告中增加 `seq_warning_count_by_source`；
- 修复后验收标准应包括：同等 60 路 / 2 FPS 压测下 seq warning 显著下降，且
  effective FPS、last-frame-age、watchlist/intrusion 事件不回退。
