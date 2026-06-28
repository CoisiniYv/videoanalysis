# Midterm 8090 推理拓扑管理与 60 路 8fps 压测报告

日期：2026-06-29

## 结论

本次已把推理拓扑控制接入 8090 管理端，并通过 8090 API 路径完成一次
60 路、8fps、单 GPU 同卡双分支压测。

压测结果：`passed`。

Artifact：

`/data/video-analytics/artifacts/pressure60_8090topology_dual1gpu_batch4_8fps_20260628T160720Z`

## 8090 新增能力

8090 现在可以管理推理拓扑，而不是只依赖手工 compose/profile 或压测脚本：

- 拓扑模式：`auto`、`single`、`dual_same_gpu`、`dual_dual_gpu`
- 分片策略：按数量均分、按摄像头 `gpu_id`、手动覆盖
- 分支参数：GPU id、Savant batch、pose batch、face detector batch、AdaFace batch、并行流上限、Forwarder FPS、Savant FPS、batched push timeout
- 手动分配：在 8090 拓扑面板中按摄像头选择 A/B 分支
- 应用动作：通过 `/api/v1/runtime/topology-config/apply` 写入 module/source/replay shard 配置，并重建/启动 A/B 分支容器
- 预检展示：双分支容器是否存在、每分支 source 数是否超过 `max_parallel_streams`、GPU 是否可识别

默认自动策略：

- 启用摄像头数 `<= streams_per_branch`：单分支
- 启用摄像头数 `> streams_per_branch`：自动双分支
- 能检测到两张 GPU 时：A 使用 GPU0，B 使用 GPU1
- 只能检测到一张 GPU 或 API 容器无法检测 GPU 时：A/B 默认同卡，避免误绑不存在的 GPU1

## 本次修复点

本次压测前发现两个 8090 拓扑 apply 问题，并已修复：

1. evidence-viewer 代理超时
   - 8090 代理原先 120 秒超时，长时间拓扑 apply 会被前端代理返回 500。
   - 已改为 `OPERATOR_PROXY_TIMEOUT_SECONDS` 可配置，默认 900 秒，并对超时返回结构化 504。

2. 停止态双分支容器不会自动启动
   - `_recreate_container_with_env()` 默认只在旧容器原本 running 时启动新容器。
   - 双分支容器平时多为 stopped，导致 apply 后停在 `Created`。
   - 拓扑 apply 现在对 `savant-a/b` 和 `analysis-forwarder-a/b` 显式 `force_start=True`。

压测脚本也补了异常清理：apply 中断或失败时会尝试清理本轮 pressure source adapter，避免残留 60 个临时容器。

## 压测配置

命令核心参数：

```bash
python scripts/runtime/run_midterm_pressure60.py \
  --dual-shard-same-gpu --dual-shard-api --dual-shard-gpu 0 \
  --fps 8/1 --min-fps 2/1 \
  --batch-size 4 --pose-batch-size 4 --face-detector-batch-size 4 \
  --face-embedding-batch-size 16 \
  --max-parallel-streams 32 \
  --batched-push-timeout 40000 \
  --duration-s 120 --sample-interval-s 15 \
  --drain-s 0 --guard-wait-s 120 \
  --keep-evidence 0 --max-validate-seq-iq 100000
```

说明：

- 本次是前端推理入口压测，`keep-evidence=0`。
- 本次验证的是 8090 拓扑控制路径，不是双分支证据链路闭环。
- 证据链路仍需要后续用 replay shard 配置和 retained evidence 另行验证。

## 关键结果

| 指标 | 结果 |
| --- | ---: |
| 压测状态 | passed |
| failure_reasons | 0 |
| warnings | 0 |
| max_forwarder_sources | 60 |
| max_savant_sources | 60 |
| max_forwarder_queue_depth | 0 |
| queue_full_samples | 0 |
| max_savant_send_failures_total | 0 |
| source containers | 60 running / 0 exited |
| source restarts | 0 |
| negative PTS | 0 |
| final frames seen | 184,120 |
| final frames forwarded | 55,582 |
| target forwarded frames | 57,600 |
| forwarded / target | 0.965 |
| max forwarder CPU | 36.88% |
| max Savant CPU | 265.91% |
| max source adapter CPU | 5.03% |

本次 8 个采样点中，稳定采样 7 个。采样稳定后 A/B 分支各看到 30 路 source。

## 仍需注意

- Savant 日志仍有 `validate_seq_iq` 高频 WARN，本轮统计为 43,349 条。当前判断仍是采样造成的预期 seq gap，未出现 send failure 或 queue full，但后续应单独做日志降噪。
- 这次 `keep-evidence=0`，所以不能用本报告证明双分支 replay/clip/media/evidence 链路已经生产闭环。
- 恢复后 8090 topology 配置回到 `auto/single`，当前启用摄像头仍为 `lab`，`primary_rtsp` 仍停用。`runtime/overview` 里 `compose_source_not_running` 仍是 primary 停用导致的已知误导项。

## 后续验收边界

本次可以认为完成：

- 8090 可以保存和应用推理拓扑配置；
- 超过 30 路可以通过 `auto` 或手动配置进入双分支；
- 单卡同卡双分支可以通过 8090 路径承载 60 路 8fps 前端推理入口压力；
- 运行控制不再要求操作员理解手工 compose override。

尚未完成：

- 双分支 topology 下的 retained evidence 端到端闭环；
- 双 GPU 真实机器上的 A/B GPU 自动分配实测；
- `validate_seq_iq` 采样日志降噪；
- 8090 runtime health 对停用 primary compose source 的误报降级。
