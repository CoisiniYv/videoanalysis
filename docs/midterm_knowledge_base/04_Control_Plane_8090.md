---
type: control-plane-note
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - "8090"
  - control-plane
---

# 8090 控制面

## 产品职责

8090 是唯一主浏览器入口，覆盖摄像头、ROI/规则、人员、人脸、轨迹、evidence、存储、
运行总览、端到端延迟、完整链路启停和高级性能/拓扑配置。

```text
Browser -> evidence-viewer:8090 -> api:8000 -> DB/Docker/runtime
```

`/api/v1/*` 和 `/media/*` 由 8090 代理；FastAPI `/docs` 不在代理范围。

## 保存与应用

```text
保存 ROI/规则
  -> DB update -> config sync -> no full restart

加入运行/改 RTSP/改性能/改拓扑
  -> evidence guard -> controlled apply -> runtime epoch/container changes
```

批量双分支的推荐入口不是逐路 camera enable，而是“选择摄像头并启动”。确认时 API 在
同一受控操作中更新所选摄像头的 enabled 状态并应用拓扑。

## 完整链路预设

- `production_t4_40`：40 路、20/20、4 FPS、MPS、ROI、rolling；
- `local_4090_60`：60 路、30/30、8 FPS、无 MPS、仍有 ROI 与 rolling；
- `custom`：高级运维，自行承担参数和容量验证。

命名预设会覆盖分支参数。页面显示值若与当前代码 preset 不同，应以服务端返回为准。

## 后台 apply

相关接口：

```text
PUT  /api/v1/runtime/topology-config
POST /api/v1/runtime/topology-config/apply-async
GET  /api/v1/runtime/topology-config/apply-status
```

状态阶段：

```text
queued/preflight -> branches -> sources -> rolling_cache_ready/prefill
  -> evidence -> complete | failed | stopped
```

状态原子持久化到 `media/.runtime/topology_apply_status.json`。页面刷新可恢复显示，
但执行线程属于当前 API 进程，API 重启会终止任务并标记失败。

## Runtime latency

`GET /api/v1/runtime/latency` 汇总：

- 最新 frame annotation 的 media lag 与 write age；
- 最新 event/bundle media time 与 DB write age；
- pending/materializing/oldest active task；
- A/B queue、source count 和最大 source receive age；
- 10 秒 healthy、60 秒 warning 阈值。

它是在线信号，不代替 pressure artifact 或逐阶段 trace。

## Evidence guard 与 epoch barrier

性能/拓扑 apply 和受控重启必须检查旧 epoch 活跃任务。默认 blocked 时返回 409；
`force=true` 只用于明确的高风险操作，并将剩余旧 epoch 任务显式终态化，不能作为普通
重试按钮。

## 停止完整链路

`POST /api/v1/runtime/control/dual/stop`：

- 停止动态源和双分支采集/推理；
- 禁用当前摄像头；
- 保留 event/media/rolling 收尾角色；
- 更新 apply-status。

它不等于 `midterm_stop.sh` 整栈停止。

## 当前开放项

- 鉴权/RBAC 和更完整操作审计；
- 跨 API 重启的持久作业队列；
- WebSocket upgrade proxy；
- 自动回滚和断流故障演练；
- 健康脚本清单与当前服务角色同步。
