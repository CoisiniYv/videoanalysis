---
type: glossary
project: video-analytics-midterm
updated: 2026-07-20
tags:
  - glossary
---

# 术语表

## 8090

操作员入口。由 evidence-viewer 提供 UI，并代理 `/api/v1/*` 和 `/media/*`。

## Full preset / 完整预设

`production_t4_40` 或 `local_4090_60`。单 GPU A/B 双分支，包含推理、轨迹、ROI
AdaFace、rolling-cache 和 evidence。只有 T4 预设使用 CUDA MPS。

## Replay

接收全率 RTSP、写 RocksDB，并向 raw fanout 输出。还提供兼容 Replay job API。完整
预设的 evidence 主物化来自 rolling segment，不是每事件 Replay job。

## replay-raw-fanout

Replay 后的原始分发器。完整预设中同时把采样帧发给 Savant、把全率编码帧 PUB 给
rolling sink。

## analysis-forwarder

单分支/推理-only 的 PTS/FPS sampler。它不提供最终原始证据视频。

## rolling-cache-sink

每 source/session 的 H.264 passthrough GStreamer sink。按 epoch/source 发布原子 MOV
fragment，并暴露 health/ready/metrics。

## 原始 PTS / mux PTS

原始 Savant PTS 用于事件、轨迹和标注；`rolling_cache_mux_pts` 用于稳定 MOV 封装、
segment 和裁剪。二者由 frame identity 映射，不能互换。

## Savant

DeepStream 推理主干：Pose、tracker、behavior rules、Face、ROI/annotation exporters。
完整预设把 AdaFace embedding 移到外置 ROI worker。

## event-worker

消费事件、执行 cooldown、写 event/task/alert。完整预设 suppress record request。

## person-observation-worker

独立消费高率人体轨迹并批量持久化，避免 event policy 饿死 person stream。

## adaface-roi-worker

消费 112x112 ROI JPEG，使用 TensorRT batch 16 生成 512 维 embedding。

## face-worker

持久化 face observation、查询图库、写 match/watchlist event。当前默认查询后端是
pgvector；Qdrant 是可选 derived index。

## clip-worker

单分支/兼容路径的 record request、proof、pure plan、Replay admission/create
coordinator。使用 Replay owner/token/generation fencing。

## media-worker

Evidence Scheduler V2：segment index、bounded image/remux/finalizer lanes、DB pool、
lease/fence/handoff、进程 finalizer、原子 publish、DB index 和 cleanup recovery。

## materialization status / phase

Status 表示任务大类，phase 表示运行阶段。词汇以
`libs/evidence_lifecycle/contract.py` 为准；`materialization_deferred` 是终态。

## read pin

media-worker 在读取 rolling segment 时建立的身份/TTL 保护，防止 retention 在处理中删除
文件。

## cleanup_pending

Evidence 已 durable terminal，但 source artifact 清理失败的可恢复状态，不允许通过全表
扫描长期轮询；migration 032 提供热路径索引。

## DB-backed evidence

文件系统保存视频/图片，PostgreSQL 保存 bundle/artifact/timeline/overlay 和状态。8090
查询数据库，不以目录扫描为主。

## runtime epoch / stream session

epoch 隔离一次 runtime apply，session 隔离同 source 的重连/PTS 重置。跨 epoch/session
媒体 fallback 必须 fail closed。

## pressure artifact

绑定 revision、profile、input、硬件和时间窗口的验证输出。旧 artifact 不证明当前代码。

## soak

长时间稳定性与恢复测试，应覆盖真实 RTSP、断流、重连、worker/API restart 和 residual
state。
