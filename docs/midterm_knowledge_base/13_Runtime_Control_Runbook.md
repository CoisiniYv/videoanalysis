---
type: runtime-runbook
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - runtime
  - 8090
  - operations
---

# 运行控制手册

本页解释哪些操作只是保存配置，哪些操作会影响运行时容器，以及每种操作应该如何验收。

## 控制面分层

```text
8090 operator UI
  -> evidence-viewer proxy
  -> api /api/v1/*
  -> PostgreSQL truth
  -> generated runtime snapshots
  -> Docker Compose / dynamic source containers
```

分层原则：

- 8090 是用户入口；
- API 是内部控制面；
- PostgreSQL 是配置事实源；
- generated YAML/source manifest 是运行快照；
- Docker 容器状态是运行事实；
- 证据生成中的任务由 evidence guard 保护。

## 保存配置

保存配置包括：

- 修改 ROI；
- 修改检测线；
- 修改算法阈值；
- 修改 cooldown；
- 修改 watchlist target person；
- 启用/禁用某个摄像头上的某个规则。

期望行为：

```text
DB update
  -> config sync
  -> generated camera/source snapshot
  -> no full runtime apply
  -> no unrelated container restart
```

验收：

- API 返回保存成功；
- `cameras.midterm.yml` 或 generated source snapshot 反映新配置；
- 8090 刷新后配置仍存在；
- 不重启 Savant / Replay / source adapter / clip-worker / media-worker；
- evidence guard 不应该因为普通保存被触发。

## 启停摄像头

启停摄像头会影响 source adapter，属于运行态变更。

期望：

- 只影响对应 source；
- 不重启不相关 Replay/Savant/worker；
- 8090 应提示这是运行态操作；
- runtime overview 应显示 enabled/disabled 和容器真实状态。

风险：

- 如果实现误走全量 apply，可能中断其他证据；
- 如果 generated source snapshot 滞后，8090 显示会和 Docker 状态漂移；
- `primary_rtsp` disabled 时，compose source exited 不一定是故障。

## 受控重启 / runtime apply

触发场景：

- 修改 RTSP URL；
- 变更 source adapter 形态；
- 修改 performance config 并 apply；
- 修改 topology config 并 apply；
- 手动点击受控重启；
- 单/双分支运行模式切换。

必须保护：

- active `evidence_tasks`；
- replay job；
- media finalization；
- Redis pending record request；
- video-file-sink 输出目录。

受控重启应记录：

- operator；
- operation type；
- target services；
- before/after health；
- evidence guard result；
- containers touched；
- runtime epoch。

## Performance config

8090 可配置的性能参数包括：

- analysis FPS；
- Savant max/min FPS；
- batch size；
- pose batch；
- face detector batch；
- embedding batch；
- max parallel streams；
- forwarder queue size；
- infer interval / ReID interval。

语义：

- 保存性能参数只是写配置；
- apply 性能参数会重建相关 forwarder/Savant 容器；
- apply 前必须判断 evidence guard；
- apply 后要等 Savant ready，并记录 effective FPS。

风险：

- 单路测试 batch 可能凑不满，不代表 60 路；
- batch 过大可能增加 latency；
- forwarder queue 堆积可能来自 Savant 消费不足，而不是 forwarder 本身慢；
- 16/1 入口压力不等于 16 FPS 推理通过。

## Topology config

支持模式：

- `auto`
- `single`
- `dual_same_gpu`
- `dual_dual_gpu`

分片策略：

- balanced；
- gpu id；
- manual；
- 30+30 双分支。

apply 输出：

- Savant branch plan；
- forwarder branch plan；
- replay shard plan；
- source adapter assignment；
- runtime epoch；
- generated config。

双分支验收必须证明：

- topology 文件 SHA256；
- clip-worker 读取的 `REPLAY_SHARDS_JSON` / config path SHA256；
- retained evidence 分布在正确 shard；
- 8090 list/detail 可查；
- Replay/video-file-sink 路由一致。

## Evidence guard

目标：

- 避免重启中断正在生成证据；
- 避免 active task 永久卡住；
- 避免旧 pending message 反复 reclaim。

guard 应看：

- `evidence_tasks` active statuses；
- `materialization_status`；
- deadline 是否已过；
- stale 终态收敛；
- Redis pending；
- media-worker backlog。

允许的情况：

- 没有 active evidence；
- active evidence 已 stale 并可终态化；
- 操作只影响非运行态配置；
- 用户明确执行强操作并留下审计。

## Runtime overview

8090 runtime overview 不应只看 compose 静态服务。

需要区分：

- static compose source；
- dynamic source containers；
- DB enabled/disabled；
- generated source manifest；
- Savant active module；
- forwarder metrics；
- Replay/service health；
- worker health。

曾经出现的误导项：

- `compose_source video-analytics-midterm-source-adapter exited`，但对应 `primary_rtsp` disabled；
- 实际启用的是 dynamic `lab` source；
- 因此 disabled compose source 不应被当成 P0 健康问题。

## 操作前检查

```bash
git status --short
docker ps -a --format '{{.Names}}\t{{.Status}}'
docker exec video-analytics-midterm-redis redis-cli XPENDING security.record_requests clip-workers-midterm
```

如果是 runtime apply，还要检查：

- active evidence tasks；
- media-worker 是否正在 materializing；
- clip-worker 是否有 pending；
- Replay/video-file-sink 是否有当前 job 输出。

## 操作后检查

基本检查：

- 8090 可访问；
- API `/health` / `/ready`；
- Docker 目标容器 running；
- Redis lag/pending 不增长；
- Savant effective FPS 恢复；
- forwarder queue 不持续堆积；
- retained evidence 可查询。

配置检查：

- DB 配置存在；
- generated YAML/source manifest 更新；
- Savant rules 加载；
- 8090 刷新后仍显示保存值。

## 不该做的事

- 不要因为 ROI 保存去重启整条推理链；
- 不要把 generated YAML 手工改成配置真相；
- 不要在 evidence 生成中无保护重启 Replay/video-file-sink/media-worker；
- 不要无故 rebuild 镜像；
- 不要用旧 runtime snapshot 判断 DB 配置；
- 不要把 disabled source 的 exited 状态当成故障。
