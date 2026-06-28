# Midterm 单卡双分支 60 路 4 FPS Batch4 压测报告 - 2026-06-28

## 结论

本轮目标是验证 60 路输入在一张 RTX 4090 上以 30+30 双 Savant 分支运行时，
`analysis-forwarder -> Savant` 前段链路能否稳定达到 4 FPS 级别。最终 4 FPS
压测通过。

- Run ID：`pressure60_dual1gpu_batch4_4fps_20260628T144353Z`
- Artifact：`/data/video-analytics/artifacts/pressure60_dual1gpu_batch4_4fps_20260628T144353Z`
- 结果：`passed`
- 60 个 source 全部进入 forwarder 和 Savant，source exited=0，restart=0，negative PTS=0
- forwarder queue depth 峰值 0
- Savant send failures 峰值 0
- 最终 forwarded / target ratio：1.0866
- GPU 峰值：62%
- decoder 峰值：61%

这轮不验证 evidence 生成吞吐。本次命令使用 `--keep-evidence 0`，因此结论只覆盖
前段解码、采样转发、Savant 接收和推理入口稳定性。

## 压测配置

```bash
python scripts/runtime/run_midterm_pressure60.py \
  --dual-shard-same-gpu --dual-shard-gpu 0 \
  --fps 4/1 --min-fps 2/1 \
  --batch-size 4 --pose-batch-size 4 --face-detector-batch-size 4 \
  --face-embedding-batch-size 16 \
  --max-parallel-streams 32 \
  --batched-push-timeout 40000 \
  --duration-s 120 --sample-interval-s 15 \
  --drain-s 0 --guard-wait-s 120 \
  --keep-evidence 0 --max-validate-seq-iq 100000 \
  --run-id pressure60_dual1gpu_batch4_4fps_20260628T144353Z
```

关键运行参数：

| 参数 | 值 |
| --- | --- |
| stream_count | 60 |
| rtsp_uri | `rtsp://192.168.1.105:8554/live/1080movie` |
| fps / min_fps | `4/1` / `2/1` |
| batch_size | 4 |
| pose_batch_size | 4 |
| face_detector_batch_size | 4 |
| face_embedding_batch_size | 16 |
| max_parallel_streams | 32 |
| batched_push_timeout | 40000 |
| duration_s | 120 |
| sample_interval_s | 15 |
| keep_evidence | 0 |
| topology | same GPU dual shard, 30+30 |

## 结果摘要

| 指标 | 结果 |
| --- | ---: |
| sample_count | 8 |
| stable_samples | 7 |
| max_forwarder_sources | 60 |
| max_savant_sources | 60 |
| max_forwarder_queue_depth | 0 |
| queue_full_samples | 0 |
| max_savant_send_failures_total | 0 |
| final_forwarder_frames_seen_total | 186739 |
| final_forwarder_frames_forwarded_total | 31294 |
| target_forwarded_frames | 28800 |
| final_forwarded_target_ratio | 1.0866 |
| max_forwarder_cpu_percent | 34.45 |
| max_savant_cpu_percent | 181.35 |
| max_source_adapter_cpu_percent | 3.56 |
| GPU max | 62% |
| decoder max | 61% |

有效 FPS 采样：

| 阶段 | avg_effective_fps_10s |
| --- | ---: |
| 首个采样窗口 | 2.787 |
| 稳定窗口最低 | 4.005 |
| 稳定窗口最高 | 4.317 |
| 最后采样窗口 | 4.132 |

首个采样窗口仍处在 60 路接入爬坡阶段；从第二个采样开始，forwarder 和 Savant
均看到 60 路，队列始终为 0。

## 本轮修复点

4 FPS 前两次尝试暴露了两个配置问题，已修复后复跑通过。

1. YOLOv8-Face 模型路径错误。
   - 旧路径：`/models/yolov8_face.onnx`
   - 正确路径：`/models/yolov8_face/yolov8n-face.onnx`
   - 错误路径会让 batch4 engine 生成到错误位置，导致 Savant 启动和 source 接入失败。

2. `MAX_PARALLEL_STREAMS=16` 不适合 30+30 双分支。
   - 每个 Savant 分支需要接入 30 路。
   - 16 会触发 `reached maximum number of streams`，导致 Savant 只看到约 32 路。
   - 最终设置为 `MAX_PARALLEL_STREAMS=32` 后，两个分支均可完整接入。

## 剩余风险

- Savant `validate_seq_iq` 仍有 30984 条 warning。本轮无 send failure、无 queue backlog、
  source 不退出，因此按采样导致的 seq gap 噪声处理；后续仍应降噪，避免 60 路生产日志压力。
- 本轮没有打开 evidence 保留，不代表 replay / clip-worker / media-worker 证据链路通过。
- 当前通过的是本机 RTX 4090 单卡双分支 4 FPS，不等价于 T4 或生产混合硬件结论。

## 判定

4 FPS 是当前单卡双分支方案的稳定基线：forwarder 无背压、Savant 可见 60 路、
GPU 和 decoder 都有余量。后续可以在该配置基础上继续向 8 FPS 压测。
