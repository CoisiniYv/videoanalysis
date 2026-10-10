# 人脸按轨迹复识别：减少 AdaFace 与人脸检索负载（2026-10-10）

相关 spec：`specs/16_dual_path_30x2_t4_production_optimization.md` §5.2（GPU 预算）、
`specs/27_midterm_face_worker_vector_matching_optimization_plan.md`（face-worker 检索负载）。
实测依据：`docs/code_review/t4_savant_stage_analysis_production_2026-07-10.md`。

## 现象

- T4 分阶段压测：pose + 人脸检测稳定 4.04–4.09 FPS，加入 AdaFace 后降到 2.75 FPS。
  AdaFace 是让 60 路 / 4 FPS 掉帧的那一级，其工作量随画面中人脸数增长；电影片段多人同框时最明显。
- 两个 8090 预设下，人脸检测约每路每秒一次（`production_t4_40`：interval 3 @ 4 FPS；
  `local_4090_60`：interval 7 @ 8 FPS）。
- 原来的每轨迹节流是 1 秒（`FACE_REID_MIN_INTERVAL_MS=1000`）。检测本身就约 1 秒一次，节流几乎不起作用：
  画面中每个人约每秒一次 AdaFace、一次 face-worker 向量检索、一行人脸观测。
- 单分支内联路径（`savant-security`，`ADAFACE_INPUT_OBJECT=yolov8_face.face`）中 AdaFace 对每个检测到的人脸都计算，
  1 秒节流在 AdaFace 之后，只限制导出。
- 节流表（`ReIDThrottleMap`、`ExportThrottleMap`）没有清理：每出现一个轨迹 ID 就永久多一条，长跑内存缓慢增长。

## 策略

同一人员轨迹（`camera:source:person_track_id`）：

| 情况 | 结果 | 计数名 |
| --- | --- | --- |
| 第一次出现合格人脸（通过原最低门槛：有轨迹、置信度、尺寸、关键点） | 立即识别 | `identity_first_sighting` |
| 距上次识别不足 `FACE_REID_MIN_INTERVAL_MS` | 跳过 | `identity_min_interval` |
| 上次是清晰脸，未满 `FACE_IDENTITY_REFRESH_MS` | 跳过 | `identity_refresh_wait` |
| 上次是清晰脸，已满刷新间隔，当前脸清晰 | 识别 | `identity_refresh` |
| 上次是清晰脸，已满刷新间隔，当前脸不清晰 | 跳过，等清晰脸 | `identity_refresh_not_clear` |
| 上次不清晰，当前脸清晰（已满最小间隔） | 立即补识别 | `identity_clear_upgrade` |
| 上次不清晰，当前也不清晰，未满刷新间隔 | 跳过 | `identity_await_clear` |
| 上次不清晰，已满刷新间隔 | 识别（低分辨率机位不会从此失声） | `identity_unclear_refresh` |
| 时间戳倒退（重连、回放） | 当作新轨迹识别 | `identity_time_reset` |

“清晰脸”只用检测器输出判断，在裁剪和 AdaFace 之前完成：

- 人脸框短边 ≥ `FACE_CLEAR_MIN_SIZE_PX`（64）；
- 检测置信度 ≥ `FACE_CLEAR_MIN_CONFIDENCE`（0.60）；
- 偏头程度 ≤ `FACE_CLEAR_MAX_YAW_RATIO`（0.35）：鼻尖相对双眼中点沿眼线方向的偏移 / 两眼距离。
  正脸约为 0，转头越多越大；沿眼线测量，平面内歪头（对齐会纠正）不计入。两眼距离小于人脸短边 0.2 倍视为侧脸。
- 关键点支持 10 值（x, y）、15 值（x, y, score）和逐点嵌套格式。

其他语义：

- ROI 队列满而丢弃的人脸按“不清晰的一次识别”记账：清晰脸 1 秒后可重试，不清晰的等刷新间隔。
- 状态按 `camera:source` 分组，按该路自己的时间清理；距上次识别超过 `FACE_IDENTITY_STATE_TTL_MS`
  （默认 60 秒，至少为刷新间隔的 2 倍）即删除。离开很久再回来的同一轨迹 ID 会当作首次出现。
- 实现：`modules/savant_security/custom/services/face_identity_refresh.py`（纯 Python）。

## 影响范围

| 路径 | 谁在用 | 本次效果 |
| --- | --- | --- |
| `face_roi_exporter` → `adaface-roi-worker` | 8090 双分支预设（`production_t4_40`、`local_4090_60`） | 在 AdaFace 之前生效：裁剪、AdaFace、观测、检索都减少 |
| `face_reid_candidate_gate`（AdaFace 前候选门） | 压测脚本 `adaface_pre_gate` / 中央 AdaFace 模块 | 在 AdaFace 之前生效 |
| `face_reid_gate`（AdaFace 后） | 单分支 `savant-security` 默认内联路径 | 只减少观测导出、face-worker 检索和写库；AdaFace 仍对检测到的人脸计算 |

`ExportThrottleMap` 改为复用有清理的 `ReIDThrottleMap`，行为不变（仍是 1 秒兜底）。

预期：

- 持续在画面中 20 秒的人：识别次数约 20 → 约 4–5；
- 出现不到 5 秒的人：1 次，最多再加 1 次清晰补识别；
- 首个合格人脸仍立即识别，名单命中时延不变；
- 场景切换时多人同时首次出现的瞬时峰值不变（见剩余风险）。

## 配置开关

`infra/env/midterm.env`（所有 Savant 容器通过 `env_file` 读取）：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `FACE_REID_MIN_INTERVAL_MS` | 1000 | 同一轨迹两次识别的最小间隔（既有） |
| `FACE_IDENTITY_REFRESH_MS` | 5000 | 已识别轨迹的复识别间隔；**0 = 关闭新策略，回到 1 秒节流** |
| `FACE_CLEAR_MIN_SIZE_PX` | 64 | 清晰脸最小短边 |
| `FACE_CLEAR_MIN_CONFIDENCE` | 0.60 | 清晰脸最小检测置信度 |
| `FACE_CLEAR_MAX_YAW_RATIO` | 0.35 | 清晰脸最大偏头比 |
| `FACE_IDENTITY_STATE_TTL_MS` | 60000 | 轨迹状态保留时长（未写入 env 文件，按需添加） |

`modules/savant_security/module.yml` 中 `face_roi_exporter`、`face_reid_gate` 的 kwargs 读取上述变量；
压测脚本生成的候选门同样传入。

8090 网页：

- 运行配置 → 性能配置：“人脸复识别间隔 ms”（单分支识别容器，`FACE_IDENTITY_REFRESH_MS`）；
- 运行拓扑 → 分支 A / B：“人脸复识别间隔 ms”（写入 `savant-a` / `savant-b` 的 `FACE_IDENTITY_REFRESH_MS`）；
  两个预设均为 5000。旧的已保存拓扑配置读取时补默认值 5000，保存表单不会意外写成 0。
- 清晰脸阈值暂只在 env 中调整。

## 对其他功能的影响

- 人脸观测行数和轨迹点密度下降：持续在场的人约 5 秒一个点；8090 轨迹页“最近一次”最多晚约 5 秒。
- 名单命中：face-worker 单次观测过阈值即命中，没有连续命中要求，冷却未改，不受间隔影响。
- 名单证据默认为图片证据，不受影响；视频证据叠框中“同轨迹身份延续”的行会变少。
- 跟踪器在人群中换 ID 时，错误身份最多持续一个刷新周期；face-worker 仍对每次观测独立比对。

## 非目标

- 不改 YOLO 人脸检测频率和姿态推理；
- 不在单分支 `module.yml` 默认接入 AdaFace 前候选门：候选对象是克隆体，与 frame annotation、
  `source_observation_id` 证据绑定的关系需要真机验证后再做；
- 不做“每帧新识别数量上限”（场景切换峰值）；
- 不改 face-worker 匹配逻辑。

## 回滚

1. 设 `FACE_IDENTITY_REFRESH_MS=0`（env 或 8090 两处表单）并重建对应 Savant 容器：恢复每轨迹 1 秒节流。
2. 或 revert 本次提交。节流表清理与新策略无关，可单独保留。

## 部署验证清单

1. 拉取 `main`。Savant 模块和 API 代码都是挂载目录，无 Dockerfile / 依赖变化，**不需要 rebuild**。
2. 重建受影响容器：
   - 单分支：`docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml up -d --no-build --force-recreate --no-deps savant-security`
   - 双分支：在 8090 运行拓扑重新应用当前预设（或按原方式重建 `savant-a`、`savant-b`）；
   - 8090 表单字段：重建 `api` 和 `evidence-viewer`（`operator.js` 缓存参数已追加 `-face-refresh-20261010`）。
3. 确认环境：`docker exec video-analytics-midterm-savant-a env | grep -E 'FACE_IDENTITY|FACE_CLEAR|FACE_REID_MIN_INTERVAL'`。
4. 看日志计数：
   - ROI 路径：`docker logs video-analytics-midterm-savant-a 2>&1 | grep 'stage=face_roi_exporter' | tail -3`，
     对比 `enqueued` 与 `throttled`，查看各 `identity_*` 计数；`identity_tracks` 应在一个范围内波动而不是一直增长；
   - 单分支：`grep '\[face_reid_gate_identity\]'`；
   - 候选门：`grep '\[face_reid_candidate_gate\]'`。
5. 同一测试视频、同一 60 路配置前后对比：Savant 有效 FPS、forwarder 积压、`adaface-roi-worker` 每秒嵌入数、
   `security.face_rois` / `security.face_observations` 增长速率、face-worker 处理速率。
6. 名单回归：测试视频已配置 Reese / Finch 目标。对比前后 `watchlist_hit` 数量和每次出场的首次命中时延。
7. 调参参考：`identity_clear_upgrade` 很少而 `identity_unclear_refresh` 占多数，说明该机位清晰阈值过严，
   可把 `FACE_CLEAR_MIN_SIZE_PX` 降到 48、`FACE_CLEAR_MAX_YAW_RATIO` 放宽到 0.45；名单命中下降时先缩短
   `FACE_IDENTITY_REFRESH_MS`（如 3000）。

## 验证（本机，离线）

本机无 GPU、无运行中的栈：

- 新增 41 个测试：
  - `harness/tests/test_face_identity_refresh.py`：清晰度判断（正脸、小脸、低置信、转头、侧脸、缺关键点、
    15 值和嵌套格式、平面内歪头）、全部策略分支、按路时间域清理、配置读取、节流表有界；
  - `harness/tests/test_face_identity_refresh_pyfuncs.py`：用 Savant 桩驱动真实的 ROI 导出、候选门、AdaFace 后门，
    同一序列“小脸首识别 → 0.5 秒跳过 → 清晰补识别 → 刷新等待 → 5 秒后刷新”，以及队列满回退和 `0` 回滚；
  - `harness/tests/test_face_identity_refresh_config.py`：env 默认值、`module.yml` kwargs、压测脚本候选门、
    8090 单分支与双分支字段、旧配置默认值。
- 相关既有测试（人脸门、观测导出、ROI 合同、运行拓扑、性能配置、压测脚本、部署合同）：无新增失败。
- 全量 harness（`-m "not integration"`）：12 failed / 1690 passed / 21 skipped / 7 collection errors，
  失败集合与改动前完全相同（缺 `python-multipart`、无 postgres、`main` 上没有 `CLAUDE.md` 等环境项）。
- 未做：真机 Savant / GPU 运行、真实视频上的识别次数与名单命中对比（见部署验证清单）。

## 剩余风险

- 清晰度判断基于 YOLOv8-face 五点关键点，小脸上关键点噪声较大；阈值未在真实机位上标定。
- 场景切换或多人同时入画时，首次识别仍是同时发生，瞬时峰值不变。
- `person_observation_exporter` 的人体观测节流表同样没有清理，属于同类问题，本次未改。
- 压测中央 AdaFace 模块同时经过候选门和 AdaFace 后门，两处策略按相同输入独立判断，结果一致；
  若后门因嵌入异常拒绝，其状态更宽松，不会多拒。

## 实现状态

- 分支 `claude/face-identity-refresh-20261010`，基于 `main` `32b8242`；
- 实现提交：见后续补记；
- 验证时间：2026-10-10，离线验证；
- 未完成：真机部署验证（按上文清单）；单分支默认接入 AdaFace 前候选门；人体观测节流表清理。
