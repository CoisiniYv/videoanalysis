---
type: architecture-note
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - architecture
  - midterm
---

# 系统总览

## 系统定位

Midterm 是一套实时视频分析和证据生成系统。用户入口集中在 8090 操作台，后台由 Docker Compose
编排 Replay、Savant/DeepStream、Redis workers、PostgreSQL 和 evidence 服务。

核心目标：

- 接入 RTSP 摄像头；
- 按可配置 FPS 抽样推理；
- 执行入侵、人脸 watchlist 命中等算法；
- 生成可播放、可审计 evidence；
- 在 8090 页面完成摄像头、ROI、规则、人员、人脸库、运行拓扑和性能参数管理。

## 主链路

```text
RTSP source
  -> Replay storage
  -> analysis-forwarder sampled branch
  -> Savant / DeepStream inference
  -> Redis Streams
  -> event-worker / face-worker
  -> PostgreSQL / pgvector
  -> clip-worker Replay job
  -> video-file-sink raw clip
  -> media-worker evidence indexing/finalization
  -> 8090 operator / evidence APIs
```

相关细节：

- [[02_Runtime_Data_Flow|运行时数据流]]
- [[03_Module_Map|模块地图]]
- [[05_Evidence_Chain|证据链]]

## Source of truth

PostgreSQL 是摄像头、规则、人员、图库和 evidence metadata 的事实源。

生成文件只作为运行时快照：

- `modules/savant_security/config/cameras.midterm.yml`
- `infra/generated/sources.generated.yml`
- `/data/video-analytics/media/.runtime/replay_shards.topology.json`

不要只凭 YAML diff 判断配置漂移。8090 和 API 应从 DB 读取并导出运行时快照。

## 当前能力边界

已证明：

- 60 路 3 FPS 下游证据链，50/50 playable；
- 单 4090 同卡双分支 60 路 4 FPS retained evidence；
- 单 4090 同卡双分支 60 路 8 FPS retained evidence；
- 8090 可管理性能配置和拓扑配置；
- clean-machine 离线迁移包支持 Docker images。

未证明：

- 真实 RTSP 混合输入长时间稳定性；
- T4 / 弱卡 / 双 GPU 生产 profile；
- 60 路 16 FPS 推理吞吐；
- 大图库 face-worker 查询性能；
- Savant 模型阶段级 latency。

## 设计偏好

- 8090 是管理入口，API 8000 仅在 compose 网络内使用。
- Evidence metadata 走 PostgreSQL，`raw_clip.mov` 仍保留在文件系统。
- 对事件风暴采用 admission/backpressure，优先保证 retained evidence 可完成，而不是全量物化所有事件。
- 运行时重启必须受 evidence guard 约束，避免中断正在生成的证据。
