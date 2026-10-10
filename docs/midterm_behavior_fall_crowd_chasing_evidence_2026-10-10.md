# 摔倒 / 聚集 / 追逐：规则补强与视频证据闭环（2026-10-10）

目标 spec：`specs/13_pose_behavior_algorithm_fusion.md`（规则合同、质量门）、
`specs/23_midterm_8090_operator_algorithm_control_plane.md`（8090 算法控制面）。

## 现象

1. `behavior.fall`、`behavior.crowd_gathering`、`behavior.chasing` 在 support matrix 中是
   `event_only`：Savant 规则能出事件，但 event-worker 把非 intrusion 行为的证据任务
   直接建成 `not_implemented`，8090 上看不到这三类告警的视频证据。
2. 8090 证据页不认识 `chasing`：没有中文标签，分类落到“未分类/全部”。
3. 规则本身存在会在真实视频上出错的问题：
   - fall 只靠 bbox 宽高比、躯干角、头-髋高度三票，相机俯拍或人头朝镜头躺下时漏报，
     蹲下前倾时两票成立即误报；
   - crowd / chasing 使用 `TrackStateStore.active_tracks`，其中包含最长 10 秒前已离开画面
     的轨迹（store 超时才删除），离开的人仍按最后位置被计入聚集，离开的一对人仍按冻结的
     最后速度继续累计“追逐时长”；
   - crowd 冷却键包含临时 cluster id，一帧检测为空或聚类断开后 cluster id 重建，同一群人
     可以绕过 60 秒冷却反复告警；
   - chasing 的 pair 键有方向（leader, follower），两人贴近时前后顺序抖动会重置持续时长，
     并且两个方向各占一个冷却槽；采样分析下单帧漏检也会把时长清零；
   - 画面中没有人的帧，运行时把帧时间取为“最近一次见到人的时间”，时间停住，store 也
     不按超时清理旧轨迹。

## 影响范围

只影响启用了这三类规则的摄像头。`run_midterm_pressure60.py` 生成的压测摄像头不启用这三类
规则，60 路压测基线不受影响。intrusion、watchlist、loitering、running 行为不变
（loitering / running 仍是 `event_only`）。

## 根因判断

- 证据缺失：event-worker `_evidence_task_initial_status`、`MIDTERM_BEHAVIOR_EVIDENCE_EVENT_TYPES`，
  media-worker 锚点提取 `SUPPORTED_EVENT_TYPES` 和 sidecar `event_types` 都只写了
  `intrusion`；env/compose 的 `RECORDING_EVENT_TYPES` 也只有 `watchlist_hit,intrusion`。
  四处各自维护列表，没有共享常量。
- 规则误判：见“现象”第 3 条，均可用纯单元测试复现（见下文测试列表，修改前全部失败）。

## 改动

### 证据链路（四类视频行为共用一个常量）

`libs/evidence_lifecycle/contract.py` 新增：

```python
BEHAVIOR_VIDEO_EVIDENCE_EVENT_TYPES = frozenset({"intrusion", "fall", "crowd_gathering", "chasing"})
```

| 位置 | 改动 |
| --- | --- |
| event-worker `worker.py` | `MIDTERM_BEHAVIOR_EVIDENCE_EVENT_TYPES` 取共享常量；无策略的旧事件同样补默认证据策略，显式 false/false 仍视为关闭 |
| event-worker `repository.py` | `_evidence_task_initial_status` 对共享常量内类型返回 `materialization_pending`（或延迟低优先级时 `manifest_ready`），不再是 `not_implemented`；coverage merge 默认类型加入三类 |
| media-worker `frame_annotation_event_window.py` | `SUPPORTED_EVENT_TYPES` = 身份事件 ∪ 共享常量，三类事件可提取 frame_uuid/frame_pts 锚点 |
| media-worker `worker.py` | 新增 `FRAME_CACHE_SIDECAR_EVENT_TYPES`（共享常量 + `watchlist_hit`），替换原硬编码 `{"intrusion", "watchlist_hit"}` |
| media-worker `annotation_style.py` | `chasing` 纳入行为事件样式 |
| `infra/env/midterm.env`、`infra/docker-compose.midterm.yml` | `RECORDING_EVENT_TYPES`、`EVIDENCE_EVENT_COVERAGE_EVENT_TYPES`、`FRAME_CACHE_SIDECAR_EVENT_TYPES` 加入三类 |
| API `repositories/events.py`、`services/storage_maintenance.py`，evidence-viewer `evidence_index.py`、`static/evidence.js` | `chasing` 归入 behavior 分类；8090 标签“追逐告警” |
| support matrix | 三类改为新状态 `evidence_ready`（`evidence_enabled=true`，`production_ready=false`）；schema、`/algorithms/support-matrix` statuses、8090 标签“证据可用”同步 |

证据形态与 intrusion 相同：Replay / rolling cache 的 `raw_clip.mov` + frame-cache 叠框 sidecar。
crowd 事件 `track_id=0`（群体事件），锚点只依赖 `source_id`、`camera_id` 和 `frame_uuid`/`frame_pts`，
不需要单人轨迹。

`evidence_ready` 的含义：规则能出事件、证据链路能生成可回放证据，但阈值尚未在现场样本上
验证，按 spec 13 §9.2 不能宣称跨摄像头生产准确率。

### fall：加入关节顺序投票

吸收原 C++ `fall_estimate` 中稳定的三个条件，作为额外投票（关键点置信度阈值沿用 0.25）：

| 投票 | 权重 | 来源 |
| --- | --- | --- |
| bbox 宽高比 ≥ `lying_aspect_ratio` | 1 | 原有 |
| 躯干与竖直夹角 ≥ `torso_horizontal_deg` | 1 | 原有 |
| 头-髋高度比 < `head_hip_collapse_ratio` | 1 | 原有 |
| 关键点外接框宽高比 ≥ `keypoint_box_aspect_ratio`（0.90，至少 6 个有效点） | 1 | C++ 条件 3 |
| 最低的肩低于最高的膝 | 1 | C++ 条件 2 |
| 两肩都不高于脚踝中点 | 2 | C++ 条件 1 |

- 总权重 ≤ 3（只有原三票时）保持旧规则：过半或平票判躺；总权重 > 3 时必须严格过半，
  平票不再判躺，防止蹲下前倾这类单一可疑形态触发。
- “直立”除宽高比外，也接受“肩 < 髋 < 膝 < 踝 依次向下且间距足够”的关节顺序，
  俯拍时胖框站立仍算直立，`require_transition` 不会因此漏掉真实摔倒。
- 未采用 C++ 条件 4（大腿与水平夹角）和条件 5（肩-髋-膝、髋-膝-踝折叠角）：坐姿大腿本来
  就接近水平，蹲下/系鞋带就是身体折叠，这两条在反例上成立，会把坐、蹲判成摔倒。
- 新配置：`joint_order_votes`（默认 true，关掉即回到原三票）、`keypoint_box_aspect_ratio`、
  `keypoint_box_min_points`；对应环境变量 `FALL_JOINT_ORDER_VOTES`、
  `FALL_KEYPOINT_BOX_ASPECT_RATIO`、`FALL_KEYPOINT_BOX_MIN_POINTS`。
- payload 新增 `shoulders_below_ankles`、`shoulders_below_knees`、`keypoint_box_aspect_ratio`、
  `legs_stacked_upright`、`lying_weight`、`total_weight`，便于现场复核票型。

### crowd_gathering

- `max_track_age_ms`（默认 1000）：只统计最后一次观测距当前帧不超过该值的轨迹；
  允许 1 帧左右漏检，离开的人不再计数。0 = 旧行为。
- 冷却键改为 `camera:crowd_gathering:zone`，不再含临时 cluster id：同一区域冷却期内只告警一次。
- `eps_height_ratio`（默认 0 = 关闭）：> 0 时两人脚点距离 ≤ 倍数 × 两人框高均值即相连，
  近处大人、远处小人用同一个比例，`eps_px` 此时不用。
- payload 新增 `eps_height_ratio`、`max_track_age_ms`。

### chasing

- `max_track_age_ms`（默认 1000）：离开画面的轨迹不再参与配对。0 = 旧行为。
- pair 按无序轨迹对记录：前后顺序抖动不重置时长；冷却键改为无序对，一对人只占一个冷却槽。
- `pair_gap_tolerance_ms`（默认 500）：只在“评估过的帧里确实缺席”超过该时长才重置；
  帧间隔本身（采样稀疏）不算缺席。
- `min_normalized_speed`（默认 0 = 关闭）：可选“身高/秒”速度下限，与 running 规则一致。
- payload 新增 `member_track_ids`（无序对）。

### 运行时帧时间

- 人体 adapter 的 `AdapterResult` 新增 `timestamp_ms`（无人帧也有值）；
- `BehaviorRulesPyFunc` 把它作为 `frame_ts_ms` 传给 `evaluate_runtime_frame`；
- `TrackStateStore.update(observations, now_ms=...)` 用帧时间做超时清理，无人帧也会清掉旧轨迹。

有人帧的帧时间与原来相同（同一 `_get_timestamp_ms`），只有无人帧行为变化。

### 8090 控制面

算法卡片新增：聚集“按身高半径倍数(0=按像素)”、追逐“最低身高速度(0=关闭)”、摔倒“关节顺序判定”。
registry `default_config` 同步加入新参数。`operator.js`、`evidence.js` 缓存参数追加 `-behavior-20261010`（与布局重做合并后见“实现状态”）。

## 非目标

- 不改模型链、FPS、batch、Savant 性能配置；
- 不承诺三类规则的现场准确率；
- 8090 播放器暂不单独高亮聚集成员 / 追逐双方 / 摔倒者：证据详情接口的 SQL 不带事件 payload，
  需要改证据查询路径，留作后续（当前叠框显示画面中全部人员框，与 intrusion 相同）；
- loitering / running 仍是 `event_only`。

## 配置开关与回滚

| 回滚目标 | 方式 |
| --- | --- |
| 关闭三类证据但保留事件 | `RECORDING_EVENT_TYPES=watchlist_hit,intrusion`；或在规则 `evidence_policy` 设 `snapshot_required=false, clip_required=false`（显式关闭会被 event-worker 尊重） |
| 关闭某摄像头的规则 | 8090 算法卡片取消勾选 |
| fall 回到原三票 | 规则配置 `joint_order_votes=false` 或 `FALL_JOINT_ORDER_VOTES=false` |
| crowd / chasing 恢复旧计数 | `max_track_age_ms=0`（或 `CROWD_MAX_TRACK_AGE_MS=0` / `CHASE_MAX_TRACK_AGE_MS=0`） |
| 整体回滚 | revert 本次提交；uos157 现有部署未改动，仍可作为回退 |

## 数据 / 状态语义

- 新事件的 evidence task 初始状态与 intrusion 相同；已入库的旧 `not_implemented` 任务不会被回填。
- `source_event_id` 规则不变：crowd 为 `producer:camera:none:crowd_gathering:start_ts`。同一聚集持续超过
  冷却期再次触发时 `start_ts` 不变，event-worker 按幂等键去重，即一次聚集过程只入库一条事件。
- `EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE` 仍只限制 `intrusion:40`；三类新类型只受全局和每路上限约束。

## 验证

本机无 GPU、无运行中的 midterm 栈，验证为离线：

- 先写失败测试，再改代码：
  - `test_fall_rule.py`：头朝镜头躺倒、俯拍站立、坐 / 蹲前倾反例、低置信关键点；
  - `test_crowd_gathering_rule.py`：离开者不计数、`max_track_age_ms=0` 兼容、单帧漏检、
    cluster 重建不绕过冷却、冷却到期、身高比例近大远小；
  - `test_chasing_rule.py`：离开的一对不再累计、`max_track_age_ms=0` 兼容、前后翻转不重置、
    翻转不绕过冷却、短暂缺席不重置、身高速度下限；原有用例改为按帧构造“新鲜”轨迹；
  - `test_frame_behavior_rule_runtime.py`、`test_person_pose_adapter.py`：无人帧时间推进与超时清理；
  - 新增 `test_behavior_video_evidence_contract.py`（24 项）：共享常量、event-worker 初始状态与默认策略、
    显式关闭、media-worker 锚点 / sidecar / 样式、env 与 compose、分类映射、8090 标签、support matrix。
- 行为规则相关测试（fall / crowd / chasing / running / loitering / intrusion / runtime / adapter / payload / registry 等）：111 passed；证据合同 + support matrix：28 passed。
- 全量 harness（`-m "not integration"`，`--continue-on-collection-errors`）：
  12 failed / 1648 passed / 21 skipped / 7 collection errors。失败与收集错误与未改动的 `main`
  完全相同（main：12 failed / 1602 passed），均为既有环境项（缺 `python-multipart`、无 postgres、
  `main` 上没有 `CLAUDE.md`）；新增 46 个通过。
- `docker compose -f infra/docker-compose.midterm.yml config`：通过；`node --check` operator.js / evidence.js：通过；
  `git diff --check`：通过。
- `scripts/smoke/current/check_midterm_deployment.sh`：失败于 `test_midterm_deployment_contract.py` 的 4 项，
  原因是 `main` 上没有 `CLAUDE.md`，与本改动无关。

未做：真机 Savant 运行、真实视频上的三类告警与证据生成、8090 页面实看。

## 剩余风险

- 阈值未经现场标定。尤其 crowd `eps_px`、chasing `min_speed_px_s` / `max_distance_px` 仍是像素值，
  不同机位需要分别调；`eps_height_ratio`、`min_normalized_speed` 默认关闭，需要在样本上确认后再开。
- 电影片段多人同框时，三类规则会增加告警与证据量；新类型没有单独的类型上限，必要时在
  `EVIDENCE_ADMISSION_MAX_ACTIVE_BY_EVENT_TYPE` 中加上限。
- 同一摄像头两个不同区域的 crowd 规则若在同一毫秒开始聚集，`source_event_id` 会相同（不含 zone），
  后一条被去重。该问题在本次之前就存在。
- 部署后需要重启受影响服务才能生效：savant-security、event-worker、media-worker、api、evidence-viewer
  （按 CLAUDE.md 用 `up -d --no-build --force-recreate --no-deps <service>`；无 Dockerfile 变化，不需要 rebuild）。

## 实现状态

- 分支 `claude/behavior-algorithms-20261010`，基于 `main` `d5d52c3` 开发；
- 实现提交：`be349b4`（规则、证据链路、8090、测试与文档）；
- 合入 `main`：2026-10-10，rebase 到 8090 布局重做（`c353822`、`dda7914`）之后 fast-forward 合入。
  唯一冲突是 `index.html` 的脚本缓存参数，合并为 `operator.js ...-behavior-20261010`、
  `trajectory.js ...-ui-20261010`、`evidence.js ...-ui-20261010-behavior-20261010`；
  合并后全量 harness 仍为 12 failed / 1648 passed（与基线相同的既有环境项），8090 mock 浏览器交互检查全部通过；
- 验证时间：2026-10-10，离线验证，见“验证”一节；
- 未完成：现场样本回归与阈值标定；8090 证据播放器按事件高亮相关人员；真机端到端证据生成。
