---
type: control-plane-note
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - "8090"
  - control-plane
---

# 8090 控制面

## 产品职责

8090 是用户和运维入口，覆盖：

- 摄像头管理；
- ROI / 区域 / 检测线；
- 算法规则；
- 人员和人脸库；
- 证据浏览；
- 存储维护；
- 运行状态；
- 性能配置；
- 单分支 / 双分支拓扑配置。

API 服务的 8000 端口只在 compose 网络内使用，8090 通过代理访问 `/api/v1/*`。

## 配置保存与 runtime apply 的区别

最重要的产品语义：

```text
保存摄像头规则 / ROI
  -> DB update
  -> /api/v1/cameras/runtime/config/sync
  -> 生成 Savant/adapter 快照
  -> 不重启推理链路

启停摄像头 / 修改 RTSP / 修改性能参数 / 修改拓扑
  -> controlled runtime apply/restart
  -> evidence guard
  -> 只重启必要容器
```

已修复：

- ROI/算法配置保存不应触发 full runtime apply；
- 保存后应返回 `containers_restarted=[]` 和 `source_containers_touched=[]`；
- 8090 需要用中文清晰区分“保存配置”和“受控重启/应用运行时”。

## Runtime performance

`/api/v1/runtime/performance-config` 管理：

- analysis FPS；
- Savant max/min FPS；
- batch size；
- pose batch；
- face detector batch；
- face embedding batch；
- max parallel streams；
- batched push timeout。

应用性能参数会影响 forwarder / Savant 容器，必须走 controlled apply。

## Runtime topology

`/api/v1/runtime/topology-config` 支持：

- `auto`
- `single`
- `dual_same_gpu`
- `dual_dual_gpu`

双分支拓扑负责生成：

- Savant branch plan；
- analysis-forwarder branch plan；
- Replay/video-file-sink branch；
- source-to-branch assignment；
- replay shard plan。

pressure harness 已证明同卡双分支 30+30 的 replay shard JSON 可以注入到 clip-worker，
并完成 retained-evidence 证据链验收。

## Evidence guard

任何可能中断取证链路的操作都必须检查 active evidence：

- pending；
- waiting proof；
- replay job created；
- materializing；
- finalizing。

目标是避免 source/replay/savant/worker 重启中断正在生成的证据。旧 active 状态已纳入 stale 终态收敛。

## 8090 后续优化

优先级低于真实 RTSP / soak，但生产前仍需要：

- 鉴权和权限；
- 操作审计更完整；
- 运行状态指标更清晰；
- WebSocket 实时告警；
- 更明确展示 evidence 延迟、队列、topology、RTSP 异常。
