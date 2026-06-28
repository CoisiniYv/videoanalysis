# Midterm 60 路前端推理入口性能诊断

日期：2026-06-28

本轮目标是把性能瓶颈拆开看：

1. `source-adapter -> Replay -> analysis-forwarder -> null sink`：只验证
   forwarder 采样、排队、写出能力，不接 Savant。
2. `source-adapter -> Replay -> analysis-forwarder -> Savant`：接回真实推理，
   扫 `BATCH_SIZE`，判断 Savant 入口/推理消费能力。

所有压测报告都保留在 `/data/video-analytics/artifacts/`。本轮测试均为 60 路，
单轮采样 120 秒，`keep-evidence=0`，因此不以证据保留作为本轮验收目标。
证据链路 50/50 playable、50/50 annotation complete 已由
`pressure60_16p1_20260628T112109Z` 证明过，本轮重点不是证据物化。

## 0. 既有 16fps 基线

既有极限压测 artifact：`pressure60_16p1_20260628T112109Z`。

| 项目 | 数值 |
| --- | ---: |
| 配置 | 60 路 * 16fps，`BATCH_SIZE=4`，300 秒 |
| forwarder seen | 478,104 |
| forwarder forwarded | 94,500 |
| forwarder dropped | 381,555 |
| forwarded / target | 0.33 |
| max queue depth | 2048 |
| queue full samples | 8 / 11 |
| Savant effective FPS avg | 4.39 |
| Savant effective FPS min/max | 3.68 / 5.21 |
| max GPU / decoder | 54% / 53% |
| source adapter 状态 | 60 running，0 exited，0 restart |
| evidence | status passed，保留 50 条，50 条可审查 |

这个基线说明：16fps 配置下输入源和 evidence 侧可以保留样本，但真实推理消费远低于
16fps，forwarder queue 长时间满。因此 16fps 只能作为极限压力观察，不能当作默认生产
指标。

## 1. Analysis-forwarder 隔离结论

| 档位 | Artifact | 状态 | max queue | send failures | forwarded/target | max forwarder CPU |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 60 路 * 4fps | `forwarder60_null_4p1_20260628T122522Z` | passed | 0 | 0 | 1.11 | 26.26% |
| 60 路 * 8fps | `forwarder60_null_8p1_20260628T123012Z` | passed | 1 | 0 | 1.04 | 29.30% |
| 60 路 * 16fps | `forwarder60_null_16p1_20260628T123353Z` | passed | 0 | 0 | 0.72 | 26.45% |

结论：

- `analysis-forwarder` 自身不是 60 路 4fps 或 8fps 的主瓶颈。
- null sink 下 4fps/8fps 都没有 queue 满、没有 ZMQ send failure、60 个 source
  都可见。
- 16fps null sink 没有背压，但只达到目标帧数约 72%。这更像是当前 PTS 采样器在
  24fps 输入帧间隔下的离散步进问题，而不是 CPU 或 ZMQ 写出能力不足。
- 因此，生产目标应继续以 4fps 必保、8fps 优化为主；16fps 只能作为极限观察。

## 2. 接回 Savant 的 batch 扫描

| 档位 | BATCH_SIZE | Artifact | 状态 | max queue | queue full samples | forwarded/target | Savant effective FPS avg | max GPU | max decoder |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 60 路 * 4fps | 4 | `pressure60_4p1_20260628T123808Z` | passed | 1589 | 0 | 1.31 | 4.15 | 59% | 62% |
| 60 路 * 8fps | 4 | `pressure60_8p1_20260628T124310Z` | failed | 2048 | 3 | 0.71 | 4.00 | 42% | 45% |
| 60 路 * 8fps | 8 | `pressure60_8p1_20260628T124750Z` | failed | 2048 | 2 | 0.68 | 4.07 | 89% | 79% |
| 60 路 * 8fps | 16 | `pressure60_8p1_20260628T125230Z` | failed | 2048 | 3 | 0.83 | 5.16 | 68% | 77% |
| 60 路 * 8fps | 32 | `pressure60_8p1_20260628T130107Z` | passed | 1954 | 0 | 0.91 | 5.82 | 68% | 86% |

结论：

- 60 路 * 4fps 在 `BATCH_SIZE=4` 下可以通过前端推理入口压力测试。
- 60 路 * 8fps 在 `BATCH_SIZE=4/8/16` 下仍会出现 forwarder queue 满。
- `BATCH_SIZE=32` 明显改善 8fps 档位，queue 没有采样到满，脚本门槛通过；但
  `forwarded/target=0.91`，Savant 10 秒窗口 effective FPS 平均约 5.82，不能宣称
  已经稳定达到 60 路 * 8fps。
- GPU 利用率没有长期打满，但 decoder 在 batch32 下最高到 86%，说明下一步不能只
  盲目加 batch；需要继续看 Savant 单实例 pipeline、模型 interval、后处理和分片。

## 3. 阶段级指标缺口

本轮从 `runtime/overview` 和压测 samples 中能稳定拿到：

- `va_savant_effective_fps`；
- forwarder seen / forwarded / dropped / queue depth / send failures；
- source adapter 容器状态；
- Docker CPU；
- `nvidia-smi` GPU / decoder / memory。

当前没有直接暴露：

- model batch latency；
- DeepStream queue / batch wait；
- pose stage FPS；
- face detector stage FPS；
- AdaFace embedding stage FPS；
- 单个 pyfunc 后处理耗时。

因此本轮可以判断“瓶颈不在 forwarder，8fps 主要卡在 Savant 消费/推理链路”，但还不能
精确证明是 pose、face、embedding、pyfunc 后处理还是单实例 pipeline 调度中的哪一段。
下一次继续优化 8fps 时，应先补 Savant 阶段级 metrics 或在现有 patch/pyfunc 中加低频
耗时统计，再决定是调 interval、降低 annotation 输出、还是拆 Savant shard。

## 4. 当前生产建议

- 必保生产档：`60 路 * 4fps`，当前单 4090 前端推理入口已跑通。
- 4090 优化档：`60 路 * 8fps`，推荐从 `BATCH_SIZE=32` 继续优化，但还不能当作
  已完成的生产承诺。
- 16fps：不作为默认目标。当前 forwarder null sink 不堵，但实际采样帧数不足；
  之前 `pressure60_16p1_20260628T112109Z` 也显示接 Savant 后 effective FPS 只有
  约 4.39，forwarder queue 多次满。

## 5. 本轮代码改动

- `analysis-forwarder` 支持 `FORWARDER_OUT_ENDPOINT=null://...` 诊断模式。
  该模式保留 reader、sampler、bounded queue 和 writer loop，只把最终写 Savant
  替换为立即成功计数，用于隔离 forwarder 自身能力。
- `infra/docker-compose.midterm.yml` 的 `FORWARDER_OUT_ENDPOINT` 改为可由环境覆盖，
  默认仍是 `dealer+connect:tcp://savant-security:5557`。
- `scripts/runtime/run_midterm_pressure60.py` 增加 `--forwarder-null-sink`，并在
  sample summary 中记录 forwarder seen/forwarded/dropped、forwarded/target、queue
  full samples、Docker CPU。
- 压测脚本增加恢复后的第二次 pressure 前缀清理，避免 worker 迟到写入的临时
  pressure evidence task 阻塞下一轮 guard。

## 6. 下一步

1. 在 `BATCH_SIZE=32` 的基础上继续诊断 Savant 内部：
   - pose / face / embedding interval；
   - frame annotation 输出频率；
   - 单实例 pipeline 是否存在 CPU/后处理瓶颈；
   - 是否需要把 60 路拆成两个 Savant shard。
2. 重新做端到端生产验收：
   - 60 路 * 4fps，证据 50/50 playable 且 annotation complete；
   - 60 路 * 8fps，先作为 4090 优化目标，不直接承诺生产弱卡。

## 7. 单卡双分支 30+30 压测

本次新增 `--dual-shard-same-gpu` 压测模式：60 路临时 camera 仍由
PostgreSQL/8090 export 生成 Savant 规则配置，但 source manifest 由压测脚本写入
artifact 目录，前 30 路连接 `replay-a -> analysis-forwarder-a -> savant-a`，后 30 路
连接 `replay-b -> analysis-forwarder-b -> savant-b`。两个 Savant 实例都通过临时
compose override pin 到同一张 GPU 0，`savant-b` 也复用
`/data/video-analytics/models`，不再要求单独的 `models-savant-b`。

第一次 4fps 同卡双分支尝试
`pressure60_dual1gpu_4p1_20260628T134436Z` 失败，原因不是 Savant 分片本身，而是
压测脚本绕过 8090 直接调用 `camera_source_controller.py` 时，controller 仍使用旧的
`RTSP_TRANSPORT=tcp`。这与 8090 runtime apply 的
`tcp,use_wallclock_as_timestamps=1,fflags=+genpts` 不一致，导致 48 次 source adapter
重启和 48 次 negative PTS。已将 controller 默认 RTSP 参数对齐 8090 runtime apply。

修复后结果：

| 档位 | BATCH_SIZE | Artifact | 状态 | max queue | queue full samples | send failures | forwarded/target | avg effective FPS | max GPU | max decoder | source restart / negative PTS |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 60 路 * 4fps | 4 | `pressure60_dual1gpu_4p1_20260628T135936Z` | passed | 0 | 0 | 0 | 1.09 | 3.91 | 70% | 50% | 0 / 0 |
| 60 路 * 8fps | 4 | `pressure60_dual1gpu_8p1_20260628T140351Z` | passed | 771 | 0 | 0 | 1.00 | 7.41 | 98% | 100% | 0 / 0 |

结论：

- 同一张 4090 上拆成两个 Savant 分支，能明显改善单实例 60 路 8fps 的入口背压。
  单实例 `BATCH_SIZE=4/8/16` 8fps 会 queue full；同卡双分支 `BATCH_SIZE=4`
  没有 queue full、没有 send failure。
- 8fps 通过时 decoder 已到 100%，GPU 也到 98%，因此这不是宽裕配置。生产上可以把
  单卡双分支 8fps 作为 4090 优化档，而不是弱卡默认承诺。
- 4fps 仍是更稳的生产必保档；8fps 需要保留 runtime metrics 观察，尤其是 decoder
  利用率和 forwarder queue 是否持续非零。
- 本轮仍是 `keep-evidence=0` 的前端推理入口压测，不代表证据链路也在同样拓扑下完成
  replay shard 取证。若后续要把双分支拓扑用于证据物化，需要同时让 clip-worker 使用
  对应 replay shard plan。

## 8. 8090 拓扑管理路径复测

后续已把双分支拓扑控制接入 8090 管理端，并通过 8090 API 路径复测 60 路 8fps：

| 档位 | 拓扑入口 | Artifact | 状态 | max queue | send failures | forwarded/target | source restart / negative PTS |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| 60 路 * 8fps | 8090 `topology-config/apply`，双分支同卡 GPU0 | `pressure60_8090topology_dual1gpu_batch4_8fps_20260628T160720Z` | passed | 0 | 0 | 0.965 | 0 / 0 |

本次复测说明：旧的手工 compose override 双分支路径已经可以被 8090 拓扑管理路径替代。
详细报告见 `docs/midterm_8090_topology_management_8fps_report_2026-06-29.md`。

仍需保持边界清晰：该复测依旧是 `keep-evidence=0` 的前端推理入口压测，不是双分支
证据链路闭环。双分支证据链路还需要 retained evidence、Replay shard、clip-worker
routing、media-worker finalization 一起验收。
