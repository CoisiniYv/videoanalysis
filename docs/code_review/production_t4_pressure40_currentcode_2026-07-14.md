# 生产 T4 当前代码 40 路 4 FPS 压测与热约束基线（2026-07-14）

## 1. 结论与生产决策

生产机 `192.168.1.100` 在单张 Tesla T4、单卡双 Savant 分支、40 路、
4 FPS 分析、双 YOLO batch 4、ROI AdaFace batch 16、rolling-cache 5+5 秒
evidence 条件下，完成了 600 秒正式采样和 120 秒 drain。最终
`report.json` 状态为 `passed`，`failure_reasons=[]`。

在当前机箱和显卡散热条件下，**40 路作为生产运行与后续回归的默认容量基线**。
暂不把 60 路作为当前生产门禁：用户现场已确认 60 路会触发 T4 降频；本轮 40 路
也出现过一次 30 秒采样点中的全局 10 秒指标窗口下滑，同时 GPU 达到 97% 至
100%，温度达到 84°C 至 85°C，说明热与功耗余量已经不宽。这个决定是当前硬件
环境约束，不代表软件架构永久只能
支持 40 路。完成风道、风扇或散热器整改后，60 路需要重新执行同口径正式门禁，
不能直接沿用本轮结论。

本轮正式 run：

```text
prod_t4_currentcode_pressure40_4fps_5s5s_600s_rerun_20260714T1335CST
```

远端 artifact：

```text
/data/video-analytics/artifacts/prod_t4_currentcode_pressure40_4fps_5s5s_600s_rerun_20260714T1335CST
```

## 2. 生效参数

| 参数 | 生效值 |
| --- | --- |
| 生产机 | `192.168.1.100`，Tesla T4 16 GB |
| 输入 | `rtsp://192.168.1.105:8554/live/1080movie` |
| stream 数 | 40 |
| 推理拓扑 | GPU 0，双 Savant 分支，balanced 20/20 |
| CUDA MPS | 开启；每支 Savant 45%，AdaFace 10% |
| analysis FPS | `4/1`，最低门槛 `3.96` |
| YOLO26 pose batch | 4 |
| YOLOv8 face batch | 4 |
| AdaFace | Redis ROI worker，engine batch 16，batch timeout 200 ms |
| evidence 模式 | rolling cache，分析支路输出 `metadata-only` |
| 原始 evidence FPS 门槛 | 不低于 20 FPS |
| evidence policy | 统一 `5:5` |
| rolling prefill / postfill | 25 秒 / 25 秒 |
| 正式采样 | 600 秒，20 个 30 秒样本 |
| drain | 120 秒 |
| cooldown | `source_id:event_type` 维度 60 秒 |
| media-worker | max active 4，rolling remux worker 1 |
| 结果清理 | `CLEAR_EXISTING_EVIDENCE=0`，保留 warmup 与历史结果 |

40/40 source 在 forwarder 和 Savant 都达到稳定可见，无 source restart。正式
600 秒从 visibility、rolling prefill 和 sampling settle 完成后开始，不把启动等待
算入采样时间。

## 3. 推理吞吐与热状态

### 3.1 正式门禁

| 指标 | 结果 |
| --- | ---: |
| steady counter FPS | `4.0266` |
| 最低要求 | `3.96` |
| steady window FPS mean | `4.0864` |
| forwarder / Savant source | 40 / 40 |
| 最大 instantaneous / confirmed queue | 0 / 0 |
| queue-full samples | 0 |
| Savant send failure sampling delta | 0 |
| raw forwarder 最大 queue | 3 |
| raw drop / send failure | 0 / 0 |

双分支 YOLO pose 和 YOLO face 的 batch-full ratio 在运行中约为 99%，没有退回
batch 1 或形成单帧推理退化。ROI AdaFace 最终发布 21,515 个 ROI，执行 1,649
个 batch，平均 batch size 为 `13.0473`，其中 1,079 个满 batch 16，最终
pending=0。正式窗口 PostgreSQL 中有 21,134 条 AdaFace ROI observation，覆盖
40/40 source，embedding norm 为 `0.99936` 至 `1.00066`。

### 3.2 一次瞬时降速及解释

`runtime_014.json` 中 40 路 10 秒窗口平均值曾降到 `3.6925 FPS`，逐源约为
3.5 至 3.8 FPS；下一样本恢复到 `4.0075 FPS`，再下一样本升到 `4.5125 FPS`。
下滑时：

- forwarder queue、queue-full、send failure 仍为 0；
- Redis events/person/record-request lag 和 pending 为 0；
- evidence task 没有 expired，media-worker 队列仍在收敛；
- T4 GPU 利用率为 97% 至 100%，温度 84°C，功耗触及限制并伴随 SM clock 波动；
- 后续温度达到 85°C。

因此这次抖动不应归因于 rolling-cache 阻塞主推理，也不是 forwarder 丢帧。更符合
T4 在现有散热与功耗条件下接近饱和后的动态降频/调度抖动。整段 counter-delta
门禁仍通过，但其余量只有约 0.67%，不能据此推导 60 路在当前散热下可稳定运行。

## 4. 事件、cooldown 与 evidence 口径

### 4.1 正式采样窗口

| 指标 | 结果 |
| --- | ---: |
| 检测事件总数 | 1,833 |
| intrusion | 1,491 |
| watchlist_hit | 342 |
| cooldown suppressed | 1,280 |
| unsuppressed | 553 |
| tasks / bundles / playable | 553 / 553 / 553 |
| active / pending / expired | 0 / 0 / 0 |
| intrusion 视频 | 331 |
| watchlist 图片 | 222 |

`1,833` 是正式窗口内的真实检测事件数；`1,280` 条由 60 秒
`source_id:event_type` cooldown 抑制，不创建重复 evidence。`553` 条未抑制事件
全部生成可用 bundle，没有 materialization expiry。

### 4.2 全程保留集

本轮设置 `PRESERVE_WARMUP_RESULTS=1`，所以全程保留集大于正式采样口径：

- `593/593` evidence tasks 最终均为 `materialized`；
- 371 个 intrusion 视频；
- 222 个 watchlist 图片；
- 8090 对 593/593 bundle 逐 ID 校验通过；
- cleanup 删除 events、evidence、face/person observations 和 evidence path 的数量
  均为 0。

生产库 evidence bundle 总数从开跑前 1,936 增加到 2,529，刚好增加 593；
`person_bbox_observations` 从 987,390 增加到 1,047,331，证明本轮没有用清理旧数据
来换取门禁通过。

## 5. MOV、timeline、annotation 与 bbox

| 门禁 | 结果 |
| --- | ---: |
| retained 视频 | 371 |
| 唯一窗口 | 371 个 `5:5` |
| duration mismatch / missing | 0 / 0 |
| raw FPS mismatch / missing | 0 / 0 |
| rolling segment source | 40 / 40 |
| rolling segments measured | 3,185 |
| rolling segment FPS p50 | `24.262` |
| rolling full-rate gate | 通过 |
| timeline | 371 / 371 OK |
| annotation | 371 / 371 OK |
| bbox missing | 0 |
| person_context missing | 0 |
| annotation count mismatch | 0 |
| filesystem fallback | 0 |

抽检视频为约 10.01 秒、约 24.01 FPS 的 H.264 MOV。连续抽取 5 个最新视频的
前 1 MB Range，均返回 HTTP 206，首包约为 7.8 至 8.2 ms；annotation API
约为 31 ms。8090 的 timeline/annotation 均来自 PostgreSQL，未回退到文件 sidecar。

## 6. 人员轨迹与 8090 加载

人员轨迹完整性门禁：

- Savant 两分支共导出 59,934 条 person observation；
- PostgreSQL 正式窗口持久化 59,941 条，40/40 source；
- persisted ratio=1.0，loss=0；
- exporter/writer drop=0，writer error=0；
- person-observation consumer 最终 lag=0、pending=0。

日常单分支恢复后，从 8090 实测：

| 请求 | 结果 |
| --- | ---: |
| Finch 轨迹查询，50 条 | HTTP 200，`0.1486s` |
| SSD 轨迹缩略图 | HTTP 200，`0.0052s` |
| 机械盘 face crop 原图 | HTTP 200，`0.0043s` |
| evidence 列表，10 条 | HTTP 200，`0.086s` |
| 5 个 MOV 的 1 MB Range | HTTP 206，`0.0078s` 至 `0.0090s` |
| 最新视频 annotation | HTTP 200，`0.031s` |

本轮没有出现轨迹图片空白、加载超时或视频无法首包播放。

### 6.1 展示入口语义

8090 当前“证据”页面列出 intrusion 视频；watchlist 的 222 个图片 bundle 通过
“人员轨迹”页面展示。因此本轮在证据页看到 371 个视频、在轨迹页查询 watchlist
图片是预期产品分流，不是 222 个 bundle 丢失。

压力 run 的内部 `source_id` 前缀不是日常操作员筛选字段。由于多轮压测复用了相同
camera_id，用完整 run 前缀查询 `/api/v1/evidence` 会混入同 camera_id 的历史视频，
且该视频列表本来就不返回 watchlist 图片。当前 run 总量必须以 report 的逐 ID
校验和 DB scoped count 为准，不能用这个内部前缀的列表 total 代替。

## 7. Warning 与非失败项

最终只有两个 warning：

- `validate_seq_iq_expected_sampling_gap`：原始约 24 FPS 视频按 4 FPS 分析产生的
  预期间隔，不是 raw evidence 丢帧；
- `tensorrt_engine_mps_partition_warning`：完整 T4 TensorRT engine 在 CUDA MPS
  分区下看到较少 SM 的预期提示。engine 正常反序列化并推理，实际 FPS、source
  覆盖和输出门禁均通过。

rolling-cache 直达模式不使用旧 Replay video-file-sink-a 至 h 作为主 evidence
路径。诊断中这些未启动占位实例的 GST error 不影响 rolling-cache bundle；最终
状态、视频可播放性和 DB-backed annotation 已独立验证。

## 8. 运行结束后的保留与恢复

- 40 个 pressure camera 已 disabled，`enabled cameras=0`；
- pressure source、双 Savant、双 rolling sink、ROI worker 和 CUDA MPS 临时容器
  均已停止并删除；
- 日常 14 个 midterm 服务已恢复，单分支 Savant healthy；
- 8090 `/health` 返回 HTTP 200；
- SSD `/home` 使用约 51 GB/379 GB，2 TB `/data` 使用约 25 GB/1.8 TB；
- production 双硬盘挂载和私有配置未改变；
- 本轮未删除任何历史或新生成的 evidence、轨迹和 bbox 数据；
- 本轮压测与文档固化没有新增业务代码修改，也没有覆盖已有 dirty worktree。

## 9. 后续容量策略

在散热整改前：

1. 生产配置保持 40 路、4 FPS、双 YOLO batch 4、ROI AdaFace batch 16。
2. 不以降低分析 FPS、退回 batch 1、关闭 AdaFace/evidence 或放宽门禁来换取 60 路。
3. 监控 T4 温度、功耗限制、SM clock 和 steady FPS；若 40 路持续出现小于 3.96
   的 counter-delta，则应先处理散热，而不是继续叠加 source。
4. 本轮是 40 路生产容量基线，不满足原长期 Goal 中“两轮 60-source +
   restart/recovery soak”的完整 Definition of Done。

散热整改后恢复 60 路验证的前置条件：

1. 同一输入、双分支和 batch 设置下先跑 60 路 60 秒 smoke；
2. 再跑 600 秒正式采样、120 秒 drain，至少连续两轮；
3. 两轮都要求 steady FPS 达标、queue/send failure=0、ROI pending/loss 达标、
   evidence expired=0、5+5/FPS/timeline/annotation/bbox/trajectory 全部通过；
4. 额外执行一次压力运行中的 worker restart/recovery soak；
5. 保存温度、功耗、clock 曲线，证明不再发生持续热降频。

## 10. 复现命令

本轮通过生产机仓库执行：

```bash
DATABASE_URL=postgresql://video:video@127.0.0.1:5439/video_analytics \
RUN_ID=prod_t4_currentcode_pressure40_4fps_5s5s_600s_rerun_20260714T1335CST \
STREAMS=40 \
DURATION_S=600 \
DRAIN_S=120 \
EVIDENCE_GROUP_SIZE=40 \
EVIDENCE_POLICY_GROUPS=5:5 \
PRESSURE_ALGORITHM_COOLDOWN_S=60 \
CLEAR_EXISTING_EVIDENCE=0 \
PRESERVE_WARMUP_RESULTS=1 \
RTSP_REPUBLISH_LOCAL_SERVER=0 \
PYTHONUNBUFFERED=1 \
bash scripts/runtime/run_pressure60_dual1gpu_profile.sh 4fps-t4
```

## 11. Artifact 校验

```text
report.json
4e65285a3c6eeaf8923c47a4b6580e60d2a2386ea024a87c1facd6cb042582d5

sample_summary.json
9776bade6c56db0eafe3ecfedccaadbc7ba3cc4c3127d7eb5879ebbc45bc396c

evidence_window_validation.json
0ebc0641453df13e38af720fdd6582809d466ed1279af87ea178d92a880e43cc

person_trajectory_completeness.json
dc308851d19fd95cb02b41498882c9c2ba9936b0eab24cd63f460191d106af6a

downstream_observability_summary.json
6a33a21832beb16ae9d9e506b9841ab5de61e1316cdc5e5f416dc65beed17c01
```

`report.json` 大小为 8,202,183 bytes。以上 SHA256 均在生产机 artifact 原目录
现场计算，用于防止后续报告被覆盖或与其他 run 混淆。
