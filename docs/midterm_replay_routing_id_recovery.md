# Midterm Replay Routing ID 故障记录

更新时间：2026-06-11

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

恢复后，如果 `security.frame_annotations` 已刷新但 `events_last_5m=0`，说明
链路已恢复到 Savant，暂时没有触发规则事件；这和 routing id 拒帧不同。
