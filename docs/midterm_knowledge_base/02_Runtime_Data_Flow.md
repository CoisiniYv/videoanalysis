---
type: data-flow-note
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - data-flow
  - runtime
---

# 运行时数据流

## 1. 摄像头配置与启动

```text
8090 camera/ROI/rule forms
  -> /api/v1/cameras/*
  -> PostgreSQL
  -> runtime config sync
  -> cameras.midterm.yml + sources.generated.yml
```

保存 ROI/规则只同步快照，不应重启整链。批量运行时：

```text
selected source_ids + named profile
  -> PUT runtime/topology-config
  -> POST runtime/topology-config/apply-async
  -> DB camera enable update + preflight
  -> new runtime epoch
  -> branch containers + dynamic sources
```

## 2. 完整视频与推理流

```text
dynamic RTSP adapter
  -> Replay A/B RocksDB
  -> replay-raw-fanout A/B
       |-> sampler -> Savant A/B
       |     -> PtsFpsGate
       |     -> yolo26_pose -> tracker -> behavior_rules
       |     -> yolov8_face -> face_person_associator
       |     -> face_roi_exporter
       |     -> frame_annotation_exporter / metrics
       `-> raw PUB -> rolling-cache-sink A/B
```

完整预设将 Savant 内 AdaFace input 指向 disabled object，并关闭 Savant 自身 face
observation export；embedding 由外置 ROI worker 生成。

## 3. Redis Streams

| Stream | Producer | Consumer | 结果 |
| --- | --- | --- | --- |
| `security.events` | behavior exporter / face-worker | event-worker | events、cooldown、tasks、alerts |
| `security.person_observations` | behavior rules | person-observation-worker | `person_bbox_observations` |
| `security.face_rois` | Savant ROI exporter | adaface-roi-worker | 512 维 observation stream |
| `security.face_observations` | Savant 单分支或 ROI worker | face-worker | face observations、matches、watchlist events |
| `security.frame_annotations` | Savant | latency/兼容 proof | 全局近时标注 |
| `security.frame_annotations.<source>` | Savant 完整预设 | media-worker | source-scoped timeline/overlay |
| `security.record_requests` | event-worker 单分支 | clip-worker | Replay 兼容取证请求 |

完整双分支预设启用 source-scoped annotation 且 media-worker 禁止回退全局 stream；
单分支默认仍用全局 stream。

## 4. 人脸与轨迹

```text
person bbox -> dedicated person worker -> PostgreSQL trajectory rows

face ROI JPEG + identity
  -> ROI AdaFace TensorRT batch
  -> face observation
  -> face-worker
  -> pgvector default or explicitly enabled Qdrant
  -> match_results / watchlist_hit
  -> event-worker
```

PostgreSQL 保存人员和 gallery embedding。Qdrant 是可选 derived index；当前 env 默认
pgvector。

## 5. Rolling evidence 主流

```text
full-rate raw fanout
  -> rolling-cache-sink
  -> <epoch>/<source>/segments/<segment>/video.mov + metadata.json

event-worker
  -> event + evidence_task (after prefill activation timestamp)

media-worker Scheduler V2
  -> wait ready/coverage
  -> fenced claim + read pin
  -> image or remux lane
  -> finalizer_pending handoff
  -> finalizer process
  -> atomic artifact publish + DB transaction
  -> materialized
```

完整预设：

- suppress record requests；
- Replay fallback=false；
- segment index 和 source stream 是主查找方式；
- raw clip/图片成功后，bundle、artifact、timeline 和 overlay 必须在 DB 可查询；
- cleanup 失败进入 durable `cleanup_pending`，由 media-worker 恢复。

## 6. 单分支兼容流

```text
event-worker -> record_request -> clip-worker
  -> proof/planner/admission
  -> Replay job -> video-file-sink
  -> media-worker sink discovery/finalization
```

Clip Coordinator V2 使用纯 plan hash、Replay owner/token/generation 和单一 ACK policy。
这条链仍受支持，但不能用它描述完整双分支预设的主数据流。

## 7. 停止流

8090 stop：

```text
stop dynamic sources + dual capture/inference
  -> disable cameras in DB
  -> keep event/media/rolling drain roles running
  -> mark topology apply status stopped
```

整栈停止由 `scripts/midterm_stop.sh` 完成。
