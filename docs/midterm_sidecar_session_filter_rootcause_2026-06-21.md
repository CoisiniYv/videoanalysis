# Sidecar stream_session_id 过滤导致标注丢失根因分析

**日期:** 2026-06-21
**状态:** 根因确认，media-worker 侧止血修复和 Savant session churn 收敛均已实现并运行态验证
**影响范围:** 所有 evidence 的 sidecar 标注覆盖不完整

## 现象

每个 evidence 的 sidecar summary 都显示 `messages_filtered_stream_session` 数百条，
实际写入的标注只有 2-61 帧（8fps 采样 10 秒应产生 ~80 帧）。

典型数据：

| evidence      | entries_scanned | messages_valid | session_filtered | annotations_written |
|---------------|-----------------|----------------|------------------|---------------------|
| 120c1cd5      | 643             | 230            | 413              | 50                  |
| c5788b60      | 642             | 322            | 320              | 2                   |
| c5e94a60      | 643             | 136            | 507              | 61                  |
| 4fd3ce2f      | ?               | ?              | 319              | 47                  |
| f35f7261      | ?               | ?              | 494              | 7                   |

8090 viewer 观察：标注只覆盖视频中间一段，前几秒和后几秒没有标注。

## 根因

### 1. StreamSessionTracker 在 PTS 回退时创建新 session

`modules/savant_security/custom/services/stream_session.py:49-58`：

```python
last_pts = state.last_pts
if pts is not None and last_pts is not None and pts < last_pts:
    state = StreamSessionState(
        source_id=source_key,
        session_id=self._new_session_id(source_key),  # 新 UUID
        last_pts=pts,
        bump_count=state.bump_count + 1,
    )
```

每次 PTS 回退，`session_id` 都会变成新的 UUID。

### 2. Redis stream 积累多个 session 的 annotation

Savant 写入 Redis `security.frame_annotations` 时，每条 annotation 都带有当前的
`stream_session_id`。当 session 变化后，新 annotation 带新 session_id，旧 annotation
仍保留在 Redis 中。

### 3. Sidecar writer 严格过滤 session_id

`services/media-worker/app/frame_cache_sidecar_writer.py:737-742`：

```python
if stream_session_id_set and (
    str(message.get("stream_session_id") or "").strip()
    not in stream_session_id_set
):
    summary["messages_filtered_stream_session"] += 1
    continue
```

sidecar writer 从 event payload 中提取 `stream_session_id`，只保留匹配的 annotation。
旧 session 的 annotation 被全部丢弃。

### 4. Event payload 只携带有限的 session_id

`_stream_session_ids_from_event`（`frame_cache_sidecar_writer.py:844-875`）收集的
session_id 候选包括：

- `event.stream_session_id`
- `payload.stream_session_id`
- `labels.stream_session_id`
- `labels.start_window_stream_session_id`
- `labels.post_window_stream_session_id`（仅当 `frame_domain_session_policy` 允许时）

但这些都只覆盖"当前 session"和"post-window session"，无法覆盖 Redis 中积累的所有
旧 session。

### 5. 完整因果链

```
RTSP 源 PTS 不连续（重连/重启/抖动）
  → Savant StreamSessionTracker 检测到 pts < last_pts
  → 创建新 session_id（新 UUID）
  → 新 annotation 写入 Redis，带新 session_id
  → 旧 annotation 仍留在 Redis 中，带旧 session_id
  → Event 触发，event payload 携带当前 session_id
  → Sidecar writer 从 Redis 读取 annotation
  → 用 session_id 过滤，丢弃旧 session 的 annotation
  → 只有当前 session 的 annotation 保留
  → 当前 session 可能只覆盖 clip 的中间部分
  → 前几秒和后几秒的标注丢失
```

## 为什么是系统性的

抽样 20 个 evidence，每个的 `expected_stream_session_id` UUID 都不同。这说明
session 变化非常频繁，不是偶发的 adapter 重启。

Redis stream 总是有 ~643 条 entries（受 `range_count=2000` 和时间窗口限制），其中约
一半来自当前 session，一半来自旧 session。这说明 Redis 在持续积累多 session 的数据。

## 非根因排除

### analysis-forwarder sampler 不是根因

`sampler.py:49-51` 在 PTS 回退时直接放行：

```python
if last_pts_ns is None or pts_ns <= last_pts_ns:
    self._accept(source_key, pts_ns)
    return True
```

sampler 不会丢弃新 clip 的第一帧。PTS 回退时 sampler 放行，但 Savant 会创建新
session。

### clip 跨 session 不是根因

不是"一个 video clip 跨越了两个 session"，而是"Redis 里积累了多个 session 的
annotation，sidecar writer 只取当前 session 的"。

## 修复方案

### A. 已实现：media-worker 侧放宽 session 硬过滤

- 新增 `FRAME_CACHE_SIDECAR_STREAM_SESSION_FILTER_MODE`，当前 midterm 配置为
  `event_window`。
- `strict` 模式保持旧行为：`stream_session_id` 不在 event/Replay labels 允许集中时
  直接计入 `messages_filtered_stream_session` 并丢弃。
- `event_window` 模式不再把同 `runtime_epoch_id`、`source_id`、`camera_id` 的跨
  session annotation 提前丢弃；这类消息计入
  `frame_cache_reader_summary.messages_stream_session_mismatch`，后续仍由 event
  wall-clock window、anchor frame、clip metadata alignment 和 freshness guard 收敛。
- 硬边界仍然是 `runtime_epoch_id`、`source_id`、`camera_id`，不跨 runtime epoch 混用。

### B. 回滚点

- 将 `FRAME_CACHE_SIDECAR_STREAM_SESSION_FILTER_MODE=strict` 即可恢复旧的严格
  session 过滤行为。
- 回滚后预期 `messages_filtered_stream_session` 会重新升高，sidecar 标注覆盖可能再次
  变短。

### C. 已实现：Savant 侧减少 session churn

- `StreamSessionTracker` 增加 `STREAM_SESSION_PTS_ROLLBACK_TOLERANCE_NS`，当前
  midterm 配置为 `5000000000`（5s）。
- 小于等于阈值的 PTS 回退视为同一 source 内的 PTS 抖动，不再 mint 新
  `stream_session_id`。
- 大于阈值的 PTS 回退仍视为真实 source adapter reset，继续切换
  `stream_session_id`，避免跨 PTS domain 混用。
- tracker 以 high-water PTS 判定回退幅度；连续小回退累计超过阈值时仍会切换
  session。

## 验证

本次代码验证目标：

```bash
python -m pytest -q harness/tests/test_media_worker_perf_safety.py::test_frame_cache_reader_uses_bounded_stream_range_and_filters_identity harness/tests/test_media_worker_perf_safety.py::test_frame_cache_reader_event_window_mode_retains_same_source_session_mismatch harness/tests/test_media_worker_perf_safety.py::test_frame_cache_reader_allows_verified_cross_session_post_window harness/tests/test_midterm_deployment_contract.py::test_midterm_media_worker_perf_controls_are_wired
python -m pytest -q harness/tests/test_frame_annotation_exporter_runtime.py::test_frame_annotation_min_interval_resets_on_small_pts_rollback_without_session_churn harness/tests/test_frame_annotation_exporter_runtime.py::test_stream_session_tolerates_small_rollback_and_tracks_large_reset harness/tests/test_midterm_deployment_contract.py::test_midterm_runtime_calibration_is_explicit
```

运行态验证目标：生成新 evidence 后检查
`summary.frame_cache.identity.json` 中
`frame_cache_reader_summary.messages_filtered_stream_session` 应接近 0，
`messages_stream_session_mismatch` 可作为跨 session 诊断计数，
`annotations_written` 应恢复到接近 `analysis_fps * clip_seconds` 的量级。

### 2026-06-22 运行态验证记录

本次修复已在 midterm runtime 中通过只重建受影响服务应用：

```bash
docker compose -f infra/docker-compose.midterm.yml up -d --no-build --force-recreate --no-deps media-worker savant-security
```

容器环境确认：

- `media-worker`: `FRAME_CACHE_SIDECAR_STREAM_SESSION_FILTER_MODE=event_window`
- `savant-security`: `STREAM_SESSION_PTS_ROLLBACK_TOLERANCE_NS=5000000000`

新 evidence 验证：

- `/data/video-analytics/media/evidence/5db84e06-9f34-42de-9ccb-75b23113e3a3/summary.frame_cache.identity.json`
  - `stream_session_filter_mode=event_window`
  - `messages_filtered_stream_session=0`
  - `messages_stream_session_mismatch=0`
  - `annotations_written=29`
- `/data/video-analytics/media/evidence/4b1bd211-e086-4b24-9406-3f0a96180eeb/summary.frame_cache.identity.json`
  - `stream_session_filter_mode=event_window`
  - `messages_filtered_stream_session=0`
  - `messages_stream_session_mismatch=0`
  - `annotations_written=47`

Redis 最近 2000 条 `security.frame_annotations` 验证：

- `primary_rtsp`: `rows=1004`, `unique_stream_sessions=1`
- `source_00000000-0000-4000-8000-781078565686`: `rows=996`,
  `unique_stream_sessions=1`

结论：sidecar session 硬过滤已解除，Savant 侧无谓 session churn 已压住。

### 最新 evidence 前几秒无 bbox 的解释

最新样本：
`/data/video-analytics/media/evidence/987dbef6-a0c9-4e27-a4f5-64d04ee10935/`

关键字段：

- `stream_session_filter_mode=event_window`
- `messages_filtered_stream_session=0`
- `messages_retained=323`
- `annotations_written=42`
- `event_projected_t_s=4.963289`
- `actual_start_pts=29771112155555`
- `actual_end_pts=29781038733333`

该样本的 `annotations.frame_cache.identity.jsonl` 第一条 bbox 在
`t_s=4.087411111`，对应 `frame_pts=29775199566666`。

对同一 Redis range 重新检查 clip PTS 窗口内的原始 frame-cache 行：

- clip 内 frame-cache 行数：80
- clip 前 4 秒 frame-cache 行数：32
- clip 前 4 秒带对象的 frame-cache 行数：0
- 第一条带对象的 frame-cache 行：
  - `stream_id=1782141336595-0`
  - `t_s=4.087`
  - `object_count=1`
  - `counts={"face": 1}`

抽帧核对：

- `raw_clip.mov` 的 `t=1s`、`t=3s` 画面中可以看到人脸。
- 但对应 frame-cache 行仍为 `object_count=0`。
- 第一条 bbox 出现在 `t_s=4.087`，同时也是
  `anchor_keyframe_uuid=019eefe6-bb7b-70c1-ac6b-6b7e5d758346` 附近。

结论：该 evidence 前几秒没有 bbox，不是 sidecar session 过滤、overlay 慢发送或
annotation timeline 对齐丢失；而是 Savant frame annotation cache 在 clip 前 4 秒内
确实没有输出对象 metadata。sidecar 只写有对象的 annotation 行，不会为
`object_count=0` 的帧生成 bbox。

如果业务期望"画面中有人/脸时前几秒也必须有框"，下一步应诊断 Savant 检测链路本身：
查看 post-savant replay/annotation 在 anchor keyframe 前是否天然缺少 object metadata，
以及 YOLO/face 检测阈值、模型 interval、检测器对侧脸/胡须/遮挡的召回。该问题和本次
session 过滤修复是不同问题。

后续补充诊断：

- `sink_metadata.json` 是 JSONL；该 evidence clip 内 239 帧 sink metadata 均为
  `objects=[]`，因此最终 bbox 不是来自 post-savant sink metadata，而是来自 Redis
  frame annotation cache。
- `annotations.frame_cache.identity.jsonl` 对象类型首帧：
  - 第一条 `face`: `t_s=4.087411111`, `confidence=0.8568100929260254`
  - 第一条 `person`: `t_s=4.838166667`, `confidence=0.56884765625`
- 每秒对象分布：
  - `0s-3s`: 无 face/person object
  - `4s`: `face=8`, `person=2`
  - `5s`: `face=8`, `person=8`
  - `6s`: `face=8`, `person=8`
  - `7s`: `face=3`
  - `8s`: `face=7`
  - `9s`: `face=8`
- Savant 日志在事件前段也显示 `primary_rtsp` 的
  `face_reid_gate faces=0`、`behavior_rules raw_observation_count=0`，到事件附近才出现
  `raw_observation_count=1`。

当前更精确结论：前 4 秒无 bbox 是 Savant detector/object metadata 对该画面前段未产生
face/person object，尤其是侧脸、胡须、遮挡和姿态变化下的召回问题；不是 frame cache
读取、sidecar 写入或 viewer overlay 问题。

## 数据来源

Evidence 目录：`/data/video-analytics/media/evidence/`
Sidecar summary：`summary.frame_cache.identity.json`
关键字段：`frame_cache_reader_summary.messages_filtered_stream_session`

## 相关文件

- `modules/savant_security/custom/services/stream_session.py` — StreamSessionTracker
- `services/media-worker/app/frame_cache_sidecar_writer.py` — sidecar writer session 过滤
- `services/clip-worker/app/worker.py:1810-1833` — clip-worker session_id 传播
- `services/media-worker/app/frame_annotation_event_window.py` — event window 选择
