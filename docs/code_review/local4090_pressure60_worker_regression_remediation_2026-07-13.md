# 本机 4090 双分支 worker 性能回归修复与压力门禁审计（2026-07-13）

## 结论

本机 RTX 4090 在单卡双 Savant 分支、60 路、8fps、双 YOLO batch=4、
AdaFace ROI batch=16、rolling-cache 5+5 秒证据条件下，已完成 600 秒正式采样、
120 秒 drain、cooldown 30 秒压力门禁。最新 native-24fps 运行的证据、bbox、轨迹和
推理吞吐门禁全部通过；总报告为 `failed_pressure_gates`，唯一失败项是
`adaface_roi_watchlist_events_zero`。2026-07-14 复核确认该项不是图库或模型问题，
而是当时错误地把另一段 60.019 秒视频循环成 1200 秒，丢失了 Reese/Finch 所在内容。

本轮确认原来约 400 秒后出现的推理下降并非 media-worker 或 evidence 落盘直接
阻塞主推理链，而是 60 个 publisher 从同一个固定 fixture 的同一内容时间点播放，
把高人脸密度画面同时送入两个 YOLO-Face 分支。普通 source 启动 stagger 只错开了
连接时间，没有错开视频内容相位，因此不能消除该人工同步峰值。

### 2026-07-13 证据帧率口径补充

上述 `passed` 对 60 路、双 YOLO 8fps 和 AdaFace ROI 的推理吞吐仍然有效，但不再
作为“原始帧率证据播放”门禁通过。复核发现该轮 publisher 输入本身是
`1080movie_o300_8fps_gop8_continuous_1200s.mp4`；抽检最新 12 个
`raw_clip.mov` 均约为 8fps，10 秒只有 73 到 80 帧。rolling-cache 接点没有落到
Savant 后：正常拓扑仍为 `Replay -> raw fanout -> rolling-cache`，分析支路再经过
resampler 后进入 Savant；该轮高密度 profile 则直接进入 raw fanout、绕过 Replay，
同样位于 resampler/Savant 之前。画面卡顿的直接原因是 raw fanout 收到的 fixture
已经只有 8fps，而不是 rolling-cache 又从 Savant 后取帧。

修复后，4090 profile 使用 23.976fps、GOP 约 0.5 秒的 1200 秒连续 fixture：

```text
/data/video-analytics/pressure-fixtures/1080movie_o300_native24_gop12_continuous_1200s.mp4
```

2026-07-14 已用真正的 44 分钟原片从第 300 秒重新生成该 fixture。它与旧可命中
8fps fixture 的 SSIM 为 `0.991608`，最终低码率版 SHA256 为
`688112c4172d9ab1328db717004e963cb0b9cf4f3642f5c9016ad9d1d76b1370`。原错误文件保留为
`1080movie_o300_native24_INVALID_60sloop_20260713.mp4`，不得再用于验收。

第一次纠正内容时使用 ultrafast CRF，fixture 达到 `8.36Mbps`，60 路约 `500Mbps`，
使 sampling settle 期间 forwarder queue 从 `214` 持续增长到 `10063`，正式 600 秒
采样未启动。最终 fixture 改为 `2.228Mbps`、23.976fps、GOP12，60 路输入约
`134Mbps`，同时保持与旧可命中 fixture 的高画面一致性。高码率中间文件保留为
`1080movie_o300_native24_HIGHBITRATE_8p36Mbps_20260714.mp4`，不得作为默认压力输入。

`--fps 8/1` 只表示分析 cadence；新增 `--rolling-cache-min-raw-fps 20`，正式 profile
若再次误用 8fps fixture 会直接触发 `rolling_cache_segment_fps_below_full_rate`，
最终每个 `raw_clip.mov` 也会从同一次 ffprobe 中读取实际 fps，低于 20fps 时触发
`evidence_clip_frame_rate_mismatch`，不能再得到 `passed`。因此下一轮同口径正式
验收必须同时证明约 24fps evidence 和约 8fps Savant 分析吞吐。

同次复核还修复了 8090 历史证据首屏：数据库有 23,274 个 bundle 时，旧 SQL 请求
最新 10/50 条均约 10.8 秒，因为先对所有候选做 artifact/task enrichment 再分页。
现在先 materialize 最新 page，再只补齐该页关联字段；本机实测最新 10 条约 85ms、
50 条约 125ms。MOV 已有 faststart，`moov` 位于文件头，HTTP Range 返回 206，
因此首屏慢点不在机械盘视频索引。

正式 artifact：

```text
/data/video-analytics/artifacts/local4090_dual60_8fps_600s_queueconfirm_20260713T160000Z
```

## Native-24fps 正式复测结果

Run id：

```text
local4090_dual60_native24raw_8fpsbbox_600s_20260713T155919Z
```

Artifact：

```text
/data/video-analytics/artifacts/local4090_dual60_native24raw_8fpsbbox_600s_20260713T155919Z
```

### 数据清理与参数

- 开始前删除 `129687` 条旧 events、`23274` 个旧 bundles 和 `13838` 个旧 evidence
  路径；清理后 evidence 相关表和目录均为 0。
- 按用户要求保留既有轨迹表，未删除历史 `face_observations` 和
  `person_bbox_observations`。
- 60 路、单张 RTX 4090、双 Savant 分支，原始输入 23.976fps，分析 cadence 8fps。
- YOLO pose batch=4、YOLO face batch=4、AdaFace engine batch=16，ROI batch
  timeout 40ms。
- rolling-cache 5+5 秒，600 秒采样、120 秒 drain、cooldown 30 秒。
- 每路 fixture 内容相位错开 4 秒，避免 60 路重复素材在同一内容位置制造同步峰值。

### 视频、bbox 与 8090

- 正式窗口 `608/608` tasks materialized，`608/608` bundles playable；最终连同
  warmup 保留 `636` 个视频 evidence，本轮未删除任何结果。
- `636/636` 均为 5+5 秒，duration mismatch、frame-rate mismatch 和
  frame-rate missing 均为 0。
- rolling segment 帧率 p50 为 `24.231fps`；每个最终 MOV 均通过不低于 20fps
  的硬门禁。抽检最新 MOV 为 H.264、`23.976fps`、`9.789s`、`235` 帧。
- HTTP Range 抽检返回 `206 Partial Content`，可供浏览器渐进加载。
- 8090 detail、DB-backed timeline 和 DB-backed annotation 均为 `636/636` OK；
  `bbox_missing=0`、`person_context_missing=0`、filesystem fallback=0。
- 抽检最新一条 evidence 得到 48 条可显示 annotation、46 个 face bbox、17 个
  person-context bbox；前几个 `clip_frame_index` 为 `0,2,5,8,11,14,17,20`，符合
  约 24fps MOV 上约 8fps 标注的帧间隔。

### 推理与人脸轨迹

- steady effective FPS 为 `8.0145`，要求不低于 `7.92`；Savant/forwarder source
  覆盖 `60/60`。
- confirmed forwarder queue 峰值 0；一次瞬时 queue=1 经复核为 transient；
  queue-full、send failure 和 drop 均为 0。
- 人员轨迹 persisted `13524` 条，覆盖 `60/60` source，persisted ratio `1.0`，
  loss=0，最终 consumer lag/pending 均为 0。
- 本轮共保留 `4677` 条 face observations，覆盖 `60/60` source；其中正式采样窗口
  `4248` 条，embedding norm 为 `0.999511` 到 `1.000297`。
- AdaFace ROI worker published `4596`，最终 pending=0，证明 ROI 推理和持久化链路
  正常。

### 唯一未通过项及 2026-07-14 纠正

总报告状态为 `failed_pressure_gates`，仅有
`adaface_roi_watchlist_events_zero`。该结论已被 2026-07-14 的素材身份复核取代：
当时使用的是由 `1080movie_fixed_20260706T065104Z.mp4` 通过 `-stream_loop 19` 生成的
60 秒循环，而旧可命中 fixture 来自 44 分钟原片第 300 秒后的连续内容。

重新生成正确 native24 fixture 后，1 路 90 秒真实链路预检在阈值 `0.60` 下产生
`2` 个正式窗口 watchlist hit，数据库最终可见 `4` 个命中，最高相似度
`0.663339`；AdaFace ROI observations `155`，pending=0。预检保留 `6/6` 条 8090
evidence，bbox/timeline 无缺失。其总状态仅因单路无法满足面向 60 路 batch 吞吐的
steady-FPS 门禁而失败，不影响名单匹配证明。正式 60 路 600 秒运行需要使用纠正后的
fixture 重跑，上一轮“0 命中”不能再作为模型或图库结论。

8090 注册人员轨迹接口复核：Finch 返回本轮 `4` 条轨迹，最高相似度 `0.663339`，
每条均有 `trajectory_thumbnail_url` 和 `face_crop_url`，因此“按人员 ID 查询并显示
轨迹图片”的前端合同也已恢复。4090 默认 profile 同时固定正确 fixture 的 SHA256；
同名错误素材会在正式压测启动前失败，不能再静默跑完整轮。

## 2026-07-14 正确原片最终 60 路正式门禁

最终 run：

```text
local4090_dual60_native24_lowbitrate_8fps_600s_final_20260714T0140CST
```

Artifact：

```text
/data/video-analytics/artifacts/local4090_dual60_native24_lowbitrate_8fps_600s_final_20260714T0140CST
```

结果为 `passed`，没有 failure reason，唯一 warning 仍为采样语义允许的
`validate_seq_iq_expected_sampling_gap`：

- 60 路、双 Savant、600 秒、分析 8fps；双 YOLO batch=4、AdaFace batch=16，
  cooldown 30 秒。
- steady FPS `8.0439`，60/60 source 覆盖；forwarder confirmed/instantaneous queue
  峰值均为 0，queue-full、send-failure delta 均为 0。
- 正式窗口 `3472` 个事件：`2899` intrusion、`573` watchlist hit；两类均覆盖
  60/60 source。
- 正式窗口 `1503/1503` tasks/bundles materialized；最终连同 warmup 保留
  `1560/1560` bundle，active/pending/expired 均为 0。
- 最终 `1014/1014` 个视频均为 5+5 秒，duration mismatch、fps mismatch 和 fps
  missing 均为 0；rolling segment 帧率 p50 `24.219fps`。
- `1560/1560` 8090 bundle detail OK；视频 timeline、annotation 均为
  `1014/1014` OK，`bbox_missing=0`、`person_context_missing=0`、fallback=0。
- 8 条 annotation count 差 1 均是多个 DB overlay row 合并到同一
  `clip_frame_index`，可显示 bbox/person_context 完整，不是缺框。
- 人员轨迹 persisted `101438` 条，60/60 source，loss=0、persisted ratio=1.0，
  最终 consumer lag/pending 均为 0。
- 正式窗口 AdaFace ROI observations `30163`，60/60 source；worker published
  `33886`，pending=0。
- Finch 轨迹 API 至少返回前 `200` 条且 `has_more=true`，200/200 有缩略图，最高
  相似度 `0.654571`。

8090 当前视频 evidence 列表总数为 `1014`；其余 `546` 个 retained bundle 是
watchlist 图片/轨迹类 evidence，通过人员轨迹页面展示。因此“列表只有1014”不是
60 路缺失，也不是 bundle 丢失，而是视频证据与人脸轨迹证据的展示入口不同。

正式轮结束后已停止所有 pressure source、临时 MediaMTX、双分支、rolling sink 和
AdaFace ROI worker，恢复日常单分支运行；本轮 evidence 和轨迹均保留。

本轮运行结束后已恢复日常单分支，60 个 pressure source、临时 MediaMTX、双分支
rolling sinks 和临时 AdaFace ROI 容器均停止；636 个 evidence、人员轨迹和人脸
observations 保持可见。生产机 `192.168.1.100` 未修改。

## 修复内容

### Fixture 内容相位

- 压测 runner 新增 `--rtsp-republish-input-offset-step-s`。
- 4090 profile 固定每路错开 4 秒：第 0 路为 0 秒，第 59 路为 236 秒。
- source 启动 stagger 继续保留，但不再把它误认为内容错相手段。

### Media Worker P0

- 启用 `RollingSegmentIndex` 后，scheduler 不再与 remux worker 重复发现 segment。
- root generation 使用 incremental refresh，不再为每个任务全量重建索引。
- rolling video/image candidate SQL 使用正式 lifecycle 字段，并与 Migration 030
  的索引口径一致。
- 新增 `MEDIA_WORKER_LEGACY_DERIVATIVES_ENABLED`；正式压力 profile 关闭 legacy
  snapshot/annotation sidecar 扫描，但保留 rolling image 和 DB expanded
  timeline/overlay/person-context 写入。
- 压力 profile 使用 materialization max active 12、rolling remux workers 8、
  max-per-poll 4、row cache 2048、reconcile 60 秒。

### 本机单盘和数据库隔离

- 正式采样期间暂停 Redis 自动 RDB，结束后恢复原 `save` schedule。
- PostgreSQL 压测期间临时使用 8GB WAL、30 分钟 checkpoint 和 WAL compression，
  结束后恢复原设置。
- rolling-cache 与 materialized 中间目录放在 `/dev/shm`；最终
  `raw_clip.mov` 仍落到 `/data/video-analytics/media/evidence`。
- tmpfs 中 root 创建的文件由一次性、无网络 helper 清理。

### Forwarder queue 门禁语义

完整 runtime overview 的原始值不会被覆盖：

- `queue_depth_instantaneous`：完整 overview 当时看到的瞬时值。
- overview 瞬时值大于 0 时，以 0.2 秒间隔读取两支 forwarder 的轻量 metrics，
  连续复核 5 次，并保存全部 confirmation samples。
- 任一完整复核样本归零且所有读取都成功，`queue_depth_confirmed=0`，归类为
  transient；所有样本持续非零则保留非零 confirmed depth。
- 任一次读取失败都 fail-closed，confirmed depth 保持原瞬时值。
- `queue>=2048` 的 queue-full 门禁始终使用瞬时值；后续归零也不能清除该失败。
- send failure、drop 和 queue-full 的原有门禁未放宽。

这样可以区分“生产者刚入队、消费者尚未取走的一帧”和持续 backlog，同时保留
完整审计数据，不能用复核机制隐藏真实拥塞。

## 正式门禁结果

### 推理与入口

- 20 个正式采样样本，steady effective FPS：`7.9948`，要求 `>=7.92`。
- forwarder/Savant source 覆盖：`60/60`。
- max instantaneous queue：`0`。
- max confirmed queue：`0`。
- queue-full samples：`0`。
- Savant send-failure sampling delta：`0`。
- pose objects：`387463`；face objects：`393299`。
- 唯一 warning 是 `validate_seq_iq_expected_sampling_gap`，不是压力失败。

### AdaFace 与轨迹

- AdaFace ROI DB observations：`39342`，source 覆盖 `60/60`。
- embedding norm：`0.999361` 到 `1.000689`。
- ROI producer/worker 无 queue drop，最终 pending 为 0。
- person trajectory producer：`140895`；PostgreSQL persisted：`140902`。
- 轨迹 source 覆盖 `60/60`，loss count `0`，persisted ratio `1.0`。
- consumer 最终 lag/pending 均为 0。

### Evidence 与 8090

- 正式采样窗口：`1679/1679` tasks materialized，`1679/1679` bundles playable，
  active/expired/blocking 均为 0。
- 因启用了 `preserve_warmup_results`，8090 最终保留并检查 `1753` 条：
  `1098` 个视频和 `655` 个图片 evidence。
- `1098/1098` 视频均为 5+5 秒；duration mismatch 为 0，实际时长约
  `9.873s` 到 `10.150s`。
- 8090 bundle detail：`1753/1753` OK。
- DB-backed timeline：`1098/1098` OK，missing/fallback 均为 0。
- DB-backed annotation：`1098/1098` OK。
- `bbox_missing=0`，`person_context_missing=0`，filesystem fallback=0。
- 两个 annotation count 差 1 的记录属于多个 DB overlay row 合并到同一
  `clip_frame_index`，可显示 bbox/person-context 完整，因此不是缺框。

### 保留与恢复

- cleanup 只禁用 60 个临时压力摄像头；删除 events、face observations、person
  bbox observations、evidence path 的数量全部为 0。
- Redis pressure stream 临时数据已清理；数据库中的轨迹、截图和 evidence 未删除。
- 压测 tmpfs 清理 `10,955,486,680` bytes，最终 evidence 未被清理。
- Redis RDB schedule 和 PostgreSQL checkpoint/WAL 设置均恢复。
- 所有 pressure source、临时 MediaMTX、双分支与 ROI worker 均停止。
- 日常单分支 Savant、forwarder、Replay、clip/media/event/face worker 和 lab camera
  已恢复，runtime health 为 healthy。

## 验证命令

```text
pytest -q harness/tests/test_midterm_pressure60_script.py
# 185 passed

pytest -q \
  harness/tests/test_media_worker_finalizer_boundary.py \
  harness/tests/test_media_worker_perf_safety.py \
  harness/tests/test_media_worker_scheduler_v2.py \
  harness/tests/test_media_worker_segment_index.py
# 98 passed

python -m py_compile \
  scripts/runtime/run_midterm_pressure60.py \
  services/media-worker/app/worker.py \
  services/media-worker/app/segment_index.py
bash -n scripts/runtime/run_pressure60_dual1gpu_profile.sh
git diff --check
```

## 已知部署遗留

本机 PostgreSQL 容器名仍为 `phase0-postgres`。这是旧归档 compose 创建并沿用的
容器命名，不是本轮压力修复产生的服务，也没有在本轮自动重建或迁移。生产部署前
应单独核对 compose project/container ownership，不能把该历史命名问题与 worker
性能回归混为一谈。

本轮仅修改并验证本机 repo；未更新 `192.168.1.100` 生产机。
