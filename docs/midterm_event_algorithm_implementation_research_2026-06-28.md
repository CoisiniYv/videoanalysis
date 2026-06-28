# Midterm 事件算法实现方式调研 - 2026-06-28

本文调研当前 midterm 支持矩阵里的算法，按以下优先级给出推荐实现：

1. 性能消耗最小；
2. 集成最方便；
3. 准确度可接受，并保留后续升级路径。

结论先行：当前阶段不建议新增动作识别模型或新的 GPU 推理链。最合适的路线是复用现有
`YOLO26-pose/person -> nvtracker -> behavior_rules` 和
`YOLOv8-Face -> AdaFace -> face-worker` 输出，在 `custom.rules` 里补轻量规则。

## 外部依据

NVIDIA DeepStream 官方 `gst-nvdsanalytics` 插件的设计与当前推荐路线一致：它基于
`nvinfer` 和 `nvtracker` 已附加的 metadata 做 ROI filtering、overcrowding、direction
detection 和 line crossing，并用 bbox bottom-center 坐标做分析。也就是说，业界
低成本实现并不是再跑一个动作模型，而是在检测/跟踪 metadata 上做规则计算。

DeepStream tracker 文档也给出明确取舍：

- IOU tracker 是最低计算成本的 baseline；
- NvSORT 在 bbox association 上加 Kalman filter，仍保持高性能；
- NvDCF 更鲁棒，但视觉跟踪会增加计算复杂度；
- NvDeepSORT/MaskTracker 等更强，但引入 ReID/分割等额外推理成本。

当前 midterm 已经有 nvtracker、TrackState、foot_point、bbox、keypoints、camera rules
和 cooldown，因此最小集成面就是继续用纯 Python rule。

## 总体实现原则

### 推荐

- 单目标算法实现为 `BehaviorRule.evaluate(track)`。
- 多目标算法实现为 `FrameBehaviorRule.evaluate_frame(frame_tracks, frame_ts_ms)`。
- 所有算法只消费当前已有 `TrackState`、bbox foot point、keypoints、zone/line config。
- 所有阈值先使用像素空间和持续时间，不直接宣称真实世界速度。
- 每个规则必须带 hysteresis / cooldown，避免一帧抖动产生大量事件。
- 先做到 `event_only`，证据链路单独升级到 `production_ready`。

### 暂不推荐

- 不先接 ST-GCN、3D CNN、Transformer action recognition。
- 不先做 optical flow。
- 不引入 sklearn/torch 运行依赖到 Savant pyfunc。
- 不在性能测试期间启用新规则到压测摄像头。

原因：这些方案会增加模型、依赖、GPU/CPU 成本和部署复杂度。按当前优先级，只有在轻量规则误报/漏报不可接受时，才进入准确度升级路线。

## 算法结论总表

| 算法 | 推荐实现 | 集成复杂度 | 性能成本 | 准确度预期 | 当前建议 |
| --- | --- | --- | --- | --- | --- |
| `behavior.intrusion` | foot point in polygon + dwell time | 已完成 | 低 | 中高，依赖 person track | 保持生产基线 |
| `behavior.loitering` | ROI 内停留时间 + 低速/低位移 | 已有基础 | 低 | 中 | 稳定现有实现 |
| `behavior.running` | track speed + 持续时间 + bbox-height 归一化可选 | 已有基础 | 低 | 中 | 稳定现有实现 |
| `behavior.wall_climb_suspicious` | line crossing + line-near dwell + pose/vertical motion cues | 中 | 低到中 | 中 | 第二批补 |
| `behavior.crowd_gathering` | ROI 内人数 + 近邻聚类/DBSCAN-like + 持续时间 | 已有基础 | 中 | 中 | 稳定现有实现 |
| `behavior.fall` | 姿态 keypoints + bbox aspect + upright-to-lying transition | 已有基础 | 低到中 | 中 | 稳定现有实现 |
| `behavior.chasing` | 多轨迹速度/距离/方向/前后关系 + 持续时间 | 已有基础 | 中到高 | 中低到中 | 稳定并加限流 |
| `face.observation` | 维持 pipeline/env 控制，不做行为规则 | 已有 | 已在模型链内 | 中高 | 不并入事件检测 |
| `face.watchlist` | AdaFace embedding + pgvector cosine search + per-camera targets | 已完成 | 中，主要在 face-worker/DB | 中高 | 保持生产基线 |
| `face.live_search` | DB 任务/查询流 + pgvector search + TTL，不是 Savant 行为规则 | 中 | DB 侧可控 | 中高 | 单独做，不和行为规则混做 |

## 推荐实现细节

### 1. `behavior.loitering`

推荐实现：单目标 `BehaviorRule`。

逻辑：

```text
track foot_point 在 ROI 内
AND inside_duration_ms >= min_loiter_ms
AND avg_speed_px_s <= max_avg_speed_px_s
AND displacement_px <= max_displacement_px 可选
AND cooldown 允许
```

建议默认参数：

```text
min_loiter_ms: 30000 或 60000
max_avg_speed_px_s: 20 到 40
max_displacement_px: 可选，按 ROI 尺寸比例设置
cooldown_s: 60
```

为什么合适：

- 直接复用现有 `TrackState.observations` 和 `avg_speed_px_s`；
- 与 `intrusion` 共享 polygon zone 和 foot_point；
- 无新增模型、无新增依赖、无 DB schema 需求。

准确度风险：

- 远近景透视会影响像素速度；
- track ID 中断会重置停留时间；
- 排队、驻足工作场景可能误报。

缓解：

- 按 camera/zone 配阈值；
- 增加 `min_observation_count`；
- 后续支持 ROI 内累计停留而不是只看连续 track；
- 有条件时增加 homography/world-coordinate 校准，但不是第一版必需。

结论：这是最适合先补的 unsupported 规则。

### 2. `behavior.running`

推荐实现：单目标 `BehaviorRule`。

逻辑：

```text
track 最近 velocity_window_ms 内 foot_point speed_px_s >= min_speed_px_s
AND 持续 min_duration_ms
AND 可选 normalized_speed = speed_px_s / bbox_height >= min_norm_speed
AND ROI 内或全画面启用
AND cooldown 允许
```

建议默认参数：

```text
velocity_window_ms: 700 到 1000
min_speed_px_s: 250 起步，需按 camera 校准
min_norm_speed: 可选，3.0 到 5.0 bbox_height/s
min_duration_ms: 500 到 1000
cooldown_s: 20
```

为什么合适：

- 已有 `TrackState.avg_speed_px_s` 和 chasing 的 `track_kinematics` 可复用；
- 单目标 O(n)，几乎不增加 GPU 成本；
- 比动作模型容易集成和解释。

准确度风险：

- 摄像机视角导致近处像素速度更大；
- 检测框抖动会造成瞬时速度尖峰；
- 快走和跑步的边界模糊。

缓解：

- 使用窗口内中位速度或平滑速度，而不是单帧差；
- 加 `min_duration_ms`；
- 增加 bbox-height 归一化；
- 后续如果必须更准，可从 keypoint 周期/步态加辅助特征，但不先上动作模型。

结论：这是第二个最适合先补的 unsupported 规则。

### 3. `behavior.wall_climb_suspicious`

推荐实现：单目标规则，但需要 line zone 支持。

逻辑：

```text
track foot_point 或 hip/torso center 靠近 configured line
AND track 在短时间内跨越 line 或沿 line 法向发生明显位移
AND bbox/keypoints 显示上半身/手臂/髋部接近墙线区域，可选
AND crossing duration 在 min_crossing_ms/max_crossing_ms 内
AND cooldown 允许
```

第一版可以只做：

```text
line-crossing + direction + min/max crossing duration
```

第二版再加 pose cues：

```text
hand/shoulder/hip 与 line 的距离
vertical displacement
bbox aspect/height change
```

集成注意：

- API/export 已经允许 `line` / `direction_line`，但当前 `camera_entry_to_legacy_config`
  会把 zones 转成只有 polygon 的 legacy `ZoneConfig`。补这个算法前，应扩展 runtime
  zone model，保留 `type` 和 line points，或者让 wall-climb 直接从 `RuleConfig.config.line_id`
  和原始 `CameraEntry.zones` 获取 line。
- 不要把 line zone 硬塞成 polygon zone。

性能：

- 单目标 O(n)，低成本；
- 如果加 keypoint cue，也只消费现有 pose keypoints，不新增模型。

准确度风险：

- 单纯 line crossing 更像越线，不一定是翻墙；
- 墙体区域需要 camera-specific 配置；
- 遮挡和低角度画面会误判。

结论：可做，但先补 line-zone runtime 表达，再实现轻量 line crossing。不要第一版就追求“真实翻墙动作识别”。

### 4. `behavior.crowd_gathering`

当前已有实现方向：多目标 `FrameBehaviorRule`，ROI 内 foot_point 聚类，持续超过阈值后出事件。

推荐保留这个方向。

最佳第一版逻辑：

```text
ROI 内 person count >= min_person_count
AND pairwise/proximity cluster count >= min_person_count
AND duration >= min_duration_ms
AND exit_person_count hysteresis
AND per camera/zone cooldown
```

聚类实现建议：

- 当前人数不高时，现有 pairwise proximity/BFS 足够；
- 如果要压 30/60 路，给每帧候选人数加 cap，并用 grid spatial index 降低 O(n^2)；
- 不建议在 pyfunc 内引入 sklearn DBSCAN 依赖；
- DBSCAN 原理适合聚集检测，但本项目实现应保持手写轻量版本。

准确度风险：

- 拥挤但分散站位可能漏报；
- 透视导致远处人间距像素更小；
- 多摄像头不同焦距需要不同 eps。

缓解：

- `eps_px` 按 zone 配置；
- 支持 `eps_ratio_of_frame` 或 `eps_ratio_of_roi`；
- 记录 `member_track_ids`、`centroid`、`person_count` 便于回放调参。

结论：不用换方案。下一步是补真实/半真实样本、上限保护、runtime metrics，而不是换成深度模型。

### 5. `behavior.fall`

当前已有实现方向：单目标 `BehaviorRule`，用 bbox aspect、torso angle、head/hip collapse、upright-to-lying transition。

推荐保留这个方向。

最佳第一版逻辑：

```text
ROI 内 track
AND 先 upright 后 lying
AND lying 持续 min_down_ms
AND keypoint/bbox 票决通过
AND visible keypoints >= min_visible_keypoints 可选
AND cooldown 允许
```

为什么不先上 ST-GCN：

- ST-GCN 这类 skeleton action recognition 准确度潜力更高，但需要序列模型、训练/导出/推理、
  模型版本管理和额外性能预算；
- 当前已有 YOLO26 keypoints，第一阶段用规则更容易集成、解释和压测隔离。

准确度风险：

- 躺卧、俯卧撑、弯腰、坐下可能误报；
- keypoints 缺失时依赖 bbox 会更不可靠；
- 快速摔倒但被遮挡可能漏报。

缓解：

- 默认 `require_transition=true`；
- `min_down_ms` 不低于 1200 到 1500；
- 增加 negative fixture：弯腰、坐下、俯卧撑、躺卧开始；
- 事件 payload 输出 posture votes，便于人工复盘；
- 后续如准确度不够，再评估小型 keypoint sequence classifier。

结论：保留现有轻量规则，先补样本和误报控制。

### 6. `behavior.chasing`

当前已有实现方向：多目标 `FrameBehaviorRule`，基于速度、距离、方向一致性、前后关系和持续时间。

推荐保留，但必须加性能保护。

最佳第一版逻辑：

```text
筛选高速 tracks
AND ROI 内
AND pair distance <= max_distance_px
AND velocity alignment >= min_cos_alignment
AND follower 位于 leader 后方
AND pair 持续 min_pair_duration_ms
AND cooldown 允许
```

性能风险：

- 最坏 O(n^2)，拥挤场景有压力；
- 应先用速度阈值、ROI、max_tracks、spatial grid 缩小候选。

准确度风险：

- 同向跑步、排队快走、体育场景容易误报；
- 摄像机视角影响前后关系；
- track ID switch 会打断持续时间。

缓解：

- `min_pair_duration_ms` 默认不低于 1500；
- 加 `min_speed_px_s` 和 `speed_ratio_tolerance`；
- 限制只在特定 ROI/时段启用；
- payload 输出 leader/follower、distance、alignment、duration，方便调参。

结论：不换方案，但先加候选裁剪和样本回归，再考虑生产。

### 7. `behavior.intrusion`

当前实现已经是合适路线：foot_point in polygon + continuous inside duration + cooldown。

建议：

- 不重写；
- 后续只补回归样本和性能指标；
- 作为其他单目标规则的模板。

### 8. `face.observation`

这不是行为事件检测规则。当前由 pipeline/env 控制更合理：

```text
YOLOv8-Face -> face/person association -> AdaFace -> ReID gate -> face_observation_exporter
```

建议：

- 不在本轮事件检测补全里改成 per-camera rule gate；
- 如果后续要做 per-camera gate，应只控制 export/match，不关闭底层模型链，否则会影响 watchlist/live_search 和性能可比性。

### 9. `face.watchlist`

当前生产路线合适：

```text
AdaFace embedding
-> person_gallery_embeddings / pgvector cosine similarity
-> per-camera target_person_ids / target_external_person_ids / target_names
-> watchlist_hit
```

建议：

- 保持现有 per-camera rule；
- 小规模 gallery 继续 exact search，保证可解释和 recall；
- gallery 变大后再引入 HNSW cosine index；
- 每次命中记录 query observation、target person、threshold、similarity、rule_id。

性能说明：

- pgvector 默认 exact nearest-neighbor search recall 最稳定；
- HNSW 查询更快但有内存和召回权衡；
- 这部分成本主要在 DB/face-worker，不应与 Savant 行为 pyfunc 混为一个性能问题。

### 10. `face.live_search`

这不是帧内事件规则，更像一次临时检索任务。

推荐实现：

```text
operator 创建 live_search job
job 包含 target embedding / person / camera_scope / ttl / threshold
face-worker 对新 face_observations 做 pgvector search
命中后发 live_search_hit
job 过期自动停止
```

建议：

- 单独作为 face-worker/API/8090 任务流实现；
- 不放进 `custom.rules`；
- 不在补 loitering/running/wall_climb 时一起做；
- 小规模 exact search，规模扩大再 HNSW。

## 编码优先级

### P0: 低风险快速补全

1. `behavior.loitering` fixture / live-smoke 回归
2. `behavior.running` fixture / live-smoke 回归

理由：两个规则已落到单目标 rules 路线，后续重点是样本、阈值和证据链路，不是新增模型。

### P1: 需要 runtime zone 结构补齐

3. `behavior.wall_climb_suspicious`

理由：算法本身不重，但需要 line zone 在 Savant rule runtime 中保真，不能复用 polygon-only legacy shape。

### P2: 稳定已有 event-only

4. `behavior.crowd_gathering`
5. `behavior.fall`
6. `behavior.chasing`

理由：已有实现和测试，下一步是样本、限流、payload、metrics 和证据链路，不是推倒重写。

### P3: 证据链路

7. 将非 intrusion 行为事件升级到 evidence `production_ready`。

理由：这会碰 event-worker、clip-worker、media-worker、evidence viewer 和 IO，必须和性能测试隔离。

### P4: 人脸任务流

8. `face.live_search`
9. `face.observation` per-camera gate 是否需要

理由：这是 face-worker/API/UI 任务，不是 behavior rule。

## 需要避免的实现路线

| 方案 | 为什么不作为第一版 |
| --- | --- |
| ST-GCN / 3D CNN / Transformer action recognition | 准确度潜力高，但新增模型、依赖、推理成本、训练/样本要求，不符合当前性能优先 |
| optical flow | 额外计算重，和现有 bbox/track/keypoint 能力重复 |
| sklearn DBSCAN runtime dependency | 依赖重，不适合 Savant pyfunc；可借鉴思想手写轻量 proximity clustering |
| DeepSORT/ReID for person behavior | 已有 nvtracker；为行为规则再加 ReID 模型成本过高 |
| 用 face detector 触发 intrusion/loitering | 语义会混乱；行为规则应以 person/pose track 为主 |
| 在 live 性能压测栈上边测边启用新规则 | 会污染性能结论 |

## 验收建议

每个行为算法至少需要：

- unit tests：正例、负例、阈值边界、cooldown、camera isolation；
- config tests：API/export/runtime loader 一致；
- payload contract tests：`algorithm_id`、`rule_id`、`camera_id`、`source_id`、`zone_id`/`line_id`；
- performance guard：每帧候选上限或复杂度说明；
- support matrix 更新：只有真实 runtime consume 后才从 `unsupported` 改为 `event_only`；
- live smoke：性能测试结束后再做，且记录 runtime epoch。

## 调研来源

- NVIDIA DeepStream `gst-nvdsanalytics`：ROI filtering、overcrowding、direction detection、line crossing 都基于 detector/tracker metadata 和 bbox bottom-center。
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvdsanalytics.html>
- NVIDIA DeepStream `gst-nvtracker`：IOU/NvSORT/NvDCF/NvDeepSORT 的性能与准确度取舍。
  <https://docs.nvidia.com/metropolis/deepstream/dev-guide/text/DS_plugin_gst-nvtracker.html>
- SORT：Kalman + Hungarian 的简单在线跟踪在论文中报告 260 Hz，证明 tracking-by-detection 规则链路适合低成本实时系统。
  <https://arxiv.org/abs/1602.00763>
- DBSCAN 原始论文：密度聚类适合任意形状空间聚类；本项目只借鉴思想，不引入 runtime 依赖。
  <https://cdn.aaai.org/KDD/1996/KDD96-037.pdf>
- ST-GCN：skeleton action recognition 的准确度升级方向，但不适合作为当前性能优先第一版。
  <https://arxiv.org/abs/1801.07455>
- BlazePose：实时人体关键点模型参考；本项目已有 YOLO26 keypoints，不建议第一版新增 pose 模型。
  <https://arxiv.org/abs/2006.10204>
- AdaFace：当前 face embedding 方向的依据。
  <https://arxiv.org/abs/2204.00964>
- pgvector：cosine distance、exact search、HNSW/IVFFlat 的速度/召回取舍。
  <https://github.com/pgvector/pgvector>
