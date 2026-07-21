# 生产机当前运行、Evidence 固化与性能审计报告

- 审计对象：`192.168.1.100`
- 审计时间：2026-07-15 22:58–23:07 CST
- 运行窗口：当前完整链路已连续运行约 4 小时
- 审计方式：只读查询 8090 API、PostgreSQL、Redis、容器日志、GPU/CPU/磁盘指标与媒体文件抽样
- 数据处理：未停止分析、未删除 Evidence、未修改生产配置

## 1. 结论

当前 40 路生产 T4 单卡双分支链路处于稳定运行状态。推理侧 40/40 路在线，两分支实时队列均为 0，发送失败为 0；AdaFace ROI Stream pending/lag 均为 0。近 4 小时没有 Evidence failed、expired、fallback 或 retry，所有已固化视频都有 timeline、annotation、bbox 和 person_context。

Evidence 固化当前能持续跟上平均流入量，但在 40 路同源同步事件波峰下存在明显的短时排队：最近 10 分钟视频生命周期 p95 为 48.48 秒，其中 queue wait p95 为 47.36 秒；finalizer 自身 p95 为 5.99 秒。并发许可和 remux lane 会在波峰打满，但各小时结束时 active 均回到 0，没有形成跨小时累积。因此当前结论是“可持续运行但突发余量有限”，不能表述为 Evidence 链完全无压力。

推理性能达到目标。两分支 20/20 路，单路有效 FPS 当前为 4.3–4.5，画面接收延迟最大 0.337 秒，forwarder queue=0，累计发送失败=0。GPU 约 69% 利用率、显存 4.28/15.36GB，但温度 84°C，已接近 85°C 最大运行温度；当前受 70W 软件功耗上限限制，SM 时钟约 660–795MHz。4fps 可维持，但不宜据此推断还可无风险扩展到 60 路。

## 2. 当前生效拓扑与配置

| 项目 | 当前值 |
|---|---:|
| 运行 profile | `production_t4_40` |
| 摄像头 | 40 路启用，41 路登记 |
| 分支 | A/B 各 20 路，单卡 GPU0 |
| 分支模式 | `dual_same_gpu` |
| 目标分析 FPS | 4fps |
| YOLO Pose batch | 4 |
| YOLO Face batch | 4 |
| AdaFace batch | 16 |
| Face infer interval | 3 |
| Evidence 窗口 | 前 5 秒 + 后 5 秒 |
| Evidence cooldown | 60 秒 |
| rolling prefill | 25 秒 |
| rolling retention | 600 秒 |
| media-worker 最大在途 | 10 |
| remux worker | 5 |
| finalizer 进程 | 5 |

拓扑 preflight 全部通过；Savant A/B、Replay A/B、rolling sink A/B、ROI AdaFace、event-worker、media-worker 和 CUDA MPS 均在运行。8090 Evidence health 返回 `status=ok, index_source=database`。

运行控制 API 仍把基础单分支容器列为 exited，并把总体 mode 字段显示为 single；这是旧控制面只识别固定单分支角色造成的展示偏差。实际 topology-config、双分支容器、40 路指标和 pipeline.ready 均证明当前是完整双分支运行。此项是控制面语义问题，不是实际链路退回单分支。

## 3. 事件与 Evidence 产出

### 3.1 数量

审计快照时数据库统计如下：

| 时间窗口 | Events | Evidence tasks | Bundles |
|---|---:|---:|---:|
| 最近 10 分钟 | 834 | 384 | 390 |
| 最近 1 小时 | 8,419 | 2,968 | 2,962 |
| 最近 4 小时 | 33,098 | 12,166 | 12,159 |

近 4 小时平均速率：

- Events：约 137.9 个/分钟
- Evidence tasks：约 50.7 个/分钟
- Bundles：约 50.7 个/分钟
- intrusion 视频：7,654 个，约 31.9 个/分钟
- watchlist_hit 图片：4,505 个，约 18.8 个/分钟

近 4 小时真实事件分布：

| 类型 | Events | Tasks | Tasks / Events |
|---|---:|---:|---:|
| intrusion | 27,224 | 7,661 | 28.1% |
| watchlist_hit | 5,875 | 4,516 | 76.9% |

Evidence 数量少于 Events 是 60 秒 recording cooldown 和事件到证据策略的预期结果，不是固化丢失。所有 40 路摄像头在 Events 和 Bundles 中均有覆盖。

最近 4 小时 tasks 与 bundles 相差 7 条，快照时对应正在等待后窗、remux 或 finalizing 的任务；后续小时聚合显示这些任务均已清空。

### 3.2 小时稳定性

| UTC 小时 | Tasks | 完成 | Active | Failed | 生命周期 p50 | p95 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| 11:00 | 2,737 | 2,737 | 0 | 0 | 19.15s | 55.05s | 66.58s |
| 12:00 | 2,991 | 2,991 | 0 | 0 | 21.45s | 56.31s | 74.07s |
| 13:00 | 3,110 | 3,110 | 0 | 0 | 17.44s | 53.98s | 72.94s |
| 14:00 | 2,968 | 2,968 | 0 | 0 | 19.17s | 55.44s | 74.18s |

四个完整小时均无跨小时积压、失败或过期，说明当前平均吞吐可持续。

## 4. Evidence 完整性与可播放性

### 4.1 数据库全量校验（近 4 小时视频）

| 校验项 | 结果 |
|---|---:|
| 视频 bundles | 7,654 |
| 5+5 秒时长超差 | 0 |
| 最短时长 | 9.422s |
| p50 时长 | 10.005s |
| p95 时长 | 10.006s |
| 最长时长 | 10.006s |
| annotation_count=0 | 0 |
| timeline 缺失 | 0 |
| 有对象 overlay 缺失 | 0 |
| bbox 缺失 | 0 |
| person_context 缺失 | 0 |
| storage fallback | 0 |
| failed / expired | 0 |

按审计后稍晚的滚动 4 小时窗口重新展开 overlay JSON，7,631/7,631 个视频同时具有 bbox 和 person_context；数量变化来自查询窗口向前滚动，不是数据减少。

### 4.2 文件与 HTTP 抽样

- 最新 20 个 `raw_clip.mov` 全部可被 ffprobe 解析。
- 平均帧率约 23.98–24.00fps。
- 抽样时长主要为 10.005–10.006 秒，个别为 9.505/9.964 秒，仍在 5+5 秒验收容差内。
- 最新 20 个视频的 HTTP Range 请求全部返回 206。
- 首 1KB Range 响应时间约 4.6–8.2ms。

因此当前 Evidence 视频不是 4fps 推理流，而是约 24fps 原始录像；bbox 来源仍是约 4fps 的分析标注，并通过 DB timeline 对齐播放。

## 5. 固化性能分析

### 5.1 数据库生命周期（近 4 小时）

| 指标 | 数值 |
|---|---:|
| materialized tasks | 12,148（查询时完整落在窗口内） |
| 生命周期 p50 | 19.081s |
| 生命周期 p95 | 55.300s |
| 生命周期 p99 | 63.956s |
| 生命周期 max | 74.182s |

### 5.2 media-worker 最近 10 分钟日志（283 个视频）

| 阶段 | p50 | p95 | max |
|---|---:|---:|---:|
| 总生命周期 | 22.682s | 48.484s | 61.848s |
| queue wait | 21.380s | 47.361s | 61.121s |
| finalization | 4.250s | 5.994s | 6.390s |
| bundle build | 1.849s | 2.540s | 3.666s |
| publish | 2.285s | 3.529s | 4.174s |
| finalizer pool wait | 0 | 0 | 0 |
| DB index | 0.150s | 1.014s | 2.078s |
| timeline index | 0.047s | 0.316s | 1.617s |
| overlay index | 0.025s | 0.233s | 0.887s |

主要结论：当前延迟主要发生在进入 remux/finalizer 前的排队，而不是 ffprobe、ffmpeg 或 finalizer 进程池等待。日志中 ffprobe/ffmpeg 调用均为 0，说明 rolling-cache 直接物化路径正在生效，没有退回完整视频二次处理。

10 分钟 scheduler 指标：

| 指标 | p50 | p95 | max |
|---|---:|---:|---:|
| tick gap | 1.000s | 2.902s | 8.869s |
| tick duration | 7ms | 844ms | 3.026s |
| oldest ready age | 0 | 40.885s | 58.219s |
| active permits | 0 | 10 | 10 |
| remux depth | 0 | 5 | 5 |
| finalizer depth | 0 | 7 | 10 |

这说明系统呈现“平时空闲、同步波峰打满、随后清空”的锯齿形负载。它解释了为什么前端偶尔看到待固化数量突然升高，但长时间观察并没有持续向上累积。

### 5.3 rolling-cache

- SSD 当前 rolling-cache：约 6.3GB、14,094 个文件。
- materialized 临时目录：约 70MB。
- retention 清理实际按 600 秒运行。
- 最近清理每轮删除约 335–480 个过期 segment，状态均为 ok。
- discovery 约 2.4–2.8 秒，锁内持有约 170–247ms。
- 个别周期 lock wait 达 6.8 秒，总周期达 9.94 秒，是 scheduler 最大间隔的一个次要来源。
- segment index hits=8,332、misses=40、fallback scans=0，说明按 segment anchor 的新索引路径生效。
- row-cache eviction 累计仍较高，但 4 小时内没有造成 fallback、过期或持续积压。

发现 39 个来自旧 runtime epoch 的陈旧 `.mov`/partial 文件超过 20 分钟未清理，另有 2 个 lock 文件。它们占总体空间很小，不影响当前 600 秒活跃 retention，但属于后续可清理的历史残留；本次审计未删除。

## 6. 推理与 AdaFace

### 6.1 双 YOLO

| 分支 | 活跃源 | 有效 FPS | Pose FPS | Face FPS | 最近帧最大年龄 | Queue | Send failures |
|---|---:|---:|---:|---:|---:|---:|---:|
| A | 20 | 4.4–4.5 | 4.4–4.5 | 4.4–4.5 | 0.210s | 0 | 0 |
| B | 20 | 4.3–4.4 | 4.3–4.4 | 4.3–4.4 | 0.337s | 0 | 0 |

当前双 YOLO batch=4 没有吞吐异常，40 路均高于 3.96fps 验收线。

### 6.2 ROI AdaFace

- ROI messages published：399,330（worker 生命周期累计）
- stream cleanup deleted：399,330
- Redis ROI stream：XLEN=0、consumer pending=0、lag=0
- AdaFace batches：31,338
- 平均 batch occupancy：12.74/16
- 满 batch=16：17,887 次，占 57.1%
- 平均 TensorRT batch wall time：约 72.5ms
- 当前 `va_adaface_roi_pending=0`
- 最近成功时间持续更新
- 近 4 小时 face observations：389,825
- 512 维 embedding：389,825/389,825
- 来源覆盖：40/40

AdaFace 当前没有形成 GPU 后的独立积压，也没有 Redis pending 或 lag。

## 7. 人脸轨迹与图片

近 4 小时：

- face observations：389,825
- distinct tracks：10,530
- 来源覆盖：40/40
- watchlist_hit：5,851，全部关联 person_id
- 两名已登记人员的轨迹 API 最新页均返回 50 条、`has_more=true`、40 路来源覆盖
- 两人的最新 50 条轨迹均有可显示图片
- 抽样 10 张轨迹缩略图全部 HTTP 200，约 4.2–5.3ms 返回

数据库中的普通 face observation 不长期保存完整截图；389,825 条 observation 中只有与名单命中对应的约 5,851 条保留 crop 路径。这与“只为可检索命中轨迹保存图片、其他 observation 只保存 embedding/关联数据”的当前策略一致。8090 的已登记人员轨迹页当前可正常显示；如果未来要求所有未知人脸也可按图回看，需要单独扩展存储合同，不能把当前结果解释为全量未知人脸图库。

## 8. 前端读取性能

| 请求 | 结果 |
|---|---:|
| Evidence 列表 50 条 | HTTP 200，72KB，0.670s |
| Evidence 列表 20 条 | HTTP 200，29KB，0.464s |
| 最新 10 条详情 | HTTP 200，约 0.030–0.071s/条 |
| 最新 10 条 annotations | HTTP 200，约 278–283KB，0.056–0.089s/条 |
| 最新 20 个视频首段 | HTTP 206，约 0.005–0.008s/条 |
| 轨迹缩略图抽样 | HTTP 200，约 0.004–0.005s/张 |

列表首屏低于 1 秒，详情、标注和视频 Range 均没有超时或空白证据。当前 35,886 个历史 bundles 下，列表 count/filter 查询约 0.5–0.7 秒，尚可接受，但应持续观察总量进一步增长后的分页查询成本。

## 9. 主机资源

### GPU

- Tesla T4
- GPU utilization：约 69%
- 显存：4,279/15,360MB
- 温度：84°C
- 最大运行温度：85°C
- 功耗：约 68–73W / 70W
- `SW Power Cap=Active`
- SM clock：约 660–795MHz，理论 max 1,590MHz

GPU 当前主要受功耗上限约束，温度也接近最大运行温度，但尚未触发 HW/SW thermal slowdown。4fps 已达到，不建议在散热未改善前继续提升路数或模型频率。

### CPU / 内存 / IO

- 16 CPU，采样平均 idle 49.1%，iowait 2.24%
- 内存 30GB，使用 14GB，可用约 14GB
- Swap 仅使用 123MB
- NVMe 实时写入约 46–93MB/s，util 20–23%，不是满载
- 机械盘采样窗口基本空闲；历史平均 util 13.4%，Evidence 落盘未显示持续饱和
- PostgreSQL：21 个 idle、1 个 active，无未授予锁
- Redis：2.22GB，约 1,054 ops/s，无 rejected connection、无 eviction

当前 CPU、数据库、Redis、SSD 和机械盘都不是持续瓶颈。Evidence 波峰延迟主要是任务同步到达、10 个共享许可和 5 个 finalizer 的排队效应，以及每条 bundle/publish 约 4 秒的生产机实际成本。

## 10. 风险与建议

### P1：保持关注 Evidence 波峰排队

当前平均吞吐通过，但 p95 约 55 秒、oldest ready 最大约 58 秒，距离常见 120 秒 deadline 仍有余量但不宽。建议在后续正式门禁中继续以 `expired=0`、oldest ready、queue wait p95 和逐小时 active=0 为联合判据，不要只看某一时刻“固化中=10”。

### P1：改善 T4 散热与功耗条件

84°C、70W power cap active、SM clock 低于理论上限，说明生产机 GPU 余量受物理条件限制。当前 40 路 4fps 可用，但扩容前应先改善风道/风扇/环境温度，并重新观察稳定时钟。

### P2：继续降低 bundle/publish 成本

finalizer 已是 5 个独立进程且 pool wait=0，GIL 不再是当前问题。下一步优化应针对：

1. bundle build p95 2.54 秒；
2. publish p95 3.53 秒；
3. DB index p95 1.01 秒，尤其 timeline 偶发 1.62 秒；
4. 同步 40 路波峰下 permit=10 的排队策略。

不建议简单继续增加进程数，因为机械盘发布、数据库 timeline/overlay 写入和 SSD metadata 操作可能随并发进一步放大。

### P2：处理旧 epoch 残留

增加只清理非活动 epoch 且无 pin 的 staging/segment 巡检，将当前 39 个旧 `.mov`/partial 纳入可审计清理。执行前仍需保留 preview/active-epoch 保护，本报告未做删除。

### P3：修正 8090 控制面 mode 展示

`runtime/control` 仍报告 mode=single，而 topology-config 和实际容器为 dual。建议统一控制面判定，避免操作人员误以为双分支未启动。

## 11. 最终判定

| 维度 | 判定 |
|---|---|
| 40 路双分支推理 | 通过 |
| 双 YOLO batch=4 | 通过 |
| AdaFace batch=16 / ROI 异步链 | 通过 |
| 4fps 稳态 | 通过 |
| Forwarder queue / send failures | 通过 |
| Evidence failed / expired / fallback | 通过 |
| 5+5 秒时长 | 通过 |
| 24fps Evidence 视频 | 通过（抽样） |
| Timeline / annotation / bbox / person_context | 通过 |
| 人脸轨迹可见性 | 通过（已登记人员命中轨迹） |
| Evidence 长时无累计积压 | 通过（4 个完整小时） |
| Evidence 突发余量 | 黄色，有限 |
| GPU 热/功耗余量 | 黄色，有限 |

综合结论：当前生产机 40 路 4fps 链路具备持续运行能力，Evidence 完整性已通过，未发现固化丢失；当前最需要继续优化的是同步事件波峰下的 media-worker 排队和生产机 GPU 物理余量，而不是双 YOLO、AdaFace Redis Stream、PostgreSQL 或 rolling-cache 录像可见性。
