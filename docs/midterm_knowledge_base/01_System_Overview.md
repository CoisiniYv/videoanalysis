---
type: architecture-note
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - architecture
  - midterm
---

# 系统总览

## 系统定位

Midterm 接入 RTSP，执行 Pose/Face/行为分析，持久化人体和人脸轨迹，并生成可播放、
可审计的告警 evidence。浏览器入口集中在 8090，后台由 Compose、动态 source、
PostgreSQL、Redis 和 GPU workers 组成。

## 控制面

```text
Browser :8090
  -> evidence-viewer
  -> api:8000
  -> PostgreSQL config/state
  -> generated snapshots
  -> Docker Engine API
```

8090 负责摄像头、ROI/规则、人员注册、轨迹、evidence、运行预设、延迟和存储维护。
API 8000 不对客户直接发布。

## 完整双分支数据面

```text
RTSP -> Replay A/B -> replay-raw-fanout A/B
  |-> sampled Savant A/B -> Redis events/person/face ROI/annotations
  `-> full-rate rolling-cache-sink A/B

Redis -> event/person/face workers -> PostgreSQL
PostgreSQL tasks + rolling segments -> media-worker -> evidence -> 8090
```

关键边界：

- Replay 接收全率源并提供原始 fanout；
- 分析 sampler 只影响 Savant cadence；
- rolling-cache 直接接 sampler 之前的全率编码帧；
- 完整预设使用 rolling materialization，不发布 per-event Replay request；
- `clip-worker/video-file-sink` 保留给单分支和兼容路径；
- 证据约 24 FPS，分析为 4/8 FPS；
- Savant 原始 PTS/UUID 与 rolling mux PTS 是两个不能混用的时间域。

## 事实源

| 内容 | 事实源 |
| --- | --- |
| 摄像头、ROI、规则 | PostgreSQL |
| 人员和注册图库 | PostgreSQL |
| 事件、任务、evidence 索引 | PostgreSQL |
| 在线消息 | Redis Streams |
| 运行配置 | DB 导出快照 + 当前容器环境 |
| 原始/最终媒体 | Replay/rolling/evidence 文件系统 |

`cameras.midterm.yml`、`sources.generated.yml` 和 topology JSON 是运行快照，不是人工
配置真相。

## 当前能力边界

已验证：

- T4 40 路、4 FPS、双分支、ROI AdaFace、5+5 evidence；
- 同生产链约 4 小时无 failed/expired/fallback；
- DB-backed timeline/annotation/bbox/person context；
- 独立人体轨迹 consumer 40/60 路覆盖；
- 8090 后台启动和状态恢复显示。

仍需验证：

- 最新双时间域 revision 的 4090 60 路、8 FPS；
- 混合真实摄像头断流/重连与长 soak；
- worker/API 重启恢复；
- T4 散热整改后的扩容；
- 鉴权、RBAC 与 8090 WebSocket 代理。

完整说明见 `docs/current_architecture.md`。
