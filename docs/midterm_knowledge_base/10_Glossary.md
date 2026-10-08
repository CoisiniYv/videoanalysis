---
type: glossary
project: video-analytics-midterm
updated: 2026-06-29
tags:
  - glossary
---

# 术语表

## 8090

用户操作台和管理入口。通过 evidence-viewer 提供静态 UI，并代理 `/api/v1/*` 到内部 API。

## Replay

全速视频存储和取证时间窗来源。analysis-forwarder 只读取分析分支，clip-worker 通过 Replay job 请求证据片段。

## analysis-forwarder

从 Replay 读取视频帧，按 PTS/FPS 采样后写入 Savant。它负责降 FPS，不负责长期证据存储。

## Savant

基于 DeepStream 的推理模块，当前包含姿态、人脸、行为规则、AdaFace 和 Redis exporters。

## source adapter

将 RTSP 流接入 Replay 的容器。当前支持静态 compose source 和动态 `video-analytics-source-*`。

## event-worker

消费 `security.events`，写 `events`，产生告警和取证请求。

## face-worker

消费 `security.face_observations`，写 `face_observations`，通过 Qdrant/pgvector rollback path 做
gallery/watchlist 查询并产生 watchlist hit。当前注册图库查询已是 Qdrant authoritative，但
observation 入库、规则解析、匹配和事件发布仍在单 consumer loop 中同步串行。

## clip-worker

消费 `security.record_requests`，调用 Replay job，更新 evidence task 状态。

## media-worker

扫描 video-file-sink 输出，校验 raw clip，写 DB-backed evidence index。

## retained evidence

压测中保留的代表性 evidence 样本。系统不承诺事件风暴下全事件物化，而是通过 admission/backpressure
保留预算内高价值证据。

## lifecycle

从事件创建到 evidence 终态完成的总耗时。当前 8 FPS pressure profile 下 media lifecycle p95 约 192 秒。

## queue wait

事件等待 media-worker 物化的时间。当前 8 FPS pressure profile 下 queue wait p95 约 190 秒。

## deadline slack

距离 materialization deadline 的剩余时间。media-worker pacer 根据 slack 决定是否 sleep。

## replay shard

双分支拓扑下 source 到 Replay/video-file-sink 分支的路由。clip-worker 必须按 replay shard 把 Replay job
发到正确分支。

## pressure source

压测生成的模拟 source。pressure source 通过不等于真实 RTSP 长时间生产通过。

## soak

长时间稳定性测试。当前仍缺真实 RTSP mixed-input 8 FPS soak。
