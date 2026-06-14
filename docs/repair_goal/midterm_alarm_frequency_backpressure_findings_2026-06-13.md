# Midterm 报警频率差异与 Source Adapter 背压排查记录

日期：2026-06-13

## 结论

- 两路摄像头的 intrusion 报警频率不一致，主要不是规则配置导致。`primary_rtsp` 的电影片段当前画面以人脸和上半身近景为主，Savant face 输出很多，但 YOLO26 pose/person 输出很少；当前 intrusion 规则只消费 pose/person 观测，不消费 face 观测。
- `source_00000000-0000-4000-8000-781078565686` 的实验室画面虽然 face 输出少，但 pose/person 输出明显更多，因此 intrusion 事件更密。
- 当前确实观察到运行时卡顿/断流迹象。主要表现是两个 source adapter 都多次重启，并在日志里出现 ZeroMQ send timeout/backpressure；Savant、Replay、API、event-worker 容器本身未出现对应的 restart count 增长。
- watchdog 记录过 annotation stall，并尝试恢复，但当次 Docker restart 调用超时；这说明 watchdog 有发现异常，但恢复链路本身还不够可观测、也不够可靠。
- 前端重启按钮只在 evidence-viewer operator 页面存在；API 静态 operator 页面未同步该按钮。Savant perf 指标目前只通过 `/metrics` 和 smoke/探针查看，没有前端面板。

## 当前两路配置

### primary_rtsp

- camera_id: `00000000-0000-4000-8000-000000000100`
- source_id: `primary_rtsp`
- rtsp_url: `rtsp://192.168.1.105:8554/live/1080movie`
- zone: 1920x1080 full frame
- intrusion:
  - `min_inside_ms=1`
  - `cooldown_s=30`
  - min person width/height: 20/40
  - confidence: 0.25

### lab / dynamic source

- camera_id: `00000000-0000-4000-8000-781078565686`
- source_id: `source_00000000-0000-4000-8000-781078565686`
- rtsp_url: `rtsp://10.37.57.157:8554/camera`
- zone: 1280x720 full frame
- intrusion:
  - `min_inside_ms=1000`
  - `cooldown_s=30`
  - min person width/height: 20/40
  - confidence: 0.25

配置层面 `primary_rtsp` 并不更严格；`min_inside_ms=1` 甚至比 lab 更宽松。因此报警频率差异不应优先归因到规则冷却或 inside dwell 时间。

## 报警频率采样

DB 事件统计使用 UTC 时间；排查发生在 2026-06-12 UTC / 2026-06-13 Asia/Shanghai。

### 最近 3 小时 intrusion 事件

| source_id | count | first event UTC | last event UTC | 约每小时 |
| --- | ---: | --- | --- | ---: |
| `primary_rtsp` | 159 | 2026-06-12 08:32:38 | 2026-06-12 11:23:48 | 56 |
| `source_00000000-0000-4000-8000-781078565686` | 240 | 2026-06-12 08:30:33 | 2026-06-12 11:23:46 | 83 |

### intrusion 间隔统计

| source_id | avg gap | median gap | p95 gap | max gap | gaps >= 60s | gaps >= 120s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `primary_rtsp` | 64.06s | 26.51s | 274.76s | 774.58s | 48 | 22 |
| `source_00000000-0000-4000-8000-781078565686` | 43.31s | 30.00s | 65.76s | 1319.19s | 15 | 8 |

采样时两路最新 intrusion 都在数秒内出现过，因此不是完全无报警；但两路都存在短时间无报警窗口，`primary_rtsp` 的长 gap 更频繁。

## Savant 指标采样

采样显示两路 frame 流都在推进，但 pose/person 输出差异很大。

| source_id | frames_seen | annotations | pose_objects | pose_frames_with_person | face_objects | face_frames_with_face | effective_fps | last_frame_age |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `primary_rtsp` | 4369 | 4369 | 163 | 125 | 571 | 387 | 6.2 | 0.0s |
| `source_00000000-0000-4000-8000-781078565686` | 4233 | 4233 | 5697 | 4109 | 19 | 19 | 4.8 | 5.499s |

解释：

- `primary_rtsp` 的 face 输出高，说明画面里有人脸，且 face detector 正常工作。
- `primary_rtsp` 的 pose/person 输出低，说明当前电影片段很多画面不满足 YOLO26 pose/person 的人体检测条件，尤其是近景人脸、半身、遮挡或镜头切换场景。
- intrusion 规则位于 `yolo26_pose -> tracker -> behavior_rules -> yolov8_face` 的 behavior 阶段，只基于 pose/person 轨迹和脚点进区判断；face 输出在 behavior 之后，不会补充触发 intrusion。

## 原始画面观察

- `primary_rtsp` 抽帧为 1920x1080 电影画面，观察到两个人脸/上半身近景，脸清晰，但完整人体和下肢不可见。
- lab 抽帧为 1280x720 办公/实验室画面，画面内多人坐姿和躯干轮廓更稳定，pose/person 更容易持续输出。

这与 metrics 中 `primary_rtsp` face 多、pose 少，以及 lab pose 多、face 少的结果一致。

## Watchdog 与重启记录

容器状态采样：

- `video-analytics-midterm-savant`: restart count `0`，healthy
- `video-analytics-midterm-api`: restart count `0`
- Replay/event-worker 等核心服务：restart count `0`
- `primary_rtsp` source adapter: restart count `57`
- dynamic lab source adapter: restart count `78`

API watchdog 日志出现过：

```text
savant supervisor recovery reason=annotation_stall(age=128s)
savant supervisor loop failed
TimeoutError: timed out
```

解释：

- watchdog 检测到了 annotation stall，并尝试恢复。
- 失败点在 Docker restart HTTP 调用超时，不是 Savant 主容器实际重启成功后的业务失败。
- 当前 watchdog 的恢复尝试需要更强的结果记录，例如恢复动作开始/成功/失败、目标容器、耗时、异常类型、下一次重试时间。

## Source Adapter 背压证据

两路 source adapter 日志均出现过：

```text
Failed to send message to ZeroMQ socket. Error is [11] Resource temporarily unavailable
Failed to send message to ZeroMQ: WriterResultSendTimeout
```

随后出现 GStreamer internal data stream error，source adapter 释放 pipeline 并重新启动。

判断：

- 卡顿/断流的主要证据在 source adapter 到 Replay/Savant ingestion 的 ZeroMQ 背压。
- 这会造成 source adapter 重启、帧流间歇中断，并放大报警 gap。
- 这不是“报警规则没有触发”单一问题，也不是 Savant 主进程频繁重启；实际不稳定点更靠近 source adapter 输出和下游 ingestion。

## 2026-06-14 补充：跳帧位置与 Replay 背压根因确认

本次补充只做只读排查，未改运行容器、未重启服务。8090 runtime overview 显示
`compose_source.restart_count=823` 后，进一步确认该值来自 Docker
`RestartCount`，不是前端误算，也不是 API/evidence-viewer 受控重启造成。

### 重启计数与直接错误

采样时固定源状态：

```text
container: video-analytics-midterm-source-adapter
RestartCount: 823
StartedAt: 2026-06-14T01:44:07Z
FinishedAt: 2026-06-14T01:44:06Z
ExitCode: 0
OOMKilled: false
```

动态源也存在同类问题：

```text
container: video-analytics-source-source_00000000-0000-4000-8000-781078565686
RestartCount: 1105
StartedAt: 2026-06-14T01:47:25Z
ExitCode: 0
OOMKilled: false
```

两路 source adapter 的退出前日志均为 ZeroMQ 发送背压：

```text
Failed to send message to ZeroMQ socket. Error is [11] Resource temporarily unavailable
Failed to send message to ZeroMQ: WriterResultSendTimeout
```

固定源 source adapter 日志显示它仍按原始 RTSP 帧率推送：

```text
primary_rtsp: 1920x1080, framerate=24000/1001, Processed ... 23.98 FPS
dynamic lab: 1280x720, framerate=30/1, Processed ... about 30 FPS
```

Replay 侧同时观察到 `send timeout` 和大量 `mismatched routing_id` warning。
Savant 侧仍能维持约 `8.00 FPS` 的推理入口节奏，但 source queue 和 decode queue
存在堆积。这说明问题不是 Savant 模型完全停止，而是 Replay/source-adapter 共享
链路承受了高于分析帧率的完整流压力。

### 关键语义：当前跳帧发生在 Savant 内部

当前 `MAX_FPS_CONTROL=true`、`MAX_FPS=8/1` 并不是 source-adapter 或 Replay
入口限流。它配置在 `modules/savant_security/module.yml` 的
`pipeline.source.ingress_frame_filter`：

```yaml
pipeline:
  source:
    element: zeromq_source_bin
    ingress_frame_filter:
      module: custom.filters.pts_fps_gate
      class_name: PtsFpsGate
```

`modules/savant_security/custom/filters/pts_fps_gate.py` 的语义是：

```text
Replay remains the media authority and stores every RTSP frame before Savant.
This filter only throttles frames admitted into the Savant inference graph.
```

而固定源 source-adapter 实际执行 `/opt/savant/adapters/gst/sources/rtsp.sh`，
该脚本未读取 `MAX_FPS`，pipeline 为：

```text
ffmpeg_src -> savant_parse_bin -> fps_meter -> zeromq_sink
```

因此当前链路不是“Replay 只给 Savant 发送抽帧后的分析流”，而是：

```text
RTSP
  -> source-adapter 原始帧率推送
  -> Replay in_stream 完整接收/存储
  -> Replay out_stream 继续转发给 Savant
  -> Savant PtsFpsGate 再丢弃未被采纳的分析帧
```

这解释了为什么“已经有跳帧检测”仍会发生背压：跳帧降低了模型推理负载，但没有
降低 source-adapter 到 Replay、Replay 写入 RocksDB、Replay out_stream 到 Savant
这几段的传输和缓存压力。

### Evidence clip 不能依赖入库前丢帧

不能把“Replay 入库前丢帧”作为最终修复。若在 Replay 存储前或 evidence
authority 路径上丢帧，生成的 `raw_clip` 会天然缺帧，和生产证据语义冲突。

目标架构应拆清两条流：

```text
完整证据路径:
  RTSP -> Replay/环形存储完整保留 -> evidence clip 生成

分析推理路径:
  Replay/存储后的分析分支 -> 可配置抽帧 -> Savant 推理
```

因此修复方向应是让 Replay 到 Savant 的分析分支可控抽帧，或引入独立的分析分支
抽帧器；Savant 内部 `PtsFpsGate` 可保留为二级保护，但不应是第一道吞吐保护。

### 修复边界

本问题是生产阻塞项。修复时应满足：

- evidence clip 仍从完整流或完整环形存储生成，不能从抽帧分析流生成。
- source-adapter 不应因下游分析分支慢而反复退出重启。
- Replay 到 Savant 的分析流需要有 per-source 可配置 FPS，例如沿用
  `MAX_FPS=8/1` 的语义，但位置前移到 Savant 之前。
- 8090 runtime overview 应继续展示累计 restart count，并补充短窗口重启率、
  最近重启时间和 ZeroMQ timeout 摘要，避免容器 `running` 掩盖重启风暴。
- 修复验收应包含至少 10-15 分钟双源运行窗口：source-adapter restart count 不再
  增长，Replay 无持续 send timeout，Savant per-source FPS 维持目标分析帧率，
  新 evidence clip 通过连续性/时长检查。

## 前端能力现状

### 重启入口

evidence-viewer operator 页面有重启按钮：

- `services/evidence-viewer/app/static/index.html`
  - button id: `restart-runtime`
  - text: `受控重启运行时`
- `services/evidence-viewer/app/static/operator.js`
  - `restartRuntime()`
  - POST `/api/v1/cameras/runtime/restart`

API 静态 operator 页面未同步该按钮。如果页面上看不到重启入口，优先检查：

- 当前打开的是 evidence-viewer 页面还是 API 静态 operator 页面；
- 浏览器是否缓存了旧静态资源；
- 部署产物是否只更新了其中一个静态页面目录。

### 性能指标展示

`va_savant_*` 指标当前没有前端展示面板。查看方式是：

```bash
curl --noproxy '*' -fsS http://127.0.0.1:18080/metrics
bash scripts/smoke/current/check_savant_perf_observability.sh
```

其中 smoke 会检查 `frames_seen`、`frame_annotations_exported`、pose/face stage 计数、effective FPS、last frame age 等 per-source 指标。

## 后续硬化建议

1. 给 source adapter 背压增加显式状态面板和 API 字段：restart count、last restart time、last ZeroMQ timeout、last frame age、effective FPS。
2. 把 watchdog 恢复动作落库或暴露到 runtime status：记录恢复原因、动作目标、开始时间、完成时间、成功/失败和异常。
3. 在 operator 前端增加 Savant perf 区块，至少展示 per-source `effective_fps`、`last_frame_age_seconds`、`pose_objects_delta`、`face_objects_delta` 和 source adapter restart count。
4. 优先定位 ZeroMQ 背压来源：Replay/Savant ingestion 是否消费慢、队列是否过小、source adapter 是否需要降帧/限队列/更稳健重连策略。
5. 明确产品语义：如果 intrusion 必须表示“有人出现”，继续使用 pose/person 是合理的；如果电影近景人脸也应触发，则需要新增 face-as-presence 规则或 person detector fallback，而不是期望当前 face detector 自动触发 intrusion。
