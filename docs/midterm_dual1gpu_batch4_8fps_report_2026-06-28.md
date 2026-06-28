# Midterm 单卡双分支 60 路 8 FPS Batch4 压测报告 - 2026-06-28

## 结论

本轮目标是在 4 FPS 已通过后，把同一张 RTX 4090、30+30 双 Savant 分支配置提升到
8 FPS，验证 `analysis-forwarder -> Savant` 前段链路是否仍然稳定。最终 8 FPS
压测通过，但 decoder 峰值达到 92%，生产上已经接近解码侧上限。

- Run ID：`pressure60_dual1gpu_batch4_8fps_20260628T144756Z`
- Artifact：`/data/video-analytics/artifacts/pressure60_dual1gpu_batch4_8fps_20260628T144756Z`
- 结果：`passed`
- 60 个 source 全部进入 forwarder 和 Savant，source exited=0，restart=0，negative PTS=0
- forwarder queue depth 峰值 0
- Savant send failures 峰值 0
- 最终 forwarded / target ratio：1.0012
- GPU 峰值：58%
- decoder 峰值：92%

这轮不验证 evidence 生成吞吐。本次命令使用 `--keep-evidence 0`，因此结论只覆盖
前段解码、采样转发、Savant 接收和推理入口稳定性。

## 压测配置

```bash
python scripts/runtime/run_midterm_pressure60.py \
  --dual-shard-same-gpu --dual-shard-gpu 0 \
  --fps 8/1 --min-fps 2/1 \
  --batch-size 4 --pose-batch-size 4 --face-detector-batch-size 4 \
  --face-embedding-batch-size 16 \
  --max-parallel-streams 32 \
  --batched-push-timeout 40000 \
  --duration-s 120 --sample-interval-s 15 \
  --drain-s 0 --guard-wait-s 120 \
  --keep-evidence 0 --max-validate-seq-iq 100000 \
  --run-id pressure60_dual1gpu_batch4_8fps_20260628T144756Z
```

关键运行参数：

| 参数 | 值 |
| --- | --- |
| stream_count | 60 |
| rtsp_uri | `rtsp://192.168.1.105:8554/live/1080movie` |
| fps / min_fps | `8/1` / `2/1` |
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
| final_forwarder_frames_seen_total | 187323 |
| final_forwarder_frames_forwarded_total | 57670 |
| target_forwarded_frames | 57600 |
| final_forwarded_target_ratio | 1.0012 |
| max_forwarder_cpu_percent | 35.26 |
| max_savant_cpu_percent | 254.49 |
| max_source_adapter_cpu_percent | 4.82 |
| GPU max | 58% |
| decoder max | 92% |

有效 FPS 采样：

| 阶段 | avg_effective_fps_10s |
| --- | ---: |
| 首个采样窗口 | 5.285 |
| 稳定窗口最低 | 7.018 |
| 稳定窗口最高 | 7.833 |
| 最后采样窗口 | 7.833 |

首个采样窗口仍处在 60 路接入爬坡阶段；从第二个采样开始，forwarder 和 Savant
均看到 60 路，队列始终为 0。最终 forwarded 帧数几乎正好达到 60 路 * 8 FPS *
120 秒的目标量。

## 与 4 FPS 的对比

| 项目 | 4 FPS | 8 FPS |
| --- | ---: | ---: |
| forwarded / target ratio | 1.0866 | 1.0012 |
| max_forwarder_queue_depth | 0 | 0 |
| max_savant_send_failures_total | 0 | 0 |
| max_forwarder_cpu_percent | 34.45 | 35.26 |
| max_savant_cpu_percent | 181.35 | 254.49 |
| GPU max | 62% | 58% |
| decoder max | 61% | 92% |
| validate_seq_iq | 30984 | 43951 |

8 FPS 下 forwarder 仍没有排队和发送失败，Savant 也能看到全部 60 路。最明显的变化
是 decoder 峰值从 61% 上升到 92%，这说明瓶颈风险更接近解码/接入侧，而不是
forwarder 队列或 Savant send failure。

## 生产判断

8 FPS 在当前单 RTX 4090 环境里可以跑通，但不建议把它当成生产安全余量很大的配置。

- decoder 峰值 92%，留给 RTSP 抖动、真实摄像头编码差异、额外 source、系统后台任务
  的余量不多。
- GPU utilization 峰值不是最高风险项，说明提高 batch 后前段主要风险不只看推理核利用率。
- 如果生产目标是稳定 60 路 8 FPS，需要继续做更长时间运行、真实 RTSP 摄像头混合输入、
  evidence 开启后的端到端压测。

## 剩余风险

- Savant `validate_seq_iq` 仍有 43951 条 warning。本轮无 send failure、无 queue backlog、
  source 不退出，因此按采样导致的 seq gap 噪声处理；后续仍应降噪。
- 本轮没有打开 evidence 保留，不代表 replay / clip-worker / media-worker 证据链路通过。
- 当前通过的是本机 RTX 4090 单卡双分支 8 FPS，不等价于 T4 或生产混合硬件结论。

## 判定

8 FPS 是当前单卡双分支方案可达到的前段吞吐上限候选，但 decoder 峰值已经接近满载。
生产上更稳妥的路线是：以 4 FPS 作为保守基线，以 8 FPS 作为高配目标继续做长跑和
端到端 evidence 压测。
