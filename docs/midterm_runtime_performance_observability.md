# Midterm Runtime Performance Observability

更新时间：2026-07-04

## 当前结论

当前 evidence 证据生成链路已经满足本轮 60 路 8 FPS 压测目标。最终采纳配置是
4 个 Replay/video-file-sink evidence shard，clip-worker 全局 materialization
concurrency 为 `36`，per-shard limit 为 `9`。

验收 run：

```text
run_id=pressure60_phase24b_evidence4_global36_8fps_prepost_5_10_20_20260704T0208
artifact_dir=/data/video-analytics/artifacts/pressure60_phase24b_evidence4_global36_8fps_prepost_5_10_20_20260704T0208
status=passed
kept_evidence=60
record_request_pending_ms p95=45549.8ms
proof_wait_ms p95=6571.45ms
sink_video_to_stable_ms p95=72.85ms
max_concurrent_reached=0
```

## 常开指标与按需诊断

生产运行默认保留轻量指标，关闭重诊断。

常开指标：

- Savant `/metrics`：`va_savant_*` per-source counters/gauges；
- analysis-forwarder `/metrics`：`va_forwarder_*` per-source 限流与发送失败；
- API `/api/v1/runtime/overview`：聚合 Savant、forwarder、容器和 evidence 状态；
- 8090 运行态页面：展示每路摄像头推理速率、累计对象数、限流丢帧和证据状态。

按需诊断：

- pressure artifact 分析：
  `scripts/tools/analyze_midterm_pressure_artifact.py`
- evidence drain 检查：
  `scripts/tools/check_midterm_evidence_drain.py`
- video-file-sink 日志解析：
  `scripts/tools/parse_video_file_sink_pressure.py`
- pose converter dump：
  `POSE_CONVERTER_DEBUG_DUMP_ENABLED=false` 默认关闭，仅定位 pose 坐标/转换问题时开启。

## Savant 每路摄像头性能指标

`modules/savant_security/custom/pyfuncs/savant_perf_metrics.py` 负责输出稳定的
`va_savant_*` 指标。它不修改 metadata，不产生事件，不生成证据，只做计数和短窗口
速率统计。

默认开关：

```text
SAVANT_PERF_METRICS_ENABLED=true
SAVANT_PERF_METRICS_STAGE_RATES_ENABLED=true
SAVANT_PERF_METRICS_FPS_WINDOW_S=10
```

如果生产机器临时需要极限降开销，可以设置：

```text
SAVANT_PERF_METRICS_ENABLED=false
```

如果只想保留累计 counters，关闭滚动 stage-rate gauges：

```text
SAVANT_PERF_METRICS_STAGE_RATES_ENABLED=false
```

关键 per-source 指标：

| 指标 | 含义 |
| --- | --- |
| `va_savant_effective_fps` | 每路摄像头进入指标点的短窗口 FPS |
| `va_savant_pose_stage_fps` | 每路进入 pose 阶段的帧率 |
| `va_savant_pose_object_fps` | 每路 person object 产出速率 |
| `va_savant_face_stage_fps` | 每路进入 face 阶段的帧率 |
| `va_savant_face_object_fps` | 每路 face object 产出速率 |
| `va_savant_adaface_embedding_fps` | 每路 AdaFace embedding 产出速率 |
| `va_savant_person_observation_fps` | 每路 person observation opportunity 速率 |
| `va_savant_face_observation_fps` | 每路通过 ReID gate 的 face observation 速率 |
| `va_savant_last_frame_age_seconds` | 每路最近帧延迟 |

累计 counters 仍保留：

- `va_savant_frames_seen_total`
- `va_savant_frame_annotations_exported_total`
- `va_savant_pose_stage_frames_total`
- `va_savant_pose_objects_total`
- `va_savant_face_stage_frames_total`
- `va_savant_face_objects_total`
- `va_savant_adaface_embeddings_total`
- `va_savant_person_observations_exported_total`
- `va_savant_face_observations_exported_total`

## 后续算法接入时怎么定位瓶颈

新增算法规则时，先把该算法拆成两个层面：

1. Savant / 推理侧：是否有 per-source stage counter 和 rolling rate；
2. 下游规则侧：是否有事件数量、跳过原因、证据请求数量和 materialization latency。

建议每个新算法至少补齐：

- `va_savant_<algorithm>_stage_frames_total`
- `va_savant_<algorithm>_objects_total` 或等价候选数；
- `va_savant_<algorithm>_stage_fps`
- event-worker 的规则命中/跳过原因统计；
- evidence materialization 的 request、deferred、ready、failed 计数。

这样 8090 可以快速回答：

- 是某路摄像头没有进入算法阶段；
- 是模型没有产出对象；
- 是规则过滤掉了候选；
- 是事件产生了但证据链路排队；
- 还是 evidence finalizer / storage 出现尾部。

## 当前剩余风险

本轮性能目标已经达成，但仍有两个边界：

- 当前验收是总量 `kept_evidence=60`，不是强制每路摄像头至少保留一条证据；
- `media_worker.queue_wait_ms` 和 `replay_to_sink_metadata_ms` 仍可能出现 2 分钟级长尾。

如果后续要把“事件触发到人能看到证据”的 p95 继续压低，应优先考虑：

- media-worker 分片；
- 独立 finalizer service；
- 同源短时间多事件合并；
- 在 pressure report 中新增 `sources_with_events` 和
  `sources_with_playable_evidence` gate。
