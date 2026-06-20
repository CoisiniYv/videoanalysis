# Midterm Replay Routing ID 故障记录

更新时间：2026-06-11

## 当前状态补充（2026-06-15）

本文记录的是 2026-06-10/11 的 Replay routing identity 和
`video-file-sink` 网络别名故障。该修复仍是当前 midterm runtime apply/restart
顺序的基础。

后续 2026-06-15 已在 Replay 和 Savant 之间增加 `analysis-forwarder`：

```text
RTSP adapter -> replay-service -> analysis-forwarder -> savant-security
```

该变化不改变本文对 source-adapter -> Replay 连接身份、Replay job sink alias、
runtime epoch、以及 fail-closed evidence guard 的要求。阅读本文时，把文中的
旧单跳 `replay-service -> savant-security` 理解为当时拓扑；当前拓扑以
`docs/current_mainline_status.md` 和 `docs/project_knowledge_network.md` 为准。

## 结论

2026-06-10 晚上没有新的 evidence 输出，不是因为 Docker healthcheck
报 unhealthy，而是 Replay live ingress 卡在 ZeroMQ routing identity 不匹配：

```text
Received message with mismatched routing_id
```

`routing_id` 是 Replay 与 source-adapter 之间 ROUTER/DEALER 连接的
ZeroMQ 连接身份，不是业务字段 `source_id`。当 source-adapter 重连或部分服务
重启后，adapter 可能拿到新的连接 identity；如果 Replay 仍缓存旧 identity，
Replay 会拒收后续帧。此时 source-adapter 仍会打印处理帧日志，容器也仍是
running/healthy，但帧没有继续进入 Savant，因此不会有新的事件、Replay job 或
evidence bundle。

## 2026-06-11 追加故障：sink 网络别名丢失

2026-06-11 01:13 左右通过 8090 受控重启恢复 Savant 后，事件和
`record_request` 已恢复，但仍没有新的 evidence bundle。该次不是
`routing_id` 问题：

- `video-analytics-midterm-savant` 为 `healthy`。
- `security.frame_annotations` 和 `security.events` 均继续刷新。
- `clip-worker` 已创建 Replay job。
- `/api/v1/job` 有 job 在运行或堆积。
- `replay-sink-output/midterm/epochs/<runtime_epoch_id>/` 没有新文件。
- `video-file-sink` 只在 `0.0.0.0:6666` 监听，没有来自 Replay 的 established
  TCP 连接。
- 在 `replay-service` 容器内执行 `getent hosts video-file-sink` 解析失败。

根因是 8090 runtime apply/restart 手动重建 `video-file-sink` 容器时只用
容器名 `video-analytics-midterm-video-file-sink` 接入 Docker 网络，没有恢复
compose 服务名别名 `video-file-sink`。而 clip-worker 提交的 Replay job 仍然
使用：

```text
dealer+connect:tcp://video-file-sink:6666
```

因此 Replay job 会在无 peer 的 ZMQ socket 上尝试发送，sink 不会收到流，也不
会写 `video.mov` / `metadata.json`。修复要求运行时重建 sink 时在 Docker
create 的 `NetworkingConfig.EndpointsConfig.<network>.Aliases` 中保留
`video-file-sink`。

同次恢复还暴露了一个 media-worker guard 误判：官方 `video-file-sink`
输出的逐帧 `metadata.json` 不保留 Replay job labels，因此
`sink_metadata_runtime_epoch_id` 为空是正常现象。epoch guard 只能把该字段作为
可选佐证：如果字段存在且与当前 epoch 不一致，必须失败；如果字段缺失，但
event payload、record request、Replay labels、sink 路径和 current epoch 全部
一致，应允许 raw clip 发布。该逻辑修复后，新事件
`91175619-b9b8-45c8-b3ca-90915c91f8eb` 生成 `clip_status=ready`、
`epoch_guard=passed` 的 evidence。

## 2026-06-11 本次固化修改内容

本次修复不改变 8090 管理端常驻在线的原则，只修受控运行时链路：

- `services/api/app/services/runtime_apply.py`
  - 手动重建 `video-file-sink` 时，在 Docker create body 中写入
    `NetworkingConfig.EndpointsConfig.<network>.Aliases=["video-file-sink"]`。
  - `CAMERA_RUNTIME_VIDEO_SINK_NETWORK_ALIAS` 为空时回落到默认
    `video-file-sink`，避免空 env 再次丢别名。
- `services/media-worker/app/worker.py`
  - `sink_metadata_runtime_epoch_id` 不再是 strict epoch guard 的必填项。
  - 如果 sink metadata 中该字段存在且与当前 epoch 不一致，仍然 fail closed。
  - 如果该字段缺失，但 event payload、record request、Replay labels、
    sink path 和 current epoch 全部一致，则允许发布 `raw_clip.mov`。
- 测试覆盖：
  - `harness/tests/test_camera_runtime_apply_service.py` 断言 runtime apply 创建
    sink 时保留 `video-file-sink` 网络别名。
  - `harness/tests/test_midterm_replay_epoch_isolation.py` 保留旧 epoch mismatch
    fail-closed 用例，并新增官方 sink metadata 缺少 epoch 时允许通过的用例。

本次验证结果：

- 8090 `/health` 正常，管理端保持在线。
- `video-analytics-midterm-video-file-sink` 网络别名包含 `video-file-sink`。
- `replay-service` 容器内 `getent hosts video-file-sink` 可解析。
- Replay `/api/v1/job` 最终为空。
- 新事件 `91175619-b9b8-45c8-b3ca-90915c91f8eb`：
  - `payload.media.clip_status=ready`
  - `payload.media.epoch_guard_status=passed`
  - `payload.media.raw_clip_path=/media/evidence/91175619-b9b8-45c8-b3ca-90915c91f8eb/raw_clip.mov`
  - 8090 bundle API 返回 `raw_clip_url`。
  - 8090 raw clip Range 读取返回 HTTP `206`、`video/quicktime`。
- 相关测试通过：
  `pytest -q harness/tests/test_midterm_replay_epoch_isolation.py harness/tests/test_camera_runtime_apply_service.py harness/tests/test_midterm_deployment_contract.py`

## 当时症状

- 容器都在运行，没有 Docker `unhealthy`。
- source-adapter 日志仍在增长，能看到类似 `Processed 1000 frames`。
- `security.frame_annotations`、`security.events`、`security.record_requests`
  不再刷新。
- `/data/video-analytics/media/evidence` 没有新的 bundle。
- Replay 日志反复出现 `mismatched routing_id`，涉及 `primary_rtsp` 和动态
  `source_...`。

## 原因

旧的 8090 `应用运行时` 动作只重启 Savant 并重建动态 source-adapter，没有先
停掉所有源，也没有重启 Replay。这个顺序在 replay-first 拓扑下不够完整：

```text
RTSP adapter -> replay-service -> savant-security
```

2026-06-15 之后的当前拓扑在 Replay 和 Savant 之间多了
`analysis-forwarder`，但 Replay 仍是 source-adapter 连接身份的状态持有者，
因此受控重启仍必须覆盖 Replay 和所有 source-adapter。

Replay 是 source-adapter 连接身份的状态持有者。只重启 Savant 不能清掉 Replay
里的旧 routing identity；只重建部分动态源也不能处理 compose 固定源
`video-analytics-midterm-source-adapter`。因此会出现“程序还活着，但 Replay 拒
帧”的假健康状态。

## 修复后的受控重启顺序

8090 管理端和内部 API 不参与重启，保持在线：

- 保持运行：
  - `video-analytics-midterm-evidence-viewer`
  - `video-analytics-midterm-api`
  - Redis/PostgreSQL
- 受控重启：
  - 先停所有 source-adapter，包括 compose 固定源和 `video-analytics-source-*`
    动态源。
  - 停 event/face/clip/media workers。
  - 创建新的 runtime epoch，并重建 `video-file-sink`。
  - 删除 `security.frame_annotations` Redis stream，重置 frame annotation
    cache。该 stream 是按帧派生缓存，不是事件/evidence 审计数据。
  - 重启 `replay-service`。
  - 重启 `savant-security`。
  - 恢复 workers。
  - 最后按当前数据库配置恢复固定源和动态 RTSP 源。

入口：

```text
POST /api/v1/cameras/runtime/restart
```

该接口通过 8090 proxy 调用：

```bash
curl --noproxy '*' -X POST http://0.0.0.0:8090/api/v1/cameras/runtime/restart
```

8090 页面也提供 `受控重启运行时` 按钮。该按钮只控制推理/录像链路，不重启
8090 管理端。

`POST /api/v1/cameras/runtime/apply` 也已改为走同一套受控顺序；保存摄像头、
区域或规则后触发的 runtime apply 不再只是重启 Savant。

## 诊断命令

检查 Replay 是否仍在拒帧：

```bash
docker logs --since 10m --tail 300 video-analytics-midterm-replay-service 2>&1 \
  | rg 'mismatched routing_id|routing_id_mismatch|Received message with invalid routing ID'
```

检查 Savant 是否重新收到帧标注：

```bash
docker exec video-analytics-midterm-redis \
  redis-cli XREVRANGE security.frame_annotations + - COUNT 3
```

检查最近事件：

```bash
docker exec phase0-postgres psql -U video -d video_analytics -x -c \
"SELECT now() AS db_now,
        max(created_at) AS latest_event_created_at,
        count(*) FILTER (WHERE created_at > now() - interval '5 minutes') AS events_last_5m
 FROM events;"
```

检查最新 evidence：

```bash
find /data/video-analytics/media/evidence -maxdepth 1 -mindepth 1 -type d \
  -printf '%T@ %TY-%Tm-%Td %TH:%TM:%TS %f\n' | sort -nr | head
```

检查 Replay job 到 sink 的服务名解析与连接：

```bash
docker exec video-analytics-midterm-replay-service getent hosts video-file-sink
docker exec video-analytics-midterm-video-file-sink sh -lc \
  'awk "$2 ~ /:1A0A/ || $3 ~ /:1A0A/ {print}" /proc/net/tcp'
find /data/video-analytics/media/replay-sink-output/midterm/epochs \
  -type f -mmin -10 -printf '%T@ %TY-%Tm-%Td %TH:%TM:%TS %s %p\n' \
  | sort -nr | head
```

恢复后，如果 `security.frame_annotations` 已刷新但 `events_last_5m=0`，说明
链路已恢复到 Savant，暂时没有触发规则事件；这和 routing id 拒帧不同。

## 2026-06-11 补充：epoch 与 frame cache

新 runtime epoch 只切换 sink 目录还不够。8090 本次播放故障里，83f0 的 epoch
guard 已通过，但 Replay sink metadata/video 时间轴里出现了重复 PTS 段：

- metadata row 46: `86404866666 -> 4848555555`
- metadata row 2800: `119671600000 -> 1004522222`
- 源 `video.mov` 第一包 PTS 为 `118.667084`

旧 media-worker 裁剪把全文件第一个 metadata PTS 当连续秒表，导致 ffmpeg seek
落到错误位置并输出空 MOV。修正后的规则是：

- `frame_uuid/uuid` 能匹配时优先以 UUID 所在连续段对齐。
- 官方 sink 没保留原始事件 UUID 时，只允许选择单个严格递增 PTS 连续段，重复
  PTS 窗口默认选择最新段。
- ffmpeg 裁剪使用连续段相对时间和 `setpts=PTS-STARTPTS,trim=...`，不再依赖
  源容器 packet PTS 起点。
- media-worker 读取 `security.frame_annotations` 时按 `runtime_epoch_id` 过滤；
  runtime apply/restart 创建新 epoch 时也会删除该 Redis stream，避免旧 epoch
  frame cache 参与新 evidence；返回体和 `.current_epoch.json` 记录
  `redis_frame_cache_streams_reset` / `redis_frame_cache_reset_count` 作为审计字段。
- 如果 routing id 已恢复、Savant 已持续写入新 epoch 的
  `security.frame_annotations`，但新 record request 仍被 clip-worker 标为
  `missing_post_savant_frame_pts_window`，问题不在 Replay 路由，而是
  post-window frame proof 等待太短。post-Savant proof 等待使用独立配置
  `POST_SAVANT_FRAME_PROOF_ATTEMPTS` / `POST_SAVANT_FRAME_PROOF_RETRY_SLEEP_S`，
  当前默认是 30 次、每次 1 秒。

## 2026-06-11 补充：frame UUID、epoch cache 与 offset 结论

本次 8090 播放问题的正确对齐锚是 frame UUID，不是前端时间，也不是只看
packet PTS。新 runtime epoch 也必须隔离 frame cache：只切换 sink 目录不够，
旧 `security.frame_annotations` 里的 frame UUID/PTS 会污染新 evidence。因此
当前实现要求：

- clip-worker 把 event/start/post window 的 frame UUID 和 PTS 写进 Replay job
  labels。
- media-worker 生成 post-Savant evidence 时，优先用 sink metadata 的
  `uuid/frame_uuid` 命中连续段；没有 UUID 时，只能选择单个严格递增 PTS 段。
- media-worker 读取 frame cache 时按 `runtime_epoch_id` 过滤。
- 8090 受控重启/runtime apply 创建新 epoch 时删除 Redis
  `security.frame_annotations`，并在 `.current_epoch.json` 记录
  `redis_frame_cache_streams_reset` / `redis_frame_cache_reset_count`。

对 Replay `offset.seconds` 的校正结论：不要改成负数。现场新样本证明当前
正 offset 语义是“从 anchor keyframe 往前 rewind”，例如
`f208b550-6b34-44ff-a847-3219041349ea`：

- `anchor_keyframe_pts=51796000000`
- `requested_start_pts=47546744444`
- `offset.seconds=4.249255556`
- sink metadata 覆盖 `47578311111..57546600000`
- evidence `raw_clip.mov` 为 10 秒、250 个 H.264 packet
- 8090 Range 读取 `/api/bundles/f208.../media/raw_clip` 返回 HTTP `206`

所以后续若再出现“没有新 evidence”，优先按以下顺序判断：

1. `security.record_requests` 是否有 lag/pending。
2. clip-worker 是否已经创建 Replay job，offset 是否仍为正 rewind 值。
3. sink epoch 目录是否出现 `video.mov` 和 `metadata.json`。
4. media-worker 是否还在等待 sink 稳定或探测长源文件；1 到 2 分钟延迟不等于
   卡死。
5. 只有 sink metadata 不覆盖 requested window 时，才回到 frame UUID/PTS 段
   选择问题排查；不要先改 offset 正负号。
