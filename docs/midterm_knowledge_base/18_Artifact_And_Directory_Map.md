---
type: artifact-directory-map
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - artifacts
  - directories
  - deployment
---

# 目录与 artifact 地图

## Repo 关键目录

| 路径 | 内容 |
| --- | --- |
| `infra/` | Compose、env、storage/operator override、generated snapshot |
| `modules/savant_security/` | Savant module、模型适配、规则和 exporters |
| `services/api/` | FastAPI、运行控制、注册与查询 |
| `services/evidence-viewer/` | 8090 UI/代理 |
| `services/analysis-forwarder/` | raw fanout 与 analysis sampler |
| `services/rolling-cache-sink/` | 自有 GStreamer rolling fragment sink |
| `services/adaface-roi-worker/` | 外置 TensorRT AdaFace |
| `services/event-worker/` | event worker 与独立 person worker |
| `services/face-worker/` | face observation/gallery/watchlist |
| `services/clip-worker/` | 兼容 Replay coordinator |
| `services/media-worker/` | Scheduler V2、segment index、finalization |
| `libs/evidence_lifecycle/` | canonical lifecycle vocabulary |
| `db/migrations/` | PostgreSQL schema；当前到 032 |
| `harness/tests/` | 契约/回归/故障注入测试 |
| `scripts/runtime/` | source、pressure、precreate 和运行工具 |
| `docs/` | 当前文档、规格证据和历史报告 |

## 持久数据根

```text
/data/video-analytics
  models/                    shared immutable model inputs
  models-savant-b/           branch-B writable TensorRT cache inputs/engines
  replay-midterm*/           Replay RocksDB
  media/
    evidence/                final evidence
    face_trajectories/       retained face trajectory media
    .runtime/                epoch/topology/apply status
  artifacts/                 pressure and audit artifacts
  downloads/                 temporary/import data, not clean-migration required
```

## 快速盘根

默认：

```text
/home/user/video-analytics-fast
  rolling-cache/
  rolling-cache-materialized/
  face_trajectory_cache/
```

真实路径由 `infra/midterm-storage.override.yml` 和环境覆盖决定。最终 evidence 不能只留在
临时/快速盘中。

## Rolling segment layout

```text
<rolling-root>/midterm/epochs/<runtime_epoch_id>/<source_id>/segments/<segment_id>/
  video.mov
  metadata.json
```

`metadata.json` 是发布 commit marker。未完成 fragment 留在 `.rolling-cache-staging`，
不能被当作可见 segment；retention 不能删除 active read pin。

## Evidence

最终物理 artifact：

- intrusion：`raw_clip.mov`；
- watchlist：image/crop/trajectory media；
- 可选 manifest/metadata/sidecar 兼容文件。

查询权威：PostgreSQL bundle/artifact/timeline/overlay。文件与 DB 必须共同收敛。

## Pressure artifact

```text
/data/video-analytics/artifacts/<run_id>/
```

重要内容通常包括：

- `report.json`、`sample_summary.json`；
- downstream/DB/drain summaries；
- runtime overview/latency samples；
- effective compose override/container env；
- source/fixture identity；
- Redis/PG/worker logs；
- evidence window、FPS、timeline/annotation checks；
- cleanup/preservation audit。

artifact 是验证证据，不是配置源，也不自动证明当前 revision。

## Generated/runtime snapshots

- `modules/savant_security/config/cameras.midterm.yml`；
- `infra/generated/sources.generated.yml`；
- `media/.runtime/replay_shards.topology.json`；
- `media/.runtime/topology_apply_status.json`；
- runtime epoch state。

它们用于排障和恢复显示，但 PostgreSQL/当前容器环境仍需交叉核对。

## 清理边界

可以受控清理：过期 rolling segment、无 pin staging、旧 pressure artifact、已确认孤儿
media。不能随意清理：active task/lease 对应媒体、当前 epoch、models、DB、用户保留
evidence 或未完成 cleanup_pending 的 source artifact。
